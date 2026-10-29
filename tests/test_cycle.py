"""Baseline, dedupe, retraction, outbox isolation, new targets, and snapshot safety."""

from __future__ import annotations

import unittest
from tibo_reset_monitor.core.scheduler import deliver_due
from tibo_reset_monitor.core.model import format_utc
from tests.support import SafetyFixture
import copy
from datetime import timedelta

from tests.support import (
    FakeClock,
    ScriptedChannel,
    ScriptedSource,
    TempStoreTest,
    failure,
    make_activity,
    make_event,
    make_post,
    not_modified,
    ok,
    sent,
    snapshot,
    target,
    utc,
)
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.model import Receipt
from tibo_reset_monitor.core.scheduler import run_cycle


def _rows(store):
    return store.conn.execute(
        "SELECT notice_id, channel, target_key, status, idempotency_key, attempts FROM outbox ORDER BY id"
    ).fetchall()


class CycleTest(TempStoreTest):
    def cycle(self, source, channels):
        return run_cycle(store=self.store, source=source, channels=channels, clock=self.clock)

    def test_first_success_is_baseline_and_later_posts_notify_once(self):
        old = make_event("reset-old", [make_post("p-old", "预告", text="历史")])
        new_post = make_post("p-new", "预告", text="今天重置")
        activity = make_activity("p-new", "announce", "reset-new", text="今天重置，完整原文更长一些")
        new = make_event("reset-new", [new_post])
        always_sent = ScriptedChannel("fake", [target("fake", "ops")], lambda *_: sent())
        source = ScriptedSource(
            [
                ok(snapshot([old]), etag='W/"1"'),
                ok(snapshot([old, new], [activity, make_activity("p-old", "announce", "reset-old")]), etag='W/"2"'),
                ok(snapshot([old, new], [activity]), etag='W/"2"'),
            ]
        )
        first = self.cycle(source, [always_sent])
        self.assertEqual(first.baselined, 1)
        self.assertEqual(_rows(self.store), [])
        self.assertIsNotNone(self.store.get_meta("baseline_completed_at"))

        second = self.cycle(source, [always_sent])
        self.assertEqual(second.new_notices, 1)
        self.assertEqual(len(always_sent.sent), 1)
        self.assertEqual(always_sent.sent[0]["message"]["post_key"], "p-new")
        self.assertEqual(always_sent.sent[0]["message"]["text"], "今天重置，完整原文更长一些")
        self.assertEqual(source.etags[1], 'W/"1"')

        third = self.cycle(source, [always_sent])
        self.assertEqual(third.new_notices, 0)
        self.assertEqual(len(always_sent.sent), 1)

    def test_same_post_in_two_feeds_notifies_once(self):
        post = make_post("p1", "补充说明", text="短")
        event = make_event("reset-1", [post])
        activity = make_activity("p1", "amend", "reset-1", text="补充说明的更长正文")
        notices = extract_notices(snapshot([event], [activity]))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].stage, "revision")
        self.assertEqual(notices[0].text, "补充说明的更长正文")
        self.assertEqual(notices[0].stage_label, "补充说明")

        related = make_activity("p-rel", None, None, kind="related", text="无关回复")
        self.assertEqual(extract_notices(snapshot([event], [activity, related])), notices[:1])

    def test_retraction_cancels_unsent_and_keeps_sent(self):
        def deliver(_message, item, _key):
            if item.key == "slow":
                return Receipt("retryable", detail="down")
            return sent("ok-fast")

        channel = ScriptedChannel(
            "fake",
            [target("fake", "slow"), target("fake", "fast")],
            deliver,
        )
        announced = make_event("reset-1", [make_post("p1", "预告")])
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"0"'),
                ok(snapshot([announced], [make_activity("p1", "announce", "reset-1")]), etag='W/"1"'),
                ok(
                    snapshot(
                        [],
                        [make_activity("p2", "withdraw", "reset-1", text="撤回这条重置")],
                    ),
                    etag='W/"2"',
                ),
            ]
        )
        self.cycle(source, [channel])
        self.cycle(source, [channel])
        self.assertEqual(sorted(item["target"] for item in channel.sent), ["fast", "slow"])
        withdrawn = self.cycle(source, [channel])
        self.assertEqual(withdrawn.cancelled, 1)
        rows = _rows(self.store)
        by_target_status = [(row["target_key"], row["status"]) for row in rows]
        self.assertIn(("slow", "cancelled"), by_target_status)
        self.assertIn(("fast", "sent"), by_target_status)
        retraction_rows = [row for row in rows if row["status"] == "sent" and row["target_key"] in {"slow", "fast"}]
        self.assertGreaterEqual(len(retraction_rows), 2)
        retraction_sends = [item for item in channel.sent if item["message"]["stage"] == "retraction"]
        self.assertEqual(sorted(item["target"] for item in retraction_sends), ["fast", "slow"])
        self.assertNotIn("slow", [item["target"] for item in channel.sent if item["message"]["stage"] == "announcement"][1:])

    def test_one_target_failure_does_not_resend_the_other(self):
        def deliver(_message, item, _key):
            if item.key == "broken":
                raise RuntimeError("socket failed after connect")
            return sent("ok")

        channel = ScriptedChannel(
            "fake",
            [target("fake", "broken", "group"), target("fake", "fine", "personal", "100")],
            deliver,
        )
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"0"'),
                ok(snapshot([make_event("reset-1", [make_post("p1", "确认完成")])]), etag='W/"1"'),
                not_modified('W/"1"'),
            ]
        )
        self.cycle(source, [channel])
        self.cycle(source, [channel])
        self.assertEqual([item["target"] for item in channel.sent], ["broken", "fine"])
        self.clock.advance(120)
        self.cycle(source, [channel])
        self.assertEqual([item["target"] for item in channel.sent], ["broken", "fine", "broken"])
        statuses = {(row["target_key"], row["status"]) for row in _rows(self.store)}
        self.assertIn(("fine", "sent"), statuses)
        self.assertIn(("broken", "ambiguous"), statuses)

    def test_new_targets_do_not_receive_history(self):
        channel = ScriptedChannel("fake", [target("fake", "original")], lambda *_: sent())
        first_post = make_event("reset-1", [make_post("p1", "预告")])
        second_post = make_event("reset-2", [make_post("p2", "预告", text="第二条")])
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"0"'),
                ok(snapshot([first_post]), etag='W/"1"'),
                not_modified('W/"1"'),
                ok(snapshot([first_post, second_post]), etag='W/"2"'),
            ]
        )
        self.cycle(source, [channel])
        self.cycle(source, [channel])
        channel.targets.append(target("fake", "late", "personal", "200"))
        self.cycle(source, [channel])
        self.assertEqual([item["target"] for item in channel.sent], ["original"])
        self.cycle(source, [channel])
        posts_for_late = [item["message"]["post_key"] for item in channel.sent if item["target"] == "late"]
        self.assertEqual(posts_for_late, ["p2"])
        posts_for_original = [item["message"]["post_key"] for item in channel.sent if item["target"] == "original"]
        self.assertEqual(posts_for_original, ["p1", "p2"])

    def test_failed_fetch_does_not_replace_snapshot_and_still_retries(self):
        calls = {"n": 0}

        def deliver(*_args):
            calls["n"] += 1
            if calls["n"] == 1:
                return Receipt("retryable", detail="temporary")
            return sent()

        channel = ScriptedChannel("fake", [target("fake", "ops")], deliver)
        good = snapshot([make_event("reset-1", [make_post("p1", "预告")])], checked_at="2026-10-28T12:00:00.000+08:00")
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"0"'),
                ok(good, etag='W/"good"'),
                failure("malformed", "events must be an array"),
                failure("rate_limit", "slow down", retry_after=30),
                not_modified('W/"good"'),
            ]
        )
        self.cycle(source, [channel])
        self.cycle(source, [channel])
        saved = self.store.get_snapshot()
        assert saved is not None
        self.assertEqual(saved["etag"], 'W/"good"')
        self.assertEqual(saved["body"]["checkedAt"], "2026-10-28T12:00:00.000+08:00")
        self.cycle(source, [channel])
        self.assertEqual(self.store.get_snapshot()["body"], saved["body"])
        self.assertEqual(self.store.get_meta("last_fetch_error"), "events must be an array")
        self.cycle(source, [channel])
        self.assertEqual(self.store.get_snapshot()["etag"], 'W/"good"')
        self.clock.advance(120)
        report = self.cycle(source, [channel])
        self.assertTrue(report.not_modified)
        self.assertEqual(self.store.get_snapshot()["body"]["checkedAt"], "2026-10-28T12:00:00.000+08:00")
        self.assertEqual([item["target"] for item in channel.sent], ["ops", "ops"])
        self.assertEqual(_rows(self.store)[0]["status"], "sent")

    def test_ambiguous_timeout_reuses_key_then_needs_review(self):
        keys = []

        def deliver(_message, _target, key):
            keys.append(key)
            return Receipt("ambiguous", detail="timeout")

        channel = ScriptedChannel("fake", [target("fake", "ops")], deliver)
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"0"'),
                ok(snapshot([make_event("reset-1", [make_post("p1", "预告")])]), etag='W/"1"'),
                not_modified(),
                not_modified(),
            ]
        )
        self.cycle(source, [channel])
        self.cycle(source, [channel])
        self.assertEqual(len(keys), 1)
        self.clock.advance(10 * 60)
        self.cycle(source, [channel])
        self.assertEqual(keys, [keys[0], keys[0]])
        self.assertLessEqual(len(keys[0]), 50)
        self.clock.advance(50 * 60)
        report = self.cycle(source, [channel])
        self.assertEqual(len(keys), 2)
        self.assertEqual(report.deliveries[0]["status"], "needs_review")
        self.assertEqual(_rows(self.store)[0]["status"], "needs_review")

    def test_inflight_without_receipt_retries_with_the_same_key(self):
        channel = ScriptedChannel("fake", [target("fake", "ops")], lambda *_: sent("later"))
        source = ScriptedSource([not_modified('W/"same"')])
        body = snapshot([make_event("reset-1", [make_post("p9", "预告")])])
        now = self.clock.now()
        with self.store.transaction():
            self.store.baseline([], body, 'W/"same"', now)
        notice = extract_notices(snapshot([make_event("reset-1", [make_post("p9", "预告")])]))[0]
        with self.store.transaction():
            self.store.ingest([notice], channel.targets, body, 'W/"same"', now)
        self.store.conn.execute("UPDATE outbox SET status = 'inflight', attempts = 1")
        self.store.conn.commit()
        original = _rows(self.store)[0]["idempotency_key"]
        self.cycle(source, [channel])
        self.assertEqual(channel.sent[0]["key"], original)
        self.assertEqual(_rows(self.store)[0]["status"], "sent")

    def test_failed_first_fetch_does_not_baseline(self):
        channel = ScriptedChannel("fake", [target("fake", "ops")], lambda *_: sent())
        source = ScriptedSource(
            [
                failure("network", "connection reset"),
                ok(snapshot([make_event("reset-1", [make_post("p1", "预告")])]), etag='W/"1"'),
            ]
        )
        first = self.cycle(source, [channel])
        self.assertEqual(first.fetch_error_kind, "network")
        self.assertIsNone(self.store.get_snapshot())
        second = self.cycle(source, [channel])
        self.assertEqual(second.baselined, 1)
        self.assertEqual(channel.sent, [])


