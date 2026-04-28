import json
import os
import re
import urllib.request
import hashlib
from datetime import datetime, date, timedelta
import calendar as calendar_module
from uuid import uuid4

from flask import Flask, render_template, request, redirect, url_for
from ics import Calendar

app = Flask(__name__)

DATA_FILE = "tasks.json"
FEED_FILE = "feed_url.txt"

SYNC_FILE = "last_sync.txt"
AUTO_SYNC_MINUTES = 30

# ============================
# Load / Save
# ============================
def load_tasks():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save_tasks(tasks):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)


def load_feed_url():
    if not os.path.exists(FEED_FILE):
        return ""
    return open(FEED_FILE, "r", encoding="utf-8").read().strip()


def save_feed_url(feed_url: str):
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(feed_url.strip())


# ============================
# Dates + labels
# ============================
def parse_iso_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def pretty_date(iso: str) -> str:
    d = parse_iso_date(iso)
    return d.strftime("%b %d, %Y") if d else ""


def days_until(iso: str):
    d = parse_iso_date(iso)
    if not d:
        return None
    return (d - date.today()).days


def status_for(iso: str):
    dleft = days_until(iso)
    if dleft is None:
        return ("nodate", "No date")
    if dleft < 0:
        return ("overdue", "Overdue")
    if dleft == 0:
        return ("today", "Due today")
    if dleft <= 3:
        return ("panic", f"Due in {dleft} day" + ("s" if dleft != 1 else ""))
    if dleft <= 7:
        return ("soon", f"Due in {dleft} days")
    return ("later", f"Due in {dleft} days")


# ============================
# Course extraction (robust)
# ============================
COURSE_REGEX = re.compile(
    r"\b(ECOR|SYSC|MATH|PHYS|CHEM|STAT|ELEC|COMP)\s*[-]?\s*(\d{4})([A-Z]?)\b",
    re.IGNORECASE,
)

def normalize_course(m) -> str:
    dept = (m.group(1) or "").upper()
    num = (m.group(2) or "").upper()
    suf = (m.group(3) or "").upper()
    return f"{dept}{num}{suf}"

def course_prefix(course_full: str) -> str:
    return course_full[:4].upper() if course_full and len(course_full) >= 4 else ""

def extract_course_from_text(text: str) -> str:
    if not text:
        return ""
    m = COURSE_REGEX.search(text)
    if not m:
        return ""
    return normalize_course(m)

def extract_course_from_event(event) -> str:
    title = (getattr(event, "name", "") or "").strip()
    desc = (getattr(event, "description", "") or "").strip()
    loc = (getattr(event, "location", "") or "").strip()
    uid = (getattr(event, "uid", "") or "").strip()

    cats = ""
    try:
        categories = getattr(event, "categories", None)
        if categories:
            cats = " ".join([str(x) for x in categories])
    except Exception:
        cats = ""

    blob = " ".join([title, desc, loc, uid, cats]).strip()
    return extract_course_from_text(blob)


# ============================
# Date extraction
# ============================
DATE_IN_TEXT_REGEX = re.compile(
    r"\b(?:Due|Deadline|Available|Availability Ends|Closes|Opens)\s*:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b",
    re.IGNORECASE
)

def parse_month_day_year(s: str):
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def get_event_date_iso(event) -> str:
    # 1) begin
    try:
        if getattr(event, "begin", None):
            d = event.begin.date()
            if isinstance(d, date):
                return d.isoformat()
    except Exception:
        pass

    # 2) end
    try:
        if getattr(event, "end", None):
            d = event.end.date()
            if isinstance(d, date):
                return d.isoformat()
    except Exception:
        pass

    # 3) description text
    desc = (getattr(event, "description", "") or "")
    m = DATE_IN_TEXT_REGEX.search(desc)
    if m:
        d = parse_month_day_year(m.group(1))
        if d:
            return d.isoformat()

    return ""


# ============================
# Kind detection (opens / due / closes)
# ============================
JUNK_KEYWORDS = [
    "lecture", "lec", "office hour", "drop-in",
    "seminar", "class time", "zoom", "meeting"
]

ASSESSMENT_WORDS = [
    "assignment", "quiz", "midterm", "exam", "test",
    "lab", "report", "project", "tutorial"
]

