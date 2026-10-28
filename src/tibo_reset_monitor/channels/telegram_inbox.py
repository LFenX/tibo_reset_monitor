"""Telegram getUpdates listener. Stdlib HTTP only.

``getUpdates`` cannot run while a webhook is set. A 409 is logged once and the
listener stops. ``setMyCommands`` failing does not stop notifications.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from tibo_reset_monitor.core.chat import Inbound, Reply
from tibo_reset_monitor.logutil import redact

log = logging.getLogger("tibo_reset_monitor")

_JOINED = {"member", "administrator", "creator"}
_LEFT = {"left", "kicked"}


def parse_update(update: dict[str, Any]) -> Inbound | None:
    """Keep only the fields chat control needs. The raw update is not stored."""
    update_id = update.get("update_id")
    event_id = "" if update_id is None else str(update_id)
    member = update.get("my_chat_member")
    if isinstance(member, dict):
        chat = member.get("chat") if isinstance(member.get("chat"), dict) else {}
        new_member = member.get("new_chat_member") if isinstance(member.get("new_chat_member"), dict) else {}
        status = str(new_member.get("status") or "")
        if status in _JOINED:
            membership = "joined"
        elif status in _LEFT:
            membership = "left"
        else:
            membership = ""
        return Inbound(
            channel="telegram",
            event_id=event_id,
            scope="group",
            chat_id=str(chat.get("id") or ""),
            user_id="",
            chat_title=str(chat.get("title") or ""),
            membership=membership,
        )
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_type = str(chat.get("type") or "")
    scope = "personal" if chat_type == "private" else "group"
    migrate = message.get("migrate_to_chat_id")
    text = message.get("text") if isinstance(message.get("text"), str) else ""
    return Inbound(
        channel="telegram",
        event_id=event_id,
        scope=scope,
        chat_id=str(chat.get("id") or ""),
        user_id=str(sender.get("id") or ""),
        text=text,
        chat_title=str(chat.get("title") or ""),
        migrate_to=str(migrate) if migrate is not None else "",
    )


class TelegramInbox:
    def __init__(self, channel: Any, controller: Any, transport: Any) -> None:
        self.channel = channel
        self.controller = controller
        self.transport = transport
        self._webhook_conflict = False

    def poll_once(self) -> None:
        if self._webhook_conflict:
            return
        token = os.environ.get(self.channel._token_env, "").strip()
        if not token:
            return
        offset = self.channel_offset()
        payload: dict[str, Any] = {"timeout": 20, "allowed_updates": ["message", "my_chat_member"]}
        if offset:
            payload["offset"] = int(offset)
        response = self.transport.send(
            "POST",
            f"{self.channel._api_base}/bot{token}/getUpdates",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(payload).encode("utf-8"),
            max(self.channel._timeout, 35),
        )
        body = _object(response.body)
        if body.get("ok") is not True:
            description = str(body.get("description") or "")
            if response.status == 409 or body.get("error_code") == 409 or "webhook" in description.lower():
                if not self._webhook_conflict:
                    log.error(
                        "Telegram getUpdates cannot run while a webhook is set. "
                        "Delete the webhook (deleteWebhook) to use chat control, or turn chat_control off."
                    )
                self._webhook_conflict = True
            return
        latest = None
        for update in body.get("result") or []:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                latest = update_id
            inbound = parse_update(update)
            if inbound is None or not inbound.chat_id and not inbound.migrate_to:
                continue
            try:
                reply = self.controller.handle(inbound)
            except Exception as exc:
                log.warning("telegram update could not be handled: %s", redact(str(exc)).replace("\n", " ")[:300])
                continue
            if reply is not None:
                try:
                    self._reply(token, reply)
                except Exception as exc:
                    log.warning("telegram reply failed: %s", redact(str(exc)).replace("\n", " ")[:300])
        if latest is not None:
            self.channel_store_offset(latest + 1)

    def register_commands(self) -> None:
        token = os.environ.get(self.channel._token_env, "").strip()
        if not token:
            return
        commands = [
            {"command": "start", "description": "Help"},
            {"command": "help", "description": "Help"},
            {"command": "status", "description": "List subscriptions"},
            {"command": "subscribe", "description": "Turn notifications on"},
            {"command": "unsubscribe", "description": "Turn notifications off"},
            {"command": "addgroup", "description": "Subscribe a group"},
            {"command": "removegroup", "description": "Unsubscribe a group"},
            {"command": "cancel", "description": "Cancel the current choice"},
            {"command": "pair", "description": "Become the first admin"},
        ]
        try:
            response = self.transport.send(
                "POST",
                f"{self.channel._api_base}/bot{token}/setMyCommands",
                {"Content-Type": "application/json; charset=utf-8"},
                json.dumps({"commands": commands}).encode("utf-8"),
                self.channel._timeout,
            )
        except Exception as exc:
            log.warning("Telegram setMyCommands failed: %s", type(exc).__name__)
            return
        body = _object(response.body)
        if body.get("ok") is not True:
            log.warning("Telegram setMyCommands was not accepted")

    def _reply(self, token: str, reply: Reply) -> None:
        self.transport.send(
            "POST",
            f"{self.channel._api_base}/bot{token}/sendMessage",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"chat_id": reply.chat_id, "text": reply.text}, ensure_ascii=False).encode("utf-8"),
            self.channel._timeout,
        )

    def channel_offset(self) -> str | None:
        store = getattr(self.controller, "store", None)
        if store is None:
            return None
        return store.get_meta("telegram_update_offset")

    def channel_store_offset(self, offset: int) -> None:
        store = getattr(self.controller, "store", None)
        if store is None:
            return
        store.set_meta("telegram_update_offset", str(offset))
        store.conn.commit()


def _object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
