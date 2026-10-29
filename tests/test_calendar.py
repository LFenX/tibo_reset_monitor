"""Calendar model, optional Pillow rendering, and history caching."""

from __future__ import annotations

import unittest
from tests.support import SafetyFixture
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.support import FakeClock, ScriptedSource, TempStoreTest, failure, make_post, ok, snapshot
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
SHANGHAI = ZoneInfo("Asia/Shanghai")
KOLKATA = ZoneInfo("Asia/Kolkata")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


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
        # A corrected full snapshot is authoritative, including deletions.
        full = {"events": recent["events"]}
        model = calendar_model(full, history, NOW)
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
                    posts=[{**make_post("amend-post", "补充说明"), "publishedAt": "2026-10-02T00:00:00Z"}],
                ),
                _event(
                    "follow",
                    updatedAt="2026-09-03T00:00:00.000+08:00",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-03T00:00:00.000+08:00",
                    occurredOn="2026-10-03",
                    posts=[{**make_post("follow-post", "确认完成"), "publishedAt": "2026-10-03T00:00:00Z", "context": [{"id": "amend-post", "author": "synthetic", "relation": "quote", "text": None, "originalText": "Synthetic amendment", "url": "https://x.com/thsottiaux/status/amend-post"}]}],
                ),
            ],
        }
        model = calendar_model(recent, {"events": []}, NOW)
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
                _event("base", posts=[{**make_post("amend-post", "补充说明"), "publishedAt": "2026-10-02T00:00:00Z"}]),
                _event(
                    "explicit",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-04T00:00:00.000+08:00",
                    occurredOn="2026-10-04",
                    presentation={"kindExplicit": True, "status": "confirmed"},
                    posts=[{**make_post("follow", "确认完成"), "publishedAt": "2026-10-04T00:00:00Z", "context": [{"id": "amend-post", "author": "synthetic", "relation": "quote", "text": None, "originalText": "Synthetic amendment", "url": "https://x.com/thsottiaux/status/amend-post"}]}],
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

    def test_english_calendar_renders_without_a_cjk_font(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        from unittest.mock import patch

        from tibo_reset_monitor.core.calendar import find_latin_font

        model = calendar_model({"events": []}, {"events": [], "historyFrom": "2026-10-01"}, NOW)
        model["days"] = {"2026-10-28": [{"id": "x", "label": "额度重置", "state": "confirmed"}]}
        with patch("tibo_reset_monitor.core.calendar.find_cjk_font", return_value=None):
            with self.assertRaises(CalendarImageUnavailable):
                render_calendar_png(model, language="zh")
            self.assertIsNotNone(find_latin_font())
            png = render_calendar_png(model, language="en")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 1000)

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


class TimezoneMonthTest(unittest.TestCase):
    def test_shanghai_month_crosses_at_utc_plus_8(self):
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
            model = calendar_model(None, {"events": []}, now, SHANGHAI)
            self.assertEqual((model["year"], model["month"], model["today"]), (year, month, today), now.isoformat())
            self.assertEqual(model["totals"], {"额度重置": 0, "发重置卡": 0, "补充确认": 0})

    def test_other_zones_do_not_share_shanghais_month_boundary(self):
        instant = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        shanghai = calendar_model(None, {"events": []}, instant, SHANGHAI)
        utc = calendar_model(None, {"events": []}, instant, timezone.utc)
        los_angeles = calendar_model(None, {"events": []}, instant, LOS_ANGELES)
        self.assertEqual(shanghai["today"], "2026-11-01")
        self.assertEqual(utc["today"], "2026-10-31")
        self.assertEqual(los_angeles["today"], "2026-10-31")

    def test_kolkata_uses_the_half_hour_offset(self):
        # 2026-10-31 18:30 UTC is 2026-11-01 00:00 in Asia/Kolkata (UTC+5:30).
        before = calendar_model(None, {"events": []}, datetime(2026, 10, 31, 18, 29, tzinfo=timezone.utc), KOLKATA)
        after = calendar_model(None, {"events": []}, datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc), KOLKATA)
        self.assertEqual(before["today"], "2026-10-31")
        self.assertEqual(after["today"], "2026-11-01")

    def test_date_only_fields_stay_put_and_timestamps_move(self):
        recent = {
            "historyFrom": "2026-01-01T00:00:00Z",
            "events": [
                _event(
                    "dated",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-31T20:00:00Z",
                    occurredOn="2026-10-31",
                    presentation={"kindExplicit": True, "status": "confirmed"},
                ),
                _event(
                    "stamped",
                    confirmationBasis="source_post",
                    confirmedAt="2026-10-31T20:00:00Z",
                    occurredOn=None,
                    presentation={"kindExplicit": True, "status": "confirmed"},
                ),
            ],
        }
        now = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
        shanghai = calendar_model(None, recent, now, SHANGHAI)
        utc = calendar_model(None, recent, now, timezone.utc)
        self.assertEqual(shanghai["days"]["2026-10-31"][0]["id"], "dated")
        self.assertEqual(shanghai["days"]["2026-11-01"][0]["id"], "stamped")
        self.assertEqual(utc["days"]["2026-10-31"][0]["id"], "dated")
        self.assertEqual({item["id"] for item in utc["days"]["2026-10-31"]}, {"dated", "stamped"})
        self.assertNotIn("2026-11-01", utc["days"])

    def test_los_angeles_daylight_saving_offset_changes(self):
        from tibo_reset_monitor.core.i18n import compose
        from tibo_reset_monitor.core.model import Notice

        summer = Notice(
            dedupe_key="s",
            post_key="s",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="thsottiaux",
            text="译",
            original_text="summer",
            url="https://x.com/thsottiaux/status/1",
            published_at="2026-07-15T19:00:00Z",
            tag_label="重置（形式未明确）",
        )
        winter = Notice(
            dedupe_key="w",
            post_key="w",
            stage="announcement",
            stage_label="预告",
            title="",
            display_label="",
            author="thsottiaux",
            text="译",
            original_text="winter",
            url="https://x.com/thsottiaux/status/2",
            published_at="2026-12-15T20:00:00Z",
            tag_label="重置（形式未明确）",
        )
        summer_line = compose(summer, "en", timezone_name="America/Los_Angeles").posted_line
        winter_line = compose(winter, "en", timezone_name="America/Los_Angeles").posted_line
        self.assertEqual(summer_line, "Posted · 2026-07-15 12:00:00 UTC-7")
        self.assertEqual(winter_line, "Posted · 2026-12-15 12:00:00 UTC-8")
        kolkata = compose(summer, "zh", timezone_name="Asia/Kolkata").posted_line
        self.assertEqual(kolkata, "帖子发布 · 2026-07-16 00:30:00 UTC+5:30")

    def test_header_counts_only_the_shanghai_month(self):
        recent = {
            "historyFrom": "2026-01-01T00:00:00.000+08:00",
            "events": [
                _event("oct", confirmationBasis="source_post", confirmedAt="2026-10-31T09:00:00+08:00", occurredOn="2026-10-31", presentation={"kindExplicit": True, "status": "confirmed"}),
                _event("nov", confirmationBasis="source_post", confirmedAt="2026-11-02T09:00:00+08:00", occurredOn="2026-11-02", presentation={"kindExplicit": True, "status": "confirmed"}),
                _event("dec", type="reset_credit", confirmationBasis="source_post", confirmedAt="2026-12-01T09:00:00+08:00", occurredOn="2026-12-01", presentation={"kindExplicit": True, "status": "confirmed"}),
            ],
        }
        october = calendar_model(None, recent, datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc), SHANGHAI)
        november = calendar_model(None, recent, datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc), SHANGHAI)
        january = calendar_model(None, recent, datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc), SHANGHAI)
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
            model = calendar_model(None, recent, today, SHANGHAI)
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
    def test_deleted_history_is_not_resurrected_by_old_recent(self):
        old = {"events": [_event("removed", occurredOn="2026-10-02")]}
        self.assertEqual(calendar_model({"events": []}, old, NOW)["days"], {})

    def test_bad_replacement_preserves_valid_history_and_304_clears_error(self):
        from tests.support import not_modified
        from copy import deepcopy
        original = snapshot([_event("virtual")])
        bad = deepcopy(original)
        bad["events"][0]["occurredOn"] = "2026-02-30"
        source = ScriptedSource([ok(original), ok(bad), not_modified()])
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=600)
        calendar.refresh()
        self.clock.advance(600)
        calendar.refresh()
        self.assertEqual(self.store.get_history()["body"], original)
        self.assertIsNotNone(self.store.get_meta("calendar_error"))
        self.clock.advance(600)
        calendar.refresh()
        self.assertIsNone(self.store.get_meta("calendar_error"))
        self.assertEqual(self.store.get_history()["body"], original)

    def test_recent_etag_change_refreshes_complete_history_after_minimum_gap(self):
        source = ScriptedSource([ok(snapshot([_event("deleted")])), ok(snapshot([]))])
        calendar = HistoryCalendar(self.store, source, self.clock)
        calendar.refresh()
        self.store.save_snapshot(snapshot([]), 'W/"changed"', self.clock.now())
        self.clock.advance(599)
        calendar.refresh()
        self.assertEqual(len(source.etags), 1)
        self.clock.advance(1)
        calendar.refresh()
        self.assertEqual(self.store.get_history()["body"]["events"], [])
        self.assertEqual(self.store.outbox_counts(), {})

    def test_invalid_cached_history_requests_full_replacement(self):
        bad = snapshot([_event("bad", occurredOn="invalid")])
        self.store.save_history(bad, 'W/"invalid"', self.clock.now())
        source = ScriptedSource([ok(snapshot([]))])
        calendar = HistoryCalendar(self.store, source, self.clock)
        calendar.refresh()
        self.assertEqual(source.etags, [None])
        self.assertEqual(self.store.get_history()["body"]["events"], [])

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
        first["historyFrom"] = second["historyFrom"] = "2026-01-01T00:00:00Z"
        source = ScriptedSource([ok(first, etag='W/"a"'), ok(second, etag='W/"b"')])
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=7 * 24 * 3600)
        calendar.refresh()
        self.clock.advance(timedelta(days=2).total_seconds())
        calendar.refresh()
        self.assertEqual(self.store.get_history()["etag"], 'W/"b"')
        self.assertEqual(self.store.get_history()["body"]["checkedAt"], "2026-10-02T00:00:00.000+08:00")

    def test_shanghai_new_month_waits_ten_minutes_then_refetches(self):
        # 2026-10-31 15:55 UTC is 23:55 in Asia/Shanghai, still October.
        self.clock.now_value = datetime(2026, 10, 31, 15, 55, tzinfo=timezone.utc)
        source = ScriptedSource(
            [
                ok(snapshot([]), etag='W/"oct"'),
                ok(snapshot([], checked_at="2026-11-01T00:05:00.000+08:00"), etag='W/"nov"'),
            ]
        )
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=30 * 24 * 3600, timezones=("Asia/Shanghai",))
        calendar.refresh()
        # 2026-11-01 00:00 in Asia/Shanghai is five minutes later, and UTC is still 31 October.
        self.clock.now_value = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        calendar.refresh()
        self.assertEqual(len(source.etags), 1)
        self.assertEqual(self.store.get_history()["etag"], 'W/"oct"')
        # Ten minutes after the October fetch: a new-month pull is allowed.
        self.clock.now_value = datetime(2026, 10, 31, 16, 5, tzinfo=timezone.utc)
        calendar.refresh()
        self.assertEqual(len(source.etags), 2)
        self.assertEqual(self.store.get_history()["etag"], 'W/"nov"')

    def test_each_timezone_refreshes_on_its_own_month(self):
        self.clock.now_value = datetime(2026, 10, 31, 15, 55, tzinfo=timezone.utc)
        source = ScriptedSource([ok(snapshot([]), etag='W/"oct"'), ok(snapshot([]), etag='W/"nov"')])
        los_angeles = HistoryCalendar(
            self.store, source, self.clock, refresh_seconds=30 * 24 * 3600, timezones=("America/Los_Angeles",)
        )
        los_angeles.refresh()
        self.clock.now_value = datetime(2026, 10, 31, 16, 30, tzinfo=timezone.utc)
        los_angeles.refresh()
        self.assertEqual(len(source.etags), 1)
        self.assertEqual(los_angeles.month("America/Los_Angeles"), (2026, 10))
        self.assertEqual(los_angeles.month("Asia/Shanghai"), (2026, 11))
        shanghai = HistoryCalendar(
            self.store, source, self.clock, refresh_seconds=30 * 24 * 3600, timezones=("Asia/Shanghai",)
        )
        shanghai.refresh()
        self.assertEqual(len(source.etags), 2)
        self.assertEqual(self.store.get_history()["etag"], 'W/"nov"')

    def test_failed_new_month_fetch_still_draws_that_month(self):
        self.clock.now_value = datetime(2026, 10, 15, 4, tzinfo=timezone.utc)
        october = snapshot([])
        october["historyFrom"] = "2026-01-01T00:00:00Z"
        source = ScriptedSource(
            [
                ok(october, etag='W/"oct"'),
                failure("network", "history down"),
            ]
        )
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=30 * 24 * 3600, timezones=("Asia/Shanghai",))
        calendar.refresh()
        # 2026-11-01 00:30 in Asia/Shanghai. The history request fails and must not raise.
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


