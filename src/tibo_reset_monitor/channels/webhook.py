"""Generic JSON webhook. The URL comes from an environment variable."""

from __future__ import annotations

import json
import os
from typing import Any

from tibo_reset_monitor.channels.base import receipt_from_transport
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.model import Notice, Receipt, Target
from tibo_reset_monitor.core.source import parse_retry_after


class WebhookChannel:
    name = "webhook"

    def __init__(self, spec: ChannelSpec, transport: Any, clock: Any) -> None:
        del clock
        self.targets = list(spec.targets)
        self._transport = transport
        self._timeout = 20.0

    def render(self, notice: Notice, target: Target) -> dict[str, Any]:
        return {
            "target": {"name": target.key, "kind": target.kind},
            "notice": notice.to_json(),
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
        try:
            response = self._transport.send(
                "POST",
                url,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Idempotency-Key": idempotency_key,
                },
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
