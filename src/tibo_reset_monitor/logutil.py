"""Logging helpers that keep tokens out of log lines."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from tibo_reset_monitor.core.http import redact_url

_URL = re.compile(r"https?://[^\s<>\"]+")
_BOT_TOKEN = re.compile(r"/bot[^/\s]+")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_AUTHORIZATION = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?!Bearer\b)\S+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(app_secret|bot_token|access_token|tenant_access_token)"
    r"(\s*[:=]\s*)([\"']?)[^\"'\s,}&]+"
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def redact(text: str) -> str:
    cleaned = _URL.sub(lambda match: redact_url(match.group(0)), text)
    cleaned = _BEARER.sub("Bearer <redacted>", cleaned)
    cleaned = _AUTHORIZATION.sub(r"\1<redacted>", cleaned)
    cleaned = _TELEGRAM_TOKEN.sub("<redacted-token>", cleaned)
    cleaned = _BOT_TOKEN.sub("/bot<redacted>", cleaned)
    cleaned = _ASSIGNED_SECRET.sub(r"\1\2\3<redacted>", cleaned)
    return cleaned


class RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str, log_file: str | None = None) -> None:
    formatter = RedactFormatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("tibo_reset_monitor")
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    root.propagate = False
