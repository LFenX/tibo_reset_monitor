"""Telegram Bot API notifications. Group and personal chats both use sendMessage."""

from __future__ import annotations

import json
import os
from typing import Any

from tibo_reset_monitor.channels.base import receipt_from_transport
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.model import Notice, Receipt, Target, notice_lines

_HTML_LIMIT = 3900


class TelegramChannel:
    name = "telegram"

    def __init__(self, spec: ChannelSpec, transport: Any, clock: Any) -> None:
        del clock
        self.targets = list(spec.targets)
        self._transport = transport
        self._api_base = spec.settings.get("api_base", "https://api.telegram.org").rstrip("/")
        self._token_env = spec.settings.get("bot_token_env", "TIBO_TELEGRAM_BOT_TOKEN")
        self._timeout = 20.0

    def render(self, notice: Notice, target: Target) -> dict[str, Any]:
        preview_url = notice.url if notice.url.startswith("https://") or notice.url.startswith("http://") else ""
        payload: dict[str, Any] = {
            "chat_id": target.address,
            "text": render_html(notice),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not bool(preview_url), "url": preview_url} if preview_url else {"is_disabled": True},
        }
        return payload

    def send(self, message: dict[str, Any], target: Target, idempotency_key: str) -> Receipt:
        del target, idempotency_key  # Telegram has no uuid; the outbox row is the idempotency record.
        token = os.environ.get(self._token_env, "").strip()
        if not token:
            return Receipt("retryable", detail=f"{self._token_env} is not set")
        url = f"{self._api_base}/bot{token}/sendMessage"
        try:
            response = self._transport.send(
                "POST",
                url,
                {"Content-Type": "application/json; charset=utf-8"},
                json.dumps(message, ensure_ascii=False).encode("utf-8"),
                self._timeout,
            )
        except TransportError as exc:
            return receipt_from_transport(exc, sent=True)
        return _receipt(response.status, response.body, response.headers)


def render_html(notice: Notice) -> str:
    text = _clip(notice.text, 1500)
    original = _clip(notice.original_text, 1000)
    lines = [f"<b>{escape_html(notice.headline())}</b>"]
    subtitle = f"@{notice.author}" if notice.author else "@thsottiaux"
    if notice.published_at:
        subtitle += f" · {notice.published_at}"
    lines.append(escape_html(subtitle))
    lines.append("")
    body_notice = Notice(**{**notice.__dict__, "text": text, "original_text": original})
    for line in notice_lines(body_notice):
        lines.append(escape_html(line))
    if notice.url:
        lines.append(f'<a href="{escape_attr(notice.url)}">查看原帖</a>')
    rendered = "\n".join(lines)
    if len(rendered) > _HTML_LIMIT:
        rendered = rendered[: _HTML_LIMIT - 1] + "…"
    return rendered


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    return escape_html(value).replace('"', "&quot;")


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _receipt(status: int, body: bytes, headers: dict[str, str]) -> Receipt:
    payload = _json_object(body)
    if payload.get("ok") is True:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        message_id = result.get("message_id")
        return Receipt("sent", provider_message_id=str(message_id) if message_id is not None else None, detail="sent")
    error_code = payload.get("error_code")
    if not isinstance(error_code, int):
        error_code = status
    description = str(payload.get("description") or "").strip()
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    retry_after = parameters.get("retry_after")
    retry_seconds = None
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        retry_seconds = float(retry_after)
    elif headers.get("retry-after"):
        try:
            retry_seconds = float(headers["retry-after"])
        except ValueError:
            retry_seconds = None
    if error_code == 429 or status == 429:
        return Receipt(
            "retryable",
            detail=f"Telegram 429: too many requests{(': ' + description) if description else ''}",
            retry_after_seconds=retry_seconds if retry_seconds is not None else 30,
        )
    if error_code == 403:
        return Receipt(
            "permanent",
            detail="Telegram 403: the bot was blocked by the user, or it cannot post in this chat. "
            "A personal target has to press Start before the bot can write to them.",
        )
    if error_code == 400 and "chat not found" in description.lower():
        return Receipt(
            "permanent",
            detail="Telegram 400: chat not found. Personal targets use the positive numeric chat id "
            "from getUpdates after the user presses Start. Group targets use the negative chat id "
            "or a public @channelusername.",
        )
    if error_code == 400 or status == 400:
        return Receipt("permanent", detail=f"Telegram 400: {description or 'bad request'}")
    if error_code == 401 or status == 401:
        return Receipt("retryable", detail="Telegram 401: the bot token was rejected")
    if error_code >= 500 or status >= 500:
        return Receipt("retryable", detail=f"Telegram {error_code}: {description or 'server error'}")
    if description:
        return Receipt("retryable", detail=f"Telegram {error_code}: {description}")
    return Receipt("retryable", detail=f"Telegram HTTP {status}")


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
