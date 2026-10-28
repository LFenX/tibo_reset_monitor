"""SQLite state: last good snapshot, baseline, seen posts, and the delivery outbox."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from tibo_reset_monitor.core.model import (
    CANCELLABLE_STATUSES,
    DUE_STATUSES,
    INFLIGHT,
    NEEDS_REVIEW,
    PENDING,
    RETRACTION,
    DeliveryState,
    Notice,
    OutboxRow,
    Target,
    format_utc,
    parse_utc,
)
from tibo_reset_monitor.core.outbox import idempotency_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    etag TEXT,
    body_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    post_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    baseline INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (post_key, stage)
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    post_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    source_event_id TEXT,
    detected_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    channel TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    first_ambiguous_at TEXT,
    last_error TEXT,
    receipt_message_id TEXT,
    receipt_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS process_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    etag TEXT,
    body_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def transaction(self):
        return _Transaction(self.conn)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_baselined(self) -> bool:
        return self.get_meta("baseline_completed_at") is not None

    def get_etag(self) -> str | None:
        row = self.conn.execute("SELECT etag FROM snapshot WHERE id = 1").fetchone()
        if row is None or row["etag"] is None:
            return None
        return str(row["etag"])

    def get_snapshot(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT etag, body_json, fetched_at, checked_at FROM snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "etag": row["etag"],
            "body": json.loads(row["body_json"]),
            "fetched_at": row["fetched_at"],
            "checked_at": row["checked_at"],
        }

    def save_snapshot(self, body: dict[str, Any], etag: str | None, fetched_at: datetime) -> None:
        checked_at = body.get("checkedAt")
        checked = checked_at if isinstance(checked_at, str) else None
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        self.conn.execute(
            """
            INSERT INTO snapshot(id, etag, body_json, fetched_at, checked_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                etag = excluded.etag,
                body_json = excluded.body_json,
                fetched_at = excluded.fetched_at,
                checked_at = excluded.checked_at
            """,
            (etag, payload, format_utc(fetched_at), checked),
        )

    def get_history(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT etag, body_json, fetched_at FROM history_snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "etag": row["etag"],
            "body": json.loads(row["body_json"]),
            "fetched_at": row["fetched_at"],
        }

    def get_history_etag(self) -> str | None:
        row = self.conn.execute("SELECT etag FROM history_snapshot WHERE id = 1").fetchone()
        if row is None or row["etag"] is None:
            return None
        return str(row["etag"])

    def save_history(self, body: dict[str, Any], etag: str | None, fetched_at: datetime) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        self.conn.execute(
            """
            INSERT INTO history_snapshot(id, etag, body_json, fetched_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                etag = excluded.etag,
                body_json = excluded.body_json,
                fetched_at = excluded.fetched_at
            """,
            (etag, payload, format_utc(fetched_at)),
        )
        self.set_meta("history_attempt_at", format_utc(fetched_at))

    def touch_history(self, fetched_at: datetime, etag: str | None = None) -> None:
        """Record a 304 without replacing the cached history body."""
        if etag is None:
            self.conn.execute(
                "UPDATE history_snapshot SET fetched_at = ? WHERE id = 1",
                (format_utc(fetched_at),),
            )
        else:
            self.conn.execute(
                "UPDATE history_snapshot SET fetched_at = ?, etag = ? WHERE id = 1",
                (format_utc(fetched_at), etag),
            )
        self.set_meta("history_attempt_at", format_utc(fetched_at))

    def acquire_lock(self, owner: str, now: datetime, stale_seconds: float) -> bool:
        row = self.conn.execute("SELECT owner, acquired_at FROM process_lock WHERE id = 1").fetchone()
        if row is not None and row["owner"] != owner:
            acquired = parse_utc(row["acquired_at"])
            if acquired is not None and (now - acquired).total_seconds() < stale_seconds:
                return False
        self.conn.execute(
            """
            INSERT INTO process_lock(id, owner, acquired_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, acquired_at = excluded.acquired_at
            """,
            (owner, format_utc(now)),
        )
        self.conn.commit()
        return True

    def release_lock(self, owner: str) -> None:
        self.conn.execute("DELETE FROM process_lock WHERE id = 1 AND owner = ?", (owner,))
        self.conn.commit()

    def baseline(self, notices: Iterable[Notice], body: dict[str, Any], etag: str | None, now: datetime) -> int:
        count = 0
        for notice in notices:
            self._mark_seen(notice, now, baseline=True)
            count += 1
        self.set_meta("baseline_completed_at", format_utc(now))
        self.save_snapshot(body, etag, now)
        self.set_meta("last_fetch_error", None)
        self.set_meta("last_success_at", format_utc(now))
        return count

    def ingest(
        self,
        notices: Iterable[Notice],
        targets: Iterable[Target],
        body: dict[str, Any],
        etag: str | None,
        now: datetime,
    ) -> tuple[list[Notice], int]:
        target_list = list(targets)
        created: list[Notice] = []
        for notice in notices:
            if self._already_seen(notice):
                continue
            self._mark_seen(notice, now, baseline=False)
            notice_id = self._insert_notice(notice, now)
            created.append(Notice(**{**notice.__dict__, "id": notice_id}))
        for notice in created:
            for target in target_list:
                self._enqueue(notice, target, now)
        cancelled = 0
        for notice in created:
            if notice.stage == RETRACTION:
                cancelled += self.cancel_unsent(notice, now)
        self.save_snapshot(body, etag, now)
        self.set_meta("last_fetch_error", None)
        self.set_meta("last_success_at", format_utc(now))
        return created, cancelled

    def note_fetch(self, status: str, error: str | None, now: datetime) -> None:
        self.set_meta("last_fetch_at", format_utc(now))
        self.set_meta("last_fetch_status", status)
        self.set_meta("last_fetch_error", error)

    def cancel_unsent(self, retraction: Notice, now: datetime) -> int:
        statuses = tuple(CANCELLABLE_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        if retraction.source_event_id:
            cursor = self.conn.execute(
                f"""
                UPDATE outbox
                SET status = 'cancelled',
                    next_attempt_at = NULL,
                    last_error = 'cancelled because the source event was retracted',
                    updated_at = ?
                WHERE status IN ({placeholders})
                  AND notice_id IN (
                      SELECT id FROM notices
                      WHERE stage != ?
                        AND source_event_id = ?
                  )
                """,
                (format_utc(now), *statuses, RETRACTION, retraction.source_event_id),
            )
        else:
            cursor = self.conn.execute(
                f"""
                UPDATE outbox
                SET status = 'cancelled',
                    next_attempt_at = NULL,
                    last_error = 'cancelled because the source post was retracted',
                    updated_at = ?
                WHERE status IN ({placeholders})
                  AND notice_id IN (
                      SELECT id FROM notices
                      WHERE stage != ?
                        AND post_key = ?
                  )
                """,
                (format_utc(now), *statuses, RETRACTION, retraction.post_key),
            )
        return int(cursor.rowcount)

    def recover_inflight(self, states_by_id: dict[int, DeliveryState], now: datetime) -> None:
        for row_id, state in states_by_id.items():
            self._write_state(row_id, state, now)

    def inflight_rows(self) -> list[OutboxRow]:
        rows = self.conn.execute("SELECT * FROM outbox WHERE status = ?", (INFLIGHT,)).fetchall()
        return [self._row(row) for row in rows]

    def due_rows(self, now: datetime) -> list[OutboxRow]:
        placeholders = ",".join("?" for _ in DUE_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT * FROM outbox
            WHERE status IN ({placeholders})
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id
            """,
            (*DUE_STATUSES, format_utc(now)),
        ).fetchall()
        return [self._row(row) for row in rows]

    def claim(self, row_id: int, now: datetime) -> None:
        self.conn.execute(
            """
            UPDATE outbox
            SET status = ?, attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'retryable', 'ambiguous')
            """,
            (INFLIGHT, format_utc(now), row_id),
        )

    def read_row(self, row_id: int) -> OutboxRow:
        row = self.conn.execute("SELECT * FROM outbox WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(row_id)
        return self._row(row)

    def apply_state(self, row_id: int, state: DeliveryState, now: datetime) -> None:
        self._write_state(row_id, state, now)

    def defer(self, row_id: int, when: datetime, detail: str) -> None:
        self.conn.execute(
            """
            UPDATE outbox
            SET next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (format_utc(when), detail, format_utc(when), row_id),
        )

    def mark_needs_review(self, row: OutboxRow, now: datetime) -> None:
        state = DeliveryState(
            status=NEEDS_REVIEW,
            attempts=row.state.attempts,
            next_attempt_at=None,
            first_ambiguous_at=row.state.first_ambiguous_at,
            last_error=row.state.last_error or "ambiguous delivery was not retried after 55 minutes",
            receipt_message_id=row.state.receipt_message_id,
            receipt_detail=row.state.receipt_detail,
        )
        self._write_state(row.id, state, now)

    def enqueue_existing(self, notice: Notice, target: Target, now: datetime, *, salt: str) -> OutboxRow:
        """Add another delivery for a stored notice, cancelling an unsent attempt to the same target."""
        if notice.id is None:
            raise ValueError("notice must already be stored")
        self.conn.execute(
            """
            UPDATE outbox
            SET status = 'cancelled',
                next_attempt_at = NULL,
                last_error = 'cancelled because a manual resend replaced it',
                updated_at = ?
            WHERE notice_id = ? AND channel = ? AND target_key = ?
              AND status IN ('pending', 'retryable', 'ambiguous')
            """,
            (format_utc(now), notice.id, target.channel, target.key),
        )
        self._enqueue(notice, target, now, salt=salt)
        return self._row_by_key(idempotency_key(notice.dedupe_key, target.channel, target.key, salt=salt))

    def insert_manual(self, notice: Notice, target: Target, now: datetime, *, salt: str) -> OutboxRow:
        notice_id = self._insert_notice(notice, now)
        stored = Notice(**{**notice.__dict__, "id": notice_id})
        self._enqueue(stored, target, now, salt=salt)
        return self._row_by_key(idempotency_key(notice.dedupe_key, target.channel, target.key, salt=salt))

    def _row_by_key(self, key: str) -> OutboxRow:
        row = self.conn.execute("SELECT * FROM outbox WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self._row(row)

    def get_notice(self, notice_id: int) -> Notice | None:
        row = self.conn.execute("SELECT id, payload_json FROM notices WHERE id = ?", (notice_id,)).fetchone()
        if row is None:
            return None
        return Notice.from_json(json.loads(row["payload_json"]), notice_id=int(row["id"]))

    def list_notices(self, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, post_key, stage, source_event_id, detected_at, payload_json
            FROM notices
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        results = []
        for row in rows:
            deliveries = self.conn.execute(
                """
                SELECT channel, target_key, target_kind, status, attempts, last_error,
                       receipt_message_id, idempotency_key
                FROM outbox WHERE notice_id = ? ORDER BY id
                """,
                (row["id"],),
            ).fetchall()
            payload = json.loads(row["payload_json"])
            results.append(
                {
                    "id": row["id"],
                    "post_key": row["post_key"],
                    "stage": row["stage"],
                    "stage_label": payload.get("stage_label"),
                    "title": payload.get("title"),
                    "source_event_id": row["source_event_id"],
                    "detected_at": row["detected_at"],
                    "url": payload.get("url"),
                    "deliveries": [dict(item) for item in deliveries],
                }
            )
        return results

    def outbox_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status").fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def status_dict(self) -> dict[str, Any]:
        snapshot = self.get_snapshot()
        summary = None
        if snapshot is not None:
            body = snapshot["body"]
            summary = {
                "etag": snapshot["etag"],
                "fetched_at": snapshot["fetched_at"],
                "checked_at": snapshot["checked_at"],
                "event_count": body.get("count"),
                "events": len(body.get("events") or []),
                "activities": len(body.get("activities") or []),
            }
        return {
            "baseline_completed_at": self.get_meta("baseline_completed_at"),
            "last_fetch_at": self.get_meta("last_fetch_at"),
            "last_fetch_status": self.get_meta("last_fetch_status"),
            "last_fetch_error": self.get_meta("last_fetch_error"),
            "last_success_at": self.get_meta("last_success_at"),
            "snapshot": summary,
            "outbox": self.outbox_counts(),
        }

    def _already_seen(self, notice: Notice) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE post_key = ? AND stage = ?",
            (notice.post_key, notice.stage),
        ).fetchone()
        return row is not None

    def _mark_seen(self, notice: Notice, now: datetime, *, baseline: bool) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen(post_key, stage, baseline, seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (notice.post_key, notice.stage, 1 if baseline else 0, format_utc(now)),
        )

    def _insert_notice(self, notice: Notice, now: datetime) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO notices(dedupe_key, post_key, stage, source_event_id, detected_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                notice.dedupe_key,
                notice.post_key,
                notice.stage,
                notice.source_event_id,
                format_utc(now),
                json.dumps(notice.to_json(), ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    def _enqueue(self, notice: Notice, target: Target, now: datetime, *, salt: str = "") -> None:
        if notice.id is None:
            raise ValueError("notice must be stored before it is enqueued")
        key = idempotency_key(notice.dedupe_key, target.channel, target.key, salt=salt)
        stamp = format_utc(now)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO outbox(
                notice_id, channel, target_key, target_kind, idempotency_key, status,
                attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                notice.id,
                target.channel,
                target.key,
                target.kind,
                key,
                PENDING,
                stamp,
                stamp,
                stamp,
            ),
        )

    def _write_state(self, row_id: int, state: DeliveryState, now: datetime) -> None:
        self.conn.execute(
            """
            UPDATE outbox
            SET status = ?,
                attempts = ?,
                next_attempt_at = ?,
                first_ambiguous_at = ?,
                last_error = ?,
                receipt_message_id = ?,
                receipt_detail = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                state.status,
                state.attempts,
                format_utc(state.next_attempt_at) if state.next_attempt_at else None,
                format_utc(state.first_ambiguous_at) if state.first_ambiguous_at else None,
                state.last_error,
                state.receipt_message_id,
                state.receipt_detail,
                format_utc(now),
                row_id,
            ),
        )

    def _row(self, row: sqlite3.Row) -> OutboxRow:
        payload_row = self.conn.execute(
            "SELECT payload_json FROM notices WHERE id = ?",
            (row["notice_id"],),
        ).fetchone()
        notice = Notice.from_json(json.loads(payload_row["payload_json"]), notice_id=int(row["notice_id"]))
        state = DeliveryState(
            status=row["status"],
            attempts=int(row["attempts"]),
            next_attempt_at=parse_utc(row["next_attempt_at"]),
            first_ambiguous_at=parse_utc(row["first_ambiguous_at"]),
            last_error=row["last_error"],
            receipt_message_id=row["receipt_message_id"],
            receipt_detail=row["receipt_detail"],
        )
        return OutboxRow(
            id=int(row["id"]),
            notice_id=int(row["notice_id"]),
            channel=row["channel"],
            target_key=row["target_key"],
            target_kind=row["target_kind"],
            idempotency_key=row["idempotency_key"],
            state=state,
            notice=notice,
        )


class _Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()

