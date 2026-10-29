"""Client for the AIHOT public Codex-reset feed.

The response shape follows CodexResetsResponse in https://aihot.news/openapi-v1.json.
A failed, rate-limited, or malformed response is returned as a failure and must not
be stored over the last valid snapshot.
"""

from __future__ import annotations

import json
import re
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from tibo_reset_monitor.core.http import TransportError, UrllibTransport
from tibo_reset_monitor.core.model import FetchResult, SourceFailure

DEFAULT_SOURCE_URL = "https://aihot.news/api/v1/codex-resets/recent"
REQUIRED_FIELDS = ("schemaVersion", "timezone", "checkedAt", "historyFrom", "count", "events")
_MAX_BODY_BYTES = 2_000_000


class SnapshotError(ValueError):
    """The body was JSON but not a CodexResetsResponse this client can use."""


def parse_retry_after(value: str | None, *, cap: float = 3600) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, cap)


def source_time(value: Any, field: str = "timestamp") -> datetime:
    """Source timestamps must explicitly identify a time zone."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        raise SnapshotError(f"{field} must be a timestamp with a time zone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("missing time zone")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise SnapshotError(f"{field} is not a valid timestamp") from exc


def validate_events(events: Any, now: datetime | None = None) -> None:
    if not isinstance(events, list):
        raise SnapshotError("events must be an array")
    identities: set[str] = set()
    for event in events:
        _record(event, "event", now)
        identity = event.get("id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise SnapshotError("event ids must be nonempty and unique")
        identities.add(identity)
        occurred = event.get("occurredOn")
        if occurred is not None:
            try:
                if not isinstance(occurred, str) or date.fromisoformat(occurred).isoformat() != occurred:
                    raise ValueError("not a standard date")
            except ValueError as exc:
                raise SnapshotError("occurredOn must be a valid YYYY-MM-DD date") from exc
        for name in ("presentation", "estimate", "schedule"):
            nested = event.get(name)
            if nested is not None and not isinstance(nested, dict):
                raise SnapshotError(f"{name} must be an object or null")
            if name != "presentation" and nested:
                for bound in ("from", "through"):
                    if nested.get(bound) is not None:
                        source_time(nested[bound], f"{name}.{bound}")
                if nested.get("from") and nested.get("through") and source_time(nested["from"]) > source_time(nested["through"]):
                    raise SnapshotError(f"{name} range is reversed")
        _posts(event.get("posts"), "posts", now)


def _record(record: Any, label: str, now: datetime | None) -> None:
    if not isinstance(record, dict):
        raise SnapshotError(f"{label} must be an object")
    for name in ("publishedAt", "createdAt", "updatedAt", "confirmedAt"):
        if record.get(name) is not None:
            stamp = source_time(record[name], f"{label}.{name}")
            if now is not None and stamp > now + timedelta(seconds=300):
                raise SnapshotError(f"{label}.{name} exceeds the local clock tolerance")
    context = record.get("context")
    if context is not None:
        if not isinstance(context, list):
            raise SnapshotError(f"{label}.context must be an array or null")
        for item in context:
            _record(item, "context", now)


def _posts(posts: Any, label: str, now: datetime | None) -> None:
    if posts is None:
        return
    if not isinstance(posts, list):
        raise SnapshotError(f"{label} must be an array")
    identities: set[str] = set()
    for post in posts:
        _record(post, label, now)
        identity = post.get("id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise SnapshotError(f"{label} ids must be nonempty and unique")
        identities.add(identity)
        if label == "activities" and post.get("eventIds") is not None:
            if not isinstance(post["eventIds"], list) or any(not isinstance(item, str) for item in post["eventIds"]):
                raise SnapshotError("activity eventIds must be an array of strings")


def automatic_after(body: dict[str, Any], now: datetime, baseline: str, last_success: str | None = None) -> datetime:
    boundaries = [source_time(baseline), now - timedelta(days=7), source_time(body["historyFrom"])]
    if last_success:
        checkpoint = source_time(last_success)
        if checkpoint > now + timedelta(seconds=300):
            raise SnapshotError("local clock is behind the last successful source check")
        boundaries.append(checkpoint - timedelta(hours=1))
    if max(boundaries) > now + timedelta(seconds=300):
        raise SnapshotError("automatic time boundary exceeds the local clock tolerance")
    return max(boundaries)


def automatic_eligible(notice: Any, after: datetime, now: datetime) -> bool:
    try:
        published = source_time(notice.published_at, "publishedAt")
    except SnapshotError:
        return False
    return after <= published <= now + timedelta(seconds=300)


def validate_snapshot(body: Any, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise SnapshotError("response JSON must be an object")
    missing = [name for name in REQUIRED_FIELDS if name not in body]
    if missing:
        raise SnapshotError("response is missing " + ", ".join(missing))
    if type(body.get("schemaVersion")) is not int or body.get("schemaVersion") != 1:
        raise SnapshotError(f"unsupported schemaVersion {body.get('schemaVersion')!r}")
    if not isinstance(body.get("events"), list):
        raise SnapshotError("events must be an array")
    if not isinstance(body.get("count"), int) or isinstance(body.get("count"), bool):
        raise SnapshotError("count must be an integer")
    if body["count"] < 0:
        raise SnapshotError("count must be nonnegative")
    if not isinstance(body.get("historyFrom"), str):
        raise SnapshotError("historyFrom must be a string")
    history_from = source_time(body["historyFrom"], "historyFrom")
    if now is not None and history_from > now + timedelta(seconds=300):
        raise SnapshotError("historyFrom exceeds the local clock tolerance")
    if body.get("checkedAt") is not None:
        source_time(body["checkedAt"], "checkedAt")
    activities = body.get("activities", [])
    if activities is None:
        activities = []
    if not isinstance(activities, list):
        raise SnapshotError("activities must be an array")
    for index, event in enumerate(body["events"]):
        if not isinstance(event, dict) or not event.get("id"):
            raise SnapshotError(f"events[{index}] is missing id")
        posts = event.get("posts", [])
        if posts is None:
            posts = []
        if not isinstance(posts, list):
            raise SnapshotError(f"events[{index}].posts must be an array")
    for index, activity in enumerate(activities):
        if not isinstance(activity, dict) or not activity.get("id"):
            raise SnapshotError(f"activities[{index}] is missing id")
    validate_events(body["events"], now)
    _posts(activities, "activities", now)
    return body


class SourceClient:
    def __init__(
        self,
        url: str = DEFAULT_SOURCE_URL,
        *,
        transport: Any = None,
        user_agent: str,
        timeout: float = 20,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self.transport = transport or UrllibTransport()

    def fetch(self, etag: str | None) -> FetchResult:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
        }
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self.transport.send("GET", self.url, headers, None, self.timeout)
        except TransportError as exc:
            kind = "timeout" if exc.ambiguous else "network"
            return FetchResult(failure=SourceFailure(kind, str(exc)))

        response_etag = _etag_of(response.headers) or etag
        if len(response.body) > _MAX_BODY_BYTES:
            return FetchResult(failure=SourceFailure("malformed", "response is too large", status=response.status))
        if response.status == 304:
            return FetchResult(not_modified=True, etag=response_etag)
        if response.status in (429, 503):
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            detail = _problem_detail(response.body) or f"HTTP {response.status}"
            return FetchResult(
                failure=SourceFailure(
                    "rate_limit",
                    detail,
                    retry_after_seconds=retry_after if retry_after is not None else 60,
                    status=response.status,
                )
            )
        if response.status != 200:
            detail = _problem_detail(response.body) or f"HTTP {response.status}"
            return FetchResult(failure=SourceFailure("http", detail, status=response.status))
        try:
            payload = json.loads(response.body.decode("utf-8"))
            validate_snapshot(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, SnapshotError) as exc:
            return FetchResult(failure=SourceFailure("malformed", str(exc), status=200))
        return FetchResult(body=payload, etag=_etag_of(response.headers), checked_at=payload.get("checkedAt"))


def _etag_of(headers: Mapping[str, str]) -> str | None:
    value = headers.get("etag")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _problem_detail(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail") or payload.get("title")
    if isinstance(detail, str):
        return detail[:300]
    return ""