def detect_kind(text: str) -> str:
    t = (text or "").lower()

    if any(w in t for w in JUNK_KEYWORDS):
        return "junk"

    if "availability ends" in t or "closes" in t or "close" in t:
        return "closes"
    if "available" in t or "opens" in t:
        return "opens"
    if "due" in t or "deadline" in t:
        return "due"

    # Brightspace often omits "due" but the title is clearly an assessment
    if any(w in t for w in ASSESSMENT_WORDS):
        return "due"

    return "general"


# ============================
# Grouping helpers
# ============================
SUFFIX_PATTERNS = [
    r"\s*-\s*available\s*$",
    r"\s*-\s*due\s*$",
    r"\s*-\s*availability\s*ends\s*$",
    r"\s*\(\s*available\s*\)\s*$",
    r"\s*\(\s*due\s*\)\s*$",
    r"\s*\(\s*availability\s*ends\s*\)\s*$",
]

def base_title(title: str) -> str:
    s = (title or "").strip()
    s2 = s
    for pat in SUFFIX_PATTERNS:
        s2 = re.sub(pat, "", s2, flags=re.IGNORECASE)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2 if s2 else s

def make_group_id(course_full: str, base: str) -> str:
    raw = f"{course_full.upper()}|{base.lower()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ============================
# Convert raw tasks -> grouped cards
# Main deadline rule: Due > Closes > Opens
# ============================
def build_grouped_cards(raw_tasks):
    grouped = {}

    for t in raw_tasks:
        if t.get("kind") == "junk":
            continue

        course_full = (t.get("course_full") or "OTHER").strip().upper() or "OTHER"
        cprefix = t.get("course_prefix") or ("OTHER" if course_full == "OTHER" else course_prefix(course_full))

        base = t.get("base_title") or base_title(t.get("title", ""))
        gid = t.get("group_id") or make_group_id(course_full, base)

        if gid not in grouped:
            grouped[gid] = {
                "group_id": gid,
                "title": base,
                "course_full": course_full,
                "course_prefix": cprefix,

                "opens_raw": "",
                "due_raw": "",
                "closes_raw": "",
                "main_due_raw": "",

                "is_deadline": True,
                "source": t.get("source", "ics"),
            }

        g = grouped[gid]
        k = t.get("kind", "general")
        d = t.get("due_raw", "")

        # Earliest date wins (in case duplicates exist)
        if k == "opens" and d:
            if (not g["opens_raw"]) or (d < g["opens_raw"]):
                g["opens_raw"] = d
        elif k == "due" and d:
            if (not g["due_raw"]) or (d < g["due_raw"]):
                g["due_raw"] = d
        elif k == "closes" and d:
            if (not g["closes_raw"]) or (d < g["closes_raw"]):
                g["closes_raw"] = d
        else:
            # general: keep it grouped but don't override opens/due/closes
            pass

    # Decide main deadline
    for g in grouped.values():
        if g["due_raw"]:
            g["main_due_raw"] = g["due_raw"]
        elif g["closes_raw"]:
            g["main_due_raw"] = g["closes_raw"]
        elif g["opens_raw"]:
            g["main_due_raw"] = g["opens_raw"]
        else:
            g["main_due_raw"] = ""

    return list(grouped.values())


# ============================
# UI buckets
# ============================
def group_for_ui(cards):
    groups = {"overdue": [], "today": [], "panic": [], "soon": [], "later": [], "nodate": []}

    for c in cards:
        status, label = status_for(c.get("main_due_raw", ""))
        c2 = dict(c)

        c2["status"] = status
        c2["status_label"] = label

        c2["main_due_pretty"] = pretty_date(c.get("main_due_raw", ""))

        c2["due_pretty"] = pretty_date(c.get("due_raw", ""))
        c2["opens_pretty"] = pretty_date(c.get("opens_raw", ""))
        c2["closes_pretty"] = pretty_date(c.get("closes_raw", ""))

        # -------- FILTER LOGIC (the important part) --------
        has_due = bool(c.get("due_raw"))

        # If it HAS a due date: hide opens/closes (students only need the deadline)
        if has_due:
            c2["show_opens"] = False
            c2["show_closes"] = False
        else:
            # If no due date, only show opens/closes if they add info (not same as deadline)
            main = c.get("main_due_raw", "")
            c2["show_opens"] = bool(c.get("opens_raw")) and (c.get("opens_raw") != main)
            c2["show_closes"] = bool(c.get("closes_raw")) and (c.get("closes_raw") != main)

        groups[status].append(c2)

    return groups


