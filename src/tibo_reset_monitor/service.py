"""Composition root: config, source, store, and channels."""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tibo_reset_monitor.channels import build_channels
from tibo_reset_monitor.config import AppConfig
from tibo_reset_monitor.core.http import UrllibTransport
from tibo_reset_monitor.core.model import MANUAL, CycleReport, Notice, Target, format_utc
from tibo_reset_monitor.core.scheduler import deliver_due, run_cycle
from tibo_reset_monitor.core.source import SourceClient
from tibo_reset_monitor.core.store import StateStore

log = logging.getLogger("tibo_reset_monitor")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class Runtime:
    config: AppConfig
    store: StateStore
    source: SourceClient
    channels: list[Any]
    clock: Any
    owner: str

    def close(self) -> None:
        self.store.close()


def build_runtime(config: AppConfig, *, transport: Any = None, clock: Any = None, owner: str | None = None) -> Runtime:
    clock = clock or SystemClock()
    transport = transport or UrllibTransport()
    store = StateStore(config.state_path)
    source = SourceClient(
        config.source_url,
        transport=transport,
        user_agent=config.user_agent,
        timeout=config.timeout_seconds,
    )
    channels = build_channels(config, transport, clock)
    for channel in channels:
        if hasattr(channel, "_timeout"):
            channel._timeout = config.timeout_seconds
    identity = owner or f"{os.getpid()}@{socket.gethostname()}"
    return Runtime(config=config, store=store, source=source, channels=channels, clock=clock, owner=identity)


def poll(runtime: Runtime) -> int:
    now = runtime.clock.now()
    if not runtime.store.acquire_lock(runtime.owner, now, runtime.config.stale_lock_seconds):
        log.warning("another monitor process is running; skipping this poll")
        return 0
    try:
        report = run_cycle(
            store=runtime.store,
            source=runtime.source,
            channels=runtime.channels,
            clock=runtime.clock,
            heartbeat=lambda: runtime.store.acquire_lock(runtime.owner, runtime.clock.now(), runtime.config.stale_lock_seconds),
        )
        log_report(report)
        return 1 if report.fetch_error else 0
    finally:
        runtime.store.release_lock(runtime.owner)


def run_forever(runtime: Runtime, sleep: Any = time.sleep) -> int:
    now = runtime.clock.now()
    if not runtime.store.acquire_lock(runtime.owner, now, runtime.config.stale_lock_seconds):
        log.error("another monitor process is running")
        return 1
    try:
        while True:
            runtime.store.acquire_lock(runtime.owner, runtime.clock.now(), runtime.config.stale_lock_seconds)
            report = run_cycle(
                store=runtime.store,
                source=runtime.source,
                channels=runtime.channels,
                clock=runtime.clock,
                heartbeat=lambda: runtime.store.acquire_lock(
                    runtime.owner, runtime.clock.now(), runtime.config.stale_lock_seconds
                ),
            )
            log_report(report)
            delay = float(runtime.config.poll_interval_seconds)
            if report.retry_after_seconds:
                delay = max(delay, float(report.retry_after_seconds))
            _interruptible_sleep(runtime, delay, sleep)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        runtime.store.release_lock(runtime.owner)


def status_text(runtime: Runtime, *, as_json: bool) -> str:
    import json

    payload = runtime.store.status_dict()
    payload["channels"] = [
        {"name": channel.name, "targets": [{"name": target.key, "kind": target.kind} for target in channel.targets]}
        for channel in runtime.channels
    ]
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines = [
        f"baseline: {payload['baseline_completed_at'] or 'not yet'}",
        f"last fetch: {payload['last_fetch_status'] or 'none'} at {payload['last_fetch_at'] or '-'}",
        f"last error: {payload['last_fetch_error'] or 'none'}",
        f"last success: {payload['last_success_at'] or '-'}",
    ]
    snapshot = payload["snapshot"]
    if snapshot:
        lines.append(
            "snapshot: "
            f"checkedAt={snapshot['checked_at']} events={snapshot['events']} "
            f"activities={snapshot['activities']} etag={snapshot['etag']}"
        )
    else:
        lines.append("snapshot: none")
    counts = payload["outbox"] or {}
    if counts:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    else:
        rendered = "empty"
    lines.append(f"outbox: {rendered}")
    for channel in payload["channels"]:
        kinds = ", ".join(f"{item['name']} ({item['kind']})" for item in channel["targets"]) or "no targets"
        lines.append(f"channel {channel['name']}: {kinds}")
    return "\n".join(lines)


