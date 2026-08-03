import os
import re
import urllib.request
import urllib.error
import urllib.parse
import json
import hashlib
import secrets
import io
import time
import zipfile
import base64
from datetime import datetime, date, timedelta
import calendar as calendar_module
from uuid import uuid4
from pathlib import Path

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
from werkzeug.utils import secure_filename
from pypdf import PdfReader
from pptx import Presentation
from docx import Document
from ics import Calendar

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    import pytesseract
except Exception:  # OCR remains optional; PDF/DOCX parsing still works.
    Image = ImageOps = ImageEnhance = ImageFilter = pytesseract = None

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
    extension_token = db.Column(db.String(128), default="")

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
    completed = db.Column(db.Boolean, default=False)


class CourseContent(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER", index=True)
    title = db.Column(db.Text, nullable=False)
    content_type = db.Column(db.String(30), default="resource")
    source_url = db.Column(db.Text, default="")
    source_uid = db.Column(db.String(255), default="", index=True)
    content_text = db.Column(db.Text, default="")
    module_name = db.Column(db.Text, default="")
    published_at = db.Column(db.String(80), default="")
    content_hash = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class GradeEntry(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER", index=True)
    name = db.Column(db.String(255), nullable=False)
    weight = db.Column(db.Float, default=0.0)
    score = db.Column(db.Float, nullable=True)
    max_score = db.Column(db.Float, default=100.0)
    category = db.Column(db.String(80), default="assessment")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CourseOutlineProfile(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER", index=True)
    source_uid = db.Column(db.String(255), default="")
    outline_title = db.Column(db.Text, default="")
    grading_json = db.Column(db.Text, default="{}")
    pass_mark = db.Column(db.Float, default=50.0)
    final_minimum = db.Column(db.Float, nullable=True)
    analysis_notes = db.Column(db.Text, default="")
    analyzed_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StudyArtifact(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER", index=True)
    source_uid = db.Column(db.String(255), default="", index=True)
    source_title = db.Column(db.Text, default="")
    artifact_type = db.Column(db.String(30), default="flashcards")
    title = db.Column(db.Text, default="")
    data_json = db.Column(db.Text, default="{}")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncedFile(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER", index=True)
    source_uid = db.Column(db.String(255), default="", index=True)
    original_source_url = db.Column(db.Text, default="")
    stored_url = db.Column(db.Text, default="")
    file_name = db.Column(db.Text, default="")
    mime_type = db.Column(db.String(255), default="")
    file_size = db.Column(db.Integer, default=0)
    content_hash = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class SyncedCourse(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    source_uid = db.Column(db.String(255), default="", index=True)
    course_code = db.Column(db.String(40), default="OTHER", index=True)
    course_name = db.Column(db.Text, default="")
    term = db.Column(db.String(120), default="")
    instructor = db.Column(db.String(255), default="")
    image_url = db.Column(db.Text, default="")
    brightspace_url = db.Column(db.Text, default="")
    content_count = db.Column(db.Integer, default=0)
    is_hidden = db.Column(db.Boolean, default=False, nullable=False)
    source_hash = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HiddenCourse(db.Model):
    """A per-user tombstone so manually removed courses stay removed after sync."""
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_code = db.Column(db.String(40), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UserGradingScale(db.Model):
    """Persistent per-user university letter-grade mapping."""
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    university_name = db.Column(db.String(255), default="")
    scale_json = db.Column(db.Text, default="[]")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AcademicUpdate(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="OTHER")
    update_type = db.Column(db.String(40), default="content")
    title = db.Column(db.Text, nullable=False)
    detail = db.Column(db.Text, default="")
    related_uid = db.Column(db.String(255), default="")
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def ensure_schema_columns():
    """Add synchronization columns to existing SQLite/Postgres databases safely."""
    migrations = {
        "user": {
            "sync_status": "VARCHAR(30) DEFAULT 'never'",
            "sync_message": "TEXT DEFAULT 'Not synchronized yet.'",
            "extension_token": "VARCHAR(128) DEFAULT ''",
        },
        "synced_course": {
            "is_hidden": "BOOLEAN DEFAULT FALSE",
        },
        "task": {
            "source_uid": "VARCHAR(255) DEFAULT ''",
            "source_hash": "VARCHAR(64) DEFAULT ''",
            "source_updated": "VARCHAR(80) DEFAULT ''",
            "sync_state": "VARCHAR(30) DEFAULT 'active'",
            "last_seen_sync": "VARCHAR(40) DEFAULT ''",
            "completed": "BOOLEAN DEFAULT FALSE",
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
        "completed": bool(t.completed),
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
        completed=task_data.get("completed", False),
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
        if t.get("kind") == "junk" or t.get("completed"):
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




def _gemini_json(system_prompt, user_prompt, max_tokens=4096, temperature=0.2):
    """Call Gemini for strict JSON with retries and a model fallback on transient 5xx errors."""
    api_key = (os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip())
    if not api_key:
        raise RuntimeError("Gemini is not configured")
    preferred = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    fallback = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite").strip()
    models = []
    for m in (preferred, fallback):
        if m and m not in models:
            models.append(m)
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    last_error = None
    for model in models:
        endpoint = ("https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(model, safe='') + ":generateContent?key=" + urllib.parse.quote(api_key, safe=''))
        for attempt in range(3):
            try:
                req = urllib.request.Request(endpoint, data=json.dumps(body).encode('utf-8'), headers={"Content-Type":"application/json"}, method='POST')
                with urllib.request.urlopen(req, timeout=90) as response:
                    result = json.loads(response.read().decode('utf-8'))
                parts = ((((result.get('candidates') or [{}])[0].get('content') or {}).get('parts')) or [])
                text_value = "\n".join(p.get('text','') for p in parts if isinstance(p,dict)).strip()
                if not text_value:
                    raise ValueError('Gemini returned no JSON')
                text_value = re.sub(r'^```(?:json)?\s*|\s*```$', '', text_value, flags=re.I|re.S).strip()
                return json.loads(text_value)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    raise RuntimeError(f"Gemini could not complete the request: {type(last_error).__name__ if last_error else 'unknown error'}")



def _source_chunks(text_value, max_chunks=48):
    """Turn extracted lecture text into useful page/slide-aware chunks."""
    cleaned = _sanitize_extracted_text(text_value)
    if not cleaned:
        return []
    parts = re.split(r'(?=\[(?:Slide|Page)\s+\d+\])', cleaned, flags=re.I)
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        label_match = re.match(r'\[((?:Slide|Page)\s+\d+)\]\s*', part, flags=re.I)
        label = label_match.group(1).title() if label_match else ''
        body = part[label_match.end():].strip() if label_match else part
        # Split very large pages while preserving the citation label.
        paras = [p.strip() for p in re.split(r'\n\s*\n|(?<=[.!?])\s+(?=[A-Z0-9])', body) if p.strip()]
        if not paras:
            paras = [body]
        bucket = ''
        for para in paras:
            candidate = (bucket + '\n' + para).strip()
            if len(candidate) > 2200 and bucket:
                chunks.append({'label': label, 'text': bucket})
                bucket = para
            else:
                bucket = candidate
        if bucket:
            chunks.append({'label': label, 'text': bucket})
        if len(chunks) >= max_chunks:
            break
    return chunks[:max_chunks]


def _key_sentences(text_value, limit=20):
    chunks = _source_chunks(text_value, 60)
    candidates=[]
    for chunk in chunks:
        for sent in re.split(r'(?<=[.!?])\s+|\n+', chunk['text']):
            sent=' '.join(sent.split()).strip(' -•\t')
            if 45 <= len(sent) <= 320 and not re.match(r'^(copyright|page \d+|slide \d+)$', sent, re.I):
                score=0
                if re.search(r'\b(is|are|means|defined as|refers to|used to|because|therefore|formula|equation|important|note)\b', sent, re.I): score+=3
                if re.search(r'[:=]|\b[A-Z][A-Za-z]+\b', sent): score+=1
                score += min(len(sent)//80,3)
                candidates.append((score,chunk['label'],sent))
    seen=set(); out=[]
    for _,label,sent in sorted(candidates,key=lambda x:x[0],reverse=True):
        key=re.sub(r'\W+','',sent.lower())[:140]
        if key in seen: continue
        seen.add(key); out.append({'label':label,'text':sent})
        if len(out)>=limit: break
    return out


def _local_study_artifact(artifact_type, title, text_value):
    """Guaranteed deterministic fallback when Gemini is unavailable."""
    points=_key_sentences(text_value, 24)
    chunks=_source_chunks(text_value, 18)
    source_name=title or 'Course material'
    if artifact_type=='flashcards':
        cards=[]
        for i,p in enumerate(points[:18],1):
            sentence=p['text']
            m=re.match(r'(.{3,80}?)\s+(?:is|are|means|refers to|is defined as)\s+(.+)',sentence,re.I)
            if m:
                front=f"What is {m.group(1).strip()}?"; back=m.group(2).strip()
            else:
                front=f"Explain this key idea from {source_name}: {sentence[:95]}{'…' if len(sentence)>95 else ''}"
                back=sentence
            cards.append({'front':front,'back':back,'source':p['label'] or source_name})
        return {'title':f'{source_name} Flashcards','cards':cards or [{'front':'What is the main idea?','back':_sanitize_extracted_text(text_value)[:500],'source':source_name}]}
    if artifact_type=='quiz':
        cards=_local_study_artifact('flashcards',title,text_value)['cards']
        answers=[c['back'] for c in cards]
        questions=[]
        for i,c in enumerate(cards[:12]):
            distractors=[]
            for j,a in enumerate(answers):
                if j!=i and a not in distractors and a!=c['back']:
                    distractors.append(a)
                if len(distractors)==3: break
            while len(distractors)<3: distractors.append('None of the provided course concepts')
            options=[c['back']]+distractors
            # deterministic rotation keeps correct index varied
            shift=i%4; options=options[shift:]+options[:shift]; answer=(4-shift)%4
            questions.append({'question':c['front'],'options':options,'answer':answer,'explanation':c['back'],'source':c['source']})
        return {'title':f'{source_name} Practice Quiz','questions':questions}
    if artifact_type=='summary':
        concepts=[]
        for p in points[:12]:
            concept=(p['text'].split(':',1)[0][:90] if ':' in p['text'] else 'Key concept')
            concepts.append({'concept':concept,'explanation':p['text'],'source':p['label'] or source_name})
        overview=' '.join(p['text'] for p in points[:3])[:1400] or _sanitize_extracted_text(text_value)[:1400]
        return {'title':f'{source_name} Summary','overview':overview,'key_concepts':concepts,'takeaways':[p['text'] for p in points[:8]]}
    sections=[]
    for idx,ch in enumerate(chunks[:14],1):
        heading=ch['label'] or f'Section {idx}'
        sections.append({'heading':heading,'content':ch['text'][:1800],'source':ch['label'] or source_name})
    return {'title':f'{source_name} Study Guide','sections':sections,'key_takeaways':[p['text'] for p in points[:10]],'practice_prompts':[f"Explain {p['text'][:100]}" for p in points[:6]]}



def _strip_bonus_sections(text_value):
    """Drop paragraphs about bonus marks / extra credit before grading-table extraction.

    These are the single biggest source of false positives: a sentence like "complete the
    quiz to earn your 1%" matches the same "label ... N%" shape as a real grading line, but
    it isn't a graded component at all. Cut the whole bonus section out before scanning.
    """
    lines = text_value.splitlines()
    out = []
    skipping = False
    bonus_start = re.compile(r'\bbonus\b|\bextra\s+credit\b', re.I)
    # A bonus section is short-lived; once we hit a clear new heading (a line that itself
    # looks like a real grading/administrative heading) we stop skipping.
    section_end = re.compile(r'^(evaluation|grading|assessment|schedule|course\s+policies|academic\s+integrity|notes?:)\b', re.I)
    for line in lines:
        stripped = line.strip()
        if not skipping and bonus_start.search(stripped) and len(stripped) < 200:
            skipping = True
            continue
        if skipping:
            if section_end.match(stripped) or not stripped:
                skipping = False
            else:
                continue
        out.append(line)
    return '\n'.join(out)


_JUNK_FRAGMENT_STARTS = re.compile(
    r'^(to|your|you|the|and|but|each|that|which|complete|earn|claim|receive|awarded|if|when|please|will|this|these|it|we|so|for|regardless|log|select|contact)\b',
    re.I,
)


def _looks_like_grading_label(name):
    """Reject sentence-fragment matches that only accidentally look like 'Label ... N%'."""
    lower = name.strip().lower()
    if not lower or len(lower.split()) > 8:
        return False
    if _JUNK_FRAGMENT_STARTS.match(lower):
        return False
    if any(x in lower for x in ['bonus', 'mentorship', 'claim your', 'awarded the', 'earn your', 'quiz to earn']):
        return False
    return True


def _component_category(name):
    lower=str(name or '').lower()
    if 'final' in lower: return 'final'
    if 'test' in lower or 'midterm' in lower: return 'test'
    if 'tutorial' in lower or 'attendance' in lower: return 'attendance'
    if 'lab' in lower: return 'lab'
    if 'assign' in lower: return 'assignment'
    if 'quiz' in lower: return 'quiz'
    if 'project' in lower: return 'project'
    return 'assessment'


def _math1104_outline_analysis(text_value, course=''):
    """Exact deterministic model for the MATH 1104/1107 Winter 2026 multi-option outline."""
    text=_sanitize_extracted_text(text_value)
    signatures=[
        'four options for the evaluation in this course',
        'option 1: the course grade will be assigned based on the final exam only',
        'average of best two written tests',
        'attend at least two tutorials',
    ]
    if not all(x in text.lower() for x in signatures):
        return None
    inputs=[
        {'name':'Test 1','weight':0,'category':'test','default_max':50,'input_label':'Test 1 result','notes':'Enter your earned marks out of 50, or use 30/100 if you prefer a percentage.'},
        {'name':'Test 2','weight':0,'category':'test','default_max':50,'input_label':'Test 2 result','notes':'Leave blank if not written.'},
        {'name':'Test 3','weight':0,'category':'test','default_max':50,'input_label':'Test 3 result','notes':'Leave blank if not written.'},
        {'name':'Tutorial attendance','weight':5,'category':'attendance','default_max':5,'input_label':'Tutorials attended','notes':'Enter the number of eligible tutorials attended, from 0 to 5.'},
        {'name':'Final Exam','weight':0,'category':'final','default_max':100,'input_label':'Final exam result','notes':'Enter the final exam mark out of 100.'},
    ]
    options=[
        {'option_name':'Option 1 — Final exam only','eligibility':{'minimum_written_tests':1},'components':[{'source':'Final Exam','method':'direct','weight':100}]},
        {'option_name':'Option 2 — Best test','eligibility':{'minimum_written_tests':1},'components':[{'source_group':['Test 1','Test 2','Test 3'],'method':'best','count':1,'weight':15},{'source':'Tutorial attendance','method':'direct','weight':5},{'source':'Final Exam','method':'direct','weight':80}]},
        {'option_name':'Option 3 — Best two tests','eligibility':{'minimum_written_tests':2},'components':[{'source_group':['Test 1','Test 2','Test 3'],'method':'best_average','count':2,'weight':30},{'source':'Tutorial attendance','method':'direct','weight':5},{'source':'Final Exam','method':'direct','weight':65}]},
        {'option_name':'Option 4 — All three tests','eligibility':{'minimum_written_tests':3},'components':[{'source_group':['Test 1','Test 2','Test 3'],'method':'average','count':3,'weight':45},{'source':'Tutorial attendance','method':'direct','weight':5},{'source':'Final Exam','method':'direct','weight':50}]},
    ]
    rules=[
        {'type':'any_component_minimum','components':['Test 1','Test 2','Test 3'],'minimum_percent':30,'message':'At least one written test must be 30% or higher.'},
        {'type':'component_minimum','component':'Final Exam','minimum_percent':30,'message':'The final exam must be 30% or higher.'},
        {'type':'overall_minimum','minimum_percent':50,'message':'The total course grade must be 50% or higher.'},
        {'type':'raw_minimum','component':'Tutorial attendance','minimum_raw':2,'message':'At least two eligible tutorials must be attended.'},
    ]
    return {
        'course_title':course or 'MATH 1104F / MATH 1107F',
        'pass_mark':50.0,
        'final_minimum':30.0,
        'components':inputs,
        'input_components':inputs,
        'grading_options':options,
        'pass_rules':rules,
        'component_minimums':[{'name':'Final Exam','minimum':30,'consequence':'Fail the course if the final exam is below 30%'}],
        'rules':[r['message'] for r in rules],
        'important_dates':[],
        'confidence':'high',
        'total_weight':100.0,
        'multiple_options':True,
        'best_option_policy':'Calculate every eligible option and use the highest valid course grade.',
    }


def _extract_generic_options(text_value):
    """Extract simple alternative schemes without merging their weights."""
    text=_strip_bonus_sections(_sanitize_extracted_text(text_value))
    matches=list(re.finditer(r'(?im)^\s*Option\s+(\d+)\s*:\s*(.*)$',text))
    if len(matches)<2:
        return []
    options=[]
    for i,m in enumerate(matches):
        block=text[m.start():matches[i+1].start() if i+1<len(matches) else len(text)]
        comps=[]
        for line in block.splitlines()[1:]:
            line=' '.join(line.split())
            mm=re.search(r'(?P<name>[A-Za-z][A-Za-z0-9 &/()\-]{2,100}?)\s*[:\-–—]?\s*(?P<weight>\d{1,3}(?:\.\d+)?)\s*%',line)
            if not mm: continue
            name=mm.group('name').strip(' :-–—')
            if not _looks_like_grading_label(name): continue
            weight=float(mm.group('weight'))
            if 0<weight<=100:
                comps.append({'source':name,'method':'direct','weight':weight})
        total=sum(c['weight'] for c in comps)
        if comps and 98<=total<=102:
            options.append({'option_name':f"Option {m.group(1)}",'components':comps,'eligibility':{}})
    return options


def _grading_section_candidates(text_value):
    """Return likely grading/evaluation sections, ordered from most focused to full text.

    Outlines use many headings: Assessment Breakdown, Assessment Distribution,
    Grade Distribution, Evaluation and Marking Scheme, Basis of Grade, etc.  We isolate
    those regions first so unrelated percentages (dates, attendance deductions, bonuses,
    textbook prices) cannot become fake grade components.
    """
    text = _strip_bonus_sections(_sanitize_extracted_text(text_value))
    headings = re.compile(
        r'(?im)^\s*(?:\d+[.)]\s*)?(?:evaluation(?:\s+in\s+this\s+course)?|evaluation\s+and\s+marking\s+scheme|'
        r'assessment\s+(?:breakdown|distribution|scheme)|grade\s+(?:distribution|breakdown)|grading\s+(?:scheme|breakdown)|'
        r'marking\s+(?:scheme|breakdown)|basis\s+of\s+(?:the\s+)?(?:final\s+)?grade|method\s+of\s+evaluation|'
        r'course\s+evaluation|final\s+grade\s+(?:calculation|breakdown)|how\s+your\s+grade\s+is\s+calculated)\s*:?[ \t]*$'
    )
    starts=[m.start() for m in headings.finditer(text)]
    sections=[]
    stop_heading=re.compile(
        r'(?im)^\s*(?:\d+[.)]\s*)?(?:academic\s+integrity|academic\s+dates|appeals?|copyright|accommodations?|'
        r'course\s+schedule|schedule\s+of\s+lectures|learning\s+outcomes|textbooks?|communications?|policies)\s*:?[ \t]*$'
    )
    for pos in starts:
        tail=text[pos:pos+14000]
        stop=stop_heading.search(tail, 250)
        section=tail[:stop.start()] if stop else tail
        if len(section.strip())>=40:
            sections.append(section)
    # A compact fallback around any line containing three or more percentages often catches
    # tables whose heading was lost during PDF extraction.
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if len(re.findall(r'\b\d{1,3}(?:\.\d+)?\s*%', line)) >= 2:
            sections.append('\n'.join(lines[max(0,i-8):min(len(lines),i+18)]))
    sections.append(text)
    # Deduplicate while preserving order.
    out=[]; seen=set()
    for section in sections:
        key=hashlib.sha256(section[:12000].encode('utf-8','ignore')).hexdigest()
        if key not in seen:
            seen.add(key); out.append(section)
    return out


def _canonical_component_name(raw):
    raw=' '.join(str(raw or '').replace('|',' ').split()).strip(' :-–—.,;')
    raw=re.sub(r'^(?:item|component|assessment|description|weight|percentage)\s+','',raw,flags=re.I)
    # Prefer a known academic component phrase and discard trailing prose/dates.
    patterns=[
        (r'\bfinal\s+(?:exam(?:ination)?|assessment)\b','Final Exam'),
        (r'\bmid[- ]?term\s+(?:exam\s*)?(?P<n>(?:#?\d+|[ivx]+))\b','Midterm {n}'),
        (r'\bmid[- ]?term\s+(?:exam(?:ination)?)?\b','Midterm'),
        (r'\bterm\s+test\s*(?P<n>(?:#?\d+|[ivx]+))?\b','Term Test {n}'),
        (r'\btest\s*(?P<n>(?:#?\d+|[ivx]+))\b','Test {n}'),
        (r'\b(?:laboratory|lab)\s*(?P<n>\d+)?\b','Lab {n}'),
        (r'\bassignments?\b','Assignments'),
        (r'\bquizzes?\b','Quizzes'),
        (r'\bprojects?\b','Project'),
        (r'\b(?:tutorial\s+)?attendance\b','Tutorial Attendance'),
        (r'\bparticipation\b','Participation'),
        (r'\bproblem\s+analysis(?:\s+sessions?)?\b','Problem Analysis'),
        (r'\b(?:weekly\s+)?homework\b','Homework'),
        (r'\b(?:coursework|term\s+work)\b','Coursework'),
        (r'\bpresentations?\b','Presentation'),
        (r'\bportfolios?\b','Portfolio'),
        (r'\bessays?\b','Essay'),
        (r'\b(?:reflection|discussion)\s+(?:posts?|boards?)\b','Discussion'),
    ]
    lower=raw.lower()
    for pat,label in patterns:
        m=re.search(pat,lower,re.I)
        if m:
            n=(m.groupdict().get('n') or '').replace('#','').upper()
            return label.format(n=n).strip()
    # Generic table label fallback, but reject sentence fragments.
    raw=re.split(r'\b(?:closed\s+book|open\s+book|tentatively|scheduled|due|weekend|description)\b',raw,1,flags=re.I)[0].strip(' :-–—.,;')
    return raw[:80] if _looks_like_grading_label(raw) else ''


def _extract_components_from_section(section):
    components=[]; seen=set()
    lines=[' '.join(x.replace('|',' ').split()) for x in section.splitlines()]

    def add(name, weight):
        if not name or not (0 < weight <= 100): return
        key=re.sub(r'\W+','',name.lower())
        if key in seen: return
        seen.add(key)
        components.append({'name':name,'weight':round(weight,2),'category':_component_category(name),'default_max':100,'notes':''})

    # 1) Same-line rows, e.g. "Midterm 1 ... 20%".
    for line in lines:
        if not line or len(line)>500: continue
        if re.search(r'\b(?:minimum|at least|required to pass|must obtain|must achieve|deduction|price|tax)\b',line,re.I):
            continue
        percents=list(re.finditer(r'(?<![\d/])(\d{1,3}(?:\.\d+)?)\s*%',line))
        for pm in percents:
            weight=float(pm.group(1))
            previous=max([x.end() for x in percents if x.end() < pm.start()] or [max(0,pm.start()-180)])
            name=_canonical_component_name(line[previous:pm.start()].strip())
            add(name,weight)

    # 2) PDF tables often extract each cell on a separate line:
    #    Midterm Exam I / description / 20%.  Track a component label and look ahead.
    for i,line in enumerate(lines):
        if not line or len(line)>180: continue
        name=_canonical_component_name(line)
        if not name: continue
        # A standalone generic heading is not a component.
        if name.lower() in {'assessment','item','description','weight','coursework'}: continue
        for j in range(i+1,min(len(lines),i+7)):
            nxt=lines[j]
            if not nxt: continue
            # Stop if another clear component starts before a percentage appears.
            other=_canonical_component_name(nxt)
            if other and other.lower()!=name.lower() and not re.search(r'\d{1,3}(?:\.\d+)?\s*%',nxt):
                break
            m=re.fullmatch(r'\s*(\d{1,3}(?:\.\d+)?)\s*%\s*',nxt)
            if m:
                add(name,float(m.group(1))); break
    return components


def _extract_generic_options(text_value):
    """Extract alternative grading schemes without combining their totals."""
    text=_strip_bonus_sections(_sanitize_extracted_text(text_value))
    matches=list(re.finditer(r'(?im)^\s*Option\s+(\d+)\s*:\s*(.*)$',text))
    if len(matches)<2: return []
    options=[]
    for i,m in enumerate(matches):
        block=text[m.start():matches[i+1].start() if i+1<len(matches) else len(text)]
        comps=_extract_components_from_section(block)
        total=sum(c['weight'] for c in comps)
        if comps and 98 <= total <= 102:
            options.append({'option_name':f'Option {m.group(1)}','components':[{'source':c['name'],'method':'direct','weight':c['weight']} for c in comps],'eligibility':{}})
    return options


def _local_outline_analysis(text_value, course=''):
    """Section-aware deterministic parser for ordinary and multi-option outlines."""
    special=_math1104_outline_analysis(text_value,course)
    if special: return special
    text=_strip_bonus_sections(_sanitize_extracted_text(text_value))
    options=_extract_generic_options(text)
    best=[]; best_score=-1
    for section in _grading_section_candidates(text):
        comps=_extract_components_from_section(section)
        total=sum(c['weight'] for c in comps)
        # Prefer complete 100% schemes, then schemes close to 100, then component count.
        score=(1000 if 99<=total<=101 else 600 if 95<=total<=105 else 250 if 80<=total<=120 else 0) + len(comps)*10 - abs(100-total)
        if score>best_score:
            best_score=score; best=comps
    components=best
    names={c.get('name','').lower() for c in components}
    if 'midterm' in names and any(n in names for n in {'midterm ii','midterm 2'}):
        for c in components:
            if c.get('name','').lower()=='midterm': c['name']='Midterm I'
    total=round(sum(c['weight'] for c in components),2)

    pass_mark=50.0
    for pat in [
        r'(?:pass(?:ing)?\s+(?:mark|grade)|minimum\s+overall(?:\s+grade)?|total\s+course\s+grade\s+must\s+be\s+at\s+least)\D{0,30}(\d{1,3}(?:\.\d+)?)\s*%',
        r'(\d{1,3}(?:\.\d+)?)\s*%\s+(?:is\s+)?required\s+to\s+pass\s+the\s+course',
    ]:
        m=re.search(pat,text,re.I|re.S)
        if m:
            try: pass_mark=float(m.group(1)); break
            except Exception: pass
    structured_rules=[]; minimums=[]; final_minimum=None
    # Final/component minimum rules in both word orders.
    rule_specs=[
        ('Final Exam',[
            r'(?:must|required\s+to)\s+(?:pass|obtain|earn|achieve).{0,80}?final(?:\s+exam|\s+examination)?.{0,50}?(\d{1,3}(?:\.\d+)?)\s*%',
            r'(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%.{0,90}?final(?:\s+exam|\s+examination)',
            r'final(?:\s+exam|\s+examination).{0,60}?(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%',
        ]),
        ('Labs',[
            r'(?:must|required\s+to).{0,80}?lab(?:s|oratory)?.{0,50}?(\d{1,3}(?:\.\d+)?)\s*%',
            r'lab(?:s|oratory)?.{0,60}?(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%',
        ]),
    ]
    for name,pats in rule_specs:
        value=None
        for pat in pats:
            m=re.search(pat,text,re.I|re.S)
            if m:
                try: value=float(m.group(1)); break
                except Exception: pass
        if value is not None:
            minimums.append({'name':name,'minimum':value,'consequence':f'Must meet the {name} minimum to pass the course'})
            structured_rules.append({'type':'component_minimum','component':name,'minimum_percent':value,'message':f'{name} must be at least {value:g}% to pass.'})
            if name=='Final Exam': final_minimum=value
    structured_rules.append({'type':'overall_minimum','minimum_percent':pass_mark,'message':f'Total course grade must be at least {pass_mark:g}%.'})
    if options:
        # Build a union of source inputs. This avoids flattening four options into 105%.
        union=[]; seen=set()
        for opt in options:
            for c in opt.get('components') or []:
                name=str(c.get('source') or c.get('name') or '').strip()
                key=re.sub(r'\W+','',name.lower())
                if name and key not in seen:
                    seen.add(key); union.append({'name':name,'weight':0,'category':_component_category(name),'default_max':100,'notes':'Used by one or more grading options.'})
        if union: components=union
        total=100.0
    confidence='high' if (options or (components and 99<=sum(float(c.get('weight') or 0) for c in components)<=101)) else 'medium' if components else 'low'
    return {
        'course_title':course,'pass_mark':pass_mark,'final_minimum':final_minimum,
        'components':components,'input_components':components,'component_minimums':minimums,
        'pass_rules':structured_rules,'rules':[r['message'] for r in structured_rules],
        'grading_options':options,'important_dates':[],'confidence':confidence,
        'total_weight':total,'multiple_options':bool(options),
    }


def _outline_rule_minimums(parsed):
    minimums={}
    if not isinstance(parsed,dict): return minimums
    raw=parsed.get('component_minimums') or parsed.get('minimums') or []
    if isinstance(raw,dict):
        raw=[{'name':k,'minimum':v} for k,v in raw.items()]
    if isinstance(raw,list):
        for item in raw:
            if not isinstance(item,dict): continue
            name=str(item.get('name') or item.get('component') or '').strip()
            try: minimum=float(item.get('minimum'))
            except Exception: continue
            if name: minimums[name.lower()]=minimum
    return minimums

def _outline_request(row):
    return {
        "source_uid": row.source_uid,
        "source_url": row.source_url,
        "download_url": row.source_url,
        "title": row.title,
        "file_name": row.title,
        "course": row.course_full,
    }

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



def _tokenize_query(value):
    return {w for w in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(w) > 2}


def _select_course_content(user_id, prompt, limit=8):
    """Rank imported content against a natural-language request without inventing a match."""
    rows = CourseContent.query.filter_by(user_id=user_id).order_by(CourseContent.updated_at.desc()).limit(500).all()
    terms = _tokenize_query(prompt)
    explicit_course = None
    course_match = re.search(r"\b([A-Za-z]{2,5})\s*[- ]?\s*(\d{3,4}[A-Za-z]?)\b", prompt or "")
    if course_match:
        explicit_course = _normalize_course_code(f"{course_match.group(1)} {course_match.group(2)}")
    lecture_numbers = set(re.findall(r"(?:lecture|lesson|week|module|chapter|slides?)\s*#?\s*(\d{1,3})", (prompt or "").lower()))

    def _clean(value):
        # Brightspace-sourced titles sometimes carry non-breaking spaces or other invisible
        # unicode whitespace that don't behave like a normal space under \b / \s in regex.
        return re.sub(r"[\s\u00a0\u200b\u2007\u202f]+", " ", (value or ""))

    ranked = []
    for row in rows:
        if explicit_course and _normalize_course_code(row.course_full) != explicit_course:
            continue
        hay = _clean(f"{row.course_full} {row.title} {row.module_name} {row.content_type}").lower()
        score = 0
        for term in terms:
            if term in hay:
                score += 4 if term in _clean(row.title).lower() else 2
        if row.content_type == 'lecture' and re.search(r"\b(explain|teach|summari[sz]e|lecture|slides?|week|module)\b", (prompt or '').lower()):
            score += 5
        if lecture_numbers and any(re.search(rf"\b{re.escape(n)}\b", hay) for n in lecture_numbers):
            score += 12
        if score:
            ranked.append((score, row.updated_at or datetime.min, row))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    results = [r for _, _, r in ranked[:limit]]
    # If the student named a specific lecture number, prefer results that literally contain
    # it in the title/module — but never fall back to an empty list just because that stricter
    # check missed on formatting; the scored ranking above already required the number to be
    # present in the title/module to get here, so `results` is a safe fallback rather than
    # silently telling the student nothing was found.
    if lecture_numbers:
        exact = []
        for row in results:
            hay = _clean(f"{row.title} {row.module_name}").lower()
            if any(re.search(rf"(?:lecture|lesson|week|module|chapter|slides?)?\s*#?\s*{re.escape(n)}\b", hay) for n in lecture_numbers):
                exact.append(row)
        return exact or results
    return results



def _gemini_image_to_outline_text(raw, mime_type):
    """Transcribe an outline screenshot with Gemini Vision when local OCR is unavailable/weak."""
    api_key=(os.environ.get('GEMINI_API_KEY','').strip() or os.environ.get('GOOGLE_API_KEY','').strip())
    if not api_key:
        raise RuntimeError('Gemini is not configured for screenshot reading.')
    model=os.environ.get('GEMINI_MODEL','gemini-2.5-flash').strip()
    endpoint=("https://generativelanguage.googleapis.com/v1beta/models/"+
              urllib.parse.quote(model,safe='')+":generateContent?key="+urllib.parse.quote(api_key,safe=''))
    prompt=(
        "Transcribe every visible word in this university course-outline screenshot, preserving table rows, "
        "assessment names, percentages, pass/fail requirements, and headings. Do not summarize and do not infer. "
        "Return plain text only."
    )
    body={
        'contents':[{'role':'user','parts':[{'text':prompt},{'inline_data':{'mime_type':mime_type,'data':base64.b64encode(raw).decode('ascii')}}]}],
        'generationConfig':{'temperature':0.0,'maxOutputTokens':8000},
    }
    req=urllib.request.Request(endpoint,data=json.dumps(body).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=90) as response:
        result=json.loads(response.read().decode('utf-8'))
    parts=((((result.get('candidates') or [{}])[0].get('content') or {}).get('parts')) or [])
    text_value='\n'.join(x.get('text','') for x in parts if isinstance(x,dict)).strip()
    if len(text_value)<40:
        raise ValueError('Gemini could not read enough text from this screenshot.')
    return text_value

def _extract_uploaded_course_file(upload):
    filename = secure_filename(upload.filename or 'lecture-file')
    raw = upload.read()
    if not raw:
        raise ValueError('The Brightspace file was empty.')
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError('This lecture is larger than the 30 MB processing limit.')
    suffix = Path(filename).suffix.lower()
    # Brightspace download endpoints sometimes return extensionless names. Detect common formats by file signature.
    if suffix in {'', '.bin'}:
        if raw.startswith(b'%PDF'):
            suffix = '.pdf'
            filename = f"{Path(filename).stem or 'lecture'}.pdf"
        elif raw.startswith(b'PK\x03\x04'):
            # PPTX and DOCX are both ZIP containers. Inspect package markers.
            try:
                import zipfile
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = set(archive.namelist())
                if any(name.startswith('ppt/') for name in names):
                    suffix = '.pptx'
                    filename = f"{Path(filename).stem or 'lecture'}.pptx"
                elif any(name.startswith('word/') for name in names):
                    suffix = '.docx'
                    filename = f"{Path(filename).stem or 'lecture'}.docx"
            except Exception:
                pass
    text_parts = []
    if suffix == '.pdf':
        reader = PdfReader(io.BytesIO(raw))
        for index, page in enumerate(reader.pages[:250], 1):
            extracted = (page.extract_text() or '').strip()
            if extracted:
                text_parts.append(f"[Page {index}]\n{extracted}")
    elif suffix in {'.pptx', '.ppt'}:
        if suffix == '.ppt':
            raise ValueError('Legacy .ppt files must be converted to .pptx by Brightspace or the instructor.')
        deck = Presentation(io.BytesIO(raw))
        for index, slide in enumerate(deck.slides[:300], 1):
            chunks = []
            for shape in slide.shapes:
                if hasattr(shape, 'text') and shape.text and shape.text.strip():
                    chunks.append(shape.text.strip())
                if getattr(shape, 'has_table', False):
                    for table_row in shape.table.rows:
                        chunks.append(' | '.join(cell.text.strip() for cell in table_row.cells))
            notes = ''
            try:
                notes = slide.notes_slide.notes_text_frame.text.strip()
            except Exception:
                pass
            if notes:
                chunks.append(f"Speaker notes: {notes}")
            if chunks:
                text_parts.append(f"[Slide {index}]\n" + '\n'.join(chunks))
    elif suffix == '.docx':
        doc = Document(io.BytesIO(raw))
        text_parts.extend(p.text.strip() for p in doc.paragraphs if p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                text_parts.append(' | '.join(cell.text.strip() for cell in row.cells))
    elif suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        ocr=''
        local_error=None
        if Image is not None and pytesseract is not None:
            try:
                image=Image.open(io.BytesIO(raw)).convert('RGB')
                image=ImageOps.exif_transpose(image)
                if max(image.size) < 1800:
                    scale=1800/max(image.size)
                    image=image.resize((int(image.width*scale),int(image.height*scale)))
                gray=ImageOps.grayscale(image)
                gray=ImageEnhance.Contrast(gray).enhance(1.7)
                gray=gray.filter(ImageFilter.SHARPEN)
                ocr=pytesseract.image_to_string(gray, config='--oem 3 --psm 3')
                if len(ocr.strip()) < 80 or len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)) < 2:
                    alternate=pytesseract.image_to_string(gray, config='--oem 3 --psm 11')
                    if len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',alternate)) > len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)):
                        ocr=alternate
            except Exception as exc:
                local_error=exc
        # Render deployments may not include the Tesseract system binary. Gemini Vision is
        # therefore a real fallback, not a fake success path.
        if len(ocr.strip()) < 80 or len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)) < 2:
            mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[suffix]
            try:
                ocr=_gemini_image_to_outline_text(raw,mime)
            except Exception as gemini_exc:
                detail=f' Local OCR: {local_error}.' if local_error else ''
                raise ValueError('Nexora could not read this screenshot. Use a clearer image or upload the PDF/DOCX.'+detail+f' Gemini Vision: {gemini_exc}')
        text_parts.append('[Outline screenshot OCR]\n'+ocr)
    elif suffix in {'.txt', '.md', '.csv', '.py', '.java', '.c', '.cpp', '.js', '.html'}:
        text_parts.append(raw.decode('utf-8', errors='replace'))
    else:
        raise ValueError(f'Automatic file reading does not yet support {suffix or "this file type"}.')
    text = '\n\n'.join(x for x in text_parts if x).strip()
    if not text:
        raise ValueError('No readable text was found. Upload a clearer PDF, DOCX, or high-resolution PNG/JPG screenshot.')
    return filename, text[:500000], hashlib.sha256(raw).hexdigest(), len(raw)


@app.route('/api/sync/process-content', methods=['POST'])
def process_course_content_file():
    """Receive one temporary Brightspace file from Nexora Sync, extract text, then discard the bytes."""
    user = _extension_user_from_request()
    if not user:
        return jsonify({'error': 'Invalid or missing extension token.'}), 401
    source_uid = str(request.form.get('source_uid') or '')[:255]
    if not source_uid:
        return jsonify({'error': 'Missing source_uid.'}), 400
    row = CourseContent.query.filter_by(user_id=user.id, source_uid=source_uid).first()
    if not row:
        return jsonify({'error': 'This imported lecture could not be found in Nexora.'}), 404
    upload = request.files.get('file')
    if not upload:
        return jsonify({'error': 'No lecture file was received.'}), 400
    try:
        filename, extracted, file_hash, size = _extract_uploaded_course_file(upload)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'stage': 'extract'}), 422
    except Exception as exc:
        app.logger.exception('Unexpected lecture extraction failure for %s', source_uid)
        return jsonify({
            'error': 'Nexora received the lecture but could not read its file format. Try syncing again or ask the professor to provide PDF/PPTX.',
            'stage': 'extract',
            'detail': type(exc).__name__,
        }), 422
    try:
        extracted = _sanitize_extracted_text(extracted)
        row.content_text = extracted
        row.content_hash = _content_payload_hash({
            'title': row.title, 'content_type': row.content_type, 'source_url': row.source_url,
            'content_text': extracted, 'module_name': row.module_name, 'published_at': row.published_at,
        })
        row.updated_at = datetime.utcnow()
        existing = SyncedFile.query.filter_by(user_id=user.id, source_uid=source_uid).first()
        if not existing:
            existing = SyncedFile(id=uuid4().hex, user_id=user.id, source_uid=source_uid, course_full=row.course_full)
            db.session.add(existing)
        existing.original_source_url = row.source_url
        existing.file_name = filename
        existing.mime_type = upload.mimetype or ''
        existing.file_size = size
        existing.content_hash = file_hash
        existing.stored_url = ''
        existing.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('Lecture text persistence failed for %s', source_uid)
        return jsonify({
            'error': 'The lecture was read successfully, but Nexora could not save its extracted text.',
            'stage': 'database',
            'detail': type(exc).__name__,
        }), 500
    return jsonify({
        'ok': True,
        'source_uid': source_uid,
        'title': row.title,
        'characters': len(extracted),
        'file_name': filename,
        'temporary_file_deleted': True,
        'cached_as_text_only': True,
    })


@app.route("/api/copilot", methods=["POST"])
@login_required
def copilot():
    """Conversational, semester-aware Nexora AI powered by Gemini."""
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("message") or "").strip()
    images = payload.get('images') or []
    if not isinstance(images, list):
        images = []
    images = images[:4]
    if not prompt and not images:
        return jsonify({"error": "Ask me something or attach an image first."}), 400

    validated_images = []
    total_image_bytes = 0
    for image in images:
        if not isinstance(image, dict):
            continue
        mime = str(image.get('mime_type') or '').lower().strip()
        data = str(image.get('data') or '').strip()
        if mime not in {'image/jpeg','image/png','image/webp','image/gif'} or not data:
            continue
        try:
            raw_size = (len(data) * 3) // 4
        except Exception:
            continue
        total_image_bytes += raw_size
        if total_image_bytes > 8 * 1024 * 1024:
            return jsonify({'error': 'Attached images are too large. Keep the total under 8 MB.'}), 413
        validated_images.append({'mime_type': mime, 'data': data})

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

    matched_content = _select_course_content(current_user.id, prompt)
    lecture_intent = bool(re.search(r"\b(explain|teach|summari[sz]e|break down|quiz me|flashcards?).{0,80}\b(lecture|slides?|week|module|chapter)\b|\b(lecture|slides?|week|module|chapter).{0,80}\b(explain|teach|summari[sz]e|break down|quiz|flashcards?)\b", prompt.lower()))
    primary = matched_content[0] if matched_content else None
    explicit_lecture_number = re.search(r'(?:lecture|lesson|week|module|chapter|slides?)\s*#?\s*(\d{1,3})', prompt.lower())
    if lecture_intent and explicit_lecture_number and not primary:
        return jsonify({
            'answer': f"I couldn't find Lecture {explicit_lecture_number.group(1)} in this course yet. I won't guess using a different file. Run Nexora Sync or choose a lecture that appears in the workspace.",
            'grounded': False,
            'sources': [],
        }), 200
    if lecture_intent and primary and len((primary.content_text or '').strip()) < 120:
        return jsonify({
            "answer": f"I found {primary.title} in {primary.course_full}. I’m securely fetching and reading it from Brightspace now…",
            "needs_content": True,
            "content_request": {
                "source_uid": primary.source_uid,
                "source_url": primary.source_url,
                "download_url": primary.source_url,
                "title": primary.title,
                "file_name": primary.title,
                "course": primary.course_full,
            },
        }), 409

    semester_context = {
        "student_first_name": first_name,
        "today": date.today().isoformat(),
        "tasks": [{
            "title": task.get("title", "Task"),
            "course": task.get("course_full", "OTHER"),
            "due": task.get("main_due_raw", ""),
        } for task in tasks[:40]],
        "matched_course_content": [{
            "course": row.course_full,
            "title": row.title,
            "type": row.content_type,
            "module": row.module_name,
            "text": (row.content_text or '')[:45000],
        } for row in matched_content[:3] if (row.content_text or '').strip()],
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
    user_parts = [{"text": (prompt or 'Please explain the attached image in the context of this course.')[:6000]}]
    for image in validated_images:
        user_parts.append({"inline_data": {"mime_type": image['mime_type'], "data": image['data']}})
    contents.append({"role": "user", "parts": user_parts})

    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
    system_instruction = (
        "You are Nexora AI, a high-quality conversational academic tutor for university students. "
        "Answer naturally, directly, and intelligently. Explain academic topics, brainstorm, write, reason, "
        "and help plan the student's semester. Use the supplied semester context only when relevant. Never "
        "invent course dates, deadlines, grades, or facts that are absent from the context. For ordinary questions, be direct. "
        "When the student asks to explain, teach, summarize, quiz, or create flashcards from a lecture, give a complete university-tutor response: follow the source order, cover every major concept, equation, example, diagram description and common mistake, and finish with takeaways and practice questions. Never reduce a full lecture explanation to one paragraph. Do not repeat a canned introduction. The current version "
        "does not have live web search, so clearly say when a question requires current information rather "
        "than pretending it is up to date.\n\n"
        "FORMATTING: Respond in Markdown. Use `##`/`###` headers to break up multi-part explanations, "
        "bullet or numbered lists for steps/enumerations, and **bold** for key terms. Keep prose paragraphs "
        "short. When generating a quiz, format each question as a numbered list item with lettered options "
        "on their own lines, and put the answer key in a final '### Answer key' section rather than inline. "
        "When generating flashcards, format each one as '**Q:** ... / **A:** ...' on separate lines.\n\n"
        "SLIDE / PAGE CITATIONS: The matched_course_content text below is pre-tagged with markers like "
        "'[Slide 4]' or '[Page 12]' showing exactly where each chunk of material came from. Whenever your "
        "explanation, quiz question, or flashcard draws on a specific tagged chunk, cite it inline in the "
        "exact form '(Slide 4)' or '(Page 12)' right after the relevant sentence, using the number from the "
        "nearest preceding marker in the source text. Cite every distinct claim you pull from the material — "
        "do not bundle multiple slides into one citation. Never invent a slide or page number that isn't "
        "present in the source text, and never cite a slide/page if you're answering from general knowledge "
        "instead of the supplied material.\n\n"
        f"CURRENT STUDENT CONTEXT:\n{json.dumps(semester_context, ensure_ascii=False)}"
    )

    body = json.dumps({
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192 if lecture_intent else 2200,
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
        result = None
        last_http_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as retry_exc:
                last_http_error = retry_exc
                if retry_exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        if result is None and last_http_error:
            raise last_http_error

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

        raw_citations = re.findall(r"\((Slide|Page)\s+(\d{1,4})\)", answer, flags=re.IGNORECASE)
        seen_citations = set()
        citations = []
        for kind, number in raw_citations:
            label = f"{kind.capitalize()} {number}"
            if label in seen_citations:
                continue
            seen_citations.add(label)
            citations.append({"label": label, "kind": kind.lower(), "number": int(number)})
        citations.sort(key=lambda c: (c["kind"], c["number"]))

        return jsonify({
            "answer": answer,
            "mode": "gemini",
            "grounded": bool(semester_context.get("matched_course_content")),
            "sources": [{"course": x["course"], "title": x["title"], "module": x["module"]} for x in semester_context.get("matched_course_content", [])],
            "citations": citations,
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

DEFAULT_GRADING_SCALE = [
    {"letter":"A+","minimum":90.0},{"letter":"A","minimum":85.0},{"letter":"A-","minimum":80.0},
    {"letter":"B+","minimum":77.0},{"letter":"B","minimum":73.0},{"letter":"B-","minimum":70.0},
    {"letter":"C+","minimum":67.0},{"letter":"C","minimum":63.0},{"letter":"C-","minimum":60.0},
    {"letter":"D+","minimum":57.0},{"letter":"D","minimum":53.0},{"letter":"D-","minimum":50.0},
    {"letter":"F","minimum":0.0},
]

def _normalize_grading_scale(raw):
    cleaned=[]
    for item in raw or []:
        if not isinstance(item,dict):
            continue
        letter=str(item.get("letter") or "").strip().upper()
        try: minimum=float(item.get("minimum"))
        except (TypeError,ValueError): continue
        if not letter or not (0 <= minimum <= 100): continue
        cleaned.append({"letter":letter[:12],"minimum":round(minimum,2)})
    # Keep one threshold per letter and sort high-to-low.
    dedup={x["letter"]:x for x in cleaned}
    result=sorted(dedup.values(),key=lambda x:x["minimum"],reverse=True)
    if not result or result[-1]["minimum"]>0:
        result.append({"letter":"F","minimum":0.0})
    return result

def _get_user_grading_scale(user_id):
    row=UserGradingScale.query.filter_by(user_id=user_id).first()
    if not row:
        return {"university_name":"","scale":DEFAULT_GRADING_SCALE,"configured":False}
    try: scale=_normalize_grading_scale(json.loads(row.scale_json or "[]"))
    except Exception: scale=[]
    if not scale: scale=DEFAULT_GRADING_SCALE
    return {"university_name":row.university_name or "","scale":scale,"configured":True}

def _numeric_to_letter(user_id, grade):
    if grade is None: return None
    profile=_get_user_grading_scale(user_id)
    for band in profile["scale"]:
        if float(grade) >= float(band["minimum"]):
            return band["letter"]
    return profile["scale"][-1]["letter"] if profile["scale"] else None

def _letter_requirement_result(user_id, grade, requirement):
    profile=_get_user_grading_scale(user_id)
    required=str(requirement.get("minimum_letter") or "").strip().upper()
    threshold=next((float(x["minimum"]) for x in profile["scale"] if x["letter"].upper()==required),None)
    actual_letter=_numeric_to_letter(user_id,grade)
    if threshold is None:
        return {"message":requirement.get("message") or f"Minimum {required}","scope":requirement.get("scope") or "course","passed":None,"actual_letter":actual_letter,"required_letter":required,"required_numeric":None,"needs_scale":True}
    return {"message":requirement.get("message") or f"Minimum {required}","scope":requirement.get("scope") or "course","passed":grade is not None and float(grade)>=threshold,"actual_letter":actual_letter,"required_letter":required,"required_numeric":threshold,"needs_scale":False}

@app.route("/settings")
@login_required
def settings():
    grading_scale=_get_user_grading_scale(current_user.id)
    return render_template("settings.html", user=current_user, grading_scale=grading_scale)

@app.route("/api/grading-scale", methods=["GET","POST"])
@login_required
def grading_scale_api():
    if request.method=="GET":
        return jsonify(_get_user_grading_scale(current_user.id))
    data=request.get_json(silent=True) or {}
    scale=_normalize_grading_scale(data.get("scale") or [])
    if len(scale)<2:
        return jsonify({"error":"Add at least two valid letter-grade bands."}),400
    row=UserGradingScale.query.filter_by(user_id=current_user.id).first()
    if not row:
        row=UserGradingScale(id=uuid4().hex,user_id=current_user.id)
        db.session.add(row)
    row.university_name=str(data.get("university_name") or "")[:255]
    row.scale_json=json.dumps(scale,ensure_ascii=False)
    row.updated_at=datetime.utcnow()
    db.session.commit()
    return jsonify({"ok":True,"university_name":row.university_name,"scale":scale})


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

    all_synced_courses = SyncedCourse.query.filter_by(user_id=current_user.id).order_by(SyncedCourse.updated_at.desc()).all()
    hidden_course_codes = {c.course_code for c in all_synced_courses if bool(getattr(c, "is_hidden", False))}
    hidden_course_codes.update({h.course_code for h in HiddenCourse.query.filter_by(user_id=current_user.id).all()})
    synced_courses = [c for c in all_synced_courses if not bool(getattr(c, "is_hidden", False)) and c.course_code not in hidden_course_codes]
    for hidden_code in hidden_course_codes:
        course_map.pop(hidden_code, None)
    for synced in synced_courses:
        code = synced.course_code or "OTHER"
        if code not in course_map:
            course_map[code] = {
                "course": code, "total": synced.content_count or 0,
                "next_title": "", "next_due": "", "progress": 0,
            }
        course_map[code]["title"] = synced.course_name or code
        course_map[code]["image_url"] = synced.image_url or ""
        course_map[code]["brightspace_url"] = synced.brightspace_url or ""
        course_map[code]["term"] = synced.term or ""
        course_map[code]["content_count"] = synced.content_count or 0

    all_course_cards = list(course_map.values())
    show_all_courses = request.args.get("view") == "all"
    course_cards = all_course_cards if show_all_courses else all_course_cards[:8]

    course_titles = {
        "SYSC2004": "Object-Oriented Programming",
        "SYSC 2004": "Object-Oriented Programming",
        "ELEC2507": "Circuits & Electronics",
        "ELEC 2507": "Circuits & Electronics",
        "ECOR1034": "Engineering Economy",
        "ECOR 1034": "Engineering Economy",
        "MATH2004": "Linear Algebra",
        "MATH 2004": "Linear Algebra",
        "MATH2005": "Differential Equations",
        "MATH 2005": "Differential Equations",
        "BIOL1103": "Cell Biology",
        "BIOL 1103": "Cell Biology",
        "CCDP2100": "Communication Skills",
        "CCDP 2100": "Communication Skills",
        "OTHER": "General Coursework",
    }

    course_art = ["code", "circuit", "economy", "math"]
    for index, course_card in enumerate(course_cards):
        total = course_card["total"]
        compact_code = course_card["course"].replace(" ", "").upper()
        course_card["title"] = course_card.get("title") or course_titles.get(course_card["course"], course_titles.get(compact_code, "Course Workspace"))
        course_card["progress"] = min(95, max(25, 100 - (total * 8)))
        course_card["art"] = course_art[index % len(course_art)]
        course_card["due_status"] = course_card.get("next_due") or "No date"

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
    academic_updates = AcademicUpdate.query.filter_by(user_id=current_user.id).order_by(AcademicUpdate.created_at.desc()).limit(4).all()

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
        academic_updates=academic_updates,
        show_all_courses=show_all_courses,
    )



@app.route("/study-room")
@login_required
def study_room():
    raw_tasks = load_tasks()
    cards = sort_cards(view_filter(build_grouped_cards(raw_tasks), "all"))
    active = [c for c in cards if not c.get("completed")][:12]
    deadlines = []
    for c in active:
        if c.get("main_due_raw"):
            deadlines.append({
                "title": c.get("title", "Task"),
                "course": c.get("course_full", "OTHER"),
                "due": c.get("main_due_pretty") or c.get("status_label") or "Upcoming",
            })
    tasks = [{
        "title": c.get("title", "Task"),
        "course": c.get("course_full", "OTHER"),
        "due": c.get("main_due_pretty") or "No date",
        "status": c.get("status_label") or "Active",
    } for c in active]
    synced = SyncedCourse.query.filter_by(user_id=current_user.id).order_by(SyncedCourse.updated_at.desc()).all()
    courses = [{"code": x.course_code or "OTHER", "name": x.course_name or x.course_code or "Course"} for x in synced[:12]]
    if not courses:
        seen = []
        for c in cards:
            code = c.get("course_full") or "OTHER"
            if code not in seen:
                seen.append(code)
                courses.append({"code": code, "name": "Course workspace"})
    room_data = {"tasks": tasks, "deadlines": deadlines[:8], "courses": courses}
    return render_template(
        "study_room.html", user=current_user, tasks=tasks, deadlines=deadlines[:8], courses=courses,
        room_data=room_data, streak_days=min(30, max(1, len(active) + 4)),
        streak_bars=[32, 48, 40, 72, 58, 84, 66]
    )


@app.route("/api/tasks/<group_id>/complete", methods=["POST"])
@login_required
def complete_task_group(group_id):
    tasks = Task.query.filter_by(user_id=current_user.id, group_id=group_id).all()
    if not tasks:
        return jsonify({"error": "Task not found."}), 404
    completed = bool((request.get_json(silent=True) or {}).get("completed", True))
    for task in tasks:
        task.completed = completed
    db.session.commit()
    return jsonify({"ok": True, "completed": completed, "group_id": group_id})


@app.route("/api/start-my-day", methods=["GET"])
@login_required
def start_my_day():
    cards = sort_cards(build_grouped_cards(load_tasks()))
    plan = []
    total_minutes = 0
    for card in cards[:5]:
        dleft = days_until(card.get("main_due_raw", ""))
        minutes = 60 if dleft is not None and dleft <= 1 else 45 if dleft is not None and dleft <= 3 else 30
        total_minutes += minutes
        plan.append({
            "group_id": card.get("group_id", ""),
            "title": card.get("title", "Task"),
            "course": card.get("course_full", "OTHER"),
            "due": pretty_date(card.get("main_due_raw", "")) or "No date",
            "minutes": minutes,
        })
    return jsonify({"plan": plan, "total_minutes": total_minutes, "count": len(plan)})



def _normalize_course_code(value):
    detected = extract_course_from_text(value or "")
    if detected:
        return detected
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return cleaned or "OTHER"


def _content_payload_hash(payload):
    raw = json.dumps({
        "title": payload.get("title", ""),
        "type": payload.get("content_type", "resource"),
        "url": payload.get("source_url", ""),
        "text": payload.get("content_text", ""),
        "module": payload.get("module_name", ""),
        "published": payload.get("published_at", ""),
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sanitize_extracted_text(value):
    """Make extracted document text safe for PostgreSQL and Gemini."""
    if value is None:
        return ""
    value = str(value).replace("\x00", "")
    # Keep normal whitespace while removing other control characters.
    value = "".join(ch for ch in value if ch in "\n\r\t" or ord(ch) >= 32)
    return value.strip()


def _extension_user_from_request():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token:
        return None
    return User.query.filter_by(extension_token=token).first()


@app.route("/course/<course_code>")
@login_required
def course_workspace(course_code):
    course = _normalize_course_code(course_code)
    all_cards = sort_cards(build_grouped_cards(load_tasks()))
    task_assignments = [c for c in all_cards if _normalize_course_code(c.get("course_full")) == course]
    contents = CourseContent.query.filter_by(user_id=current_user.id, course_full=course).order_by(CourseContent.updated_at.desc()).all()
    synced_assignments = [c for c in contents if c.content_type == "assignment"]
    assignments = task_assignments
    synced_course = SyncedCourse.query.filter_by(user_id=current_user.id, course_code=course).first()
    updates = AcademicUpdate.query.filter_by(user_id=current_user.id, course_full=course).order_by(AcademicUpdate.created_at.desc()).limit(12).all()

    lectures = [c for c in contents if c.content_type == "lecture"]
    resources = [c for c in contents if c.content_type in ("file", "resource", "link", "practice", "page")]
    quizzes = [c for c in contents if c.content_type == "quiz"]
    announcements = [c for c in contents if c.content_type == "announcement"]
    course_outlines = [c for c in contents if c.content_type == "outline" or re.search(r'course\s*outline|syllabus', c.title or '', re.I)]
    grade_entries = GradeEntry.query.filter_by(user_id=current_user.id, course_full=course).order_by(GradeEntry.created_at.asc()).all()
    outline_profile = CourseOutlineProfile.query.filter_by(user_id=current_user.id, course_full=course).first()
    study_artifacts = StudyArtifact.query.filter_by(user_id=current_user.id, course_full=course).order_by(StudyArtifact.updated_at.desc()).limit(30).all()
    try:
        outline_analysis = json.loads(outline_profile.grading_json or "{}") if outline_profile else {}
    except Exception:
        outline_analysis = {}

    next_assignment = assignments[0] if assignments else None
    return render_template(
        "course_workspace.html",
        user=current_user,
        course=course,
        assignments=assignments,
        synced_assignments=synced_assignments,
        synced_course=synced_course,
        contents=contents,
        lectures=lectures,
        resources=resources,
        quizzes=quizzes,
        announcements=announcements,
        course_outlines=course_outlines,
        grade_entries=grade_entries,
        outline_profile=outline_profile,
        study_artifacts=study_artifacts,
        outline_analysis=outline_analysis,
        updates=updates,
        next_assignment=next_assignment,
    )


@app.route("/api/course/<course_code>/grades", methods=["GET", "POST", "DELETE"])
@login_required
def course_grades(course_code):
    course = _normalize_course_code(course_code)
    if request.method == "GET":
        rows = GradeEntry.query.filter_by(user_id=current_user.id, course_full=course).order_by(GradeEntry.created_at.asc()).all()
        return jsonify({"grades": [{"id": r.id, "name": r.name, "weight": r.weight, "score": r.score, "max_score": r.max_score, "category": r.category} for r in rows]})
    data = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        row = GradeEntry.query.filter_by(id=str(data.get("id") or ""), user_id=current_user.id, course_full=course).first()
        if row:
            db.session.delete(row); db.session.commit()
        return jsonify({"ok": True})
    name = str(data.get("name") or "Assessment").strip()[:255]
    try:
        weight = max(0.0, min(100.0, float(data.get("weight") or 0)))
        score = data.get("score")
        score = None if score in (None, "") else max(0.0, float(score))
        max_score = max(0.0001, float(data.get("max_score") or 100))
    except (TypeError, ValueError):
        return jsonify({"error": "Use valid numbers for score, maximum, and weight."}), 400
    row_id = str(data.get("id") or "")
    row = GradeEntry.query.filter_by(id=row_id, user_id=current_user.id, course_full=course).first() if row_id else None
    if not row:
        row = GradeEntry.query.filter_by(user_id=current_user.id, course_full=course, name=name).first()
    if not row:
        row = GradeEntry(id=uuid4().hex, user_id=current_user.id, course_full=course)
        db.session.add(row)
    row.name=name; row.weight=weight; row.score=score; row.max_score=max_score; row.category=str(data.get("category") or row.category or "assessment")[:80]
    row.updated_at=datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "id": row.id})


def _grade_row_map(rows):
    return {re.sub(r'[^a-z0-9]+',' ',str(r.name or '').lower()).strip():r for r in rows}


def _row_percent(row, override=None):
    if override is not None:
        return max(0.0,min(100.0,float(override)))
    if not row or row.score is None:
        return None
    return max(0.0,min(100.0,float(row.score)/max(float(row.max_score or 100),0.0001)*100))


def _find_grade_row(row_map, name):
    key=re.sub(r'[^a-z0-9]+',' ',str(name or '').lower()).strip()
    if key in row_map: return row_map[key]
    for k,row in row_map.items():
        if key and (key in k or k in key): return row
    return None


def _evaluate_option(option,row_map,final_override=None):
    points=0.0; missing=[]; details=[]
    eligibility=option.get('eligibility') or {}
    test_rows=[_find_grade_row(row_map,f'Test {i}') for i in (1,2,3)]
    written=sum(1 for r in test_rows if r and r.score is not None)
    min_written=int(eligibility.get('minimum_written_tests') or 0)
    eligible=written>=min_written
    if not eligible:
        missing.append(f'Requires at least {min_written} written test(s); {written} entered.')
    for comp in option.get('components') or []:
        weight=float(comp.get('weight') or 0)
        method=str(comp.get('method') or 'direct')
        pct=None
        if comp.get('source_group'):
            vals=[]
            for source in comp.get('source_group') or []:
                r=_find_grade_row(row_map,source)
                v=_row_percent(r)
                if v is not None: vals.append(v)
            count=int(comp.get('count') or len(vals) or 1)
            if method in {'best','best_average'}:
                vals=sorted(vals,reverse=True)[:count]
            elif method=='average':
                vals=vals[:count]
            if len(vals)>=count:
                pct=sum(vals)/len(vals)
            else:
                missing.append(f"Needs {count} test score(s) for {option.get('option_name','this option')}.")
        else:
            source=str(comp.get('source') or '')
            r=_find_grade_row(row_map,source)
            override=final_override if 'final' in source.lower() else None
            pct=_row_percent(r,override)
            if pct is None: missing.append(f'{source} is not entered.')
        if pct is not None:
            contribution=pct/100*weight; points+=contribution
            details.append({'name':comp.get('source') or 'Tests','percent':round(pct,2),'weight':weight,'points':round(contribution,2)})
    return {'option_name':option.get('option_name') or 'Grading option','grade':round(points,2),'eligible':eligible,'complete':not missing,'missing':missing,'details':details}


def _evaluate_pass_rules(analysis,row_map,course_grade,final_override=None):
    results=[]; passed=True
    rules=analysis.get('pass_rules') or []
    if not rules:
        for name,minimum in _outline_rule_minimums(analysis).items():
            rules.append({'type':'component_minimum','component':name,'minimum_percent':minimum,'message':f'{name} minimum'})
        rules.append({'type':'overall_minimum','minimum_percent':float(analysis.get('pass_mark') or 50),'message':'Overall course minimum'})
    for rule in rules:
        typ=rule.get('type'); ok=False; actual=None; required=None
        if typ=='component_minimum':
            name=rule.get('component'); row=_find_grade_row(row_map,name)
            actual=_row_percent(row, final_override if 'final' in str(name).lower() else None)
            required=float(rule.get('minimum_percent') or 0); ok=actual is not None and actual>=required
        elif typ=='any_component_minimum':
            vals=[_row_percent(_find_grade_row(row_map,n)) for n in rule.get('components') or []]
            vals=[v for v in vals if v is not None]; actual=max(vals) if vals else None
            required=float(rule.get('minimum_percent') or 0); ok=actual is not None and actual>=required
        elif typ=='raw_minimum':
            row=_find_grade_row(row_map,rule.get('component')); actual=None if not row or row.score is None else float(row.score)
            required=float(rule.get('minimum_raw') or 0); ok=actual is not None and actual>=required
        elif typ=='overall_minimum':
            actual=course_grade; required=float(rule.get('minimum_percent') or 0); ok=course_grade>=required
        else:
            continue
        passed &= ok
        results.append({'message':rule.get('message') or typ,'passed':bool(ok),'actual':None if actual is None else round(actual,2),'required':required})
    return passed,results


def _decorate_grade_result(user_id, analysis, grade):
    scale_profile=_get_user_grading_scale(user_id)
    letter=_numeric_to_letter(user_id,grade)
    letter_results=[_letter_requirement_result(user_id,grade,r) for r in (analysis.get('letter_requirements') or []) if isinstance(r,dict)]
    course_pass_rules=[r for r in letter_results if r.get('scope')=='course_pass']
    prerequisite_rules=[r for r in letter_results if r.get('scope')=='prerequisite']
    return {"letter_grade":letter,"grading_scale_name":scale_profile.get("university_name") or "Your grading scale","grading_scale_configured":scale_profile.get("configured",False),"letter_rule_results":letter_results,"course_letter_rules_passed":all(r.get("passed") is True for r in course_pass_rules) if course_pass_rules else True,"prerequisite_eligibility":prerequisite_rules}

@app.route("/api/course/<course_code>/grade-outcomes", methods=["POST"])
@login_required
def grade_outcomes(course_code):
    course=_normalize_course_code(course_code)
    data=request.get_json(silent=True) or {}
    try: target=max(0.0,min(100.0,float(data.get('target',50))))
    except (TypeError,ValueError): return jsonify({'error':'Enter a valid target percentage.'}),400
    rows=GradeEntry.query.filter_by(user_id=current_user.id,course_full=course).all()
    profile=CourseOutlineProfile.query.filter_by(user_id=current_user.id,course_full=course).first()
    try: analysis=json.loads(profile.grading_json or '{}') if profile else {}
    except Exception: analysis={}
    row_map=_grade_row_map(rows)
    options=analysis.get('grading_options') or []
    if options:
        evaluated=[_evaluate_option(opt,row_map) for opt in options]
        complete=[x for x in evaluated if x['complete'] and x['eligible']]
        best=max(complete,key=lambda x:x['grade']) if complete else None
        official_grade=best['grade'] if best else None
        pass_ok,rule_results=_evaluate_pass_rules(analysis,row_map,official_grade or 0)
        official_status='INCOMPLETE' if official_grade is None else ('PASS' if pass_ok else 'FAIL')
        # Solve required final for every option by binary search, respecting all pass rules.
        required=[]
        for opt in options:
            found=None
            for step in range(0,1001):
                fs=step/10
                ev=_evaluate_option(opt,row_map,final_override=fs)
                if not ev['eligible']: continue
                # Missing final is satisfied by override; other missing inputs still block calculation.
                nonfinal_missing=[m for m in ev['missing'] if 'Final Exam' not in m and 'final exam' not in m.lower()]
                if nonfinal_missing: continue
                pass_rules,_=_evaluate_pass_rules(analysis,row_map,ev['grade'],final_override=fs)
                if ev['grade']>=target and pass_rules:
                    found=fs; break
            required.append({'option_name':opt.get('option_name'),'required_final':found,'eligible_now':_evaluate_option(opt,row_map,final_override=100)['eligible']})
        possible=[x for x in required if x['required_final'] is not None]
        best_required=min(possible,key=lambda x:x['required_final']) if possible else None
        decorated=_decorate_grade_result(current_user.id,analysis,official_grade)
        if official_grade is not None and not decorated['course_letter_rules_passed']:
            official_status='FAIL'
        return jsonify({
            'mode':'multiple_options','target':target,'options':evaluated,'best_option':best,
            'official_grade':official_grade,'official_status':official_status,'pass_rules':rule_results,
            'required_final_by_option':required,'best_required_final':best_required,
            'pass_mark':float(analysis.get('pass_mark') or (profile.pass_mark if profile else 50)),
            'final_minimum':analysis.get('final_minimum', profile.final_minimum if profile else None),
            **decorated,
        })
    # Single fixed scheme.
    scheme_total=sum(max(0.0,float(r.weight or 0)) for r in rows) or float(analysis.get('total_weight') or 100)
    earned=0.0; completed_weight=0.0; final_row=None
    for r in rows:
        if re.search(r'\bfinal(?:\s+exam)?\b',r.name or '',re.I): final_row=r
        if r.score is not None and r.weight>0:
            earned+=(_row_percent(r)/100)*float(r.weight); completed_weight+=float(r.weight)
    remaining=max(0.0,scheme_total-completed_weight); normalized_target=target/100*scheme_total
    needed_average=None if remaining<=0 else (normalized_target-earned)/remaining*100
    current_on_completed=earned/completed_weight*100 if completed_weight else 0
    official_grade=earned/scheme_total*100 if remaining<=0 and scheme_total else None
    pass_ok,rule_results=_evaluate_pass_rules(analysis,row_map,official_grade or earned/scheme_total*100 if scheme_total else 0)
    required_final=None
    if final_row and final_row.score is None and final_row.weight>0:
        other_remaining=max(0,remaining-final_row.weight)
        try: assumed=max(0,min(100,float(data.get('assumed_other_average',100))))
        except Exception: assumed=100
        required_final=(normalized_target-earned-(assumed/100)*other_remaining)/final_row.weight*100
        final_min=analysis.get('final_minimum',profile.final_minimum if profile else None)
        if final_min is not None: required_final=max(required_final,float(final_min))
    decorated=_decorate_grade_result(current_user.id,analysis,official_grade)
    official_status='INCOMPLETE' if official_grade is None else ('PASS' if pass_ok and decorated['course_letter_rules_passed'] else 'FAIL')
    return jsonify({'mode':'single_scheme','target':target,'official_grade':official_grade,'official_status':official_status,'pass_rules':rule_results,'earned_course_points':round(earned,2),'completed_weight':round(completed_weight,2),'remaining_weight':round(remaining,2),'current_on_completed':round(current_on_completed,2),'needed_average':None if needed_average is None else round(needed_average,2),'required_final':None if required_final is None else round(required_final,2),'best_possible':round((earned+remaining)/scheme_total*100,2) if scheme_total else 0,'worst_possible':round(earned/scheme_total*100,2) if scheme_total else 0,**decorated})


@app.route('/api/course/<course_code>/outline/upload', methods=['POST'])
@login_required
def upload_course_outline(course_code):
    course=_normalize_course_code(course_code)
    uploads=request.files.getlist('files') or request.files.getlist('file')
    uploads=[u for u in uploads if u and u.filename]
    if not uploads:
        return jsonify({'error':'Choose one or more outline files or screenshots.'}),400
    if len(uploads)>8:
        return jsonify({'error':'Upload at most 8 screenshots/pages at once.'}),400
    extracted_parts=[]; hashes=[]; total_size=0; names=[]
    try:
        for index,upload in enumerate(uploads,1):
            filename, extracted, file_hash, size=_extract_uploaded_course_file(upload)
            extracted=_sanitize_extracted_text(extracted)
            if not extracted:
                raise ValueError(f'Nexora could not read visible text from {filename}. Try a clearer screenshot.')
            names.append(filename); hashes.append(file_hash); total_size+=size
            extracted_parts.append(f'\n===== UPLOAD {index}: {filename} =====\n{extracted}')
    except ValueError as exc:
        return jsonify({'error':str(exc)}),422
    except Exception:
        app.logger.exception('Course outline upload failed')
        return jsonify({'error':'Nexora could not read one of these outline files.'}),422
    combined=_sanitize_extracted_text('\n'.join(extracted_parts))
    combined_hash=hashlib.sha256(':'.join(hashes).encode()).hexdigest()
    source_uid='manual-outline:'+hashlib.sha256(f'{current_user.id}:{course}:{combined_hash}'.encode()).hexdigest()[:40]
    row=CourseContent.query.filter_by(user_id=current_user.id,course_full=course,source_uid=source_uid).first()
    if not row:
        row=CourseContent(id=uuid4().hex,user_id=current_user.id,course_full=course,source_uid=source_uid)
        db.session.add(row)
    row.title=names[0] if len(names)==1 else f'{len(names)} outline screenshots/pages'
    row.content_type='outline'; row.source_url=''; row.module_name='Uploaded Course Outline'; row.content_text=combined
    row.content_hash=_content_payload_hash({'title':row.title,'content_type':'outline','source_url':'','content_text':combined,'module_name':'Uploaded Course Outline','published_at':''})
    row.updated_at=datetime.utcnow(); db.session.commit()
    return jsonify({'ok':True,'source_uid':source_uid,'title':row.title,'characters':len(combined),'file_size':total_size,'files':names})


@app.route('/api/course/<course_code>/material/upload', methods=['POST'])
@login_required
def upload_course_material(course_code):
    course=_normalize_course_code(course_code)
    upload=request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error':'Choose a supported lecture file.'}),400
    try:
        filename, extracted, file_hash, size=_extract_uploaded_course_file(upload)
        extracted=_sanitize_extracted_text(extracted)
    except ValueError as exc:
        return jsonify({'error':str(exc)}),422
    except Exception:
        app.logger.exception('Course material upload failed')
        return jsonify({'error':'Nexora could not read this file.'}),422
    source_uid='manual-material:'+hashlib.sha256(f'{current_user.id}:{course}:{file_hash}'.encode()).hexdigest()[:40]
    row=CourseContent.query.filter_by(user_id=current_user.id,course_full=course,source_uid=source_uid).first()
    if not row:
        row=CourseContent(id=uuid4().hex,user_id=current_user.id,course_full=course,source_uid=source_uid)
        db.session.add(row)
    row.title=filename; row.content_type='lecture'; row.source_url=''; row.module_name='Uploaded Material'; row.content_text=extracted
    row.content_hash=_content_payload_hash({'title':filename,'content_type':'lecture','source_url':'','content_text':extracted,'module_name':'Uploaded Material','published_at':''})
    row.updated_at=datetime.utcnow(); db.session.commit()
    return jsonify({'ok':True,'source_uid':source_uid,'title':filename,'characters':len(extracted),'file_size':size})


@app.route("/api/course/<course_code>/outline/analyze", methods=["POST"])
@login_required
def analyze_course_outline(course_code):
    course = _normalize_course_code(course_code)
    data = request.get_json(silent=True) or {}
    source_uid = str(data.get('source_uid') or '')
    query = CourseContent.query.filter_by(user_id=current_user.id, course_full=course)
    row = query.filter_by(source_uid=source_uid).first() if source_uid else None
    if not row:
        row = query.filter(CourseContent.content_type=='outline').order_by(CourseContent.updated_at.desc()).first()
    if not row:
        return jsonify({"error":"No course outline has been detected for this course yet."}),404
    if len((row.content_text or '').strip()) < 200:
        return jsonify({"needs_content":True,"content_request":_outline_request(row),"answer":"Reading the course outline from Brightspace…"}),409
    prompt = f"""Analyze this university course outline for {course}. Return strict JSON with this schema:
{{"course_title":"", "pass_mark":50, "final_minimum":null, "components":[{{"name":"Assignments","weight":20,"category":"assignments","notes":""}}], "component_minimums":[{{"name":"Final Exam","minimum":50,"consequence":"Must pass final to pass course"}}], "rules":[""], "grading_options":[{{"option_name":"Option 1","components":[{{"name":"Final Exam","weight":100,"category":"final","notes":""}}]}}], "important_dates":[], "letter_requirements":[{"minimum_letter":"C-","scope":"prerequisite","message":"C- or better required to use this course as a prerequisite"}], "confidence":"high|medium|low"}}

IMPORTANT — read the whole outline before extracting anything:
1. Some outlines define ONE fixed grading scheme (a single components list). If so, leave grading_options empty and put everything in components; weights should total 100.
2. Some outlines define MULTIPLE ALTERNATIVE grading schemes (e.g. "Option 1: Final exam 100%", "Option 2: best test 15% + tutorials 5% + final 80%") where the student's final grade is whichever option gives them the best result. If you see this pattern, put EACH option as its own entry in grading_options (each with its own components list summing to 100), and set the top-level `components` to the most detailed/informative option (usually the one with the most components) as the default shown to the student. Add a rule noting that alternate options exist and the best one is applied automatically.
3. IGNORE bonus marks, extra-credit quizzes, optional mentorship-meeting bonuses, or any "1% bonus" style paragraphs — these are not part of the graded weighting scheme and must never appear in components or grading_options. A percentage inside a sentence like "complete the quiz to earn your 1%" is a bonus incentive, not a graded component; do not extract it.
4. Only extract an item as a component if it is a genuine named grading category from a grading breakdown/table (e.g. "Midterm", "Labs", "Assignments", "Final Exam", "Tutorial attendance", "Term Tests") — never a sentence fragment.
5. Detect every special pass/fail rule (minimum score on the final exam, labs, midterms, tutorial attendance requirements, overall minimum grade) and put numeric thresholds in component_minimums. Never invent missing values.
6. Grading tables may be headed Assessment Breakdown, Assessment Distribution, Grade Distribution, Evaluation, Marking Scheme, Basis of Grade, or similar. Treat all of these as equivalent.
7. Ignore explanatory dates/descriptions between an assessment name and its weight. Example: 'Midterm Exam I Closed book (Weekend of Jan 31) 20%' means Midterm 1 = 20%.
8. If there are several alternative options, never add their weights together. Return each option separately.
9. Detect letter-grade rules such as "C- or better". Put them in letter_requirements. Distinguish scope: use "course_pass" only if the outline says the student fails the course below that letter; use "prerequisite" when it only controls prerequisite eligibility. Never turn a prerequisite requirement into a course FAIL rule.

OUTLINE:
{_sanitize_extracted_text(row.content_text)[:120000]}"""
    used_fallback=False
    try:
        parsed = _gemini_json("You are a precise university course-outline parser. Output only valid JSON.", prompt, 5000, 0.1)
        if not isinstance(parsed,dict): raise ValueError('Invalid outline response')
    except Exception as exc:
        app.logger.warning('Gemini outline analysis failed; using local parser: %s', exc)
        parsed=_local_outline_analysis(row.content_text,course)
        used_fallback=True
    components = parsed.get('components') if isinstance(parsed,dict) else []
    if not isinstance(components,list): components=[]
    clean=[]; total=0.0
    for comp in components:
        if not isinstance(comp,dict): continue
        try: weight=float(comp.get('weight',0))
        except Exception: continue
        if weight <= 0 or weight > 100: continue
        clean.append({"name":str(comp.get('name') or 'Assessment')[:120],"weight":round(weight,2),"category":str(comp.get('category') or 'assessment')[:50],"notes":str(comp.get('notes') or '')[:500]})
        total += weight
    # Validate AI extraction against a deterministic parser. Prefer the result that looks like a complete grading scheme.
    local_parsed = _local_outline_analysis(row.content_text, course)
    local_components = local_parsed.get('components') or []
    local_total = sum(float(c.get('weight') or 0) for c in local_components if isinstance(c, dict))
    ai_plausible = bool(clean) and 90 <= total <= 110
    local_plausible = bool(local_components) and 90 <= local_total <= 110
    if local_parsed.get('confidence') == 'high' or local_parsed.get('grading_options') or (not ai_plausible and local_plausible) or (not clean and local_components):
        parsed = local_parsed
        clean = local_components
        total = local_total
        used_fallback = True
    # Preserve explicit letter-grade requirements even when the AI misses them.
    letter_requirements=parsed.get('letter_requirements') if isinstance(parsed.get('letter_requirements'),list) else []
    text_for_rules=row.content_text or ''
    for match in re.finditer(r'(?:required|must|need(?:ed)?)\s+(?:to\s+)?(?:achieve|obtain|earn)?[^.\n]{0,90}?\b([ABCDF][+-]?)\b\s+or\s+better',text_for_rules,re.I):
        letter=match.group(1).upper()
        sentence=re.split(r'[\n.]',text_for_rules[match.start():match.start()+240])[0].strip()
        nearby=text_for_rules[max(0,match.start()-100):match.start()+240].lower()
        scope='prerequisite' if 'prerequisite' in nearby else 'course_pass'
        if not any(str(x.get('minimum_letter','')).upper()==letter and x.get('scope')==scope for x in letter_requirements if isinstance(x,dict)):
            letter_requirements.append({'minimum_letter':letter,'scope':scope,'message':sentence or f'{letter} or better required'})
    parsed['letter_requirements']=letter_requirements
    if not parsed.get('input_components'): parsed['input_components']=clean
    parsed['components']=clean; parsed['total_weight']=round(float(parsed.get('total_weight') or total or 100),2)
    profile = CourseOutlineProfile.query.filter_by(user_id=current_user.id, course_full=course).first()
    if not profile:
        profile=CourseOutlineProfile(id=uuid4().hex,user_id=current_user.id,course_full=course)
        db.session.add(profile)
    profile.source_uid=row.source_uid; profile.outline_title=row.title; profile.grading_json=json.dumps(parsed,ensure_ascii=False)
    try: profile.pass_mark=float(parsed.get('pass_mark') or 50)
    except Exception: profile.pass_mark=50.0
    try: profile.final_minimum=None if parsed.get('final_minimum') in (None,'') else float(parsed.get('final_minimum'))
    except Exception: profile.final_minimum=None
    profile.analysis_notes='; '.join(str(x) for x in (parsed.get('rules') or []))[:4000]
    profile.updated_at=datetime.utcnow()
    # Synchronize the grade calculator with the analyzed outline while preserving marks.
    existing_rows=GradeEntry.query.filter_by(user_id=current_user.id,course_full=course).all()
    def _grade_key(value):
        return re.sub(r'[^a-z0-9]+',' ',str(value or '').lower()).strip()
    by_key={_grade_key(r.name):r for r in existing_rows}
    analyzed_keys=set()
    sync_components=parsed.get('input_components') or clean
    for comp in sync_components:
        key=_grade_key(comp['name']); analyzed_keys.add(key)
        existing=by_key.get(key)
        if not existing:
            existing=GradeEntry(id=uuid4().hex,user_id=current_user.id,course_full=course,name=comp['name'],weight=float(comp.get('weight') or 0),score=None,max_score=float(comp.get('default_max') or 100),category=comp['category'])
            db.session.add(existing)
        else:
            existing.name=comp['name']; existing.weight=float(comp.get('weight') or 0); existing.category=comp['category'];
            if existing.score is None: existing.max_score=float(comp.get('default_max') or existing.max_score or 100)
            existing.updated_at=datetime.utcnow()
    # Remove obsolete auto-created rows only when they have no student mark.
    for old_row in existing_rows:
        if _grade_key(old_row.name) not in analyzed_keys and old_row.score is None:
            db.session.delete(old_row)
    db.session.commit()
    return jsonify({"ok":True,"fallback":used_fallback,"analysis":parsed,"pass_mark":profile.pass_mark,"final_minimum":profile.final_minimum})


@app.route("/api/course/<course_code>/study-tools/generate", methods=["POST"])
@login_required
def generate_study_tool(course_code):
    course=_normalize_course_code(course_code)
    data=request.get_json(silent=True) or {}
    source_uid=str(data.get('source_uid') or '')
    artifact_type=str(data.get('artifact_type') or 'flashcards').lower()
    if artifact_type not in {'flashcards','quiz','study_guide','summary'}:
        return jsonify({"error":"Unsupported study tool."}),400
    row=CourseContent.query.filter_by(user_id=current_user.id,course_full=course,source_uid=source_uid).first()
    if not row:
        return jsonify({"error":"Choose a synced lecture or resource first."}),404
    if len((row.content_text or '').strip()) < 120:
        return jsonify({"needs_content":True,"content_request":_outline_request(row),"answer":f"Reading {row.title} from Brightspace…"}),409
    schemas={
      'flashcards':'{"title":"","cards":[{"front":"","back":"","source":"Slide 1"}]}',
      'quiz':'{"title":"","questions":[{"question":"","options":["A","B","C","D"],"answer":0,"explanation":"","source":"Slide 1"}]}',
      'study_guide':'{"title":"","sections":[{"heading":"","content":"","source":"Slide 1"}],"key_takeaways":[],"practice_prompts":[]}',
      'summary':'{"title":"","overview":"","key_concepts":[{"concept":"","explanation":"","source":"Slide 1"}],"takeaways":[]}'
    }
    instruction=f"Create a high-quality {artifact_type.replace('_',' ')} from the supplied university material. Return strict JSON matching: {schemas[artifact_type]}. Ground every item in the source, preserve slide/page references when available, cover the whole source, and do not invent facts."
    used_fallback = False
    try:
        parsed=_gemini_json(instruction, _sanitize_extracted_text(row.content_text)[:120000], 7000, 0.35)
        if not isinstance(parsed, dict):
            raise ValueError('Invalid study-tool response')
    except Exception as exc:
        app.logger.warning('Gemini study tool generation failed; using deterministic fallback: %s', exc)
        parsed=_local_study_artifact(artifact_type, row.title, row.content_text)
        used_fallback = True
    existing=StudyArtifact.query.filter_by(user_id=current_user.id,course_full=course,source_uid=row.source_uid,artifact_type=artifact_type).first()
    if not existing:
        existing=StudyArtifact(id=uuid4().hex,user_id=current_user.id,course_full=course,source_uid=row.source_uid,artifact_type=artifact_type)
        db.session.add(existing)
    existing.source_title=row.title; existing.title=str(parsed.get('title') or f"{row.title} {artifact_type}")[:500]; existing.data_json=json.dumps(parsed,ensure_ascii=False); existing.updated_at=datetime.utcnow()
    db.session.commit()
    return jsonify({"ok":True,"fallback":used_fallback,"artifact":{"id":existing.id,"type":artifact_type,"title":existing.title,"data":parsed}})


@app.route("/api/course/<course_code>/study-tools", methods=["GET"])
@login_required
def list_study_tools(course_code):
    course=_normalize_course_code(course_code)
    rows=StudyArtifact.query.filter_by(user_id=current_user.id,course_full=course).order_by(StudyArtifact.updated_at.desc()).all()
    out=[]
    for r in rows:
        try: payload=json.loads(r.data_json or '{}')
        except Exception: payload={}
        out.append({"id":r.id,"type":r.artifact_type,"title":r.title,"source_title":r.source_title,"data":payload})
    return jsonify({"artifacts":out})


@app.route("/api/sync/extension-key", methods=["GET", "POST"])
@login_required
def extension_key():
    if request.method == "POST" or not current_user.extension_token:
        current_user.extension_token = secrets.token_urlsafe(32)
        db.session.commit()
    return jsonify({"token": current_user.extension_token})


@app.route("/api/course/<course_code>/visibility", methods=["POST"])
@login_required
def course_visibility(course_code):
    """Remove a course and all of its Nexora data, while keeping a tombstone so sync cannot re-add it."""
    course = _normalize_course_code(course_code)
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "hide").lower()

    if action == "restore":
        HiddenCourse.query.filter_by(user_id=current_user.id, course_code=course).delete(synchronize_session=False)
        rows = SyncedCourse.query.filter_by(user_id=current_user.id, course_code=course).all()
        for row in rows:
            row.is_hidden = False
        db.session.commit()
        return jsonify({"ok": True, "hidden": False, "course": course})

    tombstone = HiddenCourse.query.filter_by(user_id=current_user.id, course_code=course).first()
    if not tombstone:
        db.session.add(HiddenCourse(id=uuid4().hex, user_id=current_user.id, course_code=course))

    deleted = {}
    for model, name, field in [
        (SyncedCourse, "courses", SyncedCourse.course_code),
        (CourseContent, "content", CourseContent.course_full),
        (AcademicUpdate, "updates", AcademicUpdate.course_full),
        (GradeEntry, "grades", GradeEntry.course_full),
        (CourseOutlineProfile, "outline_profiles", CourseOutlineProfile.course_full),
        (StudyArtifact, "study_artifacts", StudyArtifact.course_full),
        (SyncedFile, "files", SyncedFile.course_full),
        (Task, "tasks", Task.course_full),
    ]:
        deleted[name] = model.query.filter(model.user_id == current_user.id, field == course).delete(synchronize_session=False)

    db.session.commit()
    return jsonify({"ok": True, "hidden": True, "course": course, "deleted": deleted})


@app.route("/api/sync/courses", methods=["POST"])
def sync_courses():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401

    payload = request.get_json(silent=True) or {}
    courses = payload.get("courses") or []
    if not isinstance(courses, list):
        return jsonify({"error": "courses must be a list."}), 400

    counts = {"added": 0, "updated": 0, "unchanged": 0}
    imported = []
    for item in courses[:80]:
        url = str(item.get("brightspace_url") or item.get("url") or "").strip()[:4000]
        raw_code = str(item.get("course_code") or item.get("code") or item.get("title") or "OTHER")
        code = _normalize_course_code(raw_code)
        if HiddenCourse.query.filter_by(user_id=user.id, course_code=code).first():
            counts["unchanged"] += 1
            continue
        name = str(item.get("course_name") or item.get("name") or item.get("title") or code).strip()[:1000]
        # Defensive cleanup for raw Brightspace cross-list/section metadata.
        name = re.sub(r"^\s*(Merge|Crosslist)\s+", "", name, flags=re.I)
        name = re.sub(r"\[[\d:\s,]+\]", " ", name)
        name = re.sub(r"\bXLS[A-Z0-9_-]+\b", " ", name, flags=re.I)
        name = re.sub(r"\bSP[_-]?\d+\b", " ", name, flags=re.I)
        name = re.sub(r"\b(Fall|Winter|Summer|Spring)\s+20\d{2}\b", " ", name, flags=re.I)
        name = re.sub(r"\((LEC|LAB|TUT|SEM|DIS|PRA)(?:\s+[^)]*)?\)", " ", name, flags=re.I)
        name = re.sub(r"^[\s,._-]+|[\s,._-]+$", "", re.sub(r"\s+", " ", name)).strip() or code
        source_uid = str(item.get("source_uid") or url or f"{code}|{name}")[:255]
        incoming_image = str(item.get("image_url") or "")[:4000]
        data = {
            "course_code": code,
            "course_name": name,
            "term": str(item.get("term") or "")[:120],
            "instructor": str(item.get("instructor") or "")[:255],
            "image_url": incoming_image,
            "brightspace_url": url,
        }
        existing = SyncedCourse.query.filter_by(user_id=user.id, source_uid=source_uid).first()
        if not existing and code != "OTHER":
            existing = SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first()
        if existing and not incoming_image:
            data["image_url"] = existing.image_url or ""
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        if existing:
            if existing.source_hash == fingerprint:
                counts["unchanged"] += 1
            else:
                existing.source_uid = source_uid
                existing.course_code = code
                existing.course_name = name
                existing.term = data["term"]
                existing.instructor = data["instructor"]
                existing.image_url = data["image_url"]
                existing.brightspace_url = url
                existing.source_hash = fingerprint
                existing.updated_at = datetime.utcnow()
                counts["updated"] += 1
        else:
            db.session.add(SyncedCourse(
                id=uuid4().hex, user_id=user.id, source_uid=source_uid,
                course_code=code, course_name=name, term=data["term"],
                instructor=data["instructor"], image_url=data["image_url"],
                brightspace_url=url, source_hash=fingerprint,
            ))
            counts["added"] += 1
            db.session.add(AcademicUpdate(
                id=uuid4().hex, user_id=user.id, course_full=code,
                update_type="new_course", title=f"Course imported: {code}",
                detail=name, related_uid=source_uid,
            ))
        imported.append({"course_code": code, "course_name": name, "brightspace_url": url})

    db.session.commit()
    return jsonify({"ok": True, **counts, "courses": imported})


@app.route("/api/sync/status")
def extension_sync_status():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401
    hidden_codes = {h.course_code for h in HiddenCourse.query.filter_by(user_id=user.id).all()}
    courses = [c for c in SyncedCourse.query.filter_by(user_id=user.id).order_by(SyncedCourse.updated_at.desc()).all() if c.course_code not in hidden_codes]
    content_total = CourseContent.query.filter_by(user_id=user.id).filter(~CourseContent.course_full.in_(hidden_codes) if hidden_codes else True).count()
    return jsonify({
        "ok": True,
        "course_count": len(courses),
        "content_count": content_total,
        "courses": [{
            "code": c.course_code, "name": c.course_name, "image_url": c.image_url,
            "source_uid": c.source_uid, "url": c.brightspace_url, "updated_at": c.updated_at.isoformat() if c.updated_at else ""
        } for c in courses]
    })


@app.route("/api/sync/content", methods=["POST"])
def sync_course_content():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401

    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list."}), 400

    hidden_codes = {h.course_code for h in HiddenCourse.query.filter_by(user_id=user.id).all()}
    replace_courses = [_normalize_course_code(c) for c in (payload.get("replace_courses") or [])]
    replace_courses = [c for c in replace_courses if c not in hidden_codes]
    if replace_courses:
        CourseContent.query.filter(CourseContent.user_id == user.id, CourseContent.course_full.in_(replace_courses)).delete(synchronize_session=False)
        # Smart Sync refreshes metadata only; preserve any user-requested cached files.
        db.session.flush()
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for item in items[:1200]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        course = _normalize_course_code(item.get("course") or item.get("course_full") or "OTHER")
        if course in hidden_codes:
            counts["unchanged"] += 1
            continue
        source_url = str(item.get("source_url") or "")[:4000]
        source_uid = str(item.get("source_uid") or source_url or f"{course}|{title}")[:255]
        item_data = {
            "title": title,
            "content_type": str(item.get("content_type") or "resource")[:30],
            "source_url": source_url,
            "content_text": str(item.get("content_text") or "")[:120000],
            "module_name": str(item.get("module_name") or "")[:1000],
            "published_at": str(item.get("published_at") or "")[:80],
        }
        fingerprint = _content_payload_hash(item_data)
        existing = CourseContent.query.filter_by(user_id=user.id, source_uid=source_uid).first()
        if existing:
            if existing.content_hash == fingerprint:
                counts["unchanged"] += 1
                continue
            existing.course_full = course
            existing.title = title
            existing.content_type = item_data["content_type"]
            existing.source_url = source_url
            existing.content_text = item_data["content_text"]
            existing.module_name = item_data["module_name"]
            existing.published_at = item_data["published_at"]
            existing.content_hash = fingerprint
            existing.updated_at = datetime.utcnow()
            counts["updated"] += 1
            db.session.add(AcademicUpdate(
                id=uuid4().hex, user_id=user.id, course_full=course,
                update_type="content_updated", title=f"{title} updated",
                detail=f"{item_data['content_type'].title()} content changed in Brightspace.",
                related_uid=source_uid,
            ))
        else:
            db.session.add(CourseContent(
                id=uuid4().hex, user_id=user.id, course_full=course,
                title=title, content_type=item_data["content_type"],
                source_url=source_url, source_uid=source_uid,
                content_text=item_data["content_text"], module_name=item_data["module_name"],
                published_at=item_data["published_at"], content_hash=fingerprint,
            ))
            counts["added"] += 1
            db.session.add(AcademicUpdate(
                id=uuid4().hex, user_id=user.id, course_full=course,
                update_type="new_content", title=f"New {item_data['content_type']}: {title}",
                detail=item_data["module_name"] or "Imported from Brightspace.",
                related_uid=source_uid,
            ))
            if item_data["content_type"] == "lecture":
                db.session.add(AcademicUpdate(
                    id=uuid4().hex, user_id=user.id, course_full=course,
                    update_type="ai_recommendation", title=f"Review recommended: {title}",
                    detail="Nexora detected a new lecture. Open Study Tools to create flashcards, a quiz, or a study guide, and schedule a 30–45 minute review.",
                    related_uid=source_uid,
                ))
            elif item_data["content_type"] == "outline":
                db.session.add(AcademicUpdate(
                    id=uuid4().hex, user_id=user.id, course_full=course,
                    update_type="outline_detected", title="Course outline ready for analysis",
                    detail="Analyze it once to build the official grading scheme and course-specific outcome calculator.",
                    related_uid=source_uid,
                ))
    # Keep course cards aware of how much content was imported.
    touched_courses = { _normalize_course_code((item or {}).get("course") or (item or {}).get("course_full") or "OTHER") for item in items if isinstance(item, dict) }
    for code in touched_courses:
        course_row = SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first()
        if course_row:
            course_row.content_count = CourseContent.query.filter_by(user_id=user.id, course_full=code).count()
            course_row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, **counts, "received": len(items), "replaced_courses": replace_courses})


@app.route("/api/updates")
@login_required
def academic_updates_api():
    updates = AcademicUpdate.query.filter_by(user_id=current_user.id).order_by(AcademicUpdate.created_at.desc()).limit(20).all()
    return jsonify({"updates": [{
        "id": u.id, "course": u.course_full, "type": u.update_type,
        "title": u.title, "detail": u.detail,
        "created_at": u.created_at.isoformat() if u.created_at else "",
        "is_read": bool(u.is_read),
    } for u in updates]})


@app.route("/api/course/<course_code>/ai-context")
@login_required
def course_ai_context(course_code):
    course = _normalize_course_code(course_code)
    contents = CourseContent.query.filter_by(user_id=current_user.id, course_full=course).order_by(CourseContent.updated_at.desc()).limit(20).all()
    cards = [c for c in sort_cards(build_grouped_cards(load_tasks())) if _normalize_course_code(c.get("course_full")) == course]
    return jsonify({
        "course": course,
        "content": [{"title": c.title, "type": c.content_type, "module": c.module_name, "text": (c.content_text or "")[:5000]} for c in contents],
        "assignments": [{"title": c.get("title"), "due": c.get("main_due_raw")} for c in cards],
    })


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


@app.route("/grade-calculator")
@login_required
def grade_calculator():
    return render_template("grade_calculator.html")


@app.route("/api/analyze-outline", methods=["POST"])
@login_required
def analyze_outline_generic():
    """Analyze an uploaded or pasted course outline and return grade components."""
    text = ""
    filename = "Pasted course outline"
    uploads = request.files.getlist("files") or request.files.getlist("file")
    uploads = [u for u in uploads if u and u.filename]
    if uploads:
        if len(uploads) > 8:
            return jsonify({"success": False, "error": "Upload at most 8 screenshots/pages at once.", "components": []}), 400
        try:
            parts = []
            names = []
            for index, upload in enumerate(uploads, 1):
                item_name, extracted, _file_hash, _size = _extract_uploaded_course_file(upload)
                extracted = _sanitize_extracted_text(extracted)
                if not extracted:
                    raise ValueError(f"Nexora could not read visible text from {item_name}.")
                names.append(item_name)
                parts.append(f"\n===== UPLOAD {index}: {item_name} =====\n{extracted}")
            filename = names[0] if len(names) == 1 else f"{len(names)} outline uploads"
            text = "\n".join(parts)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc), "components": []}), 422
        except Exception:
            app.logger.exception("Generic outline upload failed")
            return jsonify({"success": False, "error": "Nexora could not read one of these outline files.", "components": []}), 422
    else:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "")

    text = _sanitize_extracted_text(text)
    if len(text.strip()) < 20:
        return jsonify({"success": False, "error": "The outline text is empty or too short.", "components": []}), 400

    try:
        prompt = (
            "Analyze this university course outline and return strict JSON only:\n"
            '{"pass_mark":50,"final_minimum":null,"components":[{"name":"Assignments","weight":20,"category":"assignments","notes":""}],"component_minimums":[],"rules":[]}\n'
            "Extract only genuine grading categories and their weights. Ignore bonus marks. "
            "Detect any rule requiring a minimum score on the final or another component.\n\n"
            f"OUTLINE:\n{text[:120000]}"
        )
        parsed = _gemini_json(
            "You are a precise university course-outline parser. Output only valid JSON.",
            prompt,
            4000,
            0.1,
        )
    except Exception as exc:
        app.logger.warning("Generic Gemini outline analysis failed; using local parser: %s", exc)
        parsed = _local_outline_analysis(text, "COURSE")

    if not isinstance(parsed, dict):
        parsed = {}

    components = []
    total = 0.0
    for comp in parsed.get("components") or []:
        if not isinstance(comp, dict):
            continue
        try:
            weight = float(comp.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        name = str(comp.get("name") or "Assessment").strip()
        if not name or weight <= 0 or weight > 100:
            continue
        components.append({
            "name": name[:120],
            "weight": round(weight, 2),
            "score": "",
            "category": str(comp.get("category") or "assessment")[:50],
            "notes": str(comp.get("notes") or "")[:500],
        })
        total += weight

    return jsonify({
        "success": bool(components),
        "filename": filename,
        "components": components,
        "total_weight": round(total, 2),
        "pass_mark": parsed.get("pass_mark", 50),
        "final_minimum": parsed.get("final_minimum"),
        "component_minimums": parsed.get("component_minimums") or [],
        "rules": parsed.get("rules") or [],
        "error": "" if components else "No clear grade components were detected.",
    })

if __name__ == "__main__":
    app.run(debug=True)

@app.route("/api/sync/file", methods=["POST"])
def sync_course_file():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "No file supplied."}), 400
    try:
        metadata = json.loads(request.form.get("metadata") or "{}")
    except Exception:
        metadata = {}
    course = _normalize_course_code(metadata.get("course") or "OTHER")
    source_uid = str(metadata.get("source_uid") or metadata.get("source_url") or uuid4().hex)[:255]
    raw_name = metadata.get("file_name") or uploaded.filename or "resource"
    filename = secure_filename(raw_name) or f"resource-{uuid4().hex[:8]}"
    data = uploaded.read()
    if len(data) > 25 * 1024 * 1024:
        return jsonify({"error": "File exceeds 25 MB limit."}), 413
    digest = hashlib.sha256(data).hexdigest()
    folder = os.path.join(app.root_path, "static", "nexora_uploads", str(user.id), course)
    os.makedirs(folder, exist_ok=True)
    stored_name = f"{digest[:14]}-{filename}"
    path = os.path.join(folder, stored_name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    stored_url = url_for("static", filename=f"nexora_uploads/{user.id}/{course}/{stored_name}")
    record = SyncedFile.query.filter_by(user_id=user.id, source_uid=source_uid).first()
    if not record:
        record = SyncedFile(id=uuid4().hex, user_id=user.id, source_uid=source_uid)
        db.session.add(record)
    record.course_full = course
    record.original_source_url = str(metadata.get("source_url") or "")[:4000]
    record.stored_url = stored_url
    record.file_name = filename
    record.mime_type = uploaded.mimetype or "application/octet-stream"
    record.file_size = len(data)
    record.content_hash = digest
    record.updated_at = datetime.utcnow()
    content = CourseContent.query.filter_by(user_id=user.id, source_uid=source_uid).first()
    if content:
        content.source_url = stored_url
        original = record.original_source_url
        content.content_text = json.dumps({"original_source_url": original, "stored_file": True})
        content.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "stored_url": stored_url, "file_name": filename, "size": len(data)})


