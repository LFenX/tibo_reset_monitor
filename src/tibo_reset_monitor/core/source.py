"""Client for the AIHOT public Codex-reset feed.

The response shape follows CodexResetsResponse in https://aihot.news/openapi-v1.json.
A failed, rate-limited, or malformed response is returned as a failure and must not
be stored over the last valid snapshot.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from tibo_reset_monitor.core.http import TransportError, UrllibTransport
from tibo_reset_monitor.core.model import FetchResult, SourceFailure

DEFAULT_SOURCE_URL = "https://aihot.news/api/v1/codex-resets/recent"
REQUIRED_FIELDS = ("schemaVersion", "timezone", "checkedAt", "historyFrom", "count", "events")


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
    if seconds < 0:
        return None
    return min(seconds, cap)


def validate_snapshot(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise SnapshotError("response JSON must be an object")
    missing = [name for name in REQUIRED_FIELDS if name not in body]
    if missing:
        raise SnapshotError("response is missing " + ", ".join(missing))
    if body.get("schemaVersion") != 1:
        raise SnapshotError(f"unsupported schemaVersion {body.get('schemaVersion')!r}")
    if not isinstance(body.get("events"), list):
        raise SnapshotError("events must be an array")
    if not isinstance(body.get("count"), int) or isinstance(body.get("count"), bool):
        raise SnapshotError("count must be an integer")
    if not isinstance(body.get("historyFrom"), str):
        raise SnapshotError("historyFrom must be a string")
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
