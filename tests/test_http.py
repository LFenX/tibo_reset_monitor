"""Redirect safety, gzip, and log redaction."""

from __future__ import annotations

import gzip
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tibo_reset_monitor.core.http import CrossHostRedirect, TransportError, UrllibTransport, _SameHostRedirectHandler, redact_url
from tibo_reset_monitor.logutil import redact


class _Handler(BaseHTTPRequestHandler):
    port = 0

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/away":
            self.send_response(302)
            self.send_header("Location", "https://example.com/blocked")
            self.end_headers()
            return
        if path == "/hop":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.port}/land")
            self.end_headers()
            return
        if path == "/gzip":
            payload = gzip.compress(b'{"ok":1}')
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = b"landed"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        return


class HttpSafetyTest(unittest.TestCase):
    def test_same_host_redirect_and_gzip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        _Handler.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        transport = UrllibTransport()
        port = _Handler.port
        landed = transport.send("GET", f"http://127.0.0.1:{port}/hop", {}, None, 5)
        self.assertEqual(landed.status, 200)
        self.assertEqual(landed.body, b"landed")
        packed = transport.send("GET", f"http://127.0.0.1:{port}/gzip", {"Accept-Encoding": "gzip"}, None, 5)
        self.assertEqual(json.loads(packed.body.decode()), {"ok": 1})
        with self.assertRaises(CrossHostRedirect):
            transport.send("GET", f"http://127.0.0.1:{port}/away", {}, None, 5)

    def test_refuses_https_downgrade(self):
        handler = _SameHostRedirectHandler()
        request = urllib.request.Request("https://aihot.news/api/v1/codex-resets/recent")
        with self.assertRaises(CrossHostRedirect):
            handler.redirect_request(request, None, 302, "Found", {}, "http://aihot.news/other")

    def test_redact_urls_and_log_lines(self):
        token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEF"
        url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id=1"
        cleaned = redact_url(url)
        self.assertNotIn(token, cleaned)
        self.assertIn("/bot<redacted>/", cleaned)
        self.assertNotIn("chat_id", cleaned)
        line = redact(f"Authorization: Bearer t-secret app_secret={token} {url}")
        self.assertNotIn(token, line)
        self.assertNotIn("t-secret", line)
        self.assertNotIn("chat_id", line)
        self.assertIn("Bearer <redacted>", line)
        self.assertIn("app_secret=<redacted>", line)

    def test_transport_error_hides_bot_token(self):
        token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEF"
        transport = UrllibTransport()
        with self.assertRaises(TransportError) as raised:
            transport.send("GET", f"http://127.0.0.1:1/bot{token}/getMe", {}, None, 2)
        self.assertNotIn(token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
