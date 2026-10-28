"""Startup usage guide: one send per person, both languages, and a quiet skip."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import FakeClock, TempStoreTest, make_event, make_post, snapshot, utc
from tibo_reset_monitor.cli import main
from tibo_reset_monitor.core.chat import ChatController, Inbound, PairingGate
from tibo_reset_monitor.core.http import HttpResponse
from tibo_reset_monitor.core.i18n import GUIDE_VERSION, chat_text
from tibo_reset_monitor.core.model import Receipt, Target
from tibo_reset_monitor.config import load_config
from tibo_reset_monitor.core.guide import deliver_guides
from tibo_reset_monitor.core.store import StateStore
from tibo_reset_monitor.service import Runtime, _welcome_opener, build_runtime, poll, prepare_guides

NOW = utc()
TOKEN = "123456:AAAAAAAAAAAAAAAAAAAA"
COMMANDS = (
    "/start",
    "/help",
    "/status",
    "/subscribe",
    "/unsubscribe",
    "/addgroup",
    "/removegroup",
    "/cancel",
    "/pair",
    "/addadmin",
    "#状态",
    "#开启单聊通知",
    "#关闭单聊通知",
    "#添加群聊",
    "#移除群聊",
    "#取消群聊",
    "#取消通知",
    "#取消",
)


class GuideChannel:
    def __init__(self, name, settings, targets=None, send=None):
        self.name = name
        self.settings = settings
        self.targets = list(targets or [])
        self.sent: list[dict] = []
        self._send = send

    def send_text(self, recipient, text, *, idempotency_key):
        self.sent.append({"recipient": recipient, "text": text, "key": idempotency_key})
        if self._send is not None:
            return self._send(recipient, text, idempotency_key)
        return Receipt("sent", detail="sent")


def _runtime(store, channel, clock=None) -> Runtime:
    return Runtime(
        config=None,
        store=store,
        source=None,
        channels=[channel],
        clock=clock or FakeClock(NOW),
        owner="test",
    )


def _flush(runtime: Runtime) -> None:
    prepare_guides(runtime, collaborators=lambda channel: set())
    deliver_guides(runtime.store, runtime.channels, runtime.clock)


class GuideCopyTest(unittest.TestCase):
    def test_both_languages_list_every_command_and_the_short_flows(self):
        chinese = chat_text("zh", "guide")
        english = chat_text("en", "guide")
        for text in (chinese, english):
            for command in COMMANDS:
                self.assertIn(command, text)
        self.assertIn("私聊", chinese)
        self.assertIn("加进群", chinese)
        self.assertIn("private chat", english)
        self.assertIn("add the bot to the group", english)
        self.assertNotEqual(chinese, english)


class GuideDeliveryTest(TempStoreTest):
    def test_sends_once_per_version_and_again_when_the_version_changes(self):
        channel = GuideChannel(
            "telegram",
            {"language": "zh", "chat_control": "true", "welcome_message": "true", "admins": "42"},
        )
        runtime = _runtime(self.store, channel, self.clock)
        _flush(runtime)
        self.assertEqual([item["recipient"] for item in channel.sent], ["42"])
        self.assertIn("使用指南", channel.sent[0]["text"])
        _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        path = self.store.path
        self.store.close()
        self.store = StateStore(path)
        self.addCleanup(self.store.close)
        runtime.store = self.store
        _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        with patch("tibo_reset_monitor.service.GUIDE_VERSION", "2"):
            prepare_guides(runtime, collaborators=lambda channel: set())
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(self.store.guide_row("telegram", "42", "2")["status"], "sent")
        self.assertEqual(self.store.guide_row("telegram", "42", GUIDE_VERSION)["status"], "sent")

    def test_language_follows_the_personal_target_then_the_channel(self):
        channel = GuideChannel(
            "telegram",
            {"language": "en", "chat_control": "true", "welcome_message": "true", "admins": "1,2"},
            targets=[Target("telegram", "me", "personal", "1", {"language": "zh"})],
        )
        runtime = _runtime(self.store, channel, self.clock)
        _flush(runtime)
        by_recipient = {item["recipient"]: item["text"] for item in channel.sent}
        self.assertIn("使用指南", by_recipient["1"])
        self.assertIn("Usage guide", by_recipient["2"])

    def test_welcome_switch_and_cli_send_nothing(self):
        channel = GuideChannel(
            "telegram",
            {"language": "zh", "chat_control": "true", "welcome_message": "false", "admins": "42"},
        )
        runtime = _runtime(self.store, channel, self.clock)
        _flush(runtime)
        self.assertEqual(channel.sent, [])
        channel.settings["welcome_message"] = "true"
        runtime.welcome_enabled = False
        _flush(runtime)
        self.assertEqual(channel.sent, [])

    def test_retry_waits_for_the_next_poll_and_hides_secrets(self):
        calls = {"n": 0}

        def send(recipient, text, key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    f"network {TOKEN} pairing code (single use, expires in 10 minutes): abcdef"
                )
            return Receipt("sent", detail="sent")

        channel = GuideChannel(
            "telegram",
            {"language": "en", "chat_control": "true", "welcome_message": "true", "admins": "42"},
            send=send,
        )
        runtime = _runtime(self.store, channel, self.clock)
        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            _flush(runtime)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.store.guide_row("telegram", "42", "1")["status"], "retryable")
        text = "\n".join(captured.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("abcdef", text)
        self.assertIn("<redacted", text)
        continued = True
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertTrue(continued)
        self.assertEqual(calls["n"], 1)
        self.clock.advance(31)
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.store.guide_row("telegram", "42", "1")["status"], "sent")
        self.assertEqual(channel.sent[0]["key"], channel.sent[1]["key"])

    def test_cannot_dm_is_skipped_until_start_or_pair(self):
        mode = {"permanent": True}

        def send(recipient, text, key):
            if mode["permanent"]:
                return Receipt(
                    "permanent",
                    detail=f"Telegram 403: blocked. token {TOKEN}",
                )
            return Receipt("sent", detail="sent")

        channel = GuideChannel(
            "telegram",
            {"language": "zh", "chat_control": "true", "welcome_message": "true", "admins": "42"},
            send=send,
        )
        runtime = _runtime(self.store, channel, self.clock)
        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(self.store.guide_row("telegram", "42", "1")["status"], "skipped")
        logged = "\n".join(captured.output)
        self.assertIn("has not started the bot", logged)
        self.assertNotIn(TOKEN, logged)
        _flush(runtime)
        self.assertEqual(len(channel.sent), 1)

        mode["permanent"] = False
        controller = ChatController(
            store=self.store,
            clock=self.clock,
            channel="telegram",
            language="zh",
            admins=("42",),
            welcome=_welcome_opener(runtime, "telegram"),
        )
        reply = controller.handle(_inbound("s1", "/start"))
        self.assertIsNotNone(reply)
        self.assertIn("/subscribe", reply.text)
        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(self.store.guide_row("telegram", "42", "1")["status"], "sent")
        controller.handle(_inbound("s2", "/start"))
        controller.handle(_inbound("s3", "/help"))
        controller.handle(_inbound("s4", "你好"))
        self.assertEqual(len(channel.sent), 2)

        gate = PairingGate()
        code = gate.issue(self.clock.now())
        paired = ChatController(
            store=self.store,
            clock=self.clock,
            channel="telegram",
            language="zh",
            admins=(),
            pairing=gate,
            welcome=_welcome_opener(runtime, "telegram"),
        )
        paired.handle(_inbound("p1", f"/pair {code}", user_id="99", chat_id="99"))
        self.assertEqual([item["recipient"] for item in channel.sent], ["42", "42", "99"])
        paired.handle(_inbound("p2", f"/pair {gate.issue(self.clock.now())}", user_id="99", chat_id="99"))
        self.assertEqual(len(channel.sent), 3)

    def test_feishu_uses_collaborators_and_skips_when_they_are_missing(self):
        channel = GuideChannel(
            "feishu",
            {"language": "en", "chat_control": "true", "welcome_message": "true", "admins": ""},
        )
        runtime = _runtime(self.store, channel, self.clock)
        prepare_guides(runtime, collaborators=lambda channel: {"ou_b", "ou_a"})
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertEqual([item["recipient"] for item in channel.sent], ["ou_a", "ou_b"])
        self.assertIn("Usage guide", channel.sent[0]["text"])

        empty = GuideChannel(
            "feishu",
            {"language": "zh", "chat_control": "true", "welcome_message": "true", "admins": ""},
        )
        empty_runtime = _runtime(self.store, empty, self.clock)
        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            prepare_guides(empty_runtime, collaborators=lambda channel: set())
        self.assertEqual(empty.sent, [])
        self.assertIn("no collaborators", "\n".join(captured.output))
        with self.assertNoLogs("tibo_reset_monitor", level="INFO"):
            prepare_guides(empty_runtime, collaborators=lambda channel: {"ou_late"})

        broken = _runtime(
            self.store,
            GuideChannel(
                "feishu",
                {"language": "zh", "chat_control": "true", "welcome_message": "true", "admins": ""},
            ),
            self.clock,
        )

        def explode(channel):
            raise RuntimeError(f"secret {TOKEN}")

        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            prepare_guides(broken, collaborators=explode)
        self.assertIn("unavailable", "\n".join(captured.output))
        self.assertNotIn(TOKEN, "\n".join(captured.output))
        self.assertEqual(broken.channels[0].sent, [])


class GuidePollTest(unittest.TestCase):
    def test_poll_skips_a_closed_direct_chat_and_keeps_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config-chat.json"
            config_path.write_text(
                json.dumps(
                    {
                        "state": {"path": str(root / "monitor.sqlite3")},
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "admins": ["6173752247"],
                                "targets": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = TOKEN
            self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)
            transport = _Feed()
            runtime = build_runtime(load_config(config_path, environ={}), transport=transport, clock=FakeClock(NOW))
            self.addCleanup(runtime.close)
            with self.assertLogs("tibo_reset_monitor", level="INFO") as first:
                self.assertEqual(poll(runtime), 0)
            self.assertEqual(transport.guides, 1)
            self.assertIn("has not started the bot", "\n".join(first.output))
            self.assertNotIn(TOKEN, "\n".join(first.output))
            runtime.clock.advance(1)
            with self.assertLogs("tibo_reset_monitor", level="INFO") as second:
                self.assertEqual(poll(runtime), 0)
            self.assertEqual(transport.guides, 1)
            self.assertFalse(any("welcome guide" in line for line in second.output))
            row = runtime.store.guide_row("telegram", "6173752247", "1")
            self.assertEqual(row["status"], "skipped")


class CliWelcomeTest(unittest.TestCase):
    def test_run_no_welcome_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "state": {"path": str(Path(tmp) / "state.sqlite3")},
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "admins": ["42"],
                                "targets": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            seen = {}

            def fake_run(runtime, sleep=None):
                seen["enabled"] = runtime.welcome_enabled
                return 0

            with patch("tibo_reset_monitor.cli.run_forever", fake_run):
                code = main(["--config", str(path), "run", "--no-welcome"])
            self.assertEqual(code, 0)
            self.assertFalse(seen["enabled"])


class _Feed:
    def __init__(self) -> None:
        self.guides = 0

    def send(self, method, url, headers, body, timeout):
        if url.endswith("/sendMessage"):
            self.guides += 1
            payload = {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot can't initiate conversation with a user",
            }
            return HttpResponse(403, {}, json.dumps(payload).encode(), url)
        if "codex-resets" in url:
            body = snapshot([make_event("old", [make_post("old", "预告")])])
            return HttpResponse(200, {"etag": 'W/"v1"'}, json.dumps(body).encode(), url)
        return HttpResponse(200, {}, b'{"ok":true}', url)


def _inbound(event_id: str, text: str, user_id: str = "42", chat_id: str = "42") -> Inbound:
    return Inbound(
        channel="telegram",
        event_id=event_id,
        scope="personal",
        chat_id=chat_id,
        user_id=user_id,
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
