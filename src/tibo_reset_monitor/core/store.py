"""SQLite state: last good snapshot, baseline, seen posts, and the delivery outbox."""

from __future__ import annotations

import json
import base64
import sqlite3
import threading
from datetime import datetime, timedelta
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
from tibo_reset_monitor.core.source import automatic_after, automatic_eligible, source_time

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
    updated_at TEXT NOT NULL,
    automatic INTEGER NOT NULL DEFAULT 1,
    automatic_after TEXT,
    target_address TEXT,
    message_json TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS process_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription (
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    address TEXT NOT NULL,
    target_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT,
    permanent_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, kind, address)
);
CREATE TABLE IF NOT EXISTS known_chat (
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, chat_id)
);
CREATE TABLE IF NOT EXISTS chat_session (
    channel TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (channel, user_id)
);
CREATE TABLE IF NOT EXISTS chat_admin (
    channel TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, user_id)
);
CREATE TABLE IF NOT EXISTS inbound_seen (
    channel TEXT NOT NULL,
    event_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (channel, event_id)
);
CREATE TABLE IF NOT EXISTS history_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    etag TEXT,
    body_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guide_delivery (
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    version TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    first_ambiguous_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, recipient, version)
);
"""


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection per thread. SQLite rejects a connection used from any
        # thread but the one that opened it (check_same_thread). The poll loop
        # and the chat listener share this store, so each thread opens its own
        # connection to the same file. WAL lets them read and write together.
        self._local = threading.local()
        conn = self._open()
        conn.executescript(_SCHEMA)
        with _Transaction(conn):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
            for name, definition in (("automatic", "INTEGER NOT NULL DEFAULT 1"), ("automatic_after", "TEXT"), ("target_address", "TEXT"), ("message_json", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
            conn.execute("UPDATE outbox SET automatic = 0 WHERE notice_id IN (SELECT id FROM notices WHERE stage = 'manual')")
            session_columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_session)")}
            for name in ("chat_id", "prompt_id"):
                if name not in session_columns:
                    conn.execute(f"ALTER TABLE chat_session ADD COLUMN {name} TEXT")
        self._local.conn = conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _owner_only(self.path)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        conn = self._open()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the connection that belongs to the calling thread."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        conn.close()
        self._local.conn = None

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

    def has_history(self) -> bool:
        return self.conn.execute("SELECT 1 FROM history_snapshot WHERE id = 1").fetchone() is not None

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
        """Take the process lock. The check and the write are one transaction, so two processes cannot both win."""
        try:
            with self.transaction():
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
                return True
        except sqlite3.OperationalError:
            return False

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
        notices = list(notices)
        previous_success = self.get_meta("last_success_at")
        after = automatic_after(body, now, self.get_meta("baseline_completed_at"), self.get_meta("last_success_at"))
        created: list[Notice] = []
        for notice in notices:
            if self._already_seen(notice):
                self._reclassify(notice)
                continue
            self._mark_seen(notice, now, baseline=False)
            # Keep backfills inspectable for an explicit `manual --event` request.
            notice_id = self._insert_notice(notice, now)
            if not automatic_eligible(notice, after, now):
                continue
            created.append(Notice(**{**notice.__dict__, "id": notice_id}))
        for notice in created:
            for target in target_list:
                self._enqueue(notice, target, now, automatic_boundary=format_utc(after))
        cancelled = 0
        for notice in created:
            if notice.stage == RETRACTION:
                cancelled += self.cancel_unsent(notice, now)
        cancelled += self.reconcile_source(notices, now)
        self.save_snapshot(body, etag, now)
        self.set_meta("last_fetch_error", None)
        self.set_meta("last_success_at", format_utc(max(now, source_time(previous_success)) if previous_success else now))
        return created, cancelled

    def reconcile_source(self, notices: Iterable[Notice], now: datetime) -> int:
        """Only removal of a post, rather than its classification, cancels it."""
        identities = {notice.post_key for notice in notices}
        cancelled = 0
        for row in self.automatic_rows():
            if row.notice.post_key not in identities:
                self.conn.execute("UPDATE outbox SET status = 'cancelled', next_attempt_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                                  ("source post is no longer in the current snapshot", format_utc(now), row.id))
                cancelled += 1
        return cancelled

    def automatic_rows(self) -> list[OutboxRow]:
        placeholders = ",".join("?" for _ in CANCELLABLE_STATUSES)
        rows = self.conn.execute(f"SELECT * FROM outbox WHERE automatic = 1 AND status IN ({placeholders})", CANCELLABLE_STATUSES).fetchall()
        return [self._row(row) for row in rows]

    def automatic_valid(self, row: OutboxRow, body: dict[str, Any], now: datetime) -> bool:
        baseline = self.get_meta("baseline_completed_at")
        if not baseline:
            return False
        after = automatic_after(body, now, baseline)
        if row.automatic_after:
            after = max(after, source_time(row.automatic_after))
        elif row.queued_at is not None:
            after = max(after, row.queued_at - timedelta(hours=1))
        return automatic_eligible(row.notice, after, now)

    def duplicate_automatic(self, row: OutboxRow) -> bool:
        """Hold duplicate legacy stage rows for the same post and destination."""
        found = self.conn.execute(
            """SELECT 1 FROM outbox o JOIN notices n ON n.id = o.notice_id
               WHERE o.automatic = 1 AND n.post_key = ? AND o.channel = ? AND o.target_key = ?
                 AND o.id != ? AND (o.status = 'sent' OR (o.id < ? AND o.status != 'cancelled')) LIMIT 1""",
            (row.notice.post_key, row.channel, row.target_key, row.id, row.id),
        ).fetchone()
        return found is not None

    def _reclassify(self, notice: Notice) -> None:
        rows = self.conn.execute("SELECT id FROM notices WHERE post_key = ? ORDER BY id", (notice.post_key,)).fetchall()
        for row in rows:
            identity = int(row["id"])
            attempted = self.conn.execute("SELECT 1 FROM outbox WHERE notice_id = ? AND attempts > 0", (identity,)).fetchone()
            if attempted is None:
                self.conn.execute("UPDATE notices SET payload_json = ? WHERE id = ?", (json.dumps(notice.to_json(), ensure_ascii=False), identity))
                self.conn.execute("UPDATE outbox SET message_json = NULL WHERE notice_id = ? AND attempts = 0", (identity,))
            # Legacy databases may contain more than one classification of a post.
            conflict = self.conn.execute("SELECT id FROM notices WHERE dedupe_key = ? AND id != ?", (notice.dedupe_key, identity)).fetchone()
            if conflict is None:
                self.conn.execute("UPDATE notices SET dedupe_key = ?, stage = ?, source_event_id = ? WHERE id = ?",
                                  (notice.dedupe_key, notice.stage, notice.source_event_id, identity))

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
                WHERE automatic = 1 AND status IN ({placeholders})
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
                WHERE automatic = 1 AND status IN ({placeholders})
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

    def claim(self, row_id: int, now: datetime) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE outbox
            SET status = ?, attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'retryable', 'ambiguous')
              AND (automatic = 0 OR (SELECT value FROM meta WHERE key = 'automatic_blocked') = '0')
            """,
            (INFLIGHT, format_utc(now), row_id),
        )
        return cursor.rowcount > 0

    def save_message(self, row_id: int, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, default=_message_bytes, allow_nan=False)
        self.conn.execute("UPDATE outbox SET message_json = COALESCE(message_json, ?) WHERE id = ?", (payload, row_id))

    def read_row(self, row_id: int) -> OutboxRow:
        row = self.conn.execute("SELECT * FROM outbox WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(row_id)
        return self._row(row)

    def apply_state(self, row_id: int, state: DeliveryState, now: datetime) -> bool:
        """Record a receipt only while the row is still in flight.

        Unsubscribe can cancel the row after it was claimed. That cancellation
        must stick, or a failed send would put the message back in the queue.
        """
        return self._write_state(row_id, state, now, only_status=INFLIGHT) > 0

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
        self._enqueue(notice, target, now, salt=salt, automatic=False)
        return self._row_by_key(idempotency_key(notice.dedupe_key, target.channel, target.key, salt=salt))

    def insert_manual(self, notice: Notice, target: Target, now: datetime, *, salt: str) -> OutboxRow:
        notice_id = self._insert_notice(notice, now)
        stored = Notice(**{**notice.__dict__, "id": notice_id})
        self._enqueue(stored, target, now, salt=salt, automatic=False)
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
            "SELECT 1 FROM seen WHERE post_key = ?",
            (notice.post_key,),
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

    def _enqueue(self, notice: Notice, target: Target, now: datetime, *, salt: str = "", automatic: bool = True, automatic_boundary: str | None = None) -> None:
        if notice.id is None:
            raise ValueError("notice must be stored before it is enqueued")
        key = idempotency_key(notice.dedupe_key, target.channel, target.key, salt=salt)
        stamp = format_utc(now)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO outbox(
                notice_id, channel, target_key, target_kind, idempotency_key, status,
                attempts, next_attempt_at, created_at, updated_at, automatic, automatic_after, target_address
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
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
                int(automatic),
                automatic_boundary,
                target.address,
            ),
        )

    def _write_state(self, row_id: int, state: DeliveryState, now: datetime, *, only_status: str | None = None) -> int:
        sql = """
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
        """
        params: list[Any] = [
            state.status,
            state.attempts,
            format_utc(state.next_attempt_at) if state.next_attempt_at else None,
            format_utc(state.first_ambiguous_at) if state.first_ambiguous_at else None,
            state.last_error,
            state.receipt_message_id,
            state.receipt_detail,
            format_utc(now),
            row_id,
        ]
        if only_status is not None:
            sql += " AND status = ?"
            params.append(only_status)
        cursor = self.conn.execute(sql, params)
        return int(cursor.rowcount)

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
            automatic=bool(row["automatic"]),
            automatic_after=row["automatic_after"],
            target_address=row["target_address"],
            attempted_at=parse_utc(row["updated_at"]) if row["attempts"] else None,
            message=json.loads(row["message_json"], object_hook=_restore_message_bytes) if row["message_json"] else None,
            queued_at=parse_utc(row["created_at"]),
        )


    def claim_inbound(self, channel: str, event_id: str, now: datetime) -> bool:
        """Return False when this update or message was already handled."""
        try:
            with self.transaction():
                self.conn.execute(
                    "INSERT INTO inbound_seen(channel, event_id, seen_at) VALUES (?, ?, ?)",
                    (channel, event_id, format_utc(now)),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def is_chat_admin(self, channel: str, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM chat_admin WHERE channel = ? AND user_id = ?",
            (channel, user_id),
        ).fetchone()
        return row is not None

    def add_chat_admin(self, channel: str, user_id: str, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO chat_admin(channel, user_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(channel, user_id) DO NOTHING
            """,
            (channel, user_id, format_utc(now)),
        )
        self.conn.commit()

    def chat_admin_ids(self, channel: str) -> list[str]:
        rows = self.conn.execute("SELECT user_id FROM chat_admin WHERE channel = ? ORDER BY user_id", (channel,)).fetchall()
        return [str(row["user_id"]) for row in rows]

    def remember_chat(self, channel: str, chat_id: str, title: str, status: str, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO known_chat(channel, chat_id, title, status, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel, chat_id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (channel, chat_id, title or chat_id, status, format_utc(now)),
        )
        self.conn.commit()

    def known_chats(self, channel: str, *, status: str = "joined") -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT chat_id, title FROM known_chat WHERE channel = ? AND status = ? ORDER BY title, chat_id",
            (channel, status),
        ).fetchall()
        return [(str(row["chat_id"]), str(row["title"])) for row in rows]

    def migrate_chat(self, channel: str, old_id: str, new_id: str, now: datetime) -> None:
        """Point a group subscription and its pending deliveries at the new chat id."""
        if not old_id or not new_id or old_id == new_id:
            return
        with self.transaction():
            row = self.conn.execute(
                "SELECT title, status FROM known_chat WHERE channel = ? AND chat_id = ?",
                (channel, old_id),
            ).fetchone()
            title = str(row["title"]) if row else new_id
            self.conn.execute("DELETE FROM known_chat WHERE channel = ? AND chat_id = ?", (channel, old_id))
            self.conn.execute(
                """
                INSERT INTO known_chat(channel, chat_id, title, status, updated_at) VALUES (?, ?, ?, 'joined', ?)
                ON CONFLICT(channel, chat_id) DO UPDATE SET title = excluded.title, status = 'joined', updated_at = excluded.updated_at
                """,
                (channel, new_id, title, format_utc(now)),
            )
            sub = self.conn.execute(
                "SELECT target_key, display_name, kind, status, stop_reason, permanent_failures, created_at FROM subscription WHERE channel = ? AND address = ?",
                (channel, old_id),
            ).fetchone()
            if sub is None:
                return
            old_key = str(sub["target_key"])
            kind = str(sub["kind"])
            existing = self.conn.execute(
                "SELECT target_key, status FROM subscription WHERE channel = ? AND kind = ? AND address = ?",
                (channel, kind, new_id),
            ).fetchone()
            if existing is None:
                new_key = _subscription_key(channel, kind, new_id)
                self.conn.execute(
                    """
                    UPDATE subscription
                    SET address = ?, target_key = ?, updated_at = ?
                    WHERE channel = ? AND address = ?
                    """,
                    (new_id, new_key, format_utc(now), channel, old_id),
                )
                self.conn.execute(
                    "UPDATE outbox SET target_key = ?, updated_at = ? WHERE channel = ? AND target_key = ?",
                    (new_key, format_utc(now), channel, old_key),
                )
                return
            dest_key = str(existing["target_key"])
            if str(existing["status"]) != "active":
                self.conn.execute(
                    """
                    UPDATE subscription
                    SET status = 'active', stop_reason = NULL, permanent_failures = 0, updated_at = ?
                    WHERE channel = ? AND target_key = ?
                    """,
                    (format_utc(now), channel, dest_key),
                )
            self._move_outbox_target(channel, old_key, dest_key, now)
            self.conn.execute(
                """
                UPDATE subscription
                SET status = 'stopped', stop_reason = 'chat migrated', updated_at = ?
                WHERE channel = ? AND target_key = ?
                """,
                (format_utc(now), channel, old_key),
            )
            self._cancel_target_pending(channel, old_key, now, "chat migrated")

    def subscribe(
        self,
        *,
        channel: str,
        kind: str,
        address: str,
        display_name: str,
        now: datetime,
    ) -> tuple[str, bool]:
        """Activate a subscription. The bool is False when it was already active."""
        key = _subscription_key(channel, kind, address)
        with self.transaction():
            existing = self.conn.execute(
                "SELECT status FROM subscription WHERE channel = ? AND kind = ? AND address = ?",
                (channel, kind, address),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "active":
                return key, False
            self.conn.execute(
                """
                INSERT INTO subscription(
                    channel, kind, address, target_key, display_name, status, stop_reason,
                    permanent_failures, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', NULL, 0, ?, ?)
                ON CONFLICT(channel, kind, address) DO UPDATE SET
                    target_key = excluded.target_key,
                    display_name = excluded.display_name,
                    status = 'active',
                    stop_reason = NULL,
                    permanent_failures = 0,
                    updated_at = excluded.updated_at
                """,
                (channel, kind, address, key, display_name or address, format_utc(now), format_utc(now)),
            )
        return key, True

    def unsubscribe(self, channel: str, kind: str, address: str, now: datetime, *, reason: str) -> str | None:
        """Stop one subscription and cancel its unsent deliveries. Returns the target key."""
        with self.transaction():
            row = self.conn.execute(
                "SELECT target_key FROM subscription WHERE channel = ? AND kind = ? AND address = ? AND status = 'active'",
                (channel, kind, address),
            ).fetchone()
            if row is None:
                return None
            key = str(row["target_key"])
            self.conn.execute(
                """
                UPDATE subscription
                SET status = 'stopped', stop_reason = ?, updated_at = ?
                WHERE channel = ? AND target_key = ?
                """,
                (reason, format_utc(now), channel, key),
            )
            self._cancel_target_pending(channel, key, now, reason)
        return key

    def subscriptions(self, channel: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """
            SELECT kind, address, target_key, display_name, status, stop_reason
            FROM subscription WHERE channel = ? ORDER BY kind, display_name, address
            """,
            (channel,),
        ).fetchall()
        return [
            {
                "kind": str(row["kind"]),
                "address": str(row["address"]),
                "target_key": str(row["target_key"]),
                "display_name": str(row["display_name"]),
                "status": str(row["status"]),
                "stop_reason": str(row["stop_reason"] or ""),
            }
            for row in rows
        ]

    def subscription_targets(self, channel: str, language: str, timezone_name: str) -> list[Target]:
        rows = self.conn.execute(
            """
            SELECT kind, address, target_key, display_name
            FROM subscription WHERE channel = ? AND status = 'active'
            """,
            (channel,),
        ).fetchall()
        found: list[Target] = []
        for row in rows:
            kind = str(row["kind"])
            options = {
                "language": language,
                "timezone": timezone_name,
                "subscription": "1",
                "display_name": str(row["display_name"]),
            }
            if channel == "feishu":
                options["receive_id_type"] = "open_id" if kind == "personal" else "chat_id"
            found.append(
                Target(
                    channel=channel,
                    key=str(row["target_key"]),
                    kind=kind,
                    address=str(row["address"]),
                    options=options,
                )
            )
        return found

    def record_subscription_result(self, channel: str, target_key: str, *, ok: bool, detail: str, now: datetime) -> bool:
        """Count consecutive permanent failures. Three stops the subscription."""
        row = self.conn.execute(
            "SELECT permanent_failures, status FROM subscription WHERE channel = ? AND target_key = ?",
            (channel, target_key),
        ).fetchone()
        if row is None or str(row["status"]) != "active":
            return False
        if ok:
            self.conn.execute(
                "UPDATE subscription SET permanent_failures = 0, updated_at = ? WHERE channel = ? AND target_key = ?",
                (format_utc(now), channel, target_key),
            )
            self.conn.commit()
            return False
        failures = int(row["permanent_failures"]) + 1
        if failures < 3:
            self.conn.execute(
                "UPDATE subscription SET permanent_failures = ?, updated_at = ? WHERE channel = ? AND target_key = ?",
                (failures, format_utc(now), channel, target_key),
            )
            self.conn.commit()
            return False
        reason = detail or "permanent delivery failure"
        with self.transaction():
            self.conn.execute(
                """
                UPDATE subscription
                SET status = 'stopped', stop_reason = ?, permanent_failures = ?, updated_at = ?
                WHERE channel = ? AND target_key = ?
                """,
                (reason[:300], failures, format_utc(now), channel, target_key),
            )
            self._cancel_target_pending(channel, target_key, now, reason[:300])
        return True

    def save_session(self, channel: str, user_id: str, action: str, candidates: list[dict[str, str]], expires_at: datetime | None, *, chat_id: str = "", prompt_id: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO chat_session(channel, user_id, action, candidates_json, expires_at, chat_id, prompt_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, user_id) DO UPDATE SET
                action = excluded.action,
                candidates_json = excluded.candidates_json,
                expires_at = excluded.expires_at,
                chat_id = excluded.chat_id,
                prompt_id = excluded.prompt_id
            """,
            (channel, user_id, action, json.dumps(candidates, ensure_ascii=False), format_utc(expires_at) if expires_at else "", chat_id, prompt_id),
        )
        self.conn.commit()

    def load_session(self, channel: str, user_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT action, candidates_json, expires_at, chat_id, prompt_id FROM chat_session WHERE channel = ? AND user_id = ?",
            (channel, user_id),
        ).fetchone()
        if row is None:
            return None
        try:
            candidates = json.loads(row["candidates_json"])
        except json.JSONDecodeError:
            candidates = []
        return {"action": str(row["action"]), "candidates": candidates, "expires_at": str(row["expires_at"]), "chat_id": row["chat_id"], "prompt_id": row["prompt_id"]}

    def confirm_session(self, channel: str, user_id: str, prompt_id: str, expires_at: datetime) -> None:
        self.conn.execute("UPDATE chat_session SET expires_at = ? WHERE channel = ? AND user_id = ? AND prompt_id = ?",
                          (format_utc(expires_at), channel, user_id, prompt_id))
        self.conn.commit()

    def clear_session(self, channel: str, user_id: str, *, prompt_id: str | None = None) -> bool:
        sql = "DELETE FROM chat_session WHERE channel = ? AND user_id = ?"
        params = [channel, user_id]
        if prompt_id is not None:
            sql += " AND prompt_id = ?"
            params.append(prompt_id)
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor.rowcount > 0

    def enqueue_guide(self, channel: str, recipient: str, version: str, language: str, now: datetime) -> None:
        """Remember a guide to send. A row that already exists is left alone."""
        self.conn.execute(
            """
            INSERT INTO guide_delivery(
                channel, recipient, version, language, status, attempts,
                next_attempt_at, first_ambiguous_at, last_error, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, ?)
            ON CONFLICT(channel, recipient, version) DO NOTHING
            """,
            (channel, recipient, version, language, format_utc(now)),
        )
        self.conn.commit()

    def open_guide(self, channel: str, recipient: str, version: str, language: str, now: datetime) -> bool:
        """Make this guide due now unless this version was already sent."""
        self.enqueue_guide(channel, recipient, version, language, now)
        cursor = self.conn.execute(
            """
            UPDATE guide_delivery
            SET status = 'pending', language = ?, attempts = 0,
                next_attempt_at = NULL, last_error = NULL, updated_at = ?
            WHERE channel = ? AND recipient = ? AND version = ?
              AND status NOT IN ('sent', 'inflight')
            """,
            (language, format_utc(now), channel, recipient, version),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def guide_row(self, channel: str, recipient: str, version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM guide_delivery WHERE channel = ? AND recipient = ? AND version = ?",
            (channel, recipient, version),
        ).fetchone()

    def inflight_guides(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM guide_delivery WHERE status = 'inflight'").fetchall())

    def due_guides(self, now: datetime) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM guide_delivery
                WHERE status IN ('pending', 'retryable', 'ambiguous')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY channel, recipient
                """,
                (format_utc(now),),
            ).fetchall()
        )

    def claim_guide(self, channel: str, recipient: str, version: str, now: datetime) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE guide_delivery
            SET status = 'inflight', attempts = attempts + 1, updated_at = ?
            WHERE channel = ? AND recipient = ? AND version = ?
              AND status IN ('pending', 'retryable', 'ambiguous')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (format_utc(now), channel, recipient, version, format_utc(now)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def write_guide(self, channel: str, recipient: str, version: str, state: DeliveryState, now: datetime) -> None:
        self.conn.execute(
            """
            UPDATE guide_delivery
            SET status = ?, attempts = ?, next_attempt_at = ?, first_ambiguous_at = ?,
                last_error = ?, updated_at = ?
            WHERE channel = ? AND recipient = ? AND version = ?
            """,
            (
                state.status,
                state.attempts,
                format_utc(state.next_attempt_at) if state.next_attempt_at else None,
                format_utc(state.first_ambiguous_at) if state.first_ambiguous_at else None,
                (state.last_error or None),
                format_utc(now),
                channel,
                recipient,
                version,
            ),
        )
        self.conn.commit()

    def _move_outbox_target(self, channel: str, old_key: str, dest_key: str, now: datetime) -> None:
        """Point pending deliveries at the destination chat, skipping notices it already has."""
        held = {
            int(row["notice_id"])
            for row in self.conn.execute(
                "SELECT notice_id FROM outbox WHERE channel = ? AND target_key = ?",
                (channel, dest_key),
            )
        }
        rows = self.conn.execute(
            "SELECT id, notice_id FROM outbox WHERE channel = ? AND target_key = ?",
            (channel, old_key),
        ).fetchall()
        for row in rows:
            if int(row["notice_id"]) in held:
                continue
            self.conn.execute(
                "UPDATE outbox SET target_key = ?, updated_at = ? WHERE id = ?",
                (dest_key, format_utc(now), int(row["id"])),
            )

    def _cancel_target_pending(self, channel: str, target_key: str, now: datetime, reason: str) -> None:
        statuses = tuple(CANCELLABLE_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        self.conn.execute(
            f"""
            UPDATE outbox
            SET status = 'cancelled', next_attempt_at = NULL, last_error = ?, updated_at = ?
            WHERE channel = ? AND target_key = ? AND status IN ({placeholders})
            """,
            (reason[:300], format_utc(now), channel, target_key, *statuses),
        )


def _owner_only(path: Path) -> None:
    """Keep the state file, and its WAL sidecars, unreadable by other users."""
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if candidate.exists():
                candidate.chmod(0o600)
        except OSError:
            return


def _message_bytes(value: Any) -> dict[str, str]:
    if isinstance(value, (bytes, bytearray)):
        return {"__trm_bytes__": base64.b64encode(value).decode("ascii")}
    raise TypeError("message contains a nonserializable value")


def _restore_message_bytes(value: dict[str, Any]) -> Any:
    if set(value) == {"__trm_bytes__"}:
        return base64.b64decode(value["__trm_bytes__"], validate=True)
    return value


def _subscription_key(channel: str, kind: str, address: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{channel}|{kind}|{address}".encode("utf-8")).hexdigest()[:16]
    return f"sub-{digest}"


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

