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
import threading
import smtplib
from email.message import EmailMessage
import zipfile
import base64
from datetime import datetime, date, timedelta
import calendar as calendar_module
from uuid import uuid4
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
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

# Error reporting is opt-in: without SENTRY_DSN this is inert, so the app runs
# unchanged for anyone who has not set one up. Until something like this is
# configured, a crash in production is only discovered by a student hitting it
# and telling you.
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            # Grades, marks and outline text are the student's private record and
            # have no place in an error report.
            send_default_pii=False,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        )
    except Exception:  # never let monitoring take the site down
        pass

app = Flask(__name__)

database_url = os.environ.get("DATABASE_URL")

# DATABASE_URL is the most reliable signal that this is the deployed service
# rather than someone's laptop: Render sets it, local development deliberately
# leaves it unset so the app falls back to throwaway SQLite.
IS_PRODUCTION = bool(database_url)

_secret_key = os.environ.get("SECRET_KEY", "").strip()
if not _secret_key:
    if IS_PRODUCTION:
        # Falling back to a shared constant here would sign every session cookie
        # with a string that is public in the repository, so anyone could forge a
        # session for any user id. Refusing to boot is far safer than serving a
        # site whose logins can be minted by a stranger.
        raise RuntimeError(
            "SECRET_KEY is not set. Set it in the service environment before "
            "starting: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    _secret_key = "dev-secret-key-change-later"
app.config["SECRET_KEY"] = _secret_key

# Session hardening. HTTPONLY already defaults on; the rest never were.
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION  # HTTPS-only in production
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # states the CSRF defence we relied on browsers to guess
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
app.config["REMEMBER_COOKIE_SECURE"] = IS_PRODUCTION
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uni_helper.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # up to 8 outline screenshots/pages

db = SQLAlchemy(app)


def _rate_limit_identity():
    """Limit signed-in users by account, everyone else by address.

    Keying the AI routes on the account is the point: the expensive endpoints
    all sit behind @login_required, and one authenticated student rotating
    addresses should still not be able to spend the whole Gemini quota that
    every other student's outline analysis depends on.
    """
    try:
        if current_user.is_authenticated:
            return f"user:{current_user.id}"
    except Exception:
        pass
    return get_remote_address()


# With no shared store, counters live in each gunicorn worker's memory, so a
# limit of N is really N per worker and resets on restart. That still turns
# "unbounded" into "bounded", but the numbers mean what they say only when the
# workers share a store. Point REDIS_URL at a Redis / Render Key Value instance
# and the configured limits become exact, with no code change.
RATE_LIMIT_STORAGE = (
    os.environ.get("RATE_LIMIT_STORAGE_URI", "").strip()
    or os.environ.get("REDIS_URL", "").strip()
    or "memory://"
)

limiter = Limiter(
    key_func=_rate_limit_identity,
    app=app,
    storage_uri=RATE_LIMIT_STORAGE,
    strategy="fixed-window",
    headers_enabled=True,
)

# Gemini work is the scarce resource: an outline analysis is a three-stage
# pipeline, so a handful of these is worth many ordinary requests.
AI_LIMIT = "40 per day; 12 per hour"

# Email is configured entirely from the environment so any SMTP provider works
# without a code change -- Gmail with an app password costs nothing and is
# plenty at student volume, and moving to a dedicated sender later is a matter
# of changing these values. When nothing is configured the app says so honestly
# rather than accepting a reset request it cannot fulfil.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() not in ("0", "false", "no")
# Someone locked out of their account cannot reach the in-app feedback page, so
# telling them to "contact support" is only useful with an address attached.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "").strip()
PASSWORD_RESET_TTL = timedelta(hours=1)


def email_is_configured():
    return bool(SMTP_HOST and SMTP_FROM)


def send_email(to_address, subject, body):
    """Send a plain-text message. Returns True on success, False on failure.

    Never raises: a failure to send must not turn into a 500 that tells the
    caller which addresses exist.
    """
    if not email_is_configured():
        app.logger.warning("Email requested but SMTP is not configured; dropping message to %s", to_address)
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = to_address
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
        return True
    except Exception as exc:
        app.logger.exception("Could not send email to %s: %s", to_address, exc)
        return False


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
    notes = db.Column(db.String(255), default="")
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
    last_sync_ok = db.Column(db.Boolean, default=True)
    last_sync_error = db.Column(db.Text, default="")
    last_sync_items = db.Column(db.Integer, default=0)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HiddenCourse(db.Model):
    """A per-user tombstone so manually removed courses stay removed after sync."""
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_code = db.Column(db.String(40), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PasswordResetToken(db.Model):
    """A single-use, expiring password reset link.

    Only a hash of the token is stored. The plain token exists in the emailed
    link and nowhere else, so reading this table does not let anyone reset an
    account -- the same reasoning that applies to password_hash.
    """
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(128), nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
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
            "last_sync_ok": "BOOLEAN DEFAULT TRUE",
            "last_sync_error": "TEXT DEFAULT ''",
            "last_sync_items": "INTEGER DEFAULT 0",
            "last_sync_at": "TIMESTAMP NULL",
        },
        "task": {
            "source_uid": "VARCHAR(255) DEFAULT ''",
            "source_hash": "VARCHAR(64) DEFAULT ''",
            "source_updated": "VARCHAR(80) DEFAULT ''",
            "sync_state": "VARCHAR(30) DEFAULT 'active'",
            "last_seen_sync": "VARCHAR(40) DEFAULT ''",
            "completed": "BOOLEAN DEFAULT FALSE",
        },
        "grade_entry": {
            "notes": "VARCHAR(255) DEFAULT ''",
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




def _gemini_model_candidates(vision=False, reasoning=False):
    """Return resilient Gemini model candidates, newest stable first.

    Environment values are honored first, but the app always includes current
    stable fallbacks so a retired/blocked model ID cannot break uploads.
    """
    env_names = []
    if vision:
        env_names.append(os.environ.get("GEMINI_VISION_MODEL", "").strip())
    env_names.extend([
        os.environ.get("GEMINI_MODEL", "").strip(),
        os.environ.get("GEMINI_FALLBACK_MODEL", "").strip(),
    ])
    defaults = [
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
    ]
    if reasoning:
        # Outline analysis is the one task where reasoning quality decides
        # whether a student is shown a correct grade. Prefer Pro-tier models
        # and fall back to flash only if the key cannot serve them at all.
        env_reasoning = os.environ.get("GEMINI_REASONING_MODEL", "").strip()
        # "gemini-3.6-pro" is not a real model ID -- it 404s on every call and
        # silently dropped outline analysis down to flash, defeating the whole
        # point of this branch. Only IDs the API actually serves belong here.
        defaults = [m for m in [
            env_reasoning,
            "gemini-pro-latest",
            "gemini-3.1-pro-preview",
        ] if m] + defaults
    result = []
    for model in [*env_names, *defaults]:
        model = (model or "").strip()
        if model and model not in result:
            result.append(model)
    return result


# A model that just answered 429 will answer 429 again, and on a free key the
# Pro-tier candidates do so on every single call. Remembering that for a few
# minutes stops each request paying the same toll, which matters most precisely
# when quota is short. Kept in memory deliberately: it is a hint, not state
# worth a database round trip, and each worker learns it within one request.
_QUOTA_COOLDOWN_SECONDS = 600
_MISSING_MODEL_COOLDOWN_SECONDS = 86400
_gemini_model_cooldowns = {}
_gemini_cooldown_lock = threading.Lock()


def _looks_like_quota_error(exc):
    """Tell 'we are out of quota' apart from 'something broke'.

    They need different words: one is a wait, the other is a fault, and telling
    a student to try again in a few minutes when the daily quota is gone just
    makes them try again in a few minutes.
    """
    text = str(exc or "")
    return "429" in text or "quota" in text.lower() or "rate limit" in text.lower()


def _fallback_is_usable(parsed):
    """Is the deterministic parse complete enough to put in front of a student?

    Weights that do not add up are the tell. A parse with no components at all,
    or one whose total is nowhere near 100, has not understood the outline --
    presenting it as a policy to confirm invites the student to accept a grading
    scheme that will quietly mis-state their grade all term.
    """
    if not isinstance(parsed, dict):
        return False
    if parsed.get('grading_options'):
        return True
    components = [c for c in (parsed.get('components') or []) if isinstance(c, dict)]
    if not components:
        return False
    total = sum(float(c.get('weight') or 0) for c in components)
    return 90.0 <= total <= 110.0


def _mark_gemini_model_unavailable(model, seconds):
    with _gemini_cooldown_lock:
        _gemini_model_cooldowns[model] = time.time() + seconds


def _usable_gemini_models(models):
    """Drop candidates known to be unavailable, unless that would leave none.

    Falling back to the full list when everything is cooling down matters: a
    cooldown is a guess, and a wrong guess must never be the reason a student
    gets no analysis at all.
    """
    now = time.time()
    with _gemini_cooldown_lock:
        usable = [m for m in models if _gemini_model_cooldowns.get(m, 0) <= now]
    return usable or list(models)


def _gemini_http_detail(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        message = (((payload.get("error") or {}).get("message")) or raw).strip()
    except Exception:
        message = str(exc)
    return re.sub(r"\s+", " ", message)[:500]


def _gemini_json(system_prompt, user_prompt, max_tokens=4096, temperature=0.2, reasoning=False, used_model=None):
    """Call Gemini for strict JSON with retries and a model fallback on transient 5xx errors."""
    api_key = (os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip())
    if not api_key:
        raise RuntimeError("Gemini is not configured")
    models = _usable_gemini_models(_gemini_model_candidates(vision=False, reasoning=reasoning))
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
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
        # Structured JSON responses of this size normally return in a few
        # seconds. A 90s timeout x 3 attempts x 5 model candidates gave a
        # worst-case of ~22 minutes for a SINGLE call in this pipeline, which
        # runs 3 of these sequentially -- easily explaining multi-minute
        # stalls on any transient rate limit or slow model. Tightening this
        # only shrinks the failure-path ceiling; a normal successful call
        # still returns immediately regardless of the timeout value.
        for attempt in range(2):
            try:
                # Thinking models bill internal reasoning against
                # maxOutputTokens, so a reply can be cut off before the JSON
                # closes. Retry with a larger budget instead of replaying the
                # same one that already ran out.
                body["generationConfig"]["maxOutputTokens"] = (
                    max_tokens if attempt == 0 else max(max_tokens * 2, 8192)
                )
                req = urllib.request.Request(endpoint, data=json.dumps(body).encode('utf-8'), headers={"Content-Type":"application/json"}, method='POST')
                with urllib.request.urlopen(req, timeout=30) as response:
                    result = json.loads(response.read().decode('utf-8'))
                candidate = (result.get('candidates') or [{}])[0]
                parts = (((candidate.get('content') or {}).get('parts')) or [])
                text_value = "\n".join(p.get('text','') for p in parts if isinstance(p,dict)).strip()
                if candidate.get('finishReason') == 'MAX_TOKENS':
                    raise ValueError('response truncated before the JSON closed')
                if not text_value:
                    raise ValueError('Gemini returned no JSON')
                text_value = re.sub(r'^```(?:json)?\s*|\s*```$', '', text_value, flags=re.I|re.S).strip()
                if isinstance(used_model, list):
                    used_model.append(model)
                return json.loads(text_value)
            except urllib.error.HTTPError as exc:
                detail = _gemini_http_detail(exc)
                last_error = RuntimeError(f"{model}: HTTP {exc.code}: {detail}")
                if exc.code == 429:
                    # Quota, not a blip. Retrying the same model 1.5s later spends
                    # another request to be told the same thing, and on a free key
                    # the Pro candidates 429 on every call -- roughly eight doomed
                    # requests per outline analysis, against the very quota the
                    # rest of the pipeline needs. Park the model and move on.
                    _mark_gemini_model_unavailable(model, _QUOTA_COOLDOWN_SECONDS)
                    break
                if exc.code == 404:
                    # This id is not served for this key at all; nothing will
                    # change within the life of the process.
                    _mark_gemini_model_unavailable(model, _MISSING_MODEL_COOLDOWN_SECONDS)
                    break
                if exc.code in (500, 502, 503, 504) and attempt < 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # 400/403/404 usually means this specific model is unavailable
                # for the current project/key. Move on to the next candidate.
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except ValueError as exc:
                # Covers JSONDecodeError plus the truncated/empty cases above.
                # These used to escape the loop entirely, so one malformed
                # reply killed every remaining retry and model candidate
                # instead of falling through to one that works.
                last_error = RuntimeError(f"{model}: {exc}")
                if attempt < 1:
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


# Matches text that is ENTIRELY a bracketed qualifier -- "out of 12)", "(best
# 4 of 5)", "worth 12 marks" -- with no real label attached. On its own this
# is never a usable component name; it only ever qualifies a name that
# belongs on a separate, adjacent line.
_QUALIFIER_ONLY_FRAGMENT = re.compile(
    r'^\(?\s*(?:out\s+of\s+\d+|worth\s+\d+(?:\s+\w+)?|(?:best|top|highest|lowest)\s+\d+\s+(?:out\s+of|of)\s+\d+|'
    r'min(?:imum)?\s+\d+|max(?:imum)?\s+\d+)\s*\)?$',
    re.I,
)


_LEADING_ARTICLE = re.compile(r'^(?:the|a|an)\s+', re.I)


# Phrases that only occur when a regex has matched part of a sentence stating a
# requirement, never when it has matched the name of an assessment.
_RULE_PROSE_FRAGMENT = re.compile(r'\b(?:must|obtain|achieve)\b', re.I)


def _looks_like_grading_label(name):
    """Reject sentence-fragment matches that only accidentally look like 'Label ... N%'."""
    lower = name.strip().lower()
    if not lower or len(lower.split()) > 8:
        return False
    # Rule prose reliably reads as an instruction to the student rather than as
    # the name of a thing. Without this, the sentence "Students must obtain at
    # least 30% from at least one test, ... obtain at least 50% on the total
    # course grade" contributed two components worth 30% and 50%, which pushed
    # the option they trailed past any sane weight total and silently discarded
    # the whole option.
    if _RULE_PROSE_FRAGMENT.search(lower):
        return False
    # A leading article is ordinary in a real label -- "The average of best two
    # written tests" -- but 'the' sits in the junk-start list because fragments
    # often open with it too. Judge the phrase after the article so a genuine
    # name is not thrown away for its first word.
    stripped = _LEADING_ARTICLE.sub('', lower, count=1).strip() or lower
    if _JUNK_FRAGMENT_STARTS.match(stripped):
        return False
    if any(x in lower for x in ['bonus', 'mentorship', 'claim your', 'awarded the', 'earn your', 'quiz to earn']):
        return False
    # An unbalanced trailing ')' (more closes than opens) almost always means
    # this text is the tail half of a parenthetical whose opening half was on
    # a previous, separately-consumed line -- e.g. a table cell "Assessment"
    # then "(out of 12)" on the next line, where the heading got dropped as
    # generic and only "out of 12)" survived as a fake component name.
    if lower.count(')') > lower.count('('):
        return False
    if _QUALIFIER_ONLY_FRAGMENT.match(lower):
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



def _component_keys_match(key, other):
    """True when two normalised component keys denote the same assessment.

    Loose containment is deliberate: a rule often names a component more
    briefly than the outline did ("Labs" vs "Labs (all 5 count)"), and those
    must resolve to one card instead of two.

    Numbered siblings are the exception. Plain containment made "Lab 1" match
    "Lab 10", so in a numbered group every member past 9 was silently dropped
    -- a "best 10 of 13" rule produced only 9 enterable labs, making the rule
    impossible to satisfy. When both keys end in a number they are the same
    component only if stem and number both match.
    """
    if not key or not other:
        return False
    if key == other:
        return True
    a = re.match(r'^(.*?)\s*(\d+)$', key)
    b = re.match(r'^(.*?)\s*(\d+)$', other)
    if a and b:
        return a.group(1) == b.group(1) and a.group(2) == b.group(2)
    return key in other or other in key



_GROUP_NOTE_RE = re.compile(r'^Counts toward\s+(.+?)\s*(?:—|--|\.|$)\s*(.*)$', re.I)
_REQUIREMENT_NOTE = 'required minimum applies'


def _parse_group_note(notes):
    """Pull the parent name and rule summary out of a group-member note.

    Members are tagged at registration time with notes like
    "Counts toward Labs — best 10 of 13 counts." Returns
    (parent_name, rule_text) or (None, '').
    """
    m = _GROUP_NOTE_RE.match(str(notes or '').strip())
    if not m:
        return None, ''
    return (m.group(1) or '').strip(' .'), (m.group(2) or '').strip(' .')


def _group_grade_entries(entries):
    """Fold group members underneath the component they count toward.

    The calculator used to render every registered component as an equal
    full-size card, so an outline with 13 labs and 6 quizzes produced a wall
    of 21 boxes with no indication that most of them rolled up into two.

    Returns a list of dicts with:
      kind      'group' | 'single' | 'requirement'
      parent    the GradeEntry the card is built from
      children  member entries, empty for non-groups
      rule      human-readable selection rule, e.g. "best 10 of 13 counts"
    """
    entries = list(entries or [])
    members = {}          # parent key -> [child entries]
    rules = {}            # parent key -> rule text
    child_ids = set()

    for e in entries:
        parent_name, rule_text = _parse_group_note(getattr(e, 'notes', ''))
        if not parent_name or float(getattr(e, 'weight', 0) or 0) >= 0.05:
            continue
        for candidate in entries:
            if candidate is e:
                continue
            if _component_keys_match(
                re.sub(r'[^a-z0-9]+', ' ', parent_name.lower()).strip(),
                re.sub(r'[^a-z0-9]+', ' ', str(candidate.name or '').lower()).strip(),
            ):
                members.setdefault(candidate.id, []).append(e)
                rules.setdefault(candidate.id, rule_text)
                child_ids.add(e.id)
                break

    groups = []
    for e in entries:
        if e.id in child_ids:
            continue
        kids = members.get(e.id) or []
        if kids:
            groups.append({'kind': 'group', 'parent': e, 'children': kids,
                           'rule': rules.get(e.id, '')})
            continue
        notes = str(getattr(e, 'notes', '') or '').lower()
        if _REQUIREMENT_NOTE in notes and float(getattr(e, 'weight', 0) or 0) < 0.05:
            groups.append({'kind': 'requirement', 'parent': e, 'children': [], 'rule': ''})
            continue
        groups.append({'kind': 'single', 'parent': e, 'children': [], 'rule': ''})
    return groups


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
    # "best N out of M" / "top N of M"-style selection qualifiers carry real
    # grading information (which marks get dropped) and must survive even when
    # the base name below gets collapsed to a canonical label.
    qualifier=re.search(r'\(\s*(?:best|top|highest)\s+\d+\s+(?:out\s+of|of)\s+\d+\s*\)',raw,re.I)
    for pat,label in patterns:
        m=re.search(pat,lower,re.I)
        if m:
            n=(m.groupdict().get('n') or '').replace('#','').upper()
            canonical=label.format(n=n).strip()
            if qualifier:
                canonical=f"{canonical} {qualifier.group(0)}"
            return canonical
    # Generic table label fallback, but reject sentence fragments.
    raw=re.split(r'\b(?:closed\s+book|open\s+book|tentatively|scheduled|due|weekend|description)\b',raw,maxsplit=1,flags=re.I)[0].strip(' :-–—.,;')
    return raw[:80] if _looks_like_grading_label(raw) else ''


def _extract_components_from_section(section):
    components=[]; seen=set()
    lines=[' '.join(x.replace('|',' ').split()) for x in section.splitlines()]
    heading_counts={}

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
        start=i+1
        # A standalone generic heading ("Assessment", "Item", ...) is not a
        # usable component name on its own, but the very next line is often
        # its real qualifier split onto its own PDF-table cell/row --
        # "Assessment" / "(out of 12)" / "15%". Merge the two into one real
        # label instead of either discarding the heading entirely or (see the
        # unbalanced-parens/qualifier-only guards in _looks_like_grading_label)
        # accidentally adopting the orphaned qualifier fragment alone as a
        # fake component name.
        if name.lower() in {'assessment','item','description','weight','coursework'}:
            nxt=lines[i+1] if i+1<len(lines) else ''
            merged=_canonical_component_name(f'{line} {nxt}') if nxt else ''
            if merged and merged.lower()!=name.lower():
                name=merged; start=i+2
            elif nxt and re.fullmatch(r'\([^()]{1,60}\)',nxt.strip()):
                # The heading has no distinguishing text of its own (e.g. a
                # bare "Assessment" cell followed only by a qualifier cell
                # like "(out of 12)"), and _canonical_component_name always
                # strips that exact heading word as generic noise -- so the
                # merge above can never produce anything useful here. Number
                # the heading instead of silently dropping the component or
                # keeping only the orphaned qualifier as a fake name.
                heading_counts[name]=heading_counts.get(name,0)+1
                numbered=f"{name} {heading_counts[name]} {nxt.strip()}"
                if _looks_like_grading_label(numbered):
                    name=numbered; start=i+2
                else:
                    continue
            else:
                continue
        for j in range(start,min(len(lines),start+7)):
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
            r'(?:must|required\s+to)\s+(?:pass|obtain|earn|achieve)[^.\n]{0,80}?final(?:\s+exam|\s+examination)?[^.\n]{0,50}?(\d{1,3}(?:\.\d+)?)\s*%',
            r'(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%[^.\n]{0,90}?final(?:\s+exam|\s+examination)',
            r'final(?:\s+exam|\s+examination)[^.\n]{0,60}?(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%',
        ]),
        ('Labs',[
            r'(?:must|required\s+to).{0,80}?lab(?:s|oratory)?.{0,50}?(\d{1,3}(?:\.\d+)?)\s*%',
            r'lab(?:s|oratory)?.{0,60}?(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%',
        ]),
    ]
    def _is_aggregate_mention(span_text,keyword_pattern):
        """True if the keyword sits inside an unclosed, comma-separated parenthetical
        (e.g. "theory component (quizzes, tutorials and final exam)") naming it as one
        of several components in a group, rather than as its own standalone minimum."""
        km=re.search(keyword_pattern,span_text,re.I)
        if not km: return False
        before=span_text[:km.start()]
        return before.count('(')>before.count(')') and ',' in before
    for name,pats in rule_specs:
        value=None
        for pat in pats:
            m=re.search(pat,text,re.I|re.S)
            if m:
                if name=='Final Exam' and _is_aggregate_mention(m.group(0),r'final(?:\s+exam|\s+examination)'):
                    continue
                try: value=float(m.group(1)); break
                except Exception: pass
        if value is not None:
            minimums.append({'name':name,'minimum':value,'consequence':f'Must meet the {name} minimum to pass the course'})
            structured_rules.append({'type':'component_minimum','component':name,'minimum_percent':value,'message':f'{name} must be at least {value:g}% to pass.'})
            if name=='Final Exam': final_minimum=value
    # Group rules such as "at least 50% in the labs and 50% in the non-lab portion".
    group_patterns=[
        (r'(?:must|required to)\s+(?:obtain|achieve|earn|score)?\s*(\d{1,3}(?:\.\d+)?)\s*%[^.\n]{0,80}?\b(?:in|on)\s+(?:the\s+)?labs?\b', 'lab', False, 'Lab average'),
        (r'(?:labs?|laboratory component)[^.\n]{0,80}?(?:must|required to)[^.\n]{0,30}?(\d{1,3}(?:\.\d+)?)\s*%', 'lab', False, 'Lab average'),
        (r'(?:must|required to)[^.\n]{0,45}?(?:obtain|achieve|earn|score)?\s*(?:at\s+least\s*)?(\d{1,3}(?:\.\d+)?)\s*%[^.\n]{0,100}?\b(?:in|on)\s+(?:the\s+)?(?:non[- ]lab|other|non[- ]laboratory)\s+(?:component|work|portion)?', 'lab', True, 'Non-lab average'),
        (r'(?:and|,)?\s*(?:at\s+least\s*)?(\d{1,3}(?:\.\d+)?)\s*%[^.\n]{0,60}?\b(?:in|on)\s+(?:the\s+)?(?:non[- ]lab|other|non[- ]laboratory)\s+(?:component|work|portion)?', 'lab', True, 'Non-lab average'),
        (r'(?:non[- ]lab|other|non[- ]laboratory)\s+(?:component|work|portion)?[^.\n]{0,80}?(?:must|required to)[^.\n]{0,30}?(\d{1,3}(?:\.\d+)?)\s*%', 'lab', True, 'Non-lab average'),
    ]
    for pat,category,invert,label in group_patterns:
        m=re.search(pat,text,re.I|re.S)
        if not m: continue
        value=float(m.group(1))
        typ='non_category_minimum' if invert else 'category_minimum'
        if not any(r.get('type')==typ and r.get('category')==category for r in structured_rules):
            structured_rules.append({'type':typ,'category':category,'minimum_percent':value,'message':f'{label} must be at least {value:g}% to pass.'})
    # Named aggregate groups, e.g. "at least 50% on the theory component (weekly
    # quizzes, tutorials and final exam)". Extract the group label, its listed
    # members, and the threshold as one functional aggregate_minimum rule instead
    # of letting the per-component regexes above misfire on a member mentioned
    # inside the parenthetical list.
    for m in re.finditer(
        r'(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%[^.\n]{0,40}?\b(?:on|in)\s+(?:the\s+)?([a-z][a-z \-]{2,40}?component)\s*\(([^)]{3,150})\)',
        text, re.I,
    ):
        value=float(m.group(1))
        group_label=m.group(2).strip().title()
        members=[re.sub(r'\s+',' ',x).strip() for x in re.split(r'\s*,\s*|\s+and\s+', m.group(3))]
        members=[x for x in members if x]
        if len(members)<2:
            continue
        if not any(r.get('type')=='aggregate_minimum' and r.get('components')==members for r in structured_rules):
            structured_rules.append({'type':'aggregate_minimum','components':members,'minimum_percent':value,'message':f'{group_label} ({", ".join(members)}) must be at least {value:g}% to pass.'})
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
    """Read a grading screenshot with Gemini Vision.

    This uses the REST API's canonical camelCase fields (inlineData / mimeType).  The
    previous implementation used snake_case protobuf names, which caused PNG/JPG
    uploads to fail on the live Gemini endpoint even though text-only calls worked.
    """
    api_key=(os.environ.get('GEMINI_API_KEY','').strip() or os.environ.get('GOOGLE_API_KEY','').strip())
    if not api_key:
        raise RuntimeError('Gemini is not configured for screenshot reading.')

    models=_gemini_model_candidates(vision=True)

    prompt=(
        "Read this university course-outline screenshot exactly. Transcribe all visible text and preserve the "
        "logical table rows. Pay special attention to assessment names, weights, alternative grading options, "
        "minimum final/test/lab requirements, attendance rules, bonus rules, prerequisite letter-grade rules, "
        "and words such as Assessment Distribution, Grade Distribution, Evaluation, or Marking Scheme. "
        "Do not summarize, omit, or infer missing text. Return plain text only."
    )
    encoded=base64.b64encode(raw).decode('ascii')
    body={
        'contents':[{'role':'user','parts':[{'text':prompt},{'inlineData':{'mimeType':mime_type,'data':encoded}}]}],
        'generationConfig':{'temperature':0.0,'maxOutputTokens':8192},
    }
    last_error=None
    for model in models:
        endpoint=("https://generativelanguage.googleapis.com/v1beta/models/"+
                  urllib.parse.quote(model,safe='')+":generateContent?key="+urllib.parse.quote(api_key,safe=''))
        # Same reasoning as the text pipeline: a 120s timeout x 3 attempts x 5
        # model candidates gave a worst-case of ~30 minutes for reading a
        # single screenshot, before the 3-call facts/compile/audit pipeline
        # even starts. Image OCR genuinely can take a little longer than a
        # pure-text call, so this keeps a more generous timeout than the text
        # pipeline, but still caps the failure-path ceiling hard. A normal
        # successful read still completes in a few seconds either way.
        for attempt in range(2):
            try:
                req=urllib.request.Request(endpoint,data=json.dumps(body).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
                with urllib.request.urlopen(req,timeout=45) as response:
                    result=json.loads(response.read().decode('utf-8'))
                parts=((((result.get('candidates') or [{}])[0].get('content') or {}).get('parts')) or [])
                text_value='\n'.join(x.get('text','') for x in parts if isinstance(x,dict)).strip()
                if len(text_value)<40:
                    raise ValueError('Gemini returned too little readable text.')
                return text_value
            except urllib.error.HTTPError as exc:
                detail=_gemini_http_detail(exc)
                last_error=RuntimeError(f'{model}: HTTP {exc.code}: {detail}')
                if exc.code in (429,500,502,503,504) and attempt<1:
                    time.sleep(1.5*(attempt+1)); continue
                break
            except (urllib.error.URLError,TimeoutError,ValueError) as exc:
                last_error=exc
                if attempt<1:
                    time.sleep(1.5*(attempt+1)); continue
                break
    raise RuntimeError(f'Gemini Vision could not read the screenshot: {last_error or "unknown error"}')

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
@limiter.limit(AI_LIMIT)
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


# Deliberately permissive: it rejects addresses that cannot be real rather than
# trying to decide which exotic-but-valid ones are. The only address that truly
# validates an email is one that receives mail.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

MIN_PASSWORD_LENGTH = 8


def _password_problem(password):
    """Return a message explaining why a password is unusable, or '' if it is fine.

    Signup previously accepted any non-empty string, so "a" was a valid password
    protecting a student's entire academic record. Length is the property that
    actually resists guessing; composition rules mostly push people toward
    predictable substitutions, so this checks length and nothing else.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 200:
        return "That password is too long. Use 200 characters or fewer."
    if not password.strip():
        return "Your password cannot be only spaces."
    return ""


def _hash_reset_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour; 20 per day")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "GET":
        return render_template("forgot_password.html", configured=email_is_configured(),
                               support_email=SUPPORT_EMAIL)

    email = (request.form.get("email") or "").strip().lower()
    # The same answer regardless of whether the account exists: anything else
    # turns this form into a way to discover who has an account here.
    sent_message = ("If an account exists for that address, a reset link is on its way. "
                    "The link expires in one hour.")
    if not email_is_configured():
        return render_template("forgot_password.html", configured=False,
                               support_email=SUPPORT_EMAIL)

    user = User.query.filter_by(email=email).first()
    if user:
        PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).delete()
        token = secrets.token_urlsafe(32)
        db.session.add(PasswordResetToken(
            id=uuid4().hex, user_id=user.id, token_hash=_hash_reset_token(token),
            expires_at=datetime.utcnow() + PASSWORD_RESET_TTL))
        db.session.commit()
        link = url_for("reset_password", token=token, _external=True)
        send_email(
            user.email,
            "Reset your Nexora password",
            f"Hi {user.name},\n\n"
            f"Use this link to choose a new password:\n{link}\n\n"
            "The link works once and expires in one hour.\n\n"
            "If you did not ask for this, you can ignore this email -- your password has not changed.\n",
        )
    return render_template("forgot_password.html", configured=True, sent=sent_message)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour; 40 per day")
def reset_password(token):
    row = PasswordResetToken.query.filter_by(token_hash=_hash_reset_token(token), used_at=None).first()
    if not row or row.expires_at < datetime.utcnow():
        return render_template("reset_password.html", token=token, invalid=True), 400

    if request.method == "GET":
        return render_template("reset_password.html", token=token, invalid=False)

    password = request.form.get("password") or ""
    problem = _password_problem(password)
    if problem:
        return render_template("reset_password.html", token=token, invalid=False, error=problem)

    user = db.session.get(User, row.user_id)
    if not user:
        return render_template("reset_password.html", token=token, invalid=True), 400
    user.password_hash = generate_password_hash(password)
    row.used_at = datetime.utcnow()
    # Any other outstanding link for this account is now stale.
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).delete()
    db.session.commit()
    login_user(user)
    return redirect(url_for("home"))


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("5 per hour; 20 per day")
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not name or not email or not password:
            return render_template("signup.html", error="Please fill in all fields.")

        problem = _password_problem(password)
        if problem:
            return render_template("signup.html", error=problem)

        if not _EMAIL_RE.match(email):
            return render_template("signup.html", error="That email address does not look right. Check it and try again.")

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
@limiter.limit("10 per minute; 60 per hour")
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


@app.route("/api/grading-scale/analyze", methods=["POST"])
@limiter.limit(AI_LIMIT)
@login_required
def analyze_grading_scale_upload():
    uploads=request.files.getlist("files") or request.files.getlist("file")
    uploads=[u for u in uploads if u and u.filename]
    if not uploads:
        return jsonify({"error":"Upload at least one official grading-scale screenshot, PDF, or document."}),400
    if len(uploads)>6:
        return jsonify({"error":"Upload at most 6 grading-scale pages."}),400
    try:
        chunks=[]
        for upload in uploads:
            filename,text_value,_,_=_extract_uploaded_course_file(upload)
            chunks.append(f"===== {filename} =====\n{text_value}")
        combined="\n\n".join(chunks)
        schema={"university_name":"","scale":[{"letter":"A+","minimum":90},{"letter":"C-","minimum":60},{"letter":"F","minimum":0}],"confidence":"high|medium|low","ambiguities":[]}
        prompt=("Extract the official university letter-grade scale. Return only JSON matching this schema:\n"+json.dumps(schema,indent=2)+"\nCapture every letter band and its minimum numeric percentage. Never invent a missing threshold. Put uncertainty in ambiguities.\n\nCONTENT:\n"+combined[:80000])
        try:
            parsed=_gemini_json("You precisely extract official university grading scales. Output only valid JSON.",prompt,2200,0.0)
        except Exception as gemini_exc:
            app.logger.warning('Grading-scale Gemini extraction failed, falling back to regex: %s', gemini_exc)
            parsed={}
        scale=_normalize_grading_scale((parsed or {}).get("scale") or [])
        if len(scale)<2:
            found=[]
            for m in re.finditer(r"(?im)^\s*([A-F][+-]?)\s*(?:[:|\-–—]|\s)\s*(\d{1,3}(?:\.\d+)?)\s*(?:%|[-–—])?",combined):
                found.append({"letter":m.group(1).upper(),"minimum":float(m.group(2))})
            scale=_normalize_grading_scale(found)
        if len(scale)<2:
            return jsonify({"error":"Nexora could not confidently read a complete letter-grade scale. Upload a clearer page or enter the scale manually."}),422
        return jsonify({"ok":True,"university_name":str((parsed or {}).get("university_name") or ""),"scale":scale,"confidence":(parsed or {}).get("confidence") or "medium","ambiguities":(parsed or {}).get("ambiguities") or [],"accuracy_notice":"AI extraction may not be 100% accurate. Confirm every threshold before saving."})
    except Exception as exc:
        app.logger.exception("University grading-scale analysis failed")
        return jsonify({"error":str(exc)}),500


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

    # Group courses by academic term. Brightspace terms may be human-readable
    # ("Winter 2026") or D2L-style codes ("2026.10", "2026.20", "2026.30").
    term_rank={"winter":1,"spring":2,"summer":2,"fall":3,"autumn":3,"other":4}
    def _term_meta(value):
        raw=str(value or "").strip()
        m=re.search(r"(?i)\b(winter|spring|summer|fall|autumn)\s*(20\d{2})\b",raw)
        if m:
            season=m.group(1).title().replace("Autumn","Fall")
            return season,int(m.group(2))
        m=re.search(r"(?i)\b(20\d{2})[._ -]?(10|20|30)\b",raw)
        if m:
            season={"10":"Winter","20":"Summer","30":"Fall"}.get(m.group(2),"Other")
            return season,int(m.group(1))
        return "Other",0
    grouped_courses={}
    for card in course_cards:
        season,year=_term_meta(card.get("term") or card.get("title") or "")
        label=f"{season} {year}" if year else "Other courses"
        group=grouped_courses.setdefault(label,{"label":label,"season":season,"year":year,"courses":[]})
        group["courses"].append(card)
    course_groups=sorted(grouped_courses.values(),key=lambda g:(g["year"],term_rank.get(g["season"].lower(),4)),reverse=True)

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
        course_groups=course_groups,
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


def _lecture_sequence_key(item):
    """Order lectures by their actual curriculum sequence, not sync/publish time.

    Brightspace doesn't give us a reliable numeric position for lecture
    content, only a title and a module label, so the sync timestamp used for
    Announcements (newest first) ends up ordering lectures essentially at
    random relative to the course's real Lecture 1/2/3... progression. Every
    real course numbers its lectures somewhere in the title or module name,
    so pull that number out generically -- this isn't tied to any specific
    course's naming convention, just the near-universal "Lecture N" / "Week
    N" / "Module N" pattern.
    """
    title = str(getattr(item, "title", "") or "")
    module = str(getattr(item, "module_name", "") or "")
    combined = f"{module} {title}"
    numbers = [int(n) for n in re.findall(r"\d+", combined)][:3]
    # Pad to a fixed length so items with fewer numbers still compare
    # correctly against items with more (e.g. "Week 3" vs "Week 3 Lecture 2").
    padded = tuple(numbers + [10**9] * (3 - len(numbers))) if numbers else (10**9, 10**9, 10**9)
    has_number = 0 if numbers else 1
    # Natural-sort the title itself as a tiebreaker/fallback so titles with no
    # extractable number still land in a stable, sensible order rather than
    # colliding at the same sort position.
    natural_title = [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", title)]
    return (has_number, padded, natural_title)


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
    def _content_newest_key(item):
        raw=str(getattr(item,'published_at','') or '')
        try:
            return datetime.fromisoformat(raw.replace('Z','+00:00')).timestamp()
        except Exception:
            dt=getattr(item,'updated_at',None) or getattr(item,'created_at',None) or datetime.min
            try: return dt.timestamp()
            except Exception: return 0
    contents=sorted(contents,key=_content_newest_key,reverse=True)
    synced_assignments = [c for c in contents if c.content_type == "assignment"]
    assignments = task_assignments
    synced_course = SyncedCourse.query.filter_by(user_id=current_user.id, course_code=course).first()
    updates = AcademicUpdate.query.filter_by(user_id=current_user.id, course_full=course).order_by(AcademicUpdate.created_at.desc()).limit(12).all()

    lectures = [c for c in contents if c.content_type == "lecture"]
    lectures = sorted(lectures, key=_lecture_sequence_key)
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
    if outline_analysis and not outline_analysis.get('confidence_score'):
        outline_analysis['confidence_score'] = {'high': 90, 'medium': 75, 'low': 50}.get(str(outline_analysis.get('confidence') or '').lower(), 0)

    next_assignment = assignments[0] if assignments else None
    grading_scale_profile = _get_user_grading_scale(current_user.id)
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
        grade_groups=_group_grade_entries(grade_entries),
        outline_profile=outline_profile,
        study_artifacts=study_artifacts,
        outline_analysis=outline_analysis,
        updates=updates,
        next_assignment=next_assignment,
        grading_scale_configured=grading_scale_profile.get("configured", False),
    )


def _parse_mark_entry(score_value, max_value=100, allow_bare_as_points=False):
    """Accept 15/20, 15 out of 20, 75%, or (for attendance-style counts only) a
    plain number counted against a separate maximum."""
    if score_value is None:
        return None, max(0.0001, float(max_value or 100))
    raw=str(score_value).strip()
    if not raw:
        return None, max(0.0001, float(max_value or 100))
    lowered=raw.lower().strip()
    match=re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*(?:/|out\s+of)\s*(-?\d+(?:\.\d+)?)\s*',lowered,re.I)
    if match:
        earned=float(match.group(1)); maximum=float(match.group(2))
        if maximum<=0 or earned<0:
            raise ValueError('Marks must be zero or greater, and the maximum must be above zero.')
        return earned, maximum
    if '%' in lowered:
        # An explicit percent sign always means the number IS the percentage,
        # regardless of the component's stored maximum -- "75%" must always be
        # 75%, never divided against a max like 50 to produce 150%.
        earned=float(lowered.replace('%','').strip())
        if earned<0:
            raise ValueError('Marks cannot be negative.')
        return earned, 100.0
    earned=float(lowered)
    # A bare number means "this is my percentage" everywhere except attendance
    # counts (e.g. "3" sessions out of 5), where it's genuinely a raw count.
    maximum=max(0.0001,float(max_value or 100)) if allow_bare_as_points else 100.0
    if earned<0:
        raise ValueError('Marks cannot be negative.')
    return earned, maximum


@app.route("/api/course/<course_code>/grades", methods=["GET", "POST", "DELETE"])
@login_required
def course_grades(course_code):
    course = _normalize_course_code(course_code)
    if request.method == "GET":
        rows = GradeEntry.query.filter_by(user_id=current_user.id, course_full=course).order_by(GradeEntry.created_at.asc()).all()
        return jsonify({"grades": [{"id": r.id, "name": r.name, "weight": r.weight, "score": r.score, "max_score": r.max_score, "category": r.category, "notes": r.notes} for r in rows]})
    data = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        row = GradeEntry.query.filter_by(id=str(data.get("id") or ""), user_id=current_user.id, course_full=course).first()
        if row:
            db.session.delete(row); db.session.commit()
        return jsonify({"ok": True})
    name = str(data.get("name") or "Assessment").strip()[:255]
    try:
        weight = max(0.0, min(100.0, float(data.get("weight") or 0)))
        is_attendance = str(data.get("category") or "").strip().lower() == "attendance"
        score, max_score = _parse_mark_entry(data.get("score"), data.get("max_score") or 100, allow_bare_as_points=is_attendance)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "Enter a mark like 15/20 or 75%."}), 400
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


def _component_weight_lookup(analysis):
    weights={}
    for comp in (analysis.get("input_components") or analysis.get("components") or []):
        if not isinstance(comp,dict):
            continue
        key=re.sub(r"[^a-z0-9]+"," ",str(comp.get("name") or comp.get("source") or "").lower()).strip()
        try: weights[key]=float(comp.get("weight") or 0)
        except Exception: weights[key]=0.0
    return weights


def _group_percent(analysis,row_map,components,final_override=None):
    weights=_component_weight_lookup(analysis)
    values=[]
    for name in components or []:
        row=_find_grade_row(row_map,name)
        override=final_override if "final" in str(name).lower() else None
        pct=_row_percent(row,override)
        if pct is None:
            return None
        key=re.sub(r"[^a-z0-9]+"," ",str(name or "").lower()).strip()
        weight=weights.get(key,0.0)
        if not weight:
            for wk,wv in weights.items():
                if key and (key in wk or wk in key):
                    weight=wv; break
        values.append((pct,weight))
    if not values:
        return None
    weighted=[x for x in values if x[1]>0]
    if weighted:
        total=sum(w for _,w in weighted)
        return sum(pct*w for pct,w in weighted)/total if total else None
    return sum(pct for pct,_ in values)/len(values)


def _apply_selection_rules(analysis,row_map):
    virtual={}
    for rule in analysis.get("selection_rules") or []:
        if not isinstance(rule,dict):
            continue
        typ=str(rule.get("type") or "").lower()
        sources=rule.get("components") or rule.get("source_components") or []
        vals=[]
        for source in sources:
            pct=_row_percent(_find_grade_row(row_map,source))
            if pct is not None:
                vals.append((source,pct))
        if not vals or typ not in {"best_n_of_m","select_best","drop_lowest"}:
            continue
        keep=max(1,len(vals)-int(rule.get("drop") or 1)) if typ=="drop_lowest" else max(1,int(rule.get("keep") or rule.get("count") or 1))
        chosen=sorted(vals,key=lambda x:x[1],reverse=True)[:keep]
        name=str(rule.get("output_component") or rule.get("name") or "Selected assessments")
        virtual[re.sub(r"[^a-z0-9]+"," ",name.lower()).strip()]={"percent":sum(v for _,v in chosen)/len(chosen),"chosen":[x[0] for x in chosen]}
    return virtual


def _normalize_advanced_grading_model(parsed):
    if not isinstance(parsed,dict):
        parsed={}
    for key,default in (("selection_rules",[]),("derived_components",[]),("conditional_rules",[]),("scheme_policies",[]),("ambiguities",[])):
        if not isinstance(parsed.get(key),list):
            parsed[key]=default
    # A "selection rule" (best N of M, drop lowest, etc.) only means something
    # when there are at least two real items to select among. A degenerate
    # 1-member rule (e.g. extracted from "Midterms (worth 20%)... if you miss
    # one, the second helps" when the outline table only lists one "Midterm"
    # line) isn't selecting anything -- keeping it around just makes the sync
    # logic synthesize a confusing near-duplicate card (e.g. both "Midterms"
    # and "Midterm" at the same weight) with no real calculation value.
    parsed["selection_rules"]=[r for r in parsed["selection_rules"] if isinstance(r,dict) and len(r.get('components') or [])>=2]
    parsed.setdefault("confidence","low")
    parsed.setdefault("accuracy_notice","AI interpretation may not be 100% accurate. Confirm the detected scheme before relying on it.")
    if not isinstance(parsed.get("pass_rules"),list):
        parsed["pass_rules"]=[]
    def _rule_signature(rule):
        # Only rules with a single named component + numeric minimum (the shape
        # both component_minimums and plain-text extraction can independently
        # produce for the same real-world requirement) get a loose signature.
        # Every other rule type is deduplicated only on an exact full-content
        # match, so genuinely different rules are never collapsed together.
        if isinstance(rule,dict) and rule.get('component') and rule.get('minimum_percent') is not None:
            try:
                return ('component_minimum', str(rule.get('component')).strip().lower(), round(float(rule.get('minimum_percent')),2))
            except (TypeError,ValueError):
                pass
        return json.dumps(rule, sort_keys=True, default=str)
    seen_signatures={_rule_signature(r) for r in parsed["pass_rules"]}
    for item in parsed.get("component_minimums") or []:
        if not isinstance(item,dict):
            continue
        name=item.get("name") or item.get("component")
        minimum=item.get("minimum") if item.get("minimum") is not None else item.get("minimum_percent")
        if name and minimum is not None:
            candidate={"type":"component_minimum","component":name,"minimum_percent":minimum,"message":item.get("consequence") or f"{name} minimum"}
            sig=_rule_signature(candidate)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            parsed["pass_rules"].append(candidate)
    # This function can run more than once on the same already-normalized model
    # further down the pipeline (e.g. once inside the audit stage, once again
    # in the route that calls it). Deduplicate the final list defensively so
    # that scenario never doubles up every requirement shown to the student.
    deduped=[]; final_signatures=set()
    for rule in parsed["pass_rules"]:
        sig=_rule_signature(rule)
        if sig in final_signatures:
            continue
        final_signatures.add(sig)
        deduped.append(rule)
    parsed["pass_rules"]=deduped
    return parsed


def _evaluate_option(option,row_map,final_override=None,analysis=None):
    points=0.0; missing=[]; details=[]
    analysis=analysis or {}
    virtual=_apply_selection_rules(analysis,row_map)
    eligibility=option.get('eligibility') or {}
    # An option is often only open to a student who actually sat some number of
    # a group of assessments. The group used to be hardcoded as 'Test 1'..'Test
    # 3', which matched exactly one course's outline; every other multi-option
    # course found no such rows, counted zero against a threshold of zero, and
    # was silently treated as eligible. from_components names the group so any
    # outline can express the rule. minimum_written_tests is still honoured so
    # analyses saved before this change keep working.
    required=int(eligibility.get('requires_completed') or eligibility.get('minimum_written_tests') or 0)
    sources=[str(x) for x in (eligibility.get('from_components') or []) if str(x).strip()]
    if not sources:
        sources=[f'Test {i}' for i in (1,2,3)]
    completed=sum(1 for source in sources if (lambda r: r and r.score is not None)(_find_grade_row(row_map,source)))
    eligible=completed>=required
    if not eligible:
        missing.append(eligibility.get('message') or f'Requires at least {required} of {len(sources)} listed assessment(s); {completed} entered.')
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
            # Gemini/local parsers may call the component field `source`, `name`,
            # `component`, or `label`. Normalize all shapes here so alternative
            # schemes can always reuse the student's single saved mark.
            source=str(
                comp.get('source')
                or comp.get('name')
                or comp.get('component')
                or comp.get('label')
                or ''
            ).strip()
            virtual_key=re.sub(r'[^a-z0-9]+',' ',source.lower()).strip()
            virtual_match=virtual.get(virtual_key)
            if virtual_match is not None:
                # A selection rule (best_n_of_m / drop_lowest / select_best) produced
                # this component (e.g. "Midterm" from best 1 of Midterm 1/2). Use the
                # computed result instead of falling through to a raw row, otherwise
                # _find_grade_row's substring matching can silently grab the wrong
                # individual assessment (e.g. "Midterm 1" for a "Midterm" lookup).
                pct=virtual_match.get('percent')
            else:
                r=_find_grade_row(row_map,source)
                override=final_override if 'final' in source.lower() else None
                pct=_row_percent(r,override)
            if pct is None:
                missing.append(f'{source or "Assessment"} is not entered.')
        if pct is not None:
            contribution=pct/100*weight; points+=contribution
            details.append({
                'name': source if not comp.get('source_group') else (comp.get('name') or 'Tests'),
                'percent':round(pct,2),'weight':weight,'points':round(contribution,2)
            })
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
        elif typ in {'category_minimum','non_category_minimum'}:
            category=str(rule.get('category') or '').lower()
            values=[]; weighted=0.0; total_weight=0.0
            for row in row_map.values():
                row_category=str(getattr(row,'category','') or '').lower()
                matches=(row_category==category or (category=='lab' and 'lab' in str(getattr(row,'name','')).lower()))
                if typ=='non_category_minimum': matches=not matches
                pct=_row_percent(row)
                if matches and pct is not None:
                    w=max(0.0,float(getattr(row,'weight',0) or 0))
                    if w>0: weighted+=pct*w; total_weight+=w
                    else: values.append(pct)
            actual=(weighted/total_weight) if total_weight>0 else (sum(values)/len(values) if values else None)
            required=float(rule.get('minimum_percent') or 0); ok=actual is not None and actual>=required
        elif typ in {'aggregate_minimum','derived_component_minimum','group_minimum'}:
            actual=_group_percent(analysis,row_map,rule.get('components') or rule.get('includes') or [],final_override)
            required=float(rule.get('minimum_percent') or rule.get('minimum') or 0)
            ok=actual is not None and actual>=required
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

def _fail_letter(user_id):
    """The letter to display when the official status is FAIL, regardless of
    the numeric grade — a mandatory-rule failure (e.g. attendance) must never
    be shown alongside a passing letter like A+."""
    profile=_get_user_grading_scale(user_id)
    scale=profile.get("scale") or []
    return scale[-1]["letter"] if scale else "F"

def _compute_official_result(user_id, course, target=50.0, assumed_other_average=100.0):
    """Single source of truth for a student's official grade, pass/fail
    status, and pass-rule results for a course.

    Both the interactive grade-outcomes endpoint (the "Official Result"
    panel) and the Ask Nexora AI chat call this SAME function -- so the chat
    can never show a number that disagrees with what the calculator itself
    displays. It never recomputes its own version of the math; it's the only
    place the math lives.

    Returns (result_dict, error) where error is None on success, or
    (message, status_code, confirmation_required) on failure.
    """
    rows=GradeEntry.query.filter_by(user_id=user_id,course_full=course).all()
    profile=CourseOutlineProfile.query.filter_by(user_id=user_id,course_full=course).first()
    try: analysis=json.loads(profile.grading_json or '{}') if profile else {}
    except Exception: analysis={}
    if profile and analysis.get('confirmed') is False:
        return None, ('Confirm the detected grading policy before calculating official outcomes.',409,True)
    row_map=_grade_row_map(rows)
    options=analysis.get('grading_options') or []
    if options:
        evaluated=[_evaluate_option(opt,row_map,analysis=analysis) for opt in options]
        by_option={str(x.get('option_name') or '').lower():x for x in evaluated}
        for policy in analysis.get('scheme_policies') or analysis.get('conditional_rules') or []:
            if not isinstance(policy,dict) or str(policy.get('type') or '').lower() not in {'scheme_gate','conditional_scheme','option_eligibility'}:
                continue
            # NOTE: these were previously named `target`/`source`, which shadowed
            # the outer numeric `target` percentage parameter for the rest of the
            # function -- any course using a scheme_policy would silently corrupt
            # the required-final solver below (`ev['grade']>=target`) into a
            # float-vs-string comparison that raises. Renamed to avoid that.
            target_option_key=str(policy.get('target_option') or policy.get('option') or policy.get('then_option') or '').lower()
            source_option_key=str(policy.get('source_option') or policy.get('based_on_option') or policy.get('if_option') or '').lower()
            target_row=next((x for k,x in by_option.items() if target_option_key and (target_option_key in k or k in target_option_key)),None)
            source_row=next((x for k,x in by_option.items() if source_option_key and (source_option_key in k or k in source_option_key)),None)
            if not target_row or not source_row:
                continue
            threshold=policy.get('minimum_percent')
            if threshold is None and policy.get('minimum_letter'):
                scale=_get_user_grading_scale(user_id)
                threshold=next((float(x['minimum']) for x in scale['scale'] if x['letter'].upper()==str(policy.get('minimum_letter')).upper()),None)
            gate_ok=source_row.get('complete') and source_row.get('eligible') and threshold is not None and float(source_row.get('grade') or 0)>=float(threshold)
            if not gate_ok:
                target_row['eligible']=False
                target_row.setdefault('missing',[]).append(policy.get('message') or f"Requires {source_row.get('option_name')} to meet the eligibility threshold.")
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
                ev=_evaluate_option(opt,row_map,final_override=fs,analysis=analysis)
                if not ev['eligible']: continue
                # Missing final is satisfied by override; other missing inputs still block calculation.
                nonfinal_missing=[m for m in ev['missing'] if 'Final Exam' not in m and 'final exam' not in m.lower()]
                if nonfinal_missing: continue
                pass_rules,_=_evaluate_pass_rules(analysis,row_map,ev['grade'],final_override=fs)
                if ev['grade']>=target and pass_rules:
                    found=fs; break
            required.append({'option_name':opt.get('option_name'),'required_final':found,'eligible_now':_evaluate_option(opt,row_map,final_override=100,analysis=analysis)['eligible']})
        possible=[x for x in required if x['required_final'] is not None]
        best_required=min(possible,key=lambda x:x['required_final']) if possible else None
        decorated=_decorate_grade_result(user_id,analysis,official_grade)
        if official_grade is not None and not decorated['course_letter_rules_passed']:
            official_status='FAIL'
        if official_status=='FAIL':
            decorated['letter_grade']=_fail_letter(user_id)
        return {
            'mode':'multiple_options','target':target,'options':evaluated,'best_option':best,
            'official_grade':official_grade,'official_status':official_status,'pass_rules':rule_results,
            'required_final_by_option':required,'best_required_final':best_required,
            'pass_mark':float(analysis.get('pass_mark') or (profile.pass_mark if profile else 50)),
            'final_minimum':analysis.get('final_minimum', profile.final_minimum if profile else None),
            **decorated,
        }, None
    # Single fixed scheme.
    scheme_total=sum(max(0.0,float(r.weight or 0)) for r in rows) or float(analysis.get('total_weight') or 100)
    earned=0.0; completed_weight=0.0; final_row=None
    # Separate the final from all other work. Current standing uses every saved
    # mark, but the What-do-I-need solver intentionally ignores a saved final
    # so students can run a hypothetical target at any time.
    nonfinal_earned=0.0; nonfinal_completed_weight=0.0
    for r in rows:
        is_final=bool(re.search(r'\bfinal(?:\s+(?:exam|examination))?\b',r.name or '',re.I))
        if is_final: final_row=r
        if r.score is not None and r.weight>0:
            contribution=(_row_percent(r)/100)*float(r.weight)
            earned+=contribution; completed_weight+=float(r.weight)
            if not is_final:
                nonfinal_earned+=contribution; nonfinal_completed_weight+=float(r.weight)
    remaining=max(0.0,scheme_total-completed_weight); normalized_target=target/100*scheme_total
    needed_average=None if remaining<=0 else (normalized_target-earned)/remaining*100
    current_on_completed=earned/completed_weight*100 if completed_weight else 0
    official_grade=earned/scheme_total*100 if remaining<=0 and scheme_total else None
    pass_ok,rule_results=_evaluate_pass_rules(analysis,row_map,official_grade or earned/scheme_total*100 if scheme_total else 0)
    required_final=None
    if final_row and final_row.weight>0:
        other_unfinished=max(0.0, scheme_total - nonfinal_completed_weight - float(final_row.weight))
        assumed=max(0.0,min(100.0,float(assumed_other_average or 100)))
        required_final=(normalized_target-nonfinal_earned-(assumed/100)*other_unfinished)/float(final_row.weight)*100
        final_min=analysis.get('final_minimum',profile.final_minimum if profile else None)
        if final_min is not None: required_final=max(required_final,float(final_min))
    decorated=_decorate_grade_result(user_id,analysis,official_grade)
    official_status='INCOMPLETE' if official_grade is None else ('PASS' if pass_ok and decorated['course_letter_rules_passed'] else 'FAIL')
    if official_status=='FAIL':
        decorated['letter_grade']=_fail_letter(user_id)
    return {'mode':'single_scheme','target':target,'official_grade':official_grade,'official_status':official_status,'pass_rules':rule_results,'earned_course_points':round(earned,2),'completed_weight':round(completed_weight,2),'remaining_weight':round(remaining,2),'current_on_completed':round(current_on_completed,2),'needed_average':None if needed_average is None else round(needed_average,2),'required_final':None if required_final is None else round(required_final,2),'best_possible':round((earned+remaining)/scheme_total*100,2) if scheme_total else 0,'worst_possible':round(earned/scheme_total*100,2) if scheme_total else 0,**decorated}, None


@app.route("/api/course/<course_code>/grade-outcomes", methods=["POST"])
@login_required
def grade_outcomes(course_code):
    course=_normalize_course_code(course_code)
    data=request.get_json(silent=True) or {}
    try: target=max(0.0,min(100.0,float(data.get('target',50))))
    except (TypeError,ValueError): return jsonify({'error':'Enter a valid target percentage.'}),400
    try: assumed=max(0.0,min(100.0,float(data.get('assumed_other_average',100))))
    except (TypeError,ValueError): assumed=100.0
    result,error=_compute_official_result(current_user.id,course,target=target,assumed_other_average=assumed)
    if error:
        message,status,confirmation_required=error
        payload={'error':message}
        if confirmation_required: payload['confirmation_required']=True
        return jsonify(payload),status
    return jsonify(result)


def _verify_quote_in_source(quote, source_text):
    """True only if `quote` actually appears verbatim (modulo whitespace) in
    `source_text`. A citation the AI could not have really pulled from the
    outline is worse than no citation -- it looks trustworthy while being
    fabricated. Every citation returned to the student passes this check."""
    def norm(s):
        return re.sub(r'\s+',' ',str(s or '')).strip().lower()
    q=norm(quote)
    if len(q)<6:
        return False
    return q in norm(source_text)


@app.route('/api/course/<course_code>/ask', methods=['POST'])
@login_required
def ask_course_ai(course_code):
    """Ask Nexora: a grounded, last-resort backup for when a student has a
    question the outline-review UI doesn't answer, or a grading-policy edge
    case they want double-checked in plain language.

    This is deliberately NOT a general chatbot. It only answers from two
    sources: the exact text of the uploaded outline, and the same official
    grade/pass-rule numbers the calculator itself already computed via
    _compute_official_result -- it is never allowed to invent a rule that
    isn't in the outline or do its own grade math. Every text claim must
    come with a verbatim quote that gets checked against the real outline
    text server-side before it's shown; a claim that can't be verified is
    marked unverified rather than presented as fact.
    """
    course=_normalize_course_code(course_code)
    data=request.get_json(silent=True) or {}
    question=str(data.get('question') or '').strip()
    if not question:
        return jsonify({'error':'Ask a question first.'}),400
    if len(question)>2000:
        return jsonify({'error':'Keep your question under 2000 characters.'}),400

    row=CourseContent.query.filter_by(user_id=current_user.id,course_full=course,content_type='outline').order_by(CourseContent.updated_at.desc()).first()
    outline_text=_sanitize_extracted_text(row.content_text) if row and row.content_text else ''

    profile=CourseOutlineProfile.query.filter_by(user_id=current_user.id,course_full=course).first()
    try: analysis=json.loads(profile.grading_json or '{}') if profile else {}
    except Exception: analysis={}

    if not outline_text and not analysis:
        return jsonify({'error':"Upload and analyze this course's outline first — Nexora has nothing to answer from yet."}),409

    official_result,official_error=_compute_official_result(current_user.id,course)
    official_summary=official_result if not official_error else None

    excerpt=_outline_relevant_excerpt(outline_text) if outline_text else ''
    # Also give the model the confirmed structured policy (components, pass
    # rules, grading options) -- the outline text can be long and noisy, and
    # this is the exact same data the confirmation UI already showed the
    # student, so citing it is at least as trustworthy as citing raw text.
    policy_summary={
        'components':analysis.get('components') or [],
        'pass_rules':analysis.get('pass_rules') or [],
        'grading_options':analysis.get('grading_options') or [],
        'pass_mark':analysis.get('pass_mark'),
        'confirmed':bool(analysis.get('confirmed')),
    }

    answer_schema={
        "answer":"",
        "citations":[{"quote":""}],
        "confidence":"high|medium|low",
        "recommend_professor":False,
        "not_covered":False,
    }
    system_prompt=(
        "You are Ask Nexora, a grounded backup assistant for a university course-outline "
        "and grade calculator app. You are the LAST RESORT a student reaches for when the "
        "app's own structured outline review didn't cover their question -- getting this "
        "wrong can make a student think they're passing when they aren't, so you must be "
        "conservative, never guess, and never state anything as fact unless it is directly "
        "supported by what you were given. You have exactly two sources of truth: (1) the "
        "raw outline text excerpt, and (2) the pre-computed grade/pass-rule numbers, which "
        "were already calculated by the app's own grading engine -- you must reuse those "
        "numbers exactly and NEVER perform your own grade math or invent a percentage. If "
        "the student asks something the outline doesn't address, or that depends on "
        "information you were not given, set not_covered=true and tell them plainly to "
        "confirm with their professor or TA rather than guessing. Every factual claim about "
        "a policy or rule must be backed by an exact, verbatim quote from the outline text "
        "in `citations` -- do not paraphrase into a citation, quote the literal text. If a "
        "claim is grounded in the pre-computed numbers instead of outline text, don't "
        "fabricate a text quote for it -- leave citations for that claim empty and say so in "
        "the answer. Set confidence to 'low' whenever you are the least bit unsure, and set "
        "recommend_professor=true for anything grade-critical or ambiguous, even if you "
        "answered it. Return strict JSON matching this schema:\n"+json.dumps(answer_schema,indent=2)
    )
    user_prompt=(
        f"Student's question about {course}: {question}\n\n"
        "=== PRE-COMPUTED OFFICIAL GRADE RESULT (already calculated by the app's grading "
        "engine -- reuse these numbers exactly, never recompute) ===\n"
        +(json.dumps(official_summary,ensure_ascii=False,indent=2) if official_summary else "Not available yet (outline not confirmed, or no grades entered).")
        +"\n\n=== CONFIRMED STRUCTURED GRADING POLICY ===\n"
        +json.dumps(policy_summary,ensure_ascii=False,indent=2)
        +"\n\n=== RAW OUTLINE TEXT EXCERPT (only source for anything not covered above; quote "
        "it verbatim for citations) ===\n"
        +(excerpt or "No outline text available.")
    )
    try:
        raw=_gemini_json(system_prompt,user_prompt,3000,0.1)
    except Exception as exc:
        app.logger.exception('Ask Nexora failed for %s: %s',course,exc)
        return jsonify({'error':"Nexora couldn't reach the AI right now. Try again in a moment, or double check with your professor in the meantime."}),502

    if not isinstance(raw,dict):
        return jsonify({'error':'Nexora could not produce an answer. Try rephrasing your question.'}),502
    answer=str(raw.get('answer') or '').strip()[:4000]
    if not answer:
        return jsonify({'error':'Nexora could not produce an answer. Try rephrasing your question.'}),502
    raw_citations=[str(c.get('quote') or '').strip() for c in (raw.get('citations') or []) if isinstance(c,dict) and c.get('quote')]
    verified=[q[:500] for q in raw_citations if _verify_quote_in_source(q,outline_text)]
    unverified_count=len(raw_citations)-len(verified)
    confidence=str(raw.get('confidence') or 'low').strip().lower()
    if confidence not in {'high','medium','low'}: confidence='low'
    # A citation the model claimed but that doesn't actually appear in the
    # outline is a signal the answer may be partly fabricated -- never let
    # that pass as high/medium confidence even if the model said otherwise.
    if unverified_count>0 and confidence!='low':
        confidence='low'
    return jsonify({
        'answer':answer,
        'citations':verified,
        'unverified_citation_count':unverified_count,
        'confidence':confidence,
        'recommend_professor':bool(raw.get('recommend_professor')) or confidence=='low',
        'not_covered':bool(raw.get('not_covered')),
    })


@app.route('/api/course/<course_code>/outline/upload', methods=['POST'])
@limiter.limit(AI_LIMIT)
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
        app.logger.warning('Course outline upload extraction failed: %s', exc)
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
        app.logger.warning('Course material upload extraction failed: %s', exc)
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



def _outline_relevant_excerpt(text_value, max_chars=90000):
    """Keep the grading/evaluation passages and their nearby context.

    Full outlines are noisy. This extractor preserves every percentage-bearing
    paragraph plus surrounding headings, pass/fail language, and alternative
    scheme wording so Gemini sees the evidence most likely to affect grades.
    """
    text=_sanitize_extracted_text(text_value)
    if len(text)<=max_chars:
        return text
    lines=text.splitlines()
    keys=re.compile(r"(?:assessment|evaluation|grading|grade distribution|marking|weight|final exam|midterm|test|quiz|lab|assignment|tutorial|attendance|pass|fail|prerequisite|scheme|option|best|drop|replace|reweight|bonus|minimum|at least|must obtain|must achieve)",re.I)
    selected=set()
    for i,line in enumerate(lines):
        if keys.search(line) or re.search(r"\b\d+(?:\.\d+)?\s*%",line):
            for j in range(max(0,i-3),min(len(lines),i+5)):
                selected.add(j)
    excerpt='\n'.join(lines[i] for i in sorted(selected))
    return excerpt[:max_chars] if excerpt.strip() else text[:max_chars]


def _outline_model_validation(model):
    """Return deterministic issues for a compiled grading policy."""
    issues=[]
    if not isinstance(model,dict):
        return ['The model is not an object.']
    components=model.get('components') or []
    options=model.get('grading_options') or []
    def _bonus_weight(comps):
        return sum(float(c.get('weight') or 0) for c in comps if isinstance(c,dict) and
                   re.search(r'bonus|extra\s*credit', f"{c.get('name') or ''} {c.get('category') or ''}", re.I))
    def _check_total(label, comps):
        if not comps:
            issues.append(f'{label} has no components.')
            return
        total=sum(float(c.get('weight') or 0) for c in comps if isinstance(c,dict))
        if total < 99.0:
            issues.append(f'{label} totals {total:.2f}%, not 100% — some weight appears to be missing.')
            return
        if total > 101.0:
            # A scheme can legitimately total more than 100% when it includes
            # bonus/extra-credit weight on top of the base 100% (e.g. an outline
            # that lists "Bonus: Class Participation ... 4%" alongside components
            # that already sum to 100%). Only flag the excess if bonus weight
            # doesn't account for it.
            base_total = total - _bonus_weight(comps)
            if not (99.0 <= base_total <= 101.0):
                issues.append(f'{label} totals {total:.2f}%, not 100% (base weight excluding any bonus components is {base_total:.2f}%).')
    if options:
        names=[]
        for idx,opt in enumerate(options,1):
            oname=str(opt.get('option_name') or f'Option {idx}')
            names.append(oname)
            _check_total(oname, opt.get('components') or [])
        for policy in model.get('scheme_policies') or []:
            if not isinstance(policy,dict): continue
            for key in ('source_option','target_option'):
                ref=str(policy.get(key) or '')
                if ref and not any(ref.lower()==n.lower() for n in names):
                    issues.append(f'Scheme policy references unknown option: {ref}.')
    elif components:
        _check_total('Component weights', components)
    else:
        issues.append('No grading components were detected.')
    known=[]
    for c in components:
        if isinstance(c,dict): known.append(str(c.get('name') or '').lower())
    for opt in options:
        for c in opt.get('components') or []:
            if isinstance(c,dict): known.append(str(c.get('name') or c.get('source') or '').lower())
    for rule in model.get('pass_rules') or []:
        if not isinstance(rule,dict): continue
        refs=[]
        if rule.get('component'): refs.append(rule.get('component'))
        refs.extend(rule.get('components') or [])
        for ref in refs:
            refn=str(ref).lower()
            # A pass rule can legitimately reference a component using a
            # generic/singular form (e.g. "lab component", "assignment
            # component") when the actual schemes use the plural or a
            # differently-worded name (e.g. "Labs", "Assignments"). Strip
            # generic words before matching so these don't get falsely
            # flagged as referencing an unknown component.
            refn_stripped=re.sub(r'\b(component|components|category)\b','',refn).strip()
            refn_singular=re.sub(r's\b','',refn_stripped) if refn_stripped else refn_stripped
            candidates={c for c in (refn, refn_stripped, refn_singular) if c}
            if refn and not any(any(c in x or x in c for c in candidates) for x in known if x):
                issues.append(f'Pass rule references a component not found in the schemes: {ref}.')
    return list(dict.fromkeys(issues))


def _high_accuracy_outline_analysis(course, outline_text, scale_profile, schema):
    """Three-stage outline compiler: facts -> executable policy -> adversarial audit.

    This deliberately costs more than a single Gemini call, but greatly reduces
    silent omissions and invented rules. It still cannot guarantee perfect
    interpretation, so unresolved disagreements are surfaced for confirmation.
    """
    excerpt = _outline_relevant_excerpt(outline_text)
    scale = json.dumps(scale_profile.get("scale") or [], ensure_ascii=False)
    fact_schema = {
        "evidence": [{"fact": "", "quote": "", "location": ""}],
        "components": [{"name": "", "weight": 0, "evidence_quote": ""}],
        "alternative_schemes": [],
        "minimum_requirements": [],
        "selection_or_reweight_rules": [],
        "letter_grade_rules": [],
        "missing_or_ambiguous_information": [],
    }
    facts_prompt = (
        f"Extract grading facts from the complete evidence for {course}. "
        "Do not calculate or infer unstated rules. Every extracted fact must "
        "include a short exact evidence quote. Return strict JSON matching:\n"
        + json.dumps(fact_schema, ensure_ascii=False, indent=2)
        + "\nUniversity grading scale (confirmed by student):\n"
        + scale
        + "\nCOURSE OUTLINE EVIDENCE:\n"
        + excerpt
    )
    models_used = []
    facts = _gemini_json(
        "You are a forensic academic-policy fact extractor. Never invent missing information.",
        facts_prompt,
        6000,
        0.0,
        reasoning=True,
        used_model=models_used,
    )
    compile_prompt = (
        "Compile the extracted facts into an executable grading policy. Use only facts "
        "supported by the evidence. Represent alternative schemes separately; best/drop/"
        "replacement rules as selection_rules; aggregate theory/lab rules as derived_components; "
        "scheme eligibility as scheme_policies; prerequisite letter rules separately from "
        "course-pass rules. Return strict JSON matching this schema:\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        # These rules were built by the route and thrown away, so the compiler
        # worked from the schema alone and had never been told to drop bonus
        # marks or to keep a prerequisite letter rule out of the pass rules.
        + "\n\n" + _outline_extraction_rules()
        + "\nCONFIRMED UNIVERSITY SCALE:\n"
        + scale
        + "\nEXTRACTED FACTS:\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\nSOURCE EVIDENCE:\n"
        + excerpt
    )
    compiled = _gemini_json(
        "You are a grading-policy compiler. Produce executable JSON only.",
        compile_prompt,
        7000,
        0.0,
        reasoning=True,
        used_model=models_used,
    )
    audit_schema = {
        "approved": False,
        "missing_facts": [],
        "unsupported_claims": [],
        "incorrect_weights": [],
        "missed_conditions": [],
        "ambiguities": [],
        "corrected_model": schema,
        "confidence_score": 0,
    }
    audit_prompt = (
        "Audit this grading model adversarially against the source. Check every weight, "
        "pass rule, alternative scheme, conditional eligibility, best/drop/reweight policy, "
        "aggregate requirement, letter threshold, and prerequisite distinction. Correct the "
        "model only when the source clearly supports a correction. confidence_score MUST be "
        "an integer from 0 to 100 (a percentage, e.g. 96 for 96% confident) — never a 0-1 "
        "fraction. Return strict JSON matching:\n"
        + json.dumps(audit_schema, ensure_ascii=False, indent=2)
        # Audit against the same rules the compiler was given, so a bonus that
        # slipped through, or a prerequisite rule recorded as a course fail, is
        # caught here rather than shown to the student.
        + "\n\nTHE RULES THE MODEL WAS COMPILED AGAINST:\n" + _outline_extraction_rules()
        + "\nSOURCE:\n"
        + excerpt
        + "\nFACTS:\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\nCOMPILED MODEL:\n"
        + json.dumps(compiled, ensure_ascii=False)
    )
    audit = _gemini_json(
        "You are an adversarial verifier of university grading policies. Be conservative.",
        audit_prompt,
        7000,
        0.0,
        reasoning=True,
        used_model=models_used,
    )
    final = (
        audit.get("corrected_model")
        if isinstance(audit, dict) and isinstance(audit.get("corrected_model"), dict)
        else compiled
    )
    final = _normalize_advanced_grading_model(final)
    deterministic = _outline_model_validation(final)
    # Deterministic validation already knows when the model is wrong (weights
    # that do not total 100%, scheme policies pointing at options that do not
    # exist). Reporting that to the student and stopping there wastes the most
    # reliable accuracy signal in the pipeline. Feed the concrete failures back
    # and let the verifier repair them -- keeping the result only when it is
    # actually better, so a repair attempt can never make the model worse.
    if deterministic:
        try:
            repair_prompt = (
                "Your compiled grading model failed deterministic validation. Fix ONLY these "
                "specific problems using the source evidence. Do not restructure anything that "
                "already validates, and do not invent facts the source does not support.\n"
                "VALIDATION FAILURES:\n"
                + json.dumps(deterministic, ensure_ascii=False, indent=2)
                + "\nCURRENT MODEL:\n"
                + json.dumps(final, ensure_ascii=False)
                + "\nSOURCE:\n"
                + excerpt
                + "\nReturn strict JSON matching:\n"
                + json.dumps(schema, ensure_ascii=False, indent=2)
            )
            repaired = _gemini_json(
                "You repair grading policies to satisfy deterministic validation without inventing facts.",
                repair_prompt,
                7000,
                0.0,
                reasoning=True,
                used_model=models_used,
            )
            if isinstance(repaired, dict):
                candidate = _normalize_advanced_grading_model(repaired)
                candidate_issues = _outline_model_validation(candidate)
                if len(candidate_issues) < len(deterministic):
                    final, deterministic = candidate, candidate_issues
        except Exception:
            # A failed repair leaves the original result untouched; whatever is
            # still unresolved surfaces as an ambiguity for the student below.
            pass
    def _clean_ambiguity_item(v):
        """The AI can report an ambiguity as a rich dict ({'topic':..,
        'description':..}) or a plain string. Keep dicts as dicts so the
        template can render them properly -- str()-ing a dict here would
        permanently bake Python's raw repr ("{'topic': 'X', ...}") into
        storage, which is unreadable and can never be cleaned up downstream.
        """
        if isinstance(v, dict):
            desc = v.get('description') or v.get('message') or v.get('detail')
            topic = v.get('topic') or v.get('title') or ''
            if desc:
                return {'topic': str(topic), 'description': str(desc)}
            parts = [str(x) for x in v.values() if x]
            joined = ' — '.join(parts)
            return joined.strip() or None
        text = str(v).strip()
        return text or None
    ambiguities = []
    for key in (
        "missing_facts",
        "unsupported_claims",
        "incorrect_weights",
        "missed_conditions",
        "ambiguities",
    ):
        vals = audit.get(key) if isinstance(audit, dict) else []
        if isinstance(vals, list):
            for v in vals:
                cleaned = _clean_ambiguity_item(v)
                if cleaned:
                    ambiguities.append(cleaned)
    ambiguities.extend(deterministic)
    # The model chain falls back to flash tiers when a Pro model is not
    # available on this key. That silently lowers reasoning quality on the one
    # task where it matters most, so say so rather than hiding it.
    if models_used and not any("pro" in str(m).lower() for m in models_used):
        ambiguities.append(
            "Analyzed with " + str(models_used[0]) + " because no Pro-tier Gemini model was "
            "available for this API key. Accuracy on complex grading schemes is lower than "
            "usual -- set GEMINI_REASONING_MODEL to a Pro model to restore it."
        )
    for v in (facts.get("missing_or_ambiguous_information") or []):
        cleaned = _clean_ambiguity_item(v)
        if cleaned:
            ambiguities.append(cleaned)
    # Dedupe even though some items are unhashable dicts.
    _seen_ambiguity_sigs = set()
    _deduped_ambiguities = []
    for item in ambiguities:
        sig = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
        if sig in _seen_ambiguity_sigs:
            continue
        _seen_ambiguity_sigs.add(sig)
        _deduped_ambiguities.append(item)
    final["ambiguities"] = _deduped_ambiguities
    try:
        score = float(audit.get("confidence_score") or 0)
    except Exception:
        score = 0
    # The audit prompt's schema example just shows `"confidence_score": 0`
    # with no unit specified, so the model sometimes returns a 0-1 fraction
    # (e.g. 0.96) instead of a 0-100 percentage. A fraction silently survives
    # the old max(0, min(100, score)) clamp unchanged, then gets displayed as
    # "1.0%" instead of "96%". Detect the fraction case and rescale it.
    if 0 < score <= 1:
        score = score * 100
    final["confidence_score"] = max(0, min(100, score))
    final["confidence"] = (
        "high" if score >= 90 and not deterministic else "medium" if score >= 70 else "low"
    )
    final["verification"] = {
        "passes": 3,
        "auditor_approved": bool(audit.get("approved")) if isinstance(audit, dict) else False,
        "deterministic_issues": deterministic,
        "evidence_count": len(facts.get("evidence") or []) if isinstance(facts, dict) else 0,
    }
    final["accuracy_notice"] = (
        "AI interpretation cannot be guaranteed 100% accurate. Nexora used independent "
        "fact extraction, policy compilation, adversarial verification, and deterministic "
        "checks. Review and confirm every rule before relying on it."
    )
    return final

def _outline_extraction_rules():
    """The extraction rules, shared by the single-call prompt and the pipeline.

    These were written into the analyze route and then never sent: the route
    built its prompt and discarded it, so only the bare schema reached
    _high_accuracy_outline_analysis. Every instruction here -- exclude bonus
    marks, keep alternative schemes separate, distinguish a prerequisite
    letter rule from a course-fail one, never invent missing values -- was
    dead text on the path that actually runs for a course workspace. The
    visible symptom was a bonus component surviving into a scheme, so its
    weights totalled 104%.
    """
    return """IMPORTANT — read the whole outline before extracting anything:
1. Some outlines define ONE fixed grading scheme (a single components list). If so, leave grading_options empty and put everything in components; weights should total 100.
2. Some outlines define MULTIPLE ALTERNATIVE grading schemes (e.g. "Option 1: Final exam 100%", "Option 2: best test 15% + tutorials 5% + final 80%") where the student's final grade is whichever option gives them the best result. If you see this pattern, put EACH option as its own entry in grading_options (each with its own components list summing to 100), and set the top-level `components` to the most detailed/informative option (usually the one with the most components) as the default shown to the student. Add a rule noting that alternate options exist and the best one is applied automatically.
3. Bonus and extra-credit marks are ADDITIVE. They sit on top of a scheme whose other components already total 100%, so a student who earns them finishes above 100% -- that is correct and expected, not an error. Record a bonus that appears in the grading table with its stated weight as an ordinary component, and make it identifiable by keeping the word "bonus" in its name or setting its category to "bonus", so the rest of the app can separate it from the base scheme. Never fold a bonus into the base 100%, and never drop one: dropping it silently under-reports the grade of every student who earned it. Record a bonus wherever it is described -- a row in the grading table or a sentence of prose -- whenever the outline states how much it is worth. Only when no amount is given ("bonus marks may be awarded at the instructor's discretion") is there nothing to record as a component; note that in rules instead.
4. Only extract an item as a component if it is a genuine named grading category from a grading breakdown/table (e.g. "Midterm", "Labs", "Assignments", "Final Exam", "Tutorial attendance", "Term Tests") — never a sentence fragment.
5. Detect every special pass/fail rule (minimum score on the final exam, labs, midterms, tutorial attendance requirements, overall minimum grade) and put numeric thresholds in component_minimums. Never invent missing values.
6. Grading tables may be headed Assessment Breakdown, Assessment Distribution, Grade Distribution, Evaluation, Marking Scheme, Basis of Grade, or similar. Treat all of these as equivalent.
7. Ignore explanatory dates/descriptions between an assessment name and its weight. Example: 'Midterm Exam I Closed book (Weekend of Jan 31) 20%' means Midterm 1 = 20%.
8. If there are several alternative options, never add their weights together. Return each option separately.
9. Detect letter-grade rules such as "C- or better". Put them in letter_requirements. Distinguish scope: use "course_pass" only if the outline says the student fails the course below that letter; use "prerequisite" when it only controls prerequisite eligibility. Never turn a prerequisite requirement into a course FAIL rule.
10. Detect best/drop/replacement policies such as best 1 of 2 midterms, best 2 of 3 assignments, drop the lowest quiz, or final replaces a missed test. Put executable rules in selection_rules.
11. Detect aggregate requirements such as "theory component (quiz + tutorial + final) must be at least 50%". Put them in derived_components and in pass_rules using type aggregate_minimum with the included component names.
12. Detect conditional scheme logic. Example: Scheme 2 is used only if Scheme 1 is C- or higher. Put this in scheme_policies; do not treat an ineligible scheme as the winning result.
13. Interpret every letter threshold using the student's saved university grading scale, never a hardcoded university scale.
14. If wording is ambiguous or pages appear missing, list the uncertainty in ambiguities and lower confidence. Never invent missing rules. AI interpretation is not guaranteed to be 100% accurate and the student must confirm the result.
15. Some outlines move weight between components according to how the student performs, e.g. "if your final exam percentage is higher than your midterm, the midterm weight transfers to the final". Arithmetically that is the same as two alternative schemes with the better result applied, and it must always be expressed that way: put the stated weighting and the transferred weighting in grading_options, keep the top-level components as the stated weighting, and record the triggering condition in rules. Do not invent a separate rule type for it, and never merge the transferred weight into components as though it always applies.
16. An alternative scheme is sometimes open only to students who actually sat some number of a group of assessments, e.g. "options 3 and 4 require that you have written at least two and three of the written tests respectively". Record that on the option itself as eligibility, naming the group in from_components using the same component names you used elsewhere, the count in requires_completed, and a plain-language message. Leave eligibility off an option that every student can use. This is about whether an option is available at all, which is not the same as a minimum mark inside it -- a mark floor belongs in component_minimums."""


def _outline_schema():
    """The extraction schema, shared by the prompt builder and the audit pipeline.

    It lived inside _outline_analysis_prompt for one commit, which put it out
    of scope for the caller that passes it to _high_accuracy_outline_analysis.
    That raised NameError on every analysis, and the broad except around the
    pipeline reported it as "the AI pipeline was unavailable" and fell back to
    the deterministic parser, so a crash looked like a service outage.
    """
    return {
        "course_title": "",
        "pass_mark": 50,
        "final_minimum": None,
        "components": [
            {"name": "Assignments", "weight": 20, "category": "assignments", "notes": ""}
        ],
        "component_minimums": [
            {"name": "Final Exam", "minimum": 50, "consequence": "Must pass final to pass course"}
        ],
        "rules": [""],
        "grading_options": [
            {
                "option_name": "Option 1",
                "components": [
                    {"name": "Final Exam", "weight": 100, "category": "final", "notes": ""}
                ],
                "eligibility": {
                    "requires_completed": 2,
                    "from_components": ["Test 1", "Test 2", "Test 3"],
                    "message": "Requires at least two written tests to have been sat.",
                },
            }
        ],
        "important_dates": [],
        "letter_requirements": [
            {
                "minimum_letter": "C-",
                "scope": "prerequisite",
                "message": "C- or better required to use this course as a prerequisite",
            }
        ],
        "selection_rules": [
            {"type":"best_n_of_m","components":["Midterm 1","Midterm 2"],"keep":1,"output_component":"Midterm"}
        ],
        "derived_components": [
            {"name":"Theory component","components":["Quizzes","Tutorials","Final Exam"],"minimum_percent":50}
        ],
        "scheme_policies": [
            {"type":"scheme_gate","source_option":"Scheme 1","target_option":"Scheme 2","minimum_letter":"C-","message":"Scheme 2 is considered only if Scheme 1 is C- or higher."}
        ],
        "ambiguities": [],
        "confidence": "high|medium|low",
    }


def _outline_analysis_prompt(text_value, course=''):
    """Build the outline-extraction prompt shared by every analysis entry point.

    Both the course-workspace analyzer and the standalone grade-calculator
    analyzer used to carry their own copy of this schema. They drifted: the
    standalone one shrank to four lines and a components-only schema, so the
    same outline produced markedly worse results depending on which page the
    student happened to use. One definition keeps every improvement here from
    having to be made twice.
    """
    outline_schema = _outline_schema()
    prompt = (
        f"Analyze this university course outline for {course}. Return strict JSON matching this schema:\n"
        + json.dumps(outline_schema, ensure_ascii=False, indent=2)
        + """

""" + _outline_extraction_rules() + """

OUTLINE:
"""
        + _sanitize_extracted_text(text_value)[:120000]
    )
    return prompt


@app.route("/api/course/<course_code>/outline/analyze", methods=["POST"])
@limiter.limit(AI_LIMIT)
@login_required
def analyze_course_outline(course_code):
    course = _normalize_course_code(course_code)
    scale_profile = _get_user_grading_scale(current_user.id)
    if not scale_profile.get("configured"):
        return jsonify({"error":"Set up and confirm your university grading scale in Settings before analyzing a course outline. Nexora needs it to interpret rules such as C- correctly.","setup_required":"grading_scale"}),428
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
    # No prompt is built here on purpose. This route hands the outline to
    # _high_accuracy_outline_analysis, which assembles its own three stages and
    # gets the shared rules through _outline_extraction_rules(). A prompt was
    # built and discarded on this line for as long as the pipeline has existed,
    # which is how sixteen extraction rules came to be written and never sent.
    used_fallback=False
    # The deterministic parser is free (no network round-trip) and, by the same
    # override rule applied below, its result wins outright whenever it already
    # looks like a complete grading scheme (high confidence, or it found explicit
    # alternative grading options). Previously that was only discovered *after*
    # paying for the full 3-call Gemini pipeline (facts -> compile -> audit,
    # roughly 45-60s), whose output was then thrown away. Checking it first
    # skips that wasted latency for any outline the deterministic parser can
    # already handle confidently -- this is not specific to one course, it
    # applies to every outline with a clear grading breakdown or an explicit
    # "Option 1 / Option 2 / ..." structure. The full AI pipeline still runs,
    # unchanged, for messier or ambiguous outlines where the extra accuracy
    # pass is actually needed.
    local_parsed = _local_outline_analysis(row.content_text, course)
    local_components = local_parsed.get('components') or []
    local_total = sum(float(c.get('weight') or 0) for c in local_components if isinstance(c, dict))
    local_plausible = bool(local_components) and 90 <= local_total <= 110
    local_fast_path = local_parsed.get('confidence') == 'high' or bool(local_parsed.get('grading_options'))
    if local_fast_path:
        parsed = local_parsed
        used_fallback = True
    else:
        try:
            parsed = _high_accuracy_outline_analysis(course, row.content_text, scale_profile, _outline_schema())
            if not isinstance(parsed,dict): raise ValueError('Invalid outline response')
        except Exception as exc:
            app.logger.exception('High-accuracy outline analysis failed; using deterministic parser: %s', exc)
            # The fallback is a genuine safety net for outlines with a clear
            # table, but on prose-heavy ones it returns almost nothing. Offering
            # that as a grading policy with a Confirm button under it is the
            # worst behaviour available: a student can accept a near-empty model
            # believing Nexora read their outline, and every grade they see
            # afterwards is computed from it. Refuse rather than mislead.
            if not _fallback_is_usable(local_parsed):
                detail = 'quota' if _looks_like_quota_error(exc) else 'error'
                message = (
                    "Nexora's AI has hit its usage limit for now, so it could not read this "
                    "outline. Please try again later."
                    if detail == 'quota' else
                    "Nexora could not analyze this outline right now. Please try again in a few minutes."
                )
                return jsonify({
                    "error": message,
                    "retryable": True,
                    "reason": detail,
                }), 503
            parsed=local_parsed
            parsed.setdefault('ambiguities',[]).append('The multi-pass AI verification pipeline was unavailable; a simpler parser read the outline instead. Check every component and rule below especially carefully.')
            parsed['confidence']='low'
            parsed['confidence_score']=35
            used_fallback=True
    parsed=_normalize_advanced_grading_model(parsed)
    for group in parsed.get('derived_components') or []:
        if isinstance(group,dict) and group.get('minimum_percent') is not None:
            parsed['pass_rules'].append({'type':'aggregate_minimum','components':group.get('components') or group.get('includes') or [],'minimum_percent':group.get('minimum_percent'),'message':group.get('message') or f"{group.get('name','Derived component')} must meet the minimum."})
    if local_fast_path:
        clean = local_components; total = local_total
    else:
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
        # Validate AI extraction against the deterministic parser. Prefer the result that looks like a complete grading scheme.
        ai_plausible = bool(clean) and 90 <= total <= 110
        if (not ai_plausible and local_plausible) or (not clean and local_components):
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
    parsed['confirmed']=False
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
    # A confirmed new outline fully replaces the previous calculator. Never mix two courses/outlines.
    GradeEntry.query.filter_by(user_id=current_user.id,course_full=course).delete(synchronize_session=False)
    sync_components=list(parsed.get('input_components') or clean)
    existing_keys={re.sub(r'[^a-z0-9]+',' ',str(c.get('name') or '').lower()).strip() for c in sync_components}

    # The default component list can legitimately give a component 0% weight
    # (e.g. Scheme 1 gives Assignments/Labs 0% while only Scheme 2 weighs them,
    # or a component's real weight only exists per-scheme in grading_options).
    # Such components would otherwise never get an enterable card even though
    # pass rules or selection rules depend on them. Pull in anything referenced
    # by a real grading_option, a selection_rule, or a component_minimum that
    # isn't already covered.
    def _register(name, weight=0.0, category=None, notes=''):
        key=re.sub(r'[^a-z0-9]+',' ',str(name or '').lower()).strip()
        if not key:
            return
        # A rule or option often refers to a component by a shorter or looser
        # name than the one it was actually extracted under -- a pass rule
        # saying "Labs" when the real component is "Labs (all 5 count)".
        # Exact-key membership missed that and created a duplicate,
        # zero-weight "Labs" card next to the real one. Match the same way
        # _find_grade_row/_group_percent already do everywhere else in this
        # file: containment in either direction counts as the same component.
        if key in existing_keys or any(_component_keys_match(key, k) for k in existing_keys):
            return
        existing_keys.add(key)
        sync_components.append({
            "name": str(name)[:255],
            "weight": max(0.0,float(weight or 0)),
            "category": str(category or _component_category(name))[:80],
            "notes": str(notes or '')[:255],
        })

    for option in (parsed.get('grading_options') or []):
        if not isinstance(option,dict): continue
        for comp in (option.get('components') or []):
            if not isinstance(comp,dict): continue
            _register(comp.get('name'), comp.get('weight'), comp.get('category'),
                       notes=f"Weight varies by grading option (e.g. {option.get('option_name') or 'an alternate scheme'}).")

    for rule in (parsed.get('selection_rules') or []):
        if not isinstance(rule,dict): continue
        members=rule.get('components') or []
        keep=rule.get('keep'); rtype=str(rule.get('type') or '').lower()
        output_name=rule.get('output_component') or (members[0] if members else 'this group')
        if rtype=='drop_lowest':
            drop=rule.get('drop')
            note=f"Counts toward {output_name} — the lowest {drop or ''} of {len(members)} is dropped.".replace('  ',' ')
        elif rtype in ('best_n_of_m','select_best'):
            note=f"Counts toward {output_name} — best {keep or ''} of {len(members)} counts.".replace('  ',' ')
        else:
            note=f"Counts toward {output_name}."
        for member_name in members:
            _register(member_name, 0.0, None, notes=note)

    for minimum in (parsed.get('component_minimums') or []):
        if isinstance(minimum,dict):
            _register(minimum.get('name'), 0.0, None, notes='Required minimum applies to this component.')

    for comp in sync_components:
        row_entry=GradeEntry(
            id=uuid4().hex,user_id=current_user.id,course_full=course,
            name=str(comp.get('name') or 'Assessment')[:255],
            weight=float(comp.get('weight') or 0),score=None,
            max_score=float(comp.get('default_max') or 100),
            category=str(comp.get('category') or _component_category(comp.get('name')))[:80],
            notes=str(comp.get('notes') or '')[:255],
        )
        db.session.add(row_entry)
    # Keep only the outline that produced the active profile when this was a manual upload.
    if str(row.source_uid or '').startswith('manual-outline:'):
        stale=CourseContent.query.filter_by(user_id=current_user.id,course_full=course,content_type='outline').filter(CourseContent.id!=row.id).all()
        for old_outline in stale: db.session.delete(old_outline)
    db.session.commit()
    return jsonify({"ok":True,"fallback":used_fallback,"analysis":parsed,"pass_mark":profile.pass_mark,"final_minimum":profile.final_minimum})


@app.route("/api/course/<course_code>/outline/correct", methods=["POST"])
@limiter.limit(AI_LIMIT)
@login_required
def correct_course_outline_policy(course_code):
    """Let a student fix a specific detected value, or a wrongly-extracted
    component, without re-uploading the whole outline: adjust a weight or
    pass-rule threshold, remove a component that shouldn't exist (e.g. a
    duplicate created by a degenerate 1-item selection rule), or add one
    that's genuinely missing (e.g. a second midterm the AI missed). A wrong
    structure that's more than a component or two still needs a fresh
    upload, which the outline-replacement flow already covers.
    """
    course=_normalize_course_code(course_code)
    profile=CourseOutlineProfile.query.filter_by(user_id=current_user.id,course_full=course).first()
    if not profile:
        return jsonify({'error':'Analyze a course outline first.'}),404
    try: analysis=json.loads(profile.grading_json or '{}')
    except Exception: analysis={}
    if analysis.get('confirmed'):
        return jsonify({'error':'This grading policy is already confirmed. Upload a new outline to replace it instead.'}),409
    data=request.get_json(silent=True) or {}
    component_edits=data.get('components') if isinstance(data.get('components'),dict) else {}
    pass_rule_edits=data.get('pass_rules') if isinstance(data.get('pass_rules'),list) else []
    pass_mark_edit=data.get('pass_mark')
    remove_names=[str(n) for n in (data.get('remove_components') or []) if str(n).strip()] if isinstance(data.get('remove_components'),list) else []
    add_components=data.get('add_components') if isinstance(data.get('add_components'),list) else []
    rename_components=data.get('rename_components') if isinstance(data.get('rename_components'),dict) else {}
    remove_pass_rule_indices=data.get('remove_pass_rules') if isinstance(data.get('remove_pass_rules'),list) else []
    add_pass_rules=data.get('add_pass_rules') if isinstance(data.get('add_pass_rules'),list) else []

    def _norm_key(name):
        return re.sub(r'[^a-z0-9]+',' ',str(name or '').lower()).strip()

    changed=[]
    components=analysis.get('components') if isinstance(analysis.get('components'),list) else []

    # Removals first, so a removed name can't also match an edit below.
    remove_keys={_norm_key(n) for n in remove_names}
    if remove_keys:
        kept=[c for c in components if isinstance(c,dict) and _norm_key(c.get('name')) not in remove_keys]
        removed_names=[c.get('name') for c in components if isinstance(c,dict) and _norm_key(c.get('name')) in remove_keys]
        if removed_names:
            changed.append(f"Removed: {', '.join(str(n) for n in removed_names)}")
        components=kept
        # Strip dangling references so the grading engine never tries to
        # evaluate a rule against a component that no longer exists.
        for rule in analysis.get('selection_rules') or []:
            if isinstance(rule,dict) and isinstance(rule.get('components'),list):
                rule['components']=[m for m in rule['components'] if _norm_key(m) not in remove_keys]
        for option in analysis.get('grading_options') or []:
            if isinstance(option,dict) and isinstance(option.get('components'),list):
                option['components']=[c for c in option['components'] if isinstance(c,dict) and _norm_key(c.get('name')) not in remove_keys]

    # Additions.
    existing_keys={_norm_key(c.get('name')) for c in components if isinstance(c,dict)}
    for item in add_components:
        if not isinstance(item,dict): continue
        name=str(item.get('name') or '').strip()
        if not name: continue
        key=_norm_key(name)
        if not key or key in existing_keys:
            continue
        try:
            weight=max(0.0,min(100.0,float(item.get('weight') or 0)))
        except (TypeError,ValueError):
            weight=0.0
        components.append({"name":name[:255],"weight":weight,"category":str(item.get('category') or _component_category(name))[:80]})
        existing_keys.add(key)
        changed.append(f"Added: {name} ({weight}%)")

    # Weight edits on whatever components remain.
    for comp in components:
        if not isinstance(comp,dict): continue
        name=str(comp.get('name') or '')
        key=_norm_key(name)
        for edit_name,edit_weight in component_edits.items():
            if _norm_key(edit_name)==key:
                try:
                    new_weight=max(0.0,min(100.0,float(edit_weight)))
                except (TypeError,ValueError):
                    continue
                if round(new_weight,2)!=round(float(comp.get('weight') or 0),2):
                    changed.append(f"{name}: {comp.get('weight')}% → {new_weight}%")
                comp['weight']=new_weight

    # Renames: fix a garbled or wrong extracted label (e.g. "out of 12)"
    # instead of the real "Quiz Average") without losing the component's
    # identity. Keyed by the ORIGINAL name so this can't collide with the
    # weight edits above, which are also keyed by original name and must run
    # first, before any name in `components` actually changes.
    renamed_from={}  # new normalized key -> old normalized key, for the GradeEntry sync below
    if rename_components:
        for comp in components:
            if not isinstance(comp,dict): continue
            old_name=str(comp.get('name') or '')
            old_key=_norm_key(old_name)
            new_name=None
            for edit_name,candidate in rename_components.items():
                if _norm_key(edit_name)==old_key:
                    new_name=str(candidate or '').strip()
                    break
            if not new_name or new_name==old_name:
                continue
            changed.append(f"Renamed: {old_name} → {new_name}")
            comp['name']=new_name[:255]
            renamed_from[_norm_key(new_name)]=old_key

        def _apply_rename(ref_name):
            key=_norm_key(ref_name)
            for new_key,old_key in renamed_from.items():
                if old_key==key:
                    for c in components:
                        if isinstance(c,dict) and _norm_key(c.get('name'))==new_key:
                            return c.get('name')
            return ref_name

        # Keep every other reference to this component (per-option weights,
        # best/drop selection groups, pass-rule targets) pointing at the same
        # real assessment under its new name, so nothing silently stops
        # resolving after the rename.
        for option in (analysis.get('grading_options') or []):
            if not isinstance(option,dict): continue
            for c in (option.get('components') or []):
                if not isinstance(c,dict): continue
                if c.get('source'): c['source']=_apply_rename(c['source'])
                if isinstance(c.get('source_group'),list):
                    c['source_group']=[_apply_rename(x) for x in c['source_group']]
        for rule in (analysis.get('selection_rules') or []):
            if isinstance(rule,dict) and isinstance(rule.get('components'),list):
                rule['components']=[_apply_rename(x) for x in rule['components']]

    analysis['components']=components

    pass_rules=analysis.get('pass_rules') if isinstance(analysis.get('pass_rules'),list) else []
    for edit in pass_rule_edits:
        if not isinstance(edit,dict): continue
        try: idx=int(edit.get('index'))
        except (TypeError,ValueError): continue
        if not (0<=idx<len(pass_rules)) or not isinstance(pass_rules[idx],dict):
            continue
        new_min=edit.get('minimum_percent')
        if new_min is None: continue
        try:
            new_min=max(0.0,min(100.0,float(new_min)))
        except (TypeError,ValueError):
            continue
        old_min=pass_rules[idx].get('minimum_percent')
        if old_min is not None and round(float(old_min),2)!=round(new_min,2):
            changed.append(f"{pass_rules[idx].get('message') or 'pass rule'}: {old_min}% → {new_min}%")
        pass_rules[idx]['minimum_percent']=new_min

    # Pass-rule renames follow the same component identity fix above, so a
    # rule referencing the old garbled name still targets the right component.
    if rename_components:
        for rule in pass_rules:
            if not isinstance(rule,dict): continue
            if rule.get('component'): rule['component']=_apply_rename(rule['component'])
            if isinstance(rule.get('components'),list):
                rule['components']=[_apply_rename(x) for x in rule['components']]

    # Pass-rule removals, by index into the array as it stood before this
    # request touched it (edits above only change values in place, so indices
    # are still valid here).
    remove_rule_indices=set()
    for raw_idx in remove_pass_rule_indices:
        try: idx=int(raw_idx)
        except (TypeError,ValueError): continue
        if 0<=idx<len(pass_rules): remove_rule_indices.add(idx)
    if remove_rule_indices:
        removed_msgs=[pass_rules[i].get('message') or f'Pass rule {i+1}' for i in sorted(remove_rule_indices) if isinstance(pass_rules[i],dict)]
        pass_rules=[r for i,r in enumerate(pass_rules) if i not in remove_rule_indices]
        if removed_msgs:
            changed.append(f"Removed pass rule(s): {', '.join(removed_msgs)}")

    # Pass-rule additions. A rule tied to zero components is a plain overall
    # threshold; one component is a direct minimum on it; more than one is
    # treated as a combined/aggregate minimum across all of them together
    # (e.g. "quizzes + tutorials + final combined must be at least 50%").
    for item in add_pass_rules:
        if not isinstance(item,dict): continue
        try:
            minimum=max(0.0,min(100.0,float(item.get('minimum_percent'))))
        except (TypeError,ValueError):
            continue
        message=str(item.get('message') or '').strip()[:500]
        members=[str(x).strip() for x in (item.get('components') or []) if str(x).strip()]
        if not members:
            rule={'type':'overall_minimum','minimum_percent':minimum,
                  'message':message or f'Total course grade must be at least {minimum:g}%.'}
        elif len(members)==1:
            rule={'type':'component_minimum','component':members[0],'minimum_percent':minimum,
                  'message':message or f'{members[0]} must be at least {minimum:g}% to pass.'}
        else:
            rule={'type':'aggregate_minimum','components':members,'minimum_percent':minimum,
                  'message':message or f'{" + ".join(members)} combined must be at least {minimum:g}% to pass.'}
        pass_rules.append(rule)
        changed.append(f"Added pass rule: {rule['message']}")

    analysis['pass_rules']=pass_rules

    if pass_mark_edit is not None:
        try:
            new_pass_mark=max(0.0,min(100.0,float(pass_mark_edit)))
            if round(new_pass_mark,2)!=round(float(analysis.get('pass_mark') or 50),2):
                changed.append(f"Overall pass mark: {analysis.get('pass_mark')}% → {new_pass_mark}%")
            analysis['pass_mark']=new_pass_mark
        except (TypeError,ValueError):
            pass

    if not changed:
        return jsonify({'error':'No valid corrections were provided.'}),400

    analysis=_normalize_advanced_grading_model(analysis)
    analysis['ambiguities']=_outline_model_validation(analysis)
    analysis['manually_corrected']=True
    analysis['correction_log']=(analysis.get('correction_log') or [])+[{
        'at':datetime.utcnow().isoformat()+'Z','changes':changed,
    }]
    profile.grading_json=json.dumps(analysis,ensure_ascii=False)
    try: profile.pass_mark=float(analysis.get('pass_mark') or 50)
    except Exception: pass
    profile.updated_at=datetime.utcnow()

    # Sync GradeEntry rows to match: update weights, delete removed
    # components' rows (only if that mark was never actually entered, to
    # avoid silently destroying a real recorded grade), rename in place
    # (never delete+recreate) so a rename never loses an already-entered
    # mark, and create rows for newly added components.
    rows=GradeEntry.query.filter_by(user_id=current_user.id,course_full=course).all()
    row_map={_norm_key(r.name):r for r in rows}
    for key in remove_keys:
        row=row_map.get(key)
        if row is not None and row.score is None:
            db.session.delete(row)
    for comp in components:
        if not isinstance(comp,dict): continue
        new_name=str(comp.get('name') or 'Assessment')
        key=_norm_key(new_name)
        lookup_key=renamed_from.get(key,key)
        row=row_map.get(lookup_key)
        if row is not None:
            row.weight=float(comp.get('weight') or 0)
            if lookup_key!=key or row.name!=new_name[:255]:
                row.name=new_name[:255]
        else:
            db.session.add(GradeEntry(id=uuid4().hex,user_id=current_user.id,course_full=course,
                                       name=new_name[:255],
                                       weight=float(comp.get('weight') or 0),score=None,max_score=100.0,
                                       category=str(comp.get('category') or 'assessment')[:80]))
    db.session.commit()
    return jsonify({'ok':True,'changed':changed})


@app.route("/api/course/<course_code>/outline/confirm", methods=["POST"])
@login_required
def confirm_course_outline_policy(course_code):
    course=_normalize_course_code(course_code)
    profile=CourseOutlineProfile.query.filter_by(user_id=current_user.id,course_full=course).first()
    if not profile:
        return jsonify({'error':'Analyze a course outline first.'}),404
    try: analysis=json.loads(profile.grading_json or '{}')
    except Exception: analysis={}
    analysis['confirmed']=True
    analysis['confirmed_at']=datetime.utcnow().isoformat()+'Z'
    profile.grading_json=json.dumps(analysis,ensure_ascii=False)
    profile.updated_at=datetime.utcnow()
    db.session.commit()
    return jsonify({'ok':True})


@app.route("/api/course/<course_code>/study-tools/generate", methods=["POST"])
@limiter.limit(AI_LIMIT)
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
    scan_meta = payload.get("scan_meta") if isinstance(payload.get("scan_meta"), dict) else {}
    scan_course = str(payload.get("course") or (replace_courses[0] if replace_courses else "") or "").strip()
    if scan_course:
        scan_course = _normalize_course_code(scan_course)
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
    # Record the outcome of this specific sync attempt so a totally failed
    # scan (e.g. every Brightspace API call 404ing for one course while
    # others work fine) is visible on the course page itself, instead of
    # silently leaving the course empty with no explanation anywhere.
    if scan_course:
        course_row = SyncedCourse.query.filter_by(user_id=user.id, course_code=scan_course).first()
        if course_row:
            errors = scan_meta.get("diagnostics", {}).get("errors") if isinstance(scan_meta.get("diagnostics"), dict) else {}
            scan_ok = bool(items) or (scan_meta.get("complete") is True)
            course_row.last_sync_ok = scan_ok
            course_row.last_sync_items = len(items)
            course_row.last_sync_at = datetime.utcnow()
            if not scan_ok and isinstance(errors, dict) and errors:
                parts = [f"{section}: {msg}" for section, msg in list(errors.items())[:4]]
                course_row.last_sync_error = ("Brightspace didn't return any content for this course. " + "; ".join(parts))[:2000]
            elif not scan_ok:
                course_row.last_sync_error = "Brightspace didn't return any content for this course on the last sync attempt."[:2000]
            else:
                course_row.last_sync_error = ""
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
@limiter.limit(AI_LIMIT)
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
            app.logger.warning('Generic outline extraction failed: %s', exc)
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
        # Was a four-line prompt with a components-only schema while the course
        # workspace analyzed the same outline against the full one. Sharing the
        # builder means selection rules, aggregate minimums and scheme gates are
        # recognised here too, and reasoning=True matches the tier that decision
        # deserves on both paths.
        prompt = _outline_analysis_prompt(text, "COURSE")
        parsed = _gemini_json(
            "You are a precise university course-outline parser. Output only valid JSON.",
            prompt,
            4000,
            0.1,
            reasoning=True,
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

    if not components:
        app.logger.warning(
            "Generic outline analysis produced zero components. raw_components=%r extracted_chars=%d",
            parsed.get("components"), len(text),
        )

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

# ============================
# Error handling
# ============================
# Without these, an unhandled error rendered the bare Werkzeug page to a
# student, and a rate-limited request looked like the site was broken rather
# than like a limit they had hit.

def _wants_json():
    """True when the caller is the front-end's fetch() rather than a browser nav."""
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best == "application/json"


