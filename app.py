import os
import re
import urllib.request
import urllib.error
import urllib.parse
import json
import hashlib
from datetime import datetime, date, timedelta
import calendar as calendar_module
from uuid import uuid4

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required,
)
from werkzeug.security import generate_password_hash, check_password_hash
from ics import Calendar

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-later")

database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uni_helper.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

AUTO_SYNC_MINUTES = 30


# ============================
# Database Models
# ============================
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    feed_url = db.Column(db.Text, default="")
    last_sync = db.Column(db.DateTime, nullable=True)
    sync_status = db.Column(db.String(30), default="never")
    sync_message = db.Column(db.Text, default="Not synchronized yet.")

    tasks = db.relationship("Task", backref="user", lazy=True, cascade="all, delete-orphan")

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.String(40), default="")


class Task(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    group_id = db.Column(db.String(64), nullable=False)
    title = db.Column(db.Text, nullable=False)
    base_title = db.Column(db.Text, default="")
    course_full = db.Column(db.String(40), default="OTHER")
    course_prefix = db.Column(db.String(20), default="OTHER")
    due_raw = db.Column(db.String(20), default="")
    kind = db.Column(db.String(30), default="due")
    is_deadline = db.Column(db.Boolean, default=True)
    source = db.Column(db.String(30), default="manual")
    created_at = db.Column(db.String(40), default="")
    source_uid = db.Column(db.String(255), default="")
    source_hash = db.Column(db.String(64), default="")
    source_updated = db.Column(db.String(80), default="")
    sync_state = db.Column(db.String(30), default="active")
    last_seen_sync = db.Column(db.String(40), default="")


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def ensure_schema_columns():
    """Add synchronization columns to existing SQLite/Postgres databases safely."""
    migrations = {
        "user": {
            "sync_status": "VARCHAR(30) DEFAULT 'never'",
            "sync_message": "TEXT DEFAULT 'Not synchronized yet.'",
        },
        "task": {
            "source_uid": "VARCHAR(255) DEFAULT ''",
            "source_hash": "VARCHAR(64) DEFAULT ''",
            "source_updated": "VARCHAR(80) DEFAULT ''",
            "sync_state": "VARCHAR(30) DEFAULT 'active'",
            "last_seen_sync": "VARCHAR(40) DEFAULT ''",
        },
    }

    inspector = db.inspect(db.engine)
    for table_name, columns in migrations.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, definition in columns.items():
            if column_name not in existing:
                db.session.execute(text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                ))
    db.session.commit()


with app.app_context():
    db.create_all()
    ensure_schema_columns()


# ============================
# Task Load / Save helpers
# ============================
def task_to_dict(t):
    return {
        "id": t.id,
        "group_id": t.group_id,
        "title": t.title,
        "base_title": t.base_title,
        "course_full": t.course_full,
        "course_prefix": t.course_prefix,
        "due_raw": t.due_raw,
        "kind": t.kind,
        "is_deadline": t.is_deadline,
        "source": t.source,
        "created_at": t.created_at,
        "source_uid": t.source_uid or "",
        "source_hash": t.source_hash or "",
        "source_updated": t.source_updated or "",
        "sync_state": t.sync_state or "active",
        "last_seen_sync": t.last_seen_sync or "",
    }


def load_tasks():
    if not current_user.is_authenticated:
        return []

    tasks = Task.query.filter_by(user_id=current_user.id).all()
    return [task_to_dict(t) for t in tasks]


def add_task(task_data):
    task = Task(
        id=task_data.get("id") or uuid4().hex,
        user_id=current_user.id,
        group_id=task_data.get("group_id", ""),
        title=task_data.get("title", ""),
        base_title=task_data.get("base_title", ""),
        course_full=task_data.get("course_full", "OTHER"),
        course_prefix=task_data.get("course_prefix", "OTHER"),
        due_raw=task_data.get("due_raw", ""),
        kind=task_data.get("kind", "due"),
        is_deadline=task_data.get("is_deadline", True),
        source=task_data.get("source", "manual"),
        created_at=task_data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        source_uid=task_data.get("source_uid", ""),
        source_hash=task_data.get("source_hash", ""),
        source_updated=task_data.get("source_updated", ""),
        sync_state=task_data.get("sync_state", "active"),
        last_seen_sync=task_data.get("last_seen_sync", ""),
    )

    db.session.add(task)
    db.session.commit()


