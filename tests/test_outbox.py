"""Pure delivery transitions: clear retries, ambiguous review cutoff, stable keys."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tests.support import utc
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


if __name__ == "__main__":
    unittest.main()
