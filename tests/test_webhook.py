import json
import os
import unittest

from tibo_reset_monitor.channels.webhook import WebhookChannel
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import HttpResponse
from tibo_reset_monitor.core.model import MANUAL, Notice, Target


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def send(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return HttpResponse(202, {}, b'{"id":"wh_1"}', url)


class WebhookTest(unittest.TestCase):
    def test_posts_json_with_idempotency_header(self):
        os.environ["TIBO_WEBHOOK_EXAMPLE_URL"] = "https://example.invalid/hook"
        self.addCleanup(os.environ.pop, "TIBO_WEBHOOK_EXAMPLE_URL", None)
        channel = WebhookChannel(
            ChannelSpec(name="webhook", enabled=True, settings={}, targets=[]),
            FakeTransport(),
            clock=None,
        )
        target = Target("webhook", "hook", "personal", "", {"url_env": "TIBO_WEBHOOK_EXAMPLE_URL"})
        notice = Notice(
            dedupe_key="m",
            post_key="m",
            stage=MANUAL,
            stage_label="测试",
            title="测试",
            display_label="tibo-reset-monitor",
            author="tibo-reset-monitor",
            text="hello",
            original_text="",
            url="https://aihot.news/codex-reset",
            published_at="2026-10-28T00:00:00Z",
        )
        receipt = channel.send(channel.render(notice, target), target, "trm-key")
        self.assertEqual(receipt.outcome, "sent")
        self.assertEqual(receipt.provider_message_id, "wh_1")
        call = channel._transport.calls[0]
        self.assertEqual(call["headers"]["Idempotency-Key"], "trm-key")
        body = json.loads(call["body"].decode())
        self.assertEqual(body["idempotency_key"], "trm-key")
        self.assertEqual(body["target"]["kind"], "personal")
        self.assertEqual(body["notice"]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
