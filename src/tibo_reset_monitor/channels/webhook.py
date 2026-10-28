"""Generic JSON webhook. The URL comes from an environment variable.

Signing is optional. A target that sets ``secret_env`` signs the exact request
body with HMAC-SHA256. A target that omits it sends the same request as before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import timezone
from typing import Any

from tibo_reset_monitor.channels.base import receipt_from_transport
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.i18n import compose, normalize_language
from tibo_reset_monitor.core.model import Notice, Receipt, Target
from tibo_reset_monitor.core.source import parse_retry_after


class WebhookChannel:
    name = "webhook"

    def __init__(self, spec: ChannelSpec, transport: Any, clock: Any) -> None:
        self.targets = list(spec.targets)
        self._transport = transport
        self._clock = clock
        self._language = normalize_language(spec.settings.get("language"))
        self._timezone = spec.settings.get("timezone") or "UTC"
        self._timeout = 20.0

    def render(self, notice: Notice, target: Target) -> dict[str, Any]:
        language = normalize_language(target.options.get("language"), self._language)
        zone_name = (target.options.get("timezone") or self._timezone or "UTC").strip() or "UTC"
        message = compose(notice, language, timezone_name=zone_name)
        text = message.text
        if message.quote_line:
            text = f"{text}\n\n{message.quote_line}" if text else message.quote_line
        return {
            "language": message.language,
            "target": {"name": target.key, "kind": target.kind},
            "notice": notice.to_json(),
            "title": message.title,
            "tags": list(message.tags),
            "scope": message.scope,
            "text": text,
            "posted": message.posted_line,
        }

    def send(self, message: dict[str, Any], target: Target, idempotency_key: str) -> Receipt:
        env_name = target.options.get("url_env", "")
        url = os.environ.get(env_name, "").strip() if env_name else ""
        if not url:
            return Receipt("retryable", detail=f"{env_name or 'webhook url'} is not set")
        if not url.startswith("https://"):
            return Receipt("permanent", detail=f"{env_name} must be an https URL")
        payload = dict(message)
        payload["idempotency_key"] = idempotency_key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": idempotency_key,
        }
        secret_env = (target.options.get("secret_env") or "").strip()
        if secret_env:
            secret = os.environ.get(secret_env, "").strip()
            if not secret:
                return Receipt("retryable", detail=f"{secret_env} is not set")
            timestamp = str(self._unix_seconds())
            headers["X-Tibo-Timestamp"] = timestamp
            headers["X-Tibo-Signature"] = sign_body(secret, timestamp, body)
        try:
            response = self._transport.send(
                "POST",
                url,
                headers,
                body,
                self._timeout,
            )
        except TransportError as exc:
            return receipt_from_transport(exc, sent=True)
        if 200 <= response.status < 300:
            body = _json_object(response.body)
            message_id = body.get("id") if isinstance(body, dict) else None
            return Receipt("sent", provider_message_id=str(message_id) if message_id else "ok", detail="sent")
        if response.status == 429:
            return Receipt(
                "retryable",
                detail=f"webhook 429 from {env_name}",
                retry_after_seconds=parse_retry_after(response.headers.get("retry-after")) or 30,
            )
        if 400 <= response.status < 500:
            return Receipt("permanent", detail=f"webhook HTTP {response.status} from {env_name}")
        return Receipt("retryable", detail=f"webhook HTTP {response.status} from {env_name}")

    def _unix_seconds(self) -> int:
        if self._clock is not None:
            now = self._clock.now()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            return int(now.timestamp())
        return int(time.time())


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 of ``<timestamp>.<raw body bytes>``, labeled ``v1=<hex>``."""
    signed = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
