"""Data store for the Madisonville Branch talk tracker.

Production data lives in the "Branch Talk Tracker" Google Sheet. This module
also opens a local SQLite file when Google credentials are not present, so
the app can run in development.

Events may already exist with only a start date (or a legacy "date" column).
Opening the store adds end_date when it is missing and copies the start date
into any blank end date. Members and talks are not rewritten by that migration.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator

import pandas as pd

SPREADSHEET_NAME = "Branch Talk Tracker"
CANONICAL_COLUMNS = ["event_id", "title", "start_date", "end_date", "notes"]
MEMBER_COLUMNS = ["full_name", "category", "is_active"]
TALK_COLUMNS = ["talk_date", "full_name", "topic", "status", "speaker_number"]

# Prefer the most specific header when an old sheet used more than one date column.
HEADER_PRIORITY = {
    "event_id": ["event_id", "id"],
    "title": ["title", "event_name", "name", "event"],
    "start_date": ["start_date", "event_date", "date", "start", "startdate"],
    "end_date": ["end_date", "enddate", "end"],
    "notes": ["notes", "note", "description", "details"],
}


class StoreError(Exception):
    """The data store could not be opened."""


class SpreadsheetMissing(StoreError):
    """The Branch Talk Tracker spreadsheet is not available."""


class EventValidationError(ValueError):
    """An event could not be saved because a field is missing or inconsistent."""


@dataclass
class MigrationReport:
    table: list[list[str]]
    changed: bool
    added_end_column: bool
    backfilled_rows: int
    assigned_ids: int


def norm_header(header: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header).strip().lower()).strip("_")


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def to_date(value: object) -> date | None:
    """Parse a sheet, SQL, or form value into a date. Blank values stay blank."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.date()
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def canonical_date_cell(value: object) -> str:
    """Store parseable dates as YYYY-MM-DD and leave unrecognized text untouched."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"", "none", "nan", "nat"}:
        return ""
    parsed = to_date(value)
    if parsed is None:
        return text
    return parsed.isoformat()


def classify_headers(headers: list[object]) -> tuple[dict[str, int], list[tuple[int, str]]]:
    norms = [norm_header(header) for header in headers]
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for canonical, aliases in HEADER_PRIORITY.items():
        for alias in aliases:
            if alias in norms:
                index = norms.index(alias)
                mapping[canonical] = index
                used.add(index)
                break
    extras = [
        (index, str(headers[index]))
        for index in range(len(headers))
        if index not in used and str(headers[index]).strip()
    ]
    return mapping, extras


def _cell(row: list[object], index: int | None) -> str:
    if index is None or index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def _norm_table(values: list[list[object]]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in values:
        cells = []
        for cell in row:
            if cell is None:
                cells.append("")
            else:
                cells.append(str(cell).strip())
        while cells and cells[-1] == "":
            cells.pop()
        if any(cells):
            normalized.append(cells)
    return normalized


def migrate_event_table(values: list[list[object]] | None) -> MigrationReport:
    """Return an events table that has end_date, without dropping extra columns.

    A blank end date is set to that row's start date. An end date that already
    has a value is left alone. Rows and unrecognized columns are kept.
    """
    source = [list(row) for row in values] if values else []
    if not source:
        table = [list(CANONICAL_COLUMNS)]
        return MigrationReport(table, True, True, 0, 0)

    headers = list(source[0])
    mapping, extras = classify_headers(headers)
    added_end_column = "end_date" not in mapping
    new_headers = list(CANONICAL_COLUMNS) + [name for _, name in extras]
    new_rows: list[list[str]] = []
    backfilled = 0
    assigned = 0
    seen_ids: set[str] = set()

    for raw in source[1:]:
        if not any(str(cell).strip() for cell in raw if cell is not None):
            continue
        start_text = _cell(raw, mapping.get("start_date"))
        end_text = _cell(raw, mapping.get("end_date"))
        if start_text and not end_text:
            end_text = start_text
            backfilled += 1
        event_id = _cell(raw, mapping.get("event_id"))
        if event_id.lower() in {"", "none", "nan"}:
            event_id = str(uuid.uuid4())
            assigned += 1
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        title = _cell(raw, mapping.get("title"))
        notes = _cell(raw, mapping.get("notes"))
        if notes.lower() in {"none", "nan"}:
            notes = ""
        row = [
            event_id,
            title,
            canonical_date_cell(start_text),
            canonical_date_cell(end_text),
            notes,
        ]
        for index, _name in extras:
            row.append(raw[index] if index < len(raw) and raw[index] is not None else "")
        new_rows.append(["" if cell is None else str(cell) for cell in row])

    table = [new_headers] + new_rows
    changed = _norm_table(source) != _norm_table(table)
    return MigrationReport(table, changed, added_end_column, backfilled, assigned)


def log_migration(report: MigrationReport, backend: str) -> None:
    if not report.changed:
        return
    parts: list[str] = []
    if report.added_end_column:
        parts.append("added the end_date column")
    if report.backfilled_rows:
        parts.append(
            f"set end_date to the start date on {report.backfilled_rows} existing event(s)"
        )
    if report.assigned_ids:
        parts.append(f"filled in {report.assigned_ids} missing event id(s)")
    if not parts:
        parts.append("updated the events table")
    print(
        f"madisonville-talk-tracker: {backend} migration: " + "; ".join(parts),
        flush=True,
    )


def empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=CANONICAL_COLUMNS)


def frame_from_event_table(table: list[list[object]]) -> pd.DataFrame:
    if not table:
        return empty_events()
    headers = list(table[0])
    index = {str(header): position for position, header in enumerate(headers)}
    rows = []
    for raw in table[1:]:
        if not any(str(cell).strip() for cell in raw if cell is not None):
            continue

        def cell(name: str) -> str:
            position = index.get(name)
            if position is None or position >= len(raw) or raw[position] is None:
                return ""
            return str(raw[position]).strip()

        event_id = cell("event_id")
        start = to_date(cell("start_date"))
        end = to_date(cell("end_date")) or start
        notes = cell("notes")
        if notes.lower() in {"none", "nan"}:
            notes = ""
        rows.append(
            {
                "event_id": event_id,
                "title": cell("title"),
                "start_date": start,
                "end_date": end,
                "notes": notes,
            }
        )
    if not rows:
        return empty_events()
    return pd.DataFrame(rows, columns=CANONICAL_COLUMNS)


def dataframe_records(df: pd.DataFrame | None) -> list[dict[str, object]]:
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        notes = row.get("notes", "")
        title = row.get("title", "")
        event_id = row.get("event_id", "")
        records.append(
            {
                "event_id": "" if pd.isna(event_id) else str(event_id).strip(),
                "title": "" if pd.isna(title) else str(title).strip(),
                "start_date": row.get("start_date"),
                "end_date": row.get("end_date"),
                "notes": "" if pd.isna(notes) else str(notes).strip(),
            }
        )
    return records


def compose_event_table(existing_table: list[list[object]], records: list[dict[str, object]]) -> list[list[str]]:
    """Write canonical event columns and copy through any extra columns by id."""
    source = [list(row) for row in existing_table] if existing_table else [list(CANONICAL_COLUMNS)]
    headers = list(source[0]) if source else list(CANONICAL_COLUMNS)
    mapping, extras = classify_headers(headers)
    out_headers = list(CANONICAL_COLUMNS) + [name for _, name in extras]
    id_index = mapping.get("event_id")
    old_by_id: dict[str, list[object]] = {}
    for raw in source[1:]:
        event_id = _cell(raw, id_index)
        if event_id:
            old_by_id[event_id] = raw

    table = [out_headers]
    for record in records:
        event_id = str(record.get("event_id", "")).strip() or str(uuid.uuid4())
        old = old_by_id.get(event_id, [])
        start_fallback = _cell(old, mapping.get("start_date"))
        end_fallback = _cell(old, mapping.get("end_date")) or start_fallback
        start_out = canonical_date_cell(record.get("start_date")) or canonical_date_cell(start_fallback)
        end_out = canonical_date_cell(record.get("end_date")) or start_out or canonical_date_cell(end_fallback)
        title = "" if record.get("title") is None else str(record.get("title")).strip()
        notes = "" if record.get("notes") is None else str(record.get("notes")).strip()
        row: list[str] = [event_id, title, start_out, end_out, notes]
        for index, _name in extras:
            value = old[index] if index < len(old) and old[index] is not None else ""
            row.append(str(value))
        table.append(row)
    return rectangular(table)


def rectangular(table: list[list[object]]) -> list[list[str]]:
    width = max((len(row) for row in table), default=0)
    padded = []
    for row in table:
        cells = ["" if cell is None else str(cell) for cell in row]
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        padded.append(cells)
    return padded


def validate_event(
    title: object,
    start_date: object,
    end_date: object,
    notes: object = "",
) -> tuple[str, date, date, str]:
    cleaned_title = "" if title is None else str(title).strip()
    if not cleaned_title:
        raise EventValidationError("Enter an event name.")
    start = to_date(start_date)
    end = to_date(end_date)
    if start is None:
        raise EventValidationError("Choose a start date.")
    if end is None:
        end = start
    if end < start:
        raise EventValidationError("End date must be on or after the start date.")
    cleaned_notes = "" if notes is None else str(notes).strip()
    if cleaned_notes.lower() in {"none", "nan"}:
        cleaned_notes = ""
    return cleaned_title, start, end, cleaned_notes


def event_span(start: object, end: object) -> tuple[date, date] | None:
    start_day = to_date(start)
    end_day = to_date(end) or start_day
    if start_day is None or end_day is None:
        return None
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    return start_day, end_day


def covers(start: object, end: object, day: object) -> bool:
    span = event_span(start, end)
    target = to_date(day)
    if span is None or target is None:
        return False
    return span[0] <= target <= span[1]


def inclusive_day_count(start: object, end: object) -> int:
    span = event_span(start, end)
    if span is None:
        return 0
    return (span[1] - span[0]).days + 1


def format_span_label(start: object, end: object) -> str:
    """Short date range for a calendar chip. The end date is included."""
    span = event_span(start, end)
    if span is None:
        return ""
    start_day, end_day = span
    if start_day == end_day:
        return f"{start_day.strftime('%b')} {start_day.day}"
    if start_day.year == end_day.year and start_day.month == end_day.month:
        return f"{start_day.strftime('%b')} {start_day.day}–{end_day.day}"
    if start_day.year == end_day.year:
        return f"{start_day.strftime('%b')} {start_day.day}–{end_day.strftime('%b')} {end_day.day}"
    return (
        f"{start_day.strftime('%b')} {start_day.day}, {start_day.year}"
        f"–{end_day.strftime('%b')} {end_day.day}, {end_day.year}"
    )


def events_on_day(events: pd.DataFrame | None, day: object) -> pd.DataFrame:
    if events is None or events.empty:
        return empty_events()
    target = to_date(day)
    if target is None:
        return empty_events()
    mask = [
        covers(start, end, target)
        for start, end in zip(events["start_date"], events["end_date"])
    ]
    subset = events.loc[pd.Series(mask, index=events.index)].copy()
    if subset.empty:
        return subset
    subset["_sort"] = subset["start_date"].map(lambda value: to_date(value) or date.min)
    subset = subset.sort_values(["_sort", "title"], kind="mergesort").drop(columns="_sort")
    return subset.reset_index(drop=True)


def titles_on_day(events: pd.DataFrame | None, day: object) -> list[str]:
    rows = events_on_day(events, day)
    if rows.empty:
        return []
    return [str(title) for title in rows["title"].tolist()]


def events_overlapping(events: pd.DataFrame | None, start: object, end: object) -> pd.DataFrame:
    window_start = to_date(start)
    window_end = to_date(end)
    if events is None or events.empty or window_start is None or window_end is None:
        return empty_events()
    keep: list[object] = []
    for idx, row in events.iterrows():
        span = event_span(row["start_date"], row["end_date"])
        if span is None:
            continue
        if span[0] <= window_end and span[1] >= window_start:
            keep.append(idx)
    if not keep:
        return empty_events()
    subset = events.loc[keep].copy()
    subset["_sort"] = subset["start_date"].map(lambda value: to_date(value) or date.min)
    subset = subset.sort_values(["_sort", "title"], kind="mergesort").drop(columns="_sort")
    return subset.reset_index(drop=True)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def describe_day(
    day: object,
    talks: pd.DataFrame | None,
    events: pd.DataFrame | None,
    *,
    is_sunday: bool,
    is_first_sunday: bool,
) -> dict[str, object]:
    """Decide what a calendar cell shows. Talk rules match the existing Sunday grid."""
    target = to_date(day)
    titles = titles_on_day(events, target)
    speakers: list[str] = []
    if (
        is_sunday
        and not is_first_sunday
        and talks is not None
        and not talks.empty
        and "talk_date" in talks.columns
        and "full_name" in talks.columns
        and target is not None
    ):
        matched: list[tuple[str, str]] = []
        for _, row in talks.iterrows():
            if to_date(row["talk_date"]) != target:
                continue
            number = row["speaker_number"] if "speaker_number" in talks.columns else ""
            number_text = "" if pd.isna(number) else str(number)
            matched.append((number_text, str(row["full_name"])))
        matched.sort(key=lambda item: item[0])
        speakers = [name for _, name in matched]
    fast_sunday = bool(is_sunday and is_first_sunday)
    return {
        "titles": titles,
        "speakers": [] if fast_sunday else speakers,
        "fast_sunday": fast_sunday,
        "open_sunday": bool(is_sunday and not fast_sunday and not speakers),
    }


def col_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def sheet_update(worksheet: object, values: list[list[str]], range_name: str = "A1") -> None:
    """Call gspread's update with the 6.x argument order, and fall back for 5.x."""
    try:
        worksheet.update(values, range_name)  # type: ignore[attr-defined]
    except TypeError:
        worksheet.update(range_name, values)  # type: ignore[attr-defined]


