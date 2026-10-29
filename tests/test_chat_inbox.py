"""Telegram update parsing and the optional Feishu listener."""

from __future__ import annotations

import json
import os
import unittest

from tests.support import FakeClock, TempStoreTest
from tibo_reset_monitor.channels.feishu_inbox import (
    feishu_sdk_available,
    handle_feishu_event,
    inbound_from_fields,
    list_bot_chats,
    user_in_chat,
    text_from_content,
)
from tibo_reset_monitor.channels.telegram_inbox import TelegramInbox, parse_update
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.channels.telegram import TelegramChannel
from tibo_reset_monitor.core.chat import ChatController
from tibo_reset_monitor.core.http import HttpResponse


class ParseUpdateTest(unittest.TestCase):
    def test_message_membership_and_migration(self):
        private = parse_update(
            {
                "update_id": 7,
                "message": {"message_id": 1, "text": "/subscribe@Bot", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}},
            }
        )
        assert private is not None
        self.assertEqual(private.scope, "personal")
        self.assertEqual(private.user_id, "42")
        self.assertEqual(private.text, "/subscribe@Bot")
        joined = parse_update(
            {
                "update_id": 8,
                "my_chat_member": {
                    "chat": {"id": -100, "title": "Ops", "type": "supergroup"},
                    "new_chat_member": {"status": "member"},
                },
            }
        )
        assert joined is not None
        self.assertEqual(joined.membership, "joined")
        self.assertEqual(joined.chat_title, "Ops")
        moved = parse_update(
            {
                "update_id": 9,
                "message": {
                    "text": "",
                    "from": {"id": 42},
                    "chat": {"id": -100, "type": "group", "title": "Ops"},
                    "migrate_to_chat_id": -100999,
                },
            }
        )
        assert moved is not None
        self.assertEqual(moved.migrate_to, "-100999")


class InboxPollTest(TempStoreTest):
    def test_poll_replies_and_persists_offset(self):
        os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456:test-token-value"
        self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)

        class Transport:
            def __init__(self) -> None:
                self.calls = []

            def send(self, method, url, headers, body, timeout):
                self.calls.append({"url": url, "body": body, "timeout": timeout})
                if url.endswith("/getUpdates"):
                    payload = {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 4,
                                "message": {
                                    "text": "/help",
                                    "from": {"id": 42},
                                    "chat": {"id": 42, "type": "private"},
                                },
                            }
                        ],
                    }
                    return HttpResponse(200, {}, json.dumps(payload).encode(), url)
                return HttpResponse(200, {}, b'{"ok":true,"result":true}', url)

        channel = TelegramChannel(
            ChannelSpec(
                name="telegram",
                enabled=True,
                settings={"api_base": "https://api.telegram.org", "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN", "language": "zh"},
                targets=[],
            ),
            Transport(),
            clock=None,
        )
        controller = ChatController(self.store, FakeClock(), channel="telegram", language="zh", admins=("42",))
        inbox = TelegramInbox(channel, controller, channel._transport)
        inbox.poll_once()
        urls = [call["url"] for call in channel._transport.calls]
        self.assertTrue(any(url.endswith("/getUpdates") for url in urls))
        self.assertTrue(any(url.endswith("/sendMessage") for url in urls))
        self.assertEqual(self.store.get_meta("telegram_update_offset"), "5")
        sent = json.loads(channel._transport.calls[-1]["body"].decode())
        self.assertNotIn("123456:test-token-value", sent["text"])
        self.assertGreaterEqual(channel._transport.calls[0]["timeout"], 35)

    def test_webhook_conflict_is_reported_once(self):
        os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456:test-token-value"
        self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)

        class Transport:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, method, url, headers, body, timeout):
                self.calls += 1
                return HttpResponse(409, {}, b'{"ok":false,"error_code":409,"description":"Conflict: can\\u0027t use getUpdates while webhook is active"}', url)

        channel = TelegramChannel(
            ChannelSpec(
                name="telegram",
                enabled=True,
                settings={"api_base": "https://api.telegram.org", "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN"},
                targets=[],
            ),
            Transport(),
            clock=None,
        )
        inbox = TelegramInbox(channel, ChatController(self.store, FakeClock(), channel="telegram", language="en"), channel._transport)
        with self.assertLogs("tibo_reset_monitor", level="ERROR") as captured:
            inbox.poll_once()
        inbox.poll_once()
        self.assertEqual(channel._transport.calls, 1)
        self.assertIn("webhook", "\n".join(captured.output).lower())

    def test_a_broken_update_still_advances_the_offset(self):
        os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456:AAAAAAAAAAAAAAAAAAAA"
        self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)

        class Boom:
            def handle(self, inbound):
                raise RuntimeError("boom 123456:AAAAAAAAAAAAAAAAAAAA")

        class Transport:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, method, url, headers, body, timeout):
                self.calls += 1
                payload = {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 9,
                            "message": {
                                "text": "/help",
                                "from": {"id": 42},
                                "chat": {"id": 42, "type": "private"},
                            },
                        }
                    ],
                }
                return HttpResponse(200, {}, json.dumps(payload).encode(), url)

        channel = TelegramChannel(
            ChannelSpec(
                name="telegram",
                enabled=True,
                settings={"api_base": "https://api.telegram.org", "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN"},
                targets=[],
            ),
            Transport(),
            clock=None,
        )
        inbox = TelegramInbox(channel, Boom(), channel._transport)
        inbox.controller.store = self.store
        with self.assertLogs("tibo_reset_monitor", level="WARNING") as captured:
            inbox.poll_once()
        self.assertEqual(self.store.get_meta("telegram_update_offset"), "10")
        text = "\n".join(captured.output)
        self.assertNotIn("123456:AAAAAAAAAAAAAAAAAAAA", text)
        self.assertIn("<redacted-token>", text)

    def test_unreadable_offset_does_not_stop_polling(self):
        os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456:test-token-value"
        self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)
        self.store.set_meta("telegram_update_offset", "not-a-number")
        self.store.conn.commit()

        class Transport:
            def __init__(self) -> None:
                self.bodies = []

            def send(self, method, url, headers, body, timeout):
                self.bodies.append(body)
                return HttpResponse(200, {}, b'{"ok":true,"result":[]}', url)

        channel = TelegramChannel(
            ChannelSpec(
                name="telegram",
                enabled=True,
                settings={"api_base": "https://api.telegram.org", "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN"},
                targets=[],
            ),
            Transport(),
            clock=None,
        )
        inbox = TelegramInbox(channel, ChatController(self.store, FakeClock(), channel="telegram", language="zh"), channel._transport)
        with self.assertLogs("tibo_reset_monitor", level="WARNING"):
            inbox.poll_once()
        sent = json.loads(channel._transport.bodies[0].decode())
        self.assertNotIn("offset", sent)


