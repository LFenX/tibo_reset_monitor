"""Calendar model, optional Pillow rendering, and history caching."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.support import FakeClock, ScriptedSource, TempStoreTest, failure, ok, snapshot
from tibo_reset_monitor.core.calendar import (
    CalendarImageUnavailable,
    calendar_model,
    default_history_url,
    find_cjk_font,
    render_calendar_png,
)
from tibo_reset_monitor.core.history import HistoryCalendar

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)


def _event(event_id, **overrides):
    event = {
        "id": event_id,
        "type": "direct_reset",
        "updatedAt": "2026-09-01T00:00:00.000+08:00",
        "createdAt": "2026-10-02T00:00:00.000+08:00",
        "confirmedAt": None,
        "occurredOn": None,
        "confirmationBasis": None,
        "presentation": {"status": "announced", "kindExplicit": False},
        "estimate": None,
        "schedule": None,
        "posts": [],
    }
    event.update(overrides)
    return event


class CalendarModelTest(unittest.TestCase):
    def test_confirmation_dates_and_labels(self):
        history = {
            "events": [
                _event(
                    "old",
                    type="direct_reset",
                    updatedAt="2026-09-01T00:00:00.000+08:00",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-02T08:00:00.000+08:00",
                    occurredOn="2026-10-02",
                    presentation={"status": "confirmed", "kindExplicit": True},
                ),
                _event(
                    "dropped",
                    updatedAt="2026-10-25T00:00:00.000+08:00",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-25T00:00:00.000+08:00",
                    occurredOn="2026-10-25",
                    presentation={"kindExplicit": True},
                ),
            ]
        }
        recent = {
            "historyFrom": "2026-10-21T00:00:00.000+08:00",
            "events": [
                _event(
                    "card",
                    type="reset_credit",
                    updatedAt="2026-10-27T00:00:00.000+08:00",
                    confirmationBasis="receipt_review",
                    confirmedAt="2026-10-27T12:00:00.000+08:00",
                    occurredOn="2026-10-27",
                    presentation={"kindExplicit": True, "status": "confirmed"},
                ),
                _event(
                    "waiting",
                    updatedAt="2026-10-28T00:00:00.000+08:00",
                    estimate={"through": "2026-10-28T16:00:00.000+08:00"},
                    presentation={"status": "announced", "kindExplicit": False},
                ),
                _event(
                    "expected",
                    updatedAt="2026-10-26T00:00:00.000+08:00",
                    estimate={"through": "2026-10-20T16:00:00.000+08:00"},
                    presentation={"status": "likely_completed", "kindExplicit": False},
                ),
            ],
            "activities": [{"id": "w", "action": "withdraw", "eventIds": ["old"]}],
        }
        model = calendar_model(history, recent, NOW)
        self.assertEqual(model["today"], "2026-10-28")
        self.assertNotIn("old", [item["id"] for items in model["days"].values() for item in items])
        self.assertNotIn("dropped", [item["id"] for items in model["days"].values() for item in items])
        labels = {item["id"]: item for items in model["days"].values() for item in items}
        self.assertEqual(labels["card"]["label"], "发重置卡")
        self.assertEqual(labels["card"]["state"], "confirmed")
        self.assertEqual(labels["waiting"]["state"], "waiting")
        self.assertEqual(labels["waiting"]["label"], "重置确认")
        self.assertEqual(labels["expected"]["state"], "expected")
        self.assertEqual(model["days"]["2026-10-28"][0]["id"], "waiting")
        self.assertEqual(model["totals"]["发重置卡"], 1)
        self.assertEqual(model["totals"]["额度重置"], 0)
        self.assertEqual(model["totals"]["重置确认"], 0)

    def test_amendment_context_is_its_own_total(self):
        recent = {
            "historyFrom": "2026-10-01T00:00:00.000+08:00",
            "events": [
                _event(
                    "base",
                    updatedAt="2026-09-02T00:00:00.000+08:00",
                    posts=[{"id": "amend-post", "stage": "补充说明"}],
                ),
                _event(
                    "follow",
                    updatedAt="2026-09-03T00:00:00.000+08:00",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-03T00:00:00.000+08:00",
                    occurredOn="2026-10-03",
                    posts=[{"id": "follow-post", "stage": "确认完成", "context": [{"id": "amend-post"}]}],
                ),
            ],
        }
        # base is before the cutoff only when it comes from history. Put both in recent so both exist.
        model = calendar_model({"events": []}, recent, NOW)
        follow = next(item for items in model["days"].values() for item in items if item["id"] == "follow")
        self.assertEqual(follow["label"], "补充确认")
        self.assertEqual(model["totals"]["补充确认"], 1)
        self.assertEqual(model["totals"]["重置确认"], 0)

    def test_explicit_direct_reset_is_not_an_estimate(self):
        recent = {
            "historyFrom": "2026-10-01T00:00:00.000+08:00",
            "events": [
                _event(
                    "reset",
                    updatedAt="2026-09-01T00:00:00.000+08:00",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-05T10:00:00.000+08:00",
                    occurredOn=None,
                    presentation={"kindExplicit": True, "status": "confirmed"},
                    estimate={"through": "2026-10-01T00:00:00.000+08:00"},
                )
            ],
        }
        model = calendar_model(None, recent, NOW)
        self.assertEqual(model["days"]["2026-10-05"][0]["label"], "额度重置")
        self.assertEqual(model["days"]["2026-10-05"][0]["state"], "confirmed")
        self.assertNotIn("2026-10-01", model["days"])

    def test_history_url_strips_recent(self):
        self.assertEqual(
            default_history_url("https://aihot.news/api/v1/codex-resets/recent"),
            "https://aihot.news/api/v1/codex-resets",
        )

    def test_render_degrades_without_pillow_or_font(self):
        model = calendar_model({"events": []}, {"events": [], "historyFrom": "2026-10-01"}, NOW)
        try:
            import PIL  # noqa: F401

            pillow = True
        except ImportError:
            pillow = False
        fonts = find_cjk_font()
        if not pillow or fonts is None:
            with self.assertRaises(CalendarImageUnavailable):
                render_calendar_png(model, fonts)
            return
        png = render_calendar_png(model, fonts)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 1000)
        self._assert_legend_sits_at_the_bottom(png, model["year"], model["month"])

    def _assert_legend_sits_at_the_bottom(self, png: bytes, year: int, month: int) -> None:
        import calendar as calendar_mod
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        weeks = calendar_mod.Calendar(firstweekday=0).monthdatescalendar(year, month)
        cell_step = 90 + 8
        legend_y = 176 + len(weeks) * cell_step
        self.assertEqual(image.height, legend_y + 24 + 22)
        self.assertLess(image.height, 165 + len(weeks) * cell_step + 95)
        swatches = {(0x9C, 0xC5, 0xB5), (0xC7, 0xDC, 0xD0), (0xDB, 0xC9, 0xA9)}
        found = set()
        for y in range(legend_y, min(image.height, legend_y + 24)):
            for x in range(0, image.width, 2):
                pixel = image.getpixel((x, y))
                for swatch in swatches:
                    if all(abs(pixel[channel] - swatch[channel]) <= 8 for channel in range(3)):
                        found.add(swatch)
        self.assertEqual(found, swatches)
        # The old footnotes sat well below the legend. That band is now padding or the border.
        footnote_y = legend_y + 57
        self.assertGreaterEqual(footnote_y, image.height - 8)


class HistoryCacheTest(TempStoreTest):
    def test_failure_keeps_the_previous_snapshot_and_does_not_raise(self):
        body = snapshot([], checked_at="2026-10-01T00:00:00.000+08:00")
        source = ScriptedSource(
            [
                ok(body, etag='W/"h1"'),
                failure("network", "reset"),
            ]
        )
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=3600)
        calendar.refresh()
        self.assertEqual(self.store.get_history()["etag"], 'W/"h1"')
        self.clock.advance(3600)
        calendar.refresh()
        self.assertEqual(self.store.get_history()["body"]["checkedAt"], "2026-10-01T00:00:00.000+08:00")
        self.clock.advance(10)
        calendar.refresh()
        self.assertEqual(len(source.etags), 2)

    def test_month_change_refreshes_after_the_minimum_gap(self):
        self.clock.now_value = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
        first = snapshot([], checked_at="2026-09-30T00:00:00.000+08:00")
        second = snapshot([], checked_at="2026-10-02T00:00:00.000+08:00")
        source = ScriptedSource([ok(first, etag='W/"a"'), ok(second, etag='W/"b"')])
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=7 * 24 * 3600)
        calendar.refresh()
        self.clock.advance(timedelta(days=2).total_seconds())
        calendar.refresh()
        self.assertEqual(self.store.get_history()["etag"], 'W/"b"')
        self.assertEqual(self.store.get_history()["body"]["checkedAt"], "2026-10-02T00:00:00.000+08:00")


if __name__ == "__main__":
    unittest.main()
