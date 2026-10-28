"""Feishu / Lark interactive cards via the Open API. No lark-cli dependency."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from tibo_reset_monitor.channels.base import receipt_from_transport
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.i18n import compose, normalize_language
from tibo_reset_monitor.core.model import Notice, Receipt, Target
from tibo_reset_monitor.core.source import parse_retry_after

log = logging.getLogger("tibo_reset_monitor")

_TEMPLATES = {
    "announcement": "blue",
    "confirmation": "green",
    "revision": "orange",
    "retraction": "red",
    "manual": "wathet",
}
# Client errors that will not succeed on retry. Token problems are handled separately.
_PERMANENT_CODES = {230001, 230002, 230006, 230013, 99991672}
_TOKEN_CODES = {99991661, 99991663, 99991668}


class FeishuChannel:
    name = "feishu"
    supports_chat = True

    def __init__(self, spec: ChannelSpec, transport: Any, clock: Any) -> None:
        self.targets = list(spec.targets)
        self.settings = spec.settings
        self.supports_chat = True
        self._transport = transport
        self._clock = clock
        self._api_base = spec.settings.get("api_base", "https://open.feishu.cn").rstrip("/")
        self._app_id = spec.settings.get("app_id", "")
        self._app_id_env = spec.settings.get("app_id_env", "")
        self._secret_env = spec.settings.get("app_secret_env", "TIBO_FEISHU_APP_SECRET")
        self._language = normalize_language(spec.settings.get("language"))
        self._timezone = spec.settings.get("timezone") or "UTC"
        self._timeout = 20.0
        self._token: str | None = None
        self._token_until = None
        self._calendar_png: Callable[..., bytes | None] | None = None
        self._calendar_month: Callable[[], tuple[int, int]] | None = None

    def bind_calendar(
        self,
        provider: Callable[..., bytes | None],
        month: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._calendar_png = provider
        self._calendar_month = month

    def render(self, notice: Notice, target: Target) -> dict[str, Any]:
        language = normalize_language(target.options.get("language"), self._language)
        zone_name = (target.options.get("timezone") or self._timezone or "UTC").strip() or "UTC"
        year_month = _call_month(self._calendar_month, zone_name)
        message = compose(notice, language, year_month, timezone_name=zone_name)
        card = _card(message)
        return {
            "receive_id": target.address,
            "receive_id_type": target.options.get("receive_id_type", "chat_id"),
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            "photo": self._load_photo(language, zone_name),
            "photo_alt": message.calendar_line,
        }

    def send(self, message: dict[str, Any], target: Target, idempotency_key: str) -> Receipt:
        del target
        content = message.get("content")
        photo = message.get("photo")
        if isinstance(photo, (bytes, bytearray)) and photo and isinstance(content, str):
            image_key = self._upload_image(bytes(photo))
            if image_key:
                content = _with_image(content, image_key, str(message.get("photo_alt") or ""))
                message = dict(message)
                message["content"] = content
        return self._post_message(message, idempotency_key, refreshed=False)

    def _post_message(self, message: dict[str, Any], idempotency_key: str, *, refreshed: bool) -> Receipt:
        token = self._access_token(force=refreshed)
        if isinstance(token, Receipt):
            return token
        query = urlencode({"receive_id_type": message["receive_id_type"]})
        url = f"{self._api_base}/open-apis/im/v1/messages?{query}"
        body = {
            "receive_id": message["receive_id"],
            "msg_type": message["msg_type"],
            "content": message["content"],
            "uuid": idempotency_key,
        }
        try:
            response = self._transport.send(
                "POST",
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
                self._timeout,
            )
        except TransportError as exc:
            return receipt_from_transport(exc, sent=True)
        payload = _json_object(response.body)
        code = payload.get("code") if payload else None
        msg = str(payload.get("msg") or "") if payload else ""
        if response.status == 429 or code == 99991400:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            return Receipt("retryable", detail=f"Feishu rate limited: {msg or response.status}", retry_after_seconds=retry_after)
        if code in _TOKEN_CODES and not refreshed:
            self._token = None
            self._token_until = None
            return self._post_message(message, idempotency_key, refreshed=True)
        if response.status == 200 and code == 0:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            message_id = data.get("message_id")
            return Receipt("sent", provider_message_id=str(message_id) if message_id else None, detail="sent")
        detail = _feishu_detail(response.status, code, msg)
        if response.status in (400, 401, 403, 404) or code in _PERMANENT_CODES or _looks_permanent(msg):
            return Receipt("permanent", detail=detail)
        if response.status >= 500 or (isinstance(code, int) and code != 0):
            return Receipt("retryable", detail=detail)
        return Receipt("retryable", detail=detail or f"Feishu HTTP {response.status}")

    def _access_token(self, *, force: bool):
        now = self._clock.now()
        if not force and self._token and self._token_until and now < self._token_until:
            return self._token
        app_id = os.environ.get(self._app_id_env, "").strip() if self._app_id_env else ""
        if not app_id:
            app_id = self._app_id
        secret = os.environ.get(self._secret_env, "").strip()
        if not app_id:
            return Receipt("retryable", detail="Feishu app_id is empty")
        if not secret:
            return Receipt("retryable", detail=f"{self._secret_env} is not set")
        try:
            response = self._transport.send(
                "POST",
                f"{self._api_base}/open-apis/auth/v3/tenant_access_token/internal",
                {"Content-Type": "application/json; charset=utf-8"},
                json.dumps({"app_id": app_id, "app_secret": secret}).encode("utf-8"),
                self._timeout,
            )
        except TransportError as exc:
            # The message was not submitted yet, so this is safe to retry.
            return Receipt("retryable", detail=f"Feishu token request failed: {exc}")
        payload = _json_object(response.body)
        token = payload.get("tenant_access_token") if payload else None
        if response.status != 200 or not isinstance(token, str) or not token:
            code = payload.get("code") if payload else None
            msg = payload.get("msg") if payload else ""
            return Receipt("retryable", detail=f"Feishu token request failed ({code or response.status}): {msg}")
        expire = payload.get("expire") if isinstance(payload.get("expire"), int) else 7200
        skew = 300 if expire > 600 else 30
        self._token = token
        self._token_until = now + timedelta(seconds=max(30, expire - skew))
        return token

    def _load_photo(self, language: str, timezone_name: str) -> bytes | None:
        if self._calendar_png is None:
            return None
        try:
            try:
                photo = self._calendar_png(language, timezone_name)
            except TypeError as exc:
                if "argument" not in str(exc):
                    raise
                try:
                    photo = self._calendar_png(language)
                except TypeError as inner:
                    if "argument" not in str(inner):
                        raise
                    photo = self._calendar_png()
        except Exception as exc:
            log.warning("calendar image unavailable: %s", exc)
            return None
        return photo or None

    def _upload_image(self, photo: bytes) -> str | None:
        token = self._access_token(force=False)
        if isinstance(token, Receipt):
            log.warning("calendar image skipped: %s", token.detail)
            return None
        body, content_type = _image_form(photo)
        try:
            response = self._transport.send(
                "POST",
                f"{self._api_base}/open-apis/im/v1/images",
                {"Authorization": f"Bearer {token}", "Content-Type": content_type},
                body,
                self._timeout,
            )
        except TransportError as exc:
            log.warning("calendar image upload failed: %s", exc)
            return None
        payload = _json_object(response.body)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        image_key = data.get("image_key") if payload.get("code") == 0 else None
        if response.status == 200 and isinstance(image_key, str) and image_key:
            return image_key
        log.warning("calendar image upload failed: %s", payload.get("msg") or response.status)
        return None


def _call_month(provider: Callable[..., tuple[int, int]] | None, timezone_name: str) -> tuple[int, int] | None:
    if provider is None:
        return None
    try:
        return provider(timezone_name)
    except TypeError as exc:
        if "argument" not in str(exc):
            raise
        return provider()


def _card(message: Any) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": message.card_body, "text_align": "left", "text_size": "normal"}
    ]
    if message.link_url:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": message.link_label},
                "type": "primary",
                "width": "default",
                "behaviors": [{"type": "open_url", "default_url": message.link_url}],
            }
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": message.title[:200]},
            "subtitle": {"tag": "plain_text", "content": message.subtitle[:200]},
            "template": _TEMPLATES.get(message.stage, "blue"),
        },
        "body": {"direction": "vertical", "elements": elements},
    }


def _with_image(content: str, image_key: str, alt: str) -> str:
    card = json.loads(content)
    card["body"]["elements"].append(
        {
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": alt[:200] or "calendar"},
            "mode": "fit_horizontal",
            "preview": True,
        }
    )
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def _image_form(photo: bytes) -> tuple[bytes, str]:
    boundary = "tibo" + uuid.uuid4().hex
    chunks = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image_type"\r\n\r\n'
            "message\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="image"; filename="calendar.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        + photo
        + b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _feishu_detail(status: int, code: Any, msg: str) -> str:
    label = f"Feishu {code}" if code not in (None, 0) else f"Feishu HTTP {status}"
    text = msg.strip() or "request failed"
    if code in _PERMANENT_CODES or _looks_permanent(text):
        return (
            f"{label}: {text}. Check that the bot is in the group (chat_id) or that the "
            "personal open_id / user_id / email is correct, and that im:message:send_as_bot is granted."
        )
    return f"{label}: {text}"


def _looks_permanent(msg: str) -> bool:
    lowered = msg.lower()
    markers = ("chat not found", "invalid receive_id", "bot not in", "not in the chat", "open_id not found")
    return any(marker in lowered for marker in markers)
