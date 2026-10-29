"""Optional Feishu long-connection listener.

The SDK is imported only when chat control starts. Without the ``feishu-bot``
extra, static targets still receive notifications and this module logs why
chat control is off.
"""

from __future__ import annotations

import json
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from tibo_reset_monitor.core.chat import Inbound
from tibo_reset_monitor.logutil import redact

log = logging.getLogger("tibo_reset_monitor")


def feishu_sdk_available() -> bool:
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        return False
    return True


def inbound_from_fields(
    *,
    message_id: str,
    chat_id: str,
    chat_type: str,
    open_id: str,
    text: str,
    create_time: str | None = None,
) -> Inbound:
    """Build an inbound event from the few fields we keep. Tokens are not accepted."""
    scope = "personal" if chat_type in {"p2p", "private"} else "group"
    return Inbound(
        channel="feishu",
        event_id=message_id,
        scope=scope,
        chat_id=chat_id,
        user_id=open_id,
        text=text,
        chat_title="",
        created_at=datetime.fromtimestamp(int(create_time) / 1000, timezone.utc) if create_time is not None else None,
    )


def text_from_content(content: str) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return ""


def list_bot_chats(channel: Any, *, page_limit: int = 10) -> list[tuple[str, str]]:
    """Chats the Feishu bot is already in. Pages until the API says to stop."""
    token = channel._access_token(force=False)
    from tibo_reset_monitor.core.model import Receipt

    if isinstance(token, Receipt):
        raise RuntimeError("feishu token unavailable")
    found: list[tuple[str, str]] = []
    page_token = ""
    for _ in range(page_limit):
        query = {"page_size": "50"}
        if page_token:
            query["page_token"] = page_token
        response = channel._transport.send(
            "GET",
            f"{channel._api_base}/open-apis/im/v1/chats?{urlencode(query)}",
            {"Authorization": f"Bearer {token}"},
            b"",
            channel._timeout,
        )
        payload = _object(response.body)
        if response.status != 200 or payload.get("code") not in (0, None):
            raise RuntimeError("feishu chat list failed")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("chat_id"):
                continue
            found.append((str(item["chat_id"]), str(item.get("name") or item["chat_id"])))
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return found[:100]


def collaborator_open_ids(channel: Any) -> set[str]:
    """Application collaborators, used when ``admins`` is empty."""
    token = channel._access_token(force=False)
    from tibo_reset_monitor.core.model import Receipt

    if isinstance(token, Receipt):
        return set()
    app_id = channel._app_id or "me"
    response = channel._transport.send(
        "GET",
        f"{channel._api_base}/open-apis/application/v6/applications/{app_id}/collaborators?user_id_type=open_id",
        {"Authorization": f"Bearer {token}"},
        b"",
        channel._timeout,
    )
    payload = _object(response.body)
    if response.status != 200 or type(payload.get("code")) is not int or payload["code"] != 0:
        log.warning("Feishu collaborator list is unavailable; set channels.feishu.admins to open_ids")
        return set()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    found: set[str] = set()
    for item in data.get("collaborators") or data.get("items") or []:
        if not isinstance(item, dict) or item.get("type") not in {"owner", "administrator", "developer", "operator"}:
            continue
        open_id = item.get("open_id") or item.get("user_id")
        if isinstance(open_id, str) and open_id.startswith("ou_"):
            found.add(open_id)
    return found


def user_in_chat(channel: Any, user_id: str, chat_id: str, *, page_limit: int = 100) -> bool:
    """Check user membership with the bot token; lookup failures fail closed."""
    from tibo_reset_monitor.core.model import Receipt
    token = channel._access_token(force=False)
    if isinstance(token, Receipt):
        raise RuntimeError("feishu membership token unavailable")
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(page_limit):
        query = {"member_id_type": "open_id", "page_size": "100"}
        if page_token:
            query["page_token"] = page_token
        response = channel._transport.send("GET", f"{channel._api_base}/open-apis/im/v1/chats/{quote(chat_id, safe='')}/members?{urlencode(query)}",
                                           {"Authorization": f"Bearer {token}"}, b"", channel._timeout)
        payload = _object(response.body)
        data = payload.get("data")
        if response.status != 200 or type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("feishu membership lookup failed")
        for item in data["items"]:
            if not isinstance(item, dict):
                raise RuntimeError("invalid feishu member")
            if item.get("member_id_type") == "open_id" and item.get("member_id") == user_id:
                return True
        if data.get("has_more") is False:
            return False
        page_token = data.get("page_token")
        if data.get("has_more") is not True or not isinstance(page_token, str) or not page_token or page_token in seen_tokens:
            raise RuntimeError("incomplete feishu member list")
        seen_tokens.add(page_token)
    raise RuntimeError("feishu membership page limit exceeded")