class FutureHistoryMetadataTest(SafetyFixture):
    def test_future_history_cache_metadata_does_not_suspend_daily_refresh(self):
        body = snapshot([])
        self.store.save_history(body, 'W/"future-cache"', self.clock.now() + timedelta(days=3))
        source = ScriptedSource([ok(snapshot([]), etag='W/"recovered"')])
        calendar = HistoryCalendar(self.store, source, self.clock, refresh_seconds=7 * 24 * 3600)
        self.clock.advance(24 * 3600 + 1)
        calendar.refresh()
        self.assertEqual(len(source.etags), 1,
                         {"fetches": source.etags, "history": self.store.get_history(),
                          "attempt_at": self.store.get_meta("history_attempt_at")})

class InvalidHistoryTimeTest(SafetyFixture):
    def test_malformed_fetch_time_forces_an_immediate_full_replacement(self):
        self.store.save_history(snapshot([]), "synthetic-old", self.clock.now() - timedelta(days=2))
        self.store.conn.execute("UPDATE history_snapshot SET fetched_at = 'not-a-timestamp'")
        source = ScriptedSource([ok(snapshot([]), "synthetic-replaced")])
        HistoryCalendar(self.store, source, self.clock).refresh()
        self.assertEqual(source.etags, [None])
        self.assertEqual(self.store.get_history()["etag"], "synthetic-replaced")
        self.assertIsNone(self.store.get_meta("calendar_error"))

    def test_malformed_attempt_time_does_not_block_a_valid_old_cache_refresh(self):
        self.store.save_history(snapshot([]), "synthetic-old", self.clock.now() - timedelta(days=2))
        self.store.set_meta("history_attempt_at", "not-a-timestamp")
        source = ScriptedSource([ok(snapshot([]), "synthetic-replaced")])
        HistoryCalendar(self.store, source, self.clock).refresh()
        self.assertEqual(source.etags, ["synthetic-old"])
        self.assertIsNone(self.store.get_meta("calendar_error"))


if __name__ == "__main__":
    unittest.main()
