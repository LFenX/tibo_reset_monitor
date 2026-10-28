"""One usage guide per admin, delivered with the same retry rules as the outbox.

A failed send is stored and tried again on a later poll. It does not sleep and
it does not stop the source fetch. A direct message the platform will not
accept is recorded as skipped and is not retried until the person opens the
chat with /start or /pair.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from tibo_reset_monitor.core.i18n import chat_text
from tibo_reset_monitor.core.model import (
    PERMANENT,
    SENT,
    DeliveryState,
    Receipt,
    parse_utc,
)
from tibo_reset_monitor.core.outbox import idempotency_key, on_receipt, on_recover
from tibo_reset_monitor.logutil import redact

log = logging.getLogger("tibo_reset_monitor")

_PAIRING_CODE = re.compile(r"(?i)(pairing code\b[^:\n]*:\s*)([0-9a-f]{6})")


def guide_language(channel: Any, recipient: str) -> str:
    """Target language when this person is a personal target, otherwise the channel language."""
    settings = getattr(channel, "settings", None) or {}
    fallback = settings.get("language", "zh") if isinstance(settings, dict) else "zh"
    if fallback not in {"zh", "en"}:
        fallback = "zh"
    for target in getattr(channel, "targets", ()) or ():
        if getattr(target, "kind", "") == "personal" and str(getattr(target, "address", "")) == recipient:
            language = (getattr(target, "options", None) or {}).get("language") or fallback
            return "en" if language == "en" else "zh"
    return "en" if fallback == "en" else "zh"


def deliver_guides(store: Any, channels: Any, clock: Any, *, only: tuple[str, str, str] | None = None) -> None:
    """Send due guides. One failure is recorded and the rest still run."""
    try:
        now = clock.now()
        _recover_inflight(store, now)
        rows = store.due_guides(now)
    except Exception as exc:
        log.warning("welcome guide lookup failed: %s", _safe(exc))
        return
    by_name = {getattr(channel, "name", ""): channel for channel in channels}
    for row in rows:
        key = (str(row["channel"]), str(row["recipient"]), str(row["version"]))
        if only is not None and key != only:
            continue
        try:
            _deliver_one(store, by_name.get(key[0]), row, clock.now())
        except Exception as exc:
            log.warning("welcome guide failed: %s", _safe(exc))
            _hold(store, key, clock.now(), _safe(exc))


def _recover_inflight(store: Any, now: datetime) -> None:
    for row in store.inflight_guides():
        store.write_guide(str(row["channel"]), str(row["recipient"]), str(row["version"]), on_recover(_state(row), now), now)


def _deliver_one(store: Any, channel: Any, row: Any, now: datetime) -> None:
    channel_name = str(row["channel"])
    recipient = str(row["recipient"])
    version = str(row["version"])
    if not store.claim_guide(channel_name, recipient, version, now):
        return
    claimed = store.guide_row(channel_name, recipient, version)
    if claimed is None:
        return
    sender = getattr(channel, "send_text", None)
    if channel is None or sender is None:
        _skip(store, claimed, now, "this channel cannot send a direct usage guide")
        return
    text = chat_text(str(claimed["language"] or "zh"), "guide")
    key = idempotency_key(f"guide\x1f{version}", channel_name, recipient)
    try:
        receipt = sender(recipient, text, idempotency_key=key)
    except Exception as exc:
        receipt = Receipt("retryable", detail=_safe(exc))
    if not isinstance(receipt, Receipt):
        receipt = Receipt("retryable", detail="channel returned no receipt")
    previous = _state(claimed)
    decision = on_receipt(previous, receipt, now)
    if receipt.outcome == "permanent" or decision.status == PERMANENT:
        _skip(store, claimed, now, _skip_reason(channel_name, receipt.detail or decision.last_error or ""))
        return
    if decision.last_error:
        decision.last_error = redact(decision.last_error)[:300]
    store.write_guide(channel_name, recipient, version, decision, now)
    if decision.status == SENT:
        log.info("welcome guide sent channel=%s recipient=%s", channel_name, recipient)
        return
    log.info(
        "welcome guide will retry channel=%s recipient=%s: %s",
        channel_name,
        recipient,
        decision.last_error or decision.receipt_detail or "retryable",
    )


def _skip(store: Any, row: Any, now: datetime, reason: str) -> None:
    reason = redact(reason).replace("\n", " ")[:300] or "direct message is not available"
    state = _state(row)
    state.status = "skipped"
    state.next_attempt_at = None
    state.last_error = reason
    store.write_guide(str(row["channel"]), str(row["recipient"]), str(row["version"]), state, now)
    log.info(
        "welcome guide skipped channel=%s recipient=%s: %s",
        row["channel"],
        row["recipient"],
        reason,
    )


def _hold(store: Any, key: tuple[str, str, str], now: datetime, detail: str) -> None:
    """Keep a claimed row from spinning if the send path raised after the claim."""
    row = store.guide_row(*key)
    if row is None or str(row["status"]) != "inflight":
        return
    receipt = Receipt("retryable", detail=detail)
    decision = on_receipt(_state(row), receipt, now)
    if decision.last_error:
        decision.last_error = redact(decision.last_error)[:300]
    store.write_guide(key[0], key[1], key[2], decision, now)


def _state(row: Any) -> DeliveryState:
    return DeliveryState(
        status=str(row["status"]),
        attempts=int(row["attempts"] or 0),
        next_attempt_at=parse_utc(row["next_attempt_at"]),
        first_ambiguous_at=parse_utc(row["first_ambiguous_at"]),
        last_error=row["last_error"],
        receipt_message_id=None,
        receipt_detail=row["last_error"],
    )


def _skip_reason(channel: str, detail: str) -> str:
    text = (detail or "").lower()
    if channel == "telegram" and any(token in text for token in ("403", "chat not found", "blocked", "initiate")):
        return "the user has not started the bot, so a direct message is not possible yet"
    if channel == "feishu" and any(token in text for token in ("forbidden", "not found", "permission", "unavailable")):
        return "a direct message to this user is not available"
    return redact(detail)[:300] or "direct message is not available"


def _safe(exc: BaseException) -> str:
    text = redact(str(exc)).replace("\n", " ")
    return _PAIRING_CODE.sub(r"\1<redacted>", text)[:300]
