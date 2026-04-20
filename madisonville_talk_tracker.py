import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
import calendar as cal_module

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
    }
    .has-talk {background-color: #e8f0fe !important; border-color: #1a73e8;}
    .fast-sunday {background-color: #fef7e0 !important; border-color: #fbbc04;}
    .date-number {font-size: 20px; font-weight: bold; color: #202124; display: block; margin-bottom: 4px;}
    .speaker {font-size: 15px; color: #1a73e8; line-height: 1.3;}
    .open-text {font-size: 14px; color: #5f6368;}
    </style>
""", unsafe_allow_html=True)

st.title("🏛️ Madisonville Branch")
st.markdown('<p class="big-font">Sacrament Talk Scheduler</p>', unsafe_allow_html=True)
st.caption("Clean & Simple • Designed for the Branch Presidency")

# ====================== GOOGLE SHEET CONNECTION ======================
@st.cache_resource
def get_google_client():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

client = get_google_client()
SPREADSHEET_NAME = "Branch Talk Tracker"

try:
    sheet = client.open(SPREADSHEET_NAME)
except:
    st.error("Cannot find 'Branch Talk Tracker' sheet. Please create it with 'Members' and 'Talks' tabs.")
    st.stop()

members_ws = sheet.worksheet("Members") if "Members" in [ws.title for ws in sheet.worksheets()] else sheet.add_worksheet("Members", 200, 5)
talks_ws = sheet.worksheet("Talks") if "Talks" in [ws.title for ws in sheet.worksheets()] else sheet.add_worksheet("Talks", 1000, 6)

if len(members_ws.get_all_values()) == 0:
    members_ws.append_row(["full_name", "category", "is_active"])
if len(talks_ws.get_all_values()) == 0:
    talks_ws.append_row(["talk_date", "full_name", "topic", "status", "speaker_number"])

def load_members():
    data = members_ws.get_all_records()
    df = pd.DataFrame(data)
    return df if not df.empty else pd.DataFrame(columns=["full_name", "category", "is_active"])

def load_talks():
    data = talks_ws.get_all_records()
    df = pd.DataFrame(data)
    if not df.empty:
        df["talk_date"] = pd.to_datetime(df["talk_date"])
    return df if not df.empty else pd.DataFrame(columns=["talk_date", "full_name", "topic", "status", "speaker_number"])

def save_members(df):
    members_ws.clear()
    members_ws.append_row(["full_name", "category", "is_active"])
    if not df.empty:
        members_ws.append_rows(df.values.tolist())

def save_talks(df):
    talks_ws.clear()
    talks_ws.append_row(["talk_date", "full_name", "topic", "status", "speaker_number"])
    if not df.empty:
        talks_ws.append_rows(df.values.tolist())

# Sidebar
st.sidebar.markdown("### Navigation")
min_months = st.sidebar.slider("Minimum months between talks", 3, 12, 4)
show_category = st.sidebar.radio("Show", ["All Members", "Adults Only", "Youth Only"])

menu = st.sidebar.radio("Choose a page:", 
    ["🏠 Home & Suggestions", 
     "👥 Add Members", 
     "📅 Schedule or Record Talks", 
     "📆 Calendar View", 
     "📊 Monthly Report"])

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

# ====================== CALENDAR VIEW (Clean Google Calendar style + selection box) ======================
elif menu == "📆 Calendar View":
    st.markdown('<p class="medium-font">📆 Monthly Calendar</p>', unsafe_allow_html=True)
    talks = load_talks()

    # Set Sunday as first day of the week
    cal_module.setfirstweekday(cal_module.SUNDAY)

    if "calendar_month" not in st.session_state:
        st.session_state.calendar_month = datetime.today().replace(day=1)

    col1, col2, col3, col4 = st.columns([1, 2, 1, 1])
    with col1:
        if st.button("← Previous Month", use_container_width=True):
            st.session_state.calendar_month -= timedelta(days=32)
            st.session_state.calendar_month = st.session_state.calendar_month.replace(day=1)
    with col2:
        st.markdown(f"<h2 style='text-align:center; color:#1a73e8;'>{st.session_state.calendar_month.strftime('%B %Y')}</h2>", unsafe_allow_html=True)
    with col3:
        if st.button("Next Month →", use_container_width=True):
            st.session_state.calendar_month += timedelta(days=32)
            st.session_state.calendar_month = st.session_state.calendar_month.replace(day=1)
    with col4:
        if st.button("Today", use_container_width=True):
            st.session_state.calendar_month = datetime.today().replace(day=1)

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
            date_str = date_obj.strftime("%Y-%m-%d")
            is_sunday = date_obj.weekday() == 6
            is_first_sunday = (day == min([d for week in cal for d in week if d > 0 and datetime(current.year, current.month, d).weekday() == 6], default=0))

            day_talks = talks[talks["talk_date"].dt.strftime("%Y-%m-%d") == date_str] if not talks.empty else pd.DataFrame()

            if not is_sunday:
                content = f"{day}<br><span class='open-text'>—</span>"
                extra = ""
            elif is_first_sunday:
                content = f"{day}<br><strong>Fast & Testimony Meeting</strong>"
                extra = "fast-sunday"
            else:
                if not day_talks.empty:
                    speakers = day_talks.sort_values("speaker_number")["full_name"].tolist()
                    content = f"{day}<br>" + "<br>".join([f"<span class='speaker'>• {s}</span>" for s in speakers])
                    extra = "has-talk"
                else:
                    content = f"{day}<br><span class='open-text'>— Open —</span>"
                    extra = ""

            row_cols[i].markdown(
                f'<div class="calendar-day {extra}"><span class="date-number">{content}</span></div>',
                unsafe_allow_html=True
            )

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