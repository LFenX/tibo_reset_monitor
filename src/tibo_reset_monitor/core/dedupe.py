"""Turn a CodexResetsResponse into deduped notices.

A post that appears in both ``events[].posts`` and ``activities`` produces one notice
per canonical stage. Related chatter (kind ``related``, no action) is not a reset
stage and does not notify. The outage hint is not a reset fact and is ignored.
"""

from __future__ import annotations

import re
from typing import Any

from tibo_reset_monitor.core.model import (
    ANNOUNCEMENT,
    CONFIRMATION,
    RETRACTION,
    REVISION,
    STAGE_LABELS,
    Notice,
)

_STAGE_BY_TEXT = {
    "预告": ANNOUNCEMENT,
    "公告": ANNOUNCEMENT,
    "announce": ANNOUNCEMENT,
    "announcement": ANNOUNCEMENT,
    "preview": ANNOUNCEMENT,
    "确认": CONFIRMATION,
    "确认完成": CONFIRMATION,
    "confirm": CONFIRMATION,
    "confirmation": CONFIRMATION,
    "confirmed": CONFIRMATION,
    "补充": REVISION,
    "补充说明": REVISION,
    "修订": REVISION,
    "更正": REVISION,
    "amend": REVISION,
    "revision": REVISION,
    "撤回": RETRACTION,
    "撤销": RETRACTION,
    "withdraw": RETRACTION,
    "retraction": RETRACTION,
    "retracted": RETRACTION,
}

_AUTHOR = "thsottiaux"


def canonical_stage(raw: str | None) -> str:
    if raw is None:
        return "other:unknown"
    text = str(raw).strip()
    if not text:
        return "other:unknown"
    mapped = _STAGE_BY_TEXT.get(text) or _STAGE_BY_TEXT.get(text.lower())
    if mapped:
        return mapped
    slug = re.sub(r"\s+", "_", text)[:40]
    return f"other:{slug}"


def stage_label_for(stage: str, raw: str | None) -> str:
    if raw and str(raw).strip() and not str(raw).strip().isascii():
        return str(raw).strip()
    return STAGE_LABELS.get(stage, raw or stage)


def extract_notices(snapshot: dict[str, Any]) -> list[Notice]:
    """Return one notice per ``(post id, canonical stage)`` in a valid snapshot."""
    events = snapshot.get("events") or []
    events_by_id = {event["id"]: event for event in events if isinstance(event, dict) and event.get("id")}
    notices: dict[tuple[str, str], Notice] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        for post in event.get("posts") or []:
            if not isinstance(post, dict) or not post.get("id"):
                continue
            raw_stage = post.get("stage")
            if not isinstance(raw_stage, str) or not raw_stage.strip():
                continue
            stage = canonical_stage(raw_stage)
            key = (str(post["id"]), stage)
            notices[key] = _notice_from_post(event, post, stage, raw_stage if isinstance(raw_stage, str) else None)

    for activity in snapshot.get("activities") or []:
        if not isinstance(activity, dict):
            continue
        if activity.get("kind") != "event_update":
            continue
        action = activity.get("action")
        if not isinstance(action, str) or not action:
            continue
        if not activity.get("id"):
            continue
        stage = canonical_stage(action)
        key = (str(activity["id"]), stage)
        event_id = _first_event_id(activity)
        event = events_by_id.get(event_id) if event_id else None
        incoming = _notice_from_activity(activity, stage, action, event, event_id)
        existing = notices.get(key)
        notices[key] = _merge(existing, incoming) if existing else incoming

    return list(notices.values())


def _first_event_id(activity: dict[str, Any]) -> str | None:
    event_ids = activity.get("eventIds") or []
    if not isinstance(event_ids, list):
        return None
    for event_id in event_ids:
        if isinstance(event_id, str) and event_id:
            return event_id
    return None


def _notice_from_post(event: dict[str, Any], post: dict[str, Any], stage: str, raw_stage: str | None) -> Notice:
    text, original = _post_text(post)
    return _build_notice(
        post_key=str(post["id"]),
        stage=stage,
        stage_label=stage_label_for(stage, raw_stage),
        text=text,
        original_text=original,
        url=_http_url(post.get("url")),
        published_at=_as_text(post.get("publishedAt")),
        event=event,
        source_event_id=_as_text(event.get("id")) or None,
        contexts=_contexts(post.get("context")),
    )


def _notice_from_activity(
    activity: dict[str, Any],
    stage: str,
    action: str,
    event: dict[str, Any] | None,
    event_id: str | None,
) -> Notice:
    text = _as_text(activity.get("text"))
    original = _as_text(activity.get("originalText"))
    return _build_notice(
        post_key=str(activity["id"]),
        stage=stage,
        stage_label=stage_label_for(stage, action),
        text=text,
        original_text=original,
        url=_http_url(activity.get("url")),
        published_at=_as_text(activity.get("publishedAt")),
        event=event,
        source_event_id=event_id,
        contexts=_contexts(activity.get("context")),
    )


