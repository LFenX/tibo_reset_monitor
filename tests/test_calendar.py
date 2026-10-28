"""Calendar model, optional Pillow rendering, and history caching."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from tests.support import FakeClock, ScriptedSource, TempStoreTest, failure, ok, snapshot
from tibo_reset_monitor.core.calendar import (
    CalendarImageUnavailable,
    calendar_model,
    canvas_height,
    default_history_url,
    find_cjk_font,
    month_weeks,
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
        self.assertEqual(labels["waiting"]["label"], "额度重置")
        self.assertEqual(labels["expected"]["label"], "额度重置")
        self.assertEqual(labels["expected"]["state"], "expected")
        self.assertEqual(model["days"]["2026-10-28"][0]["id"], "waiting")
        self.assertEqual(model["totals"]["发重置卡"], 1)
        self.assertEqual(model["totals"]["额度重置"], 0)
        self.assertNotIn("重置确认", model["totals"])
        self.assertNotIn("重置确认", {item["label"] for item in labels.values()})

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
        self.assertNotIn("重置确认", {item["label"] for items in model["days"].values() for item in items})

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

    def test_unspecified_form_is_still_a_reset_or_credit_label(self):
        recent = {
            "historyFrom": "2026-10-01T00:00:00.000+08:00",
            "events": [
                _event(
                    "plain",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-08T00:00:00.000+08:00",
                    occurredOn="2026-10-08",
                    presentation={"kindExplicit": False, "status": "confirmed"},
                ),
                _event(
                    "quiet-card",
                    type="reset_credit",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-11T00:00:00.000+08:00",
                    occurredOn="2026-10-11",
                    presentation={"kindExplicit": False, "status": "confirmed"},
                ),
            ],
        }
        model = calendar_model(None, recent, NOW)
        labels = {item["id"]: item["label"] for items in model["days"].values() for item in items}
        self.assertEqual(labels["plain"], "额度重置")
        self.assertEqual(labels["quiet-card"], "发重置卡")
        self.assertEqual(model["totals"]["额度重置"], 1)
        self.assertEqual(model["totals"]["发重置卡"], 1)

    def test_explicit_reset_that_quotes_an_amendment_stays_a_reset(self):
        recent = {
            "historyFrom": "2026-10-01T00:00:00.000+08:00",
            "events": [
                _event("base", posts=[{"id": "amend-post", "stage": "补充说明"}]),
                _event(
                    "explicit",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-04T00:00:00.000+08:00",
                    occurredOn="2026-10-04",
                    presentation={"kindExplicit": True, "status": "confirmed"},
                    posts=[{"id": "follow", "stage": "确认完成", "context": [{"id": "amend-post"}]}],
                ),
            ],
        }
        model = calendar_model(None, recent, NOW)
        explicit = next(item for items in model["days"].values() for item in items if item["id"] == "explicit")
        self.assertEqual(explicit["label"], "额度重置")

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


class BeijingMonthTest(unittest.TestCase):
    def test_month_follows_beijing_across_midnight_and_utc(self):
        cases = [
            (datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc), 2026, 10, "2026-10-31"),
            (datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc), 2026, 11, "2026-11-01"),
            (datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc), 2026, 11, "2026-11-01"),
            (datetime(2026, 11, 30, 15, 59, tzinfo=timezone.utc), 2026, 11, "2026-11-30"),
            (datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc), 2026, 12, "2026-12-01"),
            (datetime(2026, 12, 31, 15, 59, tzinfo=timezone.utc), 2026, 12, "2026-12-31"),
            (datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc), 2027, 1, "2027-01-01"),
        ]
        for now, year, month, today in cases:
            model = calendar_model(None, {"events": []}, now)
            self.assertEqual((model["year"], model["month"], model["today"]), (year, month, today), now.isoformat())
            self.assertEqual(model["totals"], {"额度重置": 0, "发重置卡": 0, "补充确认": 0})

    def test_header_counts_only_the_beijing_month(self):
        recent = {
            "historyFrom": "2026-01-01T00:00:00.000+08:00",
            "events": [
                _event("oct", confirmationBasis="source_post", confirmedAt="2026-10-31T09:00:00+08:00", occurredOn="2026-10-31", presentation={"kindExplicit": True, "status": "confirmed"}),
                _event("nov", confirmationBasis="source_post", confirmedAt="2026-11-02T09:00:00+08:00", occurredOn="2026-11-02", presentation={"kindExplicit": True, "status": "confirmed"}),
                _event("dec", type="reset_credit", confirmationBasis="source_post", confirmedAt="2026-12-01T09:00:00+08:00", occurredOn="2026-12-01", presentation={"kindExplicit": True, "status": "confirmed"}),
            ],
        }
        october = calendar_model(None, recent, datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc))
        november = calendar_model(None, recent, datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))
        january = calendar_model(None, recent, datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(october["totals"]["额度重置"], 1)
        self.assertEqual(november["totals"], {"额度重置": 1, "发重置卡": 0, "补充确认": 0})
        self.assertEqual(january["totals"], {"额度重置": 0, "发重置卡": 0, "补充确认": 0})
        self.assertIn("2026-10-31", november["days"])
        self.assertEqual(november["days"]["2026-10-31"][0]["id"], "oct")

    def test_week_rows_resize_from_four_through_six(self):
        expected = {
            (2026, 11): 6,
            (2026, 12): 5,
            (2027, 1): 5,
            (2027, 2): 4,
            (2028, 2): 5,
        }
        heights = {}
        for (year, month), rows in expected.items():
            weeks = month_weeks(year, month)
            self.assertEqual(len(weeks), rows)
            last_bottom = 160 + (rows - 1) * 98 + 90
            legend_y = 176 + rows * 98
            self.assertLess(last_bottom, legend_y)
            self.assertGreater(canvas_height(rows), legend_y + 18)
            heights[rows] = canvas_height(rows)
        self.assertLess(heights[4], heights[5])
        self.assertLess(heights[5], heights[6])
        leap = {day.isoformat() for week in month_weeks(2028, 2) for day in week}
        plain = {day.isoformat() for week in month_weeks(2027, 2) for day in week}
        self.assertIn("2028-02-29", leap)
        self.assertIn("2028-01-31", leap)
        self.assertNotIn("2027-02-29", plain)
        self.assertNotIn("2027-01-31", plain)
        self.assertEqual(month_weeks(2027, 2)[0][0].isoformat(), "2027-02-01")

    def test_rendered_months_keep_every_week_row(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        fonts = find_cjk_font()
        if fonts is None:
            self.skipTest("no CJK font")
        import io

        from PIL import Image

        previous = None
        for year, month, today, rows in (
            (2026, 11, datetime(2026, 11, 18, 4, tzinfo=timezone.utc), 6),
            (2026, 12, datetime(2026, 12, 20, 4, tzinfo=timezone.utc), 5),
            (2027, 1, datetime(2026, 12, 31, 16, tzinfo=timezone.utc), 5),
            (2027, 2, datetime(2027, 2, 14, 4, tzinfo=timezone.utc), 4),
            (2028, 2, datetime(2028, 2, 29, 4, tzinfo=timezone.utc), 5),
        ):
            recent = {
                "historyFrom": "2026-01-01T00:00:00.000+08:00",
                "events": [
                    _event(
                        f"edge-{year}-{month}",
                        confirmationBasis="source_post",
                        confirmedAt="2026-10-31T09:00:00+08:00",
                        occurredOn="2026-10-31",
                        presentation={"kindExplicit": True, "status": "confirmed"},
                    )
                ],
            }
            if month == 2 and year == 2028:
                recent["events"].append(
                    _event(
                        "leap",
                        confirmationBasis="source_post",
                        confirmedAt="2028-02-29T09:00:00+08:00",
                        occurredOn="2028-02-29",
                        presentation={"kindExplicit": True, "status": "confirmed"},
                    )
                )
            model = calendar_model(None, recent, today)
            png = render_calendar_png(model, fonts)
            image = Image.open(io.BytesIO(png))
            self.assertEqual(image.size, (1120, canvas_height(rows)))
            self.assertNotEqual(png, previous)
            previous = png
            if (year, month) == (2026, 11):
                self._assert_cell_fill(image, 2026, 11, date(2026, 10, 31), (0xFA, 0xFB, 0xFB))
                self._assert_today_outline(image, 2026, 11, date(2026, 11, 18))

    def _assert_cell_fill(self, image, view_year, view_month, cell_date, expected) -> None:
        x, y, cell_width, cell_height = _cell_box(view_year, view_month, cell_date)
        pixel = image.getpixel((x + cell_width - 10, y + cell_height // 2))
        self.assertTrue(all(abs(pixel[channel] - expected[channel]) <= 4 for channel in range(3)), pixel)

    def _assert_today_outline(self, image, view_year, view_month, cell_date) -> None:
        x, y, cell_width, _cell_height = _cell_box(view_year, view_month, cell_date)
        target = (0x26, 0x74, 0x7B)
        found = False
        for dx in range(8, cell_width - 8):
            pixel = image.getpixel((x + dx, y + 1))
            if all(abs(pixel[channel] - target[channel]) <= 18 for channel in range(3)):
                found = True
                break
        self.assertTrue(found)


def _cell_box(view_year: int, view_month: int, cell_date: date) -> tuple[int, int, int, int]:
    weeks = month_weeks(view_year, view_month)
    width, margin, gap, cell_height = 1120, 30, 8, 90
    cell_width = (width - 2 * margin - 6 * gap) // 7
    for row, week in enumerate(weeks):
        if cell_date not in week:
            continue
        column = week.index(cell_date)
        x = margin + column * (cell_width + gap)
        y = 160 + row * (cell_height + gap)
        return x, y, cell_width, cell_height
    raise AssertionError(f"{cell_date} is not on the {view_year}-{view_month} grid")


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

    def test_beijing_new_month_waits_ten_minutes_then_refetches(self):
        # 2026-10-31 15:55 UTC is 23:55 in Beijing, still October.
        self.clock.now_value = datetime(2026, 10, 31, 15, 55, tzinfo=timezone.utc)
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"oct"'),
                ok(snapshot([], checked_at="2026-11-01T00:05:00.000+08:00"), etag='W/"nov"'),
            ]
        )
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=30 * 24 * 3600)
        calendar.refresh()
        # 2026-11-01 00:00 Beijing is five minutes later, and UTC is still 31 October.
        self.clock.now_value = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        calendar.refresh()
        self.assertEqual(len(source.etags), 1)
        self.assertEqual(self.store.get_history()["etag"], 'W/"oct"')
        # Ten minutes after the October fetch: a new-month pull is allowed.
        self.clock.now_value = datetime(2026, 10, 31, 16, 5, tzinfo=timezone.utc)
        calendar.refresh()
        self.assertEqual(len(source.etags), 2)
        self.assertEqual(self.store.get_history()["etag"], 'W/"nov"')

    def test_failed_new_month_fetch_still_draws_that_month(self):
        self.clock.now_value = datetime(2026, 10, 15, 4, tzinfo=timezone.utc)
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"oct"'),
                failure("network", "history down"),
            ]
        )
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=30 * 24 * 3600)
        calendar.refresh()
        # 2026-11-01 00:30 Beijing. The history request fails and must not raise.
        self.clock.now_value = datetime(2026, 10, 31, 16, 30, tzinfo=timezone.utc)
        calendar.refresh()
        self.assertEqual(self.store.get_history()["etag"], 'W/"oct"')
        png = calendar.png()
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.assertIsNone(png)
            return
        if find_cjk_font() is None:
            self.assertIsNone(png)
            return
        self.assertIsNotNone(png)
        assert png is not None
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.size[1], canvas_height(6))


if __name__ == "__main__":
    unittest.main()
