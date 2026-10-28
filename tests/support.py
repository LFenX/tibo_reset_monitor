"""Shared fixtures for monitor tests. Snapshots follow the AIHOT CodexResetsResponse shape."""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tibo_reset_monitor.core.model import FetchResult, Receipt, Target
from tibo_reset_monitor.core.store import StateStore

logging.getLogger("tibo_reset_monitor").setLevel(logging.CRITICAL)


def utc(year=2026, month=10, day=28, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now_value = now or utc()

    def now(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)


class ScriptedSource:
    def __init__(self, results: list[FetchResult]) -> None:
        self.results = list(results)
        self.etags: list[str | None] = []

    def fetch(self, etag: str | None) -> FetchResult:
        self.etags.append(etag)
        if not self.results:
            raise AssertionError("source script exhausted")
        return self.results.pop(0)


class ScriptedChannel:
    def __init__(self, name: str, targets: list[Target], send) -> None:
        self.name = name
        self.targets = list(targets)
        self._send = send
        self.sent: list[dict] = []

    def render(self, notice, target: Target) -> dict:
        return {
            "text": notice.text,
            "stage": notice.stage,
            "post_key": notice.post_key,
            "source_event_id": notice.source_event_id,
        }

    def send(self, message: dict, target: Target, idempotency_key: str) -> Receipt:
        self.sent.append(
            {
                "target": target.key,
                "kind": target.kind,
                "key": idempotency_key,
                "message": message,
            }
        )
        return self._send(message, target, idempotency_key)


class TempStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(self.path("state.sqlite3"))
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        logging.getLogger("tibo_reset_monitor").setLevel(logging.CRITICAL)

    def path(self, name: str) -> str:
        return f"{self._tmp.name}/{name}"


def target(channel: str, key: str, kind: str = "group", address: str = "addr") -> Target:
    return Target(channel=channel, key=key, kind=kind, address=address)


def ok(body: dict, etag: str = 'W/"v1"') -> FetchResult:
    return FetchResult(body=body, etag=etag, checked_at=body.get("checkedAt"))


def not_modified(etag: str = 'W/"v1"') -> FetchResult:
    return FetchResult(not_modified=True, etag=etag)


def failure(kind: str, message: str, retry_after: float | None = None) -> FetchResult:
    from tibo_reset_monitor.core.model import SourceFailure

    return FetchResult(failure=SourceFailure(kind, message, retry_after_seconds=retry_after))


def snapshot(events: list[dict], activities: list[dict] | None = None, checked_at: str = "2026-10-28T12:00:00.000+08:00") -> dict:
    return {
        "schemaVersion": 1,
        "timezone": "Asia/Shanghai",
        "today": "2026-10-28",
        "checkedAt": checked_at,
        "historyFrom": "2026-10-21T00:00:00.000+08:00",
        "count": len(events),
        "events": events,
        "activities": activities or [],
        "monitor": {
            "status": "healthy",
            "lastAttemptAt": checked_at,
            "lastCollectedAt": checked_at,
            "lastVerifiedAt": checked_at,
            "heldWindowCount": 0,
            "pendingCount": 0,
            "reviewCount": 0,
        },
        "outage": None,
    }


def make_post(post_id: str, stage: str, text: str = "正文", original: str = "body") -> dict:
    return {
        "id": post_id,
        "publishedAt": "2026-10-28T10:00:00.000+08:00",
        "stage": stage,
        "text": text,
        "originalText": original,
        "fullText": text,
        "fullOriginalText": original,
        "context": [],
        "url": f"https://x.com/thsottiaux/status/{post_id}",
    }


def make_event(event_id: str, posts: list[dict], title: str = "Tibo 预告将重置额度", status: str = "announced") -> dict:
    return {
        "id": event_id,
        "type": "direct_reset",
        "label": "全员重置",
        "displayLabel": "重置（形式未明确）",
        "presentation": {
            "status": "announced" if status == "announced" else "confirmed",
            "scopeKnown": False,
            "scopeLabel": None,
            "kindExplicit": False,
            "timeInferred": False,
            "audienceZh": None,
            "productsZh": None,
        },
        "estimate": {
            "from": "2026-10-28T10:00:00.000+08:00",
            "through": "2026-10-28T16:00:00.000+08:00",
            "basis": "source",
            "label": "北京时间 10月28日 10:00–16:00",
            "reason": "按原帖给出的截止时间换算成北京时间，并预留一点延迟。",
        },
        "status": status,
        "title": title,
        "scope": "",
        "createdAt": "2026-10-28T10:00:00.000+08:00",
        "updatedAt": "2026-10-28T10:00:00.000+08:00",
        "confirmedAt": None if status != "confirmed" else "2026-10-28T11:00:00.000+08:00",
        "occurredOn": None,
        "confirmationBasis": None if status != "confirmed" else "source_post",
        "schedule": {
            "precision": "deadline",
            "from": "2026-10-28T15:00:00.000+08:00",
            "through": "2026-10-28T15:00:00.000+08:00",
            "label": "北京时间预计 10月28日 15:00 前",
        },
        "posts": posts,
        "url": "https://aihot.news/codex-reset",
    }


def make_activity(post_id: str, action: str | None, event_id: str | None, text: str = "正文", original: str = "body", kind: str = "event_update") -> dict:
    return {
        "id": post_id,
        "publishedAt": "2026-10-28T10:00:00.000+08:00",
        "eventIds": [event_id] if event_id else [],
        "kind": kind,
        "text": text,
        "originalText": original,
        "context": [],
        "statusChanged": action is not None,
        "action": action,
        "url": f"https://x.com/thsottiaux/status/{post_id}",
    }


def sent(message_id: str = "m1") -> Receipt:
    return Receipt("sent", provider_message_id=message_id, detail="sent")
