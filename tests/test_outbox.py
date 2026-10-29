"""Pure delivery transitions: clear retries, ambiguous review cutoff, stable keys."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tests.support import TempStoreTest, utc
from tibo_reset_monitor.core.model import AMBIGUOUS, NEEDS_REVIEW, PERMANENT, RETRYABLE, SENT, DeliveryState, Receipt
from tibo_reset_monitor.core.outbox import ambiguous_expired, idempotency_key, on_receipt


def _state(**kwargs) -> DeliveryState:
    base = dict(
        status="inflight",
        attempts=1,
        next_attempt_at=None,
        first_ambiguous_at=None,
        last_error=None,
        receipt_message_id=None,
        receipt_detail=None,
    )
    base.update(kwargs)
    return DeliveryState(**base)


class OutboxPolicyTest(unittest.TestCase):
    def test_unknown_result_cutoff_survives_a_later_retryable_receipt(self):
        now = utc()
        unknown = on_receipt(_state(), Receipt("ambiguous", detail="unknown"), now)
        later = on_receipt(unknown, Receipt("retryable", detail="clear failure"), now + timedelta(seconds=60))
        from tibo_reset_monitor.core.outbox import ambiguous_expired
        self.assertTrue(ambiguous_expired(later, now + timedelta(seconds=55 * 60)))

    def test_idempotency_key_is_stable_and_fits_feishu(self):
        key = idempotency_key("2115\x1fannouncement", "feishu", "ops-group")
        self.assertEqual(key, idempotency_key("2115\x1fannouncement", "feishu", "ops-group"))
        self.assertNotEqual(key, idempotency_key("2115\x1fannouncement", "feishu", "ops-group", salt="manual"))
        self.assertTrue(key.startswith("trm"))
        self.assertLessEqual(len(key), 50)

    def test_clear_failure_uses_retry_after(self):
        now = utc()
        decision = on_receipt(_state(), Receipt("retryable", detail="down", retry_after_seconds=100), now)
        self.assertEqual(decision.status, RETRYABLE)
        self.assertEqual(decision.next_attempt_at, now + timedelta(seconds=100))
        self.assertEqual(decision.last_error, "down")

    def test_permanent_stops(self):
        decision = on_receipt(_state(), Receipt("permanent", detail="chat not found"), utc())
        self.assertEqual(decision.status, PERMANENT)
        self.assertIsNone(decision.next_attempt_at)

    def test_sent_keeps_receipt(self):
        decision = on_receipt(_state(), Receipt("sent", provider_message_id="om_1", detail="sent"), utc())
        self.assertEqual(decision.status, SENT)
        self.assertEqual(decision.receipt_message_id, "om_1")
        self.assertIsNone(decision.last_error)

    def test_ambiguous_becomes_needs_review_after_55_minutes(self):
        start = utc()
        first = on_receipt(_state(), Receipt("ambiguous", detail="timeout"), start)
        self.assertEqual(first.status, AMBIGUOUS)
        self.assertEqual(first.first_ambiguous_at, start)
        self.assertFalse(ambiguous_expired(first, start + timedelta(minutes=54)))
        expired = on_receipt(
            _state(attempts=2, first_ambiguous_at=start),
            Receipt("ambiguous", detail="timeout"),
            start + timedelta(minutes=56),
        )
        self.assertEqual(expired.status, NEEDS_REVIEW)
        self.assertIsNone(expired.next_attempt_at)
        self.assertTrue(ambiguous_expired(first, start + timedelta(minutes=55)))


class LegacySchemaTest(TempStoreTest):
    def test_migration_preserves_original_outbox_and_baseline(self):
        import sqlite3
        from tibo_reset_monitor.core.store import StateStore
        path = self.path("original.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE outbox(
                id INTEGER PRIMARY KEY, notice_id INTEGER NOT NULL, channel TEXT NOT NULL,
                target_key TEXT NOT NULL, target_kind TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                first_ambiguous_at TEXT, last_error TEXT, receipt_message_id TEXT, receipt_detail TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO meta VALUES ('baseline_completed_at', '2026-10-28T02:00:00Z')")
        conn.execute("INSERT INTO outbox(id,notice_id,channel,target_key,target_kind,idempotency_key,status,created_at,updated_at) VALUES (1,1,'fake','oc_virtual','group','original-virtual-key','retryable','2026-10-28T02:00:00Z','2026-10-28T02:00:00Z')")
        conn.commit()
        conn.close()
        state = StateStore(path)
        try:
            self.assertTrue(state.is_baselined())
            row = state.conn.execute("SELECT * FROM outbox WHERE id = 1").fetchone()
            self.assertEqual(row["idempotency_key"], "original-virtual-key")
            self.assertEqual(row["status"], "retryable")
            self.assertIsNone(row["automatic_after"])
            self.assertIsNone(row["target_address"])
            self.assertIsNone(row["message_json"])
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
