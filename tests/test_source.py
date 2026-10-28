"""AIHOT client: ETag, rate limit, malformed bodies, and a realistic payload."""

from __future__ import annotations

import json
import unittest

from tests.support import make_activity, make_event, make_post, snapshot
from tibo_reset_monitor.core.http import HttpResponse
from tibo_reset_monitor.core.source import SourceClient, validate_snapshot


class FakeTransport:
    def __init__(self, responder) -> None:
        self.calls = []
        self._responder = responder

    def send(self, method, url, headers, body, timeout):
        call = {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        self.calls.append(call)
        result = self._responder(call)
        if isinstance(result, Exception):
            raise result
        return result


class SourceClientTest(unittest.TestCase):
    def test_sends_etag_gzip_and_keeps_snapshot_on_304(self):
        body = snapshot([make_event("reset-1", [make_post("p1", "预告")])])
        payloads = [
            HttpResponse(200, {"etag": 'W/"abc"', "content-type": "application/json"}, json.dumps(body).encode(), "https://aihot.news/api"),
            HttpResponse(304, {"etag": 'W/"abc"'}, b"", "https://aihot.news/api"),
        ]

        def responder(_call):
            return payloads.pop(0)

        transport = FakeTransport(responder)
        client = SourceClient("https://aihot.news/api/v1/codex-resets/recent", transport=transport, user_agent="test-agent/0.1")
        first = client.fetch(None)
        self.assertEqual(first.body["events"][0]["id"], "reset-1")
        self.assertEqual(first.etag, 'W/"abc"')
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["Accept-Encoding"], "gzip")
        self.assertEqual(headers["User-Agent"], "test-agent/0.1")
        self.assertNotIn("If-None-Match", headers)
        second = client.fetch('W/"abc"')
        self.assertTrue(second.not_modified)
        self.assertIsNone(second.body)
        self.assertEqual(transport.calls[1]["headers"]["If-None-Match"], 'W/"abc"')

    def test_rate_limit_and_malformed_are_failures(self):
        responses = [
            HttpResponse(429, {"retry-after": "17", "content-type": "application/problem+json"}, b'{"title":"Slow down","detail":"slow down","status":429,"code":"rate_limit","type":"about:blank","requestId":"r"}', "https://aihot.news/api"),
            HttpResponse(200, {"content-type": "application/json"}, b"{", "https://aihot.news/api"),
            HttpResponse(200, {"content-type": "application/json"}, b'{"schemaVersion":2,"timezone":"Asia/Shanghai","checkedAt":null,"historyFrom":"x","count":0,"events":[]}', "https://aihot.news/api"),
        ]

        def responder(_call):
            return responses.pop(0)

        client = SourceClient("https://example.test/recent", transport=FakeTransport(responder), user_agent="ua")
        limited = client.fetch(None)
        self.assertEqual(limited.failure.kind, "rate_limit")
        self.assertEqual(limited.failure.retry_after_seconds, 17)
        self.assertIsNone(limited.body)
        malformed = client.fetch(None)
        self.assertEqual(malformed.failure.kind, "malformed")
        version = client.fetch(None)
        self.assertEqual(version.failure.kind, "malformed")
        self.assertIn("schemaVersion", version.failure.message)

    def test_validate_accepts_activity_feed(self):
        body = snapshot(
            [make_event("reset-1", [make_post("p1", "预告"), make_post("p2", "补充说明")])],
            [
                make_activity("p2", "amend", "reset-1"),
                make_activity("p1", "announce", "reset-1"),
                make_activity("other", None, None, kind="related"),
            ],
        )
        self.assertEqual(validate_snapshot(body)["count"], 1)


if __name__ == "__main__":
    unittest.main()
