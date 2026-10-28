"""Neutral reset notices and delivery receipts. Channels render these; core never renders a channel."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ANNOUNCEMENT = "announcement"
CONFIRMATION = "confirmation"
REVISION = "revision"
RETRACTION = "retraction"
MANUAL = "manual"

STAGE_LABELS = {
    ANNOUNCEMENT: "预告",
    CONFIRMATION: "确认",
    REVISION: "补充",
    RETRACTION: "撤回",
    MANUAL: "手动",
}

FOOTER = "数据来自 AIHOT 公开接口。确认时间是来源帖子的发布时间，不是账户额度的到账时间。"

Outcome = Literal["sent", "retryable", "permanent", "ambiguous"]

PENDING = "pending"
INFLIGHT = "inflight"
SENT = "sent"
RETRYABLE = "retryable"
AMBIGUOUS = "ambiguous"
NEEDS_REVIEW = "needs_review"
CANCELLED = "cancelled"
PERMANENT = "permanent_failure"

CANCELLABLE_STATUSES = (
    PENDING,
    INFLIGHT,
    RETRYABLE,
    AMBIGUOUS,
    NEEDS_REVIEW,
    PERMANENT,
)
DUE_STATUSES = (PENDING, RETRYABLE, AMBIGUOUS)

# Feishu dedupes a uuid for about one hour. Stop ambiguous retries before that window ends.
AMBIGUOUS_REVIEW_AFTER_SECONDS = 55 * 60


def notice_lines(notice: "Notice") -> list[str]:
    """Neutral lines shared by channel renderers. The footer states the AIHOT time caveat."""
    lines: list[str] = []
    if notice.title and notice.title not in notice.headline():
        lines.append(notice.title)
    if notice.text:
        lines.append(notice.text)
    if notice.original_text and notice.original_text != notice.text:
        lines.append("原文")
        lines.append(notice.original_text)
    if notice.schedule_label:
        lines.append(f"原帖时间范围：{notice.schedule_label}")
    if notice.estimate_label and notice.stage != CONFIRMATION:
        lines.append(f"估计落地窗口：{notice.estimate_label}")
    if notice.scope_label:
        lines.append(f"范围：{notice.scope_label}")
    if notice.presentation_status == "expired_unconfirmed":
        lines.append("展示状态：估计窗口刚过去，来源帖尚未确认。")
    elif notice.presentation_status == "likely_completed":
        lines.append("展示状态：已过估计窗口较久，历史上类似预告多数会落地，但来源帖尚未确认。")
    if notice.confirmed_at:
        lines.append(f"来源确认帖时间：{notice.confirmed_at}")
    lines.append(FOOTER)
    return lines


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Notice:
    """One source post at one stage, or a manual/test message."""

    dedupe_key: str
    post_key: str
    stage: str
    stage_label: str
    title: str
    display_label: str
    author: str
    text: str
    original_text: str
    url: str
    published_at: str
    source_event_id: str | None = None
    type_name: str = ""
    status: str = ""
    presentation_status: str = ""
    schedule_label: str = ""
    estimate_label: str = ""
    scope_label: str = ""
    event_url: str = ""
    confirmed_at: str = ""
    id: int | None = None

    def headline(self) -> str:
        label = self.display_label or self.title or "Codex / ChatGPT 额度"
        return f"{self.stage_label} · {label}"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("id", None)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any], notice_id: int | None = None) -> Notice:
        data = dict(payload)
        data.pop("id", None)
        return cls(id=notice_id, **data)


@dataclass(frozen=True)
class Target:
    channel: str
    key: str
    kind: str
    address: str
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    outcome: Outcome
    provider_message_id: str | None = None
    detail: str = ""
    retry_after_seconds: float | None = None


@dataclass
class DeliveryState:
    status: str
    attempts: int
    next_attempt_at: datetime | None
    first_ambiguous_at: datetime | None
    last_error: str | None
    receipt_message_id: str | None
    receipt_detail: str | None


@dataclass
class OutboxRow:
    id: int
    notice_id: int
    channel: str
    target_key: str
    target_kind: str
    idempotency_key: str
    state: DeliveryState
    notice: Notice


@dataclass
class SourceFailure:
    kind: str
    message: str
    retry_after_seconds: float | None = None
    status: int | None = None


@dataclass
class FetchResult:
    not_modified: bool = False
    failure: SourceFailure | None = None
    body: dict[str, Any] | None = None
    etag: str | None = None
    checked_at: str | None = None


@dataclass
class CycleReport:
    locked: bool = False
    baselined: int | None = None
    new_notices: int = 0
    cancelled: int = 0
    not_modified: bool = False
    fetch_error: str | None = None
    fetch_error_kind: str | None = None
    retry_after_seconds: float | None = None
    deliveries: list[dict[str, str]] = field(default_factory=list)
