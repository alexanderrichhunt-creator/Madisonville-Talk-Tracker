"""Tests for multi-day events, the end-date migration, and calendar coverage."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

import tracker_store
from tracker_store import (
    EventValidationError,
    GoogleSheetStore,
    SqliteStore,
    StoreError,
    build_store,
    describe_day,
    events_overlapping,
    format_span_label,
    inclusive_day_count,
    migrate_event_table,
    titles_on_day,
    to_date,
    validate_event,
)


ROOT = Path(__file__).resolve().parent


def col_index(letter: str) -> int:
    value = 0
    for character in letter:
        value = value * 26 + (ord(character) - 64)
    return value


class MemoryWorksheet:
    def __init__(self, title: str, rows: list[list[object]] | None = None):
        self.title = title
        self.rows = [list(row) for row in rows] if rows else []
        self.cleared = 0

    def get_all_values(self) -> list[list[object]]:
        return [list(row) for row in self.rows]

    def get_all_records(self) -> list[dict[str, object]]:
        if not self.rows:
            return []
        headers = [str(cell) for cell in self.rows[0]]
        records = []
        for raw in self.rows[1:]:
            if not any(str(cell).strip() for cell in raw):
                continue
            item = {}
            for index, header in enumerate(headers):
                if not header:
                    continue
                item[header] = raw[index] if index < len(raw) else ""
            records.append(item)
        return records

    def update(self, values, range_name=None, **kwargs):
        if isinstance(values, str):
            values = range_name
        values = [list(row) for row in values]
        # Overlay from A1. Rows below the written table stay until batch_clear,
        # which is how a real spreadsheet update behaves.
        for index, row in enumerate(values):
            if index >= len(self.rows):
                self.rows.append(list(row))
                continue
            current = list(self.rows[index])
            for column, cell in enumerate(row):
                if column >= len(current):
                    current.append(cell)
                else:
                    current[column] = cell
            self.rows[index] = current

    def batch_clear(self, ranges):
        for item in ranges:
            match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", item)
            if not match:
                continue
            start_col, start_row, end_col, end_row = match.groups()
            start_row = int(start_row)
            end_row = int(end_row)
            start_col = col_index(start_col)
            end_col = col_index(end_col)
            for row_number in range(start_row, min(end_row, len(self.rows)) + 1):
                row = list(self.rows[row_number - 1])
                while len(row) < end_col:
                    row.append("")
                for column in range(start_col, end_col + 1):
                    row[column - 1] = ""
                self.rows[row_number - 1] = row
        while len(self.rows) > 1 and not any(str(cell).strip() for cell in self.rows[-1]):
            self.rows.pop()

    def clear(self):
        self.cleared += 1
        self.rows = []

    def append_row(self, row):
        self.rows.append(list(row))

    def append_rows(self, rows):
        for row in rows:
            self.append_row(row)


class MemorySpreadsheet:
    def __init__(self, sheets: dict[str, MemoryWorksheet] | None = None):
        self._sheets = sheets or {}

    def worksheets(self):
        return list(self._sheets.values())

    def worksheet(self, title):
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        worksheet = MemoryWorksheet(title)
        self._sheets[title] = worksheet
        return worksheet


def sheet_store(events_rows: list[list[object]] | None) -> tuple[GoogleSheetStore, MemorySpreadsheet]:
    sheets = {
        "Members": MemoryWorksheet(
            "Members",
            [["full_name", "category", "is_active"], ["Jordan Lee", "Adult", "TRUE"]],
        ),
        "Talks": MemoryWorksheet(
            "Talks",
            [
                ["talk_date", "full_name", "topic", "status", "speaker_number"],
                ["2026-10-04", "Jordan Lee", "Faith", "Completed", 1],
            ],
        ),
    }
    if events_rows is not None:
        sheets["Events"] = MemoryWorksheet("Events", events_rows)
    book = MemorySpreadsheet(sheets)
    return GoogleSheetStore(book), book


class MigrationTests(unittest.TestCase):
    def test_blank_end_date_defaults_to_start_date(self):
        report = migrate_event_table(
            [
                ["event_id", "title", "start_date", "end_date", "notes"],
                ["fireside", "Fireside", "2026-12-01", "", ""],
                ["youth", "Youth Conference", "2027-07-08", "2027-07-10", "EFY"],
            ]
        )
        rows = {row[0]: row for row in report.table[1:]}
        self.assertEqual(rows["fireside"][3], "2026-12-01")
        self.assertEqual(rows["youth"][3], "2027-07-10")
        self.assertEqual(report.backfilled_rows, 1)
        self.assertTrue(report.changed)

        second = migrate_event_table(report.table)
        self.assertFalse(second.changed)
        self.assertEqual(second.backfilled_rows, 0)
        self.assertEqual(second.table, report.table)

    def test_legacy_date_column_becomes_start_and_end(self):
        report = migrate_event_table(
            [
                ["title", "date", "notes"],
                ["Ward Social", "2026-11-01", "Chili"],
            ]
        )
        self.assertEqual(report.table[0][:5], ["event_id", "title", "start_date", "end_date", "notes"])
        row = report.table[1]
        self.assertEqual(row[1], "Ward Social")
        self.assertEqual(row[2], "2026-11-01")
        self.assertEqual(row[3], "2026-11-01")
        self.assertEqual(row[4], "Chili")
        self.assertTrue(row[0])

    def test_specific_start_date_wins_over_legacy_date_column(self):
        report = migrate_event_table(
            [
                ["title", "date", "start_date", "location"],
                ["Nauvoo Trip", "2027-06-01", "2027-06-10", "Nauvoo"],
            ]
        )
        self.assertIn("location", report.table[0])
        row = report.table[1]
        self.assertEqual(row[2], "2027-06-10")
        self.assertEqual(row[3], "2027-06-10")
        self.assertEqual(row[report.table[0].index("location")], "Nauvoo")
        self.assertEqual(row[report.table[0].index("date")], "2027-06-01")

    def test_sheet_migration_preserves_talks_members_and_single_day_events(self):
        store, book = sheet_store(
            [
                ["title", "date"],
                ["Potluck", "2026-10-04"],
            ]
        )
        talks_before = book.worksheet("Talks").get_all_values()
        members_before = book.worksheet("Members").get_all_values()
        events = store.load_events()
        self.assertEqual(book.worksheet("Talks").get_all_values(), talks_before)
        self.assertEqual(book.worksheet("Members").get_all_values(), members_before)
        self.assertEqual(book.worksheet("Talks").cleared, 0)
        self.assertEqual(to_date(events.iloc[0].start_date), date(2026, 10, 4))
        self.assertEqual(to_date(events.iloc[0].end_date), date(2026, 10, 4))
        self.assertEqual(titles_on_day(events, date(2026, 10, 4)), ["Potluck"])
        self.assertEqual(titles_on_day(events, date(2026, 10, 5)), [])

        again = store.load_events()
        self.assertEqual(len(again), 1)
        self.assertEqual(again.iloc[0].event_id, events.iloc[0].event_id)

    def test_extra_column_survives_an_edit(self):
        store, book = sheet_store(
            [
                ["event_id", "title", "start_date", "end_date", "notes", "location"],
                ["trip", "Nauvoo Trip", "2027-06-10", "2027-06-16", "", "Nauvoo"],
            ]
        )
        store.update_event("trip", "Nauvoo Trip", date(2027, 6, 10), date(2027, 6, 18), "Youth")
        values = book.worksheet("Events").get_all_values()
        headers = values[0]
        self.assertIn("location", headers)
        self.assertEqual(values[1][headers.index("location")], "Nauvoo")
        self.assertEqual(values[1][headers.index("end_date")], "2027-06-18")
        self.assertEqual(values[1][headers.index("notes")], "Youth")

    def test_delete_clears_the_sheet_row(self):
        store, book = sheet_store(
            [
                ["event_id", "title", "start_date", "end_date", "notes"],
                ["one", "Potluck", "2026-11-01", "2026-11-01", ""],
                ["two", "Nauvoo Trip", "2027-06-10", "2027-06-16", ""],
            ]
        )
        store.delete_event("one")
        flat = " ".join(str(cell) for row in book.worksheet("Events").get_all_values() for cell in row)
        self.assertNotIn("Potluck", flat)
        self.assertIn("Nauvoo Trip", flat)
        remaining = store.load_events()
        self.assertEqual(remaining["title"].tolist(), ["Nauvoo Trip"])


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.events = pd.DataFrame(
            [
                {
                    "event_id": "trip",
                    "title": "Nauvoo Trip",
                    "start_date": date(2027, 6, 10),
                    "end_date": date(2027, 6, 16),
                    "notes": "",
                },
                {
                    "event_id": "fire",
                    "title": "Stake Fireside",
                    "start_date": date(2027, 6, 20),
                    "end_date": date(2027, 6, 20),
                    "notes": "",
                },
                {
                    "event_id": "cross",
                    "title": "Youth Conference",
                    "start_date": date(2027, 5, 30),
                    "end_date": date(2027, 6, 2),
                    "notes": "",
                },
            ]
        )

    def test_multiday_event_covers_each_inclusive_day(self):
        for day_number in range(10, 17):
            self.assertIn("Nauvoo Trip", titles_on_day(self.events, date(2027, 6, day_number)))
        self.assertNotIn("Nauvoo Trip", titles_on_day(self.events, date(2027, 6, 9)))
        self.assertNotIn("Nauvoo Trip", titles_on_day(self.events, date(2027, 6, 17)))
        self.assertEqual(inclusive_day_count(date(2027, 6, 10), date(2027, 6, 16)), 7)
        self.assertEqual(format_span_label(date(2027, 6, 10), date(2027, 6, 16)), "Jun 10–16")
        self.assertEqual(format_span_label(date(2027, 5, 30), date(2027, 6, 2)), "May 30–Jun 2")
        self.assertEqual(format_span_label(date(2027, 6, 20), date(2027, 6, 20)), "Jun 20")

    def test_single_day_event_covers_only_that_day(self):
        self.assertEqual(titles_on_day(self.events, date(2027, 6, 20)), ["Stake Fireside"])
        self.assertNotIn("Stake Fireside", titles_on_day(self.events, date(2027, 6, 19)))
        self.assertNotIn("Stake Fireside", titles_on_day(self.events, date(2027, 6, 21)))

    def test_event_crossing_months_shows_in_both(self):
        self.assertIn("Youth Conference", titles_on_day(self.events, date(2027, 5, 30)))
        self.assertIn("Youth Conference", titles_on_day(self.events, date(2027, 6, 2)))
        self.assertNotIn("Youth Conference", titles_on_day(self.events, date(2027, 5, 29)))
        june = events_overlapping(self.events, date(2027, 6, 1), date(2027, 6, 30))
        self.assertEqual(set(june["title"]), {"Nauvoo Trip", "Stake Fireside", "Youth Conference"})
        may = events_overlapping(self.events, date(2027, 5, 1), date(2027, 5, 31))
        self.assertEqual(may["title"].tolist(), ["Youth Conference"])

    def test_calendar_cell_keeps_talks_and_shows_events(self):
        talks = pd.DataFrame(
            [
                {
                    "talk_date": pd.Timestamp("2027-06-13"),
                    "full_name": "Jordan Lee",
                    "topic": "Faith",
                    "status": "Scheduled",
                    "speaker_number": 1,
                }
            ]
        )
        sunday = describe_day(
            date(2027, 6, 13),
            talks,
            self.events,
            is_sunday=True,
            is_first_sunday=False,
        )
        self.assertEqual(sunday["speakers"], ["Jordan Lee"])
        self.assertIn("Nauvoo Trip", sunday["titles"])
        self.assertNotIn("Stake Fireside", sunday["titles"])

        weekday = describe_day(
            date(2027, 6, 11),
            talks,
            self.events,
            is_sunday=False,
            is_first_sunday=False,
        )
        self.assertEqual(weekday["speakers"], [])
        self.assertEqual(weekday["titles"], ["Nauvoo Trip"])

        fast = describe_day(
            date(2027, 6, 6),
            talks,
            self.events,
            is_sunday=True,
            is_first_sunday=True,
        )
        self.assertTrue(fast["fast_sunday"])
        self.assertEqual(fast["speakers"], [])
        self.assertEqual(fast["titles"], [])

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(EventValidationError):
            validate_event("Trip", date(2027, 6, 16), date(2027, 6, 10))

    def test_missing_end_date_means_one_day(self):
        title, start, end, _notes = validate_event("Potluck", date(2026, 11, 1), None)
        self.assertEqual(title, "Potluck")
        self.assertEqual(start, end)


class SqliteStoreTests(unittest.TestCase):
    def test_legacy_date_column_defaults_end_and_crud_updates_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "legacy.db")
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE events (event_id TEXT, title TEXT, date TEXT, notes TEXT, location TEXT)"
            )
            connection.execute(
                "INSERT INTO events VALUES ('keep-me', 'Nauvoo Trip', '2027-06-10', 'Youth', 'Nauvoo')"
            )
            connection.execute(
                "CREATE TABLE talks (talk_id INTEGER PRIMARY KEY, talk_date TEXT, full_name TEXT, topic TEXT, status TEXT, speaker_number TEXT)"
            )
            connection.execute(
                "INSERT INTO talks VALUES (1, '2026-10-04', 'Jordan Lee', 'Faith', 'Completed', '1')"
            )
            connection.commit()
            connection.close()

            store = SqliteStore(path)
            events = store.load_events()
            self.assertEqual(events.iloc[0].event_id, "keep-me")
            self.assertEqual(to_date(events.iloc[0].start_date), date(2027, 6, 10))
            self.assertEqual(to_date(events.iloc[0].end_date), date(2027, 6, 10))
            self.assertEqual(titles_on_day(events, date(2027, 6, 10)), ["Nauvoo Trip"])
            self.assertEqual(titles_on_day(events, date(2027, 6, 11)), [])

            talks = store.load_talks()
            self.assertEqual(talks.iloc[0].full_name, "Jordan Lee")
            self.assertEqual(to_date(talks.iloc[0].talk_date), date(2026, 10, 4))

            store.update_event("keep-me", "Nauvoo Trip", date(2027, 6, 10), date(2027, 6, 16), "Youth")
            events = store.load_events()
            for day_number in range(10, 17):
                self.assertIn("Nauvoo Trip", titles_on_day(events, date(2027, 6, day_number)))
            self.assertNotIn("Nauvoo Trip", titles_on_day(events, date(2027, 6, 17)))

            raw = sqlite3.connect(path)
            columns = [row[1] for row in raw.execute("PRAGMA table_info(events)").fetchall()]
            self.assertIn("location", columns)
            location = raw.execute("SELECT location FROM events WHERE event_id = 'keep-me'").fetchone()[0]
            self.assertEqual(location, "Nauvoo")
            raw.close()

            fireside = store.add_event("Stake Fireside", date(2027, 6, 20), date(2027, 6, 20))
            events = store.load_events()
            self.assertEqual(titles_on_day(events, date(2027, 6, 20)), ["Stake Fireside"])
            store.delete_event(fireside)
            events = store.load_events()
            self.assertNotIn("Stake Fireside", titles_on_day(events, date(2027, 6, 20)))
            self.assertIn("Nauvoo Trip", titles_on_day(events, date(2027, 6, 12)))

            # A second open does not invent another copy of the migrated row.
            reopened = SqliteStore(path)
            self.assertEqual(len(reopened.load_events()), 1)

    def test_table_without_id_or_end_date_is_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE events (title TEXT, start_date TEXT)")
            connection.execute("INSERT INTO events VALUES ('Potluck', '2026-11-01')")
            connection.commit()
            connection.close()

            store = SqliteStore(path)
            events = store.load_events()
            self.assertEqual(to_date(events.iloc[0].end_date), date(2026, 11, 1))
            self.assertTrue(str(events.iloc[0].event_id).strip())
            self.assertEqual(titles_on_day(events, date(2026, 11, 1)), ["Potluck"])


class StoreSelectionTests(unittest.TestCase):
    def test_auto_without_credentials_uses_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "local.db")
            with mock.patch.dict(
                os.environ,
                {"STORAGE_BACKEND": "auto", "TALK_TRACKER_DB": path, "GOOGLE_CREDENTIALS_JSON": ""},
            ):
                with mock.patch.object(tracker_store, "google_credentials_available", return_value=False):
                    store = build_store()
            self.assertEqual(store.kind, "sqlite")
            self.assertTrue(os.path.exists(path))

    def test_sheets_backend_requires_credentials(self):
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "sheets", "GOOGLE_CREDENTIALS_JSON": ""}):
            with mock.patch.object(tracker_store, "google_credentials_available", return_value=False):
                with self.assertRaises(StoreError):
                    build_store()


class DeployFileTests(unittest.TestCase):
    def test_render_start_command_and_requirements(self):
        render = (ROOT / "render.yaml").read_text()
        procfile = (ROOT / "Procfile").read_text()
        requirements = (ROOT / "requirements.txt").read_text().lower()
        for text in (render, procfile):
            self.assertIn("streamlit run madisonville_talk_tracker.py", text)
            self.assertIn("--server.port", text)
            self.assertIn("0.0.0.0", text)
        for package in ("streamlit", "pandas", "gspread", "google-auth"):
            self.assertIn(package, requirements)
        self.assertIn("requirements.txt", render)


if __name__ == "__main__":
    unittest.main()
