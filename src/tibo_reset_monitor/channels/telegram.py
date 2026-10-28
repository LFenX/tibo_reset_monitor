"""Telegram Bot API notifications.

One notice is one message. When a calendar PNG is available it is sent with
``sendPhoto`` and an HTML caption. Otherwise the same caption is a single
``sendMessage``. There is no second message for the post link, and no avatar:
a photo message can carry only the calendar image.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Callable

from tibo_reset_monitor.channels.base import receipt_from_transport
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.i18n import OutboundMessage, compose, normalize_language
from tibo_reset_monitor.core.model import MANUAL, Notice, Receipt, Target

log = logging.getLogger("tibo_reset_monitor")

_PHOTO_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096
_BODY_CAP = 6000
_TAG_RE = re.compile(r"<[^>]+>")


class TelegramChannel:
    name = "telegram"

    def __init__(self, spec: ChannelSpec, transport: Any, clock: Any) -> None:
        del clock
        self.targets = list(spec.targets)
        self._transport = transport
        self._api_base = spec.settings.get("api_base", "https://api.telegram.org").rstrip("/")
        self._token_env = spec.settings.get("bot_token_env", "TIBO_TELEGRAM_BOT_TOKEN")
        self._language = normalize_language(spec.settings.get("language"))
        self._timeout = 20.0
        self._calendar_png: Callable[[], bytes | None] | None = None
        self._calendar_month: Callable[[], tuple[int, int]] | None = None

    def bind_calendar(
        self,
        provider: Callable[[], bytes | None],
        month: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._calendar_png = provider
        self._calendar_month = month

    def render(self, notice: Notice, target: Target) -> dict[str, Any]:
        language = _target_language(target, self._language)
        photo = self._load_photo(language)
        year_month = self._calendar_month() if self._calendar_month else None
        return {
            "chat_id": target.address,
            "photo": photo,
            "caption": build_caption(notice, _PHOTO_CAPTION_LIMIT, year_month, language),
            "text": build_caption(notice, _TEXT_LIMIT, year_month, language),
            "parse_mode": "HTML",
        }

    def send(self, message: dict[str, Any], target: Target, idempotency_key: str) -> Receipt:
        del target, idempotency_key  # Telegram has no uuid; the outbox row is the idempotency record.
        token = os.environ.get(self._token_env, "").strip()
        if not token:
            return Receipt("retryable", detail=f"{self._token_env} is not set")
        photo = message.get("photo")
        if isinstance(photo, (bytes, bytearray)) and photo:
            body, content_type = _multipart(
                [
                    ("chat_id", str(message["chat_id"])),
                    ("caption", message["caption"]),
                    ("parse_mode", "HTML"),
                    ("show_caption_above_media", "true"),
                ],
                bytes(photo),
            )
            url = f"{self._api_base}/bot{token}/sendPhoto"
            headers = {"Content-Type": content_type}
        else:
            payload: dict[str, Any] = {
                "chat_id": message["chat_id"],
                "text": message["text"],
                "parse_mode": "HTML",
            }
            link = _href_from_html(message.get("text") or "")
            if link:
                payload["link_preview_options"] = {"is_disabled": False, "url": link}
            else:
                payload["link_preview_options"] = {"is_disabled": True}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            url = f"{self._api_base}/bot{token}/sendMessage"
            headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            response = self._transport.send("POST", url, headers, body, self._timeout)
        except TransportError as exc:
            return receipt_from_transport(exc, sent=True)
        return _receipt(response.status, response.body, response.headers)

    def _load_photo(self, language: str) -> bytes | None:
        if self._calendar_png is None:
            return None
        try:
            photo = _call_calendar(self._calendar_png, language)
        except Exception as exc:
            log.warning("calendar image unavailable: %s", exc)
            return None
        if not photo:
            log.warning("calendar image unavailable; sending a text message")
            return None
        return photo


def _target_language(target: Target, default: str) -> str:
    return normalize_language(target.options.get("language"), default)


def _call_calendar(provider: Callable[..., bytes | None], language: str) -> bytes | None:
    """Pass the language when the provider accepts it. Older callables take no argument."""
    try:
        return provider(language)
    except TypeError as exc:
        if "argument" not in str(exc):
            raise
        return provider()


def build_caption(
    notice: Notice,
    limit: int,
    year_month: tuple[int, int] | None = None,
    language: str = "zh",
) -> str:
    """HTML caption whose parsed text length is at most ``limit`` UTF-16 code units.

    When the caption is too long, the main body and the original-post preview are
    shortened first. Tags, times, and the post link stay. English messages use the
    post's original text as the body and shorten the one-line quote the same way.
    """
    language = "en" if language == "en" else "zh"
    if language == "en" and notice.stage != MANUAL:
        main_src = _clip(notice.original_text, _BODY_CAP)
        preview_src = ""
        contexts = notice.contexts[:1]
        quote_srcs = tuple(_clip(item.get("original_text") or "", 2000) for item in contexts)
    else:
        main_src = _clip(notice.text, _BODY_CAP)
        preview_src = _clip(notice.original_text, _BODY_CAP)
        contexts = notice.contexts
        quote_srcs = tuple(_clip(_context_body(item), 2000) for item in contexts)

    def render(main_n: int, preview_n: int, quote_ns: tuple[int, ...]) -> str:
        message = compose(
            notice,
            language,
            year_month,
            body=_cut(main_src, main_n),
            preview=_cut(preview_src, preview_n),
            quote_bodies=tuple(_cut(source, size) for source, size in zip(quote_srcs, quote_ns)),
        )
        return _html(message)

    full_quotes = tuple(len(source) for source in quote_srcs)
    if parsed_length(render(len(main_src), len(preview_src), full_quotes)) <= limit:
        return render(len(main_src), len(preview_src), full_quotes)
    main_n = _largest_fit(lambda size: render(size, len(preview_src), full_quotes), len(main_src), limit)
    if parsed_length(render(main_n, len(preview_src), full_quotes)) <= limit:
        return render(main_n, len(preview_src), full_quotes)
    preview_n = _largest_fit(lambda size: render(main_n, size, full_quotes), len(preview_src), limit)
    quote_ns = full_quotes
    while parsed_length(render(main_n, preview_n, quote_ns)) > limit and any(quote_ns):
        longest = max(range(len(quote_ns)), key=lambda index: quote_ns[index])
        shrunk = max(0, int(quote_ns[longest] * 0.6) - 1)
        if shrunk == quote_ns[longest]:
            shrunk = 0
        quote_ns = tuple(shrunk if index == longest else size for index, size in enumerate(quote_ns))
    candidate = render(main_n, preview_n, quote_ns)
    if parsed_length(candidate) <= limit:
        return candidate
    return _hard_trim(render(0, 0, tuple(0 for _ in quote_srcs)), limit)


def _largest_fit(render, hi: int, limit: int) -> int:
    if parsed_length(render(0)) > limit:
        return 0
    lo = 0
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if parsed_length(render(mid)) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _cut(source: str, size: int) -> str:
    if size >= len(source):
        return source
    if size <= 1:
        return ""
    return source[: size - 1].rstrip() + "…"


def _clip(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def parsed_length(html: str) -> int:
    """Length Telegram counts after HTML entities are parsed, in UTF-16 code units."""
    text = _TAG_RE.sub("", html)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    return len(text.encode("utf-16-le")) // 2


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    return escape_html(value).replace('"', "&quot;")


def _context_body(item: dict[str, str]) -> str:
    return item.get("original_text") or item.get("text") or ""


def _html(message: OutboundMessage) -> str:
    if message.stage == MANUAL:
        lines = [
            f"<b>{escape_html(message.manual_heading)}</b>",
            f"<i>{escape_html(message.manual_note)}</i>",
            "",
        ]
        if message.text:
            lines.append(escape_html(message.text))
        lines.append("")
        lines.append(escape_html(message.calendar_line))
        return "\n".join(lines).strip()
    lines = [
        f"<b>{escape_html(message.author_name)}</b>",
        f"<i>{escape_html(message.author_caption)}</i>",
        "",
    ]
    if len(message.tags) >= 2:
        tag, stage = message.tags[0], message.tags[1]
        lines.append(f"<code>{escape_html(tag)}</code> <code>{escape_html(stage)}</code>")
    elif message.tags:
        lines.append(f"<code>{escape_html(message.tags[0])}</code>")
    lines.append("")
    if message.text:
        lines.append(escape_html(message.text))
        lines.append("")
    for detail in message.detail_lines:
        lines.append(f"<i>{escape_html(detail)}</i>")
    if message.preview_lines:
        inner = "\n".join(escape_html(line) for line in message.preview_lines)
        lines.append("")
        lines.append(f"<blockquote>{inner}</blockquote>")
    if message.quote_line:
        lines.append("")
        lines.append(escape_html(message.quote_line))
    if message.link_url and message.stage != MANUAL:
        lines.append(f'<a href="{escape_attr(message.link_url)}">{escape_html(message.link_label)}</a>')
    lines.append("")
    lines.append(escape_html(message.calendar_line))
    return "\n".join(lines).strip()


def _href_from_html(html: str) -> str:
    match = re.search(r'href="([^"]+)"', html)
    if not match:
        return ""
    return match.group(1).replace("&amp;", "&").replace("&quot;", '"')


def _hard_trim(html: str, limit: int) -> str:
    """Last resort when the shell itself is over the limit."""
    text = _TAG_RE.sub("", html)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    trimmed = text
    while trimmed and len(trimmed.encode("utf-16-le")) // 2 > limit - 1:
        trimmed = trimmed[:-1]
    return escape_html(trimmed + "…") if trimmed else "…"


def _multipart(fields: list[tuple[str, str]], photo: bytes) -> tuple[bytes, str]:
    boundary = "tibo" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="photo"; filename="calendar.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        + photo
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


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

