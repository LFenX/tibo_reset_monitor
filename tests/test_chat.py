"""Chat control: pairing, permissions, subscriptions, and the group picker."""

from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tests.support import FakeClock, TempStoreTest, make_event, make_post, snapshot
from tibo_reset_monitor.channels import REGISTRY
from tibo_reset_monitor.core.chat import ChatController, Inbound, PairingGate
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.model import DeliveryState, Notice, Receipt, Target
from tibo_reset_monitor.core.scheduler import effective_targets
from tibo_reset_monitor.core.store import StateStore


NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def _inbound(channel="telegram", **overrides) -> Inbound:
    payload = dict(
        channel=channel,
        event_id="e1",
        scope="personal",
        chat_id="42",
        user_id="42",
        text="/help",
        chat_title="",
    )
    payload.update(overrides)
    return Inbound(**payload)


class ChatFlowTest(TempStoreTest):
    def setUp(self) -> None:
        super().setUp()
        self.clock.now_value = NOW
        self.pairing = PairingGate()
        self.groups = [("100", "Ops"), ("200", "Ops"), ("300", "Quiet")]
        self.controller = self._controller()

    def _controller(self, **kwargs) -> ChatController:
        defaults = dict(
            store=self.store,
            clock=self.clock,
            channel="telegram",
            language="zh",
            admins=("42",),
            allow_self_subscribe=False,
            group_catalog=lambda: list(self.groups),
            pairing=self.pairing,
        )
        defaults.update(kwargs)
        return ChatController(**defaults)

    def test_help_and_duplicate_event(self):
        reply = self.controller.handle(_inbound(text="/start"))
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("添加群聊", reply.text)
        self.assertIsNone(self.controller.handle(_inbound(text="/help")))

    def test_non_admin_is_refused_without_details(self):
        reply = self.controller.handle(_inbound(event_id="e2", user_id="7", chat_id="7", text="#添加群聊"))
        assert reply is not None
        self.assertIn("管理员", reply.text)
        self.assertNotIn("token", reply.text.lower())
        self.assertNotIn("secret", reply.text.lower())
        self.assertIsNone(self.store.load_session("telegram", "7"))

    def test_self_subscribe_and_unsubscribe(self):
        on = self.controller.handle(_inbound(event_id="e2", text="#开启单聊通知"))
        assert on is not None
        self.assertIn("已开启", on.text)
        again = self.controller.handle(_inbound(event_id="e3", text="/subscribe"))
        assert again is not None
        self.assertIn("已经", again.text)
        off = self.controller.handle(_inbound(event_id="e4", text="#关闭单聊通知"))
        assert off is not None
        self.assertIn("已关闭", off.text)
        active = [item for item in self.store.subscriptions("telegram") if item["status"] == "active"]
        self.assertEqual(active, [])

    def test_stranger_can_self_subscribe_only_when_allowed(self):
        refused = self.controller.handle(_inbound(event_id="e2", user_id="9", chat_id="9", text="/subscribe"))
        assert refused is not None
        self.assertIn("管理员", refused.text)
        open_controller = self._controller(allow_self_subscribe=True, admins=())
        allowed = open_controller.handle(_inbound(event_id="e3", user_id="9", chat_id="9", text="/subscribe"))
        assert allowed is not None
        self.assertIn("已开启", allowed.text)

    def test_group_picker_number_name_duplicate_timeout_and_stale_list(self):
        opened = self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))
        assert opened is not None
        self.assertIn("1. Ops", opened.text)
        self.assertIn("2. Ops", opened.text)
        self.assertIn("3. Quiet", opened.text)
        ambiguous = self.controller.handle(_inbound(event_id="e3", text="Ops"))
        assert ambiguous is not None
        self.assertIn("重名", ambiguous.text)
        self.groups = [("300", "Quiet")]
        refreshed = self.controller.handle(_inbound(event_id="e4", text="/addgroup"))
        assert refreshed is not None
        self.assertIn("Quiet", refreshed.text)
        self.assertNotIn("Ops", refreshed.text)
        picked = self.controller.handle(_inbound(event_id="e5", text="1"))
        assert picked is not None
        self.assertIn("Quiet", picked.text)
        active = [item for item in self.store.subscriptions("telegram") if item["status"] == "active"]
        self.assertEqual(active[0]["address"], "300")
        self.controller.handle(_inbound(event_id="e6", text="#移除群聊"))
        self.clock.advance(61)
        expired = self.controller.handle(_inbound(event_id="e7", text="1"))
        assert expired is not None
        self.assertIn("超时", expired.text)

    def test_cancel_and_remove_group(self):
        self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))
        cancelled = self.controller.handle(_inbound(event_id="e3", text="#取消"))
        assert cancelled is not None
        self.assertIn("退出", cancelled.text)
        self.assertIsNone(self.store.load_session("telegram", "42"))
        self.store.subscribe(channel="telegram", kind="group", address="100", display_name="Ops", now=NOW)
        self.controller.handle(_inbound(event_id="e4", text="#取消群聊"))
        removed = self.controller.handle(_inbound(event_id="e5", text="Ops"))
        assert removed is not None
        self.assertIn("取消", removed.text)
        self.assertEqual(
            [item for item in self.store.subscriptions("telegram") if item["status"] == "active"],
            [],
        )

    def test_group_shortcut_and_bot_suffix(self):
        reply = self.controller.handle(
            _inbound(event_id="e2", scope="group", chat_id="-100", user_id="42", chat_title="Ops", text="/subscribe@ResetBot")
        )
        assert reply is not None
        self.assertIn("Ops", reply.text)
        off = self.controller.handle(
            _inbound(event_id="e3", scope="group", chat_id="-100", user_id="42", chat_title="Ops", text="/unsubscribe")
        )
        assert off is not None
        self.assertIn("取消", off.text)

    def test_pairing_and_add_admin(self):
        gate = PairingGate()
        code = gate.issue(NOW)
        guest = self._controller(admins=(), pairing=gate)
        bad = guest.handle(_inbound(event_id="e2", user_id="9", chat_id="9", text="/pair nope"))
        assert bad is not None
        self.assertIn("无效", bad.text)
        ok = guest.handle(_inbound(event_id="e3", user_id="9", chat_id="9", text=f"/pair {code}"))
        assert ok is not None
        self.assertIn("管理员", ok.text)
        self.assertTrue(self.store.is_chat_admin("telegram", "9"))
        again = guest.handle(_inbound(event_id="e4", user_id="9", chat_id="9", text=f"/pair {code}"))
        assert again is not None
        self.assertIn("无效", again.text)
        added = guest.handle(_inbound(event_id="e5", user_id="9", chat_id="9", text="/addadmin 15"))
        assert added is not None
        self.assertTrue(self.store.is_chat_admin("telegram", "15"))
        for index in range(5):
            gate.consume("8", "nope", NOW)
        limited = guest.handle(_inbound(event_id=f"lim-{index}", user_id="8", chat_id="8", text="/pair nope"))
        assert limited is not None
        self.assertIn("过多", limited.text)
        stranger = self._controller()
        refused = stranger.handle(_inbound(event_id="e9", user_id="7", chat_id="7", text="/addadmin 16"))
        assert refused is not None
        self.assertIn("管理员", refused.text)
        self.assertFalse(self.store.is_chat_admin("telegram", "16"))
        junk = guest.handle(_inbound(event_id="e10", user_id="9", chat_id="9", text="/addadmin not-an-id"))
        assert junk is not None
        self.assertIn("用法", junk.text)
        self.assertFalse(self.store.is_chat_admin("telegram", "not-an-id"))
        negative = guest.handle(_inbound(event_id="e11", user_id="9", chat_id="9", text="/addadmin -100"))
        assert negative is not None
        self.assertIn("用法", negative.text)
        self.assertFalse(self.store.is_chat_admin("telegram", "-100"))
        feishu_admin = guest.handle(_inbound(event_id="e12", user_id="9", chat_id="9", text="/addadmin ou_abc_1"))
        assert feishu_admin is not None
        self.assertTrue(self.store.is_chat_admin("telegram", "ou_abc_1"))

    def test_pairing_accepts_the_code_in_either_case_once(self):
        gate = PairingGate()
        code = gate.issue(NOW)
        guest = self._controller(admins=(), pairing=gate)
        ok = guest.handle(_inbound(event_id="e2", user_id="9", chat_id="9", text=f"/pair {code.upper()}"))
        assert ok is not None
        self.assertIn("已成为", ok.text)
        replay = guest.handle(_inbound(event_id="e3", user_id="9", chat_id="9", text=f"/pair {code}"))
        assert replay is not None
        self.assertIn("无效", replay.text)

    def test_a_group_message_does_not_answer_a_direct_picker(self):
        self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))
        reply = self.controller.handle(
            _inbound(event_id="e3", scope="group", chat_id="-100", user_id="42", chat_title="Ops", text="1")
        )
        self.assertIsNone(reply)
        self.assertIsNotNone(self.store.load_session("telegram", "42"))
        self.assertEqual(
            [item for item in self.store.subscriptions("telegram") if item["status"] == "active"],
            [],
        )
        picked = self.controller.handle(_inbound(event_id="e4", text="3"))
        assert picked is not None
        self.assertIn("Quiet", picked.text)

    def test_a_failed_refresh_drops_the_previous_list(self):
        self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))

        def boom():
            raise RuntimeError("down")

        self.controller.group_catalog = boom
        failed = self.controller.handle(_inbound(event_id="e3", text="/addgroup"))
        assert failed is not None
        self.assertIn("暂时", failed.text)
        self.assertIsNone(self.store.load_session("telegram", "42"))
        leftover = self.controller.handle(_inbound(event_id="e4", text="1"))
        assert leftover is not None
        self.assertNotIn("已订阅", leftover.text)

    def test_unicode_digit_does_not_crash_the_picker(self):
        self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))
        reply = self.controller.handle(_inbound(event_id="e3", text="²"))
        assert reply is not None
        self.assertIn("没有这个选项", reply.text)
        self.assertIsNotNone(self.store.load_session("telegram", "42"))

    def test_two_processes_cannot_both_hold_the_lock(self):
        other = StateStore(self.store.path)
        self.addCleanup(other.close)
        self.assertTrue(self.store.acquire_lock("solo", NOW, 900))
        self.assertTrue(self.store.acquire_lock("solo", NOW, 900))
        self.assertFalse(other.acquire_lock("other", NOW, 900))
        self.store.release_lock("solo")
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def grab(store: StateStore, owner: str) -> None:
            barrier.wait()
            try:
                results.append(store.acquire_lock(owner, NOW, 900))
            finally:
                store.close()

        threads = [
            threading.Thread(target=grab, args=(self.store, "proc-a")),
            threading.Thread(target=grab, args=(other, "proc-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(self.store.acquire_lock("proc-c", NOW, 900))

    def test_unsubscribe_while_a_send_is_in_flight_is_not_queued_again(self):
        self.store.subscribe(channel="telegram", kind="personal", address="42", display_name="42", now=NOW)
        notice = Notice(
            dedupe_key="p",
            post_key="p",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="",
            text="hi",
            original_text="",
            url="",
            published_at="",
        )
        targets = self.store.subscription_targets("telegram", "zh", "UTC")
        self.store.ingest([notice], targets, {"events": []}, None, NOW)
        self.store.conn.commit()
        row = self.store.due_rows(NOW)[0]
        self.store.claim(row.id, NOW)
        self.store.conn.commit()
        self.store.unsubscribe("telegram", "personal", "42", NOW, reason="unsubscribed")
        applied = self.store.apply_state(
            row.id,
            DeliveryState(
                status="retryable",
                attempts=1,
                next_attempt_at=NOW,
                first_ambiguous_at=None,
                last_error="network",
                receipt_message_id=None,
                receipt_detail="network",
            ),
            NOW,
        )
        self.assertFalse(applied)
        self.assertEqual(self.store.due_rows(NOW), [])
        self.assertEqual(self.store.read_row(row.id).state.status, "cancelled")

    def test_help_follows_role_channel_and_ignores_the_picker(self):
        admin = self.controller.handle(_inbound(event_id="h1", text="/help@ResetBot"))
        assert admin is not None
        self.assertIn("/addadmin", admin.text)
        self.assertIn("allow_self_subscribe 当前关闭", admin.text)
        self.assertIn("点 Start", admin.text)
        self.assertNotIn("版本管理与发布", admin.text)
        stranger = self.controller.handle(_inbound(event_id="h2", user_id="7", chat_id="7", text="#指南"))
        assert stranger is not None
        self.assertNotIn("/addadmin", stranger.text)
        self.assertIn("请联系管理员", stranger.text)
        self.assertIn("/help", stranger.text)
        opened = self.controller
        opened.handle(_inbound(event_id="h3", text="#添加群聊"))
        during = opened.handle(_inbound(event_id="h4", text="#使用说明"))
        assert during is not None
        self.assertIn("先读我", during.text)
        self.assertIsNotNone(self.store.load_session("telegram", "42"))
        brief = self.controller.handle(
            _inbound(event_id="h5", scope="group", chat_id="-100", user_id="42", text="/help")
        )
        assert brief is not None
        self.assertIn("私聊", brief.text)
        self.assertNotIn("/addadmin", brief.text)
        self.assertIsNone(
            self.controller.handle(_inbound(event_id="h6", scope="group", chat_id="-100", user_id="7", text="大家好"))
        )
        in_group = self.controller.handle(
            _inbound(event_id="h6b", scope="group", chat_id="-100", user_id="7", text="#开启单聊通知")
        )
        assert in_group is not None
        self.assertIn("allow_self_subscribe 关闭", in_group.text)
        self.assertIn("/help", in_group.text)
        refused = self.controller.handle(_inbound(event_id="h7", user_id="7", chat_id="7", text="/subscribe"))
        assert refused is not None
        self.assertIn("allow_self_subscribe 关闭", refused.text)
        self.assertIn("/help", refused.text)
        unsubscribe = self.controller.handle(_inbound(event_id="h7b", user_id="7", chat_id="7", text="/unsubscribe"))
        assert unsubscribe is not None
        self.assertIn("不在管理员名单", unsubscribe.text)
        self.assertNotIn("allow_self_subscribe 关闭", unsubscribe.text)
        open_self = self._controller(allow_self_subscribe=True, admins=("42",), channel="feishu")
        feishu_admin = open_self.handle(_inbound(channel="feishu", event_id="h8", text="/help"))
        assert feishu_admin is not None
        self.assertIn("版本管理与发布", feishu_admin.text)
        self.assertIn("allow_self_subscribe 当前开启", feishu_admin.text)
        nobody = self._controller(admins=(), channel="feishu", extra_admins=lambda: set())
        empty = nobody.handle(_inbound(channel="feishu", event_id="h9", user_id="7", chat_id="7", text="/status"))
        assert empty is not None
        self.assertIn("还没有管理员", empty.text)
        self.assertIn("协作者", empty.text)

    def test_state_file_is_owner_only(self):
        mode = os.stat(self.store.path).st_mode
        self.assertEqual(mode & 0o077, 0)

    def test_english_copy_and_every_chat_channel(self):
        english = self._controller(language="en")
        reply = english.handle(_inbound(text="/help"))
        assert reply is not None
        self.assertIn("Read this first", reply.text)
        self.assertIn("/addadmin", reply.text)
        supporting = [name for name, cls in REGISTRY.items() if getattr(cls, "supports_chat", False)]
        self.assertEqual(set(supporting), {"telegram", "feishu"})
        for name in supporting:
            for language, needle in (("zh", "先读我"), ("en", "Read this first")):
                controller = self._controller(channel=name, language=language, group_catalog=lambda: [])
                answer = controller.handle(_inbound(channel=name, event_id=f"{name}-{language}", text="/help"))
                assert answer is not None
                self.assertIn(needle, answer.text)

    def test_unsubscribe_cancels_pending_and_permanent_failures_stop(self):
        _key, _created = self.store.subscribe(channel="telegram", kind="group", address="-100", display_name="Ops", now=NOW)
        notice = Notice(
            dedupe_key="p",
            post_key="p",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="",
            text="hi",
            original_text="",
            url="",
            published_at="",
        )
        targets = self.store.subscription_targets("telegram", "zh", "UTC")
        self.store.ingest([notice], targets, {"events": [], "activities": []}, None, NOW)
        self.store.conn.commit()
        self.assertEqual(len(self.store.due_rows(NOW)), 1)
        self.store.unsubscribe("telegram", "group", "-100", NOW, reason="unsubscribed")
        self.assertEqual(self.store.due_rows(NOW), [])
        self.store.subscribe(channel="telegram", kind="personal", address="42", display_name="42", now=NOW)
        key = self.store.subscription_targets("telegram", "zh", "UTC")[0].key
        for _ in range(2):
            self.assertFalse(
                self.store.record_subscription_result("telegram", key, ok=False, detail="blocked", now=NOW)
            )
        self.assertTrue(self.store.record_subscription_result("telegram", key, ok=False, detail="blocked", now=NOW))
        stopped = [item for item in self.store.subscriptions("telegram") if item["kind"] == "personal"]
        self.assertEqual(stopped[0]["status"], "stopped")
        self.assertIn("blocked", stopped[0]["stop_reason"])

    def test_new_subscription_does_not_receive_old_notices(self):
        old = extract_notices(snapshot([make_event("a", [make_post("p1", "预告")])]))[0]
        self.store.baseline([old], snapshot([]), None, NOW)
        self.store.subscribe(channel="telegram", kind="personal", address="42", display_name="42", now=NOW)

        class Channel:
            name = "telegram"
            settings = {"language": "zh", "timezone": "UTC"}
            targets: list[Target] = []

            def render(self, notice, target):
                return {}

            def send(self, message, target, idempotency_key):
                return Receipt("sent")

        channel = Channel()
        created, _cancelled = self.store.ingest([old], effective_targets([channel], self.store), snapshot([]), None, NOW)
        self.store.conn.commit()
        self.assertEqual(created, [])
        new = extract_notices(snapshot([make_event("b", [make_post("p2", "确认", text="new")])]))[0]
        created, _cancelled = self.store.ingest([old, new], effective_targets([channel], self.store), snapshot([]), None, NOW)
        self.store.conn.commit()
        self.assertEqual(len(created), 1)
        self.assertEqual(self.store.due_rows(NOW)[0].target_key, self.store.subscription_targets("telegram", "zh", "UTC")[0].key)

    def test_migrate_chat_id(self):
        self.store.remember_chat("telegram", "-100", "Ops", "joined", NOW)
        self.store.subscribe(channel="telegram", kind="group", address="-100", display_name="Ops", now=NOW)
        old_key = self.store.subscription_targets("telegram", "zh", "UTC")[0].key
        notice = Notice(
            dedupe_key="m",
            post_key="m",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="",
            text="hi",
            original_text="",
            url="",
            published_at="",
        )
        self.store.ingest([notice], self.store.subscription_targets("telegram", "zh", "UTC"), {"events": [], "activities": []}, None, NOW)
        self.store.conn.commit()
        self.controller.handle(
            _inbound(event_id="mig", scope="group", chat_id="-100", user_id="42", text="", migrate_to="-100999")
        )
        moved = self.store.subscription_targets("telegram", "zh", "UTC")[0]
        self.assertEqual(moved.address, "-100999")
        self.assertNotEqual(moved.key, old_key)
        self.assertEqual(self.store.due_rows(NOW)[0].target_key, moved.key)

    def test_migrate_into_an_existing_subscription_keeps_one_target(self):
        self.store.remember_chat("telegram", "-100", "Ops", "joined", NOW)
        self.store.subscribe(channel="telegram", kind="group", address="-100", display_name="Ops", now=NOW)
        self.store.subscribe(channel="telegram", kind="group", address="-100999", display_name="Ops supergroup", now=NOW)
        dest = next(item for item in self.store.subscription_targets("telegram", "zh", "UTC") if item.address == "-100999")
        notice = Notice(
            dedupe_key="m2",
            post_key="m2",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="",
            text="hi",
            original_text="",
            url="",
            published_at="",
        )
        old = next(item for item in self.store.subscription_targets("telegram", "zh", "UTC") if item.address == "-100")
        self.store.ingest([notice], [old], {"events": [], "activities": []}, None, NOW)
        self.store.conn.commit()
        self.store.migrate_chat("telegram", "-100", "-100999", NOW)
        active = [item for item in self.store.subscriptions("telegram") if item["status"] == "active"]
        self.assertEqual([item["address"] for item in active], ["-100999"])
        self.assertEqual(self.store.due_rows(NOW)[0].target_key, dest.key)
        stopped = [item for item in self.store.subscriptions("telegram") if item["address"] == "-100"]
        self.assertEqual(stopped[0]["status"], "stopped")
        self.assertEqual(stopped[0]["stop_reason"], "chat migrated")

    def test_membership_removal_stops_the_group(self):
        self.store.subscribe(channel="telegram", kind="group", address="-100", display_name="Ops", now=NOW)
        self.controller.handle(
            _inbound(event_id="left", scope="group", chat_id="-100", user_id="", text="", membership="left", chat_title="Ops")
        )
        row = self.store.subscriptions("telegram")[0]
        self.assertEqual(row["status"], "stopped")
        self.assertIn("removed", row["stop_reason"])

    def test_empty_catalog_and_list_failure(self):
        empty = self._controller(group_catalog=lambda: [])
        reply = empty.handle(_inbound(event_id="e2", text="#添加群聊"))
        assert reply is not None
        self.assertIn("没有可添加", reply.text)
        broken = self._controller(group_catalog=lambda: (_ for _ in ()).throw(RuntimeError("down")))
        failed = broken.handle(_inbound(event_id="e3", text="#添加群聊"))
        assert failed is not None
        self.assertIn("暂时", failed.text)


if __name__ == "__main__":
    unittest.main()