def write_table(worksheet: object, table: list[list[str]], previous: list[list[object]] | None) -> None:
    """Write a table starting at A1, then blank leftover rows and columns.

    The new table is fully built in memory before the sheet is touched. The
    update is issued before any cells are cleared, so a failed update leaves
    the previous sheet contents in place.
    """
    table = rectangular(table)
    sheet_update(worksheet, table, "A1")
    if not previous:
        return
    old_height = len(previous)
    new_height = len(table)
    old_width = max((len(row) for row in previous), default=0)
    new_width = max((len(row) for row in table), default=0)
    ranges: list[str] = []
    if old_height > new_height:
        last_col = col_letter(max(old_width, new_width, 1))
        ranges.append(f"A{new_height + 1}:{last_col}{old_height}")
    if old_width > new_width and max(old_height, new_height) > 0:
        ranges.append(
            f"{col_letter(new_width + 1)}1:{col_letter(old_width)}{max(old_height, new_height)}"
        )
    if ranges and hasattr(worksheet, "batch_clear"):
        worksheet.batch_clear(ranges)


class EventCRUD:
    def add_event(self, title: object, start_date: object, end_date: object, notes: object = "") -> str:
        cleaned = validate_event(title, start_date, end_date, notes)
        title_text, start, end, notes_text = cleaned
        events = self.load_events()
        event_id = str(uuid.uuid4())
        row = {
            "event_id": event_id,
            "title": title_text,
            "start_date": start,
            "end_date": end,
            "notes": notes_text,
        }
        events = pd.concat([events, pd.DataFrame([row])], ignore_index=True)
        self.save_events(events)
        return event_id

    def update_event(
        self,
        event_id: object,
        title: object,
        start_date: object,
        end_date: object,
        notes: object = "",
    ) -> None:
        title_text, start, end, notes_text = validate_event(title, start_date, end_date, notes)
        events = self.load_events()
        key = str(event_id)
        if events.empty or not (events["event_id"].astype(str) == key).any():
            raise EventValidationError("That event could not be found. Refresh and try again.")
        mask = events["event_id"].astype(str) == key
        events.loc[mask, "title"] = title_text
        events.loc[mask, "start_date"] = start
        events.loc[mask, "end_date"] = end
        events.loc[mask, "notes"] = notes_text
        self.save_events(events)

    def delete_event(self, event_id: object) -> None:
        events = self.load_events()
        key = str(event_id)
        if events.empty or not (events["event_id"].astype(str) == key).any():
            raise EventValidationError("That event could not be found. Refresh and try again.")
        remaining = events.loc[events["event_id"].astype(str) != key].reset_index(drop=True)
        self.save_events(remaining)

    def load_events(self) -> pd.DataFrame:
        raise NotImplementedError

    def save_events(self, events: pd.DataFrame) -> None:
        raise NotImplementedError


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _records_frame(records: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    if not records:
        return _empty_frame(columns)
    frame = pd.DataFrame(records)
    for column in columns:
        if column not in frame.columns:
            frame[column] = ""
    return frame


class GoogleSheetStore(EventCRUD):
    kind = "sheets"

    def __init__(self, spreadsheet: object):
        self.sheet = spreadsheet
        self._ensure_worksheet("Members", 200, 5, MEMBER_COLUMNS)
        self._ensure_worksheet("Talks", 1000, 6, TALK_COLUMNS)
        # Migrate events as soon as the process starts, before anyone edits.
        self.load_events()

    @classmethod
    def from_env(cls) -> "GoogleSheetStore":
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        try:
            if raw:
                info = json.loads(raw)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
            else:
                path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
                creds = Credentials.from_service_account_file(path, scopes=scopes)
            client = gspread.authorize(creds)
            spreadsheet = client.open(SPREADSHEET_NAME)
        except json.JSONDecodeError as exc:
            raise StoreError("GOOGLE_CREDENTIALS_JSON is not valid JSON.") from exc
        except Exception as exc:
            name = exc.__class__.__name__
            if name == "SpreadsheetNotFound" or "SpreadsheetNotFound" in str(exc):
                raise SpreadsheetMissing(str(exc)) from exc
            if name in {"FileNotFoundError", "DefaultCredentialsError"}:
                raise StoreError(str(exc)) from exc
            raise
        return cls(spreadsheet)

    def _ensure_worksheet(self, title: str, rows: int, cols: int, headers: list[str]) -> object:
        titles = [worksheet.title for worksheet in self.sheet.worksheets()]
        if title not in titles:
            worksheet = self.sheet.add_worksheet(title, rows, cols)
            sheet_update(worksheet, [headers], "A1")
            return worksheet
        worksheet = self.sheet.worksheet(title)
        values = worksheet.get_all_values()
        if len(values) == 0:
            sheet_update(worksheet, [headers], "A1")
        return worksheet

    def load_members(self) -> pd.DataFrame:
        worksheet = self._ensure_worksheet("Members", 200, 5, MEMBER_COLUMNS)
        records = worksheet.get_all_records()
        frame = pd.DataFrame(records)
        if frame.empty:
            return _empty_frame(MEMBER_COLUMNS)
        return frame

    def save_members(self, members: pd.DataFrame) -> None:
        worksheet = self._ensure_worksheet("Members", 200, 5, MEMBER_COLUMNS)
        worksheet.clear()
        worksheet.append_row(MEMBER_COLUMNS)
        if members is not None and not members.empty:
            worksheet.append_rows(members.values.tolist())

    def load_talks(self) -> pd.DataFrame:
        worksheet = self._ensure_worksheet("Talks", 1000, 6, TALK_COLUMNS)
        records = worksheet.get_all_records()
        frame = pd.DataFrame(records)
        if frame.empty:
            return _empty_frame(TALK_COLUMNS)
        if "talk_date" in frame.columns:
            frame["talk_date"] = pd.to_datetime(frame["talk_date"], errors="coerce")
        return frame

    def save_talks(self, talks: pd.DataFrame) -> None:
        worksheet = self._ensure_worksheet("Talks", 1000, 6, TALK_COLUMNS)
        worksheet.clear()
        worksheet.append_row(TALK_COLUMNS)
        if talks is not None and not talks.empty:
            worksheet.append_rows(talks.values.tolist())

    def load_events(self) -> pd.DataFrame:
        worksheet = self._ensure_worksheet("Events", 500, 8, list(CANONICAL_COLUMNS))
        values = worksheet.get_all_values()
        report = migrate_event_table(values)
        if report.changed:
            write_table(worksheet, report.table, previous=values)
            log_migration(report, "Google Sheets")
        return frame_from_event_table(report.table)

    def save_events(self, events: pd.DataFrame) -> None:
        worksheet = self._ensure_worksheet("Events", 500, 8, list(CANONICAL_COLUMNS))
        existing = worksheet.get_all_values()
        migrated = migrate_event_table(existing).table
        table = compose_event_table(migrated, dataframe_records(events))
        write_table(worksheet, table, previous=existing)


class SqliteStore(EventCRUD):
    kind = "sqlite"

    def __init__(self, path: str):
        self.path = path
        self._memory: sqlite3.Connection | None = None
        if path == ":memory:":
            self._memory = sqlite3.connect(":memory:")
        self._migrate()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        owns = self._memory is None
        connection = sqlite3.connect(self.path) if owns else self._memory
        assert connection is not None
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if owns:
                connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    full_name TEXT PRIMARY KEY,
                    category TEXT,
                    is_active TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS talks (
                    talk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    talk_date TEXT,
                    full_name TEXT,
                    topic TEXT,
                    status TEXT,
                    speaker_number TEXT
                )
                """
            )
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
            ).fetchone()
            if not exists:
                connection.execute(
                    """
                    CREATE TABLE events (
                        event_id TEXT PRIMARY KEY,
                        title TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        notes TEXT
                    )
                    """
                )
                return

            columns = [row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()]
            added_end = False
            if "start_date" not in columns and "date" in columns:
                connection.execute(
                    f"ALTER TABLE events RENAME COLUMN {quote_ident('date')} TO {quote_ident('start_date')}"
                )
                columns = [row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()]
            if "start_date" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN start_date TEXT")
            if "end_date" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN end_date TEXT")
                added_end = True
            if "event_id" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            if "title" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN title TEXT")
            if "notes" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN notes TEXT")

            backfill = connection.execute(
                """
                UPDATE events
                SET end_date = start_date
                WHERE start_date IS NOT NULL
                  AND TRIM(start_date) != ''
                  AND (end_date IS NULL OR TRIM(end_date) = '')
                """
            )
            assigned = 0
            for rowid, event_id in connection.execute("SELECT rowid, event_id FROM events").fetchall():
                if event_id is None or str(event_id).strip() == "":
                    connection.execute(
                        "UPDATE events SET event_id = ? WHERE rowid = ?",
                        (str(uuid.uuid4()), rowid),
                    )
                    assigned += 1
            report = MigrationReport(
                table=[],
                changed=bool(added_end or backfill.rowcount or assigned),
                added_end_column=added_end,
                backfilled_rows=max(backfill.rowcount or 0, 0),
                assigned_ids=assigned,
            )
            log_migration(report, "SQLite")

    def _event_columns(self, connection: sqlite3.Connection) -> list[str]:
        return [row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()]

    def load_members(self) -> pd.DataFrame:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT full_name, category, is_active FROM members ORDER BY full_name"
            ).fetchall()
        return _records_frame(
            [
                {"full_name": row[0], "category": row[1], "is_active": row[2]}
                for row in rows
            ],
            MEMBER_COLUMNS,
        )

    def save_members(self, members: pd.DataFrame) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM members")
            if members is None or members.empty:
                return
            for _, row in members.iterrows():
                connection.execute(
                    "INSERT INTO members (full_name, category, is_active) VALUES (?, ?, ?)",
                    (
                        "" if pd.isna(row.get("full_name")) else str(row.get("full_name")),
                        "" if pd.isna(row.get("category", "")) else str(row.get("category", "Adult")),
                        "" if pd.isna(row.get("is_active", "")) else str(row.get("is_active", "TRUE")),
                    ),
                )

    def load_talks(self) -> pd.DataFrame:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT talk_date, full_name, topic, status, speaker_number FROM talks ORDER BY talk_id"
            ).fetchall()
        frame = _records_frame(
            [
                {
                    "talk_date": row[0],
                    "full_name": row[1],
                    "topic": row[2],
                    "status": row[3],
                    "speaker_number": row[4],
                }
                for row in rows
            ],
            TALK_COLUMNS,
        )
        if not frame.empty:
            frame["talk_date"] = pd.to_datetime(frame["talk_date"], errors="coerce")
        return frame

    def save_talks(self, talks: pd.DataFrame) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM talks")
            if talks is None or talks.empty:
                return
            for _, row in talks.iterrows():
                connection.execute(
                    """
                    INSERT INTO talks (talk_date, full_name, topic, status, speaker_number)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_date_cell(row.get("talk_date")),
                        "" if pd.isna(row.get("full_name")) else str(row.get("full_name")),
                        "" if pd.isna(row.get("topic", "")) else str(row.get("topic", "")),
                        "" if pd.isna(row.get("status", "")) else str(row.get("status", "")),
                        "" if pd.isna(row.get("speaker_number", "")) else str(row.get("speaker_number", "")),
                    ),
                )

    def load_events(self) -> pd.DataFrame:
        self._migrate()
        with self._connection() as connection:
            columns = self._event_columns(connection)
            selected = [column for column in CANONICAL_COLUMNS if column in columns]
            quoted = ", ".join(quote_ident(column) for column in selected)
            fetched = connection.execute(f"SELECT {quoted} FROM events").fetchall()
        records = [dict(zip(selected, row)) for row in fetched]
        if not records:
            return empty_events()
        frame = pd.DataFrame(records)
        for column in CANONICAL_COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        frame["start_date"] = frame["start_date"].map(to_date)
        frame["end_date"] = [
            to_date(end) or to_date(start)
            for start, end in zip(frame["start_date"], frame["end_date"])
        ]
        frame["event_id"] = frame["event_id"].map(lambda value: "" if pd.isna(value) else str(value))
        frame["title"] = frame["title"].map(lambda value: "" if pd.isna(value) else str(value))
        frame["notes"] = frame["notes"].map(lambda value: "" if pd.isna(value) else str(value))
        return frame[CANONICAL_COLUMNS]

    def save_events(self, events: pd.DataFrame) -> None:
        self._migrate()
        records = dataframe_records(events)
        with self._connection() as connection:
            columns = self._event_columns(connection)
            extras = [column for column in columns if column not in CANONICAL_COLUMNS]
            old_extras: dict[str, tuple[object, ...]] = {}
            if extras:
                extra_sql = ", ".join(quote_ident(column) for column in extras)
                for row in connection.execute(
                    f"SELECT {quote_ident('event_id')}, {extra_sql} FROM events"
                ):
                    old_extras[str(row[0])] = tuple(row[1:])
            connection.execute("DELETE FROM events")
            for record in records:
                event_id = str(record.get("event_id", "")).strip() or str(uuid.uuid4())
                start_out = canonical_date_cell(record.get("start_date"))
                end_out = canonical_date_cell(record.get("end_date")) or start_out
                values: list[object] = [
                    event_id,
                    "" if record.get("title") is None else str(record.get("title")),
                    start_out,
                    end_out,
                    "" if record.get("notes") is None else str(record.get("notes")),
                ]
                values.extend(list(old_extras.get(event_id, tuple("" for _ in extras))))
                column_sql = ", ".join(quote_ident(column) for column in CANONICAL_COLUMNS + extras)
                placeholders = ", ".join("?" for _ in CANONICAL_COLUMNS + extras)
                connection.execute(
                    f"INSERT INTO events ({column_sql}) VALUES ({placeholders})",
                    values,
                )


