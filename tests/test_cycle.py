"""Baseline, dedupe, retraction, outbox isolation, new targets, and snapshot safety."""

from __future__ import annotations

import unittest

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
        self.assertIn(("broken", "retryable"), statuses)

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
        body = snapshot([])
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


if __name__ == "__main__":
    unittest.main()
