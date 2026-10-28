"""Delivery state machine.

Each (notice, target) row keeps one idempotency key for its whole life. A clear
failure is retried with backoff. A timeout is ambiguous: the same key is reused
until 55 minutes after the first ambiguous outcome, then the row becomes
needs_review instead of being retried blindly. Feishu dedupes that key for about
an hour, so the review cutoff stays inside that window.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from tibo_reset_monitor.core.model import (
    AMBIGUOUS,
    AMBIGUOUS_REVIEW_AFTER_SECONDS,
    NEEDS_REVIEW,
    PERMANENT,
    RETRYABLE,
    SENT,
    DeliveryState,
    Receipt,
)

_CLEAR_BASE_SECONDS = 30
_AMBIGUOUS_BASE_SECONDS = 120
_MAX_DELAY_SECONDS = 900


def idempotency_key(dedupe_key: str, channel: str, target_key: str, *, salt: str = "") -> str:
    """Stable key, at most 48 characters, safe for the Feishu ``uuid`` field (max 50)."""
    material = f"{dedupe_key}|{channel}|{target_key}|{salt}".encode("utf-8")
    return "trm" + hashlib.sha256(material).hexdigest()[:45]


def ambiguous_expired(state: DeliveryState, now: datetime) -> bool:
    if state.status != AMBIGUOUS or state.first_ambiguous_at is None:
        return False
    age = (now - state.first_ambiguous_at).total_seconds()
    return age >= AMBIGUOUS_REVIEW_AFTER_SECONDS


def retry_delay(attempts: int, retry_after_seconds: float | None, *, ambiguous: bool) -> float:
    exponent = max(0, attempts - 1)
    base = _AMBIGUOUS_BASE_SECONDS if ambiguous else _CLEAR_BASE_SECONDS
    delay = min(_MAX_DELAY_SECONDS, base * (2**exponent))
    if retry_after_seconds is not None:
        delay = max(delay, min(_MAX_DELAY_SECONDS, retry_after_seconds))
    return float(delay)


def on_receipt(previous: DeliveryState, receipt: Receipt, now: datetime) -> DeliveryState:
    attempts = previous.attempts
    if receipt.outcome == "sent":
        return DeliveryState(
            status=SENT,
            attempts=attempts,
            next_attempt_at=None,
            first_ambiguous_at=previous.first_ambiguous_at,
            last_error=None,
            receipt_message_id=receipt.provider_message_id,
            receipt_detail=receipt.detail or "sent",
        )
    if receipt.outcome == "permanent":
        return DeliveryState(
            status=PERMANENT,
            attempts=attempts,
            next_attempt_at=None,
            first_ambiguous_at=previous.first_ambiguous_at,
            last_error=receipt.detail or "permanent failure",
            receipt_message_id=None,
            receipt_detail=receipt.detail,
        )
    if receipt.outcome == "ambiguous":
        first = previous.first_ambiguous_at or now
        detail = receipt.detail or "ambiguous outcome"
        if (now - first).total_seconds() >= AMBIGUOUS_REVIEW_AFTER_SECONDS:
            return DeliveryState(
                status=NEEDS_REVIEW,
                attempts=attempts,
                next_attempt_at=None,
                first_ambiguous_at=first,
                last_error=detail,
                receipt_message_id=None,
                receipt_detail=detail,
            )
        delay = retry_delay(attempts, receipt.retry_after_seconds, ambiguous=True)
        return DeliveryState(
            status=AMBIGUOUS,
            attempts=attempts,
            next_attempt_at=now + timedelta(seconds=delay),
            first_ambiguous_at=first,
            last_error=detail,
            receipt_message_id=None,
            receipt_detail=detail,
        )
    if receipt.outcome != "retryable":
        raise ValueError(f"unknown receipt outcome {receipt.outcome!r}")
    delay = retry_delay(attempts, receipt.retry_after_seconds, ambiguous=False)
    detail = receipt.detail or "retryable failure"
    return DeliveryState(
        status=RETRYABLE,
        attempts=attempts,
        next_attempt_at=now + timedelta(seconds=delay),
        first_ambiguous_at=previous.first_ambiguous_at,
        last_error=detail,
        receipt_message_id=None,
        receipt_detail=detail,
    )


def on_recover(previous: DeliveryState, now: datetime) -> DeliveryState:
    """An in-flight row has no receipt. Treat it as ambiguous and keep its key."""
    first = previous.first_ambiguous_at or now
    detail = previous.last_error or "delivery interrupted before a receipt was recorded"
    if (now - first).total_seconds() >= AMBIGUOUS_REVIEW_AFTER_SECONDS:
        return DeliveryState(
            status=NEEDS_REVIEW,
            attempts=previous.attempts,
            next_attempt_at=None,
            first_ambiguous_at=first,
            last_error=detail,
            receipt_message_id=previous.receipt_message_id,
            receipt_detail=detail,
        )
    return DeliveryState(
        status=AMBIGUOUS,
        attempts=previous.attempts,
        next_attempt_at=now,
        first_ambiguous_at=first,
        last_error=detail,
        receipt_message_id=None,
        receipt_detail=detail,
    )