def google_credentials_available() -> bool:
    if os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip():
        return True
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    return os.path.isfile(path)


def resolve_sqlite_path() -> str:
    override = os.environ.get("TALK_TRACKER_DB", "").strip()
    if override:
        return override
    folder = os.path.join(os.path.expanduser("~"), ".madisonville_talk_tracker")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "talk_tracker.db")


def build_store() -> GoogleSheetStore | SqliteStore:
    """Open Google Sheets when credentials exist. Otherwise open local SQLite.

    STORAGE_BACKEND=sheets requires credentials and never falls back, so a
    Render deploy cannot silently start writing to a disposable local file.
    STORAGE_BACKEND=sqlite forces the local file. The default, auto, keeps the
    current spreadsheet whenever credentials are configured.
    """
    backend = os.environ.get("STORAGE_BACKEND", "auto").strip().lower() or "auto"
    if backend in {"sqlite", "local"}:
        return SqliteStore(resolve_sqlite_path())
    if backend in {"sheets", "google"}:
        if not google_credentials_available():
            raise StoreError(
                "Google Sheets is selected, but no credentials were found. "
                "Add credentials.json or set GOOGLE_CREDENTIALS_JSON."
            )
        return GoogleSheetStore.from_env()
    if backend != "auto":
        raise StoreError("STORAGE_BACKEND must be auto, sheets, or sqlite.")
    if google_credentials_available():
        return GoogleSheetStore.from_env()
    return SqliteStore(resolve_sqlite_path())
