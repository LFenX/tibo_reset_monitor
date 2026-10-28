"""Chat control: pairing, permissions, subscriptions, and the group picker."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from tests.support import FakeClock, TempStoreTest, make_event, make_post, snapshot
from tibo_reset_monitor.channels import REGISTRY
from tibo_reset_monitor.core.chat import ChatController, Inbound, PairingGate
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.model import Notice, Receipt, Target
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

    def test_unicode_digit_does_not_crash_the_picker(self):
        self.controller.handle(_inbound(event_id="e2", text="#添加群聊"))
        reply = self.controller.handle(_inbound(event_id="e3", text="²"))
        assert reply is not None
        self.assertIn("没有这个选项", reply.text)
        self.assertIsNotNone(self.store.load_session("telegram", "42"))

    def test_state_file_is_owner_only(self):
        mode = os.stat(self.store.path).st_mode
        self.assertEqual(mode & 0o077, 0)

    def test_english_copy_and_every_chat_channel(self):
        english = self._controller(language="en")
        reply = english.handle(_inbound(text="/help"))
        assert reply is not None
        self.assertIn("Commands", reply.text)
        self.assertNotIn("添加群聊", reply.text)
        supporting = [name for name, cls in REGISTRY.items() if getattr(cls, "supports_chat", False)]
        self.assertEqual(set(supporting), {"telegram", "feishu"})
        for name in supporting:
            for language, needle in (("zh", "订阅"), ("en", "Commands")):
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
