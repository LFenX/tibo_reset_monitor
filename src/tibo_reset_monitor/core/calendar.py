"""Month calendar shared by channels.

The model follows AIHOT's public history plus the recent feed. Pillow and a CJK
font are optional: when either is missing, ``render_calendar_png`` raises
``CalendarImageUnavailable`` and the caller sends text instead.
"""

from __future__ import annotations

import calendar
import glob
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BEIJING = timezone(timedelta(hours=8))
_CANVAS_WIDTH = 1120
_MARGIN = 30
_GAP = 8
_CELL_HEIGHT = 90


def month_weeks(year: int, month: int) -> list[list[date]]:
    """Monday-first weeks covering the month, including leading and trailing days."""
    return calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)


def canvas_height(week_count: int) -> int:
    """Pixel height for a month grid. Grows with 4, 5, or 6 week rows and stays below the legend."""
    legend_y = 176 + week_count * (_CELL_HEIGHT + _GAP)
    return legend_y + 24 + 22

# (regular, bold). Bold may be missing; the regular face is used for both weights.
_FONT_CANDIDATES: tuple[tuple[str, str | None], ...] = (
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", None),
    ("/Library/Fonts/Arial Unicode.ttf", None),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", None),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None),
    (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf",
    ),
)
_FONT_GLOBS = (
    "/usr/share/fonts/**/[Nn]otoSansCJK-Regular.ttc",
    "/usr/share/fonts/**/[Nn]otoSansCJK-Regular.otf",
    "/usr/share/fonts/**/[Nn]otoSansCJKsc-Regular.otf",
    "/usr/share/fonts/**/wqy-microhei.ttc",
    "/usr/share/fonts/**/wqy-zenhei.ttc",
    "/usr/share/fonts/**/SourceHanSansSC-Regular.otf",
    "/usr/local/share/fonts/**/*.ttc",
    "/usr/local/share/fonts/**/*.otf",
)


class CalendarImageUnavailable(Exception):
    """Pillow or a CJK font is missing, or drawing failed."""


@dataclass(frozen=True)
class FontChoice:
    regular: str
    bold: str
    index: int


def beijing_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(BEIJING)


def default_history_url(recent_url: str) -> str:
    """The full snapshot is ``/api/v1/codex-resets``; ``/recent`` is the polling form."""
    trimmed = recent_url.split("?", 1)[0].rstrip("/")
    if trimmed.endswith("/recent"):
        return trimmed[: -len("/recent")]
    return "https://aihot.news/api/v1/codex-resets"


