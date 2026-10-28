"""The chat listener and the poll loop share one SQLite file from two threads."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.support import FakeClock, make_event, make_post, snapshot, utc
from tibo_reset_monitor.config import load_config
from tibo_reset_monitor.core.http import HttpResponse
from tibo_reset_monitor.service import build_runtime, run_forever, run_telegram_chat_loop


class RoutingTransport:
    """One transport for the feed and the Telegram API. Returns immediately."""

    def __init__(self, runtime_holder: dict) -> None:
        self.runtime_holder = runtime_holder
        self.sent: list[dict] = []
        self._updates = 0

    def send(self, method, url, headers, body, timeout):
        if url.endswith("/getUpdates"):
            self._updates += 1
            if self._updates == 1:
                payload = {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "text": "/subscribe",
                                "from": {"id": 6173752247},
                                "chat": {"id": 6173752247, "type": "private"},
                            },
                        }
                    ],
                }
            else:
                payload = {"ok": True, "result": []}
            return HttpResponse(200, {}, json.dumps(payload).encode(), url)
        if "api.telegram.org" in url:
            self.sent.append({"url": url, "body": body})
            return HttpResponse(200, {}, b'{"ok":true,"result":{"message_id":1}}', url)
        if "codex-resets" in url:
            return self._feed(headers, url)
        return HttpResponse(500, {}, b"", url)

    def _feed(self, headers, url):
        runtime = self.runtime_holder["runtime"]
        active = [item for item in runtime.store.subscriptions("telegram") if item["status"] == "active"]
        if active:
            body = snapshot(
                [make_event("old", [make_post("old", "预告")]), make_event("new", [make_post("new", "确认", text="brand new")])]
            )
            return HttpResponse(200, {"etag": 'W/"v2"'}, json.dumps(body).encode(), url)
        if headers.get("If-None-Match") == 'W/"v1"':
            return HttpResponse(304, {"etag": 'W/"v1"'}, b"", url)
        body = snapshot([make_event("old", [make_post("old", "预告")])])
        return HttpResponse(200, {"etag": 'W/"v1"'}, json.dumps(body).encode(), url)


class ChatThreadIntegrationTest(unittest.TestCase):
    def test_run_loop_and_chat_thread_share_one_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "monitor.sqlite3"
            config_path = root / "config-chat.json"
            config_path.write_text(
                json.dumps(
                    {
                        "state": {"path": str(db)},
                        "source": {"poll_interval_seconds": 60},
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "chat_control": True,
                                "admins": ["6173752247"],
                                "targets": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TIBO_TELEGRAM_BOT_TOKEN"] = "123456:test-token-value-not-real"
            self.addCleanup(os.environ.pop, "TIBO_TELEGRAM_BOT_TOKEN", None)
            holder: dict = {}
            transport = RoutingTransport(holder)
            config = load_config(config_path, environ={})
            runtime = build_runtime(config, transport=transport, clock=FakeClock(utc()))
            runtime.config.poll_interval_seconds = 0.2
            holder["runtime"] = runtime
            self.addCleanup(runtime.close)
            self.assertFalse(str(db).startswith(":memory:"))
            self.assertTrue(db.parent.is_dir())

            deadline = time.time() + 8

            def sleeper(_seconds: float) -> None:
                active = [item for item in runtime.store.subscriptions("telegram") if item["status"] == "active"]
                delivered = any(b"brand new" in item["body"] for item in transport.sent)
                if active and delivered:
                    raise KeyboardInterrupt
                if time.time() > deadline:
                    raise AssertionError(
                        f"timed out updates={transport._updates} active={bool(active)} sent={len(transport.sent)}"
                    )

            started = time.time()
            original_sleep = time.sleep

            def quick_sleep(seconds: float) -> None:
                original_sleep(0.01 if seconds > 0.01 else seconds)

            time.sleep = quick_sleep
            try:
                code = run_forever(runtime, sleep=sleeper)
            finally:
                time.sleep = original_sleep
            self.assertEqual(code, 0)
            self.assertLess(time.time() - started, 15)
            active = [item for item in runtime.store.subscriptions("telegram") if item["status"] == "active"]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["address"], "6173752247")
            self.assertEqual(active[0]["kind"], "personal")
            # A second connection, like the other thread, can read the same file.
            other = sqlite3.connect(db)
            try:
                row = other.execute(
                    "SELECT address, status FROM subscription WHERE channel = 'telegram'"
                ).fetchone()
            finally:
                other.close()
            self.assertEqual(tuple(row), ("6173752247", "active"))
            self.assertTrue(any(b"brand new" in item["body"] for item in transport.sent))

    def test_failures_log_the_reason_and_back_off(self):
        class Boom:
            def __init__(self) -> None:
                self.calls = 0

            def poll_once(self) -> None:
                self.calls += 1
                if self.calls < 3:
                    raise sqlite3.ProgrammingError(
                        "SQLite objects created in a thread can only be used in that same thread; "
                        "token 123456:test-token-value-not-real pairing code (single use, expires in 10 minutes): abcdef"
                    )

        waits: list[float] = []

        def pause(seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) >= 3

        with self.assertLogs("tibo_reset_monitor", level="WARNING") as captured:
            run_telegram_chat_loop(Boom(), threading.Event(), pause)
        self.assertEqual(waits, [1.0, 2.0, 1.0])
        text = "\n".join(captured.output)
        self.assertIn("SQLite objects created in a thread", text)
        self.assertNotIn("123456:test-token-value-not-real", text)
        self.assertNotIn("abcdef", text)
        self.assertIn("<redacted>", text)


if __name__ == "__main__":
    unittest.main()