def list_text(runtime: Runtime, *, limit: int, as_json: bool) -> str:
    import json

    rows = runtime.store.list_notices(limit)
    if as_json:
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if not rows:
        return "no notices yet"
    lines = []
    for row in rows:
        lines.append(
            f"#{row['id']} {row['detected_at']} {row['stage_label'] or row['stage']} "
            f"post={row['post_key']} event={row['source_event_id'] or '-'}"
        )
        if row.get("title"):
            lines.append(f"    {row['title']}")
        for delivery in row["deliveries"]:
            detail = delivery.get("last_error") or delivery.get("receipt_message_id") or ""
            suffix = f" — {detail}" if detail else ""
            lines.append(
                f"    {delivery['channel']}/{delivery['target_key']} ({delivery['target_kind']}) "
                f"{delivery['status']}{suffix}"
            )
    return "\n".join(lines)


def send_test(runtime: Runtime, channel_name: str, target_name: str) -> int:
    channel, target = _resolve(runtime, channel_name, target_name)
    notice = _synthetic(
        title="测试消息",
        text="这是 tibo-reset-monitor 的测试消息，不代表额度重置。",
        stage_label="测试",
    )
    return _deliver_manual(runtime, channel, target, notice, salt=f"test:{uuid.uuid4().hex}")


def manual_send(
    runtime: Runtime,
    *,
    channel_name: str,
    target_name: str,
    event_id: int | None,
    text: str | None,
    preview: bool,
) -> int:
    channel, target = _resolve(runtime, channel_name, target_name)
    if event_id is not None:
        notice = runtime.store.get_notice(event_id)
        if notice is None:
            raise KeyError(f"no notice #{event_id}")
    else:
        notice = _synthetic(title="手动消息", text=text or "", stage_label="手动")
    if preview:
        rendered = channel.render(notice, target)
        print(_preview(rendered))
        return 0
    if event_id is not None:
        now = runtime.clock.now()
        row = runtime.store.enqueue_existing(notice, target, now, salt=f"manual:{uuid.uuid4().hex}")
        runtime.store.conn.commit()
        deliver_due(runtime.store, runtime.channels, runtime.clock)
        updated = runtime.store.read_row(row.id)
        print(f"{updated.state.status}: {updated.state.receipt_detail or updated.state.last_error or ''}")
        return 0 if updated.state.status == "sent" else 1
    return _deliver_manual(runtime, channel, target, notice, salt=f"manual:{uuid.uuid4().hex}")


def log_report(report: CycleReport) -> None:
    if report.baselined is not None:
        log.info("poll baseline=%s", report.baselined)
        return
    log.info(
        "poll new=%s cancelled=%s not_modified=%s deliveries=%s fetch_error=%s",
        report.new_notices,
        report.cancelled,
        report.not_modified,
        len(report.deliveries),
        report.fetch_error_kind or "",
    )


def _deliver_manual(runtime: Runtime, channel: Any, target: Target, notice: Notice, *, salt: str) -> int:
    now = runtime.clock.now()
    row = runtime.store.insert_manual(notice, target, now, salt=salt)
    runtime.store.conn.commit()
    deliver_due(runtime.store, runtime.channels, runtime.clock)
    updated = runtime.store.read_row(row.id)
    detail = updated.state.receipt_detail or updated.state.last_error or ""
    print(f"{updated.state.status}: {detail}")
    return 0 if updated.state.status == "sent" else 1


def _synthetic(*, title: str, text: str, stage_label: str) -> Notice:
    stamp = format_utc(datetime.now(timezone.utc))
    post_key = f"manual:{uuid.uuid4().hex}"
    return Notice(
        dedupe_key=f"{post_key}\x1f{MANUAL}",
        post_key=post_key,
        stage=MANUAL,
        stage_label=stage_label,
        title=title,
        display_label="tibo-reset-monitor",
        author="tibo-reset-monitor",
        text=text,
        original_text="",
        url="https://aihot.news/codex-reset",
        published_at=stamp,
    )


def _resolve(runtime: Runtime, channel_name: str, target_name: str) -> tuple[Any, Target]:
    for channel in runtime.channels:
        if channel.name != channel_name:
            continue
        for target in channel.targets:
            if target.key == target_name:
                return channel, target
        known = ", ".join(target.key for target in channel.targets) or "(none)"
        raise KeyError(f"target {target_name!r} is not configured on {channel_name}; known targets: {known}")
    names = ", ".join(channel.name for channel in runtime.channels) or "(none enabled)"
    raise KeyError(f"channel {channel_name!r} is not enabled; enabled channels: {names}")


def _preview(rendered: dict[str, Any]) -> str:
    import json

    printable = dict(rendered)
    content = printable.get("content")
    if isinstance(content, str):
        try:
            printable["content"] = json.loads(content)
        except json.JSONDecodeError:
            pass
    return json.dumps(printable, ensure_ascii=False, indent=2)


def _interruptible_sleep(runtime: Runtime, seconds: float, sleep: Any) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sleep(min(30.0, remaining))
        runtime.store.acquire_lock(runtime.owner, runtime.clock.now(), runtime.config.stale_lock_seconds)
