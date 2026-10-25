import html
import calendar as cal_module
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from tracker_store import (
    EventValidationError,
    SpreadsheetMissing,
    StoreError,
    build_store,
    describe_day,
    event_span,
    events_on_day,
    events_overlapping,
    format_span_label,
    inclusive_day_count,
    month_bounds,
    titles_on_day,
    to_date,
)

# Force light theme look
st.set_page_config(page_title="Madisonville Branch Talk Scheduler", layout="wide", page_icon="🏛️")

st.markdown("""
    <style>
    .big-font {font-size: 30px !important; font-weight: bold; color: #1a73e8;}
    .medium-font {font-size: 24px !important; color: #202124;}
    .stButton>button {font-size: 20px; height: 55px; width: 100%; background-color: #1a73e8; color: white; border-radius: 8px;}
    .success-msg {font-size: 24px; color: #137333; font-weight: bold;}
    .calendar-day {
        border: 2px solid #dadce0;
        padding: 10px;
        min-height: 145px;
        font-size: 15px;
        background-color: #ffffff;
        border-radius: 8px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        text-align: center;
        overflow-wrap: anywhere;
    }
    .has-talk {background-color: #e8f0fe !important; border-color: #1a73e8;}
    .has-event {background-color: #f0faf4 !important; border-color: #137333;}
    .fast-sunday {background-color: #fef7e0 !important; border-color: #fbbc04;}
    .date-number {font-size: 20px; font-weight: bold; color: #202124; display: block; margin-bottom: 4px;}
    .speaker {font-size: 15px; color: #1a73e8; line-height: 1.3;}
    .open-text {font-size: 14px; color: #5f6368;}
    .event-name {
        display: block;
        font-size: 13px;
        font-weight: 600;
        color: #137333;
        background-color: #e6f4ea;
        border-radius: 4px;
        margin-top: 4px;
        padding: 2px 4px;
        line-height: 1.25;
    }
    .event-range {
        display: block;
        font-size: 11px;
        font-weight: 500;
        color: #137333;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏛️ Madisonville Branch")
st.markdown('<p class="big-font">Sacrament Talk Scheduler</p>', unsafe_allow_html=True)
st.caption("Clean & Simple • Designed for the Branch Presidency")


@st.cache_resource
def get_cached_store():
    return build_store()


try:
    store = get_cached_store()
except SpreadsheetMissing:
    st.error("Cannot find 'Branch Talk Tracker' sheet. Please create it with 'Members' and 'Talks' tabs.")
    st.stop()
except StoreError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:
    st.error(f"Could not open the branch data store: {exc}")
    st.stop()

if store.kind == "sqlite":
    st.sidebar.caption("This session is storing data in a local database.")


def load_members():
    return store.load_members()


def save_members(df):
    store.save_members(df)


def load_talks():
    return store.load_talks()


def save_talks(df):
    store.save_talks(df)


def pretty_date(value):
    parsed = to_date(value)
    if parsed is None:
        return ""
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def sync_new_event_end():
    if not st.session_state.get("new_end_touched"):
        st.session_state.new_end = st.session_state.new_start
    elif st.session_state.new_end < st.session_state.new_start:
        st.session_state.new_end = st.session_state.new_start


def mark_new_event_end_touched():
    st.session_state.new_end_touched = True
    if st.session_state.new_end < st.session_state.new_start:
        st.session_state.new_end = st.session_state.new_start


def event_chip_html(events, day):
    rows = events_on_day(events, day)
    if rows.empty:
        return ""
    chips = []
    for row in rows.itertuples(index=False):
        title = html.escape(str(row.title))
        span = event_span(row.start_date, row.end_date)
        range_html = ""
        tooltip = title
        if span and span[0] != span[1]:
            label = format_span_label(span[0], span[1])
            tooltip = html.escape(f"{row.title} ({label})")
            range_html = f"<span class='event-range'>{html.escape(label)}</span>"
        chips.append(f"<span class='event-name' title='{tooltip}'>{title}{range_html}</span>")
    return "".join(chips)


def render_events_page():
    st.markdown('<p class="medium-font">Branch Events</p>', unsafe_allow_html=True)
    st.write(
        "Add a branch activity with a start date and an end date. "
        "A one-day event uses the same date for both. "
        "The end date is included, so a Nauvoo trip shows on every day it covers."
    )
    flash = st.session_state.pop("flash", None)
    if flash:
        st.markdown(f'<p class="success-msg">{html.escape(flash)}</p>', unsafe_allow_html=True)
    if st.session_state.pop("clear_new_event_text", False):
        st.session_state.new_event_title = ""
        st.session_state.new_event_notes = ""
    if st.session_state.pop("reset_event_editor", False):
        st.session_state.pop("event_to_edit", None)
        st.session_state.pending_delete = None

    today = date.today()
    if "new_start" not in st.session_state:
        st.session_state.new_start = today
    if "new_end" not in st.session_state:
        st.session_state.new_end = st.session_state.new_start
    if "new_end_touched" not in st.session_state:
        st.session_state.new_end_touched = False
    if "new_event_title" not in st.session_state:
        st.session_state.new_event_title = ""
    if "new_event_notes" not in st.session_state:
        st.session_state.new_event_notes = ""

    st.subheader("Add an Event")
    st.text_input("Event name", key="new_event_title", placeholder="Nauvoo Trip")
    col1, col2 = st.columns(2)
    with col1:
        st.date_input("Start date", key="new_start", on_change=sync_new_event_end)
    with col2:
        st.date_input("End date", key="new_end", on_change=mark_new_event_end_touched)
    st.text_input("Notes (optional)", key="new_event_notes", placeholder="Meet at the chapel")
    day_count = inclusive_day_count(st.session_state.new_start, st.session_state.new_end)
    if day_count == 1:
        st.caption("This event will show on 1 day.")
    elif day_count > 1:
        st.caption(f"This event will show on {day_count} days, including the end date.")
    if st.button("➕ Add This Event", use_container_width=True):
        try:
            store.add_event(
                st.session_state.new_event_title,
                st.session_state.new_start,
                st.session_state.new_end,
                st.session_state.new_event_notes,
            )
        except EventValidationError as exc:
            st.error(str(exc))
        else:
            name = st.session_state.new_event_title.strip()
            st.session_state.flash = f"Added {name}."
            st.session_state.clear_new_event_text = True
            st.session_state.reset_event_editor = True
            st.rerun()

    events = store.load_events()
    st.subheader("Existing Events")
    if events.empty:
        st.info("No events yet.")
        return

    ordered = events.copy()
    ordered["_sort"] = ordered["start_date"].map(lambda value: to_date(value) or date.max)
    ordered = ordered.sort_values(["_sort", "title"], kind="mergesort")
    show = pd.DataFrame(
        {
            "Event": ordered["title"],
            "Starts": ordered["start_date"].map(pretty_date),
            "Ends": ordered["end_date"].map(pretty_date),
            "Days": [
                inclusive_day_count(start, end)
                for start, end in zip(ordered["start_date"], ordered["end_date"])
            ],
            "Notes": ordered["notes"],
        }
    )
    st.dataframe(show, use_container_width=True, hide_index=True)

    labels = {
        str(row.event_id): f"{row.title} — {pretty_date(row.start_date)} to {pretty_date(row.end_date)}"
        for row in ordered.itertuples(index=False)
    }
    selected_id = st.selectbox(
        "Select an event to edit or delete",
        list(labels),
        format_func=lambda event_id: labels[event_id],
        key="event_to_edit",
    )
    selected = events.loc[events["event_id"].astype(str) == str(selected_id)].iloc[0]
    title_key = f"edit_title_{selected_id}"
    start_key = f"edit_start_{selected_id}"
    end_key = f"edit_end_{selected_id}"
    notes_key = f"edit_notes_{selected_id}"
    if title_key not in st.session_state:
        title_value = selected["title"]
        st.session_state[title_key] = "" if pd.isna(title_value) else str(title_value)
    if start_key not in st.session_state:
        st.session_state[start_key] = to_date(selected["start_date"]) or today
    if end_key not in st.session_state:
        st.session_state[end_key] = to_date(selected["end_date"]) or st.session_state[start_key]
    if notes_key not in st.session_state:
        notes_value = selected["notes"]
        st.session_state[notes_key] = "" if pd.isna(notes_value) else str(notes_value)

    st.subheader("Edit Event")
    st.text_input("Event name", key=title_key)
    edit_cols = st.columns(2)
    with edit_cols[0]:
        st.date_input("Start date", key=start_key)
    with edit_cols[1]:
        st.date_input("End date", key=end_key)
    st.text_input("Notes (optional)", key=notes_key)
    edit_count = inclusive_day_count(st.session_state[start_key], st.session_state[end_key])
    if edit_count == 1:
        st.caption("This event will show on 1 day.")
    elif edit_count > 1:
        st.caption(f"This event will show on {edit_count} days, including the end date.")
    else:
        st.caption("End date must be on or after the start date.")

    save_col, delete_col = st.columns(2)
    with save_col:
        if st.button("Save Changes", use_container_width=True):
            try:
                store.update_event(
                    selected_id,
                    st.session_state[title_key],
                    st.session_state[start_key],
                    st.session_state[end_key],
                    st.session_state[notes_key],
                )
            except EventValidationError as exc:
                st.error(str(exc))
            else:
                st.session_state.flash = "Event saved."
                st.rerun()
    with delete_col:
        if st.button("Delete Event", use_container_width=True):
            st.session_state.pending_delete = str(selected_id)

    if str(st.session_state.get("pending_delete")) == str(selected_id):
        st.warning("Delete this event? It will disappear from every day it covers.")
        confirm_col, keep_col = st.columns(2)
        with confirm_col:
            if st.button("Confirm Delete", use_container_width=True):
                try:
                    store.delete_event(selected_id)
                except EventValidationError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.flash = "Event deleted."
                    st.session_state.reset_event_editor = True
                    st.rerun()
        with keep_col:
            if st.button("Keep Event", use_container_width=True):
                st.session_state.pending_delete = None
                st.rerun()


# Sidebar
st.sidebar.markdown("### Navigation")
min_months = st.sidebar.slider("Minimum months between talks", 3, 12, 4)
show_category = st.sidebar.radio("Show", ["All Members", "Adults Only", "Youth Only"])

menu = st.sidebar.radio(
    "Choose a page:",
    [
        "🏠 Home & Suggestions",
        "👥 Add Members",
        "📅 Schedule or Record Talks",
        "📌 Branch Events",
        "📆 Calendar View",
        "📊 Monthly Report",
    ],
)

# ====================== HOME & SUGGESTIONS ======================
if menu == "🏠 Home & Suggestions":
    st.markdown('<p class="medium-font">Who should speak next? (Longest wait first)</p>', unsafe_allow_html=True)
    members = load_members()
    talks = load_talks()
    if members.empty:
        st.info("Add members first on the 'Add Members' page.")
    else:
        active = members[members.get("is_active", "TRUE") == "TRUE"]
        if show_category == "Adults Only":
            active = active[active.get("category", "Adult") == "Adult"]
        elif show_category == "Youth Only":
            active = active[active.get("category", "Adult") == "Youth"]
        suggestions = []
        for _, row in active.iterrows():
            name = row["full_name"]
            past = talks[talks["full_name"] == name] if not talks.empty else pd.DataFrame()
            if past.empty:
                days = 9999
                last_str = "Never spoken"
            else:
                last_date = past["talk_date"].max()
                days = (datetime.today() - last_date).days
                last_str = last_date.strftime("%b %d, %Y")
            suggestions.append({"Name": name, "Days Since Last Talk": days, "Last Talk": last_str, "Category": row.get("category", "Adult")})
        df = pd.DataFrame(suggestions).sort_values("Days Since Last Talk", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)

# ====================== ADD MEMBERS ======================
elif menu == "👥 Add Members":
    st.markdown('<p class="medium-font">Add Branch Members</p>', unsafe_allow_html=True)

    members = load_members()
    if not members.empty:
        st.dataframe(members[["full_name", "category"]], use_container_width=True, hide_index=True)

    st.subheader("📤 Upload CSV File (from Ward Directory)")
    st.write("CSV must have a column named 'Full Name' or 'Name'")
    uploaded_file = st.file_uploader("Choose CSV file", type=["csv"])
    if uploaded_file is not None:
        try:
            df_upload = pd.read_csv(uploaded_file)
            name_col = None
            for possible in ["Full Name", "Name", "Member Name", "full_name", "name"]:
                if possible in df_upload.columns:
                    name_col = possible
                    break
            if name_col is None:
                st.error("Could not find a 'Full Name' or 'Name' column in the CSV.")
            else:
                new_names = df_upload[name_col].dropna().astype(str).str.strip().unique()
                new_rows = pd.DataFrame([{"full_name": n, "category": "Adult", "is_active": "TRUE"} for n in new_names if n])
                if not new_rows.empty:
                    members = pd.concat([members, new_rows], ignore_index=True).drop_duplicates(subset=["full_name"])
                    save_members(members)
                    st.markdown(f'<p class="success-msg">✅ Successfully added {len(new_rows)} members!</p>', unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Error reading CSV: {str(e)}")

    st.subheader("Add One Member Manually")
    col1, col2 = st.columns(2)
    with col1:
        new_name = st.text_input("Full Name (First Last)", placeholder="John Smith")
    with col2:
        new_cat = st.selectbox("Category", ["Adult", "Youth"])
    if st.button("➕ Add This Member", use_container_width=True) and new_name.strip():
        new_row = pd.DataFrame([{"full_name": new_name.strip(), "category": new_cat, "is_active": "TRUE"}])
        members = pd.concat([members, new_row], ignore_index=True).drop_duplicates(subset=["full_name"])
        save_members(members)
        st.markdown('<p class="success-msg">✅ Member added successfully!</p>', unsafe_allow_html=True)

    st.subheader("Bulk Paste Names (one per line)")
    bulk_names = st.text_area("Paste names here", height=100, placeholder="John Smith\nJane Doe\n...")
    if st.button("➕ Add All Pasted Names (as Adults)", use_container_width=True) and bulk_names.strip():
        names_list = [line.strip() for line in bulk_names.splitlines() if line.strip()]
        new_rows = pd.DataFrame([{"full_name": n, "category": "Adult", "is_active": "TRUE"} for n in names_list])
        members = pd.concat([members, new_rows], ignore_index=True).drop_duplicates(subset=["full_name"])
        save_members(members)
        st.markdown(f'<p class="success-msg">✅ Added {len(names_list)} members!</p>', unsafe_allow_html=True)

# ====================== SCHEDULE OR RECORD TALKS ======================
elif menu == "📅 Schedule or Record Talks":
    st.markdown('<p class="medium-font">Schedule or Record Talks for a Sunday</p>', unsafe_allow_html=True)
    talk_date = st.date_input("Select Sunday Date", value=datetime.today())
    status = st.radio("Status", ["Scheduled", "Completed"], horizontal=True)
    members = load_members()
    member_list = ["— None —"] + sorted(members["full_name"].tolist())

    for i in range(1, 4):
        st.subheader(f"Speaker {i}")
        speaker = st.selectbox(f"Choose Speaker {i}", member_list, key=f"sp_{i}")
        topic = st.text_input(f"Topic (optional)", key=f"top_{i}")
        if speaker != "— None —" and st.button(f"Save Speaker {i}", key=f"save_{i}", use_container_width=True):
            new_talk = pd.DataFrame([{
                "talk_date": talk_date.strftime("%Y-%m-%d"),
                "full_name": speaker,
                "topic": topic,
                "status": status,
                "speaker_number": i
            }])
            talks = load_talks()
            talks = pd.concat([talks, new_talk], ignore_index=True)
            save_talks(talks)
            st.markdown(f'<p class="success-msg">✅ Speaker {i} saved for {talk_date.strftime("%B %d, %Y")}!</p>', unsafe_allow_html=True)

# ====================== BRANCH EVENTS ======================
elif menu == "📌 Branch Events":
    render_events_page()

# ====================== CALENDAR VIEW (Clean Google Calendar style + selection box) ======================
elif menu == "📆 Calendar View":
    st.markdown('<p class="medium-font">📆 Monthly Calendar</p>', unsafe_allow_html=True)
    st.caption(
        "Green labels are branch events. A multi-day event is listed on every day from its start date through its end date. "
        "Add or change events on the Branch Events page."
    )
    talks = load_talks()
    events = store.load_events()

    # Set Sunday as first day of the week
    cal_module.setfirstweekday(cal_module.SUNDAY)

    if "calendar_month" not in st.session_state:
        st.session_state.calendar_month = datetime.today().replace(day=1)

    col1, col2, col3, col4 = st.columns([1, 2, 1, 1])
    with col1:
        if st.button("← Previous Month", use_container_width=True):
            st.session_state.calendar_month -= timedelta(days=32)
            st.session_state.calendar_month = st.session_state.calendar_month.replace(day=1)
            st.rerun()
    with col2:
        st.markdown(f"<h2 style='text-align:center; color:#1a73e8;'>{st.session_state.calendar_month.strftime('%B %Y')}</h2>", unsafe_allow_html=True)
    with col3:
        if st.button("Next Month →", use_container_width=True):
            st.session_state.calendar_month += timedelta(days=32)
            st.session_state.calendar_month = st.session_state.calendar_month.replace(day=1)
            st.rerun()
    with col4:
        if st.button("Today", use_container_width=True):
            st.session_state.calendar_month = datetime.today().replace(day=1)
            st.rerun()

    current = st.session_state.calendar_month
    cal = cal_module.monthcalendar(current.year, current.month)

    # Find all Sundays in this month for the dropdown
    sundays_this_month = []
    for week in cal:
        for d in week:
            if d > 0 and datetime(current.year, current.month, d).weekday() == 6:
                date_str = datetime(current.year, current.month, d).strftime("%Y-%m-%d")
                day_talks = talks[talks["talk_date"].dt.strftime("%Y-%m-%d") == date_str] if not talks.empty else pd.DataFrame()
                if not day_talks.empty:
                    sp_names = ", ".join(day_talks.sort_values("speaker_number")["full_name"].tolist())
                    label = f"{current.strftime('%b')} {d} — {sp_names}"
                else:
                    label = f"{current.strftime('%b')} {d} — Open"
                event_names = titles_on_day(events, date(current.year, current.month, d))
                if event_names:
                    label += " | " + ", ".join(event_names)
                sundays_this_month.append((date_str, label))

    # Selection box (dropdown) at the top
    st.subheader("Select a Sunday to add or edit speakers")
    if sundays_this_month:
        selected_option = st.selectbox(
            "Choose Sunday",
            options=[date for date, label in sundays_this_month],
            format_func=lambda x: next(label for date, label in sundays_this_month if date == x),
            key="sunday_selector"
        )
        if st.button("Load Selected Sunday", use_container_width=True):
            st.session_state.selected_date = selected_option
            st.rerun()
    else:
        st.info("No Sundays in this month.")

    # Clean calendar grid (all markdown for perfect square alignment)
    cols = st.columns(7)
    for i, day_name in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        cols[i].markdown(f"**{day_name}**", unsafe_allow_html=True)

    for week in cal:
        row_cols = st.columns(7)
        for i, day in enumerate(week):
            if day == 0:
                row_cols[i].markdown("")
                continue

            date_obj = datetime(current.year, current.month, day)
            is_sunday = date_obj.weekday() == 6
            is_first_sunday = (day == min([d for week in cal for d in week if d > 0 and datetime(current.year, current.month, d).weekday() == 6], default=0))
            info = describe_day(
                date_obj,
                talks,
                events,
                is_sunday=is_sunday,
                is_first_sunday=is_first_sunday,
            )

            if info["fast_sunday"]:
                body = "<strong>Fast & Testimony Meeting</strong>"
                extra = "fast-sunday"
            elif info["speakers"]:
                body = "<br>".join(
                    [f"<span class='speaker'>• {html.escape(str(speaker))}</span>" for speaker in info["speakers"]]
                )
                extra = "has-talk"
            elif info["open_sunday"]:
                body = "<span class='open-text'>— Open —</span>"
                extra = "has-event" if info["titles"] else ""
            elif info["titles"]:
                body = ""
                extra = "has-event"
            else:
                body = "<span class='open-text'>—</span>"
                extra = ""

            chips = event_chip_html(events, date_obj)
            content = f"{day}<br>{body}{chips}"
            row_cols[i].markdown(
                f'<div class="calendar-day {extra}"><span class="date-number">{content}</span></div>',
                unsafe_allow_html=True
            )

    month_start, month_end = month_bounds(current.year, current.month)
    month_events = events_overlapping(events, month_start, month_end)
    st.subheader("Events this month")
    if month_events.empty:
        st.info("No branch events this month.")
    else:
        month_table = pd.DataFrame(
            {
                "Event": month_events["title"],
                "Starts": month_events["start_date"].map(pretty_date),
                "Ends": month_events["end_date"].map(pretty_date),
                "Days": [
                    inclusive_day_count(start, end)
                    for start, end in zip(month_events["start_date"], month_events["end_date"])
                ],
                "Notes": month_events["notes"],
            }
        )
        st.dataframe(month_table, use_container_width=True, hide_index=True)

    # Form appears below the calendar when a Sunday is selected
    if "selected_date" in st.session_state:
        selected = st.session_state.selected_date
        st.subheader(f"➕ Add Speakers for {selected}")
        members = load_members()
        member_list = ["— None —"] + sorted(members["full_name"].tolist())
        status = st.radio("Status", ["Scheduled", "Completed"], horizontal=True, key=f"status_{selected}")

        sp1 = st.selectbox("Speaker 1", member_list, key=f"sp1_{selected}")
        sp2 = st.selectbox("Speaker 2", member_list, key=f"sp2_{selected}")
        sp3 = st.selectbox("Speaker 3", member_list, key=f"sp3_{selected}")

        col_save, col_cancel = st.columns(2)
        with col_save:
            if st.button("Save Speakers for this Sunday", use_container_width=True):
                talks = load_talks()
                added = 0
                for idx, sp in enumerate([sp1, sp2, sp3], 1):
                    if sp != "— None —":
                        new = pd.DataFrame([{
                            "talk_date": selected,
                            "full_name": sp,
                            "topic": "",
                            "status": status,
                            "speaker_number": idx
                        }])
                        talks = pd.concat([talks, new], ignore_index=True)
                        added += 1
                if added > 0:
                    save_talks(talks)
                    st.success(f"✅ Added {added} speaker(s) to {selected}")
                    del st.session_state.selected_date
                    st.rerun()
                else:
                    st.warning("No speakers were selected.")
        with col_cancel:
            if st.button("Cancel", use_container_width=True):
                del st.session_state.selected_date
                st.rerun()

# ====================== MONTHLY REPORT ======================
elif menu == "📊 Monthly Report":
    st.markdown('<p class="medium-font">Talks Given by Month</p>', unsafe_allow_html=True)
    talks = load_talks()
    if talks.empty:
        st.info("No talks recorded yet.")
    else:
        talks["talk_date"] = pd.to_datetime(talks["talk_date"])
        talks["Month"] = talks["talk_date"].dt.strftime("%B %Y")
        report = talks.groupby("Month")["full_name"].count().reset_index()
        report.columns = ["Month", "Number of Talks"]
        st.dataframe(report.sort_values("Month", ascending=False), use_container_width=True, hide_index=True)

        if st.button("Download Full Report as CSV", use_container_width=True):
            talks.to_csv("Madisonville_Talk_Report.csv", index=False)
            st.success("Report downloaded as Madisonville_Talk_Report.csv")

st.caption("❤️ Madisonville Branch • Private tool for the Branch Presidency • Sunday now first day of week")