def calendar_model(history: dict[str, Any] | None, recent: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Qualified reset forms and confirmation dates. An estimate is never stored as a confirmation."""
    today = beijing_now(now).date()
    history = history or {}
    recent = recent or {}
    cutoff = str(recent.get("historyFrom") or "9999")
    cutoff_day = cutoff[:10]
    events: dict[str, dict[str, Any]] = {}
    for event in history.get("events") or []:
        if not isinstance(event, dict) or not event.get("id"):
            continue
        updated = str(event.get("updatedAt") or "")[:10]
        if updated < cutoff_day:
            events[str(event["id"])] = event
    for event in recent.get("events") or []:
        if isinstance(event, dict) and event.get("id"):
            events[str(event["id"])] = event
    for activity in recent.get("activities") or []:
        if not isinstance(activity, dict) or activity.get("action") != "withdraw":
            continue
        for identity in activity.get("eventIds") or []:
            events.pop(str(identity), None)

    owners: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for event in events.values():
        for post in event.get("posts") or []:
            if isinstance(post, dict) and post.get("id"):
                owners[str(post["id"])] = (event, post)

    days: dict[str, list[dict[str, str]]] = {}
    totals = {"额度重置": 0, "发重置卡": 0, "补充确认": 0}
    for event in events.values():
        placed = _place_event(event, owners)
        if placed is None:
            continue
        day, label, state = placed
        days.setdefault(day, []).append({"id": str(event["id"]), "label": label, "state": state})
        if day[:7] == today.strftime("%Y-%m") and state == "confirmed":
            totals[label] += 1
    return {
        "year": today.year,
        "month": today.month,
        "today": today.isoformat(),
        "days": days,
        "totals": totals,
    }


def _place_event(
    event: dict[str, Any],
    owners: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[str, str, str] | None:
    presentation = event.get("presentation") if isinstance(event.get("presentation"), dict) else {}
    confirmed = event.get("confirmationBasis") in ("source_post", "receipt_review") and bool(event.get("confirmedAt"))
    if confirmed:
        raw_day = event.get("occurredOn") or str(event.get("confirmedAt") or "")[:10]
        state = "confirmed"
    else:
        window = event.get("estimate") or event.get("schedule") or {}
        if not isinstance(window, dict):
            window = {}
        raw_day = str(window.get("through") or event.get("createdAt") or "")[:10]
        state = "expected" if presentation.get("status") == "likely_completed" else "waiting"
    try:
        day = date.fromisoformat(str(raw_day)[:10]).isoformat()
    except (ValueError, TypeError):
        return None
    event_type = event.get("type")
    label = "发重置卡" if event_type == "reset_credit" else "额度重置"
    # 补充确认 stays limited to events that were not an explicit reset or credit grant.
    explicit = presentation.get("kindExplicit") is True
    unspecified_form = not (explicit and event_type in ("reset_credit", "direct_reset"))
    if unspecified_form and _quotes_amendment(event, owners):
        label = "补充确认"
    return day, label, state


def _quotes_amendment(
    event: dict[str, Any],
    owners: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    for post in event.get("posts") or []:
        if not isinstance(post, dict):
            continue
        for context in post.get("context") or []:
            if not isinstance(context, dict):
                continue
            owner = owners.get(str(context.get("id") or ""))
            if owner and owner[0].get("id") != event.get("id") and owner[1].get("stage") == "补充说明":
                return True
    return False


def find_cjk_font(
    font_path: str | None = None,
    bold_path: str | None = None,
    font_index: int | None = None,
) -> FontChoice | None:
    """Search an explicit path, then Windows / macOS / Linux CJK faces."""
    regular = _existing(font_path) or _search_regular()
    if regular is None:
        return None
    bold = _existing(bold_path) or _bold_sibling(regular)
    index = font_index if font_index is not None else _default_index(regular)
    return FontChoice(regular=regular, bold=bold or regular, index=index)


def render_calendar_png(model: dict[str, Any], fonts: FontChoice | None = None) -> bytes:
    """Draw the month grid. Raises ``CalendarImageUnavailable`` when it cannot."""
    if fonts is None:
        fonts = find_cjk_font()
    if fonts is None:
        raise CalendarImageUnavailable("no CJK font found")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise CalendarImageUnavailable("Pillow is not installed") from exc

    def face(size: int, strong: bool = False):
        path = fonts.bold if strong else fonts.regular
        try:
            return ImageFont.truetype(path, size=size, index=fonts.index)
        except OSError:
            return ImageFont.truetype(path, size=size)

    try:
        weeks = month_weeks(model["year"], model["month"])
        width, margin, gap, cell_height = _CANVAS_WIDTH, _MARGIN, _GAP, _CELL_HEIGHT
        cell_width = (width - 2 * margin - 6 * gap) // 7
        legend_y = 176 + len(weeks) * (cell_height + gap)
        image = Image.new("RGB", (width, canvas_height(len(weeks))), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, image.width - 2, image.height - 2), radius=20, outline="#dbe3e5", width=2)
        draw.text((margin, 25), f'{model["year"]} 年 {model["month"]} 月', font=face(30, True), fill="#20333f")
        totals = model["totals"]
        summary = f'额度重置 {totals["额度重置"]} · 发卡 {totals["发重置卡"]}'
        if totals["补充确认"]:
            summary += f' · 补充确认 {totals["补充确认"]}'
        draw.text((margin, 76), summary, font=face(21), fill="#546b79")
        for column, weekday in enumerate("一二三四五六日"):
            x = margin + column * (cell_width + gap)
            draw.text((x + cell_width // 2 - 10, 121), weekday, font=face(21), fill="#6a7c87")
        for row, week in enumerate(weeks):
            for column, cell_date in enumerate(week):
                x = margin + column * (cell_width + gap)
                y = 160 + row * (cell_height + gap)
                records = model["days"].get(cell_date.isoformat(), [])
                current = cell_date.month == model["month"]
                if any(record["state"] == "confirmed" for record in records):
                    state = "confirmed"
                elif any(record["state"] == "expected" for record in records):
                    state = "expected"
                else:
                    state = "waiting"
                fill = {"confirmed": "#e5f0ec", "expected": "#f0f6f2", "waiting": "#f7f0e5"}.get(state) if records else "#f7f9fa"
                if not current:
                    fill = "#fafbfb"
                outline = "#26747b" if cell_date.isoformat() == model["today"] else None
                draw.rounded_rectangle(
                    (x, y, x + cell_width, y + cell_height),
                    radius=12,
                    fill=fill,
                    outline=outline,
                    width=2,
                )
                draw.text(
                    (x + 12, y + 9),
                    str(cell_date.day),
                    font=face(22),
                    fill="#223b47" if current else "#a0acb1",
                )
                ink = "#276c5b" if current else "#9aaea8"
                for index, record in enumerate(records[:2]):
                    draw.text((x + 10, y + 40 + index * 23), record["label"], font=face(17), fill=ink)
                if len(records) > 2:
                    draw.text((x + cell_width - 35, y + 65), "+" + str(len(records) - 2), font=face(15), fill="#60767e")
        for x, color, label in ((margin, "#9cc5b5", "已确认"), (230, "#c7dcd0", "预计已生效"), (485, "#dbc9a9", "等待确认")):
            draw.ellipse((x, legend_y + 5, x + 13, legend_y + 18), fill=color)
            draw.text((x + 24, legend_y), label, font=face(18), fill="#647b86")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()
    except CalendarImageUnavailable:
        raise
    except Exception as exc:
        raise CalendarImageUnavailable(str(exc)) from exc


def _existing(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_file():
        return str(candidate)
    return None


def _search_regular() -> str | None:
    for regular, _bold in _FONT_CANDIDATES:
        if Path(regular).is_file():
            return regular
    for pattern in _FONT_GLOBS:
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[0]
    return None


def _bold_sibling(regular: str) -> str | None:
    for candidate, bold in _FONT_CANDIDATES:
        if candidate == regular and bold and Path(bold).is_file():
            return bold
    path = Path(regular)
    for replacement in ("Regular", "regular"):
        if replacement in path.name:
            bold_name = path.name.replace(replacement, "Bold" if replacement == "Regular" else "bold")
            bold_path = path.with_name(bold_name)
            if bold_path.is_file():
                return str(bold_path)
    return None


def _default_index(path: str) -> int:
    """Noto/Source Han CJK collections keep Simplified Chinese at face index 2."""
    name = Path(path).name.lower()
    if "cjk" in name and name.endswith(".ttc"):
        return 2
    return 0