def sort_cards(cards):
    def key(c):
        d = parse_iso_date(c.get("main_due_raw", ""))
        if d is None:
            return (1, date.max, (c.get("title") or "").lower())
        return (0, d, (c.get("title") or "").lower())
    return sorted(cards, key=key)


def view_filter(cards, view):
    # Archive rule:
    # More than 7 days overdue = archived
    def is_archived(c):
        dleft = days_until(c.get("main_due_raw", ""))
        return dleft is not None and dleft < -7

    if view == "archive":
        return [c for c in cards if is_archived(c)]

    # Normal views hide archived tasks
    cards = [c for c in cards if not is_archived(c)]

    if view == "today":
        out = []
        for c in cards:
            dleft = days_until(c.get("main_due_raw", ""))
            if dleft is None or dleft <= 0:
                out.append(c)
        return out

    if view == "week":
        out = []
        for c in cards:
            dleft = days_until(c.get("main_due_raw", ""))
            if dleft is None:
                continue
            if dleft <= 7:
                out.append(c)
        return out

    return cards


def apply_filters(cards, course_filter, deadlines_only):
    out = cards

    if course_filter and course_filter != "all":
        cf = course_filter.strip().upper()
        if len(cf) == 4 and cf.isalpha():
            out = [c for c in out if (c.get("course_prefix") or "").upper() == cf]
        else:
            out = [c for c in out if (c.get("course_full") or "").upper() == cf]

    # optional toggle (calendar cards are basically all deadlines)
    if deadlines_only == "no":
        out = []

    return out


def get_course_options(raw_tasks):
    prefixes = sorted({t.get("course_prefix") for t in raw_tasks if t.get("course_prefix") and t.get("course_prefix") != "OTHER"})
    fulls = sorted({t.get("course_full") for t in raw_tasks if t.get("course_full") and t.get("course_full") != "OTHER"})
    has_other = any(t.get("course_full") == "OTHER" for t in raw_tasks)
    if has_other:
        prefixes.append("OTHER")
    return prefixes, fulls