def load_feed_url():
    if not current_user.is_authenticated:
        return ""
    return current_user.feed_url or ""


def save_feed_url(feed_url):
    current_user.feed_url = feed_url.strip()
    db.session.commit()


def load_last_sync():
    if not current_user.is_authenticated:
        return None
    return current_user.last_sync


def save_sync_result(status, message, successful=False):
    current_user.sync_status = status
    current_user.sync_message = message
    if successful:
        current_user.last_sync = datetime.now()
    db.session.commit()


# ============================
# Dates + labels
# ============================
def parse_iso_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def pretty_date(iso):
    d = parse_iso_date(iso)
    return d.strftime("%b %d, %Y") if d else ""


def days_until(iso):
    d = parse_iso_date(iso)
    if not d:
        return None
    return (d - date.today()).days


def status_for(iso):
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
# Course extraction
# ============================
COURSE_REGEX = re.compile(
    r"\b(ECOR|SYSC|MATH|PHYS|CHEM|STAT|ELEC|COMP|BIOL|PHIL|PSCI|CCDP|ARCC|ARCS|ARCU|ARTH|BUSI|CGSC|CLCV|CRCJ|ENGL|GEOG|HIST|LING|PHIL|PSYC|SOCI)\s*[-]?\s*(\d{4})([A-Z]?)\b",
    re.IGNORECASE,
)


def normalize_course(m):
    dept = (m.group(1) or "").upper()
    num = (m.group(2) or "").upper()
    suf = (m.group(3) or "").upper()
    return f"{dept}{num}{suf}"


def course_prefix(course_full):
    return course_full[:4].upper() if course_full and len(course_full) >= 4 else ""


def extract_course_from_text(text):
    if not text:
        return ""

    m = COURSE_REGEX.search(text)
    if not m:
        return ""

    return normalize_course(m)


def extract_course_from_event(event):
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
    re.IGNORECASE,
)


def parse_month_day_year(s):
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def get_event_date_iso(event):
    try:
        if getattr(event, "begin", None):
            d = event.begin.date()
            if isinstance(d, date):
                return d.isoformat()
    except Exception:
        pass

    try:
        if getattr(event, "end", None):
            d = event.end.date()
            if isinstance(d, date):
                return d.isoformat()
    except Exception:
        pass

    desc = (getattr(event, "description", "") or "")
    m = DATE_IN_TEXT_REGEX.search(desc)
    if m:
        d = parse_month_day_year(m.group(1))
        if d:
            return d.isoformat()

    return ""


# ============================
# Kind detection
# ============================
JUNK_KEYWORDS = [
    "lecture",
    "lec",
    "office hour",
    "drop-in",
    "seminar",
    "class time",
    "zoom",
    "meeting",
]

ASSESSMENT_WORDS = [
    "assignment",
    "quiz",
    "midterm",
    "exam",
    "test",
    "lab",
    "report",
    "project",
    "tutorial",
]


def detect_kind(text):
    t = (text or "").lower()

    if any(w in t for w in JUNK_KEYWORDS):
        return "junk"

    if "availability ends" in t or "closes" in t or "close" in t:
        return "closes"
    if "available" in t or "opens" in t:
        return "opens"
    if "due" in t or "deadline" in t:
        return "due"

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


def base_title(title):
    s = (title or "").strip()
    s2 = s

    for pat in SUFFIX_PATTERNS:
        s2 = re.sub(pat, "", s2, flags=re.IGNORECASE)

    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2 if s2 else s


def make_group_id(course_full, base):
    raw = f"{course_full.upper()}|{base.lower()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ============================
# Convert raw tasks to grouped cards
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

        if k == "opens" and d:
            if (not g["opens_raw"]) or (d < g["opens_raw"]):
                g["opens_raw"] = d
        elif k == "due" and d:
            if (not g["due_raw"]) or (d < g["due_raw"]):
                g["due_raw"] = d
        elif k == "closes" and d:
            if (not g["closes_raw"]) or (d < g["closes_raw"]):
                g["closes_raw"] = d

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

        has_due = bool(c.get("due_raw"))

        if has_due:
            c2["show_opens"] = False
            c2["show_closes"] = False
        else:
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
    def is_archived(c):
        dleft = days_until(c.get("main_due_raw", ""))
        return dleft is not None and dleft < -7

    if view == "archive":
        return [c for c in cards if is_archived(c)]

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

    if deadlines_only == "no":
        out = []

    return out


