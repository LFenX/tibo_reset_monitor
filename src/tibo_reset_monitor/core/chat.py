"""Channel-neutral chat control.

A channel turns an inbound event into :class:`Inbound` and sends the
:class:`Reply`. Parsing, permissions, the one-minute picker, and subscription
changes live here so every channel shares them.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from tibo_reset_monitor.core.i18n import chat_text, usage_guide
from tibo_reset_monitor.core.model import parse_utc
from tibo_reset_monitor.core.store import StateStore
from tibo_reset_monitor.logutil import redact

log = logging.getLogger("tibo_reset_monitor")

SESSION_SECONDS = 60
PAIR_SECONDS = 10 * 60
PAIR_ATTEMPTS = 5
COMMAND_LIMIT = 20
COMMAND_WINDOW = 60

# Telegram user ids are positive decimals. Feishu open_ids start with ou_.
_ADMIN_ID = re.compile(r"^(?:[1-9]\d{0,18}|ou_[A-Za-z0-9_]{1,120})$")

_COMMANDS = {
    "/start": "help",
    "/help": "help",
    "#帮助": "help",
    "#指南": "help",
    "#使用说明": "help",
    "/status": "status",
    "#状态": "status",
    "/subscribe": "subscribe",
    "#开启单聊通知": "subscribe_dm",
    "/unsubscribe": "unsubscribe",
    "#关闭单聊通知": "unsubscribe_dm",
    "/addgroup": "add_group",
    "#添加群聊": "add_group",
    "/removegroup": "remove_group",
    "#移除群聊": "remove_group",
    "#取消群聊": "remove_group",
    "#取消通知": "remove_group",
    "/cancel": "cancel",
    "#取消": "cancel",
    "/pair": "pair",
    "/addadmin": "add_admin",
}


@dataclass(frozen=True)
class Inbound:
    """Fields a channel is allowed to keep from an inbound event."""

    channel: str
    event_id: str
    scope: str
    chat_id: str
    user_id: str
    text: str = ""
    chat_title: str = ""
    migrate_to: str = ""
    membership: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class Reply:
    chat_id: str
    text: str
    picker_user_id: str = ""
    picker_id: str = ""
    result_id: str = ""
    result_user_id: str = ""
    result_group_id: str = ""


@dataclass
class PairingGate:
    """One in-memory code. It is never written to the state database."""

    code: str = ""
    expires_at: datetime | None = None
    used: bool = False
    failures: dict[str, tuple[int, datetime]] = field(default_factory=dict)

    def issue(self, now: datetime) -> str:
        self.code = secrets.token_hex(3)
        self.expires_at = now + timedelta(seconds=PAIR_SECONDS)
        self.used = False
        return self.code

    def too_many(self, user_id: str, now: datetime) -> bool:
        count, start = self.failures.get(user_id, (0, now))
        if (now - start).total_seconds() > PAIR_SECONDS:
            return False
        return count >= PAIR_ATTEMPTS

    def consume(self, user_id: str, offered: str, now: datetime) -> bool:
        if self.too_many(user_id, now):
            return False
        fresh = (
            not self.used
            and bool(self.code)
            and _codes_match(offered, self.code)
            and self.expires_at is not None
            and now <= self.expires_at
        )
        if fresh:
            self.used = True
            self.failures.pop(user_id, None)
            return True
        count, start = self.failures.get(user_id, (0, now))
        if (now - start).total_seconds() > PAIR_SECONDS:
            count, start = 0, now
        self.failures[user_id] = (count + 1, start)
        return False


class ChatController:
    def __init__(
        self,
        store: StateStore,
        clock: object,
        *,
        channel: str,
        language: str,
        admins: tuple[str, ...] = (),
        allow_self_subscribe: bool = False,
        group_catalog: Callable[[], list[tuple[str, str]] | None] | None = None,
        pairing: PairingGate | None = None,
        extra_admins: Callable[[], set[str]] | None = None,
        welcome: Callable[[str], None] | None = None,
        group_membership: Callable[[str, str], bool] | None = None,
        require_reply_ack: bool = False,
    ) -> None:
        self.store = store
        self.clock = clock
        self.channel = channel
        self.language = "en" if language == "en" else "zh"
        self.admins = tuple(admins)
        self.allow_self_subscribe = allow_self_subscribe
        self.group_catalog = group_catalog
        self.pairing = pairing or PairingGate()
        self.extra_admins = extra_admins
        self.welcome = welcome
        self.group_membership = group_membership
        self.require_reply_ack = require_reply_ack
        self._hits: dict[str, list[datetime]] = {}
        self._extra_loaded = False
        self._extra_ids: set[str] | None = None

    def handle(self, inbound: Inbound) -> Reply | None:
        if inbound.channel != self.channel:
            return None
        now = self.clock.now()
        command, argument = _split_command(inbound.text)
        selection = (command is None and inbound.scope == "personal" and bool(inbound.text.strip())
                     and not inbound.migrate_to and not inbound.membership
                     and self.store.load_session(self.channel, inbound.user_id) is not None)
        deferred_claim = selection or (inbound.scope == "personal" and command in {"add_group", "remove_group"})
        duplicate = False
        if inbound.event_id:
            duplicate = (self.store.has_inbound(self.channel, inbound.event_id) if deferred_claim else
                         not self.store.claim_inbound(self.channel, inbound.event_id, now))
        if duplicate:
            result = self._result_reply(inbound)
            if result is not None:
                return result
            session = self.store.load_session(self.channel, inbound.user_id)
            if (inbound.scope == "personal" and session and not session["expires_at"]
                    and session["prompt_id"] == inbound.event_id and session["chat_id"] == inbound.chat_id
                    and session["reply_text"]):
                return Reply(inbound.chat_id, session["reply_text"], inbound.user_id, inbound.event_id)
            return None
        if inbound.migrate_to:
            self.store.migrate_chat(self.channel, inbound.chat_id, inbound.migrate_to, now)
            return None
        if inbound.membership:
            self._membership(inbound, now)
            return None
        if not inbound.text.strip():
            return None
        self._extra_loaded = False
        if command == "cancel":
            session = self.store.load_session(self.channel, inbound.user_id)
            if inbound.scope != "personal" or (session and session["chat_id"] != inbound.chat_id):
                return Reply(inbound.chat_id, self._t("only_dm"))
            if session and self._older_than_session(inbound, session):
                return Reply(inbound.chat_id, self._t("expired"))
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t("cancelled"))
        if self._limited(inbound.user_id, now):
            return Reply(inbound.chat_id, self._t("slow"))
        if command == "help":
            return self._guide_for(inbound)
        if command is None and inbound.scope == "personal" and self.store.load_session(self.channel, inbound.user_id):
            return self._select(inbound, inbound.text.strip(), now)
        if command is None:
            if inbound.scope == "personal":
                return self._guide_for(inbound)
            return None
        return self._dispatch(inbound, command, argument, now)

    def _dispatch(self, inbound: Inbound, command: str, argument: str, now: datetime) -> Reply | None:
        if self.channel == "feishu" and inbound.scope != "personal" and command in {"add_group", "remove_group", "subscribe", "unsubscribe"}:
            return Reply(inbound.chat_id, self._t("not_group_cmd"))
        if command == "help":
            return self._guide_for(inbound)
        if command == "pair":
            return self._pair(inbound, argument, now)
        if command in {"subscribe_dm", "unsubscribe_dm"} and inbound.scope != "personal" and not self._is_admin(inbound.user_id):
            if self.allow_self_subscribe:
                return Reply(inbound.chat_id, self._t("only_dm"))
            return self._deny(inbound, subscribe=command == "subscribe_dm")
        if command == "subscribe_dm":
            return self._subscribe_dm(inbound, now)
        if command == "unsubscribe_dm":
            return self._unsubscribe_dm(inbound, now)
        if command == "subscribe" and inbound.scope == "group":
            if not self._is_admin(inbound.user_id):
                return self._deny(inbound, group=True)
            return self._subscribe_group(inbound.chat_id, inbound.chat_title or inbound.chat_id, inbound.chat_id, now)
        if command == "unsubscribe" and inbound.scope == "group":
            if not self._is_admin(inbound.user_id):
                return self._deny(inbound, group=True)
            return self._unsubscribe_group(inbound.chat_id, inbound.chat_title or inbound.chat_id, inbound.chat_id, now)
        if command == "subscribe":
            return self._subscribe_dm(inbound, now)
        if command == "unsubscribe":
            return self._unsubscribe_dm(inbound, now)
        if not self._is_admin(inbound.user_id):
            return self._deny(inbound)
        if command == "status":
            return Reply(inbound.chat_id, self._status())
        if command == "cancel":
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t("cancelled"))
        if command == "add_admin":
            return self._add_admin(inbound, argument, now)
        if command in {"add_group", "remove_group"}:
            if inbound.scope != "personal":
                return Reply(inbound.chat_id, self._t("not_group_cmd"))
            return self._open_picker(inbound, command, now)
        return Reply(inbound.chat_id, self._t("help"))

    def _subscribe_dm(self, inbound: Inbound, now: datetime) -> Reply:
        if inbound.scope != "personal":
            return Reply(inbound.chat_id, self._t("only_dm"))
        if not self.allow_self_subscribe and not self._is_admin(inbound.user_id):
            return self._deny(inbound, subscribe=True)
        _key, created = self.store.subscribe(
            channel=self.channel,
            kind="personal",
            address=inbound.user_id,
            display_name=inbound.user_id,
            now=now,
        )
        return Reply(inbound.chat_id, self._t("dm_on" if created else "dm_on_already"))

    def _unsubscribe_dm(self, inbound: Inbound, now: datetime) -> Reply:
        if not self.allow_self_subscribe and not self._is_admin(inbound.user_id):
            return self._deny(inbound)
        removed = self.store.unsubscribe(self.channel, "personal", inbound.user_id, now, reason="unsubscribed")
        return Reply(inbound.chat_id, self._t("dm_off" if removed else "dm_off_none"))

    def _subscribe_group(self, chat_id: str, title: str, reply_chat: str, now: datetime) -> Reply:
        _key, created = self.store.subscribe(
            channel=self.channel,
            kind="group",
            address=chat_id,
            display_name=title,
            now=now,
        )
        return Reply(reply_chat, self._t("group_on" if created else "group_on_already", name=title))

    def _unsubscribe_group(self, chat_id: str, title: str, reply_chat: str, now: datetime) -> Reply:
        removed = self.store.unsubscribe(self.channel, "group", chat_id, now, reason="unsubscribed")
        return Reply(reply_chat, self._t("group_off" if removed else "group_off_none", name=title))

    def _open_picker(self, inbound: Inbound, command: str, now: datetime) -> Reply | None:
        previous = self.store.load_session(self.channel, inbound.user_id)
        if previous and self._older_than_session(inbound, previous):
            return Reply(inbound.chat_id, self._t("expired"))
        active_rows = [
            item
            for item in self.store.subscriptions(self.channel)
            if item["kind"] == "group" and item["status"] == "active"
        ]
        active = {item["address"] for item in active_rows}
        if command == "remove_group":
            choices = [(item["address"], item["display_name"]) for item in active_rows]
            empty = "empty_remove"
            intro = "remove_intro"
        else:
            groups = self._groups()
            if groups is None:
                self.store.clear_session(self.channel, inbound.user_id)
                return Reply(inbound.chat_id, self._t("list_failed"))
            choices = [(chat_id, title) for chat_id, title in groups if chat_id not in active]
            empty = "empty_add"
            intro = "add_intro"
        if not choices:
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t(empty))
        if self.group_membership is not None:
            try:
                choices = [(identity, title) for identity, title in choices if self.group_membership(inbound.user_id, identity)]
            except Exception:
                self.store.clear_session(self.channel, inbound.user_id)
                return Reply(inbound.chat_id, self._t("list_failed"))
        if not choices:
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t(empty))
        payload = [{"id": chat_id, "title": title} for chat_id, title in choices]
        lines = [self._t(intro)]
        for index, item in enumerate(payload, start=1):
            lines.append(self._t("choice", index=str(index), name=item["title"], tail=_tail(item["id"])))
        text = "\n".join(lines)
        with self.store.transaction():
            if inbound.event_id and not self.store.claim_inbound(self.channel, inbound.event_id, self.clock.now()):
                current = self.store.load_session(self.channel, inbound.user_id)
                if (current and not current["expires_at"] and current["prompt_id"] == inbound.event_id
                        and current["chat_id"] == inbound.chat_id):
                    return Reply(inbound.chat_id, current["reply_text"], inbound.user_id, inbound.event_id)
                return None
            current = self.store.load_session(self.channel, inbound.user_id)
            if current and self._older_than_session(inbound, current):
                return Reply(inbound.chat_id, self._t("expired"))
            self.store.save_session(self.channel, inbound.user_id, command, payload,
                                    None if self.require_reply_ack else now + timedelta(seconds=SESSION_SECONDS),
                                    chat_id=inbound.chat_id, prompt_id=inbound.event_id, started_at=now, reply_text=text)
        return Reply(inbound.chat_id, text, inbound.user_id, inbound.event_id)

    @staticmethod
    def _older_than_session(inbound: Inbound, session: dict) -> bool:
        started = parse_utc(session.get("started_at"))
        return bool(inbound.created_at is not None and (started is None or inbound.created_at < started))

    def prepare_reply(self, reply: Reply) -> bool:
        """Recheck sensitive picker data immediately before the adapter sends it."""
        if reply.result_id:
            payload = self.store.inbound_reply(self.channel, reply.result_id)
            created = parse_utc(payload.get("created_at")) if payload else None
            self._extra_loaded = False
            valid = bool(payload and payload.get("user_id") == reply.result_user_id
                         and payload.get("chat_id") == reply.chat_id and payload.get("text") == reply.text
                         and payload.get("group_id") == reply.result_group_id and created
                         and 0 <= (self.clock.now() - created).total_seconds() < 55 * 60
                         and self._is_admin(reply.result_user_id))
            if valid and self.group_membership is not None:
                try:
                    valid = self.group_membership(reply.result_user_id, reply.result_group_id)
                except Exception:
                    valid = False
            self._extra_loaded = False
            return bool(valid and self._is_admin(reply.result_user_id) and created
                        and 0 <= (self.clock.now() - created).total_seconds() < 55 * 60)
        if not reply.picker_user_id:
            return True
        self._extra_loaded = False
        session = self.store.load_session(self.channel, reply.picker_user_id)
        valid = bool(session and session["prompt_id"] == reply.picker_id
                     and session["chat_id"] == reply.chat_id and session["reply_text"] == reply.text
                     and self._is_admin(reply.picker_user_id))
        if valid:
            valid = self._prompt_time_valid(session)
        if valid:
            groups = self._groups()
            valid = groups is not None and all(item["id"] in {identity for identity, _title in groups} for item in session["candidates"])
        if valid and self.group_membership is not None:
            try:
                valid = all(self.group_membership(reply.picker_user_id, item["id"]) for item in session["candidates"])
            except Exception:
                valid = False
        self._extra_loaded = False
        valid = valid and self._is_admin(reply.picker_user_id)
        current = self.store.load_session(self.channel, reply.picker_user_id)
        valid = bool(valid and current and current["prompt_id"] == reply.picker_id
                     and current["chat_id"] == reply.chat_id and current["reply_text"] == reply.text
                     and self._prompt_time_valid(current))
        if not valid:
            self.store.clear_session(self.channel, reply.picker_user_id, prompt_id=reply.picker_id)
        elif self.require_reply_ack:
            self.store.note_session_attempt(self.channel, reply.picker_user_id, reply.picker_id, self.clock.now())
        return valid

    def _prompt_time_valid(self, session: dict) -> bool:
        try:
            now = self.clock.now()
            if session["expires_at"]:
                expires = parse_utc(session["expires_at"])
                return bool(expires and now < expires)
            first = parse_utc(session.get("reply_attempted_at") or session["started_at"])
            return bool(first and 0 <= (now - first).total_seconds() < 55 * 60)
        except (TypeError, ValueError, OverflowError):
            return False

    def confirm_reply(self, reply: Reply, *, sent_at: datetime | None = None) -> None:
        if reply.result_id:
            self.store.confirm_inbound_reply(self.channel, reply.result_id, reply.result_user_id, reply.chat_id)
        if reply.picker_user_id:
            session = self.store.load_session(self.channel, reply.picker_user_id)
            if not session or session["prompt_id"] != reply.picker_id or session["chat_id"] != reply.chat_id:
                return
            first = parse_utc(session.get("reply_attempted_at") or session["started_at"])
            if first is None:
                return
            if (not isinstance(sent_at, datetime) or sent_at.tzinfo is None
                    or not first - timedelta(seconds=2) <= sent_at <= self.clock.now() + timedelta(seconds=30)):
                sent_at = first
            self.store.confirm_session(self.channel, reply.picker_user_id, reply.picker_id,
                                       sent_at + timedelta(seconds=SESSION_SECONDS), started_at=sent_at)

    def _result_reply(self, inbound: Inbound) -> Reply | None:
        payload = self.store.inbound_reply(self.channel, inbound.event_id)
        if (payload and inbound.scope == "personal" and payload.get("user_id") == inbound.user_id
                and payload.get("chat_id") == inbound.chat_id):
            return Reply(inbound.chat_id, payload["text"], result_id=inbound.event_id,
                         result_user_id=inbound.user_id, result_group_id=payload["group_id"])
        return None

    def _select(self, inbound: Inbound, text: str, now: datetime) -> Reply | None:
        session = self.store.load_session(self.channel, inbound.user_id)
        if session is None:
            return Reply(inbound.chat_id, self._t("help"))
        if session["chat_id"] != inbound.chat_id:
            return Reply(inbound.chat_id, self._t("only_dm"))
        if self._older_than_session(inbound, session):
            return Reply(inbound.chat_id, self._t("expired"))
        if not self._is_admin(inbound.user_id):
            self.store.clear_session(self.channel, inbound.user_id)
            return self._deny(inbound)
        expires = parse_utc(session["expires_at"])
        if expires is None or now >= expires:
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t("expired"))
        candidates = session["candidates"]
        index = _list_index(text)
        if index is not None:
            if not 1 <= index <= len(candidates):
                return Reply(inbound.chat_id, self._t("bad_choice"))
            chosen = candidates[index - 1]
        else:
            matches = [item for item in candidates if item.get("title") == text]
            if len(matches) > 1:
                return Reply(inbound.chat_id, self._t("ambiguous"))
            if len(matches) != 1:
                return Reply(inbound.chat_id, self._t("bad_choice"))
            chosen = matches[0]
        groups = self._groups()
        try:
            member = self.group_membership is None or self.group_membership(inbound.user_id, str(chosen["id"]))
        except Exception:
            member = False
        self._extra_loaded = False
        if groups is None or str(chosen["id"]) not in {identity for identity, _title in groups} or not member or not self._is_admin(inbound.user_id):
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t("list_failed"))
        with self.store.transaction():
            if inbound.event_id and not self.store.claim_inbound(self.channel, inbound.event_id, self.clock.now()):
                return self._result_reply(inbound)
            current = self.store.load_session(self.channel, inbound.user_id)
            current_expiry = parse_utc(current["expires_at"]) if current else None
            at_commit = self.clock.now()
            if (not current or current["prompt_id"] != session["prompt_id"] or
                    current["chat_id"] != inbound.chat_id or current_expiry is None or at_commit >= current_expiry):
                return Reply(inbound.chat_id, self._t("expired"))
            if not self.store.clear_session(self.channel, inbound.user_id, prompt_id=session["prompt_id"], commit=False):
                return Reply(inbound.chat_id, self._t("expired"))
            if session["action"] == "add_group":
                result = self._subscribe_group(str(chosen["id"]), str(chosen["title"]), inbound.chat_id, at_commit)
            else:
                result = self._unsubscribe_group(str(chosen["id"]), str(chosen["title"]), inbound.chat_id, at_commit)
            if self.require_reply_ack and inbound.event_id:
                self.store.save_inbound_reply(self.channel, inbound.event_id, {
                    "user_id": inbound.user_id, "chat_id": inbound.chat_id, "group_id": str(chosen["id"]),
                    "text": result.text, "created_at": at_commit.isoformat(),
                })
                return self._result_reply(inbound)
            return result

    def _status(self) -> str:
        rows = self.store.subscriptions(self.channel)
        if not rows:
            return self._t("status_title") + "\n" + self._t("status_empty")
        lines = [self._t("status_title")]
        for item in rows:
            kind = self._t("kind_personal" if item["kind"] == "personal" else "kind_group")
            extra = self._t("stopped", reason=item["stop_reason"]) if item["status"] != "active" and item["stop_reason"] else ""
            lines.append(
                self._t(
                    "status_line",
                    kind=kind,
                    name=item["display_name"],
                    tail=_tail(item["address"]),
                    extra=extra,
                ).rstrip()
            )
        return "\n".join(lines)

    def _pair(self, inbound: Inbound, argument: str, now: datetime) -> Reply:
        if inbound.scope != "personal":
            return Reply(inbound.chat_id, self._t("pair_dm"))
        if self.pairing.too_many(inbound.user_id, now):
            return Reply(inbound.chat_id, self._t("pair_limited"))
        if not self.pairing.consume(inbound.user_id, argument, now):
            return Reply(inbound.chat_id, self._t("pair_bad"))
        self.store.add_chat_admin(self.channel, inbound.user_id, now)
        self._welcome(inbound.user_id)
        return Reply(inbound.chat_id, self._t("pair_ok"))

    def _add_admin(self, inbound: Inbound, argument: str, now: datetime) -> Reply:
        user_id = argument.strip()
        if _ADMIN_ID.fullmatch(user_id) is None:
            return Reply(inbound.chat_id, self._t("admin_usage"))
        self.store.add_chat_admin(self.channel, user_id, now)
        return Reply(inbound.chat_id, self._t("admin_added"))

    def _membership(self, inbound: Inbound, now: datetime) -> None:
        status = "joined" if inbound.membership == "joined" else "left"
        self.store.remember_chat(self.channel, inbound.chat_id, inbound.chat_title or inbound.chat_id, status, now)
        if status == "left":
            self.store.unsubscribe(self.channel, "group", inbound.chat_id, now, reason="bot removed from the chat")

    def _groups(self) -> list[tuple[str, str]] | None:
        if self.group_catalog is None:
            return self.store.known_chats(self.channel, status="joined")
        try:
            listed = self.group_catalog()
        except Exception as exc:
            log.warning("group list failed: %s", type(exc).__name__)
            return None
        return listed

    def _guide_for(self, inbound: Inbound) -> Reply:
        return Reply(
            inbound.chat_id,
            usage_guide(
                self.language,
                channel=self.channel,
                allow_self_subscribe=self.allow_self_subscribe,
                admin=self._is_admin(inbound.user_id),
                scope=inbound.scope,
            ),
        )

    def _deny(self, inbound: Inbound, *, subscribe: bool = False, group: bool = False) -> Reply:
        if not self._any_admin():
            key = "denied_no_admin_feishu" if self.channel == "feishu" else "denied_no_admin_telegram"
            return Reply(inbound.chat_id, self._t(key))
        if subscribe:
            return Reply(inbound.chat_id, self._t("denied_subscribe"))
        if group:
            return Reply(inbound.chat_id, self._t("need_admin_group"))
        return Reply(inbound.chat_id, self._t("denied"))

    def _is_admin(self, user_id: str) -> bool:
        if user_id in self.admins or self.store.is_chat_admin(self.channel, user_id):
            return True
        extra = self._extra_admin_ids()
        return bool(extra and user_id in extra)

    def _any_admin(self) -> bool:
        if self.admins or self.store.chat_admin_ids(self.channel):
            return True
        if self.extra_admins is None:
            return False
        extra = self._extra_admin_ids()
        if extra is None:
            return True
        return bool(extra)

    def _extra_admin_ids(self) -> set[str] | None:
        """Collaborators, when configured. ``None`` means the lookup failed."""
        if self._extra_loaded:
            return self._extra_ids
        self._extra_loaded = True
        if self.extra_admins is None:
            self._extra_ids = set()
            return self._extra_ids
        try:
            self._extra_ids = {str(item) for item in self.extra_admins() if str(item)}
        except Exception as exc:
            log.warning("admin lookup failed: %s", type(exc).__name__)
            self._extra_ids = None
        return self._extra_ids

    def _limited(self, user_id: str, now: datetime) -> bool:
        recent = [stamp for stamp in self._hits.get(user_id, []) if (now - stamp).total_seconds() < COMMAND_WINDOW]
        if len(recent) >= COMMAND_LIMIT:
            self._hits[user_id] = recent
            return True
        recent.append(now)
        self._hits[user_id] = recent
        return False

    def _t(self, key: str, **values: str) -> str:
        return chat_text(self.language, key, **values)

    def _welcome(self, user_id: str) -> None:
        if self.welcome is None or not user_id:
            return
        try:
            self.welcome(user_id)
        except Exception as exc:
            log.warning("welcome guide failed: %s", redact(str(exc)).replace("\n", " ")[:300])


def normalize_command_text(text: str) -> str:
    """Drop a Telegram ``@BotName`` suffix from the first token."""
    stripped = text.strip()
    if not stripped:
        return ""
    token, _, rest = stripped.partition(" ")
    if "@" in token:
        token = token.split("@", 1)[0]
    return (token + (" " + rest if rest else "")).strip()


def _codes_match(offered: str, code: str) -> bool:
    """Compare a pairing attempt without leaking the code through equality timing."""
    left = offered.strip().lower().encode("utf-8")
    right = code.strip().lower().encode("utf-8")
    if len(left) > 64 or len(right) > 64:
        return False
    return hmac.compare_digest(left, right)


def _list_index(text: str) -> int | None:
    """A short ASCII number. Unicode digits are not indexes; ``int('²')`` would throw."""
    if not text.isascii() or not text.isdigit() or len(text) > 6:
        return None
    return int(text)


def _split_command(text: str) -> tuple[str | None, str]:
    normalized = normalize_command_text(text)
    if not normalized:
        return None, ""
    token, _, rest = normalized.partition(" ")
    name = _COMMANDS.get(token) or _COMMANDS.get(token.lower())
    if name is None:
        return None, ""
    return name, rest.strip()


def _tail(value: str) -> str:
    text = value or ""
    return text[-8:] if len(text) > 8 else text


def issue_pairing_code(gate: PairingGate, now: datetime) -> str:
    """Print a one-time code to stdout. The logging module does not receive it."""
    code = gate.issue(now)
    print(f"Telegram pairing code (single use, expires in 10 minutes): {code}", flush=True)
    log.info("a Telegram pairing code was printed to stdout; it is single use and expires in 10 minutes")
    return code
