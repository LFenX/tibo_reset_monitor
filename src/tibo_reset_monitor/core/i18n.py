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


# Bump this when the guide copy changes. Each (channel, recipient, version) is sent once.
GUIDE_VERSION = "2"

_CHAT = {
    "zh": {
        "help": (
            "可用指令：\n"
            "/subscribe（#开启单聊通知）开启自己的单聊通知\n"
            "/unsubscribe（#关闭单聊通知）关闭自己的单聊通知\n"
            "/addgroup（#添加群聊）从机器人已加入的群里选择\n"
            "/removegroup（#移除群聊，也可写 #取消群聊 / #取消通知）停止一个群\n"
            "/cancel（#取消）退出当前选择\n"
            "/status（#状态）查看订阅\n"
            "在群里发送 /subscribe 可直接订阅该群。\n"
            "首次使用可在单聊发送 /pair <配对码>。"
        ),
        "denied": "你不在管理员名单里，不能管理通知。请联系管理员。发送 /help 或 #帮助 查看你能用的指令。",
        "denied_subscribe": "现在只有管理员能开单聊（allow_self_subscribe 关闭）。请联系管理员。发送 /help 或 #帮助 查看说明。",
        "denied_no_admin_telegram": "还没有管理员。请让看到启动配对码的人，在私聊发送 /pair <配对码>。发送 /help 查看说明。",
        "denied_no_admin_feishu": "还没有管理员。请在配置的 admins 里填写 open_id，或在开放平台把管理人员加为应用协作者。发送 /help 查看说明。",
        "slow": "操作太频繁，请稍后再试。",
        "dm_on": "已开启你的单聊通知。之后的新动态会发到这里。",
        "dm_on_already": "你的单聊通知已经开着。",
        "dm_off": "已关闭你的单聊通知，未发出的消息已取消。",
        "dm_off_none": "你还没有开启单聊通知。",
        "group_on": "已订阅群 {name}。之后的新动态会发到这个群。",
        "group_on_already": "这个群已经在订阅中。",
        "group_off": "已取消群 {name} 的通知，未发出的消息已取消。",
        "group_off_none": "这个群没有在订阅中。",
        "only_dm": "单聊通知要在与机器人的私聊里开启。",
        "need_admin_group": "只有管理员能在群里开关通知。请联系管理员。发送 /help 查看说明。",
        "status_title": "当前订阅：",
        "status_empty": "还没有订阅。",
        "status_line": "{kind} {name}（…{tail}）{extra}",
        "kind_personal": "单聊",
        "kind_group": "群聊",
        "stopped": "已停止：{reason}",
        "add_intro": "回复编号、完整群名来订阅。一次一个群，1 分钟内有效。",
        "remove_intro": "回复编号、完整群名来取消。一次一个群，1 分钟内有效。",
        "empty_add": "没有可添加的群。请先把机器人加进群。",
        "empty_remove": "还没有订阅任何群。",
        "choice": "{index}. {name}（…{tail}）",
        "ambiguous": "有重名的群，请回复编号。",
        "bad_choice": "没有这个选项。请按最新名单回复编号或完整群名。",
        "expired": "选择已超时，请重新发送指令。",
        "cancelled": "已退出选择。",
        "pair_ok": "你已成为管理员。",
        "pair_bad": "配对码无效或已过期。",
        "pair_limited": "尝试次数过多，请稍后再试。",
        "pair_dm": "请在与机器人的单聊里发送配对码。",
        "admin_added": "已添加管理员。",
        "admin_usage": "用法：/addadmin <用户 id>",
        "list_failed": "暂时列不出群，请稍后再试。",
        "not_group_cmd": "请在与机器人的单聊里管理群订阅。发送 /help 查看说明。",
    },
    "en": {
        "help": (
            "Commands:\n"
            "/subscribe turns on direct messages for you\n"
            "/unsubscribe turns them off\n"
            "/addgroup picks a group the bot has joined\n"
            "/removegroup stops one group\n"
            "/cancel leaves the current picker\n"
            "/status lists subscriptions\n"
            "Send /subscribe inside a group to subscribe that group.\n"
            "The first admin sends /pair <code> in a direct chat."
        ),
        "denied": "You are not on the admin list, so you cannot manage notifications. Ask an admin. Send /help for what you can do.",
        "denied_subscribe": "Only an admin can turn on direct messages right now (allow_self_subscribe is off). Ask an admin. Send /help for what you can do.",
        "denied_no_admin_telegram": "This bot has no admin yet. The person who saw the startup pairing code should send /pair <code> in a direct chat. Send /help for details.",
        "denied_no_admin_feishu": "This bot has no admin yet. Put open_ids in admins, or add the managers as app collaborators. Send /help for details.",
        "slow": "Too many requests. Try again in a moment.",
        "dm_on": "Direct messages are on. New updates will come here.",
        "dm_on_already": "Direct messages are already on.",
        "dm_off": "Direct messages are off. Unsent messages were cancelled.",
        "dm_off_none": "Direct messages were not on.",
        "group_on": "Subscribed to {name}. New updates will go to that group.",
        "group_on_already": "That group is already subscribed.",
        "group_off": "Unsubscribed from {name}. Unsent messages were cancelled.",
        "group_off_none": "That group was not subscribed.",
        "only_dm": "Turn on direct messages in a private chat with the bot.",
        "need_admin_group": "Only an admin can change notifications in a group. Ask an admin. Send /help for details.",
        "status_title": "Subscriptions:",
        "status_empty": "No subscriptions yet.",
        "status_line": "{kind} {name} (…{tail}) {extra}",
        "kind_personal": "direct",
        "kind_group": "group",
        "stopped": "stopped: {reason}",
        "add_intro": "Reply with a number or the full group name. One group, valid for 1 minute.",
        "remove_intro": "Reply with a number or the full group name to remove it. One group, valid for 1 minute.",
        "empty_add": "No groups to add. Add the bot to a group first.",
        "empty_remove": "No group subscriptions yet.",
        "choice": "{index}. {name} (…{tail})",
        "ambiguous": "More than one group has that name. Reply with the number.",
        "bad_choice": "That does not match the latest list. Reply with a number or the full name.",
        "expired": "That choice expired. Send the command again.",
        "cancelled": "Cancelled.",
        "pair_ok": "You are now an admin.",
        "pair_bad": "That pairing code is invalid or expired.",
        "pair_limited": "Too many attempts. Try again later.",
        "pair_dm": "Send the pairing code in a direct chat with the bot.",
        "admin_added": "Admin added.",
        "admin_usage": "Usage: /addadmin <user id>",
        "list_failed": "The group list is unavailable. Try again shortly.",
        "not_group_cmd": "Manage group subscriptions in a direct chat with the bot. Send /help for details.",
    },
}