def _error_response(code, title, message):
    if _wants_json():
        return jsonify({"error": message}), code
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(404)
def handle_not_found(_exc):
    return _error_response(404, "Page not found", "That page does not exist. It may have been renamed or removed.")


@app.errorhandler(413)
def handle_too_large(_exc):
    return _error_response(
        413, "That file is too large",
        "Uploads are limited to 64 MB in total. Try fewer pages, or compress the screenshots first.",
    )


@app.errorhandler(429)
def handle_rate_limited(exc):
    detail = getattr(exc, "description", "") or "Too many requests."
    return _error_response(
        429, "Slow down for a moment",
        f"{detail} This limit protects the shared AI quota every student's outline analysis depends on.",
    )


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_unexpected(exc):
    # Re-raise the HTTP errors handled above so they keep their own responses.
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    return _error_response(
        500, "Something went wrong",
        "Nexora hit an unexpected error. It has been logged. Try again, and if it keeps happening the problem is on our side, not yours.",
    )


@app.route("/healthz")
@limiter.exempt
def healthz():
    """Cheap liveness probe: confirms the process is up and the database answers."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok"})
    except Exception as exc:
        app.logger.exception("Health check failed: %s", exc)
        return jsonify({"status": "degraded", "database": "unreachable"}), 503


if __name__ == "__main__":
    app.run(debug=True)
