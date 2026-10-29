"""AIHOT client: ETag, rate limit, malformed bodies, and a realistic payload."""

from __future__ import annotations

import json
import unittest

from tests.support import SafetyFixture, make_activity, make_event, make_post, snapshot, utc
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
    def test_nested_types_duplicate_ids_and_bad_dates_reject_whole_snapshot(self):
        from copy import deepcopy
        from tibo_reset_monitor.core.source import SnapshotError
        original = snapshot([make_event("virtual", [make_post("virtual-post", "预告")])])
        mutations = [
            lambda b: b["events"].append(deepcopy(b["events"][0])),
            lambda b: b["events"][0].update(presentation=[]),
            lambda b: b["events"][0].update(schedule="invalid"),
            lambda b: b["events"][0].update(occurredOn="2026-02-30"),
            lambda b: b["events"][0].update(occurredOn="20261028"),
            lambda b: b["events"][0].update(updatedAt="2026-10-28T02:00:00"),
            lambda b: b["events"][0].update(confirmedAt="invalid"),
            lambda b: b["events"][0]["estimate"].update(through="2026-02-30T00:00:00Z"),
            lambda b: b["events"][0]["posts"][0].update(context={}),
            lambda b: b["events"][0]["posts"].append(deepcopy(b["events"][0]["posts"][0])),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                body = deepcopy(original)
                mutate(body)
                with self.assertRaises(SnapshotError):
                    validate_snapshot(body, utc())

    def test_future_estimate_is_valid_but_future_record_timestamp_is_not(self):
        from tibo_reset_monitor.core.source import SnapshotError
        body = snapshot([make_event("virtual", [make_post("virtual-post", "预告")])])
        body["events"][0]["estimate"]["through"] = "2027-01-01T00:00:00Z"
        validate_snapshot(body, utc())
        body["events"][0]["confirmedAt"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(SnapshotError):
            validate_snapshot(body, utc())

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

    def test_oversized_body_is_not_parsed(self):
        transport = FakeTransport(
            lambda _call: HttpResponse(200, {}, b"{" + b"x" * 2_000_000, "https://aihot.news/api")
        )
        client = SourceClient("https://aihot.news/api/v1/codex-resets/recent", transport=transport, user_agent="test-agent/0.1")
        result = client.fetch(None)
        assert result.failure is not None
        self.assertEqual(result.failure.kind, "malformed")
        self.assertIn("too large", result.failure.message)
        self.assertIsNone(result.body)


class SourceStringSafetyTest(SafetyFixture):
    def test_required_and_supplied_strings_fail_before_any_state_change(self):
        self.baseline()
        cases = [("post", "url", None), ("post", "url", {}), ("post", "url", ""),
                 ("post", "url", "https://"), ("post", "url", "relative/path"),
                 ("post", "text", None), ("post", "text", {}), ("post", "text", ""),
                 ("post", "originalText", None), ("post", "originalText", []),
                 ("post", "fullText", {}), ("activity", "originalText", None),
                 ("activity", "originalText", ""), ("activity", "text", {}),
                 ("activity", "url", {}), ("event", "type", None),
                 ("context", "author", []), ("context", "url", None),
                 ("root", "checkedAt", None), ("root", "timezone", {})]
        for index, (level, field, value) in enumerate(cases):
            with self.subTest(level=level, field=field, value=value):
                body = self.body(identity=f"synthetic-post-{index}")
                if level == "activity":
                    activity = make_activity(f"synthetic-activity-{index}", "confirm", "virtual-event")
                    activity["publishedAt"] = body["events"][0]["posts"][0]["publishedAt"]
                    body["activities"] = [activity]
                    record = activity
                elif level == "context":
                    record = {"id": "synthetic-context", "author": "synthetic", "relation": "quote",
                              "text": None, "originalText": "Synthetic original",
                              "url": "https://x.com/thsottiaux/status/synthetic-context"}
                    body["events"][0]["posts"][0]["context"] = [record]
                else:
                    record = {"post": body["events"][0]["posts"][0], "event": body["events"][0], "root": body}[level]
                if value is None:
                    del record[field]
                else:
                    record[field] = value
                previous = self.store.get_snapshot()
                seen = self.store.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
                notices = len(self.store.list_notices(100))
                sent_count = len(self.channel.sent)
                report = self.cycle(body)
                self.assertIsNotNone(report.fetch_error)
                self.assertEqual(self.store.get_snapshot(), previous)
                self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], seen)
                self.assertEqual(len(self.store.list_notices(100)), notices)
                self.assertEqual(len(self.channel.sent), sent_count)
                self.assertEqual(self.store.get_meta("automatic_blocked"), "1")
                recovered = self.cycle(self.body(identity=f"synthetic-post-{index}"))
                self.assertIsNone(recovered.fetch_error)
                self.assertEqual(len(self.channel.sent), sent_count + 1)

    def test_nullable_translation_and_unknown_nonempty_activity_kind_remain_valid(self):
        self.baseline()
        activity = make_activity("synthetic-null-translation", "confirm", None)
        activity["text"] = None
        activity["publishedAt"] = self.body()["events"][0]["posts"][0]["publishedAt"]
        report = self.cycle(snapshot([], [activity]))
        self.assertIsNone(report.fetch_error)
        self.assertEqual(len(self.channel.sent), 1)
        unknown = dict(activity, id="synthetic-future-activity", kind="future_kind", action=None)
        report = self.cycle(snapshot([], [activity, unknown]))
        self.assertIsNone(report.fetch_error)
        self.assertEqual(len(self.channel.sent), 1)


if __name__ == "__main__":
    unittest.main()