def usage_guide(
    language: str,
    *,
    channel: str,
    allow_self_subscribe: bool,
    admin: bool,
    scope: str = "personal",
) -> str:
    """Short usage guide for the current channel and the real permission settings."""
    en = language == "en"
    if scope == "group":
        return _guide_brief(en, channel, admin, allow_self_subscribe)
    return _guide_full(en, channel, allow_self_subscribe, admin)


def _guide_brief(en: bool, channel: str, admin: bool, allow_self_subscribe: bool) -> str:
    if en:
        if allow_self_subscribe:
            lead = (
                "Read this first: only an admin can manage groups. "
                "allow_self_subscribe is on, so anyone can turn their own direct messages on or off in a private chat. "
                "Everyone in the group receives notices. Nobody manages the group from here."
            )
        else:
            lead = (
                "Read this first: only an admin can turn notifications on or off. "
                "allow_self_subscribe is off. Everyone in the group receives notices. Nobody manages them from the group."
            )
        lines = [
            lead,
            _channel_line(en, channel, brief=True),
        ]
        if admin:
            lines.append("Admins manage groups in a direct chat.")
        lines.append("For the full guide, send /help or #帮助 in a direct chat.")
        return "\n".join(lines)
    if allow_self_subscribe:
        lead = "先读我：群通知只有管理员能开关。allow_self_subscribe 当前开启，你可以在私聊给自己开关单聊。群里每个人都会收到，但不能在群里管理。"
    else:
        lead = "先读我：只有管理员能开关通知。allow_self_subscribe 当前关闭。群里每个人都会收到，但不能在群里管理。"
    lines = [
        lead,
        _channel_line(en, channel, brief=True),
    ]
    if admin:
        lines.append("管理员请在私聊里管理群。")
    lines.append("完整说明请私聊发送 /help 或 #帮助。")
    return "\n".join(lines)


def _guide_full(en: bool, channel: str, allow_self_subscribe: bool, admin: bool) -> str:
    parts = [
        _preamble(en, allow_self_subscribe),
        _channel_line(en, channel, brief=False),
        _command_block(en, allow_self_subscribe, admin),
    ]
    return "\n\n".join(part for part in parts if part)


def _preamble(en: bool, allow_self_subscribe: bool) -> str:
    if en:
        flag = (
            "allow_self_subscribe is on: anyone can turn their own direct messages on or off, but only an admin can manage groups."
            if allow_self_subscribe
            else "allow_self_subscribe is off: a non-admin cannot turn on their own direct messages."
        )
        return (
            "Read this first\n"
            "Only an admin can turn notifications on or off, list subscriptions, and add groups. "
            f"{flag} "
            "Everyone in a subscribed group receives the notices. They still cannot manage them. "
            "Send /start, /help, #帮助, #指南, or #使用说明 any time."
        )
    flag = (
        "allow_self_subscribe 当前开启：任何人可以在私聊给自己开关单聊，但不能管理群。"
        if allow_self_subscribe
        else "allow_self_subscribe 当前关闭：非管理员不能自己开单聊。"
    )
    return (
        "先读我\n"
        "只有管理员能开关通知、查看订阅和添加群。"
        f"{flag}"
        "群通知由管理员添加，群里所有人都会收到，但不能管理。"
        "随时发送 /start、/help、#帮助、#指南 或 #使用说明。"
    )