def start_feishu_listener(channel: Any, controller: Any) -> Any | None:
    """Start the long connection when lark-oapi is installed. Otherwise log and return."""
    try:
        import lark_oapi as lark
    except ImportError:
        log.warning(
            "Feishu chat control is off because lark-oapi is not installed. "
            "pip install 'tibo-reset-monitor[feishu-bot]' to talk to the bot. "
            "Static targets still receive notifications."
        )
        return None

    def on_message(data: Any) -> None:
        handle_feishu_event(channel, controller, data)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    app_id = channel._app_id
    secret = os_secret(channel)
    if not app_id or not secret:
        log.warning("Feishu chat control needs app_id and the app secret environment variable")
        return None
    client = lark.ws.Client(app_id, secret, event_handler=handler)
    log.info("Feishu chat control connected")
    return client


def handle_feishu_event(channel: Any, controller: Any, data: Any) -> None:
    """Handle one inbound event. A bad payload or a failed reply does not raise."""
    try:
        message = data.event.message
        sender = data.event.sender.sender_id
        if data.event.sender.sender_type != "user" or not str(sender.open_id or "").startswith("ou_"):
            return
        create_time = str(getattr(message, "create_time", "") or "")
        if not create_time.isdecimal() or int(create_time) <= 0 or not message.message_id:
            raise ValueError("message timestamp or identity is missing")
        inbound = inbound_from_fields(
            message_id=str(message.message_id or ""),
            chat_id=str(message.chat_id or ""),
            chat_type=str(message.chat_type or ""),
            open_id=str(sender.open_id or ""),
            text=text_from_content(str(message.content or "")),
            create_time=create_time,
        )
    except Exception:
        log.warning("Feishu message could not be read")
        return
    try:
        reply = controller.handle(inbound)
    except Exception as exc:
        log.warning("Feishu message could not be handled: %s", redact(str(exc)).replace("\n", " ")[:300])
        return
    if reply is None:
        return
    try:
        prepare = getattr(controller, "prepare_reply", lambda _reply: True)
        if not prepare(reply):
            return
        sent = (_send_text(channel, reply.chat_id, reply.text, idempotency_key=hashlib.sha256(
                    (reply.picker_user_id + "\x1f" + reply.picker_id + "\x1f" + reply.chat_id).encode()).hexdigest()[:43])
                if reply.picker_user_id else _send_text(channel, reply.chat_id, reply.text))
        if sent:
            confirm = getattr(controller, "confirm_reply", lambda _reply: None)
            confirm(reply)
    except Exception as exc:
        log.warning("Feishu reply failed: %s", redact(str(exc)).replace("\n", " ")[:300])


def os_secret(channel: Any) -> str:
    import os

    return os.environ.get(channel._secret_env, "").strip()


def _send_text(channel: Any, chat_id: str, text: str, *, idempotency_key: str | None = None) -> bool:
    from tibo_reset_monitor.core.model import Receipt

    token = channel._access_token(force=False)
    if isinstance(token, Receipt):
        log.warning("Feishu reply skipped: %s", token.detail)
        return False
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if idempotency_key:
        body["uuid"] = idempotency_key
    response = channel._transport.send(
        "POST",
        f"{channel._api_base}/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        channel._timeout,
    )
    payload = _object(response.body)
    data = payload.get("data")
    return (response.status == 200 and type(payload.get("code")) is int and payload["code"] == 0
            and isinstance(data, dict) and isinstance(data.get("message_id"), str) and bool(data["message_id"].strip())
            and data.get("chat_id") == chat_id)


def _object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
