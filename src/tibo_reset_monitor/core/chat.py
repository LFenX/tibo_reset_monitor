"""Channel-neutral chat control.

A channel turns an inbound event into :class:`Inbound` and sends the
:class:`Reply`. Parsing, permissions, the one-minute picker, and subscription
changes live here so every channel shares them.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from tibo_reset_monitor.core.i18n import chat_text
from tibo_reset_monitor.core.model import parse_utc
from tibo_reset_monitor.core.store import StateStore

log = logging.getLogger("tibo_reset_monitor")

SESSION_SECONDS = 60
PAIR_SECONDS = 10 * 60
PAIR_ATTEMPTS = 5
COMMAND_LIMIT = 20
COMMAND_WINDOW = 60

_COMMANDS = {
    "/start": "help",
    "/help": "help",
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


@dataclass(frozen=True)
class Reply:
    chat_id: str
    text: str


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
            and offered.strip() == self.code
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
        self._hits: dict[str, list[datetime]] = {}

    def handle(self, inbound: Inbound) -> Reply | None:
        now = self.clock.now()
        if inbound.event_id and not self.store.claim_inbound(self.channel, inbound.event_id, now):
            return None
        if inbound.migrate_to:
            self.store.migrate_chat(self.channel, inbound.chat_id, inbound.migrate_to, now)
            return None
        if inbound.membership:
            self._membership(inbound, now)
            return None
        if not inbound.text.strip():
            return None
        if self._limited(inbound.user_id, now):
            return Reply(inbound.chat_id, self._t("slow"))
        command, argument = _split_command(inbound.text)
        if command is None and self.store.load_session(self.channel, inbound.user_id):
            return self._select(inbound, inbound.text.strip(), now)
        if command is None:
            if inbound.scope == "personal":
                return Reply(inbound.chat_id, self._t("help"))
            return None
        return self._dispatch(inbound, command, argument, now)

    def _dispatch(self, inbound: Inbound, command: str, argument: str, now: datetime) -> Reply | None:
        if command == "help":
            return Reply(inbound.chat_id, self._t("help"))
        if command == "pair":
            return self._pair(inbound, argument, now)
        if command in {"subscribe_dm", "unsubscribe_dm"} and inbound.scope != "personal" and not self._is_admin(inbound.user_id):
            return Reply(inbound.chat_id, self._t("only_dm"))
        if command == "subscribe_dm":
            return self._subscribe_dm(inbound, now)
        if command == "unsubscribe_dm":
            return self._unsubscribe_dm(inbound, now)
        if command == "subscribe" and inbound.scope == "group":
            if not self._is_admin(inbound.user_id):
                return Reply(inbound.chat_id, self._t("need_admin_group"))
            return self._subscribe_group(inbound.chat_id, inbound.chat_title or inbound.chat_id, inbound.chat_id, now)
        if command == "unsubscribe" and inbound.scope == "group":
            if not self._is_admin(inbound.user_id):
                return Reply(inbound.chat_id, self._t("need_admin_group"))
            return self._unsubscribe_group(inbound.chat_id, inbound.chat_title or inbound.chat_id, inbound.chat_id, now)
        if command == "subscribe":
            return self._subscribe_dm(inbound, now)
        if command == "unsubscribe":
            return self._unsubscribe_dm(inbound, now)
        if not self._is_admin(inbound.user_id):
            return Reply(inbound.chat_id, self._t("denied"))
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
            return Reply(inbound.chat_id, self._t("denied"))
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
            return Reply(inbound.chat_id, self._t("denied"))
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

    def _open_picker(self, inbound: Inbound, command: str, now: datetime) -> Reply:
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
                return Reply(inbound.chat_id, self._t("list_failed"))
            choices = [(chat_id, title) for chat_id, title in groups if chat_id not in active]
            empty = "empty_add"
            intro = "add_intro"
        if not choices:
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t(empty))
        payload = [{"id": chat_id, "title": title} for chat_id, title in choices]
        self.store.save_session(self.channel, inbound.user_id, command, payload, now + timedelta(seconds=SESSION_SECONDS))
        lines = [self._t(intro)]
        for index, item in enumerate(payload, start=1):
            lines.append(self._t("choice", index=str(index), name=item["title"], tail=_tail(item["id"])))
        return Reply(inbound.chat_id, "\n".join(lines))

    def _select(self, inbound: Inbound, text: str, now: datetime) -> Reply:
        session = self.store.load_session(self.channel, inbound.user_id)
        if session is None:
            return Reply(inbound.chat_id, self._t("help"))
        expires = parse_utc(session["expires_at"])
        if expires is None or now > expires:
            self.store.clear_session(self.channel, inbound.user_id)
            return Reply(inbound.chat_id, self._t("expired"))
        candidates = session["candidates"]
        if text.isdigit():
            index = int(text)
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
        self.store.clear_session(self.channel, inbound.user_id)
        if session["action"] == "add_group":
            return self._subscribe_group(str(chosen["id"]), str(chosen["title"]), inbound.chat_id, now)
        return self._unsubscribe_group(str(chosen["id"]), str(chosen["title"]), inbound.chat_id, now)

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
        return Reply(inbound.chat_id, self._t("pair_ok"))

    def _add_admin(self, inbound: Inbound, argument: str, now: datetime) -> Reply:
        user_id = argument.strip()
        if not user_id or not user_id.lstrip("-").isdigit() and not user_id.startswith("ou_"):
            if not user_id:
                return Reply(inbound.chat_id, self._t("admin_usage"))
        if not user_id:
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

    def _is_admin(self, user_id: str) -> bool:
        if user_id in self.admins or self.store.is_chat_admin(self.channel, user_id):
            return True
        if self.extra_admins is None:
            return False
        try:
            return user_id in self.extra_admins()
        except Exception as exc:
            log.warning("admin lookup failed: %s", type(exc).__name__)
            return False

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


def normalize_command_text(text: str) -> str:
    """Drop a Telegram ``@BotName`` suffix from the first token."""
    stripped = text.strip()
    if not stripped:
        return ""
    token, _, rest = stripped.partition(" ")
    if "@" in token:
        token = token.split("@", 1)[0]
    return (token + (" " + rest if rest else "")).strip()


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