# ============================
# ICS ingestion
# ============================
def import_calendar_string(ics_text: str) -> int:
    tasks = load_tasks()

    existing_keys = {
        (
            (t.get("title", "") or "").lower(),
            t.get("due_raw", ""),
            (t.get("course_full", "") or "").upper(),
            t.get("kind", ""),
            t.get("source", "")
        )
        for t in tasks
    }

    cal = Calendar(ics_text)
    imported = 0

    oldest_allowed = date.today() - timedelta(days=14)

    for event in cal.events:
        title = (getattr(event, "name", "") or "").strip()
        if not title:
            continue

        desc = (getattr(event, "description", "") or "")
        kind = detect_kind(title + " " + desc)

        if kind == "junk":
            continue

        due_raw = get_event_date_iso(event)

        # Skip undated events to avoid "No date" spam
        if not due_raw:
            continue

        d = parse_iso_date(due_raw)
        if d and d < date.today():
            continue

        course_full = extract_course_from_event(event) or "OTHER"
        cprefix = "OTHER" if course_full == "OTHER" else course_prefix(course_full)

        base = base_title(title)
        gid = make_group_id(course_full, base)

        key = (title.lower(), due_raw, course_full.upper(), kind, "ics")
        if key in existing_keys:
            continue

        tasks.append({
            "id": uuid4().hex,
            "group_id": gid,
            "title": title,
            "base_title": base,
            "course_full": course_full,
            "course_prefix": cprefix,
            "due_raw": due_raw,
            "kind": kind,
            "is_deadline": True,
            "source": "ics",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        existing_keys.add(key)
        imported += 1

    save_tasks(tasks)
    return imported


def fetch_feed_ics(feed_url: str) -> str:
    if not feed_url.startswith("https://brightspace.carleton.ca/"):
        return ""

    req = urllib.request.Request(feed_url, headers={"User-Agent": "UniHelper/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="ignore")


def load_last_sync():
    if not os.path.exists(SYNC_FILE):
        return None
    try:
        raw = open(SYNC_FILE, "r", encoding="utf-8").read().strip()
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def save_last_sync():
    with open(SYNC_FILE, "w", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def should_auto_sync():
    last = load_last_sync()
    if last is None:
        return True
    return datetime.now() - last >= timedelta(minutes=AUTO_SYNC_MINUTES)


def auto_sync_if_needed():
    feed_url = load_feed_url()
    if not feed_url:
        return 0

    if not should_auto_sync():
        return 0

    try:
        ics_text = fetch_feed_ics(feed_url)
        if ics_text:
            imported = import_calendar_string(ics_text)
            save_last_sync()
            return imported
    except Exception:
        return 0

    return 0

# ============================
# ROUTES
# ============================
@app.route("/", methods=["GET", "POST"])
def home():
    view = request.args.get("view", "today")
    course_filter = request.args.get("course", "all")
    deadlines_only = request.args.get("deadlines", "yes")

    auto_sync_if_needed()
    raw_tasks = load_tasks()

    # Manual add (optional)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        course_input = (request.form.get("course") or "").strip()
        due_raw = (request.form.get("due") or "").strip()

        if title and due_raw:
            detected = extract_course_from_text(course_input) or extract_course_from_text(title)
            course_full = detected or (course_input.upper().strip() if course_input else "OTHER")
            course_full = course_full.strip().upper() or "OTHER"
            cprefix = "OTHER" if course_full == "OTHER" else course_prefix(course_full)

            base = base_title(title)
            gid = make_group_id(course_full, base)

            raw_tasks.append({
                "id": uuid4().hex,
                "group_id": gid,
                "title": title,
                "base_title": base,
                "course_full": course_full,
                "course_prefix": cprefix,
                "due_raw": due_raw,
                "kind": "due",
                "is_deadline": True,
                "source": "manual",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            save_tasks(raw_tasks)

        return redirect(url_for("home", view=view, course=course_filter, deadlines=deadlines_only))

    cards = build_grouped_cards(raw_tasks)
    cards = apply_filters(cards, course_filter, deadlines_only)
    cards = view_filter(cards, view)
    cards = sort_cards(cards)
    groups = group_for_ui(cards)

    urgent = len(groups["overdue"]) + len(groups["today"]) + len(groups["panic"])
    week = urgent + len(groups["soon"])

    # Next upcoming deadline for hero card
    all_cards = sort_cards(build_grouped_cards(raw_tasks))
    next_deadline = None

    for c in all_cards:
        dleft = days_until(c.get("main_due_raw", ""))
        if dleft is not None and dleft >= 0:
            next_deadline = {
                "title": c.get("title", "Task"),
                "course": c.get("course_full", ""),
                "date": pretty_date(c.get("main_due_raw", "")),
                "status": "Due today" if dleft == 0 else f"Due in {dleft} day" + ("s" if dleft != 1 else "")
            }
            break

    summary = {
        "urgent": urgent,
        "week": week,
        "next_deadline": next_deadline
    }
    reminders = []

    for c in cards:
        dleft = days_until(c.get("main_due_raw", ""))
        title = c.get("title", "Task")
        course = c.get("course_full", "")

        name = f"{course}: {title}" if course else title

        if dleft is not None:
            if dleft < 0:
                reminders.append(f"{name} is overdue.")
            elif dleft == 0:
                reminders.append(f"{name} is due today.")
            elif dleft == 1:
                reminders.append(f"{name} is due tomorrow.")
            elif dleft <= 3:
                reminders.append(f"{name} is due in {dleft} days.")

    reminders = reminders[:5]

    summary = {
        "urgent": urgent,
        "week": week,
        "reminders": reminders
    }

    prefixes, fulls = get_course_options(raw_tasks)
    feed_url = load_feed_url()
    last_sync = load_last_sync()

    if last_sync:
        last_sync_text = last_sync.strftime("%b %d, %Y at %I:%M %p")
    else:
        last_sync_text = "Never"

    return render_template(
        "index.html",
        groups=groups,
        view=view,
        summary=summary,
        prefixes=prefixes,
        fulls=fulls,
        course_filter=course_filter,
        deadlines_only=deadlines_only,
        feed_url=feed_url,
        last_sync_text=last_sync_text,
    )



@app.route("/calendar")
def calendar_page():
    month_raw = request.args.get("month", "")

    try:
        current_month = datetime.strptime(month_raw, "%Y-%m").date()
    except ValueError:
        today = date.today()
        current_month = date(today.year, today.month, 1)

    year = current_month.year
    month = current_month.month

    first_day = date(year, month, 1)
    last_day_num = calendar_module.monthrange(year, month)[1]
    last_day = date(year, month, last_day_num)

    prev_month = (first_day - timedelta(days=1)).replace(day=1)
    next_month = (last_day + timedelta(days=1)).replace(day=1)

    raw_tasks = load_tasks()
    cards = sort_cards(build_grouped_cards(raw_tasks))

    tasks_by_date = {}

    for c in cards:
        dleft = days_until(c.get("main_due_raw", ""))
        if dleft is not None and dleft < -7:
            continue

        due = parse_iso_date(c.get("main_due_raw", ""))
        if not due:
            continue

        if due.year == year and due.month == month:
            iso = due.isoformat()
            if iso not in tasks_by_date:
                tasks_by_date[iso] = []

            course = c.get("course_full", "") or "OTHER"
            color_index = sum(ord(ch) for ch in course) % 8

            tasks_by_date[iso].append({
                "title": c.get("title", "Task"),
                "course": course,
                "color_index": color_index,
                "status": status_for(c.get("main_due_raw", ""))[1],
            })

    weeks = []
    week = []

    # Monday = 0, Sunday = 6
    start_blank_days = first_day.weekday()

    for _ in range(start_blank_days):
        week.append(None)

    for day_num in range(1, last_day_num + 1):
        d = date(year, month, day_num)
        iso = d.isoformat()

        week.append({
            "day": day_num,
            "iso": iso,
            "is_today": d == date.today(),
            "tasks": tasks_by_date.get(iso, [])
        })

        if len(week) == 7:
            weeks.append(week)
            week = []

    if week:
        while len(week) < 7:
            week.append(None)
        weeks.append(week)

    return render_template(
        "calendar.html",
        weeks=weeks,
        month_title=current_month.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m"),
    )

@app.route("/connect-brightspace")
def connect_brightspace():
    feed_url = load_feed_url()
    last_sync = load_last_sync()

    if last_sync:
        last_sync_text = last_sync.strftime("%b %d, %Y at %I:%M %p")
    else:
        last_sync_text = "Never"

    return render_template(
        "connect_brightspace.html",
        feed_url=feed_url,
        last_sync_text=last_sync_text
    )



@app.post("/delete/<group_id>")
def delete_group(group_id):
    tasks = load_tasks()
    tasks = [t for t in tasks if str(t.get("group_id")) != str(group_id)]
    save_tasks(tasks)

    view = request.args.get("view", "today")
    course_filter = request.args.get("course", "all")
    deadlines_only = request.args.get("deadlines", "yes")

    return redirect(url_for("home", view=view, course=course_filter, deadlines=deadlines_only))


@app.post("/import")
def import_ics_file():
    file = request.files.get("ics_file")
    if not file or file.filename == "":
        return redirect(url_for("home"))

    if not file.filename.lower().endswith(".ics"):
        return redirect(url_for("home"))

    raw = file.read().decode("utf-8", errors="ignore")
    import_calendar_string(raw)
    return redirect(url_for("home", view="all"))


@app.post("/save_feed")
def save_feed():
    feed_url = (request.form.get("feed_url") or "").strip()
    if feed_url:
        save_feed_url(feed_url)
    return redirect(url_for("home", view="all"))


@app.get("/sync")
def sync_now():
    feed_url = load_feed_url()
    if feed_url:
        ics_text = fetch_feed_ics(feed_url)
        if ics_text:
            import_calendar_string(ics_text)
            save_last_sync()
    return redirect(url_for("home", view="all"))


if __name__ == "__main__":
    app.run(debug=True)