def _build_notice(
    *,
    post_key: str,
    stage: str,
    stage_label: str,
    text: str,
    original_text: str,
    url: str,
    published_at: str,
    event: dict[str, Any] | None,
    source_event_id: str | None,
    contexts: tuple[dict[str, str], ...] = (),
) -> Notice:
    presentation = event.get("presentation") if event else None
    if not isinstance(presentation, dict):
        presentation = {}
    schedule = event.get("schedule") if event else None
    estimate = event.get("estimate") if event else None
    display_label = ""
    title = ""
    type_name = ""
    status = ""
    event_url = ""
    confirmed_at = ""
    scope_label = _as_text(presentation.get("scopeLabel"))
    presentation_status = _as_text(presentation.get("status"))
    audience_zh = _as_text(presentation.get("audienceZh"))
    products_zh = _as_text(presentation.get("productsZh"))
    qualified_scope = _as_text(presentation.get("scopeLabel"))
    if event:
        display_label = _as_text(event.get("displayLabel")) or _as_text(event.get("label"))
        title = _as_text(event.get("title"))
        type_name = _as_text(event.get("type"))
        status = _as_text(event.get("status"))
        event_url = _http_url(event.get("url"))
        confirmed_at = _as_text(event.get("confirmedAt"))
        if not scope_label:
            scope_label = _as_text(event.get("scope"))
    return Notice(
        dedupe_key=f"{post_key}\x1f{stage}",
        post_key=post_key,
        stage=stage,
        stage_label=stage_label,
        title=title,
        display_label=display_label,
        author=_AUTHOR,
        text=text,
        original_text=original_text,
        url=url,
        published_at=published_at,
        source_event_id=source_event_id,
        type_name=type_name,
        status=status,
        presentation_status=presentation_status,
        schedule_label=_label_of(schedule),
        estimate_label=_label_of(estimate),
        scope_label=scope_label,
        event_url=event_url,
        confirmed_at=confirmed_at,
        audience_zh=audience_zh,
        products_zh=products_zh,
        qualified_scope=qualified_scope,
        tag_label=_event_tag(event),
        contexts=contexts,
    )


def _merge(existing: Notice, incoming: Notice) -> Notice:
    """Keep event metadata and the longer source text when two feeds describe one post."""
    return Notice(
        dedupe_key=existing.dedupe_key,
        post_key=existing.post_key,
        stage=existing.stage,
        stage_label=existing.stage_label if not existing.stage_label.isascii() else incoming.stage_label or existing.stage_label,
        title=existing.title or incoming.title,
        display_label=existing.display_label or incoming.display_label,
        author=existing.author or incoming.author,
        text=_longer(existing.text, incoming.text),
        original_text=_longer(existing.original_text, incoming.original_text),
        url=existing.url or incoming.url,
        published_at=existing.published_at or incoming.published_at,
        source_event_id=existing.source_event_id or incoming.source_event_id,
        type_name=existing.type_name or incoming.type_name,
        status=existing.status or incoming.status,
        presentation_status=existing.presentation_status or incoming.presentation_status,
        schedule_label=existing.schedule_label or incoming.schedule_label,
        estimate_label=existing.estimate_label or incoming.estimate_label,
        scope_label=existing.scope_label or incoming.scope_label,
        event_url=existing.event_url or incoming.event_url,
        confirmed_at=existing.confirmed_at or incoming.confirmed_at,
        audience_zh=existing.audience_zh or incoming.audience_zh,
        products_zh=existing.products_zh or incoming.products_zh,
        qualified_scope=existing.qualified_scope or incoming.qualified_scope,
        tag_label=existing.tag_label or incoming.tag_label,
        contexts=existing.contexts or incoming.contexts,
    )


def _event_tag(event: dict[str, Any] | None) -> str:
    """Tag text for a card: displayLabel, else 重置卡 for an explicit credit grant."""
    if event:
        display = _as_text(event.get("displayLabel"))
        if display:
            return display
        if event.get("type") == "reset_credit":
            return "重置卡"
    return "重置（形式未明确）"


def _contexts(raw: Any) -> tuple[dict[str, str], ...]:
    """Keep quote/reply context using the live CodexResetContextPost fields."""
    found: list[dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        author = _as_text(item.get("author"))
        original = _as_text(item.get("originalText"))
        text = _as_text(item.get("text"))
        if not (author or original or text):
            continue
        found.append(
            {
                "relation": _as_text(item.get("relation")),
                "author": author,
                "original_text": original,
                "text": text,
                "url": _http_url(item.get("url")),
            }
        )
    return tuple(found)


def _post_text(post: dict[str, Any]) -> tuple[str, str]:
    text = _as_text(post.get("fullText")) or _as_text(post.get("text"))
    original = _as_text(post.get("fullOriginalText")) or _as_text(post.get("originalText"))
    return text, original


def _label_of(value: Any) -> str:
    if isinstance(value, dict):
        return _as_text(value.get("label"))
    return ""


def _longer(left: str, right: str) -> str:
    if len(right) > len(left):
        return right
    return left


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return ""
