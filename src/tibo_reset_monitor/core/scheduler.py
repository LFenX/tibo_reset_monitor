"""One poll cycle: fetch, baseline or enqueue, then deliver due outbox rows.

Channel objects are injected. This module does not import any channel implementation.
A fetch failure never writes a snapshot. Delivery of one target cannot roll back another.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Protocol, Sequence

from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.model import (
    INFLIGHT,
    CycleReport,
    Receipt,
    Target,
    utc_now,
)
from tibo_reset_monitor.core.outbox import ambiguous_expired, on_receipt, on_recover
from tibo_reset_monitor.core.store import StateStore

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
) -> CycleReport:
    now = clock.now()
    report = CycleReport()
    _beat(heartbeat)
    _recover(store, now)

    result = source.fetch(store.get_etag())
    if result.failure is not None:
        failure = result.failure
        report.fetch_error = failure.message
        report.fetch_error_kind = failure.kind
        report.retry_after_seconds = failure.retry_after_seconds
        store.note_fetch(failure.kind, failure.message, now)
        store.conn.commit()
        log.error("source fetch failed (%s): %s", failure.kind, failure.message)
    elif result.not_modified:
        report.not_modified = True
        store.note_fetch("not_modified", None, now)
        store.conn.commit()
        log.info("source not modified")
    else:
        try:
            notices = extract_notices(result.body)
        except (KeyError, TypeError, ValueError) as exc:
            report.fetch_error = str(exc)
            report.fetch_error_kind = "malformed"
            store.note_fetch("malformed", str(exc), now)
            store.conn.commit()
            log.error("source payload could not be read: %s", exc)
        else:
            with store.transaction():
                if not store.is_baselined():
                    count = store.baseline(notices, result.body, result.etag, now)
                    report.baselined = count
                    store.note_fetch("baseline", None, now)
                    log.info("baseline recorded %s source posts; no notifications sent", count)
                else:
                    created, cancelled = store.ingest(notices, _targets(channels), result.body, result.etag, now)
                    report.new_notices = len(created)
                    report.cancelled = cancelled
                    store.note_fetch("ok", None, now)
                    log.info("detected %s new source posts; cancelled %s unsent deliveries", len(created), cancelled)

    report.deliveries = deliver_due(store, channels, clock, heartbeat=heartbeat)
    return report


def deliver_due(
    store: StateStore,
    channels: Sequence[ChannelAdapter],
    clock: Any,
    heartbeat: Any = None,
) -> list[dict[str, str]]:
    by_name = {channel.name: channel for channel in channels}
    reports: list[dict[str, str]] = []
    for row in store.due_rows(clock.now()):
        _beat(heartbeat)
        now = clock.now()
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
        target = _find_target(channel, row.target_key) if channel else None
        if channel is None or target is None:
            when = now + timedelta(seconds=600)
            store.defer(row.id, when, "target is not in the current config")
            store.conn.commit()
            reports.append(_delivery_report(row.channel, row.target_key, "deferred", "target is not in the current config"))
            continue
        store.claim(row.id, now)
        store.conn.commit()
        claimed = store.read_row(row.id)
        if claimed.state.status != INFLIGHT:
            continue
        receipt = _send(channel, target, claimed)
        decided_at = clock.now()
        decision = on_receipt(claimed.state, receipt, decided_at)
        store.apply_state(claimed.id, decision, decided_at)
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
        message = channel.render(row.notice, target)
        return channel.send(message, target, row.idempotency_key)
    except TimeoutError as exc:
        return Receipt("ambiguous", detail=f"timeout: {exc}")
    except OSError as exc:
        ambiguous = isinstance(exc, TimeoutError)
        return Receipt("ambiguous" if ambiguous else "retryable", detail=str(exc))
    except Exception as exc:  # a channel bug must not block the rest of the outbox
        log.exception("channel %s raised while sending to %s", channel.name, target.key)
        return Receipt("retryable", detail=f"{type(exc).__name__}: {exc}")


def _recover(store: StateStore, now: Any) -> None:
    rows = store.inflight_rows()
    if not rows:
        return
    updates = {}
    for row in rows:
        updates[row.id] = on_recover(row.state, now)
    store.recover_inflight(updates, now)
    store.conn.commit()
    log.warning("recovered %s in-flight deliveries as ambiguous", len(rows))


def _targets(channels: Sequence[ChannelAdapter]) -> list[Target]:
    found: list[Target] = []
    for channel in channels:
        found.extend(channel.targets)
    return found


def _find_target(channel: ChannelAdapter | None, key: str) -> Target | None:
    if channel is None:
        return None
    for target in channel.targets:
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
