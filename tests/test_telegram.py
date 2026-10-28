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

    def test_group_and_personal_are_one_photo_message(self):
        png = b"\x89PNG\r\n\x1a\ncalendar"
        transport = FakeTransport(
            [
                HttpResponse(200, {}, json.dumps({"ok": True, "result": {"message_id": 10}}).encode(), "u"),
                HttpResponse(200, {}, json.dumps({"ok": True, "result": {"message_id": 11}}).encode(), "u"),
            ]
        )
        channel = _channel(transport)
        channel.bind_calendar(lambda: png)
        group = Target("telegram", "ops", "group", "-1001234567890")
        person = Target("telegram", "me", "personal", "424242")
        for target in (group, person):
            before = len(transport.calls)
            receipt = channel.send(channel.render(self.notice, target), target, "ignored-by-telegram")
            self.assertEqual(receipt.outcome, "sent")
            self.assertEqual(len(transport.calls), before + 1)
        self.assertTrue(transport.calls[0]["url"].endswith("/sendPhoto"))
        self.assertTrue(transport.calls[1]["url"].endswith("/sendPhoto"))
        self.assertNotIn("sendMessage", transport.calls[0]["url"])
        group_parts = _form(transport.calls[0])
        person_parts = _form(transport.calls[1])
        self.assertEqual(group_parts["chat_id"], "-1001234567890")
        self.assertEqual(person_parts["chat_id"], "424242")
        self.assertEqual(group_parts["parse_mode"], "HTML")
        self.assertEqual(group_parts["show_caption_above_media"], "true")
        self.assertTrue(group_parts["photo"].startswith(b"\x89PNG"))
        caption = group_parts["caption"]
        self.assertLessEqual(_parsed_len(caption), 1024)
        self.assertIn("<b>Tibo</b>", caption)
        self.assertIn("@thsottiaux · X", caption)
        self.assertIn("<code>重置（形式未明确）</code>", caption)
        self.assertIn("<code>预告</code>", caption)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", caption)
        self.assertIn("&amp; more", caption)
        self.assertIn("帖子发布 · 2026-10-28 10:00:00（北京时间）", caption)
        self.assertIn("帖子未明确适用范围", caption)
        self.assertIn('href="https://x.com/thsottiaux/status/p1"', caption)
        self.assertIn("查看原帖", caption)
        self.assertIn("<blockquote>", caption)
        self.assertIn("Just kidding", caption)
        self.assertNotIn("<b>bold</b>", caption)

    def test_long_body_is_truncated_inside_the_caption_limit(self):
        from tibo_reset_monitor.channels.telegram import build_caption, parsed_length
        from tibo_reset_monitor.core.model import Notice

        notice = Notice(
            dedupe_key="k",
            post_key="p",
            stage="announcement",
            stage_label="预告",
            title="标题",
            display_label="重置（形式未明确）",
            author="thsottiaux",
            text="测" * 5000 + " <end>",
            original_text="",
            url="https://x.com/thsottiaux/status/999",
            published_at="2026-10-28T10:00:00+08:00",
            tag_label="重置（形式未明确）",
        )
        caption = build_caption(notice, 1024)
        self.assertLessEqual(parsed_length(caption), 1024)
        self.assertIn("…", caption)
        self.assertIn("查看原帖", caption)
        self.assertNotIn("<end>", caption)

    def test_missing_calendar_falls_back_to_one_text_message(self):
        transport = FakeTransport(
            [
                HttpResponse(200, {}, json.dumps({"ok": True, "result": {"message_id": 12}}).encode(), "u"),
            ]
        )
        channel = _channel(transport)
        channel.bind_calendar(lambda: None)
        target = Target("telegram", "me", "personal", "424242")
        with self.assertLogs("tibo_reset_monitor", level="WARNING") as captured:
            receipt = channel.send(channel.render(self.notice, target), target, "k")
        self.assertEqual(receipt.outcome, "sent")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(transport.calls[0]["url"].endswith("/sendMessage"))
        body = json.loads(transport.calls[0]["body"].decode())
        self.assertEqual(body["chat_id"], "424242")
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertLessEqual(_parsed_len(body["text"]), 4096)
        self.assertIn("<b>Tibo</b>", body["text"])
        self.assertIn("&amp; more", body["text"])
        self.assertEqual(body["link_preview_options"]["url"], "https://x.com/thsottiaux/status/p1")
        self.assertTrue(any("calendar" in line for line in captured.output))

    def test_caption_includes_original_post_preview(self):
        from tests.support import make_event, make_post, snapshot
        from tibo_reset_monitor.channels.telegram import build_caption
        from tibo_reset_monitor.core.dedupe import extract_notices

        post = make_post("p9", "预告", text="短译", original="short")
        post["text"] = "搞定了。明天见，会有好消息。"
        post["fullText"] = post["text"]
        post["originalText"] = "And it's done."
        post["fullOriginalText"] = "And it's done. See you tomorrow for some good news."
        post["publishedAt"] = "2026-10-28T14:29:38.000+08:00"
        post["context"] = [
            {
                "id": "quoted",
                "author": "thsottiaux",
                "relation": "quote",
                "text": "太平洋时间午夜前落地。",
                "originalText": "Lands by midnight PST.",
                "url": "https://x.com/thsottiaux/status/quoted",
            }
        ]
        notice = extract_notices(snapshot([make_event("reset-9", [post], title="预告")]))[0]
        self.assertEqual(notice.original_text, "And it's done. See you tomorrow for some good news.")
        self.assertEqual(notice.contexts[0]["relation"], "quote")
        caption = build_caption(notice, 1024, (2026, 10))
        self.assertLessEqual(_parsed_len(caption), 1024)
        self.assertIn("<code>重置（形式未明确）</code> <code>预告</code>", caption)
        self.assertIn("搞定了。明天见，会有好消息。", caption)
        self.assertIn("帖子未明确适用范围", caption)
        self.assertIn("帖子发布 · 2026-10-28 14:29:38（北京时间）", caption)
        self.assertIn("<blockquote>", caption)
        self.assertIn("原帖 · Tibo @thsottiaux", caption)
        self.assertIn("And it's done. See you tomorrow for some good news.", caption)
        self.assertIn("引用 @thsottiaux", caption)
        self.assertIn("Lands by midnight PST.", caption)
        self.assertIn("2026-10-28 14:29:38 · 北京时间", caption)
        self.assertIn('href="https://x.com/thsottiaux/status/p9"', caption)
        self.assertIn("重置日历 · 2026 年 10 月", caption)
        self.assertNotIn("重置确认：形式未明确", caption)

    def test_long_preview_is_truncated_before_tags_and_link(self):
        from tibo_reset_monitor.channels.telegram import build_caption
        from tibo_reset_monitor.core.model import Notice

        notice = Notice(
            dedupe_key="k",
            post_key="p",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="thsottiaux",
            text="主" * 3000,
            original_text="orig " * 800,
            url="https://x.com/thsottiaux/status/1",
            published_at="2026-10-28T14:29:38+08:00",
            tag_label="重置（形式未明确）",
            contexts=(
                {
                    "relation": "quote",
                    "author": "thsottiaux",
                    "original_text": "quote " * 400,
                    "text": "",
                    "url": "",
                },
            ),
        )
        caption = build_caption(notice, 1024, (2026, 10))
        self.assertLessEqual(_parsed_len(caption), 1024)
        self.assertIn("…", caption)
        self.assertIn("<code>重置（形式未明确）</code>", caption)
        self.assertIn("<code>预告</code>", caption)
        self.assertIn("帖子发布 · 2026-10-28 14:29:38（北京时间）", caption)
        self.assertIn("查看原帖", caption)
        self.assertIn("重置日历 · 2026 年 10 月", caption)

    def test_manual_and_other_authors(self):
        from tibo_reset_monitor.channels.telegram import build_caption
        from tibo_reset_monitor.core.model import MANUAL, Notice

        manual = Notice(
            dedupe_key="m",
            post_key="m",
            stage=MANUAL,
            stage_label="手动",
            title="别的标题",
            display_label="x",
            author="tibo-reset-monitor",
            text="hello <b>",
            original_text="",
            url="https://aihot.news/codex-reset",
            published_at="2026-10-28T00:00:00Z",
        )
        text = build_caption(manual, 4096)
        self.assertIn("<b>手动提醒</b>", text)
        self.assertIn("手动发送", text)
        self.assertIn("hello &lt;b&gt;", text)
        self.assertNotIn("帖子未明确适用范围", text)
        other = Notice(
            dedupe_key="o",
            post_key="o",
            stage="confirmation",
            stage_label="确认",
            title="",
            display_label="",
            author="someone",
            text="hi",
            original_text="",
            url="https://x.com/someone/status/5",
            published_at="2026-10-28T01:02:03Z",
            tag_label="重置卡",
            products_zh="Codex",
            type_name="reset_credit",
        )
        other_text = build_caption(other, 1024)
        self.assertIn("<b>@someone</b>", other_text)
        self.assertIn("<i>X</i>", other_text)
        self.assertIn("<code>重置卡</code>", other_text)
        self.assertIn("<code>确认</code>", other_text)
        self.assertIn("Codex", other_text)
        self.assertIn("2026-10-28 09:02:03", other_text)
        host = Notice(
            dedupe_key="h",
            post_key="h",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="",
            text="hi",
            original_text="",
            url="https://example.com/post",
            published_at="",
            tag_label="重置（形式未明确）",
        )
        host_text = build_caption(host, 1024)
        self.assertIn("<b>example.com</b>", host_text)
        self.assertIn("<i>example.com</i>", host_text)

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


def _parsed_len(html: str) -> int:
    from tibo_reset_monitor.channels.telegram import parsed_length

    return parsed_length(html)


def _form(call: dict) -> dict:
    content_type = call["headers"]["Content-Type"]
    boundary = content_type.split("boundary=", 1)[1].strip().encode()
    parts: dict = {}
    for chunk in call["body"].split(b"--" + boundary):
        if b"Content-Disposition" not in chunk:
            continue
        header, _, data = chunk.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name = header.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        parts[name] = data if name == "photo" else data.decode("utf-8")
    return parts


if __name__ == "__main__":
    unittest.main()
