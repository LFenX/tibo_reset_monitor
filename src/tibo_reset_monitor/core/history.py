"""Cached copy of the AIHOT full reset history.

``/api/v1/codex-resets`` is the same document as ``/recent``, kept for the
calendar. A failed fetch never replaces a saved copy and never raises into the
poll cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tibo_reset_monitor.core.calendar import (
    BEIJING,
    CalendarImageUnavailable,
    calendar_model,
    find_cjk_font,
    render_calendar_png,
)
from tibo_reset_monitor.core.model import parse_utc

log = logging.getLogger("tibo_reset_monitor")

_MIN_GAP_SECONDS = 600


class HistoryCalendar:
    def __init__(
        self,
        store: Any,
        source: Any,
        clock: Any,
        *,
        refresh_seconds: float = 6 * 60 * 60,
        font_path: str | None = None,
        bold_path: str | None = None,
        font_index: int | None = None,
    ) -> None:
        self.store = store
        self.source = source
        self.clock = clock
        self.refresh_seconds = max(_MIN_GAP_SECONDS, float(refresh_seconds))
        self.font_path = font_path or None
        self.bold_path = bold_path or None
        self.font_index = font_index

    def refresh(self) -> None:
        now = self.clock.now()
        try:
            if not self._due(now):
                return
            result = self.source.fetch(self.store.get_history_etag())
        except Exception as exc:
            log.warning("history fetch failed: %s", exc)
            self._note_attempt(now)
            return
        self._note_attempt(now)
        if result.not_modified:
            self.store.touch_history(now, result.etag)
            self.store.conn.commit()
            log.info("history not modified")
            return
        if result.failure is not None:
            log.warning("history fetch failed (%s): %s", result.failure.kind, result.failure.message)
            self.store.conn.commit()
            return
        self.store.save_history(result.body, result.etag, now)
        self.store.conn.commit()
        log.info("history snapshot stored events=%s", len(result.body.get("events") or []))

    def png(self) -> bytes | None:
        """Render the current month. ``None`` means the caller should send text."""
        try:
            recent = self.store.get_snapshot()
            history = self.store.get_history()
            model = calendar_model(
                history["body"] if history else None,
                recent["body"] if recent else None,
                self.clock.now(),
            )
            fonts = find_cjk_font(self.font_path, self.bold_path, self.font_index)
            return render_calendar_png(model, fonts)
        except CalendarImageUnavailable as exc:
            log.warning("calendar image unavailable: %s", exc)
            return None
        except Exception as exc:
            log.warning("calendar image unavailable: %s", exc)
            return None

    def _due(self, now: datetime) -> bool:
        last_attempt = parse_utc(self.store.get_meta("history_attempt_at"))
        if last_attempt is not None and (now - last_attempt).total_seconds() < _MIN_GAP_SECONDS:
            return False
        cached = self.store.get_history()
        if cached is None:
            if last_attempt is not None and (now - last_attempt).total_seconds() < self.refresh_seconds:
                return False
            return True
        fetched = parse_utc(cached["fetched_at"])
        if fetched is None:
            return True
        if _month(fetched) != _month(now):
            return True
        return (now - fetched).total_seconds() >= self.refresh_seconds

    def _note_attempt(self, now: datetime) -> None:
        from tibo_reset_monitor.core.model import format_utc

        self.store.set_meta("history_attempt_at", format_utc(now))
        self.store.conn.commit()


def _month(value: datetime) -> tuple[int, int]:
    local = value.astimezone(BEIJING)
    return local.year, local.month