class StageMapTest(unittest.TestCase):
    def test_confirmation_and_unknown_stage(self):
        event = make_event(
            "reset-1",
            [make_post("p1", "确认完成", text="已经重置")],
            title="Tibo 确认重置",
            status="confirmed",
        )
        notices = extract_notices(snapshot([event], [make_activity("p1", "confirm", "reset-1", text="已经重置")]))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].stage, "confirmation")
        self.assertEqual(notices[0].stage_label, "确认完成")
        unknown = extract_notices(snapshot([make_event("reset-2", [make_post("p2", "观察")])]) )
        self.assertEqual(unknown[0].stage, "other:观察")


class ClockSanityTest(unittest.TestCase):
    def test_fake_clock_advances(self):
        clock = FakeClock(utc())
        clock.advance(1)
        self.assertEqual(clock.now().second, 1)


class SourceSafetyTest(TempStoreTest):
    """Synthetic backfill and failure recovery: no production data or network."""

    def setUp(self):
        super().setUp()
        self.clock.now_value = utc(hour=4)
        self.channel = ScriptedChannel("fake", [target("fake", "oc_virtual")], lambda *_: sent())

    def cycle(self, body=None, result=None):
        return run_cycle(store=self.store, source=ScriptedSource([result or ok(body)]),
                         channels=[self.channel], clock=self.clock)

    def body(self, identity="virtual-post", published="2026-10-28T04:00:01Z", stage="预告"):
        post = make_post(identity, stage)
        post["publishedAt"] = published
        return snapshot([make_event("virtual-event", [post])])

    def baseline(self):
        self.cycle(snapshot([]))
        self.clock.advance(2)

    def test_historical_backfill_after_empty_baseline_is_not_a_new_post(self):
        self.baseline()
        body = self.body(published="2026-07-01T00:00:00Z")
        body["events"][0]["updatedAt"] = "2026-10-28T04:00:02Z"
        self.cycle(body)
        self.assertEqual(self.channel.sent, [])

    def test_healthy_checks_reject_late_backfill_but_accept_out_of_order_new_posts(self):
        self.baseline()
        self.clock.advance(3 * 3600)
        self.cycle(snapshot([]))
        self.cycle(self.body("old-backfill"))
        self.assertEqual(self.channel.sent, [])
        self.cycle(self.body("newer", "2026-10-28T07:00:00Z"))
        self.cycle(self.body("out-of-order", "2026-10-28T06:59:30Z"))
        self.assertEqual([item["message"]["post_key"] for item in self.channel.sent], ["newer", "out-of-order"])

    def test_failure_and_restart_block_cached_delivery_until_valid_304(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        self.cycle(result=failure("network", "unavailable"))
        self.assertEqual(len(self.channel.sent), 1)
        self.store.close()
        from tibo_reset_monitor.core.store import StateStore
        self.store = StateStore(self.path("state.sqlite3"))
        self.addCleanup(self.store.close)
        from tibo_reset_monitor.core.scheduler import deliver_due
        deliver_due(self.store, [self.channel], self.clock)
        self.assertEqual(len(self.channel.sent), 1)
        self.channel._send = lambda *_: sent()
        self.cycle(result=not_modified())
        self.assertEqual(len(self.channel.sent), 2)

    def test_unexpected_source_exception_does_not_release_old_queue(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        class BrokenSource:
            def fetch(self, _etag):
                raise RuntimeError("synthetic parse failure")
        report = run_cycle(store=self.store, source=BrokenSource(), channels=[self.channel], clock=self.clock)
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual(len(self.channel.sent), 1)

    def test_stage_reclassification_does_not_resend_a_delivered_post(self):
        self.baseline()
        self.cycle(self.body())
        self.cycle(self.body(stage="确认完成"))
        self.assertEqual(len(self.channel.sent), 1)

    def test_conflicting_stages_reject_the_whole_response(self):
        self.baseline()
        body = self.body()
        body["activities"] = [make_activity("virtual-post", "confirm", "virtual-event")]
        body["activities"][0]["publishedAt"] = "2026-10-28T04:00:01Z"
        report = self.cycle(body)
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual(self.channel.sent, [])

    def test_legal_retry_retains_original_boundary_after_later_health_checks(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        key = self.channel.sent[0]["key"]
        self.clock.advance(2 * 3600)
        self.channel._send = lambda *_: sent()
        self.cycle(self.body())
        self.assertEqual([item["key"] for item in self.channel.sent], [key, key])

    def test_removed_source_does_not_send_a_pending_post(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        self.channel._send = lambda *_: sent()
        self.cycle(snapshot([]))
        self.assertEqual(len(self.channel.sent), 1)

    def test_304_without_a_cached_snapshot_is_not_success(self):
        report = self.cycle(result=not_modified())
        self.assertIsNotNone(report.fetch_error)
        self.assertFalse(self.store.is_baselined())

    def test_process_interruption_persists_block_and_valid_304_recovers(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        class Interrupted:
            def fetch(self, _etag):
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_cycle(store=self.store, source=Interrupted(), channels=[self.channel], clock=self.clock)
        self.store.close()
        from tibo_reset_monitor.core.store import StateStore
        from tibo_reset_monitor.core.scheduler import deliver_due
        self.store = StateStore(self.path("state.sqlite3"))
        self.addCleanup(self.store.close)
        deliver_due(self.store, [self.channel], self.clock)
        self.assertEqual(len(self.channel.sent), 1)
        self.cycle(result=not_modified())
        self.assertEqual(len(self.channel.sent), 2)

    def test_conflict_is_rejected_even_for_pending_post_older_than_overlap(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(2 * 3600)
        self.cycle(self.body())
        body = self.body()
        activity = make_activity("virtual-post", "confirm", "virtual-event")
        activity["publishedAt"] = "2026-10-28T04:00:01Z"
        body["activities"] = [activity]
        report = self.cycle(body)
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual(len(self.channel.sent), 2)

    def test_future_post_at_tolerance_is_allowed_and_beyond_it_is_rejected(self):
        self.baseline()
        self.cycle(self.body("edge", "2026-10-28T04:05:02Z"))
        report = self.cycle(self.body("beyond", "2026-10-28T04:05:03Z"))
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual([item["message"]["post_key"] for item in self.channel.sent], ["edge"])

    def test_changed_target_is_held_instead_of_delivering_old_work_to_new_address(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        self.channel.targets = [target("fake", "oc_virtual", address="different-virtual-address")]
        self.cycle(result=not_modified())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(_rows(self.store)[0]["status"], "needs_review")

    def test_attempted_reclassification_keeps_key_and_original_body(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        original = self.channel.sent[0]
        self.clock.advance(60)
        changed = self.body(stage="确认完成")
        changed["events"][0]["posts"][0]["publishedAt"] = "2026-10-28T09:30:01+05:30"
        changed["events"][0]["posts"][0]["fullText"] = "changed source text"
        self.channel._send = lambda *_: sent()
        self.cycle(changed)
        self.assertEqual(len(self.channel.sent), 2)
        self.assertEqual(self.channel.sent[1]["key"], original["key"])
        self.assertEqual(self.channel.sent[1]["message"], original["message"])

    def test_unknown_receipt_is_ambiguous_and_does_not_block_other_target(self):
        self.baseline()
        self.channel.targets.append(target("fake", "second-virtual"))
        self.channel._send = lambda _message, dest, _key: [] if dest.key == "oc_virtual" else sent()
        self.cycle(self.body())
        self.assertEqual([row["status"] for row in _rows(self.store)], ["ambiguous", "sent"])

    def test_retry_freezes_rendered_text_and_calendar_bytes(self):
        self.baseline()
        photo = [b"virtual-original-calendar"]
        rendered = []
        def render(notice, target):
            rendered.append(True)
            return {"post_key": notice.post_key, "photo": photo[0], "text": "first rendering"}
        self.channel.render = render
        self.channel._send = lambda *_: Receipt("ambiguous", detail="unknown")
        self.cycle(self.body())
        original = self.channel.sent[0]
        photo[0] = b"different-calendar"
        self.clock.advance(120)
        self.channel._send = lambda *_: sent()
        self.cycle(result=not_modified())
        self.assertEqual(len(rendered), 1)
        self.assertEqual(self.channel.sent[1], original)

    def test_non_idempotent_provider_does_not_retry_an_unknown_send(self):
        self.baseline()
        self.channel.supports_idempotency = False
        self.channel._send = lambda *_: Receipt("ambiguous", detail="unknown")
        self.cycle(self.body())
        self.clock.advance(120)
        self.cycle(result=not_modified())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(_rows(self.store)[0]["status"], "needs_review")

    def test_manual_row_does_not_drain_automatic_queue(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        historical = self.body("historical", "2026-07-01T00:00:00Z")
        notice = extract_notices(historical)[0]
        manual = self.store.insert_manual(notice, self.channel.targets[0], self.clock.now(), salt="virtual-explicit-request")
        self.channel._send = lambda *_: sent()
        from tibo_reset_monitor.core.scheduler import deliver_due
        deliver_due(self.store, [self.channel], self.clock, only_row_id=manual.id)
        self.assertEqual([entry["message"]["post_key"] for entry in self.channel.sent], ["virtual-post", "historical"])
        self.assertEqual(_rows(self.store)[0]["status"], "retryable")

    def test_unverifiable_legacy_target_is_held_for_review(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.store.conn.execute("UPDATE outbox SET automatic_after = NULL, target_address = NULL, message_json = NULL")
        self.clock.advance(60)
        self.cycle(result=not_modified())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(_rows(self.store)[0]["status"], "needs_review")

    def test_duplicate_legacy_classifications_do_not_send_twice(self):
        self.baseline()
        self.cycle(self.body())
        duplicate = extract_notices(self.body(stage="确认完成"))[0]
        identity = self.store._insert_notice(duplicate, self.clock.now())
        from dataclasses import replace
        self.store._enqueue(replace(duplicate, id=identity), self.channel.targets[0], self.clock.now())
        self.cycle(self.body(stage="确认完成"))
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(_rows(self.store)[1]["status"], "needs_review")

    def test_failures_do_not_advance_the_successful_check_boundary(self):
        self.baseline()
        self.clock.advance(3 * 3600)
        self.cycle(result=failure("network", "offline"))
        self.cycle(self.body("during-offline", "2026-10-28T04:30:00Z"))
        self.assertEqual([entry["message"]["post_key"] for entry in self.channel.sent], ["during-offline"])

    def test_seven_day_limit_rejects_old_post_even_after_offline_period(self):
        self.baseline()
        self.clock.advance(8 * 24 * 3600)
        self.cycle(self.body())
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(len(self.store.list_notices(10)), 1)

    def test_invalid_cached_304_cannot_release_pending_work(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="offline")
        self.cycle(self.body())
        self.clock.advance(60)
        self.store.conn.execute("UPDATE snapshot SET body_json = ?", ('{"events":"bad"}',))
        report = self.cycle(result=not_modified())
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual(len(self.channel.sent), 1)

    def test_success_watermark_is_monotonic_and_large_clock_rollback_blocks(self):
        self.baseline()
        self.clock.advance(240)
        self.cycle(self.body())
        checkpoint = self.store.get_meta("last_success_at")
        self.clock.advance(-240)
        self.cycle(result=not_modified())
        self.assertEqual(self.store.get_meta("last_success_at"), checkpoint)
        self.clock.now_value = utc(hour=3, minute=58)
        report = self.cycle(result=not_modified())
        self.assertIsNotNone(report.fetch_error)
        self.assertEqual(self.store.get_meta("last_success_at"), checkpoint)

    def test_future_source_window_does_not_establish_a_baseline(self):
        body = snapshot([])
        body["historyFrom"] = "2026-12-01T00:00:00Z"
        report = self.cycle(body)
        self.assertIsNotNone(report.fetch_error)
        self.assertFalse(self.store.is_baselined())

    def test_concurrent_claim_has_one_winner(self):
        import threading
        from tibo_reset_monitor.core.store import StateStore
        row = self.store.insert_manual(extract_notices(self.body())[0], self.channel.targets[0], self.clock.now(), salt="virtual-race")
        barrier = threading.Barrier(2)
        winners, errors = [], []
        def worker():
            state = None
            try:
                state = StateStore(self.store.path)
                barrier.wait(timeout=5)
                winners.append(state.claim(row.id, self.clock.now()))
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                if state:
                    state.close()
        workers = [threading.Thread(target=worker) for _ in range(2)]
        for item in workers:
            item.start()
        for item in workers:
            item.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(winners), [False, True])

    def test_unserializable_render_does_not_count_as_a_send_attempt(self):
        self.baseline()
        self.channel.targets.append(target("fake", "good-virtual"))
        self.channel.render = lambda notice, dest: {"text": object()} if dest.key == "oc_virtual" else {"text": "virtual"}
        self.cycle(self.body())
        rows = _rows(self.store)
        self.assertEqual((rows[0]["attempts"], rows[0]["status"]), (0, "pending"))
        self.assertEqual((rows[1]["attempts"], rows[1]["status"]), (1, "sent"))


class AdversarialDeliveryTest(SafetyFixture):
    def test_reclassification_does_not_replace_the_queued_source_identity(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="synthetic offline")
        self.cycle(self.body())
        changed = self.body(stage="确认完成", published="2026-10-28T04:00:02Z")
        changed["events"][0]["posts"][0]["url"] = "https://x.com/thsottiaux/status/virtual-other"
        self.clock.advance(60)
        self.channel._send = lambda *_: sent()
        self.cycle(changed)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.store.read_row(1).state.status, "needs_review")

    def test_another_process_waits_for_an_active_send_lease(self):
        from tibo_reset_monitor.core.dedupe import extract_notices
        self.baseline()
        self.channel.supports_idempotency = False
        row = self.store.insert_manual(extract_notices(self.body())[0], self.channel.targets[0],
                                       self.clock.now(), salt="virtual-leased-orphan")
        self.store.claim(row.id, self.clock.now(), lease_until=self.clock.now() + timedelta(seconds=600))
        self.store.conn.commit()
        self.cycle(snapshot([]))
        self.assertEqual(self.store.read_row(row.id).state.status, "inflight")
        self.assertEqual(self.channel.sent, [])
        self.clock.advance(600)
        self.cycle(snapshot([]))
        recovered = self.store.read_row(row.id)
        self.assertEqual(recovered.state.status, "needs_review")
        self.assertEqual(recovered.idempotency_key, row.idempotency_key)
        self.assertEqual(self.channel.sent, [])

    def test_an_already_processed_retraction_does_not_cancel_later_confirmation(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="virtual offline")
        self.cycle(self.body())
        withdrawn = self.body(stage="撤回", identity="virtual-withdrawal")
        self.clock.advance(1)
        self.cycle(withdrawn)
        confirmed = self.body(stage="确认完成", identity="virtual-later-confirmation",
                              published="2026-10-28T04:00:04Z")
        confirmed["events"][0]["posts"].append(withdrawn["events"][0]["posts"][0])
        self.clock.advance(1)
        self.channel._send = lambda *_: sent()
        self.cycle(confirmed)
        self.assertTrue(any(item["message"]["stage"] == "confirmation" for item in self.channel.sent))

    def test_missing_publication_must_reject_without_poisoning_seen(self):
        self.baseline()
        body = self.body()
        del body["events"][0]["posts"][0]["publishedAt"]
        before = self.store.get_snapshot()
        report = self.cycle(body)
        actual = {"error": report.fetch_error,
                  "snapshot_unchanged": self.store.get_snapshot() == before,
                  "seen": self.store.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]}
        self.assertIsNotNone(report.fetch_error, actual)
        self.assertEqual(self.store.get_snapshot(), before)

    def test_corrected_publication_recovers_without_permanent_suppression(self):
        self.baseline()
        bad = self.body()
        bad["events"][0]["posts"][0]["publishedAt"] = None
        first = self.cycle(bad)
        second = self.cycle(self.body())
        self.assertEqual(len(self.channel.sent), 1,
                         {"bad_error": first.fetch_error, "good_error": second.fetch_error,
                          "notices": self.store.list_notices(10), "sends": self.channel.sent})

    def test_same_post_reclassified_to_withdrawal_cancels_old_attempt(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="synthetic offline")
        self.cycle(self.body())
        self.clock.advance(60)
        self.channel._send = lambda *_: sent()
        report = self.cycle(self.body(stage="撤回"))
        actual = {"cancelled": report.cancelled, "sends": self.channel.sent,
                  "counts": self.store.outbox_counts()}
        self.assertEqual(len(self.channel.sent), 1, actual)
        self.assertEqual(self.store.outbox_counts().get("cancelled"), 1, actual)

    def test_retraction_outside_new_notice_overlap_still_cancels_pending_event(self):
        self.baseline()
        self.channel._send = lambda *_: Receipt("retryable", detail="synthetic offline")
        original = self.body()
        self.cycle(original)
        self.clock.advance(3 * 3600)
        self.cycle(original)
        # A valid success at 07:00 establishes a 06:00 new-post cutoff.
        # Retraction at 05:00 belongs to the current event but is a historical notice.
        post = make_post("virtual-withdrawal", "撤回")
        post["publishedAt"] = "2026-10-28T05:00:00Z"
        changed = copy.deepcopy(original)
        changed["events"][0]["posts"].append(post)
        self.clock.advance(60)
        self.channel._send = lambda *_: sent()
        before = len(self.channel.sent)
        report = self.cycle(changed)
        self.assertEqual(len(self.channel.sent), before,
                         {"report": report.__dict__, "sends": self.channel.sent,
                          "counts": self.store.outbox_counts()})

    def test_conflicting_same_stage_post_across_events_must_not_replace_cache(self):
        self.baseline()
        first = self.body()
        second = copy.deepcopy(first["events"][0])
        second["id"] = "virtual-second-event"
        second["posts"][0]["publishedAt"] = "2026-10-28T04:00:02Z"
        second["posts"][0]["url"] = "https://x.com/thsottiaux/status/virtual-other"
        first["events"].append(second)
        first["count"] = 2
        report = self.cycle(first)
        self.assertIsNotNone(report.fetch_error,
                             {"error": report.fetch_error, "sends": self.channel.sent})

    def test_reclassification_refreshes_unattempted_target_independently(self):
        self.channel.targets.append(target("fake", "virtual-unattempted", address="other-virtual-address"))
        original_render = self.channel.render
        fail_second = True

        def render(notice, destination):
            if fail_second and destination.key == "virtual-unattempted":
                raise ValueError("synthetic pre-send render failure")
            return original_render(notice, destination)

        self.channel.render = render
        self.baseline()
        self.cycle(self.body())
        second_row = self.store.conn.execute("SELECT * FROM outbox WHERE target_key = 'virtual-unattempted'").fetchone()
        self.assertEqual(second_row["attempts"], 0)
        fail_second = False
        self.clock.advance(60)
        self.cycle(self.body(stage="确认完成"))
        latest = self.channel.sent[-1]
        self.assertEqual(latest["target"], "virtual-unattempted")
        self.assertEqual(latest["message"]["stage"], "confirmation",
                         {"second_target": latest, "all_sends": self.channel.sent})

    def test_active_manual_send_is_not_recovered_as_an_orphan_by_a_poll(self):
        from tibo_reset_monitor.core.dedupe import extract_notices
        from tibo_reset_monitor.core.scheduler import deliver_due
        self.baseline()
        self.channel.supports_idempotency = False
        nested = False

        def send_during_poll(*_):
            nonlocal nested
            if not nested:
                nested = True
                self.cycle(snapshot([]))
            return sent("virtual-ack-after-concurrent-poll")

        self.channel._send = send_during_poll
        row = self.store.insert_manual(extract_notices(self.body())[0], self.channel.targets[0],
                                       self.clock.now(), salt="virtual-concurrent-manual")
        deliver_due(self.store, [self.channel], self.clock, only_row_id=row.id)
        actual = self.store.read_row(row.id).state
        self.assertEqual(actual.status, "sent", actual.__dict__)

    def test_trusted_chat_migration_keeps_unattempted_pending_delivery_usable(self):
        self.channel = ScriptedChannel("telegram", [], lambda *_: sent())
        self.store.subscribe(channel="telegram", kind="group", address="-10042",
                             display_name="Virtual migrated group", now=self.clock.now())
        self.baseline()
        original_render = self.channel.render
        self.channel.render = lambda *_: (_ for _ in ()).throw(ValueError("synthetic render unavailable"))
        self.cycle(self.body())
        queued = self.store.due_rows(self.clock.now() + timedelta(seconds=60))[0]
        self.assertEqual(queued.state.attempts, 0)
        self.store.migrate_chat("telegram", "-10042", "-10099", self.clock.now())
        self.channel.render = original_render
        self.clock.advance(60)
        report = self.cycle(self.body())
        actual = self.store.read_row(queued.id)
        self.assertEqual(actual.state.status, "sent",
                         {"status": actual.state.status, "error": actual.state.last_error,
                          "stored_address": actual.target_address,
                          "current_address": self.store.subscription_targets("telegram", "zh", "UTC")[0].address,
                          "report": report.deliveries})


class SourceLifecycleRegressionTest(SafetyFixture):
    def hold_render(self):
        original = self.channel.render
        self.channel.render = lambda *_: (_ for _ in ()).throw(ValueError('synthetic render unavailable'))
        return original

    def test_multi_event_withdrawal_cancels_every_associated_event(self):
        self.baseline()
        first = self.body(identity='r2-first')
        second_event = copy.deepcopy(first['events'][0])
        second_event['id'] = 'r2-second-event'
        second_event['posts'][0]['id'] = 'r2-second'
        second_event['posts'][0]['url'] = 'https://x.com/thsottiaux/status/r2-second'
        first['events'].append(second_event)
        first['count'] = 2
        self.channel._send = lambda *_: Receipt('retryable', detail='synthetic reject')
        self.cycle(first)
        self.clock.advance(60)
        withdrawal = make_activity('r2-withdrawal', 'withdraw', 'virtual-event')
        withdrawal['eventIds'] = ['virtual-event', 'r2-second-event']
        withdrawal['publishedAt'] = format_utc(self.clock.now())
        changed = copy.deepcopy(first)
        changed['activities'] = [withdrawal]
        self.channel._send = lambda *_: sent()
        report = self.cycle(changed)
        actual = [dict(row) for row in self.store.conn.execute('SELECT n.post_key, o.status FROM outbox o JOIN notices n ON n.id=o.notice_id ORDER BY o.id')]
        statuses = {row['post_key']: row['status'] for row in actual}
        self.assertEqual(statuses['r2-first'], 'cancelled', actual)
        self.assertEqual(statuses['r2-second'], 'cancelled', actual)

    def test_manual_unattempted_delivery_keeps_explicitly_selected_content(self):
        self.baseline()
        original_render = self.hold_render()
        original = self.body()
        original['events'][0]['posts'][0].update(text='r2-original', fullText='r2-original')
        self.cycle(original)
        notice = self.store.get_notice(self.store.list_notices(1)[0]['id'])
        row = self.store.enqueue_existing(notice, self.channel.targets[0], self.clock.now(), salt='r2-explicit')
        deliver_due(self.store, [self.channel], self.clock, only_row_id=row.id)
        self.assertEqual(self.store.read_row(row.id).state.attempts, 0)
        changed = self.body(stage='确认完成')
        changed['events'][0]['posts'][0].update(text='r2-source-changed', fullText='r2-source-changed')
        self.clock.advance(60)
        self.channel.render = original_render
        self.cycle(changed)
        actual = self.channel.sent[-1]
        self.assertEqual(actual['message']['text'], 'r2-original', actual)
        self.assertEqual(actual['message']['stage'], 'announcement', actual)

    def test_missing_stage_rejects_without_cancelling_valid_pending(self):
        self.baseline()
        original_render = self.hold_render()
        good = self.body()
        self.cycle(good)
        before = self.store.get_snapshot()
        bad = copy.deepcopy(good)
        del bad['events'][0]['posts'][0]['stage']
        self.clock.advance(60)
        report = self.cycle(bad)
        after_bad = self.store.outbox_counts()
        cached_after_bad = self.store.get_snapshot()
        self.channel.render = original_render
        self.clock.advance(60)
        restored = self.cycle(good)
        self.assertIsNotNone(report.fetch_error, {'error': report.fetch_error, 'counts': after_bad})
        self.assertEqual(len(self.channel.sent), 1)

    def test_null_posts_rejects_without_cancelling_valid_pending(self):
        self.baseline()
        original_render = self.hold_render()
        good = self.body()
        self.cycle(good)
        bad = copy.deepcopy(good)
        bad['events'][0]['posts'] = None
        self.clock.advance(60)
        report = self.cycle(bad)
        after_bad = self.store.outbox_counts()
        self.channel.render = original_render
        self.clock.advance(60)
        self.cycle(good)
        self.assertIsNotNone(report.fetch_error, {'error': report.fetch_error, 'counts': after_bad})
        self.assertEqual(len(self.channel.sent), 1)


class SourceIdentityRecoveryTest(SafetyFixture):
    def test_merged_post_keeps_all_event_links_for_withdrawal(self):
        self.baseline()
        body = self.body()
        other = copy.deepcopy(body["events"][0])
        other["id"] = "virtual-second-event"
        body["events"].append(other)
        body["count"] = 2
        self.channel._send = lambda *args: Receipt("retryable")
        self.cycle(body)
        self.assertEqual(set(self.store.read_row(1).notice.event_ids), {"virtual-event", "virtual-second-event"})
        withdrawal = make_activity("virtual-linked-withdrawal", "withdraw", "virtual-second-event")
        body["activities"] = [withdrawal]
        self.clock.advance(60)
        self.channel._send = lambda *args: sent()
        self.cycle(body)
        self.assertEqual(self.store.read_row(1).state.status, "cancelled")

    def test_manual_notice_snapshot_survives_restart_and_source_changes(self):
        from tibo_reset_monitor.core.store import StateStore
        self.baseline()
        self.cycle(self.body())
        original = self.store.get_notice(1)
        manual = self.store.enqueue_existing(original, self.channel.targets[0], self.clock.now(), salt="virtual-manual")
        self.cycle(self.body(stage="确认完成"))
        restarted = StateStore(self.store.path)
        try:
            current = restarted.read_row(manual.id)
            self.assertFalse(current.automatic)
            self.assertEqual(current.notice.to_json(), original.to_json())
        finally:
            restarted.close()

    def test_outbox_old_ack_cannot_overwrite_a_newer_claim(self):
        from tibo_reset_monitor.core.model import DeliveryState
        self.baseline()
        self.channel.render = lambda *args: (_ for _ in ()).throw(ValueError("synthetic rendering hold"))
        self.cycle(self.body())
        self.clock.advance(60)
        self.assertTrue(self.store.claim(1, self.clock.now()))
        self.store.recover_inflight({1: DeliveryState("retryable", 1, None, None, None, None, None)}, self.clock.now())
        self.assertTrue(self.store.claim(1, self.clock.now()))
        self.assertFalse(self.store.apply_state(1, DeliveryState("sent", 1, None, None, None, "virtual-late-ack", None), self.clock.now()))
        current = self.store.read_row(1)
        self.assertEqual((current.state.status, current.state.attempts), ("inflight", 2))

    def test_incomplete_source_identity_fields_reject_before_reconciliation(self):
        from tibo_reset_monitor.core.source import SnapshotError, validate_snapshot
        cases = [("post", "stage", None), ("post", "stage", " "),
                 ("event", "posts", None), ("event", "posts", "missing"),
                 ("activity", "kind", None), ("activity", "action", None),
                 ("activity", "eventIds", None), ("activity", "eventIds", [""])]
        for level, field, value in cases:
            with self.subTest(level=level, field=field, value=value):
                body = self.body()
                body["activities"] = [make_activity("virtual-activity", "confirm", "virtual-event")]
                record = {"event": body["events"][0], "post": body["events"][0]["posts"][0],
                          "activity": body["activities"][0]}[level]
                if value == "missing":
                    del record[field]
                else:
                    record[field] = value
                with self.assertRaises(SnapshotError):
                    validate_snapshot(body, self.clock.now())


if __name__ == "__main__":
    unittest.main()
