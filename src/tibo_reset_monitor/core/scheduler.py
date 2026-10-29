"""One poll cycle: fetch, baseline or enqueue, then deliver due outbox rows.

Channel objects are injected. This module does not import any channel implementation.
A fetch failure never writes a snapshot. Delivery of one target cannot roll back another.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any, Protocol, Sequence

from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.model import (
    INFLIGHT,
    PERMANENT,
    SENT,
    CycleReport,
    Receipt,
    Target,
    utc_now,
)
from tibo_reset_monitor.core.outbox import ambiguous_expired, on_receipt, on_recover
from tibo_reset_monitor.core.store import StateStore
from tibo_reset_monitor.core.source import automatic_after, automatic_eligible, source_time, validate_snapshot

log = logging.getLogger("tibo_reset_monitor")


class ChannelAdapter(Protocol):
    name: str
    targets: Sequence[Target]

    def render(self, notice: Any, target: Target) -> dict[str, Any]:
        ...

    def send(self, message: dict[str, Any], target: Target, idempotency_key: str) -> Receipt:
        ...


def run_cycle(
    *,
    store: StateStore,
    source: Any,
    channels: Sequence[ChannelAdapter],
    clock: Any,
    heartbeat: Any = None,
    before_delivery: Any = None,
) -> CycleReport:
    now = clock.now()
    report = CycleReport()
    _beat(heartbeat)
    _recover(store, now)

    # Commit before fetch/parse: a killed process must not release old cached work.
    store.set_meta("automatic_blocked", "1")
    store.conn.commit()
    try:
        result = source.fetch(store.get_etag())
        if result.failure is not None:
            report.fetch_error_kind = result.failure.kind
            report.retry_after_seconds = result.failure.retry_after_seconds
            raise ValueError(result.failure.message)
        cached = store.get_snapshot() if result.not_modified else None
        body = cached["body"] if cached else result.body
        validate_snapshot(body, now)
        notices = extract_notices(body)
        _check_conflicts(store, notices, body, now)
        previous_success = store.get_meta("last_success_at")
        success_at = max(now, source_time(previous_success)) if previous_success else now
        with store.transaction():
            if not store.is_baselined():
                if result.not_modified:
                    raise ValueError("304 has no completed baseline")
                report.baselined = store.baseline(notices, body, result.etag, now)
                status = "baseline"
            elif result.not_modified:
                report.not_modified = True
                report.cancelled = store.reconcile_source(notices, now)
                store.save_snapshot(body, result.etag or cached["etag"], now)
                status = "not_modified"
            else:
                created, cancelled = store.ingest(notices, effective_targets(channels, store), body, result.etag, now)
                report.new_notices = len(created)
                report.cancelled = cancelled
                status = "ok"
            store.note_fetch(status, None, now)
            store.set_meta("last_success_at", utc_stamp(success_at))
            store.set_meta("automatic_blocked", "0")
    except Exception as exc:
        report.fetch_error = str(exc)
        report.fetch_error_kind = report.fetch_error_kind or "malformed"
        store.note_fetch(report.fetch_error_kind, str(exc), now)
        store.conn.commit()
        log.error("source check failed (%s): %s", report.fetch_error_kind, type(exc).__name__)

    if before_delivery is not None and report.fetch_error is None:
        before_delivery()
    report.deliveries = deliver_due(store, channels, clock, heartbeat=heartbeat)
    return report


def utc_stamp(now: Any) -> str:
    from tibo_reset_monitor.core.model import format_utc
    return format_utc(now)


def _check_conflicts(store: StateStore, notices: Any, body: Any, now: Any) -> None:
    baseline = store.get_meta("baseline_completed_at") or utc_stamp(now)
    after = automatic_after(body, now, baseline, store.get_meta("last_success_at"))
    protected = {notice.post_key for notice in notices if automatic_eligible(notice, after, now)}
    protected.update(row.notice.post_key for row in store.automatic_rows() if store.automatic_valid(row, body, now))
    stages: dict[str, set[str]] = {}
    for notice in notices:
        stages.setdefault(notice.post_key, set()).add(notice.stage)
    if any(len(stages.get(identity, ())) > 1 for identity in protected):
        raise ValueError("source post has conflicting stages")


def deliver_due(
    store: StateStore,
    channels: Sequence[ChannelAdapter],
    clock: Any,
    heartbeat: Any = None,
    only_row_id: int | None = None,
) -> list[dict[str, str]]:
    by_name = {channel.name: channel for channel in channels}
    reports: list[dict[str, str]] = []
    for row in store.due_rows(clock.now()):
        if only_row_id is not None and row.id != only_row_id:
            continue
        _beat(heartbeat)
        now = clock.now()
        if row.automatic and store.get_meta("automatic_blocked") != "0":
            continue
        if ambiguous_expired(row.state, now):
            store.mark_needs_review(row, now)
            store.conn.commit()
            reports.append(_delivery_report(row.channel, row.target_key, "needs_review", row.state.last_error or ""))
            log.error(
                "delivery needs review channel=%s target=%s notice=%s",
                row.channel,
                row.target_key,
                row.notice_id,
            )
            continue
        channel = by_name.get(row.channel)
        if channel is not None and getattr(channel, "supports_idempotency", True) is False and row.state.first_ambiguous_at is not None:
            row.state.last_error = "provider cannot safely retry an ambiguous delivery"
            store.mark_needs_review(row, now)
            store.conn.commit()
            reports.append(_delivery_report(row.channel, row.target_key, "needs_review", row.state.last_error))
            continue
        target = _find_target(channel, row.target_key, store) if channel else None
        if target is not None and (target.kind != row.target_kind or (row.target_address is not None and target.address != row.target_address)):
            row.state.last_error = "delivery target changed since it was queued"
            store.mark_needs_review(row, now)
            store.conn.commit()
            reports.append(_delivery_report(row.channel, row.target_key, "needs_review", row.state.last_error))
            continue
        if row.automatic:
            try:
                snapshot = store.get_snapshot()
                body = validate_snapshot(snapshot["body"], now) if snapshot else None
                current = extract_notices(body) if body else []
                safe = (body is not None and store.automatic_valid(row, body, now)
                        and any(item.post_key == row.notice.post_key and source_time(item.published_at) == source_time(row.source_published_at)
                                and item.url == row.source_url for item in current)
                        and target is not None and row.target_address == target.address and not store.duplicate_automatic(row))
            except Exception:
                safe = False
            if not safe:
                row.state.last_error = "automatic delivery no longer matches the source, time window or target"
                store.mark_needs_review(row, now)
                store.conn.commit()
                reports.append(_delivery_report(row.channel, row.target_key, "needs_review", row.state.last_error))
                continue
        if channel is None or target is None:
            when = now + timedelta(seconds=600)
            store.defer(row.id, when, "target is not in the current config")
            store.conn.commit()
            reports.append(_delivery_report(row.channel, row.target_key, "deferred", "target is not in the current config"))
            continue
        if row.message is None:
            try:
                message = channel.render(row.notice, target)
                if not isinstance(message, dict):
                    raise TypeError("render must return an object")
                store.save_message(row.id, message)
                store.conn.commit()
            except Exception as exc:
                store.defer(row.id, now + timedelta(seconds=30), f"render failed: {type(exc).__name__}")
                store.conn.commit()
                reports.append(_delivery_report(row.channel, row.target_key, "deferred", "render failed"))
                continue
        with store.delivery_guard(row.id) as owned:
            claimed_at = clock.now()
            timeout = float(getattr(channel, "_timeout", 20))
            timeout = timeout if math.isfinite(timeout) and timeout >= 0 else 20
            lease_until = claimed_at + timedelta(seconds=max(600, timeout * 8 + 60))
            if not owned or not store.claim(row.id, claimed_at, lease_until=lease_until):
                continue
            store.conn.commit()
            claimed = store.read_row(row.id)
            if claimed.state.status != INFLIGHT:
                continue
            receipt = _send(channel, target, claimed)
            decided_at = clock.now()
            if receipt.outcome == "ambiguous" and claimed.state.first_ambiguous_at is None:
                claimed.state.first_ambiguous_at = claimed_at
            decision = on_receipt(claimed.state, receipt, decided_at)
            if receipt.outcome == "ambiguous" and getattr(channel, "supports_idempotency", True) is False:
                decision.status = "needs_review"
                decision.next_attempt_at = None
            if not store.apply_state(claimed.id, decision, decided_at):
                continue
            store.conn.commit()
        if target is not None and target.options.get("subscription") == "1":
            if decision.status == SENT:
                store.record_subscription_result(claimed.channel, claimed.target_key, ok=True, detail="", now=decided_at)
            elif decision.status == PERMANENT:
                store.record_subscription_result(
                    claimed.channel,
                    claimed.target_key,
                    ok=False,
                    detail=decision.last_error or decision.receipt_detail or "permanent delivery failure",
                    now=decided_at,
                )
        store.conn.commit()
        reports.append(_delivery_report(claimed.channel, claimed.target_key, decision.status, decision.last_error or decision.receipt_detail or ""))
        log.info(
            "delivery channel=%s target=%s notice=%s status=%s",
            claimed.channel,
            claimed.target_key,
            claimed.notice_id,
            decision.status,
        )
    return reports


def _send(channel: ChannelAdapter, target: Target, row: Any) -> Receipt:
    try:
        message = row.message if row.message is not None else channel.render(row.notice, target)
    except Exception as exc:
        return Receipt("retryable", detail=f"render failed: {type(exc).__name__}")
    try:
        receipt = channel.send(message, target, row.idempotency_key)
        if not isinstance(receipt, Receipt) or receipt.outcome not in {"sent", "retryable", "ambiguous", "permanent"}:
            return Receipt("ambiguous", detail="channel returned an invalid receipt")
        retry = receipt.retry_after_seconds
        if (not isinstance(receipt.detail, str) or (retry is not None and
                (isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry < 0))):
            return Receipt("ambiguous", detail="channel returned invalid receipt fields")
        if receipt.outcome == "sent" and (not isinstance(receipt.provider_message_id, str) or not receipt.provider_message_id.strip()):
            return Receipt("ambiguous", detail="channel returned no message id")
        return receipt
    except TimeoutError as exc:
        return Receipt("ambiguous", detail="send timed out")
    except OSError as exc:
        return Receipt("ambiguous", detail=type(exc).__name__)
    except Exception as exc:  # a channel bug must not block the rest of the outbox
        log.warning("channel %s send result unknown (%s)", channel.name, type(exc).__name__)
        return Receipt("ambiguous", detail=f"send result unknown: {type(exc).__name__}")


def _recover(store: StateStore, now: Any) -> None:
    rows = store.inflight_rows()
    if not rows:
        return
    updates = {}
    for row in rows:
        if store.delivery_active(row.id) or (row.lease_until is not None and now < row.lease_until):
            continue
        row.state.first_ambiguous_at = row.state.first_ambiguous_at or row.attempted_at
        updates[row.id] = on_recover(row.state, now)
    store.recover_inflight(updates, now)
    store.conn.commit()
    if updates:
        log.warning("recovered %s in-flight deliveries as ambiguous", len(updates))


def effective_targets(channels: Sequence[ChannelAdapter], store: StateStore) -> list[Target]:
    """Static config targets plus active chat subscriptions. The same address is kept once."""
    found: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for channel in channels:
        settings = getattr(channel, "settings", {}) or {}
        language = settings.get("language", "zh") if isinstance(settings, dict) else "zh"
        timezone_name = settings.get("timezone", "UTC") if isinstance(settings, dict) else "UTC"
        for target in channel.targets:
            found.append(target)
            seen.add((target.channel, target.address))
        for target in store.subscription_targets(channel.name, language, timezone_name):
            if (target.channel, target.address) in seen:
                continue
            found.append(target)
            seen.add((target.channel, target.address))
    return found


def _find_target(channel: ChannelAdapter | None, key: str, store: StateStore) -> Target | None:
    if channel is None:
        return None
    for target in channel.targets:
        if target.key == key:
            return target
    settings = getattr(channel, "settings", {}) or {}
    language = settings.get("language", "zh") if isinstance(settings, dict) else "zh"
    timezone_name = settings.get("timezone", "UTC") if isinstance(settings, dict) else "UTC"
    for target in store.subscription_targets(channel.name, language, timezone_name):
        if target.key == key:
            return target
    return None


def _beat(heartbeat: Any) -> None:
    if heartbeat is not None:
        heartbeat()


def _delivery_report(channel: str, target: str, status: str, detail: str) -> dict[str, str]:
    return {"channel": channel, "target": target, "status": status, "detail": detail}


def system_clock():
    class _Clock:
        def now(self):
            return utc_now()

    return _Clock()