class FeishuInboxTest(unittest.TestCase):
    def test_membership_pages_are_checked_and_errors_fail_closed(self):
        class Channel:
            _api_base = "https://open.feishu.cn"
            _timeout = 5
            def _access_token(self, force):
                return "virtual-token"
        class Transport:
            def __init__(self):
                self.pages = [
                    {"code": 0, "data": {"items": [], "has_more": True, "page_token": "next"}},
                    {"code": 0, "data": {"items": [{"member_id_type": "open_id", "member_id": "ou_virtual"}], "has_more": False}},
                ]
            def send(self, method, url, headers, body, timeout):
                return HttpResponse(200, {}, json.dumps(self.pages.pop(0)).encode(), url)
        channel = Channel()
        channel._transport = Transport()
        self.assertTrue(user_in_chat(channel, "ou_virtual", "oc_virtual"))
        for invalid in [{"code": 0, "data": []}, {"code": 0, "data": {"items": [], "has_more": True}}, {"data": {"items": []}}]:
            channel._transport.pages = [invalid]
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                user_in_chat(channel, "ou_virtual", "oc_virtual")

    def test_keeps_only_needed_fields(self):
        self.assertEqual(text_from_content('{"text":"#添加群聊"}'), "#添加群聊")
        inbound = inbound_from_fields(
            message_id="om_1",
            chat_id="oc_chat",
            chat_type="p2p",
            open_id="ou_user",
            text="#状态",
        )
        self.assertEqual(inbound.scope, "personal")
        self.assertEqual(inbound.user_id, "ou_user")
        self.assertEqual(inbound.event_id, "om_1")

    def test_chat_list_follows_pages(self):
        class Channel:
            _api_base = "https://open.feishu.cn"
            _timeout = 5
            _app_id = "cli_example"

            def __init__(self) -> None:
                self.calls = 0

            def _access_token(self, force):
                return "t-token"

            def _transport_send(self, method, url, headers, body, timeout):
                self.calls += 1
                if "page_token" not in url:
                    payload = {"code": 0, "data": {"items": [{"chat_id": "oc_a", "name": "A"}], "has_more": True, "page_token": "next"}}
                else:
                    payload = {"code": 0, "data": {"items": [{"chat_id": "oc_b", "name": "B"}], "has_more": False}}
                return HttpResponse(200, {}, json.dumps(payload).encode(), url)

        channel = Channel()
        channel._transport = type("T", (), {"send": channel._transport_send})()
        chats = list_bot_chats(channel)
        self.assertEqual(chats, [("oc_a", "A"), ("oc_b", "B")])
        self.assertNotIn("t-token", json.dumps(chats))

    def test_a_handler_failure_does_not_escape(self):
        class Event:
            class event:
                class message:
                    message_id = "om_1"
                    chat_id = "oc_1"
                    chat_type = "p2p"
                    content = '{"text":"/help"}'

                class sender:
                    sender_type = "user"
                    class sender_id:
                        open_id = "ou_1"

        class Boom:
            def handle(self, inbound):
                raise RuntimeError("boom 123456:AAAAAAAAAAAAAAAAAAAA")

        channel = type("C", (), {"_timeout": 5, "_api_base": "https://open.feishu.cn", "_access_token": lambda self, force: None})()
        with self.assertLogs("tibo_reset_monitor", level="WARNING") as captured:
            handle_feishu_event(channel, Boom(), Event())
        text = "\n".join(captured.output)
        self.assertIn("could not be handled", text)
        self.assertNotIn("123456:AAAAAAAAAAAAAAAAAAAA", text)

    def test_sdk_flag_matches_the_environment(self):
        try:
            import lark_oapi  # noqa: F401
        except ImportError:
            self.assertFalse(feishu_sdk_available())
        else:
            self.assertTrue(feishu_sdk_available())


if __name__ == "__main__":
    unittest.main()