def get_course_options(raw_tasks):
    prefixes = sorted({
        t.get("course_prefix")
        for t in raw_tasks
        if t.get("course_prefix") and t.get("course_prefix") != "OTHER"
    })

    fulls = sorted({
        t.get("course_full")
        for t in raw_tasks
        if t.get("course_full") and t.get("course_full") != "OTHER"
    })

    has_other = any(t.get("course_full") == "OTHER" for t in raw_tasks)

    if has_other:
        prefixes.append("OTHER")

    return prefixes, fulls


# ============================
# ICS ingestion
# ============================
def _event_updated_text(event):
    for attr in ("last_modified", "created"): 
        try:
            value = getattr(event, attr, None)
            if value:
                return str(value)
        except Exception:
            pass
    return ""


def _event_uid(event, course_full, title, kind):
    uid = (getattr(event, "uid", "") or "").strip()
    if uid:
        return uid[:255]
    raw = f"{course_full}|{base_title(title)}|{kind}".lower().encode("utf-8")
    return "fallback-" + hashlib.sha1(raw).hexdigest()


def _event_hash(event, title, due_raw, course_full, kind):
    description = (getattr(event, "description", "") or "").strip()
    location = (getattr(event, "location", "") or "").strip()
    updated = _event_updated_text(event)
    raw = json.dumps({
        "title": title,
        "description": description,
        "location": location,
        "due": due_raw,
        "course": course_full,
        "kind": kind,
        "updated": updated,
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def import_calendar_string(ics_text, mark_missing=True):
    """Upsert a full Brightspace feed and return detailed synchronization counts."""
    cal = Calendar(ics_text)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = date.today() - timedelta(days=7)

    existing = Task.query.filter_by(user_id=current_user.id, source="ics").all()
    existing_by_uid = {t.source_uid: t for t in existing if t.source_uid}
    seen_uids = set()
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "ignored": 0}

    for event in cal.events:
        title = (getattr(event, "name", "") or "").strip()
        if not title:
            counts["ignored"] += 1
            continue

        desc = (getattr(event, "description", "") or "")
        kind = detect_kind(title + " " + desc)
        if kind == "junk":
            counts["ignored"] += 1
            continue

        due_raw = get_event_date_iso(event)
        due_date = parse_iso_date(due_raw)
        if not due_raw or (due_date and due_date < cutoff):
            counts["ignored"] += 1
            continue

        course_full = extract_course_from_event(event) or "OTHER"
        cprefix = "OTHER" if course_full == "OTHER" else course_prefix(course_full)
        base = base_title(title)
        gid = make_group_id(course_full, base)
        source_uid = _event_uid(event, course_full, title, kind)
        source_hash = _event_hash(event, title, due_raw, course_full, kind)
        source_updated = _event_updated_text(event)
        seen_uids.add(source_uid)

        task = existing_by_uid.get(source_uid)
        if task:
            changed = task.source_hash != source_hash or task.sync_state != "active"
            task.group_id = gid
            task.title = title
            task.base_title = base
            task.course_full = course_full
            task.course_prefix = cprefix
            task.due_raw = due_raw
            task.kind = kind
            task.is_deadline = True
            task.source_hash = source_hash
            task.source_updated = source_updated
            task.sync_state = "active"
            task.last_seen_sync = now_text
            counts["updated" if changed else "unchanged"] += 1
        else:
            task = Task(
                id=uuid4().hex, user_id=current_user.id, group_id=gid, title=title,
                base_title=base, course_full=course_full, course_prefix=cprefix,
                due_raw=due_raw, kind=kind, is_deadline=True, source="ics",
                created_at=now_text, source_uid=source_uid, source_hash=source_hash,
                source_updated=source_updated, sync_state="active", last_seen_sync=now_text,
            )
            db.session.add(task)
            existing_by_uid[source_uid] = task
            counts["added"] += 1

    if mark_missing:
        for task in existing:
            task_date = parse_iso_date(task.due_raw)
            if task.source_uid and task.source_uid not in seen_uids and (not task_date or task_date >= cutoff):
                db.session.delete(task)
                counts["removed"] += 1

    db.session.commit()
    return counts


def validate_feed_url(feed_url):
    try:
        parsed = urllib.parse.urlparse(feed_url.strip())
    except Exception:
        return False
    return parsed.scheme == "https" and parsed.hostname == "brightspace.carleton.ca"


def fetch_feed_ics(feed_url):
    if not validate_feed_url(feed_url):
        raise ValueError("Use the HTTPS calendar subscription link from Carleton Brightspace.")

    req = urllib.request.Request(feed_url, headers={"User-Agent": "NexoraSync/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        data = resp.read()

    text_data = data.decode("utf-8", errors="ignore")
    if "BEGIN:VCALENDAR" not in text_data:
        raise ValueError("Brightspace did not return a valid calendar feed.")
    return text_data


def should_auto_sync():
    last = load_last_sync()
    return last is None or datetime.now() - last >= timedelta(minutes=AUTO_SYNC_MINUTES)


def perform_feed_sync():
    feed_url = load_feed_url()
    if not feed_url:
        raise ValueError("Connect your Brightspace calendar feed first.")

    ics_text = fetch_feed_ics(feed_url)
    counts = import_calendar_string(ics_text, mark_missing=True)
    summary = (
        f"Sync complete: {counts['added']} added, {counts['updated']} updated, "
        f"{counts['removed']} removed, {counts['unchanged']} unchanged."
    )
    save_sync_result("success", summary, successful=True)
    return counts, summary


def auto_sync_if_needed():
    if not load_feed_url() or not should_auto_sync():
        return None
    try:
        counts, _ = perform_feed_sync()
        return counts
    except Exception as exc:
        app.logger.warning("Automatic Brightspace sync failed: %s", exc)
        save_sync_result("error", f"Automatic sync failed: {exc}", successful=False)
        return None


# ============================
# ROUTES
# ============================
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/api/copilot/status", methods=["GET"])
@login_required
def copilot_status():
    """Report Gemini configuration without exposing any secret value."""
    connected = bool(
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    return jsonify({
        "connected": connected,
        "provider": "Gemini",
        "model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    })


@app.route("/api/copilot", methods=["POST"])
@login_required
def copilot():
    """Conversational, semester-aware Nexora AI powered by Gemini."""
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("message") or "").strip()
    if not prompt:
        return jsonify({"error": "Ask me something first."}), 400

    tasks = sort_cards(build_grouped_cards(load_tasks()))
    upcoming = []
    overdue = []
    for task in tasks:
        left = days_until(task.get("main_due_raw", ""))
        if left is not None and left < 0:
            overdue.append(task)
        elif left is not None and left <= 14:
            upcoming.append(task)

    history = payload.get("history") or []
    if not isinstance(history, list):
        history = []
    history = history[-10:]
    first_name = (current_user.name or "Student").split()[0]

    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        return jsonify({
            "error": "Gemini is not connected to this service. Add GEMINI_API_KEY to the Render web service, then redeploy."
        }), 503

    semester_context = {
        "student_first_name": first_name,
        "today": date.today().isoformat(),
        "tasks": [{
            "title": task.get("title", "Task"),
            "course": task.get("course_full", "OTHER"),
            "due": task.get("main_due_raw", ""),
        } for task in tasks[:40]],
    }

    contents = []
    for item in history:
        role = item.get("role") if isinstance(item, dict) else None
        content = item.get("content") if isinstance(item, dict) else None
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content[:4000]}],
            })
    contents.append({"role": "user", "parts": [{"text": prompt[:6000]}]})

    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
    system_instruction = (
        "You are Nexora AI, a high-quality conversational and academic assistant for university students. "
        "Answer naturally, directly, and intelligently. Explain academic topics, brainstorm, write, reason, "
        "and help plan the student's semester. Use the supplied semester context only when relevant. Never "
        "invent course dates, deadlines, grades, or facts that are absent from the context. Be concise by "
        "default but provide detail when requested. Do not repeat a canned introduction. The current version "
        "does not have live web search, so clearly say when a question requires current information rather "
        "than pretending it is up to date.\n\n"
        f"CURRENT STUDENT CONTEXT:\n{json.dumps(semester_context, ensure_ascii=False)}"
    )

    body = json.dumps({
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1200,
        },
    }).encode("utf-8")
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model, safe='')}:generateContent?key="
        f"{urllib.parse.quote(api_key, safe='')}"
    )
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))

        candidates = result.get("candidates") or []
        if not candidates:
            block_reason = ((result.get("promptFeedback") or {}).get("blockReason") or "").strip()
            if block_reason:
                return jsonify({"error": f"Gemini could not answer this request ({block_reason})."}), 400
            raise ValueError("Gemini returned no candidates")

        parts = (((candidates[0].get("content") or {}).get("parts")) or [])
        answer = "\n".join(
            part.get("text", "").strip()
            for part in parts
            if isinstance(part, dict) and part.get("text", "").strip()
        ).strip()
        if not answer:
            raise ValueError("Gemini returned no text")

        return jsonify({
            "answer": answer,
            "mode": "gemini",
            "grounded": False,
            "sources": [],
            "meta": {"upcoming": len(upcoming), "overdue": len(overdue), "tasks": len(tasks)},
        })
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        app.logger.error("Gemini API HTTP %s: %s", exc.code, detail[:1600])
        if exc.code == 429:
            message = "Gemini's rate limit is temporarily exhausted. Wait a moment and try again."
            status = 429
        elif exc.code in (401, 403):
            message = "Gemini rejected the API key. Check GEMINI_API_KEY in Render."
            status = 502
        elif exc.code == 400:
            message = "Gemini rejected this request or model. Check GEMINI_MODEL in Render."
            status = 502
        else:
            message = f"Gemini returned an API error ({exc.code}). Please try again."
            status = 502
        return jsonify({"error": message}), status
    except urllib.error.URLError as exc:
        app.logger.error("Gemini connection error: %s", str(exc)[:1200])
        return jsonify({"error": "Nexora could not reach Gemini right now. Please try again."}), 502
    except Exception as exc:
        app.logger.exception("Gemini Copilot error: %s", exc)
        return jsonify({"error": "Gemini could not respond right now. Please try again."}), 502


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not name or not email or not password:
            return render_template("signup.html", error="Please fill in all fields.")

        existing_user = User.query.filter_by(email=email).first()

        if existing_user:
            return render_template("signup.html", error="An account with this email already exists.")

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
        )

        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for("home"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            return render_template("login.html", error="Invalid email or password.")

        login_user(user)
        return redirect(url_for("home"))

    return render_template("login.html")

@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html", user=current_user)

@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", user=current_user)


@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    if request.method == "POST":
        message = (request.form.get("message") or "").strip()

        if not message:
            return render_template("feedback.html", user=current_user, error="Please write feedback before sending.")

        fb = Feedback(
            user_id=current_user.id,
            message=message,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        db.session.add(fb)
        db.session.commit()

        return render_template("feedback.html", user=current_user, success="Thanks. Your feedback was sent.")

    return render_template("feedback.html", user=current_user)
@app.route("/admin/feedback")
@login_required
def admin_feedback():
    # Change this email to YOUR real admin email
    admin_email = "bassemtamer2016@gmail.com"

    if current_user.email.lower() != admin_email.lower():
        return redirect(url_for("home"))

    feedback_messages = Feedback.query.order_by(Feedback.id.desc()).all()

    return render_template(
        "admin_feedback.html",
        user=current_user,
        feedback_messages=feedback_messages
    )

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("landing"))


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def home():
    view = request.args.get("view", "today")
    course_filter = request.args.get("course", "all")
    deadlines_only = request.args.get("deadlines", "yes")

    auto_sync_if_needed()
    raw_tasks = load_tasks()

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

            add_task({
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

        return redirect(url_for("home", view=view, course=course_filter, deadlines=deadlines_only))

    cards = build_grouped_cards(raw_tasks)
    cards = apply_filters(cards, course_filter, deadlines_only)
    cards = view_filter(cards, view)
    cards = sort_cards(cards)
    groups = group_for_ui(cards)

    urgent = len(groups["overdue"]) + len(groups["today"]) + len(groups["panic"])
    week = urgent + len(groups["soon"])

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

    # Dashboard V2 helper data
    dashboard_cards = sort_cards(view_filter(build_grouped_cards(raw_tasks), "all"))

    course_map = {}

    for c in dashboard_cards:
        course = c.get("course_full") or "OTHER"

        if course not in course_map:
            course_map[course] = {
                "course": course,
                "total": 0,
                "next_title": "",
                "next_due": "",
                "progress": 0,
            }

        course_map[course]["total"] += 1

        if not course_map[course]["next_title"]:
            course_map[course]["next_title"] = c.get("title", "Task")
            course_map[course]["next_due"] = c.get("main_due_pretty", "")

    course_cards = list(course_map.values())[:4]

    for course_card in course_cards:
        total = course_card["total"]
        course_card["progress"] = min(95, max(25, 100 - (total * 8)))

    task_preview = dashboard_cards[:6]

    upcoming_cards = []

    for c in dashboard_cards:
        dleft = days_until(c.get("main_due_raw", ""))

        if dleft is not None:
            upcoming_cards.append({
                "title": c.get("title", "Task"),
                "course_full": c.get("course_full", "OTHER"),
                "due_label": c.get("status_label", ""),
                "due_pretty": c.get("main_due_pretty", ""),
                "status": c.get("status", ""),
            })

    upcoming_cards = upcoming_cards[:4]

    today = date.today()
    month_title = today.strftime("%B %Y")
    month_days = []

    first_day = date(today.year, today.month, 1)
    last_day_num = calendar_module.monthrange(today.year, today.month)[1]

    deadline_days = set()

    for c in dashboard_cards:
        d = parse_iso_date(c.get("main_due_raw", ""))

        if d and d.year == today.year and d.month == today.month:
            deadline_days.add(d.day)

    for day_num in range(1, last_day_num + 1):
        month_days.append({
            "day": day_num,
            "is_today": day_num == today.day,
            "has_deadline": day_num in deadline_days,
        })


    summary = {
        "urgent": urgent,
        "week": week,
        "reminders": reminders,
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
        user=current_user,
        course_cards=course_cards,
        task_preview=task_preview,
        upcoming_cards=upcoming_cards,
        month_title=month_title,
        month_days=month_days,
    )


@app.route("/calendar")
@login_required
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
            "tasks": tasks_by_date.get(iso, []),
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
        user=current_user,
    )


@app.route("/connect-brightspace")
@login_required
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
        last_sync_text=last_sync_text,
        sync_status=current_user.sync_status or "never",
        sync_message=current_user.sync_message or "Not synchronized yet.",
        user=current_user,
    )


@app.post("/delete/<group_id>")
@login_required
def delete_group(group_id):
    Task.query.filter_by(user_id=current_user.id, group_id=str(group_id)).delete()
    db.session.commit()

    view = request.args.get("view", "today")
    course_filter = request.args.get("course", "all")
    deadlines_only = request.args.get("deadlines", "yes")

    return redirect(url_for("home", view=view, course=course_filter, deadlines=deadlines_only))


@app.post("/import")
@login_required
def import_ics_file():
    file = request.files.get("ics_file")

    if not file or file.filename == "":
        return redirect(url_for("home"))

    if not file.filename.lower().endswith(".ics"):
        return redirect(url_for("home"))

    raw = file.read().decode("utf-8", errors="ignore")
    try:
        counts = import_calendar_string(raw, mark_missing=False)
        save_sync_result(
            "success",
            f"Backup import complete: {counts['added']} added and {counts['updated']} updated.",
            successful=True,
        )
    except Exception as exc:
        save_sync_result("error", f"Backup import failed: {exc}")
    return redirect(url_for("connect_brightspace"))


@app.post("/save_feed")
@login_required
def save_feed():
    feed_url = (request.form.get("feed_url") or "").strip()
    if not feed_url:
        save_sync_result("error", "Paste your Brightspace calendar feed link first.")
        return redirect(url_for("connect_brightspace"))
    if not validate_feed_url(feed_url):
        save_sync_result("error", "That is not a valid Carleton Brightspace HTTPS calendar feed link.")
        return redirect(url_for("connect_brightspace"))

    save_feed_url(feed_url)
    try:
        perform_feed_sync()
    except Exception as exc:
        app.logger.warning("Initial Brightspace sync failed: %s", exc)
        save_sync_result("error", f"Link saved, but synchronization failed: {exc}")
    return redirect(url_for("connect_brightspace"))


@app.get("/sync")
@login_required
def sync_now():
    try:
        perform_feed_sync()
    except Exception as exc:
        app.logger.warning("Manual Brightspace sync failed: %s", exc)
        save_sync_result("error", f"Synchronization failed: {exc}")
    return redirect(url_for("connect_brightspace"))


if __name__ == "__main__":
    app.run(debug=True)
