"""Startup usage guide: one send per person, both languages, and a quiet skip."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from tests.support import SafetyFixture
from types import SimpleNamespace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.support import FakeClock, TempStoreTest, make_event, make_post, snapshot, utc
from tibo_reset_monitor.cli import main
from tibo_reset_monitor.core.chat import ChatController, Inbound, PairingGate
from tibo_reset_monitor.core.http import HttpResponse
from tibo_reset_monitor.core.i18n import GUIDE_VERSION, usage_guide
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
        return Receipt("sent", provider_message_id="synthetic-guide-id", detail="sent")


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
        chinese = usage_guide("zh", channel="telegram", allow_self_subscribe=False, admin=True)
        english = usage_guide("en", channel="telegram", allow_self_subscribe=False, admin=True)
        for text in (chinese, english):
            for command in COMMANDS:
                self.assertIn(command, text)
        self.assertIn("私聊", chinese)
        self.assertIn("加进群", chinese)
        self.assertIn("direct chat", english)
        self.assertIn("Add the bot to a group", english)
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
        self.assertIn("先读我", channel.sent[0]["text"])
        self.assertIn("allow_self_subscribe 当前关闭", channel.sent[0]["text"])
        _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        path = self.store.path
        self.store.close()
        self.store = StateStore(path)
        self.addCleanup(self.store.close)
        runtime.store = self.store
        _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        with patch("tibo_reset_monitor.service.GUIDE_VERSION", "3"):
            prepare_guides(runtime, collaborators=lambda channel: set())
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(self.store.guide_row("telegram", "42", "3")["status"], "sent")
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
        self.assertIn("先读我", by_recipient["1"])
        self.assertIn("Read this first", by_recipient["2"])

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
                return Receipt(
                    "retryable", detail=f"network {TOKEN} pairing code (single use, expires in 10 minutes): abcdef"
                )
            return Receipt("sent", provider_message_id="synthetic-guide-retry-id", detail="sent")

        channel = GuideChannel(
            "telegram",
            {"language": "en", "chat_control": "true", "welcome_message": "true", "admins": "42"},
            send=send,
        )
        runtime = _runtime(self.store, channel, self.clock)
        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            _flush(runtime)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.store.guide_row("telegram", "42", GUIDE_VERSION)["status"], "retryable")
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
        self.assertEqual(self.store.guide_row("telegram", "42", GUIDE_VERSION)["status"], "sent")
        self.assertEqual(channel.sent[0]["key"], channel.sent[1]["key"])

    def test_cannot_dm_is_skipped_until_start_or_pair(self):
        mode = {"permanent": True}

        def send(recipient, text, key):
            if mode["permanent"]:
                return Receipt(
                    "permanent",
                    detail=f"Telegram 403: blocked. token {TOKEN}",
                )
            return Receipt("sent", provider_message_id="synthetic-guide-open-id", detail="sent")

        channel = GuideChannel(
            "telegram",
            {"language": "zh", "chat_control": "true", "welcome_message": "true", "admins": "42"},
            send=send,
        )
        runtime = _runtime(self.store, channel, self.clock)
        with self.assertLogs("tibo_reset_monitor", level="INFO") as captured:
            _flush(runtime)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(self.store.guide_row("telegram", "42", GUIDE_VERSION)["status"], "skipped")
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
        self.assertIn("先读我", reply.text)
        self.assertIn("/subscribe", reply.text)
        self.assertEqual(len(channel.sent), 1)
        controller.handle(_inbound("s2", "/start"))
        controller.handle(_inbound("s3", "/help"))
        controller.handle(_inbound("s4", "你好"))
        self.assertEqual(len(channel.sent), 1)

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
        self.assertEqual([item["recipient"] for item in channel.sent], ["42", "99"])
        paired.handle(_inbound("p2", f"/pair {gate.issue(self.clock.now())}", user_id="99", chat_id="99"))
        self.assertEqual(len(channel.sent), 2)

    def test_feishu_uses_collaborators_and_skips_when_they_are_missing(self):
        channel = GuideChannel(
            "feishu",
            {"language": "en", "chat_control": "true", "welcome_message": "true", "admins": ""},
        )
        runtime = _runtime(self.store, channel, self.clock)
        prepare_guides(runtime, collaborators=lambda channel: {"ou_b", "ou_a"})
        deliver_guides(runtime.store, runtime.channels, runtime.clock)
        self.assertEqual([item["recipient"] for item in channel.sent], ["ou_a", "ou_b"])
        self.assertIn("Read this first", channel.sent[0]["text"])

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
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as resources:
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
                                "admins": ["42"],
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
            resources.callback(runtime.close)
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
            row = runtime.store.guide_row("telegram", "42", GUIDE_VERSION)
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


class GuideSafetyRegressionTest(SafetyFixture):
    def test_telegram_guide_ambiguous_result_is_not_blindly_retried(self):
        from tibo_reset_monitor.channels.telegram import TelegramChannel
        from tibo_reset_monitor.config import ChannelSpec
        from tibo_reset_monitor.core.guide import deliver_guides
        from tibo_reset_monitor.core.http import TransportError
        attempts = []

        class Transport:

            def send(inner, method, url, headers, body, timeout):
                attempts.append(json.loads(body))
                if len(attempts) == 1:
                    raise TransportError('synthetic provider accepted but ack lost', ambiguous=True)
                response = {'ok': True, 'result': {'message_id': 2, 'chat': {'id': 400020001}}}
                return HttpResponse(200, {}, json.dumps(response).encode(), 'https://example.invalid/')
        channel = TelegramChannel(ChannelSpec('telegram', True, {'bot_token_env': 'ADVERSARIAL_R2_SYNTHETIC_TOKEN'}, []), Transport(), self.clock)
        self.store.enqueue_guide('telegram', '400020001', 'r2-v1', 'zh', self.clock.now())
        with patch.dict(os.environ, {'ADVERSARIAL_R2_SYNTHETIC_TOKEN': 'synthetic-token'}):
            deliver_guides(self.store, [channel], self.clock)
            first = dict(self.store.guide_row('telegram', '400020001', 'r2-v1'))
            self.clock.advance(120)
            deliver_guides(self.store, [channel], self.clock)
        final = dict(self.store.guide_row('telegram', '400020001', 'r2-v1'))
        self.assertEqual(first['status'], 'needs_review', first)
        self.assertEqual(len(attempts), 1, attempts)

    def test_guide_unknown_exception_and_missing_id_are_not_clear_success_or_failure(self):
        from tibo_reset_monitor.core.guide import deliver_guides

        def unknown(*args, **kwargs):
            raise ConnectionResetError('synthetic after acceptance')
        channel = SimpleNamespace(name='feishu', settings={}, targets=[], send_text=unknown)
        self.store.enqueue_guide('feishu', 'ou_r2_guide', 'r2-unknown', 'zh', self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        first = dict(self.store.guide_row('feishu', 'ou_r2_guide', 'r2-unknown'))
        channel.send_text = lambda *args, **kwargs: Receipt('sent', provider_message_id=' ')
        self.store.enqueue_guide('feishu', 'ou_r2_other', 'r2-invalid-id', 'zh', self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        second = dict(self.store.guide_row('feishu', 'ou_r2_other', 'r2-invalid-id'))
        self.assertEqual(first['status'], 'ambiguous', first)
        self.assertNotEqual(second['status'], 'sent', second)

    def test_active_guide_send_is_not_recovered_as_an_orphan(self):
        from tibo_reset_monitor.core.guide import deliver_guides
        attempts = []

        def send_text(recipient, text, *, idempotency_key):
            attempts.append(idempotency_key)
            if len(attempts) == 1:
                deliver_guides(self.store, [channel], self.clock)
            return Receipt('sent', provider_message_id='r2-guide-ack-' + str(len(attempts)))
        channel = SimpleNamespace(name='telegram', settings={}, targets=[], supports_idempotency=False, send_text=send_text)
        self.store.enqueue_guide('telegram', '400020002', 'r2-active', 'zh', self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        final = dict(self.store.guide_row('telegram', '400020002', 'r2-active'))
        self.assertEqual(len(attempts), 1, {'attempts': attempts, 'row': final})


class GuideRecoveryBoundaryTest(SafetyFixture):
    def test_legacy_attempt_without_a_body_cannot_be_reopened_or_sent(self):
        from tibo_reset_monitor.core.model import DeliveryState
        key = ("feishu", "ou_virtual_legacy", "virtual-version")
        self.store.enqueue_guide(*key, "en", self.clock.now())
        self.store.claim_guide(*key, self.clock.now())
        self.store.write_guide(*key, DeliveryState("retryable", 1, None, None, None, None, None), self.clock.now())
        calls = []
        channel = SimpleNamespace(name="feishu", settings={}, targets=[], supports_idempotency=True,
                                  send_text=lambda *args, **kwargs: calls.append(args) or Receipt("sent", provider_message_id="virtual-ack"))
        reopened = self.store.open_guide(*key, "en", self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        self.assertEqual(calls, [])
        self.assertFalse(reopened)
        self.assertEqual(self.store.guide_row(*key)["status"], "needs_review")

    def test_start_does_not_rearm_an_unknown_telegram_guide(self):
        calls = []
        channel = SimpleNamespace(name="telegram", settings={}, targets=[], supports_idempotency=False,
                                  send_text=lambda *args, **kwargs: calls.append(args) or Receipt("ambiguous"))
        self.store.enqueue_guide("telegram", "400030001", "virtual-version", "en", self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        self.assertFalse(self.store.open_guide("telegram", "400030001", "virtual-version", "en", self.clock.now()))
        self.clock.advance(3600)
        deliver_guides(self.store, [channel], self.clock)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.guide_row("telegram", "400030001", "virtual-version")["status"], "needs_review")

    def test_retry_keeps_original_guide_body_key_and_unknown_cutoff(self):
        calls = []
        def send(recipient, text, *, idempotency_key):
            calls.append((recipient, text, idempotency_key))
            return Receipt("ambiguous", detail="synthetic ACK loss")
        channel = SimpleNamespace(name="feishu", settings={"allow_self_subscribe": "false"}, targets=[],
                                  supports_idempotency=True, send_text=send)
        self.store.enqueue_guide("feishu", "ou_virtual_guide", "virtual-version", "en", self.clock.now())
        deliver_guides(self.store, [channel], self.clock)
        channel.settings["allow_self_subscribe"] = "true"
        self.clock.advance(120)
        deliver_guides(self.store, [channel], self.clock)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.clock.advance(3300)
        deliver_guides(self.store, [channel], self.clock)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.guide_row("feishu", "ou_virtual_guide", "virtual-version")["status"], "needs_review")

    def test_guide_lease_waits_then_recovers_from_original_submission_time(self):
        from datetime import timedelta
        from tibo_reset_monitor.core.model import parse_utc
        submitted = self.clock.now()
        key = ("feishu", "ou_virtual_lease", "virtual-version")
        self.store.enqueue_guide(*key, "en", submitted)
        self.store.save_guide_message(*key, "Virtual frozen guide")
        self.assertTrue(self.store.claim_guide(*key, submitted, lease_until=submitted + timedelta(seconds=600)))
        calls = []
        channel = SimpleNamespace(name="feishu", settings={}, targets=[], supports_idempotency=True,
                                  send_text=lambda *args, **kwargs: calls.append(args) or Receipt("ambiguous"))
        restarted = StateStore(self.store.path)
        try:
            self.clock.advance(599)
            deliver_guides(restarted, [channel], self.clock)
            self.assertEqual(calls, [])
            self.assertEqual(restarted.guide_row(*key)["status"], "inflight")
            self.clock.advance(1)
            deliver_guides(restarted, [channel], self.clock)
            row = restarted.guide_row(*key)
            self.assertEqual(len(calls), 1)
            self.assertEqual(parse_utc(row["first_ambiguous_at"]), submitted)
            self.clock.advance(2700)
            deliver_guides(restarted, [channel], self.clock)
            self.assertEqual(len(calls), 1)
            self.assertEqual(restarted.guide_row(*key)["status"], "needs_review")
        finally:
            restarted.close()

    def test_old_guide_ack_cannot_overwrite_a_later_claim(self):
        from tibo_reset_monitor.core.model import DeliveryState
        key = ("feishu", "ou_virtual_claim", "virtual-version")
        self.store.enqueue_guide(*key, "en", self.clock.now())
        self.store.claim_guide(*key, self.clock.now())
        self.store.write_guide(*key, DeliveryState("retryable", 1, None, None, None, None, None), self.clock.now())
        self.store.claim_guide(*key, self.clock.now())
        old_ack = DeliveryState("sent", 1, None, None, None, "virtual-late-ack", None)
        self.assertFalse(self.store.write_guide(*key, old_ack, self.clock.now(), only_status="inflight"))
        row = self.store.guide_row(*key)
        self.assertEqual((row["status"], row["attempts"]), ("inflight", 2))

    def test_legacy_clear_private_chat_failure_can_reopen_when_user_starts(self):
        from tibo_reset_monitor.core.model import DeliveryState
        key = ("telegram", "400030002", "virtual-version")
        self.store.enqueue_guide(*key, "en", self.clock.now())
        self.store.write_guide(*key, DeliveryState("skipped", 1, None, None, "private chat unavailable", None, None), self.clock.now())
        self.assertTrue(self.store.open_guide(*key, "en", self.clock.now()))
        calls = []
        channel = SimpleNamespace(name="telegram", settings={}, targets=[], supports_idempotency=False,
                                  send_text=lambda *args, **kwargs: calls.append(args) or Receipt("sent", provider_message_id="virtual-open-ack"))
        deliver_guides(self.store, [channel], self.clock)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.guide_row(*key)["status"], "sent")


if __name__ == "__main__":
    unittest.main()