def _channel_line(en: bool, channel: str, *, brief: bool) -> str:
    feishu = channel == "feishu"
    if en:
        if feishu:
            if brief:
                return "This Feishu app works only inside its own company, and only for people in its availability scope."
            return (
                "Feishu: this is a custom app for one company. Other companies cannot use it; they deploy their own bot and secrets. "
                "In the open platform, under Version management and release, set the availability scope to the people or departments who should use it, then publish."
            )
        if brief:
            return "On Telegram, press Start before the bot can send you a direct message."
        return (
            "Telegram: press Start in the direct chat or the bot cannot message you. "
            "To see commands that do not start with / (such as #帮助) inside a group, turn off privacy mode in BotFather."
        )
    if feishu:
        if brief:
            return "这个飞书应用只在本企业、且在可用范围内的人能单聊。"
        return (
            "飞书：这是企业自建应用，只能在创建它的企业里用。别的企业的人用不了，需要自己部署机器人和密钥。"
            "到开放平台「版本管理与发布」，把可用范围设成要使用的成员或部门，并发布（入口名称以当前页面为准）。"
        )
    if brief:
        return "Telegram 要先在私聊点 Start，机器人才发得了消息。"
    return (
        "Telegram：对方要先在私聊点 Start，否则收不到机器人的私聊。"
        "群里要识别 #帮助 这类不以 / 开头的指令，需要在 BotFather 里关闭 privacy mode。"
    )


def _command_block(en: bool, allow_self_subscribe: bool, admin: bool) -> str:
    if en:
        if admin:
            return (
                "Admin commands\n"
                "/subscribe or #开启单聊通知 turns on your direct messages. /unsubscribe or #关闭单聊通知 turns them off.\n"
                "Add the bot to a group, then in a direct chat send /addgroup or #添加群聊 and reply with the number. "
                "/subscribe inside the group subscribes that group.\n"
                "/removegroup, #移除群聊, #取消群聊, or #取消通知 removes one. /cancel or #取消 leaves the picker.\n"
                "/status or #状态 lists subscriptions. /pair <code> in a direct chat becomes an admin. /addadmin <user id> adds one."
            )
        if allow_self_subscribe:
            return (
                "What you can do\n"
                "In a direct chat, /subscribe or #开启单聊通知 turns on your own messages. "
                "/unsubscribe or #关闭单聊通知 turns them off.\n"
                "/help, /start, #帮助, #指南, or #使用说明 shows this guide.\n"
                "Managing groups and listing every subscription still needs an admin."
            )
        return (
            "What you can do\n"
            "/help, /start, #帮助, #指南, or #使用说明 shows this guide.\n"
            "You cannot turn on direct messages or manage groups. Ask an admin."
        )
    if admin:
        return (
            "管理员指令\n"
            "/subscribe 或 #开启单聊通知 开自己的单聊；/unsubscribe 或 #关闭单聊通知 关掉。\n"
            "先把机器人加进群，再在私聊发送 /addgroup 或 #添加群聊 并回复编号。群里发送 /subscribe 会直接订阅该群。\n"
            "/removegroup、#移除群聊、#取消群聊 或 #取消通知 移除。/cancel 或 #取消 退出选择。\n"
            "/status 或 #状态 查看订阅。私聊 /pair <配对码> 成为管理员。/addadmin <用户 id> 添加管理员。"
        )
    if allow_self_subscribe:
        return (
            "你能用的指令\n"
            "私聊发送 /subscribe 或 #开启单聊通知 给自己开单聊；/unsubscribe 或 #关闭单聊通知 关掉。\n"
            "/help、/start、#帮助、#指南 或 #使用说明 查看本说明。\n"
            "管理群和查看全部订阅仍需要管理员。"
        )
    return (
        "你能用的指令\n"
        "/help、/start、#帮助、#指南 或 #使用说明 查看本说明。\n"
        "你不能开单聊，也不能管理群。请联系管理员。"
    )


def chat_text(language: str, key: str, **values: str) -> str:
    """Reply copy for chat control. Unknown keys fall back to English, then the key."""
    catalog = _CHAT["en"] if language == "en" else _CHAT["zh"]
    template = catalog.get(key) or _CHAT["en"].get(key) or key
    return template.format(**values) if values else template
