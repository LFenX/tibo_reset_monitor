"""One usage guide per admin, delivered with the same retry rules as the outbox.

A clear failure is stored and tried again on a later poll. Unknown outcomes use
the notification safety rules. It does not sleep and
it does not stop the source fetch. A direct message the platform will not
accept is recorded as skipped and is not retried until the person opens the
chat with /start or /pair.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from tibo_reset_monitor.core.i18n import usage_guide
from tibo_reset_monitor.core.model import (
    PERMANENT,
    NEEDS_REVIEW,
    SENT,
    DeliveryState,
    Receipt,
    parse_utc,
)
from tibo_reset_monitor.core.outbox import ambiguous_expired, idempotency_key, on_receipt, on_recover, send_lease_until, validated_receipt
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
    by_name = {getattr(channel, "name", ""): channel for channel in channels}
    try:
        now = clock.now()
        _recover_inflight(store, now, by_name)
        rows = store.due_guides(now)
    except Exception as exc:
        log.warning("welcome guide lookup failed: %s", _safe(exc))
        return
    for row in rows:
        key = (str(row["channel"]), str(row["recipient"]), str(row["version"]))
        if only is not None and key != only:
            continue
        try:
            _deliver_one(store, by_name.get(key[0]), row, clock.now())
        except Exception as exc:
            log.warning("welcome guide failed: %s", _safe(exc))
            # A failure after submission may have lost its ACK. Leave the claim
            # for guarded recovery rather than changing another owner's send.


def _idempotent(channel: Any, channel_name: str) -> bool:
    return getattr(channel, "supports_idempotency", channel_name != "telegram") is not False


def _recover_inflight(store: Any, now: datetime, channels: dict[str, Any]) -> None:
    for row in store.inflight_guides():
        key = (str(row["channel"]), str(row["recipient"]), str(row["version"]))
        with store.delivery_guard(("guide", *key)) as owned:
            if not owned:
                continue
            current = store.guide_row(*key)
            if current is None or current["status"] != "inflight":
                continue
            lease = parse_utc(current["lease_until"])
            if lease is not None and now < lease:
                continue
            state = _state(current)
            state.first_ambiguous_at = state.first_ambiguous_at or parse_utc(current["updated_at"])
            decision = on_recover(state, now)
            if not _idempotent(channels.get(key[0]), key[0]):
                decision.status, decision.next_attempt_at = NEEDS_REVIEW, None
            store.write_guide(*key, decision, now, only_status="inflight")


def _deliver_one(store: Any, channel: Any, row: Any, now: datetime) -> None:
    identity = ("guide", str(row["channel"]), str(row["recipient"]), str(row["version"]))
    with store.delivery_guard(identity) as owned:
        if owned:
            current = store.guide_row(*identity[1:])
            if current is not None:
                _deliver_guarded(store, channel, current, now)


def _deliver_guarded(store: Any, channel: Any, row: Any, now: datetime) -> None:
    channel_name = str(row["channel"])
    recipient = str(row["recipient"])
    version = str(row["version"])
    previous = _state(row)
    unverified_body = previous.attempts > 0 and row["message_text"] is None
    if unverified_body or ambiguous_expired(previous, now) or (previous.first_ambiguous_at is not None and not _idempotent(channel, channel_name)):
        previous.status, previous.next_attempt_at = NEEDS_REVIEW, None
        previous.last_error = ("original attempted guide body is unavailable" if unverified_body else
                               previous.last_error or "guide outcome requires review before another send")
        store.write_guide(channel_name, recipient, version, previous, now, only_status=str(row["status"]))
        return
    if row["message_text"] is None and channel is not None:
        settings = getattr(channel, "settings", None) or {}
        allow_self = settings.get("allow_self_subscribe") == "true" if isinstance(settings, dict) else False
        text = usage_guide(str(row["language"] or "zh"), channel=channel_name, allow_self_subscribe=allow_self, admin=True, scope="personal")
        store.save_guide_message(channel_name, recipient, version, text)
    if not store.claim_guide(channel_name, recipient, version, now, lease_until=send_lease_until(now, getattr(channel, "_timeout", 20))):
        return
    claimed = store.guide_row(channel_name, recipient, version)
    if claimed is None:
        return
    sender = getattr(channel, "send_text", None)
    if channel is None or sender is None:
        _skip(store, claimed, now, "this channel cannot send a direct usage guide")
        return
    text = str(claimed["message_text"])
    key = idempotency_key(f"guide\x1f{version}", channel_name, recipient)
    try:
        receipt = sender(recipient, text, idempotency_key=key)
    except Exception as exc:
        receipt = Receipt("ambiguous", detail=_safe(exc))
    receipt = validated_receipt(receipt)
    previous = _state(claimed)
    if receipt.outcome == "ambiguous" and previous.first_ambiguous_at is None:
        previous.first_ambiguous_at = now
    decision = on_receipt(previous, receipt, now)
    if receipt.outcome == "ambiguous" and not _idempotent(channel, channel_name):
        decision.status, decision.next_attempt_at = NEEDS_REVIEW, None
    if receipt.outcome == "permanent" or decision.status == PERMANENT:
        _skip(store, claimed, now, _skip_reason(channel_name, receipt.detail or decision.last_error or ""))
        return
    if decision.last_error:
        decision.last_error = _safe_text(decision.last_error)
    if not store.write_guide(channel_name, recipient, version, decision, now, only_status="inflight"):
        return
    if decision.status == SENT:
        log.info("welcome guide sent channel=%s recipient=%s", channel_name, recipient)
        return
    if decision.status == NEEDS_REVIEW:
        log.warning("welcome guide needs review channel=%s recipient=%s: %s", channel_name, recipient, decision.last_error)
        return
    log.info(
        "welcome guide will retry channel=%s recipient=%s: %s",
        channel_name,
        recipient,
        decision.last_error or decision.receipt_detail or "retryable",
    )


def _skip(store: Any, row: Any, now: datetime, reason: str) -> None:
    reason = _safe_text(reason) or "direct message is not available"
    state = _state(row)
    state.status = "skipped"
    state.next_attempt_at = None
    state.last_error = reason
    if not store.write_guide(str(row["channel"]), str(row["recipient"]), str(row["version"]), state, now, only_status="inflight"):
        return
    log.info(
        "welcome guide skipped channel=%s recipient=%s: %s",
        row["channel"],
        row["recipient"],
        reason,
    )


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
    return _safe_text(str(exc))


def _safe_text(value: str) -> str:
    text = redact(value).replace("\n", " ")
    return _PAIRING_CODE.sub(r"\1<redacted>", text)[:300]
