"""Telegram request formation, HTML escaping, and error mapping."""

from __future__ import annotations

import json
import os
import unittest

from tests.support import make_event, make_post, snapshot
from tibo_reset_monitor.channels.telegram import TelegramChannel, escape_html
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.http import HttpResponse, TransportError
from tibo_reset_monitor.core.model import Target


class FakeTransport:
    def __init__(self, responses) -> None:
        self.calls = []
        self._responses = list(responses)

    def send(self, method, url, headers, body, timeout):
        call = {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        self.calls.append(call)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _channel(transport) -> TelegramChannel:
    return TelegramChannel(
        ChannelSpec(
            name="telegram",
            enabled=True,
            settings={"api_base": "https://api.telegram.org", "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN"},
            targets=[],
        ),
        transport,
        clock=None,
    )


class TelegramRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456789:example-token-value-not-real"
        self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)
        self.notice = extract_notices(
            snapshot([make_event("reset-1", [make_post("p1", "预告", text="a <b>bold</b> & more", original="Just kidding")])])
        )[0]

    def test_group_and_personal_html(self):
        transport = FakeTransport(
            [
                HttpResponse(200, {}, json.dumps({"ok": True, "result": {"message_id": 10}}).encode(), "https://api.telegram.org/bot<redacted>/sendMessage"),
                HttpResponse(200, {}, json.dumps({"ok": True, "result": {"message_id": 11}}).encode(), "https://api.telegram.org/bot<redacted>/sendMessage"),
            ]
        )
        channel = _channel(transport)
        group = Target("telegram", "ops", "group", "-1001234567890")
        person = Target("telegram", "me", "personal", "424242")
        for target in (group, person):
            receipt = channel.send(channel.render(self.notice, target), target, "ignored-by-telegram")
            self.assertEqual(receipt.outcome, "sent")
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertTrue(transport.calls[0]["url"].endswith("/bot123456789:example-token-value-not-real/sendMessage"))
        group_body = json.loads(transport.calls[0]["body"].decode())
        person_body = json.loads(transport.calls[1]["body"].decode())
        self.assertEqual(group_body["chat_id"], "-1001234567890")
        self.assertEqual(person_body["chat_id"], "424242")
        self.assertEqual(group_body["parse_mode"], "HTML")
        self.assertEqual(group_body["link_preview_options"]["url"], "https://x.com/thsottiaux/status/p1")
        self.assertFalse(group_body["link_preview_options"]["is_disabled"])
        text = group_body["text"]
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", text)
        self.assertIn("&amp; more", text)
        self.assertIn("<b>", text)
        self.assertIn("查看原帖", text)
        self.assertIn("来源帖子的发布时间", text)
        self.assertIn(escape_html("Just kidding"), text)
        self.assertNotIn("<b>bold</b>", text)

    def test_error_codes(self):
        transport = FakeTransport(
            [
                HttpResponse(
                    429,
                    {},
                    json.dumps(
                        {
                            "ok": False,
                            "error_code": 429,
                            "description": "Too Many Requests: retry after 9",
                            "parameters": {"retry_after": 9},
                        }
                    ).encode(),
                    "u",
                ),
                HttpResponse(
                    403,
                    {},
                    json.dumps({"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}).encode(),
                    "u",
                ),
                HttpResponse(
                    400,
                    {},
                    json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}).encode(),
                    "u",
                ),
                TransportError("timeout talking to /bot<redacted>/sendMessage", ambiguous=True),
            ]
        )
        channel = _channel(transport)
        target = Target("telegram", "me", "personal", "424242")
        message = channel.render(self.notice, target)
        limited = channel.send(message, target, "k")
        blocked = channel.send(message, target, "k")
        missing = channel.send(message, target, "k")
        timed = channel.send(message, target, "k")
        self.assertEqual(limited.outcome, "retryable")
        self.assertEqual(limited.retry_after_seconds, 9)
        self.assertIn("429", limited.detail)
        self.assertEqual(blocked.outcome, "permanent")
        self.assertIn("blocked", blocked.detail)
        self.assertIn("Start", blocked.detail)
        self.assertEqual(missing.outcome, "permanent")
        self.assertIn("chat not found", missing.detail)
        self.assertIn("getUpdates", missing.detail)
        self.assertEqual(timed.outcome, "ambiguous")

    def test_missing_token_is_retryable(self):
        os.environ.pop("TIBO_TELEGRAM_BOT_TOKEN", None)
        transport = FakeTransport([])
        channel = _channel(transport)
        target = Target("telegram", "me", "personal", "424242")
        receipt = channel.send(channel.render(self.notice, target), target, "k")
        self.assertEqual(receipt.outcome, "retryable")
        self.assertIn("TIBO_TELEGRAM_BOT_TOKEN", receipt.detail)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
