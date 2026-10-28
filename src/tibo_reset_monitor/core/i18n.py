"""Shared zh/en copy and the channel-neutral outbound message.

Channels render :class:`OutboundMessage`. They do not invent their own stage
names, tags, or calendar captions. ``zh`` is the default and matches the
wording the Chinese channels already send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from urllib.parse import urlparse

from tibo_reset_monitor.core.model import CONFIRMATION, MANUAL, Notice, notice_lines
from tibo_reset_monitor.core.timeutil import format_local, zone_for

LANGUAGES = frozenset({"zh", "en"})
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}

_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_TAG_EN = {
    "重置（形式未明确）": "Reset (form unspecified, defaults to quota reset)",
    "重置（形式未明确，默认额度重置）": "Reset (form unspecified, defaults to quota reset)",
    "重置（额度重置）": "Quota reset",
    "额度重置": "Quota reset",
    "发重置卡": "Reset card",
    "重置卡": "Reset card",
    "补充确认": "Amendment",
}

_STAGE_EN = {
    "announcement": "Preview",
    "confirmation": "Confirmed",
    "revision": "Amendment",
    "retraction": "Withdrawn",
    "manual": "Manual",
    "预告": "Preview",
    "公告": "Preview",
    "确认": "Confirmed",
    "确认完成": "Confirmed",
    "补充": "Amendment",
    "补充说明": "Amendment",
    "修订": "Amendment",
    "更正": "Amendment",
    "撤回": "Withdrawn",
    "撤销": "Withdrawn",
    "手动": "Manual",
    "测试": "Test",
}

_CELL_EN = {"额度重置": "Quota reset", "发重置卡": "Reset card", "补充确认": "Amendment"}

_SCOPE_FRAGMENTS = (
    ("帖子未明确适用范围", "Scope not specified in the post"),
    ("未明确适用范围", "Scope not specified in the post"),
    ("适用范围未明确", "Scope not specified in the post"),
    ("全员", "all accounts"),
    ("部分用户", "some accounts"),
    ("订阅用户", "subscribers"),
    ("用户", "accounts"),
    ("、", ", "),
    ("，", ", "),
    ("和", " and "),
    ("与", " and "),
)

FOOTER_EN = (
    "Data from the public AIHOT feed. The confirmation time is when the source post "
    "was published, not when the quota arrives in the account."
)
_UNSPECIFIED_ZH = "重置（形式未明确，默认额度重置）"
_UNSPECIFIED_EN = "Reset (form unspecified, defaults to quota reset)"
_SCOPE_MISSING_ZH = "帖子未明确适用范围"
_SCOPE_MISSING_EN = "Scope not specified in the post"


@dataclass(frozen=True)
class OutboundMessage:
    """One notice rendered for one language. Raw ids and timestamps stay on the notice."""

    language: str
    title: str
    subtitle: str
    tags: tuple[str, ...]
    text: str
    scope: str
    detail_lines: tuple[str, ...]
    link_label: str
    link_url: str
    posted_line: str
    calendar_line: str
    quote_line: str
    preview_lines: tuple[str, ...]
    card_body: str
    author_name: str
    author_caption: str
    manual_heading: str
    manual_note: str
    stage: str
    post_key: str
    published_at: str
    url: str
    author: str
    source_event_id: str | None


def normalize_language(value: str | None, default: str = "zh") -> str:
    raw = (value or "").strip().lower()
    if raw in LANGUAGES:
        return raw
    fallback = (default or "zh").strip().lower()
    return fallback if fallback in LANGUAGES else "zh"


def compose(
    notice: Notice,
    language: str = "zh",
    year_month: tuple[int, int] | None = None,
    *,
    body: str | None = None,
    preview: str | None = None,
    quote_bodies: tuple[str, ...] | None = None,
    timezone_name: str | None = None,
) -> OutboundMessage:
    """Fill every user-facing string for ``language``.

    ``body``, ``preview``, and ``quote_bodies`` override the source text. Telegram
    passes already-shortened strings so a caption can stay under the API limit.
    """
    language = "en" if language == "en" else "zh"
    zone = zone_for(timezone_name)
    author_name, author_caption = _author_lines(notice)
    text = _body(notice, language) if body is None else body
    scope = "" if notice.stage == MANUAL else _scope(notice, language)
    products = "" if notice.stage == MANUAL else _products(notice, language)
    posted = "" if notice.stage == MANUAL else _posted(notice, language, zone)
    details = tuple(line for line in (scope, products, posted) if line)
    link_url = notice.url if notice.url.startswith(("https://", "http://")) else ""
    calendar_line = calendar_caption(year_month, language, zone)
    quote_line, preview_lines = _context_copy(notice, language, preview, quote_bodies, zone)
    manual_heading = "Manual reminder" if language == "en" else "手动提醒"
    manual_note = "Sent manually" if language == "en" else "手动发送"
    if notice.stage == MANUAL:
        tags = (display_stage(notice, language),)
        title = manual_heading if language == "en" else notice.headline()
    else:
        tags = (display_tag(notice, language), display_stage(notice, language))
        title = f"{tags[1]} · {tags[0]}" if language == "en" else notice.headline()
    message = OutboundMessage(
        language=language,
        title=title,
        subtitle=_subtitle(notice, language, zone),
        tags=tags,
        text=text,
        scope=scope,
        detail_lines=details,
        link_label="View original post" if language == "en" else "查看原帖",
        link_url=link_url,
        posted_line=posted,
        calendar_line=calendar_line,
        quote_line=quote_line,
        preview_lines=preview_lines,
        card_body="",
        author_name=author_name,
        author_caption=author_caption,
        manual_heading=manual_heading,
        manual_note=manual_note,
        stage=notice.stage,
        post_key=notice.post_key,
        published_at=notice.published_at,
        url=notice.url,
        author=notice.author,
        source_event_id=notice.source_event_id,
    )
    return OutboundMessage(
        **{**message.__dict__, "card_body": _card_body(notice, message)}
    )


def display_tag(notice: Notice, language: str) -> str:
    tag = notice.tag_label or "重置（形式未明确）"
    if language != "en":
        if tag == "重置（形式未明确）":
            return _UNSPECIFIED_ZH
        return tag
    mapped = _TAG_EN.get(tag)
    if mapped:
        return mapped
    if tag.startswith("重置（") and tag.endswith("）"):
        inner = tag[3:-1]
        if inner in _TAG_EN:
            inner_en = _TAG_EN[inner]
            if inner_en.startswith("Reset ("):
                return inner_en
            return f"Reset ({inner_en})"
        inner_en = _replace_fragments(inner)
        if not _CJK_RE.search(inner_en):
            return f"Reset ({inner_en})"
    if not _CJK_RE.search(tag):
        return tag
    translated = _replace_fragments(tag)
    if not _CJK_RE.search(translated):
        return translated
    return _UNSPECIFIED_EN


def display_stage(notice: Notice, language: str) -> str:
    label = notice.stage_label or notice.stage
    if language != "en":
        return label
    if label in _STAGE_EN:
        return _STAGE_EN[label]
    if notice.stage in _STAGE_EN:
        return _STAGE_EN[notice.stage]
    if not _CJK_RE.search(label):
        return label
    return _STAGE_EN.get(notice.stage, "Preview")


def month_title(year: int, month: int, language: str = "zh") -> str:
    if language == "en":
        return f"{_MONTHS_EN[month - 1]} {year}"
    return f"{year} 年 {month} 月"


def calendar_caption(year_month: tuple[int, int] | None, language: str = "zh", zone: tzinfo | None = None) -> str:
    if year_month is None:
        local = datetime.now(timezone.utc).astimezone(zone or timezone.utc)
        year_month = (local.year, local.month)
    year, month = year_month
    title = month_title(year, month, "en" if language == "en" else "zh")
    if language == "en":
        return f"Reset calendar · {title}"
    return f"重置日历 · {title}"


def weekdays(language: str) -> tuple[str, ...]:
    if language == "en":
        return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return tuple("一二三四五六日")


def cell_label(label: str, language: str) -> str:
    if language != "en":
        return label
    return _CELL_EN.get(label, label if not _CJK_RE.search(label) else "Quota reset")


def totals_summary(totals: dict[str, int], language: str) -> str:
    if language == "en":
        summary = f'Quota reset {totals.get("额度重置", 0)} · Reset card {totals.get("发重置卡", 0)}'
        if totals.get("补充确认"):
            summary += f' · Amendment {totals["补充确认"]}'
        return summary
    summary = f'额度重置 {totals.get("额度重置", 0)} · 发卡 {totals.get("发重置卡", 0)}'
    if totals.get("补充确认"):
        summary += f' · 补充确认 {totals["补充确认"]}'
    return summary


def legend_labels(language: str) -> tuple[str, str, str]:
    if language == "en":
        return ("Confirmed", "Expected in effect", "Awaiting confirmation")
    return ("已确认", "预计已生效", "等待确认")


def _card_body(notice: Notice, message: OutboundMessage) -> str:
    if message.language != "en":
        return "\n\n".join(notice_lines(notice))
    paragraphs: list[str] = []
    if message.stage == MANUAL:
        if message.text:
            paragraphs.append(message.text)
        paragraphs.append(message.manual_note)
    else:
        if message.text:
            paragraphs.append(message.text)
        paragraphs.extend(message.detail_lines)
        if message.quote_line:
            paragraphs.append(message.quote_line)
        status = _english_status(notice)
        if status:
            paragraphs.append(status)
        if notice.confirmed_at and not _CJK_RE.search(notice.confirmed_at):
            paragraphs.append(f"Source confirmation time: {notice.confirmed_at}")
        paragraphs.append(FOOTER_EN)
    paragraphs.append(message.calendar_line)
    return "\n\n".join(paragraphs)


def _body(notice: Notice, language: str) -> str:
    if language == "en" and notice.stage != MANUAL:
        return notice.original_text or ""
    return notice.text or ""


def _scope(notice: Notice, language: str) -> str:
    if notice.type_name == "activity":
        return ""
    raw = notice.audience_zh or notice.qualified_scope or ""
    if language != "en":
        return raw or _SCOPE_MISSING_ZH
    if not raw:
        return _SCOPE_MISSING_EN
    if raw in {_SCOPE_MISSING_ZH, "未明确适用范围"}:
        return _SCOPE_MISSING_EN
    converted = _replace_fragments(raw)
    if _CJK_RE.search(converted):
        converted = _CJK_RE.sub("", converted)
        converted = re.sub(r"\s+", " ", converted).strip(" ,;/")
    return converted or _SCOPE_MISSING_EN


def _products(notice: Notice, language: str) -> str:
    raw = notice.products_zh or ""
    if not raw or language != "en":
        return raw
    converted = _replace_fragments(raw)
    if _CJK_RE.search(converted):
        converted = _CJK_RE.sub("", converted)
        converted = re.sub(r"\s+", " ", converted).strip(" ,;/")
    return converted


def _posted(notice: Notice, language: str, zone: tzinfo) -> str:
    stamp = format_local(notice.published_at, zone)
    if not stamp:
        return ""
    if language == "en":
        return f"Posted · {stamp}"
    return f"帖子发布 · {stamp}"


def _subtitle(notice: Notice, language: str, zone: tzinfo) -> str:
    del language
    author = f"@{notice.author}" if notice.author else "@thsottiaux"
    stamp = format_local(notice.published_at, zone)
    if stamp:
        return f"{author} · {stamp}"
    if notice.published_at:
        return f"{author} · {notice.published_at}"
    return author


def _english_status(notice: Notice) -> str:
    if notice.presentation_status == "expired_unconfirmed":
        return "Status: the estimated window just ended and the source post is not confirmed yet."
    if notice.presentation_status == "likely_completed":
        return (
            "Status: the estimated window ended a while ago. Similar previews usually land, "
            "but the source post is not confirmed yet."
        )
    return ""


def _context_copy(
    notice: Notice,
    language: str,
    preview: str | None,
    quote_bodies: tuple[str, ...] | None,
    zone: tzinfo,
) -> tuple[str, tuple[str, ...]]:
    if notice.stage == MANUAL:
        return "", ()
    if language == "en":
        contexts = notice.contexts[:1]
        if quote_bodies is None:
            quote_bodies = tuple((item.get("original_text") or "") for item in contexts)
        if not contexts:
            return "", ()
        return _quote_line(contexts[0], quote_bodies[0] if quote_bodies else ""), ()
    if preview is None:
        preview = notice.original_text or ""
    if quote_bodies is None:
        quote_bodies = tuple(_context_body(item) for item in notice.contexts)
    return "", _preview_lines(notice, preview, quote_bodies, zone)


def _quote_line(item: dict[str, str], body: str) -> str:
    """One line of quote context. English messages do not include the original-post card."""
    author = (item.get("author") or "").lstrip("@")
    who = f"@{author}" if author else ""
    relation = item.get("relation") or ""
    if relation == "reply":
        prefix = f"Replying to {who}".rstrip()
    else:
        prefix = f"Quoting {who}".rstrip() if who else "Quoting"
    if body:
        return f"{prefix}: {body}"
    if not who:
        return ""
    return prefix


def _preview_lines(notice: Notice, preview: str, quote_bodies: tuple[str, ...], zone: tzinfo) -> tuple[str, ...]:
    contexts = notice.contexts
    if not preview and not any(quote_bodies):
        return ()
    name, _caption = _author_lines(notice)
    _host, handle = _identity(notice.url)
    if not handle:
        handle = (notice.author or "").strip().lstrip("@")
    header = f"原帖 · {name}"
    if handle:
        header += f" @{handle}"
    lines = [header]
    if preview:
        lines.append(preview)
    for item, body in zip(contexts, quote_bodies):
        relation = item.get("relation") or ""
        author = item.get("author") or ""
        if relation == "quote":
            lines.append(f"引用 @{author}" if author else "引用")
        elif relation == "reply":
            lines.append(f"回复 @{author}" if author else "回复")
        elif author:
            lines.append(f"@{author}")
        if body:
            lines.append(body)
    stamp = format_local(notice.published_at, zone)
    if stamp:
        lines.append(stamp)
    return tuple(lines)


def _context_body(item: dict[str, str]) -> str:
    return item.get("original_text") or item.get("text") or ""


def _author_lines(notice: Notice) -> tuple[str, str]:
    host, handle = _identity(notice.url)
    if host in _X_HOSTS and handle:
        if handle.lower() == "thsottiaux":
            return "Tibo", "@thsottiaux · X"
        return f"@{handle}", "X"
    if host:
        return host, host
    author = (notice.author or "").strip()
    if author.lower() == "thsottiaux":
        return "Tibo", "@thsottiaux · X"
    if author:
        return f"@{author.lstrip('@')}", "X"
    return "Tibo", "@thsottiaux · X"


def _identity(url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    handle = ""
    if host in _X_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() != "i":
            handle = parts[0]
    return host, handle


def _replace_fragments(text: str) -> str:
    for source, target in _SCOPE_FRAGMENTS:
        text = text.replace(source, target)
    return re.sub(r"\s+", " ", text).strip()
