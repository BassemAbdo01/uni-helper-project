import os
import re
import html as html_module
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
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
import calendar as calendar_module
from functools import wraps
from uuid import uuid4
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, send_file, session, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, or_
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
# pypdf, python-pptx and python-docx are imported where they are USED, not here.
# Measured with -X importtime: they cost ~0.5s of the 2.7s it takes to import
# this module, and Render gives the web service half a vCPU, so that is several
# seconds of every worker boot. They are needed by one function -- pulling text
# out of an uploaded lecture -- and by no request that does not upload a file.
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
# Stay signed in for a month. This said 14 days and delivered a browser-session
# cookie: PERMANENT_SESSION_LIFETIME applies only to a session marked permanent,
# and `login_user(user)` was called with no `remember`, so Flask-Login never set
# a remember cookie at all. Reported as being logged out at least once a day --
# which, for an app a student opens to check what is due tonight, is enough to
# stop them opening it.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_REFRESH_EACH_REQUEST"] = True
app.config["REMEMBER_COOKIE_SECURE"] = IS_PRODUCTION
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# NAME THE DRIVER. A bare postgresql:// lets SQLAlchemy pick its default DBAPI,
# and that default is not a constant: a Render rebuild installed a newer
# SQLAlchemy whose default became psycopg (v3) instead of psycopg2, and every
# worker died at import with `ModuleNotFoundError: No module named 'psycopg'`
# -- on a deploy whose diff could not have touched it. requirements.txt pinned
# nothing, so the build that broke differed from the build that worked only by
# the day it ran. Pinning fixes that; saying which driver we mean makes it
# impossible for the same class of change to reach us again.
if database_url and database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uni_helper.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # up to 8 outline screenshots/pages

# OCR reads text no better above roughly 2600px on the long edge, and every pixel
# past that is memory this instance does not have. Below 1100px it starts losing
# small print, so images are scaled toward the band from whichever side they are on.
# A guard against an account with an implausible number of enrolments, not a
# page size: a student's courses should all be on their dashboard. Raised once
# the import stopped collapsing offerings of the same code into one row -- a
# four-year degree is comfortably past sixty enrolments, and the page groups by
# term, so the only thing a low cap bought was courses vanishing in silence.
DASHBOARD_COURSE_LIMIT = int(os.environ.get("DASHBOARD_COURSE_LIMIT", "400"))

# The most enrolments one sync will store. Same reasoning: a bound on a
# pathological payload, not an opinion about how many courses a student may
# have. Anything past it is REPORTED rather than dropped quietly -- see the
# `truncated` key on the import result.
COURSE_IMPORT_MAX = int(os.environ.get("COURSE_IMPORT_MAX", "400"))
# Items per content payload. It was a bare `[:1200]` in three places with
# nothing counting what fell off; the import result now says `truncated`.
CONTENT_IMPORT_MAX = int(os.environ.get("CONTENT_IMPORT_MAX", "1200"))
# A payload carrying less than this share of what a course already holds is
# refused its `replace_courses` delete. See the S1 comment in _import_content:
# the extension's "complete" is its own opinion, and a scrape that returns 3 of
# 40 topics with a 200 looks exactly like a complete one.
REPLACE_FLOOR = float(os.environ.get("SYNC_REPLACE_FLOOR", "0.5"))
ACADEMIC_UPDATE_KEEP_DAYS = int(os.environ.get("ACADEMIC_UPDATE_KEEP_DAYS", "45"))

# Render sets RENDER_GIT_COMMIT on every deploy. Reported by /healthz so "did it
# ship?" has an answer that does not depend on spotting a changed word somewhere.
BUILD_REVISION = (os.environ.get("RENDER_GIT_COMMIT", "").strip() or "dev")[:12]

OCR_MAX_EDGE = int(os.environ.get("OCR_MAX_EDGE", "2600"))
OCR_MIN_EDGE = int(os.environ.get("OCR_MIN_EDGE", "1800"))

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
    # A ceiling on every route, which the tighter per-route limits override.
    # Thirty-four API endpoints had none; decorating them by hand would have
    # left the thirty-fifth uncovered the day someone added it.
    default_limits=["600 per hour", "60 per minute"],
    # The .ics feed is polled by Google on its own schedule and carries its own
    # limit; page loads should not be counted against an API budget.
    default_limits_exempt_when=lambda: request.endpoint in {"static", "healthz"},
)

# Gemini work is the scarce resource: an outline analysis is a three-stage
# pipeline, so a handful of these is worth many ordinary requests.
#
# It was "40 per day; 12 per hour", which is plenty for a student using a
# feature that works and nowhere near enough for one troubleshooting a feature
# that does not. Reported from the calendar after a schedule import kept coming
# back empty: twelve attempts is about fifteen minutes of trying different
# wordings, and the reward for persevering was being locked out of the feature.
# Environment-tunable so the ceiling can move without a deploy.
AI_LIMIT = os.environ.get("AI_LIMIT", "150 per day; 40 per hour")


def ai_limit(view):
    """The AI allowance.

    Every call counts, including the ones that end in an error. Not charging
    for failures was tried and backed out: a vision call that reads an image and
    then answers 422 "nothing dated in that picture" has ALREADY spent the
    Gemini quota this limit exists to protect, so exempting it puts a hole in
    the guarantee rather than a kindness in the budget. Two existing tests
    caught it by hammering a route with failing requests and never being
    stopped. The reported lock-out was the ceiling being too low, which is
    fixed above, and not what it counted.
    """
    return limiter.limit(AI_LIMIT)(view)

# ============================
# Access: a free month, then paid
# ============================
# Every number here is an environment variable because the pricing is not
# settled. Nothing in the code assumes an amount or a currency; the gate only
# ever asks _entitlement whether this account is active today.
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "30"))
REFERRAL_MONTH_DAYS = int(os.environ.get("REFERRAL_MONTH_DAYS", "30"))
REFERRAL_MONTHS_MAX = int(os.environ.get("REFERRAL_MONTHS_MAX", "6"))
# Existing accounts at the moment this shipped are not shown a paywall for
# something they have been using for free; they are granted a year.
GRANDFATHER_DAYS = int(os.environ.get("GRANDFATHER_DAYS", "365"))
# Set once there is something to charge. Until then the gate still works and
# the expiry copy simply says pricing is coming.
PRICE_LABEL = os.environ.get("PRICE_LABEL", "").strip()

# The public outline reader: the one endpoint strangers can spend money on.
# One Gemini call each, roughly a cent, so the ceiling is what keeps the bill
# predictable. Raise it when there is revenue behind it.
# How many structurally different outlines evals/outline_corpus.py covers. The
# landing page quotes it as proof, so it lives beside the other public-facing
# numbers rather than being typed into the template and left to rot.
# The public Chrome Web Store listing, live since 2026-10-07. It is the default
# rather than something to remember to set, because an unset env var here means
# every student is told to unzip a folder and load it unpacked -- six steps a
# first-time user will not finish. Still overridable, for a fork or a relisting.
EXTENSION_URL = (os.environ.get("EXTENSION_URL", "").strip()
                 or "https://chromewebstore.google.com/detail/nexora-sync/hoildppeiocamckogdoljglekifhcpkb")
# The packaged extension, served from the site itself. Until the Chrome Web
# Store listing exists there is NO way to get it -- the connect page could
# only tell students to search a store that has nothing on it, which is a
# first-run instruction that cannot be followed. The store link still wins
# when EXTENSION_URL is set, because one click beats unzipping.
EXTENSION_ZIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store", "nexora-sync.zip")
EXTENSION_ZIP_AVAILABLE = os.path.exists(EXTENSION_ZIP)

# Used only until a student's browser reports their own zone. UTC is the honest
# default: it is what every date meant before User.timezone existed.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "UTC").strip() or "UTC"

# A shortlist for the Settings picker, not a limit: the page adds whatever the
# browser reports if it is not here, and /api/timezone accepts any IANA name
# zoneinfo can resolve. Ordered west to east across the places students are.
COMMON_TIMEZONES = [
    "America/Vancouver", "America/Edmonton", "America/Winnipeg", "America/Toronto",
    "America/Halifax", "America/St_Johns", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Mexico_City",
    "America/Bogota", "America/Sao_Paulo", "Europe/London", "Europe/Dublin",
    "Europe/Lisbon", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
    "Europe/Rome", "Europe/Athens", "Europe/Istanbul", "Europe/Moscow",
    "Africa/Lagos", "Africa/Cairo", "Africa/Nairobi", "Africa/Johannesburg",
    "Asia/Jerusalem", "Asia/Riyadh", "Asia/Dubai", "Asia/Karachi",
    "Asia/Kolkata", "Asia/Dhaka", "Asia/Bangkok", "Asia/Shanghai",
    "Asia/Hong_Kong", "Asia/Singapore", "Asia/Seoul", "Asia/Tokyo",
    "Australia/Perth", "Australia/Adelaide", "Australia/Sydney",
    "Pacific/Auckland", "UTC",
]

OUTLINE_CORPUS_CASES = 16

PUBLIC_ANALYSES_PER_DAY = int(os.environ.get("PUBLIC_ANALYSES_PER_DAY", "150"))
PUBLIC_ANALYSES_PER_IP = os.environ.get("PUBLIC_ANALYSES_PER_IP", "3 per day")
# Whether the cap message mentions making an account. Off means it just says
# come back tomorrow -- a rate limit dressed as a sales pitch reads as
# manufactured scarcity, and students notice.
PUBLIC_CAP_MENTIONS_SIGNUP = os.environ.get("PUBLIC_CAP_MENTIONS_SIGNUP", "1") != "0"

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
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "nexora.notify@gmail.com").strip()
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


# Which academic term a course belongs to, and how terms sort. Brightspace
# terms arrive human-readable ("Winter 2026") or as D2L codes ("2026.10").
# This lived inside home() and the grade calculator then listed every course a
# student had ever taken in one flat dropdown -- thirty-one rows for a full
# degree, with no way to find this semester's. Two screens grouping courses by
# different rules is the same class of bug as two screens disagreeing about a
# grade, so there is one definition.
_TERM_RANK = {"winter": 1, "spring": 2, "summer": 2, "fall": 3, "autumn": 3, "other": 4}


def _term_meta(value):
    """(season, year) for a term string; ("Other", 0) when it says nothing."""
    raw = str(value or "").strip()
    m = re.search(r"(?i)\b(winter|spring|summer|fall|autumn)\s*(20\d{2})\b", raw)
    if m:
        return m.group(1).title().replace("Autumn", "Fall"), int(m.group(2))
    m = re.search(r"(?i)\b(20\d{2})[._ -]?(10|20|30)\b", raw)
    if m:
        return {"10": "Winter", "20": "Summer", "30": "Fall"}.get(m.group(2), "Other"), int(m.group(1))
    return "Other", 0


def _term_label(value):
    season, year = _term_meta(value)
    return f"{season} {year}" if year else "Other courses"


def _term_sort_key(value):
    """Newest term first. Sort a list of labels with reverse=True."""
    season, year = _term_meta(value)
    return (year, _TERM_RANK.get(season.lower(), 4))


# An org unit with no course code in its name gets `COURSE<orgUnitId>` from the
# extension's parser -- Carleton's "(MS LAP) Math and Stats Learning Assistance
# Program" is a real example, and so is a co-op or orientation shell. The code
# has to stay as it is, because it is the join key for content, deadlines and
# grades, but printing "COURSE1006" at the top of a card tells a student
# nothing and looks like a fault. The card shows the course's name instead.
_PLACEHOLDER_CODE = re.compile(r"^COURSE\d+$", re.I)


# ---------------------------------------------------------------------------
# Organising a course's material
# ---------------------------------------------------------------------------
# The workspace grouped by the FULL Brightspace breadcrumb, so every
# "Lectures › Topic 04 - OOP concepts, and Refactoring" became its own heading
# -- twenty headings over twenty-two lectures, most of them holding one item,
# each heading repeating the title directly underneath it. Reported, fairly, as
# "insanely disorganized".
_NUM_RUN = re.compile(r"(\d+)")
_BREADCRUMB = re.compile(r"\s*[›>/|»]\s*")


def _natural_key(text):
    """Sort "Topic 2" before "Topic 10".

    Plain string ordering puts 10 before 2, which on a course with more than
    nine of anything shuffles the list into nonsense -- and a lecture list out
    of order is the one thing a student cannot work around.
    """
    return [int(part) if part.isdigit() else part.lower()
            for part in _NUM_RUN.split(str(text or ""))]


def _strip_course_prefix(title, code):
    """"SYSC2004 - Topic 01 - ..." -> "Topic 01 - ...".

    Every row repeated the course code, on the one page that can only ever be
    about that course. The code on the row is often the bare one (SYSC2004)
    while the workspace is an offering (SYSC2004A), so the comparison is on the
    letters-and-digits stem.
    """
    title = str(title or "").strip()
    stem = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    if not stem:
        return title
    match = re.match(r"^([A-Za-z]{2,8})\s*([0-9]{3,5})[A-Za-z]?\s*[-–—:|]\s*", title)
    if match and (match.group(1) + match.group(2)).upper() == stem[:len(match.group(1) + match.group(2))]:
        return title[match.end():].strip() or title
    return title


def _organise_course_items(items, code=""):
    """Group a course's material the way a student reads it.

    By the TOP of the breadcrumb, not the whole of it. Brightspace nests
    "Lectures › Topic 04 › ..." and grouping on the full path makes a heading
    per topic; grouping on the first segment gives three real sections and puts
    the topic on the row, where it reads as context rather than as structure.

    Returns [{heading, items: [{row, title, label}]}] with both the groups and
    the rows in natural order, so Topic 2 precedes Topic 10.
    """
    groups = {}
    for row in items or []:
        crumbs = [c for c in _BREADCRUMB.split(str(getattr(row, "module_name", "") or "")) if c.strip()]
        heading = crumbs[0].strip() if crumbs else "Course material"
        label = " › ".join(c.strip() for c in crumbs[1:])
        entry = groups.setdefault(heading, {"heading": heading, "items": []})
        entry["items"].append({
            "row": row,
            "title": _strip_course_prefix(getattr(row, "title", ""), code),
            "label": label,
        })
    out = list(groups.values())
    for group in out:
        # Inside a topic, the lecture itself comes before the code samples and
        # loose files that sit beside it -- otherwise "Shape" and "Die" sort
        # above the Topic 05 and Topic 08 lectures they belong to.
        group["items"].sort(key=lambda x: (
            _natural_key(x["label"]),
            0 if re.match(r"^(topic|lecture|week|module|chapter|unit)\b", x["title"], re.I) else 1,
            _natural_key(x["title"])))
    # Groups in the order their first item would sort, so "Lecture recording"
    # and "Lectures" keep Brightspace's own shape rather than being alphabetised
    # into something the student has never seen.
    out.sort(key=lambda g: _natural_key(g["heading"]))
    return out


_RECORDING_WORD = re.compile(
    r"\b(recording|recordings|recorded|webinar)\b|\blecture\s+capture\b", re.I)
# A video or audio file sitting in the lecture list IS the recording, whatever
# the lecturer called it.
_RECORDING_EXT = {"mp4", "m4v", "mov", "webm", "avi", "mkv", "wmv", "m4a", "mp3", "wav"}

# Labs, tutorials and the rest of the timetabled small-group work. A student
# reads these on a different day from the slides, which is the whole reason
# they want them apart. `\blab\b` deliberately does not match "lab2_starter",
# which is a code archive and belongs with the resources.
_LAB_WORD = re.compile(
    r"\b(lab|labs|laboratory|tutorial|tutorials|workshop|workshops"
    r"|seminar|seminars|practical|practicals|recitation)\b", re.I)

# Words that name teaching material in the item's OWN title.
_LECTURE_WORD = re.compile(
    r"\b(lecture|lectures|slides|slide\s*deck|lesson|lessons|notes|handout)\b", re.I)
_LECTURE_NUMBERED = re.compile(
    r"\b(?:lecture|lec|slides?|week|wk|module|chapter|ch|lesson|unit|topic)"
    r"\s*[-_#:.]?\s*\d+"
    r"|\d+\s*[-_#:.]?\s*(?:lecture|lec|slides?|week|module|chapter|lesson|unit|topic)\b", re.I)
_LECTURE_EXT = {"ppt", "pptx", "key"}

# A module that names the MATERIAL, as opposed to one that names a WEEK.
# This distinction is the whole bug: "Week 1" is a time bucket and everything
# a course puts in it -- the Piazza link, the welcome note, the integrity
# policy -- is not thereby a lecture.
_LECTURE_MODULE = re.compile(
    r"\b(lecture|lectures|slides|notes|lesson|lessons|course\s*material"
    r"|courseware|readings?)\b", re.I)


def _is_lecture_recording(row):
    """Is this item the recording rather than the slides?

    Reported from a real ELEC2501B workspace: "Lecture_3" and "Lecture 3
    Recording" sat next to each other, as did Lecture 4, Lecture 05 and both
    parts of Lecture 06 -- eighteen rows alternating between the thing you read
    and the thing you watch. They classify identically (both `lecture`) and
    carry the same breadcrumb, so nothing downstream could tell them apart.

    Deliberately narrow. The cost of a wrong answer here is only a row under
    the wrong heading, but "video" on its own is a topic as often as it is a
    format -- "Lecture 5: Video Transmission" is a slide deck -- so the word
    is not matched. A genuine video lecture with a video file on it is caught
    by its extension instead, which is the half that cannot be wrong.
    """
    ext = str(getattr(row, "file_ext", "") or "").strip().lower().lstrip(".")
    if ext in _RECORDING_EXT:
        return True
    text = "%s %s" % (getattr(row, "title", "") or "",
                      getattr(row, "module_name", "") or "")
    return bool(_RECORDING_WORD.search(text))


def _material_section(row):
    """Which heading of the Lectures tab this row belongs under.

    `lecture` | `recording` | `lab` | `other`.

    Reported as *"theres like so many types recordings, then labs, then
    lectures again which arent lectures like theres SO much useless stuff"*.
    Measured against the published `classify.js`, with its own corpus shapes:
    "Piazza", "Welcome!", "Grades FAQ", "Academic Integrity Policy", "Java
    Install Guide" and a Zoom link were ALL classified `lecture`, because
    `LECTURE_NUMBERED` is tested against the item's text *plus its module name*
    and `"Week 1"` matches a week-plus-number pattern. Every item in such a module
    became a lecture.

    So the section is decided on a POSITIVE signal in the item's own title and
    the default is `other` -- never on a blacklist of junk words. A list of
    things to exclude is endless and wrong the first time a course invents a
    new kind of link; a list of things that mean "this is a lecture" is short,
    and anything unrecognised lands under "Other course material", where it is
    still on the page and still one click from the student.

    A module only counts when it names the MATERIAL ("Lectures", "Course
    material"), never when it names a week.
    """
    if _is_lecture_recording(row):
        return "recording"
    title = str(getattr(row, "title", "") or "")
    module = str(getattr(row, "module_name", "") or "")
    if _LAB_WORD.search(title) or _LAB_WORD.search(module):
        return "lab"
    ext = str(getattr(row, "file_ext", "") or "").strip().lower().lstrip(".")
    if ext in _LECTURE_EXT:
        return "lecture"
    if _LECTURE_WORD.search(title) or _LECTURE_NUMBERED.search(title):
        return "lecture"
    # A material module vouches for an item, but only one that is itself a
    # numbered item or a real file. Left to vouch on its own it has the same
    # fault as the week module, just rarer: a course that files everything
    # under one "Course material" folder would empty the whole folder back
    # into the Lectures list, Piazza link and all. A bare link with neither a
    # number nor a file behind it is the one shape that is never a lecture.
    # The number may be split across the two ("04" inside "Lecture Slides"),
    # which is the one place reading the module is safe -- the branch is
    # already gated on a module that names the material.
    if _LECTURE_MODULE.search(module) and (
            ext or _LECTURE_NUMBERED.search("%s %s" % (title, module))):
        return "lecture"
    return "other"


def _flatten_thin_groups(groups):
    """Drop the sub-headings when they are not organising anything.

    Brightspace courses are as often filed by WEEK as by topic, and a week
    holds one lecture. So a four-lecture section rendered as four headings
    reading "Week 1 (1)", "Week 2 (1)", "Week 3 (1)", "Week 4 (1)" -- more
    heading than content, and the week is already in every title. Same
    complaint `_organise_course_items` exists to answer ("thirteen headings
    over twenty-two lectures"), arriving by a different road.

    The test is whether the grouping pays for itself: more groups than half
    the items means the average group holds fewer than two, so it is costing
    a heading per row. The heading is then moved ONTO the row, where it still
    says which week without claiming to be structure.
    """
    groups = list(groups or [])
    items = sum(len(g["items"]) for g in groups)
    if len(groups) <= 1 or len(groups) * 2 <= items:
        return groups
    merged = []
    for group in groups:
        for entry in group["items"]:
            entry = dict(entry)
            crumbs = [c for c in (group["heading"], entry.get("label") or "") if c]
            entry["label"] = " › ".join(crumbs)
            merged.append(entry)
    return [{"heading": "", "items": merged}]


def _section_course_material(lectures, resources):
    """{'lecture': [...], 'recording': [...], 'lab': [...], 'other': [...]}.

    Both buckets are re-sectioned together on purpose. A lab can be sitting in
    either -- promoted into `lecture` by the module bug, or dropped to `page`
    because the extension has no lab rule at all -- and a student looking for
    Lab 3 does not care which. Nothing is dropped: whatever is not teaching
    material joins the resources under "Other course material", so this can
    only ever MOVE a row, never hide one.
    """
    out = {"lecture": [], "recording": [], "lab": [], "other": []}
    for row in list(lectures or []):
        out[_material_section(row)].append(row)
    for row in list(resources or []):
        section = _material_section(row)
        # A resource is only promoted when it is clearly lab or recording
        # material. Pulling every pdf that says "notes" out of the resource
        # list would be the same over-reach in the other direction.
        out[section if section in ("lab", "recording") else "other"].append(row)
    return out


def _display_code(code):
    """The code to print on a card, or "" when the code is a placeholder."""
    text = str(code or "").strip()
    return "" if _PLACEHOLDER_CODE.match(text) else text


def _newest_offering_per_code(rows):
    """{course_code: the offering that should represent it}.

    A course taken twice is two SyncedCourse rows sharing one code, and every
    screen that lists courses has to pick one. Left to a dict comprehension the
    winner is whichever row the query happened to yield last, so ECOR1031 --
    taken in first year and again this year -- turned up filed under Fall 2025
    with its old title, on a page a student uses to enter this term's marks.

    The newest term wins, because a student naming a course means the one they
    are in now. Keep this as the single definition: the dashboard, the grade
    calculator and the study room all answer the same question, and two of them
    disagreeing is the same class of bug as two disagreeing about a grade.
    """
    best = {}
    for row in rows:
        code = getattr(row, "course_code", "") or "OTHER"
        current = best.get(code)
        if current is None or _term_sort_key(row.term or "") > _term_sort_key(current.term or ""):
            best[code] = row
    return best


def _current_term_label(today=None):
    """September is Fall, January is Winter -- the date decides, never whichever
    group happens to sort first, or an early Winter enrolment made in November
    takes the page over."""
    today = today or date.today()
    season = "Winter" if today.month <= 4 else "Summer" if today.month <= 8 else "Fall"
    return f"{season} {today.year}"


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
    # The last ATTEMPT, so a feed that cannot succeed still backs off.
    last_sync_attempt = db.Column(db.DateTime, nullable=True)
    announce_scan_at = db.Column(db.DateTime, nullable=True)
    sync_status = db.Column(db.String(30), default="never")
    sync_message = db.Column(db.Text, default="Not synchronized yet.")
    extension_token = db.Column(db.String(128), default="")
    # The feed URL is handed to Google, which cannot log in, so the token in
    # the path is the whole of the authentication. It is issued on demand and
    # can be rotated, which is the only way to un-share a link once shared.
    calendar_token = db.Column(db.String(64), default="")
    # Whether the first-run tour has been seen. On the account rather than in
    # localStorage: a student signing in on a laptop after their phone has
    # already been shown it, and clearing site data should not start it again.
    tour_done = db.Column(db.Boolean, default=False)

    # --- access ---
    # A month free from signup, no card. After that the account goes read-only
    # rather than dark: what is already imported stays visible, nothing
    # refreshes, and nothing costs a Gemini call. See _entitlement.
    trial_ends_at = db.Column(db.DateTime, nullable=True)
    paid_until = db.Column(db.DateTime, nullable=True)
    # Referral: a month for each friend who actually starts using it. The code
    # is the student's own; referred_by is whoever sent them.
    referral_code = db.Column(db.String(12), default="", index=True)
    referred_by = db.Column(db.String(12), default="")
    referral_months = db.Column(db.Integer, default=0)
    referral_credited = db.Column(db.Boolean, default=False)
    # The student's own timezone, as an IANA name ("America/Toronto"), captured
    # from the browser. Without it every deadline was dated in UTC, and a
    # Brightspace due date of 11:59 PM Eastern -- the commonest deadline in a
    # university -- is 03:59Z the NEXT DAY. The calendar showed it a day late,
    # which is the one failure that makes a student miss the work.
    timezone = db.Column(db.String(64), default="")
    # So the "your month is nearly up" email is sent once, not on every visit.
    trial_notice_sent = db.Column(db.String(20), default="")

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
    # The instant the LMS actually gave, kept verbatim. `due_raw` is a calendar
    # day derived from it in the student's timezone, and a derived value alone
    # cannot be re-derived: when the zone was wrong, every date was wrong and
    # unrepairable without re-fetching from the university. Storing this makes
    # a timezone change a local recomputation -- see _recompute_task_dates.
    source_due_at = db.Column(db.String(40), default="")
    # Where this deadline lives in the LMS. Only ever a URL the LMS itself
    # gave us -- see _lms_link. Empty for anything the student typed, and for
    # a source that offered no link; the UI shows no button rather than
    # guessing one, because a link that 404s is the same class of failure as
    # a wrong date: confidently wrong on the screen they trust.
    source_link = db.Column(db.Text, default="")
    sync_state = db.Column(db.String(30), default="active")
    last_seen_sync = db.Column(db.String(40), default="")
    completed = db.Column(db.Boolean, default=False)
    # A midterm is a deadline that also has a clock time and a room. Rather than
    # a second table that the dashboard, calendar, feed and completion logic
    # would all have to learn about separately, an exam is a Task with `kind`
    # set and these three filled in -- so everything downstream already works.
    due_time = db.Column(db.String(5), default="")
    location = db.Column(db.String(255), default="")
    notes = db.Column(db.Text, default="")


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
    # The extension has sent this on every item for a long time and the server
    # dropped it, which is why a stale row cannot say whether it is a PDF or a
    # web page -- and therefore why a lecture link could never be turned into a
    # download. It is the ONLY thing that separates the two, measured against
    # D2L's own TypeIdentifier across ten real courses: 0 wrong out of 100
    # non-files. "A scraped value is worthless until something consumes it",
    # the same shape as `due_at` before `_import_deadlines` existed.
    file_ext = db.Column(db.String(12), default="")
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
    # What each assessment actually covers, read out of the outline once and
    # kept. The planner rebuilds constantly; a Gemini call per rebuild would
    # spend the quota the outline pipeline needs, and the answer never changes
    # between rebuilds anyway.
    study_brief_json = db.Column(db.Text, default="")
    brief_built_at = db.Column(db.DateTime, nullable=True)
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
    # ok | partial | error | never. `last_sync_ok` is a boolean and cannot say
    # "this sync arrived short and was refused its replace", which is neither
    # a success nor a failure and is the one state a student can act on.
    last_sync_state = db.Column(db.String(20), default="never")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClassBlock(db.Model):
    """One recurring commitment in a student's week.

    Times are minutes from midnight rather than a time column: the planner does
    arithmetic on them constantly, and a weekly repeat has no date to anchor a
    timestamp to. Not only classes -- a shift at work or a commute blocks study
    time exactly as a lecture does, which is why `kind` is open.
    """
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_full = db.Column(db.String(40), default="")
    label = db.Column(db.String(200), default="")
    weekday = db.Column(db.Integer, default=0)          # 0 = Monday
    start_min = db.Column(db.Integer, default=0)
    end_min = db.Column(db.Integer, default=0)
    location = db.Column(db.String(255), default="")
    kind = db.Column(db.String(30), default="lecture")
    # Biweekly labs are ordinary, and a weekly-only model is wrong both ways:
    # entered as weekly it steals study time on the off weeks, and left out
    # entirely the planner schedules straight over the lab. 'weekly' | 'biweekly'.
    repeat = db.Column(db.String(16), default="weekly")
    # A date the block DOES fall on. Deliberately not "odd or even weeks":
    # whose numbering -- ISO, the term's, the department's? A student can
    # always answer "when is your next lab", and that is unambiguous.
    anchor_date = db.Column(db.String(10), default="")
    source = db.Column(db.String(20), default="manual")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class StudyPreference(db.Model):
    """When this student will actually work.

    A plan that ignores these is the thing everybody abandons: it schedules
    three hours at 7am for someone who does not exist before ten.
    """
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    earliest_min = db.Column(db.Integer, default=9 * 60)
    latest_min = db.Column(db.Integer, default=22 * 60)
    session_min = db.Column(db.Integer, default=50)
    break_min = db.Column(db.Integer, default=10)
    max_daily_min = db.Column(db.Integer, default=240)
    # Weekdays kept clear, comma-separated, 0 = Monday.
    days_off = db.Column(db.String(20), default="")
    # Minutes to leave either side of a class, for getting there and back.
    buffer_min = db.Column(db.Integer, default=15)
    # What the plan is FOR. 'all' schedules every piece of coursework, which
    # buries revision under pre-lab quizzes and milestones -- a 2% quiz took a
    # block while a 30% midterm waited. 'exams' plans only tests, midterms and
    # finals, which is what a student asking "how do I organise my studying"
    # actually means.
    focus = db.Column(db.String(20), default="all")
    # Course codes to plan for, comma-separated. Empty means all of them.
    focus_courses = db.Column(db.String(400), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StudySession(db.Model):
    """One block of the generated plan.

    Stored rather than recomputed on view so that ticking sessions off means
    something, and so the plan does not silently rearrange itself between two
    looks at the same week.
    """
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    on_date = db.Column(db.String(20), default="", index=True)
    start_min = db.Column(db.Integer, default=0)
    end_min = db.Column(db.Integer, default=0)
    course_full = db.Column(db.String(40), default="")
    title = db.Column(db.String(255), default="")
    focus = db.Column(db.String(500), default="")
    reason = db.Column(db.String(255), default="")
    task_id = db.Column(db.String(64), default="")
    item_kind = db.Column(db.String(30), default="work")
    # planned | done | skipped | missed. 'skipped' is a decision the student
    # made; 'missed' is a day that simply went by. They are different facts and
    # only one of them is worth apologising for -- neither, as it turns out.
    state = db.Column(db.String(20), default="planned")
    # How long it really took, in minutes. 0 means the student did not say, and
    # such a session is excluded from learning: recording the PLANNED length as
    # the actual one would teach the estimator its own guess back and it would
    # never improve. Nexora measured nothing about what a student actually did
    # until this existed -- `state` was written by Done/Skip and read by
    # nothing at all -- which is what blocked every "learn the student" idea.
    actual_minutes = db.Column(db.Integer, default=0)
    completed_at = db.Column(db.DateTime, nullable=True)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)


class PublicUsage(db.Model):
    """How many anonymous outline analyses have run today.

    A per-IP limit alone does not bound the bill -- a thousand people asking
    once each is a thousand analyses. This is the ceiling on the day, and it is
    in the database rather than in memory because a worker restart must not
    hand out a fresh allowance.
    """
    day = db.Column(db.String(10), primary_key=True)
    count = db.Column(db.Integer, default=0)


class HiddenCourse(db.Model):
    """A per-user tombstone so manually removed courses stay removed after sync."""
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    course_code = db.Column(db.String(40), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CanvasAccount(db.Model):
    """One Canvas connection per student.

    The token is stored encrypted rather than hashed, because unlike a password
    it has to be replayed to Canvas on every sync. It is a bearer credential for
    the student's whole Canvas account, so the database holding it must not be
    enough to use it.
    """
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    base_url = db.Column(db.String(400), nullable=False)
    token_encrypted = db.Column(db.Text, nullable=False)
    canvas_name = db.Column(db.String(255), default="")
    last_sync = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class DayNote(db.Model):
    """Something the student wants to do today, in their own words.

    Deliberately NOT a Task. A Task is a dated deadline, and the dashboard
    queue, the calendar, the .ics feed and the study planner all read Tasks --
    so "email my prof about the lab" would appear on a calendar as a deadline,
    be exported to Google, and have study hours allocated to it. Teaching each
    of those to ignore a new kind is the trap CLAUDE.md records with
    build_grouped_cards, where an unknown kind silently landed on no day.

    `day` is the student's own day, not the server's -- the same rule as every
    other date here.
    """
    __tablename__ = "day_note"
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    text = db.Column(db.String(500), nullable=False)
    day = db.Column(db.String(10), default="", index=True)
    done = db.Column(db.Boolean, default=False)
    done_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CourseColour(db.Model):
    """A colour the student chose for a course, overriding the automatic one.

    The automatic map is alphabetical over the courses with dated work, so it
    is stable but arbitrary -- and it reshuffles the moment a course gains its
    first deadline, which is exactly when a student has started thinking of
    BIOL as "the amber one". A choice made here outranks it for good.

    Stored per account rather than per browser: the mapping is how a student
    reads a month at a glance, and it must be the same on their phone. It keeps
    the INDEX, not a hex value, so every choice stays inside the palette the
    dark theme was built around and no course can be made illegible.
    """
    __tablename__ = "course_colour"
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    # 40 to match COURSE_CODE_MAX, which is defined below the models -- the same
    # literal every other course column here uses.
    course_full = db.Column(db.String(40), nullable=False, index=True)
    colour_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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
            "trial_ends_at": "TIMESTAMP NULL",
            "paid_until": "TIMESTAMP NULL",
            "referral_code": "VARCHAR(12) DEFAULT ''",
            "referred_by": "VARCHAR(12) DEFAULT ''",
            "referral_months": "INTEGER DEFAULT 0",
            "referral_credited": "BOOLEAN DEFAULT FALSE",
            "trial_notice_sent": "VARCHAR(20) DEFAULT ''",
            "timezone": "VARCHAR(64) DEFAULT ''",
            "calendar_token": "VARCHAR(64) DEFAULT ''",
            # When the automatic sync last TRIED, as opposed to last
            # succeeded. Without it a failing feed retried on every page
            # load, for ever -- see should_auto_sync.
            "last_sync_attempt": "TIMESTAMP NULL",
            # When the announcement scan last SPENT quota. The scan costs a
            # Gemini call, and a student who opens the calendar ten times a day
            # must not cost ten of them.
            "announce_scan_at": "TIMESTAMP NULL",
            "tour_done": "BOOLEAN DEFAULT FALSE",
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
            "last_sync_state": "VARCHAR(20) DEFAULT 'never'",
        },
        "task": {
            "due_time": "VARCHAR(5) DEFAULT ''",
            "location": "VARCHAR(255) DEFAULT ''",
            "notes": "TEXT DEFAULT ''",
            "source_uid": "VARCHAR(255) DEFAULT ''",
            "source_hash": "VARCHAR(64) DEFAULT ''",
            "source_updated": "VARCHAR(80) DEFAULT ''",
            "sync_state": "VARCHAR(30) DEFAULT 'active'",
            "last_seen_sync": "VARCHAR(40) DEFAULT ''",
            "completed": "BOOLEAN DEFAULT FALSE",
            # The instant the LMS gave, so a date can be re-derived without
            # going back to the university. See _recompute_task_dates.
            "source_due_at": "VARCHAR(40) DEFAULT ''",
            # Existing rows get theirs on the next sync, the same way the
            # timezone repair works. There is nothing to back-fill from.
            "source_link": "TEXT DEFAULT ''",
        },
        "course_content": {
            # Without this line the model has a column Postgres does not, and
            # EVERY query against CourseContent raises UndefinedColumn -- the
            # course workspace, the dashboard's Recent updates, the study
            # tools. `db.create_all()` creates missing TABLES and never adds a
            # column to one that already exists, which is the whole reason this
            # function exists. Existing rows default to "" and gain a real
            # extension on the next sync, the same as every other backfill here.
            "file_ext": "VARCHAR(12) DEFAULT ''",
        },
        "grade_entry": {
            "notes": "VARCHAR(255) DEFAULT ''",
        },
        "course_outline_profile": {
            "study_brief_json": "TEXT DEFAULT ''",
            "brief_built_at": "TIMESTAMP NULL",
        },
        "study_session": {
            "actual_minutes": "INTEGER DEFAULT 0",
            "completed_at": "TIMESTAMP NULL",
        },
        "study_preference": {
            "focus": "VARCHAR(20) DEFAULT 'all'",
            "focus_courses": "VARCHAR(400) DEFAULT ''",
        },
        "class_block": {
            # Biweekly labs. Existing rows default to 'weekly', which is what
            # they already meant.
            "repeat": "VARCHAR(16) DEFAULT 'weekly'",
            "anchor_date": "VARCHAR(10) DEFAULT ''",
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


def grandfather_existing_accounts():
    """Anyone already using Nexora when access control shipped keeps it.

    Showing a paywall to the people who used it while it was free -- including
    the early testers who found the bugs -- would be a poor way to say thank
    you. They get a year; new signups get the month.
    """
    stamp = datetime.utcnow() + timedelta(days=GRANDFATHER_DAYS)
    granted = 0
    for user in User.query.filter(
            User.trial_ends_at.is_(None), User.paid_until.is_(None)).all():
        user.paid_until = stamp
        granted += 1
    if granted:
        db.session.commit()
        app.logger.info("Granted %s existing account(s) %s days of access.",
                        granted, GRANDFATHER_DAYS)


with app.app_context():
    db.create_all()
    ensure_schema_columns()
    grandfather_existing_accounts()


# ============================
# Access: one place that decides
# ============================
# Every gated route asks this and nothing else. Two screens disagreeing about
# whether someone has paid is the same class of bug as two screens disagreeing
# about whether they passed -- and here it costs money in both directions.


def _referral_code_for(user):
    """A short, unambiguous code. No 0/O/1/I, because it gets read aloud."""
    if user.referral_code:
        return user.referral_code
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(12):
        code = "".join(secrets.choice(alphabet) for _ in range(7))
        if not User.query.filter_by(referral_code=code).first():
            user.referral_code = code
            db.session.commit()
            return code
    return ""          # astronomically unlikely; the caller shows nothing


def _entitlement(user, now=None):
    """Is this account active today, and why.

    `state` is one of trial / paid / expired. `active` is the only thing a
    gate should branch on.
    """
    now = now or datetime.utcnow()
    paid_until = user.paid_until
    trial_ends = user.trial_ends_at

    if paid_until and paid_until > now:
        left = (paid_until - now).days
        return {"active": True, "state": "paid", "until": paid_until.date().isoformat(),
                "days_left": left,
                "message": f"Active until {paid_until.strftime('%d %b %Y')}."}
    if trial_ends and trial_ends > now:
        left = (trial_ends - now).days
        return {"active": True, "state": "trial", "until": trial_ends.date().isoformat(),
                "days_left": left,
                "message": (f"Free month — {left} day{'' if left == 1 else 's'} left."
                            if left else "Free month — last day.")}
    ended = paid_until or trial_ends
    return {"active": False, "state": "expired",
            "until": ended.date().isoformat() if ended else None, "days_left": 0,
            "message": ("Your free month has ended. Everything you have already imported "
                        "stays here to read; syncing and the AI features need a "
                        "subscription." if not paid_until else
                        "Your subscription has ended. Everything you have already imported "
                        "stays here to read.")}


def _start_trial(user):
    """Called once, at signup. Idempotent so a re-run cannot extend anyone."""
    if user.trial_ends_at or user.paid_until:
        return
    user.trial_ends_at = datetime.utcnow() + timedelta(days=TRIAL_DAYS)


def _grant_days(user, days):
    """Add time, from a referral or a payment, on top of whatever is left.

    Extends from the later of now and the current expiry, so crediting someone
    mid-trial adds a month rather than replacing the days they had.
    """
    now = datetime.utcnow()
    base = max([d for d in (user.paid_until, user.trial_ends_at, now) if d])
    user.paid_until = base + timedelta(days=days)


def _credit_referrer(user):
    """Pay the person who referred this student, once they actually use it.

    Deliberately not on signup: "a month for every email address you can think
    of" is the whole exploit. It fires the first time the referred account does
    real work, and only ever once.
    """
    if user.referral_credited or not user.referred_by:
        return
    referrer = User.query.filter_by(referral_code=user.referred_by).first()
    user.referral_credited = True          # set either way; one shot per account
    if not referrer or referrer.id == user.id:
        db.session.commit()
        return
    if (referrer.referral_months or 0) >= REFERRAL_MONTHS_MAX:
        db.session.commit()
        return
    referrer.referral_months = (referrer.referral_months or 0) + 1
    _grant_days(referrer, REFERRAL_MONTH_DAYS)
    db.session.add(AcademicUpdate(
        id=uuid4().hex, user_id=referrer.id, course_full="",
        update_type="referral",
        title="A friend started using Nexora — a free month is on your account",
        detail=f"{REFERRAL_MONTH_DAYS} days added. "
               f"{REFERRAL_MONTHS_MAX - referrer.referral_months} left to earn."))
    db.session.commit()


def requires_active(view):
    """Gate the things that cost money or reach the outside world.

    What stays available on an expired account, on purpose: reading everything
    already imported, entering marks by hand, exporting, and deleting the
    account. Taking away work a student has already done is how you get told
    about it publicly.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        state = _entitlement(current_user)
        if state["active"]:
            return view(*args, **kwargs)
        return jsonify({
            "error": state["message"],
            "subscription_required": True,
            "state": state["state"],
            # Nothing here names a price until there is one to name.
            "price": PRICE_LABEL or None,
        }), 402
    return wrapper


@app.route("/api/plan")
@login_required
def plan_api():
    state = _entitlement(current_user)
    code = _referral_code_for(current_user)
    return jsonify({
        "ok": True, **state,
        "referral": {
            "code": code,
            "url": url_for("signup", ref=code, _external=True) if code else "",
            "earned": current_user.referral_months or 0,
            "max": REFERRAL_MONTHS_MAX,
            "days_per_referral": REFERRAL_MONTH_DAYS,
        },
        "price": PRICE_LABEL or None,
        "trial_days": TRIAL_DAYS,
    })




# ============================
# Task Load / Save helpers
# ============================
def _lms_link(url):
    """A link into the student's LMS, or "" if it cannot be trusted.

    Every one of these arrives as scraped or fed text -- the extension reads
    D2L's own item ids, Canvas hands over `html_url`, an ICS feed may carry a
    `URL:` property -- so none of it may reach an `href` unchecked:

    - Only http(s) survives. A `javascript:` URL rendered into an anchor is
      cross-site scripting on our own dashboard, delivered by the university
      page we scraped.
    - Userinfo is refused outright. `https://brightspace.example.ca@evil.tld/`
      reads as the university at a glance and resolves to evil.tld, which is
      the whole phishing trick.
    - There must be a host, so a relative or malformed URL is dropped rather
      than rendered as a link to nowhere.

    Nothing here is ever constructed by Nexora. If the source gave no link the
    answer is "", and the UI shows no button.
    """
    text = str(url or "").strip()
    if not text or len(text) > 4000:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"}:
        return ""
    if "@" in parts.netloc or not parts.hostname:
        return ""
    return _browsable_d2l(text)


# D2L's content API endpoint and D2L's own page for the same topic. Both ids come
# out of the URL the LMS itself gave us; neither is guessed.
_D2L_TOPIC_FILE = re.compile(
    r"^(https?://[^/]+)/d2l/api/le/[^/]+/(\d+)/content/topics/(\d+)/file/?$", re.I)


def _browsable_d2l(text):
    """Turn a D2L *API* content URL into the page a browser can actually open.

    The extension stored `/d2l/api/le/<ver>/<ou>/content/topics/<id>/file` as the
    item's link. That endpoint is for a fetch carrying the student's session from
    inside Brightspace; opening it as a top-level navigation answers
    `{ Errors: [ {Message: "Forbidden"} ] }` on a blank white page. Reported from
    a phone: every lecture link in Nexora did this.

    This is NOT Nexora inventing a link -- the rule that it never constructs one
    still holds. Host, org unit and topic id are all read back out of the URL the
    LMS supplied, and rewritten into D2L's own viewer route for that same topic.
    Anything that does not match is returned untouched.

    It runs inside `_lms_link`, which is the single gate every rendered link
    passes through, so rows imported before this shipped are fixed on read -- no
    re-sync and no extension update required.
    """
    m = _D2L_TOPIC_FILE.match(text)
    if not m:
        return text
    return "%s/d2l/le/content/%s/viewContent/%s/View" % (m.group(1), m.group(2), m.group(3))


# D2L's own viewer route, which is what a stale row renders as. Both ids are
# read back out of it; neither is guessed.
_D2L_VIEW_TOPIC = re.compile(
    r"^(https?://[^/]+)/d2l/le/content/(\d+)/viewContent/(\d+)/View/?$", re.I)

# What may become a download. Deliberately documents only.
#
# Measured against D2L's own `TypeIdentifier` across ten of one student's real
# courses, 210 topics: this set turned 83 File topics into downloads and
# **never once** turned a non-File into one -- 0 wrong out of 100 Links and
# ContentService topics, which is the property that matters, because
# DirectFileTopicDownload answers **403** for those and a link that 403s is
# worse than one that opens.
#
# `html` is excluded on purpose and is most of what it passes over (100 of the
# 127 File topics it declines): those are course pages -- an EHS training
# module, a welcome note -- and a student clicking one wants to read it, not
# save it. Video is excluded for the same reason: a recording is for watching.
_DOWNLOADABLE_EXT = {"pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx",
                     "zip", "txt", "csv", "rtf", "odt", "ods", "odp"}


def _d2l_download_href(url, file_ext):
    """A lecture link that saves the lecture, where the row proves it is a file.

    `_browsable_d2l` already turns D2L's content API endpoint into its viewer,
    which opens -- so the Forbidden page went away and the lecture still did not
    download. The download route exists (`DirectFileTopicDownload`, 200 with
    `Content-Disposition: attachment`) but is only valid for a FILE topic, and
    nothing on a stale row said which ones those were.

    `file_ext` is what says it. The extension has always sent it; the server
    dropped it until now. Anything without a document extension is left exactly
    as it was, so this can only ever add downloads and never break a link.
    """
    ext = str(file_ext or "").strip().lower().lstrip(".")
    if ext not in _DOWNLOADABLE_EXT:
        return url
    match = _D2L_VIEW_TOPIC.match(str(url or "").strip())
    if not match:
        return url
    return "%s/d2l/le/content/%s/topics/files/download/%s/DirectFileTopicDownload" % (
        match.group(1), match.group(2), match.group(3))


# D2L's file-UPLOAD form for an assignment folder. This is what the extension
# has always built, and it answers "Internal Error" for any folder that has
# nothing to upload to -- an on-paper or observed submission. Reported against
# a real CCDP2100C assignment due that day, titled "... (Homework)".
_D2L_SUBMIT_FOLDER = re.compile(
    r"^(https?://[^/]+)/d2l/lms/dropbox/user/folder_submit_files\.d2l\?(.+)$", re.I)


def _d2l_assignment_href(url):
    """Rewrite an assignment's upload form into the assignment itself.

    Same shape as the lecture links, one layer over: we were linking to the
    ACTION endpoint instead of the VIEW endpoint. `folder_submit_files.d2l` is
    where you upload a file, so a folder that takes no file -- on paper, or
    observed in person -- has no such page and D2L 500s.

    The target was measured, not guessed, which matters because the first guess
    was wrong. Probed against Carleton unauthenticated, where a route that
    exists redirects to login and one that does not returns the same 303 to
    /d2l/error/404 as a deliberately invented control:

        folder_submit_files.d2l   302 -> login      exists (and 500s signed in)
        folder_user_view.d2l      303 -> 404        does not exist
        folder_history.d2l        303 -> 404        does not exist
        folder.d2l                303 -> 404        does not exist
        /d2l/le/dropbox/<ou>/<db>/View              exists

    `folder_user_view.d2l` was the first proposal and would have replaced a 500
    with a 404. The surviving candidate was then confirmed by the owner against
    his own session: it opens the assignment.

    Both ids are already in the stored URL, so this repairs every row already
    in the database and needs no extension release -- the same property that
    made the `file_ext` download fix work. The extension is corrected too, for
    rows scraped after it ships.
    """
    match = _D2L_SUBMIT_FOLDER.match(str(url or "").strip())
    if not match:
        return url
    params = urllib.parse.parse_qs(match.group(2))
    folder = (params.get("db") or [""])[0].strip()
    org = (params.get("ou") or [""])[0].strip()
    # Read, never invented. Without both ids there is no link to build, and a
    # half-built one is worse than the upload form we already have.
    if not (folder.isdigit() and org.isdigit()):
        return url
    return "%s/d2l/le/dropbox/%s/%s/View" % (match.group(1), org, folder)


def content_href(url, file_ext=""):
    """The href for a CourseContent row: the LMS gate, plus our own uploads.

    The course workspace rendered `row.source_url` straight into an `href` --
    every lecture, resource, assignment and quiz on the page -- so the gate that
    exists to stop a scraped `javascript:` URL becoming cross-site scripting on
    our own page was never on that path at all. It is also why the D2L API links
    kept answering `{ Errors: [ {Message: "Forbidden"} ] }` after the rewrite
    shipped: the rewrite lives in `_lms_link`, and this page never called it.

    A file the student uploaded is stored as a relative `/static/nexora_uploads/`
    path, which `_lms_link` refuses for having no host -- correctly, since it
    cannot tell a relative LMS link from a broken one. Those are ours, served by
    us, so they are allowed through explicitly rather than by loosening the gate.
    """
    text = str(url or "").strip()
    if text.startswith("/static/nexora_uploads/"):
        return text
    # The gate runs FIRST and every rewrite second, over what the gate allowed
    # -- so a scraped `javascript:` URL can never reach a rewrite, and a rewrite
    # can only ever move a link that was already safe to render.
    safe = _d2l_assignment_href(_lms_link(text))
    return _d2l_download_href(safe, file_ext)


app.jinja_env.filters["content_href"] = content_href


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
        "due_time": t.due_time or "",
        "location": t.location or "",
        "notes": t.notes or "",
        "source_link": t.source_link or "",
    }


def load_tasks():
    if not current_user.is_authenticated:
        return []

    # 'dismissed' is a student deleting something the LMS gave them. It is a
    # state rather than a row deletion because the next sync would otherwise
    # import it again and it would look like the delete had not worked.
    tasks = Task.query.filter_by(user_id=current_user.id).filter(
        (Task.sync_state.is_(None)) | (Task.sync_state != "dismissed")).all()
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
        # utcnow, not now: every other writer of a timestamp in this app stores
        # UTC, and `local_stamp` converts on the way out. Mixing the two means a
        # sync time that is right or four hours wrong depending on which code
        # path wrote it.
        current_user.last_sync = datetime.utcnow()
    db.session.commit()


# ============================
# Dates + labels
# ============================
def local_stamp(value, fmt="%b %d at %I:%M %p", user=None):
    """A stored UTC timestamp, rendered on the student's own clock.

    Every datetime column in this app holds UTC. Templates were calling
    `.strftime()` on them directly, so a sync that happened at 10:04 PM in
    Ottawa displayed as **2:04 AM** -- next to a line claiming it was the last
    sync, which reads as the app being a day out or simply broken.

    Same fault as the deadline dates, in a place nobody thought to look: a
    derived local value was never derived. See "Every date is the student's
    day, not UTC".
    """
    if not value:
        return ""
    zone = _user_zone(user if user is not None else
                      (current_user if current_user.is_authenticated else None))
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone).strftime(fmt)


# Registered so a template can never accidentally print a raw UTC datetime
# again: `{{ row.last_sync|local_stamp }}`.
app.jinja_env.filters["local_stamp"] = local_stamp


def _term_from_dates(dates):
    """Which academic term a course's own deadlines put it in.

    Brightspace does not always state a term -- and a student who connected
    only the calendar feed has deadlines and no course rows at all -- so a
    course with work in it was landing in "Other courses". The dashboard opens
    on the CURRENT term, so that hid exactly the courses the student is taking
    right now behind a picker, while old enrolments filled the page.

    Their work says which term it is: the same season rule as
    `_current_term_label`, over the dates already held. Most common wins, and a
    tie goes to the later term -- a course straddling December and January is
    the one starting, not the one ending. Returns "" when there is nothing to
    go on, because inventing a term is worse than admitting to none.
    """
    counts = {}
    for value in dates:
        day = parse_iso_date(value)
        if not day:
            continue
        season = ("Winter" if day.month <= 4 else
                  "Summer" if day.month <= 8 else "Fall")
        key = (season, day.year)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    season, year = max(counts, key=lambda k: (counts[k], k[1],
                                              _TERM_RANK.get(k[0].lower(), 4)))
    return f"{season} {year}"


def _recompute_task_dates(user):
    """Re-derive every deadline's calendar day from the instant the LMS gave.

    This is what makes a timezone correction instant and free. Before
    `source_due_at` existed the day was the only thing stored, so a wrong zone
    left every date wrong and only a re-fetch from the university could repair
    it -- which meant telling students to go and press sync, for a mistake that
    was ours.

    Rows imported before that column existed have nothing to recompute from
    and are left alone; the next sync rewrites them.
    """
    zone = _user_zone(user)
    changed = 0
    rows = Task.query.filter_by(user_id=user.id).all()
    for row in rows:
        stamp = (row.source_due_at or "").strip()
        if not stamp:
            continue
        # A stamp stored without an offset came from a floating feed time and
        # is wall-clock; one with an offset is an instant.
        floating = "+" not in stamp[10:] and "-" not in stamp[10:] and not stamp.endswith("Z")
        fresh = local_date_iso(stamp, zone, naive_is_utc=not floating)
        fresh_time = local_time_label(stamp, zone, naive_is_utc=not floating)
        if fresh and (fresh != (row.due_raw or "") or fresh_time != (row.due_time or "")):
            row.due_raw = fresh
            row.due_time = fresh_time
            changed += 1
    if changed:
        db.session.commit()
    return changed


def _user_zone(user=None):
    """The student's timezone, falling back to the deployment's and then UTC.

    A wrong guess here dates a deadline wrongly, so there is no clever guessing:
    the browser reports its own IANA zone and that is what gets stored.
    """
    name = ""
    if user is not None:
        name = (getattr(user, "timezone", "") or "").strip()
    if not name:
        name = DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        # An unknown zone name must never take a page down; UTC is what the app
        # did everywhere before this existed.
        return timezone.utc


def _student_now(user_or_id=None):
    """The current moment on the student's own clock.

    The planner took `date.today()` -- the server's date, which on Render is
    UTC. At 8 PM in Ottawa on a Tuesday, UTC is already Wednesday: the plan
    would start tomorrow, Tuesday's evening slots were simply gone, and
    `_close_off_yesterday` marked the untouched Tuesday blocks MISSED while the
    student still had the whole evening to do them. "Every date is the
    student's day, not UTC" -- the same lesson the deadlines learnt, in the one
    feature that is entirely about which day it is.
    """
    user = user_or_id
    if user is not None and not hasattr(user, "timezone"):
        user = User.query.get(user)
    return datetime.now(_user_zone(user))


def _student_today(user_or_id=None):
    return _student_now(user_or_id).date()


def _valid_timezone(name):
    name = str(name or "").strip()[:64]
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except Exception:
        return ""
    return name


def local_date_iso(value, zone=None, naive_is_utc=True):
    """The calendar day a source timestamp falls on, in the student's zone.

    `2026-10-16T03:59:00Z` is Wednesday 15 October at 23:59 in Toronto, and
    dating it the 16th tells a student their work is due tomorrow when it is
    due tonight. A bare date with no time is already a calendar day and is
    passed straight through -- shifting it by an offset it never had would
    move every all-day event.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    cleaned = text.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(cleaned)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return m.group(1) if m else ""
    if stamp.tzinfo is None:
        if not naive_is_utc:
            # A "floating" iCalendar time carries no zone and means local
            # wall-clock. Converting it from UTC pulled an early-morning
            # deadline back a day: 00:30 became 20:30 the previous evening.
            return stamp.date().isoformat()
        # The LMS APIs send UTC, and assuming local for those would shift a
        # correct date the wrong way.
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(zone or timezone.utc).date().isoformat()


def local_time_label(value, zone=None, naive_is_utc=True):
    """The clock time a deadline falls at, in the student's zone, as HH:MM.

    Shown on the calendar so a student can check Nexora against their LMS at a
    glance. A day that agrees but a time that does not is a bug worth seeing;
    silence is what let a whole term of dates be wrong by one day.
    """
    text = str(value or "").strip()
    if not text or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        if not naive_is_utc:
            return f"{stamp.hour:02d}:{stamp.minute:02d}"
        stamp = stamp.replace(tzinfo=timezone.utc)
    local = stamp.astimezone(zone or timezone.utc)
    return f"{local.hour:02d}:{local.minute:02d}"


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


def _today_for_request():
    """Today, on the clock of whoever is asking.

    `date.today()` is the SERVER's date, and on Render the server is UTC. For a
    student in Eastern that is already tomorrow from 8pm, so between 8pm and
    midnight -- which is exactly when someone opens Nexora to decide what to do
    tonight -- every countdown was a day out: work due tonight at 11:59 computed
    to -1 and rendered **Overdue**, and "due in 2 days" read as tomorrow.

    This is the bug CLAUDE.md calls the worst this project has had, surviving in
    the one place that measures a date rather than stores one. `local_date_iso`
    fixed what day a deadline falls on; nothing fixed what day it is NOW.

    Falls back to the server's date outside a request, or for an anonymous
    page, which is what the public reader and the .ics feed want anyway.
    """
    try:
        if current_user and current_user.is_authenticated:
            return _student_today(current_user)
    except Exception:
        pass
    return date.today()


def days_until(iso, today=None):
    d = parse_iso_date(iso)
    if not d:
        return None
    return (d - (today or _today_for_request())).days


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


def _ics_floating_uids(ics_text):
    """UIDs whose DTSTART carries no zone at all.

    The `ics` library parses a floating time and labels it UTC, so after
    parsing it is indistinguishable from a real `...Z`. The raw text is the
    only place the difference survives, so it is read once, here.
    """
    floating = set()
    uid = ""
    for line in str(ics_text or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("BEGIN:VEVENT"):
            uid = ""
        elif upper.startswith("UID:"):
            uid = stripped[4:].strip()
        elif upper.startswith("DTSTART"):
            head, _, value = stripped.partition(":")
            # Zoned if it ends in Z, names a TZID, or is a date-only VALUE=DATE
            # (which is a calendar day already and needs no shifting).
            if not value.strip().upper().endswith("Z") and "TZID" not in head.upper() \
                    and "VALUE=DATE" not in head.upper():
                if uid:
                    floating.add(uid)
                else:
                    floating.add(f"__line__{stripped}")
    return floating


def _ics_all_day_uids(ics_text):
    """UIDs whose DTSTART is a bare calendar day (`DTSTART;VALUE=DATE:20261007`).

    An all-day event is a DAY, not an instant, and must never be converted. The
    `ics` library hands it back as midnight UTC, which after parsing is
    indistinguishable from a real `...T000000Z` -- so, exactly like the floating
    case, the raw text is the only place the difference survives.

    This was the Oct 7 career fair showing as **Oct 6 at 20:00**: midnight UTC on
    the 7th is 20:00 Eastern on the 6th, so one missing check produced both a day
    that was wrong and a time that was never in the feed. `_ics_floating_uids`
    already said a VALUE=DATE "is a calendar day already and needs no shifting",
    but excluding it from `floating` sent it down the branch that shifts.
    """
    all_day = set()
    uid = ""
    for line in str(ics_text or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("BEGIN:VEVENT"):
            uid = ""
        elif upper.startswith("UID:"):
            uid = stripped[4:].strip()
        elif upper.startswith("DTSTART"):
            head, _, _value = stripped.partition(":")
            if "VALUE=DATE" in head.upper():
                all_day.add(uid if uid else f"__line__{stripped}")
    return all_day


def _event_source_stamp(event, floating=False):
    """The instant the feed gave, verbatim, for later re-derivation."""
    for attr in ("begin", "end"):
        try:
            moment = getattr(event, attr, None)
            if moment is None:
                continue
            stamp = getattr(moment, "datetime", None) or moment
            if isinstance(stamp, datetime):
                if floating:
                    # Store it without an offset, so re-deriving it later knows
                    # it was wall-clock rather than UTC.
                    return stamp.replace(tzinfo=None).isoformat()[:40]
                return stamp.isoformat()[:40]
        except Exception:
            continue
    return ""


def get_event_date_iso(event, zone=None, floating=False, all_day=False):
    """The calendar day this event falls on, in the student's timezone.

    `event.begin.date()` is the UTC day, and a Brightspace deadline of 11:59 PM
    Eastern is 03:59Z the next morning -- so every late-evening deadline, which
    is most of them, was dated a day late. A student reading that believes the
    work is due tomorrow when it is due tonight.
    """
    zone = zone or timezone.utc
    for attr in ("begin", "end"):
        try:
            moment = getattr(event, attr, None)
            if moment is None:
                continue
            stamp = getattr(moment, "datetime", None) or moment
            if isinstance(stamp, datetime):
                if floating or all_day:
                    # Wall-clock, or a bare calendar day: the day is what the
                    # feed wrote, unshifted. Applying an offset an all-day event
                    # never had is how Oct 7 became Oct 6.
                    return stamp.replace(tzinfo=None).date().isoformat()
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return stamp.astimezone(zone).date().isoformat()
            d = moment.date() if hasattr(moment, "date") else None
            if isinstance(d, date):
                return d.isoformat()
        except Exception:
            continue

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
def build_grouped_cards(raw_tasks, completed=False):
    """Group task rows into cards. `completed=True` builds the OTHER list --
    the things a student has ticked off -- which used to exist nowhere: a
    completed task was dropped before any view was built, a sync never
    un-completes one, and the undo toast lasts nine seconds. One wrong click
    after that and the deadline was gone from Nexora for good."""
    grouped = {}

    for t in raw_tasks:
        if t.get("kind") == "junk" or bool(t.get("completed")) != completed:
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
                # What a calendar entry needs to be editable: which rows it is
                # made of, and the detail a student typed onto it.
                "task_ids": [],
                "kind": t.get("kind", "due"),
                "due_time": "",
                "location": "",
                "notes": "",
                "source_link": "",
            }

        g = grouped[gid]
        k = t.get("kind", "general")
        d = t.get("due_raw", "")
        g["task_ids"].append(t.get("id", ""))
        if t.get("due_time") and not g["due_time"]: g["due_time"] = t["due_time"]
        if t.get("location") and not g["location"]: g["location"] = t["location"]
        if t.get("notes") and not g["notes"]: g["notes"] = t["notes"]
        # A group is one card built from several rows -- opens, due, closes --
        # and only some of them carry the link. First one wins.
        if t.get("source_link") and not g["source_link"]: g["source_link"] = t["source_link"]

        if k == "opens" and d:
            if (not g["opens_raw"]) or (d < g["opens_raw"]):
                g["opens_raw"] = d
        elif k == "closes" and d:
            if (not g["closes_raw"]) or (d < g["closes_raw"]):
                g["closes_raw"] = d
        elif d:
            # 'due' and anything the student created ('exam', 'event'). A kind
            # that fell through here used to leave main_due_raw empty, so a
            # midterm someone added by hand never landed on a day at all.
            g["kind"] = k
            if (not g["due_raw"]) or (d < g["due_raw"]):
                g["due_raw"] = d

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
def decorate_card(c):
    """Add the display fields every template reads off a card.

    This used to live inside group_for_ui, so only cards that went through the
    bucketing had status_label/main_due_pretty. The dashboard builds a second,
    unbucketed list for its course cards and deadline rail, which is why every
    course read "No date" and every deadline pill was empty regardless of the
    dates actually in the database."""
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

    return c2


def group_for_ui(cards):
    groups = {"overdue": [], "today": [], "panic": [], "soon": [], "later": [], "nodate": []}

    for c in cards:
        c2 = decorate_card(c)
        groups[c2["status"]].append(c2)

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

    feed_zone = _user_zone(current_user)
    floating_uids = _ics_floating_uids(ics_text)
    all_day_uids = _ics_all_day_uids(ics_text)
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

        uid_text = getattr(event, "uid", "")
        is_floating = bool(uid_text and uid_text in floating_uids)
        is_all_day = bool(uid_text and uid_text in all_day_uids)
        due_raw = get_event_date_iso(event, feed_zone, floating=is_floating,
                                     all_day=is_all_day)
        source_stamp = _event_source_stamp(event, floating=is_floating or is_all_day)
        # An all-day event has no clock time. Deriving one from midnight gave
        # "20:00" on a career fair the feed only ever stated a DAY for, which
        # reads as a real start time and is not one.
        due_time_label = ("" if is_all_day else
                          local_time_label(source_stamp, feed_zone,
                                           naive_is_utc=not is_floating))
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
        # Brightspace does not always write one, and a feed that omits it is
        # not a fault -- the card simply gets no link.
        event_link = _lms_link(getattr(event, "url", ""))
        seen_uids.add(source_uid)

        task = existing_by_uid.get(source_uid)
        if task and task.sync_state == "dismissed":
            # Deleted on purpose. Re-importing it would undo the student's
            # decision every time the feed refreshed.
            counts["unchanged"] += 1
            continue
        if task:
            changed = task.source_hash != source_hash or task.sync_state != "active"
            task.group_id = gid
            task.title = title
            task.base_title = base
            task.course_full = course_full
            task.course_prefix = cprefix
            task.due_raw = due_raw
            task.source_due_at = source_stamp
            # The clock time, so the student can check Nexora against their LMS
            # rather than taking the day on trust.
            task.due_time = due_time_label
            task.kind = kind
            task.is_deadline = True
            task.source_hash = source_hash
            task.source_updated = source_updated
            task.source_link = event_link
            task.sync_state = "active"
            task.last_seen_sync = now_text
            counts["updated" if changed else "unchanged"] += 1
        else:
            task = Task(
                id=uuid4().hex, user_id=current_user.id, group_id=gid, title=title,
                base_title=base, course_full=course_full, course_prefix=cprefix,
                due_raw=due_raw, source_due_at=source_stamp,
                due_time=due_time_label,
                kind=kind, is_deadline=True, source="ics", source_link=event_link,
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
        data = resp.read()

    text_data = data.decode("utf-8", errors="ignore")
    if "BEGIN:VCALENDAR" not in text_data:
        raise ValueError("Brightspace did not return a valid calendar feed.")
    return text_data


def should_auto_sync():
    """Whether to try the calendar feed on this page load.

    Gated on the last ATTEMPT, not the last success. It used to read
    `last_sync`, which `save_sync_result` only advances when the sync works --
    so a feed answering 403, which is what an expired Brightspace token does,
    retried on EVERY page load for ever. Every dashboard render then waited on
    a network call that could not succeed, and the university was hit once per
    page view. Seen in the local log: seven failures in two minutes of
    ordinary browsing.

    `utcnow`, not `now`: `last_sync` is written with `datetime.utcnow()`, and
    comparing it against local time is harmless only on a server that is
    already UTC. The same mistake was found and fixed in the writer; it was
    still here in the reader.
    """
    if not getattr(current_user, "is_authenticated", False):
        return False
    last_try = getattr(current_user, "last_sync_attempt", None) or load_last_sync()
    return last_try is None or datetime.utcnow() - last_try >= timedelta(minutes=AUTO_SYNC_MINUTES)


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
    """Run the feed sync if one is due, and say which of the three happened.

    `ran` and `ok` are separate on purpose. This returned a bare `None` for
    both "nothing was due" and "it tried and failed", which is the mistake
    `_evaluate_pass_rules` is a whole section of CLAUDE.md about: an answer
    that cannot be given and a bad answer are not the same fact. Here it means
    a student whose Brightspace token has expired cannot be told apart from one
    who simply synced ten minutes ago -- and the first of those needs telling.
    """
    if not load_feed_url() or not should_auto_sync():
        return {"ran": False, "ok": True, "counts": {}}
    # Stamped BEFORE the attempt and committed now, so a feed that times out
    # still backs off -- the except below may never be reached.
    current_user.last_sync_attempt = datetime.utcnow()
    db.session.commit()
    try:
        counts, _ = perform_feed_sync()
        return {"ran": True, "ok": True, "counts": counts}
    except Exception as exc:
        app.logger.warning("Automatic Brightspace sync failed: %s", exc)
        save_sync_result("error", f"Automatic sync failed: {exc}", successful=False)
        return {"ran": True, "ok": False, "counts": {}}


# ============================
# ROUTES
# ============================
@app.route("/")
def landing():
    # The page quotes the trial length and the corpus size. Both are facts that
    # move, and this is the most-read public page on the site, so it must never
    # carry a hardcoded number that has quietly stopped being true. "Free while
    # you study. No card." shipped for one deploy after the trial went live,
    # which reads as a promise of permanent free access.
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
    # Ordered by MEASURED availability on the current key, newest first among
    # equals -- not by version number alone. Probed 2026-09-05, three passes:
    # 3.7-flash answered every time in about a second; 3.6-flash, which used to
    # lead this list, answered 429 on two of three; 3.8-flash answered 503 on
    # every attempt after the first and cost 6-12s each time before falling
    # through. Both Pro candidates 429 on every call, so the reasoning chain
    # lands here in practice, and whichever model leads this list is the one
    # that reads a student's outline.
    #
    # That mattered more than it looks: with 3.6 quota-exhausted the chain fell
    # all the way to gemini-3.5-flash-lite, and flash-lite is the model that
    # drops selection_rules on a multi-option outline (see the corpus note in
    # CLAUDE.md). The eval was failing on the weakest model in the list purely
    # because the two above it were unavailable.
    # Re-probed 2026-10-09 -- three text passes and two vision passes, on the
    # live key, against a real screenshot. The September order had
    # gemini-3.7-flash leading, and it now answers NOTHING:
    #
    #   model                     text (3)        vision (2)   fastest OK
    #   gemini-3.7-flash          timeout,503,503 timeout,503  never
    #   gemini-3.8-flash          503,200,503     timeout,x2   4.0s text only
    #   gemini-3.6-flash          200,200,503     503,200      1.1s / 9.1s
    #   gemini-3.5-flash-lite     200,200,200     200,200      0.4s / 1.8s
    #   gemini-flash-lite-latest  200,200,200     200,200      0.5s / 2.5s
    #   gemini-flash-latest       200,200,503     200,200      4.0s / 13.2s
    #   gemini-3.1-flash-lite     503,200,200     200,200      6.4s / 9.4s
    #
    # So the two models at the front of the chain were costing every call the
    # price of a timeout and a 503 before anything answered -- about seventy
    # seconds on the vision path. They are now last: kept, because availability
    # moves and they may come back, but never paid for first.
    #
    # Capability still leads where it survives. 3.6-flash answers two text calls
    # in three and is the strongest that does, so it stays in front of
    # flash-lite, which is the model documented as dropping selection_rules on a
    # multi-option outline. Ordering purely by uptime would put the weakest model
    # first on the one task where reasoning decides a student's grade.
    defaults = [
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
    ]
    if vision:
        # 3.8-flash timed out on BOTH vision passes while answering text once in
        # three, so one order cannot serve both. Reading an image is closer to
        # transcription than reasoning, and flash-lite transcribed a real
        # timetable completely in 1.8s, so it sits second rather than last.
        defaults = [
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-flash-latest",
            "gemini-3.1-flash-lite",
            "gemini-3.8-flash",
            "gemini-3.7-flash",
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
# 503 is "overloaded", which is usually transient but can persist for a while:
# gemini-3.8-flash answered 503 on every probe, and each attempt burned 6-12
# seconds before the chain moved on. Retrying it once is right; retrying it on
# every call for the next minute is 20 wasted seconds per analysis on a path
# that already takes 37. Much shorter than the quota cooldown, because unlike
# quota this really does clear on its own.
_OVERLOADED_COOLDOWN_SECONDS = 120
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


# Model orders for work that is not the outline pipeline. Measured 2026-09-14
# on a 1,500-word lecture summary, live key:
#
#   the default chain, as a student experiences it     46.8s
#   gemini-3.7-flash (first in that chain)              503, then a retry, then parked
#   gemini-3.6-flash, default thinking                  10.8s  -- 1,241 tokens of thinking
#   gemini-3.6-flash, thinkingLevel "low"                5.0s  -- same eight concepts
#   gemini-3.5-flash-lite                                2.5s  -- same eight concepts
#
# Thinking is the right tool for reading a grading policy and the wrong one for
# "summarise this lecture": it bought nothing visible and cost most of the
# wait. So a summary, a set of flashcards or a tutor answer starts from the
# models that answer in seconds, and the outline pipeline keeps the reasoning
# chain it was built on. `_usable_gemini_models` still skips anything parked.
_FAST_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-flash-lite-latest",
                "gemini-3.7-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]
_QUICK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash",
                 "gemini-flash-lite-latest", "gemini-flash-latest"]
# Only the models that accept it. Sending thinkingConfig to one that does not
# is a 400, which reads as the model failing and burns a retry.
_THINKING_LEVEL_MODELS = re.compile(r"^gemini-3\.\d+-flash$")


def _gemini_json(system_prompt, user_prompt, max_tokens=4096, temperature=0.2, reasoning=False, used_model=None, mode=None):
    """Call Gemini for strict JSON with retries and a model fallback on transient 5xx errors.

    `mode` is None for the outline pipeline and anything else that needs the
    reasoning chain; "fast" starts from flash-lite (summaries, flashcards,
    quizzes -- 2-3s); "quick" starts from 3.6-flash with low thinking (the
    tutor -- ~5s, a little more reasoning). See _FAST_MODELS.
    """
    api_key = (os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip())
    if not api_key:
        raise RuntimeError("Gemini is not configured")
    if mode == "fast":
        models = _usable_gemini_models(list(_FAST_MODELS))
    elif mode == "quick":
        models = _usable_gemini_models(list(_QUICK_MODELS))
    else:
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
                if mode and _THINKING_LEVEL_MODELS.match(model):
                    body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
                else:
                    body["generationConfig"].pop("thinkingConfig", None)
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
                if exc.code in (500, 502, 503, 504):
                    # Retried and still overloaded. Park it briefly so the next
                    # request does not pay the same wait to learn the same
                    # thing; _usable_gemini_models un-parks everything if the
                    # whole list ends up cold, so this can never be the reason
                    # a student gets nothing.
                    _mark_gemini_model_unavailable(model, _OVERLOADED_COOLDOWN_SECONDS)
                    break
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
    # A stretch of a sentence that contains NO percentage. The gap between a
    # threshold and the word "final" must not be allowed to cross another
    # threshold: "at least 50% overall AND at least 40% on the final
    # examination" was read as a 50% final minimum, because the plain gap
    # matched from the FIRST "at least" and skipped straight over the second.
    # A student at 45% on the final would have been told they failed.
    NO_PCT=r'(?:(?!\d{1,3}(?:\.\d+)?\s*%)[^.\n])'
    # Final/component minimum rules in both word orders.
    rule_specs=[
        ('Final Exam',[
            r'(?:must|required\s+to)\s+(?:pass|obtain|earn|achieve)'+NO_PCT+r'{0,80}?final(?:\s+exam|\s+examination)?'+NO_PCT+r'{0,50}?(\d{1,3}(?:\.\d+)?)\s*%',
            r'(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%'+NO_PCT+r'{0,90}?final(?:\s+exam|\s+examination)',
            r'final(?:\s+exam|\s+examination)'+NO_PCT+r'{0,60}?(?:at\s+least|minimum(?:\s+of)?)\s*(\d{1,3}(?:\.\d+)?)\s*%',
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



def _gemini_image_to_outline_text(raw, mime_type, prompt=None, models=None):
    """Read a grading screenshot with Gemini Vision.

    This uses the REST API's canonical camelCase fields (inlineData / mimeType).  The
    previous implementation used snake_case protobuf names, which caused PNG/JPG
    uploads to fail on the live Gemini endpoint even though text-only calls worked.
    """
    api_key=(os.environ.get('GEMINI_API_KEY','').strip() or os.environ.get('GOOGLE_API_KEY','').strip())
    if not api_key:
        raise RuntimeError('Gemini is not configured for screenshot reading.')

    # The default chain is ordered for reading a course OUTLINE -- dense prose
    # with grading rules, where flash-lite is documented as the model that drops
    # things. A caller reading something simpler may hand in its own order.
    models=models or _gemini_model_candidates(vision=True)

    # The default asks for an outline; an exam timetable is a different document
    # with the same need -- transcribe, do not summarise. The model chain, the
    # retries and the timeout ceiling are the valuable part and are shared.
    prompt=prompt or (
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
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        for index, page in enumerate(reader.pages[:250], 1):
            extracted = (page.extract_text() or '').strip()
            if extracted:
                text_parts.append(f"[Page {index}]\n{extracted}")
    elif suffix in {'.pptx', '.ppt'}:
        if suffix == '.ppt':
            raise ValueError('Legacy .ppt files must be converted to .pptx by Brightspace or the instructor.')
        from pptx import Presentation
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
        from docx import Document
        doc = Document(io.BytesIO(raw))
        text_parts.extend(p.text.strip() for p in doc.paragraphs if p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                text_parts.append(' | '.join(cell.text.strip() for cell in row.cells))
    elif suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        ocr=''
        local_error=None
        vision_bytes, vision_mime = raw, {'.png':'image/png','.jpg':'image/jpeg',
                                          '.jpeg':'image/jpeg','.webp':'image/webp'}[suffix]

        # Prepare the image once, whatever reads it afterwards. This used to sit
        # behind "pytesseract is importable", so a deployment without the
        # Tesseract binary did the work and threw it away, and one without the
        # package skipped scaling altogether.
        if Image is not None:
            try:
                image=Image.open(io.BytesIO(raw))
                image=ImageOps.exif_transpose(image)
                if image.mode not in ('RGB', 'L'):   # palette and RGBA resample badly
                    image=image.convert('RGB')
                longest=max(image.size)
                if longest > OCR_MAX_EDGE:
                    scale=OCR_MAX_EDGE/longest
                    image=image.resize((max(1,int(image.width*scale)),
                                        max(1,int(image.height*scale))), Image.LANCZOS)
                elif longest < OCR_MIN_EDGE:
                    scale=OCR_MIN_EDGE/longest
                    image=image.resize((int(image.width*scale),int(image.height*scale)))

                # Vision gets this, not the original. The raw file can be many
                # megabytes, and base64 adds a third on top of that for one POST.
                shrunk=io.BytesIO()
                image.convert('RGB').save(shrunk,'JPEG',quality=82,optimize=True)
                if shrunk.tell() < len(raw):
                    vision_bytes, vision_mime = shrunk.getvalue(), 'image/jpeg'

                gray=ImageOps.grayscale(image)
                image=None
                gray=ImageEnhance.Contrast(gray).enhance(1.7)
                gray=gray.filter(ImageFilter.SHARPEN)
            except Exception as exc:
                gray=None
                local_error=exc
        else:
            gray=None

        if gray is not None and pytesseract is not None:
            try:
                ocr=pytesseract.image_to_string(gray, config='--oem 3 --psm 3')
                if len(ocr.strip()) < 80 or len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)) < 2:
                    alternate=pytesseract.image_to_string(gray, config='--oem 3 --psm 11')
                    if len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',alternate)) > len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)):
                        ocr=alternate
            except Exception as exc:
                local_error=exc
        gray=None
        # Render deployments may not include the Tesseract system binary. Gemini Vision is
        # therefore a real fallback, not a fake success path.
        if len(ocr.strip()) < 80 or len(re.findall(r'\d{1,3}(?:\.\d+)?\s*%',ocr)) < 2:
            mime=vision_mime
            try:
                ocr=_gemini_image_to_outline_text(vision_bytes,mime)
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


def _copilot_course(prompt, history, user_id):
    """Which course the student means: the workspace prefix, a code in the
    message, then a code anywhere in the recent conversation."""
    known = _known_course_codes(user_id)
    def _find(text):
        for m in re.finditer(r"\b([A-Za-z]{2,5})\s*[- ]?\s*(\d{3,4}[A-Za-z]?)\b", text or ""):
            code = _normalize_course_code(f"{m.group(1)} {m.group(2)}")
            if code in known:
                return code
        return ""
    m = re.match(r"\s*Course:\s*([A-Za-z0-9 -]{4,20})\.", prompt or "")
    if m and _normalize_course_code(m.group(1)) in known:
        return _normalize_course_code(m.group(1))
    found = _find(prompt)
    if found:
        return found
    for item in reversed(history or []):
        if isinstance(item, dict):
            found = _find(str(item.get("content") or ""))
            if found:
                return found
    return ""


def _copilot_amend_outline(user_id, prompt, history, images):
    """The tutor's side of "add this to the outline": find the text, find the
    course, amend, and say what changed."""
    course = _copilot_course(prompt, history, user_id)
    question = re.sub(r"^\s*Course:\s*[A-Za-z0-9 -]{4,20}\.\s*(?:Student question:\s*)?", "", prompt or "", flags=re.I)
    # The text to add: what came with the message, a screenshot attached to
    # it, or -- "add it" -- what the student pasted a turn earlier.
    without_command = _AMEND_INTENT.sub("", question).strip(" .:\n")
    text, label = "", "Added by the student"
    if len(without_command) >= 40:
        text = without_command
    elif images:
        parts = []
        for image in images[:4]:
            try:
                parts.append(_gemini_image_to_outline_text(base64.b64decode(image["data"]), image["mime_type"]))
            except Exception as exc:
                app.logger.warning("Amendment screenshot read failed: %s", exc)
        text, label = "\n".join(p for p in parts if p).strip(), "Added by the student from a screenshot"
    else:
        turns = [t for t in (history or []) if isinstance(t, dict) and str(t.get("content") or "").strip()]
        for turn in reversed(turns):
            content = str(turn.get("content") or "").strip()
            if turn.get("role") == "user" and len(content) >= 40 and not _wants_outline_amendment(content) \
                    and not content.lower().startswith("explain the attached image"):
                text = content
                break
            if turn.get("role") == "assistant" and len(content) >= 40:
                # The student showed a screenshot and the tutor read it back;
                # that reading is the only copy of the text we have.
                text, label = content, "Added by the student, as the tutor read it from a screenshot"
                break
    if not course:
        return {"answer": "Which course is this for? Say the course code with it -- for example \"add this to the BIOL 1103 outline\".",
                "grounded": True, "sources": []}
    if not text:
        return {"answer": f"Paste the text you want added to the {course} outline (or attach the screenshot) and say \"add this to the outline\".",
                "grounded": True, "sources": []}
    try:
        result = _amend_course_outline(user_id, course, text, label)
    except ValueError as exc:
        return {"answer": str(exc), "grounded": True, "sources": []}
    link = url_for("grade_calculator", course=course)
    if result["changed"]:
        lines = "\n".join(f"- {c}" for c in result["changed"])
        answer = (f"Done — added to the **{course}** grading policy on file:\n\n{lines}\n\n"
                  f"{result['summary']}\n\n"
                  f"Check it in the [grade calculator]({link}); a bonus is additive, so it only ever adds to your mark.")
    else:
        answer = (f"I have kept that with the **{course}** outline, but nothing in it changes the grading scheme "
                  f"({result['summary'] or 'no weights, bonuses or minimums are stated'}). "
                  f"If it should have, tell me what the text says in one sentence -- for example \"a 1% bonus on the lab grade for completing 6 of 8 quizzes\".")
    return {"answer": answer, "grounded": True, "sources": [], "outline_changed": result["changed"], "course": course}


@app.route("/api/copilot", methods=["POST"])
@ai_limit
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

    # "Add this to the course outline" is an instruction, not a question. It
    # used to reach the model, which answered that it had no access to edit
    # the outline. It does now: the text is read, the scheme on file is
    # amended, and the reply says exactly what changed. No chat call is made.
    if _wants_outline_amendment(prompt):
        return jsonify(_copilot_amend_outline(current_user.id, prompt, history, validated_images))

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

    # The chain, not one model. This called gemini-3.6-flash alone, with full
    # thinking, and on a 429 slept and retried the SAME model three times --
    # 4.5s of sleeping and then an error, on a key where 3.6 answers 429 two
    # calls in three. A tutor that fails or takes forty seconds is one nobody
    # asks twice. GEMINI_MODEL, if set, still goes first.
    preferred = os.environ.get("GEMINI_MODEL", "").strip()
    models = _usable_gemini_models(([preferred] if preferred else []) + [m for m in _QUICK_MODELS if m != preferred])
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

    def request_for(model):
        config = {
            "temperature": 0.7,
            "maxOutputTokens": 8192 if lecture_intent else 2200,
        }
        # Low thinking: measured at half the latency of the default on an
        # explanation task with the same coverage. Full thinking is for the
        # outline pipeline, where the reasoning is the product.
        if _THINKING_LEVEL_MODELS.match(model):
            config["thinkingConfig"] = {"thinkingLevel": "low"}
        body = json.dumps({
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": contents,
            "generationConfig": config,
        }).encode("utf-8")
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model, safe='')}:generateContent?key="
            f"{urllib.parse.quote(api_key, safe='')}"
        )
        return urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        result = None
        last_http_error = None
        for model in models:
            try:
                with urllib.request.urlopen(request_for(model), timeout=90) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as retry_exc:
                last_http_error = retry_exc
                if retry_exc.code in (429, 500, 502, 503, 504):
                    # Park it the way _gemini_json does and move straight on;
                    # the next model answers in the time a sleep would have cost.
                    _mark_gemini_model_unavailable(
                        model, _QUOTA_COOLDOWN_SECONDS if retry_exc.code == 429 else _OVERLOADED_COOLDOWN_SECONDS)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                _mark_gemini_model_unavailable(model, _OVERLOADED_COOLDOWN_SECONDS)
                continue
        if result is None and last_http_error:
            raise last_http_error
        if result is None:
            raise urllib.error.URLError("every model timed out")

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
    if not user:
        # The REPLY stays identical -- saying "no such account" turns this form
        # into a way to check who has one. But the operator needs to be able to
        # tell "no account" from "the mail failed", and from the outside those
        # look the same: a 200 and nothing in the inbox. This took a round trip
        # through the logs to work out the first time it happened.
        app.logger.info("Password reset asked for an address with no account; nothing sent.")
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


@app.route("/api/password", methods=["POST"])
@limiter.limit("10 per hour; 30 per day")
@login_required
def change_password():
    """Change your own password while signed in.

    The profile page said this was "not built yet" and offered /forgot-password
    instead -- which redirects a signed-in user straight to the dashboard, so
    the one link on the one page that mentioned passwords could never work for
    the only people who could see it. A student who simply wanted to change
    their password had no way to, and the fallback was to log out and claim to
    be locked out.

    The current password is required. Without it, anyone who finds an unlocked
    laptop owns the account permanently rather than until it is locked again.
    """
    data = request.get_json(silent=True) or {}
    current = str(data.get("current") or "")
    new_password = str(data.get("new") or "")

    if not check_password_hash(current_user.password_hash, current):
        return jsonify({"error": "That is not your current password."}), 400
    problem = _password_problem(new_password)
    if problem:
        return jsonify({"error": problem}), 400
    if check_password_hash(current_user.password_hash, new_password):
        return jsonify({"error": "That is the password you already have."}), 400

    current_user.password_hash = generate_password_hash(new_password)
    # Any outstanding reset link is stale the moment the password changes --
    # otherwise an old email in an inbox still opens the account.
    PasswordResetToken.query.filter_by(user_id=current_user.id, used_at=None).delete()
    db.session.commit()
    return jsonify({"ok": True, "message": "Password changed."})


@app.route("/api/password/email-link", methods=["POST"])
@limiter.limit("5 per hour; 15 per day")
@login_required
def email_my_reset_link():
    """Send a reset link to your own address while signed in.

    /forgot-password exists for someone who cannot get in, and it redirects an
    authenticated user to the dashboard -- which is right for that route and
    meant a signed-in student had no way to ask for the email at all. They can
    change it on the profile directly, but some people would rather do it from
    a link, and taking that away was a loss.

    It goes to the address on the account and nowhere else: a field here would
    be a way to send Nexora-branded mail to anyone.
    """
    if not email_is_configured():
        return jsonify({"error": "Email is not set up yet, so Nexora cannot send "
                                 "the link. Change your password here instead."}), 503

    PasswordResetToken.query.filter_by(user_id=current_user.id, used_at=None).delete()
    token = secrets.token_urlsafe(32)
    db.session.add(PasswordResetToken(
        id=uuid4().hex, user_id=current_user.id, token_hash=_hash_reset_token(token),
        expires_at=datetime.utcnow() + PASSWORD_RESET_TTL))
    db.session.commit()
    link = url_for("reset_password", token=token, _external=True)
    sent = send_email(
        current_user.email,
        "Reset your Nexora password",
        f"Hi {current_user.name},\n\n"
        f"Use this link to choose a new password:\n{link}\n\n"
        "The link works once and expires in one hour.\n\n"
        "If you did not ask for this, you can ignore this email -- your password "
        "has not changed.\n",
    )
    if not sent:
        # Signed in, so there is no account to hide: say what happened instead
        # of the deliberately vague answer /forgot-password has to give.
        return jsonify({"error": "Nexora could not send that email. Check the mail "
                                 "settings, or change your password here instead."}), 502
    return jsonify({"ok": True,
                    "message": f"Sent to {current_user.email}. The link expires in an hour."})


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

    if check_password_hash(user.password_hash, password):
        # The reason that matters here is the student who reset because they
        # think someone else got in. Letting them re-set the same password ends
        # that flow looking successful while changing nothing. For the ordinary
        # case -- forgot it, remembered halfway through -- knowing they guessed
        # right is more useful than a silent no-op, so say that instead.
        return render_template(
            "reset_password.html", token=token, invalid=False,
            error="That is already your password. Choose a different one, or go back and log in with it.")

    user.password_hash = generate_password_hash(password)
    row.used_at = datetime.utcnow()
    # Any other outstanding link for this account is now stale.
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).delete()
    db.session.commit()
    session.permanent = True
    login_user(user, remember=True)
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
        # A referral code arrives in the URL, so it is checked against a real
        # account before it is kept -- and self-referral is refused here rather
        # than being caught later when the credit is paid.
        referred = (request.form.get("ref") or request.args.get("ref") or "").strip().upper()[:12]
        if referred:
            referrer = User.query.filter_by(referral_code=referred).first()
            if referrer and referrer.email != email:
                user.referred_by = referred
        _start_trial(user)

        db.session.add(user)
        db.session.commit()

        session.permanent = True
        login_user(user, remember=True)
        # Straight to the one question that makes Nexora work. A new account
        # landing on an empty dashboard has to go and find "Brightspace" in
        # the rail on its own, which was being covered by telling people in
        # person. /start sends an already-connected account on to the
        # dashboard, so this can never strand anyone.
        return redirect(url_for("start_here"))

    return render_template("signup.html", ref=(request.args.get("ref") or "").strip().upper()[:12])


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

        session.permanent = True
        login_user(user, remember=True)
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
    # Shown only if one has already been issued: the page should not mint a
    # secret URL for a student who never asked for one.
    token=(current_user.calendar_token or "").strip()
    return render_template("settings.html", user=current_user, grading_scale=grading_scale,
                           timezones=COMMON_TIMEZONES,
                           current_timezone=(current_user.timezone or "").strip(),
                           calendar_feed_url=url_for("calendar_feed", token=token, _external=True) if token else "")

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
@ai_limit
@login_required
@requires_active
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
    if (current_user.email or "").lower() != ADMIN_EMAIL:
        return redirect(url_for("home"))

    feedback_messages = Feedback.query.order_by(Feedback.id.desc()).all()

    return render_template(
        "admin_feedback.html",
        user=current_user,
        feedback_messages=feedback_messages
    )

# ============================
# Taking your data out, and taking your account down
# ============================
# The privacy policy already promised deletion -- by emailing a human, with no
# SMTP configured behind it. That is not a promise a paid product can keep, and
# "email us and we will get to it" is not something to ask money for. Both are
# self-service now.

_USER_TABLES = [
    ("Task", "user_id"), ("CourseContent", "user_id"), ("GradeEntry", "user_id"),
    ("CourseOutlineProfile", "user_id"), ("StudyArtifact", "user_id"),
    ("SyncedFile", "user_id"), ("SyncedCourse", "user_id"), ("HiddenCourse", "user_id"),
    ("CanvasAccount", "user_id"), ("PasswordResetToken", "user_id"),
    ("UserGradingScale", "user_id"), ("AcademicUpdate", "user_id"),
    ("ClassBlock", "user_id"), ("StudyPreference", "user_id"),
    ("StudySession", "user_id"), ("Feedback", "user_id"),
]


@app.route("/api/account/export")
@login_required
def account_export():
    """Everything Nexora holds about this student, as one JSON file.

    Built by walking the models rather than hand-listing columns, so a table
    added later is included instead of being quietly left out of an export
    somebody is relying on.
    """
    user_id = current_user.id
    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": {"name": current_user.name, "email": current_user.email,
                    "joined": None},
        "data": {},
    }
    for model_name, column in _USER_TABLES:
        model = globals().get(model_name)
        if model is None:
            continue
        rows = model.query.filter_by(**{column: user_id}).all()
        out = []
        for row in rows:
            item = {}
            for field in model.__table__.columns.keys():
                value = getattr(row, field, None)
                if isinstance(value, (datetime, date)):
                    value = value.isoformat()
                item[field] = value
            out.append(item)
        payload["data"][model_name] = out

    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    stamp = date.today().isoformat()
    return Response(body, content_type="application/json; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="nexora-export-{stamp}.json"',
    })


@app.route("/api/account", methods=["DELETE"])
@limiter.limit("6 per hour")
@login_required
def account_delete():
    """Delete the account and everything attached to it.

    The password is required: this is irreversible, and a session left open on
    a shared machine must not be enough to wipe someone's term.
    """
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if not password or not check_password_hash(current_user.password_hash, password):
        return jsonify({"error": "That password is not right, so nothing was deleted."}), 403
    if str(data.get("confirm") or "").strip().upper() != "DELETE":
        return jsonify({"error": "Type DELETE to confirm."}), 400

    user_id = current_user.id
    email = current_user.email
    for model_name, column in _USER_TABLES:
        model = globals().get(model_name)
        if model is None:
            continue
        model.query.filter_by(**{column: user_id}).delete(synchronize_session=False)
    db.session.flush()
    user = User.query.get(user_id)
    if user:
        db.session.delete(user)
    db.session.commit()
    logout_user()
    app.logger.info("Account deleted on request: user %s", user_id)
    return jsonify({"ok": True, "deleted": email})


@app.route("/logout", methods=["POST", "GET"])
def logout():
    """POST for real; GET only to keep an old bookmark working.

    As a GET this was a one-tag attack: any page anywhere could sign a student
    out with <img src="https://nexora/logout">. Harmless on its own, and
    exactly the kind of thing that should not be true of a paid product. The
    templates all POST now; the GET branch survives so a bookmarked link or a
    stale tab still lands somewhere sensible rather than on an error.
    """
    logout_user()
    return redirect(url_for("landing"))


# Content that is news when it is published, not when it is imported. An
# assignment's published_at is its DUE date in the extension, so those are
# left to the deadline lists; here it is what a student would read.
_UPDATE_CONTENT_TYPES = ("announcement", "lecture", "page", "resource", "practice")


def _recent_updates(user, limit=4, within_days=None):
    """The newest things Brightspace published in this student's courses,
    dated on the student's own day and each carrying the link Brightspace
    gave, checked by _lms_link. Ordered by the item's own date; a row with
    no date is not "recent" and is left out rather than dated by the sync."""
    zone = _user_zone(user)
    today = _student_today(user)
    rows = (CourseContent.query.filter_by(user_id=user.id)
            .filter(CourseContent.content_type.in_(_UPDATE_CONTENT_TYPES))
            .filter(CourseContent.published_at != "")
            .order_by(CourseContent.updated_at.desc()).limit(1500).all())
    out = []
    for row in rows:
        day = parse_iso_date(local_date_iso(row.published_at, zone))
        if not day or day > today:
            continue
        if within_days is not None and (today - day).days > within_days:
            continue
        out.append({
            "title": row.title or "Untitled", "course_full": row.course_full or "",
            # "Sep 12" on a notice from two years ago reads as this September.
            "kind": row.content_type, "day": day,
            "pretty": day.strftime("%b %d") if day.year == today.year else day.strftime("%b %d, %Y"),
            "link": _lms_link(row.source_url),
        })
    out.sort(key=lambda u: (u["day"], u["title"]), reverse=True)
    return out[:limit]


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def home():
    view = request.args.get("view", "today")
    course_filter = request.args.get("course", "all")
    deadlines_only = request.args.get("deadlines", "yes")

    # The feed sync is deliberately NOT run here. It fetched the .ics from the
    # university, parsed it and imported every event before a single byte of
    # this page was sent: measured at 11.6s and 12.1s against 0.4s for the rest
    # of the render, so one dashboard load in every AUTO_SYNC_MINUTES cost
    # twelve seconds and the other 29 minutes of loads were fast. Reported as
    # "when i click open dashboard it takes SO LONG to load and open", which is
    # exactly what an intermittent twelve seconds looks like from the outside.
    # The page asks for it after it has painted. The back-off still lives in
    # `auto_sync_if_needed`, which also re-checks `should_auto_sync`, so a feed
    # answering 403 cannot retry per load and three open tabs run one sync.
    sync_due = bool(load_feed_url()) and should_auto_sync()
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
    dashboard_cards = [decorate_card(c) for c in sort_cards(view_filter(build_grouped_cards(raw_tasks), "all"))]

    course_map = {}
    # Every dated piece of work, per course. Which term a course belongs to is
    # answerable from these when Brightspace never said -- see _term_from_dates.
    course_dates = {}

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
        if c.get("main_due_raw"):
            course_dates.setdefault(course, []).append(c["main_due_raw"])

        if not course_map[course]["next_title"]:
            course_map[course]["next_title"] = c.get("title", "Task")
            course_map[course]["next_due"] = c.get("main_due_pretty", "")

    all_synced_courses = SyncedCourse.query.filter_by(user_id=current_user.id).order_by(SyncedCourse.updated_at.desc()).all()
    hidden_course_codes = {c.course_code for c in all_synced_courses if bool(getattr(c, "is_hidden", False))}
    hidden_course_codes.update({h.course_code for h in HiddenCourse.query.filter_by(user_id=current_user.id).all()})
    synced_courses = [c for c in all_synced_courses if not bool(getattr(c, "is_hidden", False)) and c.course_code not in hidden_course_codes]
    for hidden_code in hidden_course_codes:
        course_map.pop(hidden_code, None)
    # One card per course code, but there can now be several ROWS behind it --
    # the same course taken in two terms is two offerings. `_newest_offering_per_code`
    # is the one place that decides which of them the card speaks for.
    for synced in _newest_offering_per_code(synced_courses).values():
        code = synced.course_code or "OTHER"
        if code not in course_map:
            course_map[code] = {
                "course": code, "total": synced.content_count or 0,
                "next_title": "", "next_due": "", "progress": 0,
            }
        card = course_map[code]
        card["title"] = synced.course_name or code
        card["display_code"] = _display_code(code)
        card["image_url"] = synced.image_url or ""
        card["brightspace_url"] = synced.brightspace_url or ""
        card["term"] = synced.term or ""
        card["content_count"] = synced.content_count or 0

    # Sorted by code so a course keeps its place. Ordering by updated_at meant
    # the page rearranged itself after every sync, and combined with the old
    # cap of eight it looked exactly like courses being dropped at random.
    all_course_cards = sorted(course_map.values(), key=lambda c: str(c.get("course") or ""))
    show_all_courses = request.args.get("view") == "all"
    # The page groups by term already, so there is nothing to spare a student by
    # hiding their courses. The cap that remains is a guard against a
    # pathological account, not a page-size.
    course_cards = all_course_cards[:DASHBOARD_COURSE_LIMIT]

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
        # Every card, synced or built from deadlines alone, gets one.
        course_card["display_code"] = _display_code(course_card.get("course"))
        total = course_card["total"]
        compact_code = course_card["course"].replace(" ", "").upper()
        course_card["title"] = course_card.get("title") or course_titles.get(course_card["course"], course_titles.get(compact_code, "Course Workspace"))
        course_card["progress"] = min(95, max(25, 100 - (total * 8)))
        course_card["art"] = course_art[index % len(course_art)]
        course_card["due_status"] = course_card.get("next_due") or "No date"

    # Twelve, of which the page shows five and reveals the next as one is
    # ticked off. It sent six and showed five, so two ticks left a list of
    # three with nothing moving up to fill it.
    task_preview = dashboard_cards[:12]
    # What has been ticked off, newest deadline first, so a wrong click can be
    # put back after the undo toast has gone.
    done_cards = sorted(
        [decorate_card(c) for c in build_grouped_cards(raw_tasks, completed=True)],
        key=lambda c: c.get("main_due_raw") or "", reverse=True)[:12]

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
                "link": c.get("source_link", ""),
            })

    upcoming_cards = upcoming_cards[:4]

    today = date.today()
    month_title = today.strftime("%B %Y")
    month_days = []

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
    # "Recent" by the item's own date, never by when it was imported. The
    # AcademicUpdate rows are stamped at sync time, and "import every term"
    # brought first-year announcements in on the same afternoon as this
    # week's -- so a CHEM 1101 notice from two years ago led the panel,
    # dated today, above anything from the term in progress.
    academic_updates = _recent_updates(current_user, limit=4)
    updates_this_week = _recent_updates(current_user, limit=50, within_days=7)

    # The student's clock, not the server's. See local_stamp.
    last_sync_text = local_stamp(last_sync, "%b %d, %Y at %I:%M %p") or "Never"

    # Group courses by academic term. Brightspace terms may be human-readable
    # ("Winter 2026") or D2L-style codes ("2026.10", "2026.20", "2026.30").
    term_rank=_TERM_RANK
    # A course whose term Brightspace never stated is still plainly in one: its
    # own deadlines say which. Without this a student's CURRENT courses -- the
    # ones that actually have work in them -- fell into "Other courses", and
    # since the page opens on the current term they were hidden behind the
    # picker while finished enrolments filled the screen.
    for card in course_cards:
        if _term_meta(card.get("term") or card.get("title") or "")[1]:
            continue
        guessed = _term_from_dates(course_dates.get(card.get("course"), []))
        if guessed:
            card["term"] = guessed
    grouped_courses={}
    for card in course_cards:
        season,year=_term_meta(card.get("term") or card.get("title") or "")
        label=f"{season} {year}" if year else "Other courses"
        group=grouped_courses.setdefault(label,{"label":label,"season":season,"year":year,"courses":[]})
        group["courses"].append(card)
    course_groups=sorted(grouped_courses.values(),key=lambda g:(g["year"],term_rank.get(g["season"].lower(),4)),reverse=True)
    # Which term the page opens on. A student who imports every term they have
    # ever taken should still land on this one; the rest are a click away in
    # the picker. September is Fall, January is Winter -- the date decides, not
    # whichever group happens to sort first, because a future term (an early
    # Winter enrolment made in November) would otherwise take the page over.
    current_term_label=_current_term_label(today)
    if not any(g["label"]==current_term_label for g in course_groups):
        current_term_label=course_groups[0]["label"] if course_groups else ""
    # The courses in the term the page opens on. The "Active courses" stat
    # counted every card instead, so a third-year student read 33 above a list
    # showing five -- a number that disagrees with what is under it.
    current_term_courses=next((g["courses"] for g in course_groups
                               if g["label"]==current_term_label), [])

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
        current_term_label=current_term_label,
        current_term_courses=current_term_courses,
        show_tour=not bool(getattr(current_user, "tour_done", False)),
        # Accounts that existed before /start, and anyone who skipped it. Two
        # indexed counts per dashboard load, and it disappears the moment
        # anything is connected -- a setup prompt that outlives setup reads
        # as the app being broken.
        setup=_setup_state(current_user.id),
        day_notes=_day_notes_for(current_user.id, _student_today(current_user)),
        task_preview=task_preview,
        done_cards=done_cards,
        upcoming_cards=upcoming_cards,
        month_title=month_title,
        month_days=month_days,
        academic_updates=academic_updates,
        updates_this_week=len(updates_this_week),
        show_all_courses=show_all_courses,
        sync_due=sync_due,
    )



def _room_stats(user, today):
    """What this student has actually done, from the sessions the room's own
    clock recorded. `actual_minutes` was written by Done and read by nothing
    outside the estimator; this is the other thing it is good for -- telling
    a student what their week really looked like, which no other screen does.

    Only finished sessions with a real duration count. A session marked done
    with no minutes recorded is not evidence of anything, the same rule
    `_learned_effort` follows.
    """
    since = today - timedelta(days=27)
    rows = (StudySession.query.filter_by(user_id=user.id, state="done")
            .filter(StudySession.on_date >= since.isoformat(),
                    StudySession.on_date <= today.isoformat()).all())
    by_day, by_course = {}, {}
    for row in rows:
        minutes = int(row.actual_minutes or 0)
        if minutes <= 0:
            continue
        by_day[row.on_date] = by_day.get(row.on_date, 0) + minutes
        code = row.course_full or "OTHER"
        by_course[code] = by_course.get(code, 0) + minutes
    week = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    # A streak counts back from today, and today not being done yet does not
    # break it -- otherwise every morning reads as a lost streak.
    streak = 0
    day = today
    if not by_day.get(day.isoformat()):
        day -= timedelta(days=1)
    while by_day.get(day.isoformat()):
        streak += 1
        day -= timedelta(days=1)
    return {
        "week": [{"date": d, "label": datetime.strptime(d, "%Y-%m-%d").strftime("%a")[:1],
                  "minutes": by_day.get(d, 0), "today": d == today.isoformat()} for d in week],
        "week_minutes": sum(by_day.get(d, 0) for d in week),
        "month_minutes": sum(by_day.values()),
        "sessions": sum(1 for r in rows if int(r.actual_minutes or 0) > 0),
        "streak": streak,
        "courses": sorted(({"course": c, "minutes": m} for c, m in by_course.items()),
                          key=lambda x: x["minutes"], reverse=True)[:5],
        "best": max(by_day.values()) if by_day else 0,
    }


@app.route("/study-room")
@login_required
def study_room():
    """The room is the place a plan block gets done. Everything on the desk
    is the student's real work, with the identity the drawer needs to act on
    it: a task's group to tick it, a deadline's link to open it, tonight's
    plan blocks to run the clock against."""
    raw_tasks = load_tasks()
    cards = [decorate_card(c) for c in sort_cards(view_filter(build_grouped_cards(raw_tasks), "all"))]
    active = [c for c in cards if not c.get("completed")][:12]
    deadlines = []
    for c in active:
        if c.get("main_due_raw"):
            deadlines.append({
                "title": c.get("title", "Task"),
                "course": c.get("course_full", "OTHER"),
                "due": c.get("main_due_pretty") or c.get("status_label") or "Upcoming",
                "status": c.get("status", ""),
                "status_label": c.get("status_label", ""),
                "link": c.get("source_link") or "",
            })
    tasks = [{
        "group_id": c.get("group_id", ""),
        "title": c.get("title", "Task"),
        "course": c.get("course_full", "OTHER"),
        "due": c.get("main_due_pretty") or "No date",
        "status": c.get("status_label") or "Active",
        "link": c.get("source_link") or "",
    } for c in active]
    # One entry per course, named by the newest offering -- the same rule the
    # dashboard and the grade calculator use.
    synced = list(_newest_offering_per_code(
        SyncedCourse.query.filter_by(user_id=current_user.id).all()).values())
    synced.sort(key=lambda x: (_term_sort_key(x.term or ""), x.course_code or ""), reverse=True)
    courses = [{"code": x.course_code or "OTHER", "name": x.course_name or x.course_code or "Course"} for x in synced[:12]]
    if not courses:
        seen = []
        for c in cards:
            code = c.get("course_full") or "OTHER"
            if code not in seen:
                seen.append(code)
                courses.append({"code": code, "name": "Course workspace"})
    # Tonight's plan, on the student's clock. The desk clock runs a block and
    # records what actually happened, which is the fact the planner learns
    # from (see _learned_effort).
    today = _student_today(current_user)
    # A week of blocks: the screen shows today (or, at 23:50 with nothing
    # left, tomorrow), and the whiteboard on the wall shows the week.
    horizon = (today + timedelta(days=6)).isoformat()
    blocks = _sessions_json(StudySession.query.filter_by(user_id=current_user.id)
                            .filter(StudySession.on_date >= today.isoformat(), StudySession.on_date <= horizon)
                            .order_by(StudySession.on_date.asc(), StudySession.start_min.asc()).all())
    now = _student_now(current_user)
    room_data = {
        "tasks": tasks, "deadlines": deadlines[:8], "courses": courses, "blocks": blocks,
        "today": today.isoformat(), "now_min": now.hour * 60 + now.minute,
        "first_name": (current_user.name or "Student").split()[0],
        "stats": _room_stats(current_user, today),
    }
    return render_template(
        "study_room.html", user=current_user, tasks=tasks, deadlines=deadlines[:8], courses=courses,
        blocks=blocks, room_data=room_data,
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


# ============================
# Calendar events the student owns
# ============================
# An event is a Task. It deliberately is not its own table: the dashboard, the
# calendar, the completion toggle and the .ics feed already understand Tasks,
# and a parallel model would have to be taught to every one of them.

EVENT_KINDS = {"due", "exam", "event"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _event_fields(data, partial=False):
    """Validate one event payload. Returns (fields, error)."""
    fields = {}

    if not partial or "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            return None, "Give the event a name."
        fields["title"] = title[:500]

    if not partial or "date" in data:
        raw = str(data.get("date") or "").strip()
        if not raw:
            return None, "Choose a date."
        if not parse_iso_date(raw):
            return None, "That date could not be read. Use the date picker."
        fields["due_raw"] = raw

    if "time" in data:
        raw = str(data.get("time") or "").strip()
        if raw and not _TIME_RE.match(raw):
            return None, "Use a 24-hour time such as 14:30, or leave it blank."
        fields["due_time"] = raw

    if "course" in data:
        course = _normalize_course_code(str(data.get("course") or "")) or "OTHER"
        fields["course_full"] = course
        fields["course_prefix"] = "OTHER" if course == "OTHER" else course_prefix(course)

    if "kind" in data:
        kind = str(data.get("kind") or "event").strip().lower()
        fields["kind"] = kind if kind in EVENT_KINDS else "event"

    if "location" in data:
        fields["location"] = str(data.get("location") or "").strip()[:255]

    if "notes" in data:
        fields["notes"] = str(data.get("notes") or "").strip()[:2000]

    return fields, None


def _event_json(task):
    return {
        "id": task.id, "group_id": task.group_id, "title": task.title,
        "course": task.course_full or "OTHER", "date": task.due_raw or "",
        "time": task.due_time or "", "kind": task.kind or "event",
        "location": task.location or "", "notes": task.notes or "",
        "completed": bool(task.completed),
        # An imported deadline can be deleted, but the delete has to be
        # remembered differently, and the student is told which kind it is.
        "imported": (task.source or "manual") != "manual",
    }


@app.route("/api/events", methods=["POST"])
@login_required
def create_event():
    data = request.get_json(silent=True) or {}
    fields, error = _event_fields(data)
    if error:
        return jsonify({"error": error}), 400
    fields.setdefault("course_full", "OTHER")
    fields.setdefault("course_prefix", "OTHER")
    fields.setdefault("kind", "event")
    # base_title() exists to merge an LMS feed's "Assignment 1 (opens)" with its
    # "(due)" twin. A student's own event has no twin, and collapsing it would
    # show them a title they did not type.
    base = fields["title"]
    task = Task(
        id=uuid4().hex, user_id=current_user.id,
        group_id=make_group_id(fields["course_full"], base), base_title=base,
        is_deadline=True, source="manual", sync_state="active",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **fields,
    )
    db.session.add(task)
    db.session.commit()
    return jsonify({"ok": True, "event": _event_json(task)}), 201


@app.route("/api/events/<event_id>", methods=["POST", "PATCH"])
@login_required
def update_event(event_id):
    task = Task.query.filter_by(user_id=current_user.id, id=event_id).first()
    if not task:
        return jsonify({"error": "That event no longer exists."}), 404
    data = request.get_json(silent=True) or {}
    fields, error = _event_fields(data, partial=True)
    if error:
        return jsonify({"error": error}), 400
    for key, value in fields.items():
        setattr(task, key, value)
    if "title" in fields:
        task.base_title = task.title if (task.source or "manual") == "manual" else base_title(task.title)
    if "title" in fields or "course_full" in fields:
        task.group_id = make_group_id(task.course_full or "OTHER", task.base_title or "")
    db.session.commit()
    return jsonify({"ok": True, "event": _event_json(task)})


@app.route("/api/events/<event_id>", methods=["DELETE"])
@login_required
def delete_event(event_id):
    """Delete the whole calendar entry, not one row of it.

    An imported assignment can be three Tasks -- opens, due, closes -- sharing
    a group, and the calendar shows the group once. Deleting only the row the
    student clicked let the group fall back to its next date, so the entry
    reappeared a few days later and the delete looked broken.
    """
    task = Task.query.filter_by(user_id=current_user.id, id=event_id).first()
    if not task:
        return jsonify({"error": "That event no longer exists."}), 404
    siblings = Task.query.filter_by(user_id=current_user.id, group_id=task.group_id).all()
    restore = _event_json(task)
    restore["group_id"] = task.group_id
    restorable = False
    for row in siblings:
        if (row.source or "manual") == "manual":
            db.session.delete(row)
        else:
            # Imported: keep the row so the next sync recognises it and leaves
            # it alone, rather than importing it again and undoing the delete.
            row.sync_state = "dismissed"
            restorable = True
    db.session.commit()
    restore["restorable"] = restorable
    return jsonify({"ok": True, "deleted": restore})


@app.route("/api/events/<event_id>/restore", methods=["POST"])
@login_required
def restore_event(event_id):
    """Undo for a deleted import. A manual event's rows are gone, so the client
    sends its fields back to /api/events instead."""
    task = Task.query.filter_by(user_id=current_user.id, id=event_id).first()
    if not task:
        return jsonify({"error": "That event cannot be restored."}), 404
    for row in Task.query.filter_by(user_id=current_user.id, group_id=task.group_id).all():
        row.sync_state = "active"
    db.session.commit()
    return jsonify({"ok": True, "event": _event_json(task)})


# ============================
# "Biweekly tests, skipping reading week" -> the dates it means
# ============================
# Same split as /api/study-plan/ask: the model reads the REQUEST, the server
# does the arithmetic. A model emitting the dates directly would be a different
# calendar every time you asked, would cost quota to redraw, and could not be
# checked -- and these are dates, which is the one thing this app must never be
# creative about. So a sentence becomes a recurrence, and this expands it.
#
# Nothing here writes. The route previews, the student confirms, and only then
# are events created: a misread "biweekly" as "daily" is the difference between
# six entries and ninety, and that has to be visible BEFORE it happens.

RECURRENCE_CAP = 60              # loud, reported as `truncated` -- never silent
RECURRENCE_HORIZON_DAYS = 550    # ~18 months; a degree is not a recurrence
_RECURRENCE_STEPS = {"weekly": 7, "biweekly": 14, "fortnightly": 14}


def _add_months(day, n):
    """Same day-of-month n months on, clamped to the end of a short month."""
    month = day.month - 1 + n
    year = day.year + month // 12
    month = month % 12 + 1
    last = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def _recurrence_excluded(day, skip_dates, skip_ranges):
    if day.isoformat() in skip_dates:
        return True
    return any(a <= day <= b for a, b in skip_ranges)


def _expand_recurrence(spec, today=None):
    """The exact dates a recurrence means. Pure arithmetic, no model, no writes.

    Returns (dates, notes). `notes` is every reason the answer is not simply
    "what you asked for" -- a cap hit, a horizon reached, holidays dropped --
    because a limit a student cannot see is indistinguishable from a bug.

    An excluded date does NOT consume `count`: "six tests, skipping reading
    week" means six tests. The preview shows the real dates, so if that reading
    is wrong it is wrong visibly.
    """
    today = today or date.today()
    notes = []

    start = parse_iso_date(str(spec.get("start") or "").strip())
    if not start:
        return [], ["No start date could be read, so nothing was worked out."]

    repeat = str(spec.get("repeat") or "once").strip().lower()
    until = parse_iso_date(str(spec.get("until") or "").strip())

    try:
        count = int(spec.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0 and not until and repeat != "once":
        count = 12
        notes.append("You did not say how many or until when, so this stops after 12.")
    if count > RECURRENCE_CAP:
        notes.append(f"You asked for {count}; capped at {RECURRENCE_CAP} entries.")
        count = RECURRENCE_CAP
    count = max(count, 0)

    skip_dates = {str(d).strip() for d in (spec.get("skip_dates") or []) if str(d).strip()}
    skip_ranges = []
    for pair in (spec.get("skip_ranges") or []):
        try:
            a, b = parse_iso_date(str(pair[0])), parse_iso_date(str(pair[1]))
        except (TypeError, IndexError, KeyError):
            continue
        if a and b:
            skip_ranges.append((a, b) if a <= b else (b, a))

    # What an excluded date MEANS. "There is no class that week" almost always
    # postpones the schedule -- the test happens the week term resumes and the
    # rhythm carries on from there -- it does not delete a test. Dropping was the
    # only behaviour at first, and on a real request (biweekly from Oct 13, no
    # uni Oct 23-28) it silently produced a 28-day hole and carried on as though
    # nothing had moved. "shift" is the default for that reason; "drop" is for
    # work that is genuinely cancelled rather than moved.
    mode = str(spec.get("on_excluded") or "shift").strip().lower()
    if mode not in {"shift", "drop"}:
        mode = "shift"

    horizon = today + timedelta(days=RECURRENCE_HORIZON_DAYS)
    step = _RECURRENCE_STEPS.get(repeat)
    # Shift a WEEK at a time for a weekly or biweekly series: moving day by day
    # would land a Tuesday test on a Thursday, and the weekday is the half of a
    # class schedule nobody restates.
    hop = 7 if repeat in _RECURRENCE_STEPS else 1
    kept, dropped, moved_notes, guard, nth = [], 0, [], 0, 0
    anchor = start                      # the cadence is measured from here
    cur = start

    def _next(n):
        return (_add_months(anchor, n) if repeat == "monthly"
                else anchor + timedelta(days=(step or 7) * n))

    while guard < 400:
        guard += 1
        if cur > horizon:
            notes.append("Stopped about 18 months out; add the rest nearer the time.")
            break
        if until and cur > until:
            break

        if _recurrence_excluded(cur, skip_dates, skip_ranges):
            placed = None
            if mode == "shift":
                probe = cur
                for _ in range(12):
                    probe = probe + timedelta(days=hop)
                    if probe > horizon or (until and probe > until):
                        break
                    if not _recurrence_excluded(probe, skip_dates, skip_ranges):
                        placed = probe
                        break
            if placed is None:
                dropped += 1
            else:
                moved_notes.append((cur, placed))
                kept.append(placed)
                # Re-anchor: everything after a postponement follows from the new
                # date, which is what "then we continue" means. Keeping the old
                # grid would put the next one 7 days after the moved test.
                anchor, nth = placed, 0
                if count and len(kept) >= count:
                    break
                if len(kept) >= RECURRENCE_CAP:
                    notes.append(f"Capped at {RECURRENCE_CAP} entries.")
                    break
                if repeat == "once":
                    break
                nth += 1
                cur = _next(nth)
                continue
        else:
            kept.append(cur)
            if count and len(kept) >= count:
                break
            if len(kept) >= RECURRENCE_CAP:
                notes.append(f"Capped at {RECURRENCE_CAP} entries.")
                break

        if repeat == "once":
            break
        # Always step from the ANCHOR, never from the last date produced.
        # Stepping from the result makes clamping cumulative: monthly from the
        # 31st gave Jan 31, Feb 28, then Mar 28 -- the 31st silently became the
        # 28th for the rest of the year. It also stops any drift accumulating.
        nth += 1
        cur = _next(nth)

    for was, now in moved_notes[:3]:
        notes.append(f"{was.isoformat()} fell in the dates you excluded, so it moved "
                     f"to {now.isoformat()} and the rest follow from there.")
    if len(moved_notes) > 3:
        notes.append(f"{len(moved_notes) - 3} more were moved the same way.")
    if dropped:
        notes.append(f"{dropped} date{'' if dropped == 1 else 's'} could not be placed "
                     f"outside the dates you excluded, so {'it was' if dropped == 1 else 'they were'} dropped.")
    return kept, notes


def _recurrence_events(spec, dates):
    """One event payload per date, in the shape /api/events already validates."""
    title = str(spec.get("title") or "").strip()[:500]
    course = _normalize_course_code(str(spec.get("course") or "")) or "OTHER"
    kind = str(spec.get("kind") or "event").strip().lower()
    if kind in {"test", "midterm", "final", "quiz"}:
        kind = "exam"          # EVENT_KINDS is due|exam|event; a test is an exam
    if kind not in EVENT_KINDS:
        kind = "event"
    time_raw = str(spec.get("time") or "").strip()
    if time_raw and not _TIME_RE.match(time_raw):
        time_raw = ""
    out = []
    for i, day in enumerate(dates, start=1):
        label = title or "Event"
        if len(dates) > 1:
            label = f"{label} {i}"
        out.append({"title": label, "date": day.isoformat(), "time": time_raw,
                    "course": course, "kind": kind,
                    "location": str(spec.get("location") or "").strip()[:200]})
    return out


CALENDAR_SAMPLE_DAYS = 240       # how far the sample shown to the model reaches
CALENDAR_SAMPLE_BACK = 30
CALENDAR_SAMPLE_MAX = 90
CALENDAR_MATCH_CAP = 200         # how many rows one operation may select
CALENDAR_CHANGE_CAP = 120        # loud, never silent


def _calendar_summary(user_id, today):
    """What the student HAS, as counts rather than rows.

    The first version handed the model a flat list of entries and made every
    operation name ids out of it. That fails twice over: the list is capped, so
    on an account with thirty-five courses it never reached December and the
    entries a student asked about were simply not in it; and "the exams of
    ELEC2501" is a FILTER, not a list of ids -- the model refused the request
    outright, saying renaming "requires specific entry IDs".

    So the model gets the shape of the calendar (which courses, which kinds, how
    many, over what dates) and writes a filter. The server resolves it against
    every row the student has, which is complete by construction and needs no
    enumeration.
    """
    rows = (Task.query.filter_by(user_id=user_id)
            .filter(Task.sync_state != "dismissed")
            .filter(Task.due_raw != "", Task.due_raw.isnot(None)).all())
    by_course = {}
    for t in rows:
        key = (t.course_full or "OTHER", t.kind or "event")
        slot = by_course.setdefault(key, {"n": 0, "lo": "", "hi": "", "titles": []})
        slot["n"] += 1
        d = t.due_raw or ""
        if d and (not slot["lo"] or d < slot["lo"]):
            slot["lo"] = d
        if d and (not slot["hi"] or d > slot["hi"]):
            slot["hi"] = d
        if len(slot["titles"]) < 4:
            slot["titles"].append((t.title or "")[:46])
    lines = []
    for (course, kind), slot in sorted(by_course.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        lines.append(f'{course} | {kind} | {slot["n"]} entries | {slot["lo"]}..{slot["hi"]} '
                     f'| e.g. {"; ".join(slot["titles"])}')
    return "\n".join(lines[:60])


def _calendar_sample(user_id, today):
    """A window of real entries WITH ids, for "move the midterm" -- the cases
    where the student means one specific thing and naming it is the point."""
    lo = (today - timedelta(days=CALENDAR_SAMPLE_BACK)).isoformat()
    hi = (today + timedelta(days=CALENDAR_SAMPLE_DAYS)).isoformat()
    rows = (Task.query.filter_by(user_id=user_id)
            .filter(Task.sync_state != "dismissed")
            .filter(Task.due_raw >= lo, Task.due_raw <= hi)
            .order_by(Task.due_raw.asc(), Task.id.asc())
            .limit(CALENDAR_SAMPLE_MAX).all())
    return "\n".join(
        f'{t.id}|{t.due_raw or "?"}|{(t.due_time or "--")}|{t.course_full or "OTHER"}|'
        f'{(t.kind or "event")}|{(t.title or "")[:60]}' for t in rows)


_MATCH_VERB = {"delete": "delete", "move": "move", "retime": "retime",
               "rename": "rename", "recourse": "change the course of"}


def _match_description(op):
    """What an operation said it was looking for, in the student's own terms.

    Only used to explain a filter that found nothing, which is the one case
    where the student needs to know what Nexora went looking for.
    """
    m = op.get("match") if isinstance(op.get("match"), dict) else {}
    bits = []
    title = str(m.get("title_contains") or "").strip()
    course = str(m.get("course") or "").strip()
    kind = str(m.get("kind") or "").strip().lower()
    if title:
        bits.append(f"named like “{title}”")
    if kind in EVENT_KINDS:
        bits.append({"exam": "exams", "due": "deadlines"}.get(kind, "events"))
    if course:
        bits.append(f"on {course}")
    frm, to = str(m.get("from") or "").strip(), str(m.get("to") or "").strip()
    if frm and to:
        bits.append(f"between {frm} and {to}")
    elif frm:
        bits.append(f"from {frm}")
    elif to:
        bits.append(f"up to {to}")
    return " ".join(bits)


def _resolve_known_course(value, user_id):
    """The student's OWN code for what they just typed, where there is one.

    Someone looking at entries filed under OTHER asks for "ELEC2501", and their
    course is ELEC2501B. Storing the string as typed moves the entries off OTHER
    and onto a code that still matches no course they have -- so the outline, the
    grades and the study planner go on ignoring them, and the only visible change
    is the label. That reads as fixed and is not.

    A code that uniquely extends, or is uniquely extended by, one of theirs wins.
    Two offerings that both match (ELEC2501A and ELEC2501B) are ambiguous, so the
    literal stands rather than Nexora picking a section for them.
    """
    code = _normalize_course_code(value or "")
    if not code or code == "OTHER":
        return ""
    known = {c for c in _known_course_codes(user_id) if c and c != "OTHER"}
    if code in known:
        return code
    near = [k for k in known if k.startswith(code) or code.startswith(k)]
    return near[0] if len(near) == 1 else code


def _calendar_match(op, user_id, pending=None):
    """The rows an operation applies to: explicit ids, or a filter resolved here.

    A filter that names nothing matches NOTHING, deliberately. "Delete my
    events" with an empty filter would otherwise clear the calendar, and a
    vague sentence must never be able to do that.
    """
    q = (Task.query.filter_by(user_id=user_id)
         .filter(Task.sync_state != "dismissed"))
    ids = [str(i).strip() for i in (op.get("ids") or []) if str(i).strip()]
    if ids:
        rows = q.filter(Task.id.in_(ids[:CALENDAR_MATCH_CAP])).all()
        return rows, len(ids) - len(rows)

    m = op.get("match") or {}
    if not isinstance(m, dict):
        return [], 0
    used = False
    # "not given" and "literally OTHER" are different filters, and they used to
    # be the same one: _normalize_course_code("") returns "OTHER", so the guard
    # against an unset field also threw away a deliberate OTHER -- which is the
    # one course a student ever asks about by name, because it is where every
    # entry lands that nothing named a course for.
    raw_course = str(m.get("course") or "").strip()
    if raw_course:
        course = _normalize_course_code(raw_course)
        if course:
            q, used = q.filter(Task.course_full == course), True
    kind = str(m.get("kind") or "").strip().lower()
    if kind in EVENT_KINDS:
        q, used = q.filter(Task.kind == kind), True
    contains = str(m.get("title_contains") or "").strip()
    if contains:
        # The model chains operations: asked to rename an entry and then move
        # it, it matched the second one on the name the FIRST one will create.
        # Resolving every filter against the stored titles found nothing and
        # half the request was silently dropped. Rows already being renamed in
        # this plan are matched on their new name.
        renamed = [rid for rid, title in (pending or {}).items()
                   if contains.lower() in (title or "").lower()]
        cond = Task.title.ilike("%" + contains + "%")
        if renamed:
            cond = or_(cond, Task.id.in_(renamed))
        q, used = q.filter(cond), True
    frm = parse_iso_date(str(m.get("from") or "").strip())
    to = parse_iso_date(str(m.get("to") or "").strip())
    if frm:
        q, used = q.filter(Task.due_raw >= frm.isoformat()), True
    if to:
        q, used = q.filter(Task.due_raw <= to.isoformat()), True
    if not used:
        return [], 0
    return q.order_by(Task.due_raw.asc()).limit(CALENDAR_MATCH_CAP).all(), 0


def _calendar_plan(operations, user_id, today):
    """Turn the model's operations into concrete, checked changes.

    Nothing is written here. Every row comes out of a query scoped to this
    student, so an id the model invented and a filter it guessed can both only
    ever reach rows they actually own. Dates are expanded by
    `_expand_recurrence`, never by the model.
    """
    changes, notes = [], []
    pending = {}            # row id -> the title a rename earlier in this plan gives it
    unknown_ops = []        # kinds the model asked for that this server cannot do
    if not isinstance(operations, list):
        return [], ["Nexora could not read that as calendar changes."]

    for op in operations[:40]:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip().lower()

        if kind == "add":
            dates, sub = _expand_recurrence(op, today)
            notes.extend(sub)
            for payload in _recurrence_events(op, dates):
                changes.append({"op": "add", **payload,
                                # The course is part of what is being agreed to.
                                # Without it on the label a student confirmed a
                                # series of tests and only found out afterwards
                                # that every one had been filed under OTHER.
                                "label": f"Add “{payload['title']}” on {payload['date']}"
                                         + (f" at {payload['time']}" if payload["time"] else "")
                                         + f" · {payload.get('course_full') or 'OTHER'}"})
            continue

        # An operation this server cannot carry out must SAY so. Dropping it
        # silently is how "change the course for Test 2,3,4,5 from OTHER to
        # ELEC2501" produced the sentence "I will update the course for Test 2,
        # Test 3, Test 4 and Test 5" directly above "Nexora found nothing to
        # change" -- a confident promise and a flat contradiction, on one screen.
        if kind not in {"delete", "move", "retime", "rename", "recourse"}:
            unknown_ops.append(kind or "(unnamed)")
            continue

        rows, missing = _calendar_match(op, user_id, pending)
        if missing:
            notes.append(f"{missing} entr{'y' if missing == 1 else 'ies'} named in that "
                         "request no longer exist, so they were left out.")
        if not rows:
            # A filter that finds nothing is the OTHER way a promise goes
            # unkept in silence. Asked to recourse the labs and also "push every
            # ELEC2501B test back by 7 days" when no test is on that course yet,
            # the reply said it was doing both, did one, and explained nothing.
            # Correct behaviour, invisible -- which is the half that reads as
            # the AI not thinking.
            desc = _match_description(op)
            verb = _MATCH_VERB.get(kind, kind)
            notes.append(f"Nothing matched the entries to {verb}"
                         + (f" ({desc})" if desc else "")
                         + ", so that part was not done.")
            continue

        # Dispatch on what the operation DESCRIBES, not only on what it is
        # labelled. Asked to "rename Elec Lab 3 and move it to December 20" the
        # model returned one operation named "rename" carrying a "to" as well,
        # and the move was silently dropped -- half a request, done confidently.
        to_date = parse_iso_date(str(op.get("to") or "").strip())
        by_days = op.get("by_days")
        has_move = bool(to_date) or by_days not in (None, "", 0)
        time_given = str(op.get("time") or "").strip()
        has_retime = kind == "retime" or bool(time_given)
        repl = op.get("replace") if isinstance(op.get("replace"), dict) else {}
        # "title" counts only on an op actually labelled rename -- an ADD is
        # handled above and returns, so this can never read an ADD's own title.
        has_rename = bool(str(repl.get("from") or "").strip()
                          or str(op.get("title_new") or "").strip()
                          or (kind == "rename" and str(op.get("title") or "").strip()))

        if kind == "delete":
            for row in rows:
                shown = pending.get(row.id, row.title)
                changes.append({"op": "delete", "id": row.id, "title": shown,
                                "date": row.due_raw or "",
                                "label": f"Delete “{shown}” on {row.due_raw or 'no date'}"})

        if has_move:
            to, by = to_date, by_days
            for row in rows:
                if to and len(rows) == 1:
                    target = to
                elif by not in (None, "", 0):
                    base = parse_iso_date(row.due_raw or "")
                    if not base:
                        continue
                    try:
                        target = base + timedelta(days=int(by))
                    except (TypeError, ValueError):
                        continue
                elif to:
                    # Moving many entries ONTO one date is almost never meant and
                    # cannot be undone in one go; say so rather than stack them.
                    notes.append(f"{len(rows)} entries matched, so they were not all moved "
                                 "onto one date. Say how many days to move them by instead.")
                    break
                else:
                    continue
                changes.append({"op": "move", "id": row.id,
                                "title": pending.get(row.id, row.title),
                                "from": row.due_raw or "", "date": target.isoformat(),
                                "label": f"Move “{pending.get(row.id, row.title)}” from "
                                         f"{row.due_raw or 'no date'} to {target.isoformat()}"})

        if has_retime:
            time_raw = time_given
            if time_raw and not _TIME_RE.match(time_raw):
                notes.append(f"Ignored the time “{time_raw}”; it is not a 24-hour time.")
                time_raw = None
            for row in (rows if time_raw is not None else []):
                shown = pending.get(row.id, row.title)
                changes.append({"op": "retime", "id": row.id, "title": shown,
                                "time": time_raw,
                                "label": (f"Set “{shown}” to {time_raw}"
                                          if time_raw else f"Clear the time on “{shown}”")})

        if has_rename:
            find = str(repl.get("from") or "").strip()
            into = str(repl.get("to") or "").strip()
            fixed = str(op.get("title_new") or op.get("title") or "").strip()[:500]
            for row in rows:
                current = pending.get(row.id, row.title) or ""
                if find:
                    # A substitution, so "Elec Lab 4" becomes "Elec Test 4" and
                    # the numbering survives. Setting one title across many rows
                    # would collapse a series into five identical entries.
                    new = re.sub(re.escape(find), into, current, flags=re.IGNORECASE).strip()
                elif fixed and len(rows) == 1:
                    new = fixed
                elif fixed:
                    notes.append(f"{len(rows)} entries matched one new name, which would make "
                                 "them identical. Say what to replace instead, such as "
                                 "“Elec Lab” to “Elec Test”.")
                    break
                else:
                    continue
                new = new[:500]
                if not new or new == current:
                    continue
                pending[row.id] = new
                changes.append({"op": "rename", "id": row.id, "title": new,
                                "from_title": current,
                                "label": f"Rename “{current}” to “{new}”"})

        # Which course an entry belongs to. Everything downstream joins on the
        # code -- the outline's weights, _course_standing, which block the study
        # plan says is for what -- so an entry stranded on OTHER is not a label
        # problem, it is invisible to every feature that matters.
        if kind == "recourse":
            wanted = _resolve_known_course(
                str(op.get("course_new") or op.get("course") or ""), user_id)
            if not wanted:
                notes.append("That did not name a course to move them to.")
            else:
                for row in rows:
                    was = row.course_full or "OTHER"
                    if was == wanted:
                        continue
                    shown = pending.get(row.id, row.title)
                    changes.append({"op": "recourse", "id": row.id, "title": shown,
                                    "course": wanted, "from_course": was,
                                    "label": f"Move “{shown}” from {was} to {wanted}"})

    if unknown_ops:
        names = ", ".join(sorted(set(unknown_ops)))
        notes.append(f"Nexora cannot do this to a calendar entry: {names}. "
                     "It can add, move, retime, rename, change the course, and delete.")

    if len(changes) > CALENDAR_CHANGE_CAP:
        notes.append(f"That came to {len(changes)} changes; showing the first "
                     f"{CALENDAR_CHANGE_CAP}. Ask for the rest separately.")
        changes = changes[:CALENDAR_CHANGE_CAP]
    return changes, notes


def _calendar_apply(changes, user_id):
    """Apply a plan the student has confirmed. Ownership is re-checked here --
    the plan makes a round trip through the browser, so it is not trusted."""
    owned = {t.id: t for t in Task.query.filter_by(user_id=user_id).all()}
    done = {"added": 0, "deleted": 0, "moved": 0, "retimed": 0, "renamed": 0,
            "recoursed": 0}

    for change in (changes or [])[:CALENDAR_CHANGE_CAP]:
        if not isinstance(change, dict):
            continue
        op = str(change.get("op") or "").strip().lower()

        if op == "add":
            fields, error = _event_fields(change)
            if error:
                continue
            fields.setdefault("course_full", "OTHER")
            fields.setdefault("course_prefix", "OTHER")
            base = fields["title"]
            db.session.add(Task(
                id=uuid4().hex, user_id=user_id,
                group_id=make_group_id(fields["course_full"], base), base_title=base,
                is_deadline=True, source="manual", sync_state="active",
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **fields))
            done["added"] += 1
            continue

        row = owned.get(str(change.get("id") or "").strip())
        if row is None:
            continue

        if op == "delete":
            # The whole entry, not one row of it, and an import is dismissed
            # rather than deleted -- the same rules as /api/events DELETE, or a
            # three-row assignment reappears on its next date and the next sync
            # undoes the student's decision.
            for sib in Task.query.filter_by(user_id=user_id, group_id=row.group_id).all():
                if (sib.source or "manual") == "manual":
                    db.session.delete(sib)
                else:
                    sib.sync_state = "dismissed"
            done["deleted"] += 1
        elif op == "move":
            target = parse_iso_date(str(change.get("date") or "").strip())
            if target:
                row.due_raw = target.isoformat()
                done["moved"] += 1
        elif op == "retime":
            time_raw = str(change.get("time") or "").strip()
            if not time_raw or _TIME_RE.match(time_raw):
                row.due_time = time_raw
                done["retimed"] += 1
        elif op == "rename":
            title = str(change.get("title") or "").strip()[:500]
            if title:
                row.title = title
                if (row.source or "manual") == "manual":
                    row.base_title = title
                row.group_id = make_group_id(row.course_full or "OTHER", row.base_title or title)
                done["renamed"] += 1
        elif op == "recourse":
            # Re-resolved here rather than trusted: the plan went through the
            # browser, and this writes the key every other feature joins on.
            course = _resolve_known_course(str(change.get("course") or ""), user_id)
            if course:
                # group_id is derived from the course, so the whole group moves
                # together or an imported assignment's three rows split across
                # two courses and the calendar shows it twice.
                for sib in Task.query.filter_by(user_id=user_id,
                                                group_id=row.group_id).all():
                    sib.course_full = course
                    sib.course_prefix = course
                    sib.group_id = make_group_id(course, sib.base_title or sib.title or "")
                done["recoursed"] += 1

    db.session.commit()
    return done


ANNOUNCE_RESCAN_HOURS = 6        # how often the calendar may spend a call unasked
ANNOUNCEMENT_SCAN_DAYS = 75      # how far back an announcement can be and still be about work to come
ANNOUNCEMENT_SCAN_MAX = 14       # how many candidates reach the model
ANNOUNCEMENT_HORIZON_DAYS = 300  # how far ahead a stated date may be


def _announcement_candidates(user_id, today, zone):
    """Announcements recent enough, and date-bearing enough, to be worth reading.

    This feature existed once and was removed. It read dates out of announcements
    with a regular expression, and once the extension imported every term it read
    every old announcement too -- the dates carry no year, so a first-year test
    was offered as "Tue, Sep 29" this September, ten of them above the calendar.
    The note left behind said that if it came back it had to be current-term only,
    future-dated only and year-aware.

    So the deterministic helpers are the FILTER, not the answer: they decide
    which announcements are even worth a model's attention, and the publish date
    is the reference that gives a year-less "Nov 7" its year. The model then
    reads what the announcement actually means, and the server throws out
    anything in the past.
    """
    cutoff = (today - timedelta(days=ANNOUNCEMENT_SCAN_DAYS)).isoformat()
    rows = (CourseContent.query
            .filter_by(user_id=user_id, content_type="announcement")
            .filter(CourseContent.published_at >= cutoff)
            .order_by(CourseContent.published_at.desc()).limit(120).all())
    out = []
    for row in rows:
        blob = ((row.title or "") + "\n" + (row.content_text or "")).strip()
        # _EXAM_WORDS, not _looks_like_an_exam. That helper is
        # `_EXAM_WORDS and not _NOT_AN_EXAM`, and _NOT_AN_EXAM exists to stop
        # the OLD panel offering "exam review session" as an exam. Used as a
        # gate on what the model may even see, it threw away the announcement
        # this feature was built for: "the midterm exam Module has become
        # available for you to review the information" was vetoed on the word
        # "review". The regex decides what is worth a model's attention; the
        # model decides what it means. That is the whole point of it thinking.
        if not blob or not _EXAM_WORDS.search(blob):
            continue
        ref = parse_iso_date(local_date_iso(row.published_at, zone)) or today
        if _find_date(blob, ref) is None:
            continue                      # no date stated: nothing to offer
        out.append((row, ref, blob))
        if len(out) >= ANNOUNCEMENT_SCAN_MAX:
            break
    return out


def _already_on_the_calendar(user_id, course, day, title):
    """Whether this is a date the student already has.

    Offering a midterm that is already on the calendar is how a feature becomes
    noise, and this one was removed for being noise once already.
    """
    stem = _assessment_stem(title) if title else ""
    rows = (Task.query.filter_by(user_id=user_id)
            .filter(Task.sync_state != "dismissed")
            .filter(Task.due_raw.in_([day.isoformat(),
                                      (day - timedelta(days=1)).isoformat(),
                                      (day + timedelta(days=1)).isoformat()])).all())
    for row in rows:
        if course and (row.course_full or "") != course:
            continue
        other = _assessment_stem(row.title or "")
        if stem and other and (stem in other or other in stem):
            return True
        if (row.kind or "") == "exam":
            return True               # an exam already sits on that day
    return False


def _outline_dated_assessments(user_id, today, zone):
    """Dates the outline pipeline already pulled out, that never reached the calendar.

    An analysed outline records `important_dates` -- "Midterm 2, October 30, 6pm,
    Southam" -- and the course workspace shows them while the calendar, the one
    place a student goes to find out when things are, never did.

    No model runs here. The pipeline did that work when the outline was analysed,
    and re-reading structured output with an LLM would spend quota to learn
    nothing. That also means this picks up a REVISED outline for free: a
    re-analysis rewrites `grading_json`, and the next scan sees the new dates.

    `_looks_like_an_exam` is deliberately not the gate, for the same reason it is
    not the gate on announcements: it is `_EXAM_WORDS and not _NOT_AN_EXAM`, and
    _NOT_AN_EXAM vetoes an entry for mentioning a review. A row in
    `important_dates` is already the outline saying "this date matters".
    """
    out = []
    rows = CourseOutlineProfile.query.filter_by(user_id=user_id).all()
    for profile in rows:
        try:
            analysis = json.loads(profile.grading_json or "{}")
        except (ValueError, TypeError):
            continue
        course = profile.course_full or "OTHER"
        # The outline's own year is the reference, so "Oct 30" in a Fall outline
        # is this Fall and not whichever one the server happens to be in.
        reference = today
        for item in (analysis.get("important_dates") or [])[:60]:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or item.get("title") or "").strip()
            raw = str(item.get("date") or "").strip()
            if not description and not raw:
                continue
            when = _find_date(raw, reference) or _find_date(description, reference)
            if not when:
                continue
            out.append({
                "course": course,
                "date": when,
                "title": _exam_title(description or raw, course),
                "time": _find_time(raw) or _find_time(description) or "",
                "location": _find_location(description) or "",
                "why": (description or raw)[:240],
            })
    return out


IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# A timetable is a small table, not an outline, and the default vision chain is
# ordered for the harder job. Measured on a real Carleton exam schedule,
# 2026-10-09: 3.7-flash (which leads that chain) timed out at 60s, 3.6-flash
# answered 503 after 8.3s, 3.8-flash took 29.1s -- so roughly 70 seconds was
# spent on dead candidates before anything answered. flash-lite transcribed the
# same image in 2.0s with all seven courses and every date present. The full
# chain still follows, so a lite failure costs nothing but the usual fallback.
# End to end on that image: 106s before, 4.8s after.
def _anchor_undated_year(month, day, today):
    """The year for a row printed as "07-Sep", which states no year at all.

    A course schedule is written for people who already know which term it is,
    so most of them print the day and the month and nothing else. The year is
    then the one thing on the row that nobody wrote down -- and it is exactly
    what the "exams found in your course material" panel got wrong before it was
    deleted: it offered a MATH 1004 test from first year as "Tue, Sep 29" this
    September, ten of them above the calendar.

    So the year is never the model's to choose. The server takes the nearest
    occurrence to the student's own today, which is right for a term schedule
    read during that term in either direction -- 07-Sep lands in the autumn just
    gone, 07-Dec in the December ahead -- and is at worst six months out where a
    guess could be a whole year out.
    """
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue                      # 29 February in a common year
        gap = abs((candidate - today).days)
        if best is None or gap < best[0]:
            best = (gap, candidate)
    return best[1] if best else None


_WEEKDAY_INDEX = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
                  "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
                  "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
                  "sunday": 6, "sun": 6}


def _day_in_stated_week(stated, weekday_name):
    """The actual date of a weekday inside a week a schedule named.

    A great many course schedules are printed by WEEK -- "Week of 19 October" --
    because the lab or tutorial falls on whichever day that student is enrolled
    for. Turning that into a date is arithmetic, and the model proved it cannot
    be trusted with it: asked for the Tuesday of the week of 19 October it
    answered 2026-10-21 (a Wednesday) from a table and 2026-10-20 (correct) from
    the same information written as prose. Two answers to one question, one of
    them a day out, on exactly the screen a student checks to decide what to do.

    The week is anchored on its Monday rather than on the printed date itself,
    so "week of Sunday 18 October" and "week of Monday 19 October" mean the same
    week -- which is what they mean to the student reading the schedule.
    """
    index = _WEEKDAY_INDEX.get(str(weekday_name or "").strip().lower())
    if stated is None or index is None:
        return None
    monday = stated - timedelta(days=stated.weekday())
    return monday + timedelta(days=index)


_TIMETABLE_VISION_MODELS = (["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
                            + _gemini_model_candidates(vision=True))

# Deliberately not "exam timetable": the table that prompted the instruction
# support is a weekly lab/tutorial schedule with a Note column carrying
# "Thanksgiving Monday", "Fall Break" and "Midterm", and the note column is
# where half the meaning lives.
_SCHEDULE_OCR_PROMPT = (
    "Read this screenshot of a university timetable or course schedule exactly. "
    "Transcribe every visible row and preserve the table structure, keeping each "
    "row on its own line with its columns in order. Include column headers, and "
    "include every note or comment column even when most of its cells are empty. "
    "Do not summarise, reorder, omit or infer anything that is not printed. "
    "Return plain text only."
)


@app.route("/api/calendar/from-image", methods=["POST"])
@ai_limit
@login_required
@requires_active
def calendar_from_image():
    """Read a timetable out of a screenshot, following what the student says.

    A university publishes finals as a table -- course, type, date, start, end --
    and a student otherwise types seven of them in by hand, which is both the
    most tedious thing Nexora could ask for and the easiest place to make a
    mistake that matters.

    **The picture is half of it; the sentence beside it is the other half.** The
    table that prompted this is a weekly lab/tutorial schedule, and what the
    owner wanted from it was "for everything written as Tutorial here, treat it
    as Test -- Tutorial 2 would be Test 2 for ELEC". No amount of reading the
    image produces that: the word Test is not printed anywhere on it, and nor is
    the course. An image reader with no instruction can only ever give back what
    the table already says, which the student can already see.

    So `instruction` reaches the same call the image does, and may rename rows,
    change what kind of entry they become, say which course they belong to, and
    drop the rows that are not wanted. Everything the rest of the calendar does
    still holds: the model decides WHAT each row means, the SERVER resolves every
    date, and nothing is written until the student has seen the list and
    confirmed it. `understood` comes back with the plan so a misread instruction
    is visible on the page rather than silently reshaping a term.
    """
    upload = request.files.get("image")
    if upload is None:
        return jsonify({"error": "Attach a screenshot of your timetable."}), 400
    raw = upload.read(IMAGE_MAX_BYTES + 1)
    if not raw:
        return jsonify({"error": "That file was empty."}), 400
    if len(raw) > IMAGE_MAX_BYTES:
        return jsonify({"error": "That image is too large. Keep it under 8 MB."}), 400
    mime = (upload.mimetype or "").split(";")[0].strip().lower()
    if mime not in IMAGE_MIMES:
        return jsonify({"error": "Attach a PNG, JPG, WEBP or GIF."}), 400

    instruction = str(request.form.get("instruction") or "").strip()[:500]
    today = _today_for_request()
    try:
        text = _gemini_image_to_outline_text(raw, mime, prompt=_SCHEDULE_OCR_PROMPT,
                                             models=_TIMETABLE_VISION_MODELS)
    except Exception as exc:
        app.logger.warning("Timetable OCR failed: %s", exc)
        return jsonify({"error": ("Nexora's AI is at its limit right now. Try again "
                                  "later, or add the dates with + Add event."
                                  if _looks_like_quota_error(exc) else
                                  "Nexora could not read that image.")}), 503
    if not str(text or "").strip():
        return jsonify({"error": "Nexora could not read any text in that image."}), 422

    codes = sorted(_known_course_codes(current_user.id))
    schema = {"understood": "",
              "rows": [{"course": "", "title": "", "kind": "exam", "date": "",
                        "year_printed": False, "names_a_week": False, "weekday": "",
                        "time": "", "location": "", "skip": False, "why": ""}]}
    rules = [
        f"Today is {today.isoformat()}. Every date is ISO yyyy-mm-dd.",
        "The text below was transcribed from a student's timetable or course "
        "schedule. Turn each row that schedules something on a date into one "
        "entry.",
        "Use the START date and START time. Ignore end times and durations.",
        'Set skip true for a row that schedules nothing -- "No sessions", a '
        "break or holiday with no activity, a blank date, a placeholder.",
        # The course is as often named in the INSTRUCTION as in the table -- a
        # schedule printed by one course rarely repeats its own code on every
        # row -- and it is named loosely there: "for elec" has to resolve to
        # ELEC2501B, or the entries land under a code that joins to no outline,
        # no grades and no study plan.
        "course should match one of the student's own codes whenever the row or "
        "the instruction names that course, however loosely it is written and "
        "even without the section letter. Theirs are: "
        + (", ".join(codes) or "none imported yet") + ". "
        "If none matches, use the code exactly as it is printed.",
        "title is what the entry should be called on a calendar. Default to "
        "what the row itself calls the activity.",
        # A university's exam schedule names the COURSE, the date, the time and
        # the room, and never the activity -- so the default above would title
        # every row with a course code, and `_major_assessment_label` would see
        # no word that means an exam. The FINAL badge, the outlined day and the
        # planner's revision ladder all hang off that word being there.
        'A FINAL EXAM SCHEDULE is a table of courses each with a date, a start '
        'time and a room, naming no other activity. Title every row of one '
        '"Final Exam", set kind "exam", and put the building and room in '
        "location.",
        'kind is "exam" for anything sat or graded, "event" otherwise.',
        # The year is the one part of the date a course schedule usually never
        # prints, so the server replaces it. Asking for this flag is cheaper
        # and far more reliable than asking the model not to guess.
        'year_printed is true only if the row itself shows a year. A row '
        'reading "07-Sep" shows none, so it is false -- still give your best '
        "date, and the server will fix the year.",
        # Measured: asked for the Tuesday of "Week of 19 October" the model
        # answered 2026-10-21 (a Wednesday) from a table and 2026-10-20
        # (correct) from the same facts written as prose. It must report the
        # week and let the server count the days.
        'names_a_week is true when the row gives a WEEK rather than a day -- '
        '"Week of 19 October", "w/c 19 Oct". Put that week\'s own date in date '
        "and do NOT work out the weekday yourself.",
        'weekday is the day the student\'s instruction asks for inside that '
        'week ("Monday".."Sunday"), or "" if they did not say. The server turns '
        "the week and the weekday into the date.",
        'time is 24-hour HH:MM taken from the start time, or "" if blank.',
        "why is the row as printed, so the student can check it.",
    ]
    if instruction:
        rules += [
            "",
            "THE STUDENT'S INSTRUCTION outranks the defaults above. It may "
            "rename rows, change their kind, name the course, and say which "
            "rows to leave out. Apply it to every row it covers.",
            'For example "treat every Tutorial as a Test" turns a row reading '
            '"Tutorial (2)" into title "Test 2", kind "exam".',
            "INSTRUCTION: " + instruction,
            "",
            "understood: one sentence to the student saying what you took the "
            "instruction to mean, naming the rename and the course if they gave "
            "one. Say so plainly if you could not follow part of it.",
        ]
    else:
        rules += ["", 'understood: "".']
    rules += ["", "TIMETABLE TEXT:", str(text)[:12000]]
    try:
        parsed = _gemini_json("You turn a transcribed exam timetable into JSON rows. "
                              "Output only JSON.",
                              "Return strict JSON matching this schema:\n"
                              + json.dumps(schema, indent=2) + "\n\n" + "\n".join(rules),
                              # Turning a transcribed table into rows is
                              # mechanical, so it starts on flash-lite rather
                              # than walking the thinking chain.
                              max_tokens=2000, mode="fast")
    except Exception as exc:
        app.logger.warning("Timetable parse failed: %s", exc)
        return jsonify({"error": "Nexora read the image but could not make sense of "
                                 "the table. Try a clearer screenshot."}), 503
    if isinstance(parsed, list):
        parsed = {"rows": parsed}
    rows = (parsed or {}).get("rows") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        rows = []

    changes, notes, skipped, undated, already, past = [], [], 0, 0, 0, 0
    for item in rows[:40]:
        if not isinstance(item, dict):
            continue
        if item.get("skip"):
            skipped += 1
            continue
        day = parse_iso_date(str(item.get("date") or "").strip())
        if not day:
            undated += 1
            continue
        # The year on a row that never printed one is the model's guess, and a
        # guess here is a whole year out rather than a day out. See
        # _anchor_undated_year: the server takes the nearest occurrence instead.
        if not item.get("year_printed"):
            anchored = _anchor_undated_year(day.month, day.day, today)
            if anchored:
                day = anchored
        # A row that names a week, and an instruction that names the day inside
        # it, are two facts; the date between them is arithmetic and belongs
        # here. Done after the year is settled, because the week's own date is
        # usually printed without one.
        if item.get("names_a_week"):
            in_week = _day_in_stated_week(day, item.get("weekday"))
            if in_week:
                day = in_week
        # Resolved on the server, not left to the prompt. "for elec" came back
        # as ELEC, and a row filed under ELEC when the student's course is
        # ELEC2501B joins to no outline, no grades and no study plan -- the same
        # dead end as OTHER, wearing a better label.
        course = _resolve_known_course(str(item.get("course") or ""),
                                       current_user.id) or "OTHER"
        title = str(item.get("title") or "").strip()[:200] or "Scheduled"
        if _already_on_the_calendar(current_user.id, course, day, title):
            already += 1
            continue
        # Same mapping _recurrence_events uses -- EVENT_KINDS is due|exam|event
        # and a test is an exam. `due` is deliberately unreachable from here: it
        # means an LMS deadline, and this row came from a picture.
        kind = str(item.get("kind") or "event").strip().lower()
        if kind in {"test", "midterm", "final", "quiz", "exam"}:
            kind = "exam"
        else:
            kind = "event"
        time_raw = str(item.get("time") or "").strip()
        if time_raw and not _TIME_RE.match(time_raw):
            time_raw = ""
        gone = day < today
        if gone:
            past += 1
        changes.append({
            "op": "add", "title": title, "date": day.isoformat(), "time": time_raw,
            "course": course, "kind": kind,
            "location": str(item.get("location") or "").strip()[:200],
            "source_title": "Timetable",
            "why": str(item.get("why") or "").strip()[:240],
            # A term schedule always carries weeks that have already happened.
            # They are offered rather than dropped -- a row the student can see
            # in their own picture must not vanish without a word -- but the
            # sheet leaves them unticked, so nothing from last month is added by
            # pressing one button.
            "past": gone,
            "label": f"Add “{title}” on {day.isoformat()}"
                     + (f" at {time_raw}" if time_raw else "")
                     + f" · {course}",
        })

    if skipped:
        notes.append(f"{skipped} row{'s' if skipped != 1 else ''} scheduled nothing.")
    if undated:
        notes.append(f"{undated} row{'s' if undated != 1 else ''} stated no date.")
    if already:
        notes.append(f"{already} {'is' if already == 1 else 'are'} already on your calendar.")
    if past:
        notes.append(f"{past} {'date has' if past == 1 else 'dates have'} already "
                     "passed, so they start unticked.")
    if not changes and not notes:
        notes.append("Nexora found nothing dated in that image.")
    understood = ""
    if isinstance(parsed, dict):
        understood = str(parsed.get("understood") or "").strip()[:400]
    return jsonify({"ok": True, "preview": True, "changes": changes,
                    "counts": {"add": len(changes)} if changes else {},
                    "understood": understood, "notes": notes})


@app.route("/api/calendar/scan-announcements", methods=["POST"])
@ai_limit
@login_required
@requires_active
def scan_announcements():
    """Find dates your instructors announced but never put on the calendar.

    The announcement that prompted this said a midterm "is set to Saturday Nov 7
    at 2PM". Brightspace publishes that as prose in an announcement and never as
    a calendar item, so Nexora had every word of it and the calendar stayed
    empty -- which, as the owner put it, is most of the point of the calendar.

    Nothing is written. It returns changes in the same shape the calendar
    assistant uses, so the student confirms them through the same path.
    """
    data = request.get_json(silent=True) or {}
    today = _today_for_request()
    zone = _user_zone(current_user)
    # Dates the outline pipeline already extracted. Deterministic, so these cost
    # nothing and are collected whether or not anything is announced.
    outline_changes, outline_past, outline_have = [], 0, 0
    for found in _outline_dated_assessments(current_user.id, today, zone):
        day = found["date"]
        if day < today:
            outline_past += 1
            continue
        if day > today + timedelta(days=ANNOUNCEMENT_HORIZON_DAYS):
            continue
        if _already_on_the_calendar(current_user.id, found["course"], day, found["title"]):
            outline_have += 1
            continue
        outline_changes.append({
            "op": "add", "title": found["title"], "date": day.isoformat(),
            "time": found["time"], "course": found["course"], "kind": "exam",
            "location": found["location"], "source_title": "Course outline",
            "why": found["why"],
            "label": f"Add “{found['title']}” on {day.isoformat()}"
                     + (f" at {found['time']}" if found["time"] else "")
                     + f" · {found['course']}",
        })

    candidates = _announcement_candidates(current_user.id, today, zone)
    if not candidates:
        notes = []
        if outline_past:
            notes.append(f"{outline_past} date{'s' if outline_past != 1 else ''} in your "
                         "outlines have already passed.")
        if outline_have:
            notes.append(f"{outline_have} {'is' if outline_have == 1 else 'are'} already "
                         "on your calendar.")
        if not outline_changes and not notes:
            notes.append("No announcement or course outline states a date that is not "
                         "already on your calendar.")
        return jsonify({"ok": True, "preview": True, "changes": outline_changes,
                        "counts": {"add": len(outline_changes)} if outline_changes else {},
                        "understood": "", "notes": notes})

    listing = []
    for i, (row, ref, blob) in enumerate(candidates):
        listing.append(f"[{i}] course={row.course_full or 'OTHER'} "
                       f"published={ref.isoformat()}\n{blob[:900]}")
    schema = {"found": [{"i": 0, "is_dated_assessment": True, "title": "",
                         "kind": "exam", "date": "", "time": "", "location": "",
                         "why": ""}]}
    rules = [
        f"Today is {today.isoformat()}. Every date is ISO yyyy-mm-dd.",
        "Each block below is one announcement. Decide whether it STATES the date "
        "of a test, midterm, exam, quiz or other sitting the student must attend.",
        'Set is_dated_assessment false when it only mentions one in passing -- '
        '"the midterm will be covered in the review", "marks are released" -- or '
        "when the date is of something else, like a lecture or an office hour.",
        "A year is often missing. Use the announcement's published date to decide "
        "which year is meant; it is almost always the next occurrence after it.",
        "title is what a student would want to see on their calendar, short, and "
        "naming the assessment rather than quoting the announcement.",
        'kind is "exam" for any sitting, "due" for something to hand in.',
        'time is 24-hour HH:MM, or "" if none is stated. Never invent one.',
        "why is a short quote from the announcement that states the date.",
        "Leave out any announcement that states no date.",
        "",
        "ANNOUNCEMENTS:",
        "\n\n".join(listing),
    ]
    # The clock moves when a CALL is about to be spent, not when the page asks.
    # An outline-only scan costs nothing and must not use up the budget that
    # stops a student's ten calendar visits becoming ten Gemini calls.
    if data.get("auto"):
        current_user.announce_scan_at = datetime.utcnow()
        db.session.commit()

    try:
        parsed = _gemini_json("You find dated assessments in course announcements. "
                              "Output only JSON.",
                              "Return strict JSON matching this schema:\n"
                              + json.dumps(schema, indent=2) + "\n\n" + "\n".join(rules),
                              max_tokens=1600, mode="quick")
    except Exception as exc:
        app.logger.warning("Announcement scan failed: %s", exc)
        return jsonify({"error": ("Nexora's AI is at its limit right now. Try again "
                                  "later, or add the date with + Add event."
                                  if _looks_like_quota_error(exc) else
                                  "Nexora could not read your announcements just now.")}), 503
    if isinstance(parsed, list) and parsed:
        parsed = {"found": parsed}
    found = (parsed or {}).get("found") if isinstance(parsed, dict) else None
    if not isinstance(found, list):
        found = []

    changes, notes, skipped_past, skipped_have = [], [], 0, 0
    for item in found[:ANNOUNCEMENT_SCAN_MAX]:
        if not isinstance(item, dict) or not item.get("is_dated_assessment"):
            continue
        try:
            row, ref, _blob = candidates[int(item.get("i"))]
        except (TypeError, ValueError, IndexError):
            continue
        day = parse_iso_date(str(item.get("date") or "").strip())
        if not day:
            continue
        # Future-dated only, and never further out than a year -- the two rules
        # the old version broke. A model reading "Nov 7" out of a 2024
        # announcement would otherwise put it on next month's calendar.
        if day < today:
            skipped_past += 1
            continue
        if day > today + timedelta(days=ANNOUNCEMENT_HORIZON_DAYS):
            continue
        course = row.course_full or "OTHER"
        title = str(item.get("title") or "").strip()[:200] or (row.title or "Exam")
        if _already_on_the_calendar(current_user.id, course, day, title):
            skipped_have += 1
            continue
        kind = str(item.get("kind") or "exam").strip().lower()
        if kind not in EVENT_KINDS:
            kind = "exam"
        time_raw = str(item.get("time") or "").strip()
        if time_raw and not _TIME_RE.match(time_raw):
            time_raw = ""
        changes.append({
            "op": "add", "title": title, "date": day.isoformat(), "time": time_raw,
            "course": course, "kind": kind,
            "location": str(item.get("location") or "").strip()[:200],
            "source_title": row.title or "",
            "why": str(item.get("why") or "").strip()[:240],
            "label": f"Add “{title}” on {day.isoformat()}"
                     + (f" at {time_raw}" if time_raw else "")
                     + f" · {course}",
        })

    # Outline dates first: they are the course's own statement of when things
    # are, and an announcement is usually a change to one of them.
    seen = {(c["course"], c["date"]) for c in outline_changes}
    changes = outline_changes + [c for c in changes if (c["course"], c["date"]) not in seen]
    skipped_past += outline_past
    skipped_have += outline_have

    if skipped_past:
        notes.append(f"{skipped_past} stated a date that has already passed.")
    if skipped_have:
        notes.append(f"{skipped_have} {'is' if skipped_have == 1 else 'are'} already on your calendar.")
    if not changes and not notes:
        notes.append("Nothing new. Every date your announcements state is already "
                     "on your calendar.")
    return jsonify({"ok": True, "preview": True, "changes": changes,
                    "counts": {"add": len(changes)} if changes else {},
                    "understood": "", "notes": notes,
                    "scanned": len(candidates)})


@app.route("/api/calendar/ai-events", methods=["POST"])
@ai_limit
@login_required
@requires_active
def calendar_ai_events():
    """Say what you want done to your calendar; check the changes; apply them.

    This is an assistant with access to the calendar, not a form with a chat box
    on it. It adds, moves, deletes, retimes and renames, and when a request is
    none of those it says so -- the first version answered "change the colour for
    CCDP2100" by offering to create an event called "Change course color", which
    is the worst kind of wrong: confident, plausible and useless.

    Two calls, always. The first reads the request against the student's real
    entries and returns a PREVIEW of every change. The second applies the plan
    they confirmed. Deletes are in scope now, so acting on a model's first guess
    is out of the question.

    The split that makes it safe is the one the planner uses: the model chooses
    WHAT to do and to WHICH entries, picking from ids it was given; the server
    works out every date, and re-checks every id on the way back in.
    """
    data = request.get_json(silent=True) or {}
    today = _today_for_request()

    # The announcement scan shares this endpoint's preview/confirm shape, so the
    # sheet, the Apply button and the ownership re-check are one implementation.
    if data.get("scan"):
        return scan_announcements()

    if data.get("confirm"):
        changes = data.get("changes")
        if not isinstance(changes, list) or not changes:
            return jsonify({"error": "Nothing to apply."}), 400
        done = _calendar_apply(changes, current_user.id)
        if not any(done.values()):
            return jsonify({"error": "None of those changes could be applied."}), 400
        return jsonify({"ok": True, "done": done})

    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Say what you want doing to your calendar."}), 400
    if len(text) > 2000:
        return jsonify({"error": "That is too long. Ask for one thing at a time."}), 400

    summary = _calendar_summary(current_user.id, today)
    sample = _calendar_sample(current_user.id, today)
    codes = sorted(_known_course_codes(current_user.id))

    schema = {
        "understood": "", "unsupported": "",
        "operations": [{"op": "add", "title": "", "course": "", "kind": "exam",
                        "time": "", "location": "", "start": "", "repeat": "once",
                        "count": 0, "until": "", "skip_dates": [], "skip_ranges": [],
                        "on_excluded": "shift",
                        "match": {"course": "", "kind": "", "title_contains": "",
                                  "from": "", "to": ""},
                        "ids": [], "to": "", "by_days": 0,
                        "replace": {"from": "", "to": ""}, "title_new": "",
                        "course_new": ""}],
    }
    rules = [
        f"Today is {today.isoformat()}. Every date is ISO yyyy-mm-dd.",
        'Each operation has an "op" of: add, move, delete, retime, rename, '
        "recourse.",
        "ADD: set title, course, kind, time, start. For something repeating also "
        'set repeat ("weekly", "biweekly", "monthly"), and count or until. '
        "skip_ranges [[from,to],...] and skip_dates are holidays they excluded. "
        'on_excluded is "shift" when a date landing in a gap should move after it '
        'and the rest carry on ("no class that week", "we continue after"), and '
        '"drop" only when that one is cancelled outright. Default "shift".',
        "",
        "MOVE, DELETE, RETIME, RENAME and RECOURSE choose their entries in ONE of "
        "two ways, and you do not need to list anything to use them:",
        '  (a) "match": a filter the server runs over ALL their entries. Set any '
        'of course, kind, title_contains, from, to. This is how you do "all the '
        'exams of ELEC2501" or "every Module quiz" -- you do NOT need ids, and '
        'you must NOT refuse for want of ids. At least one field must be set.',
        '  (b) "ids": only when they mean specific entries you can see in the '
        "sample below. Never invent an id.",
        "",
        'MOVE takes "to" (one entry onto a date) or "by_days" (shift them all).',
        "RETIME takes time.",
        'RENAME takes "replace": {"from": ..., "to": ...} -- a substitution inside '
        'each title, so "Elec Lab 4" becomes "Elec Test 4" and the numbering '
        'survives. Use "title_new" ONLY when exactly one entry is meant; giving '
        "many entries one name would make them identical.",
        # Entries land on OTHER whenever nothing named a course, and a student
        # looking at them wants exactly this and nothing else. Without the verb
        # the model promised the change in `understood` and emitted an op that
        # was silently dropped.
        'RECOURSE changes which course entries belong to. Put the new code in '
        '"course_new", and use "match" to choose them -- match.course is which '
        'course they are on NOW ("OTHER" is allowed there), course_new is where '
        "they are going. This is the op for \"these are ELEC2501, not OTHER\".",
        "",
        'kind is "exam" for tests, midterms, finals and quizzes, "due" for work to '
        'hand in, otherwise "event".',
        "course must be one of the student's own codes or \"\". Theirs are: "
        + (", ".join(codes) or "none imported yet") + ".",
        "Never invent a course code or an id.",
        'time is 24-hour HH:MM, or "" if they gave none. Never guess a time.',
        "If the request is not something a calendar can do -- changing colours, "
        "settings or grades, anything that is not an entry and a date -- put why "
        'in "unsupported", leave operations empty, and do NOT invent an entry. '
        "Needing to act on many entries at once is NOT a reason to call something "
        "unsupported; use a match filter.",
        "understood is one short sentence to the student saying what you are "
        "about to do, including anything you were unsure about.",
        "",
        "WHAT THEY HAVE (course | kind | count | dates | example titles):",
        summary or "(nothing imported yet)",
        "",
        "SOME ENTRIES WITH IDS (id|date|time|course|kind|title):",
        sample or "(nothing in this window)",
    ]
    prompt = ("Return strict JSON matching this schema:\n" + json.dumps(schema, indent=2)
              + "\n\n" + "\n".join(rules) + "\n\nREQUEST: " + text)
    try:
        parsed = _gemini_json("You turn a student's request into calendar operations. "
                              "Output only JSON.", prompt, max_tokens=1800, mode="quick")
    except Exception as exc:
        app.logger.warning("Calendar AI failed: %s", exc)
        message = ("Nexora's AI is at its limit right now. Use + Add event, or edit "
                   "an entry by clicking it."
                   if _looks_like_quota_error(exc) else
                   "Nexora could not read that. Try naming the course, the dates, "
                   "and what you want done.")
        return jsonify({"error": message}), 503
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]          # flash-lite sometimes wraps the object in a list
    if not isinstance(parsed, dict):
        return jsonify({"error": "Nexora could not read that."}), 503

    understood = str(parsed.get("understood") or "").strip()
    unsupported = str(parsed.get("unsupported") or "").strip()
    if unsupported:
        return jsonify({"error": unsupported, "understood": understood,
                        "unsupported": True}), 422

    changes, notes = _calendar_plan(parsed.get("operations"), current_user.id, today)
    if not changes:
        # Usually this is not a misreading -- it is that nothing matched. Saying
        # "could not work out any changes" sends a student off to rephrase a
        # request that was perfectly clear and simply had no entries to act on.
        return jsonify({"error": "Nexora found nothing to change. It can only act on "
                                 "entries already in your calendar, between a month "
                                 "back and about five months ahead. "
                                 + (" ".join(notes) or ""),
                        "understood": understood}), 422

    counts = {}
    for c in changes:
        counts[c["op"]] = counts.get(c["op"], 0) + 1
    return jsonify({"ok": True, "preview": True, "understood": understood,
                    "notes": notes, "counts": counts, "changes": changes})


# ============================
# Finding exams a student has not put on their calendar
# ============================
# The dates are already in the building: an analysed outline records
# important_dates, and announcements say "Midterm 2 is on Oct 30 at 6pm in
# Southam". Neither ever became a calendar entry, so a student who had done
# everything Nexora asked still had to copy their own midterms across by hand.
#
# Deliberately deterministic. Dates are the one thing a model must not be
# allowed to be creative about, and this runs on every page view -- a Gemini
# call here would burn the quota the outline pipeline needs.

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "Midterm 2", "Final Exam", "Unit Test 1", "Term Test", "Quiz 3"
_EXAM_WORDS = re.compile(
    r"\b((?:unit|term)\s*\d*\s*test|final\s+exam(?:ination)?|final|"
    r"midterm(?:\s+exam)?|mid-term|test|quiz|exam(?:ination)?)\b", re.I)
# Words that make an "exam" mention about something other than a sitting.
_NOT_AN_EXAM = re.compile(
    r"\b(practice|review|prep(?:aration)?|sample|past|mock|solutions?|"
    r"answers?|study\s+guide|office\s+hours|tutorial\s+on)\b", re.I)

_DATE_PATTERNS = [
    # 2026-10-30
    (re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b"), ("y", "m", "d")),
    # Oct 30, 2026 / October 30 2026 / Oct. 30
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", re.I), ("mon", "d", "y?")),
    # 30 October 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
                r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(20\d{2}))?\b", re.I),
     ("d", "mon", "y?")),
]

_TIME_PATTERNS = [
    # 6:30 pm / 6 pm
    re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I),
    # 18:30
    re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b"),
]

_LOCATION_RE = re.compile(
    r"\b(?i:in|at|room|location)\b[:\s]+(?i:the\s+)?"
    r"((?:[A-Z][A-Za-z.]*\s+){0,3}"
    r"(?:Hall|Building|Centre|Center|Theatre|Theater|Gym|Room|Fieldhouse)"
    r"(?:\s+\d{1,4}[A-Z]?)?|[A-Z]{2,4}\s?\d{2,4})\b")


def _guess_year(month, day, reference=None):
    """An outline says "Oct 30" and means the term in progress. Assume the next
    occurrence, so a date in the past few weeks stays in the past rather than
    jumping a year forward."""
    reference = reference or date.today()
    for year in (reference.year, reference.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= reference - timedelta(days=45):
            return candidate
    return None


def _find_date(text, reference=None):
    for pattern, order in _DATE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        groups = match.groups()
        try:
            if order[0] == "y":
                return date(int(groups[0]), int(groups[1]), int(groups[2]))
            fields = dict(zip(order, groups))
            month = _MONTHS[fields["mon"][:3].lower()]
            day = int(fields["d"])
            year = fields.get("y?")
            if year:
                return date(int(year), month, day)
            return _guess_year(month, day, reference)
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _find_time(text):
    for pattern in _TIME_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        groups = match.groups()
        try:
            hour = int(groups[0])
            minute = int(groups[1] or 0)
        except (TypeError, ValueError):
            continue
        if len(groups) == 3 and groups[2]:
            meridiem = groups[2].lower()
            if hour == 12:
                hour = 0 if meridiem == "a" else 12
            elif meridiem == "p":
                hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def _find_location(text):
    match = _LOCATION_RE.search(text or "")
    return match.group(1).strip()[:255] if match else ""


def _looks_like_an_exam(text):
    text = str(text or "")
    return bool(_EXAM_WORDS.search(text)) and not _NOT_AN_EXAM.search(text)


# A quiz is not a midterm. _EXAM_WORDS above deliberately includes "quiz"
# because the old suggestion panel wanted anything that smelled like a sitting;
# what has to stand out on a calendar is the handful of things that decide the
# grade. Flagging every pre-lab quiz as major is the same as flagging nothing.
_MAJOR_WORDS = [
    ("FINAL", re.compile(r"\bfinal(\s+(exam|examination|test|assessment))?\b", re.I)),
    ("MIDTERM", re.compile(r"\b(midterm|mid-term|mid\s+term)(\s+(exam|examination|test))?\b", re.I)),
    ("TEST", re.compile(r"\b((?:unit|term|chapter)\s*\d*\s*test|test\s*\d*)\b", re.I)),
    ("EXAM", re.compile(r"\bexam(ination)?\b", re.I)),
]
# Task.kind, when the student added it by hand.
_MAJOR_KINDS = {"exam", "midterm", "final", "test"}


def _major_assessment_label(kind, title):
    """"FINAL" / "MIDTERM" / "TEST" / "EXAM" for something that decides a
    grade, or "" for ordinary work.

    The title is the only signal that works for imported deadlines, which is
    nearly all of them: Brightspace hands over every assignment and quiz as the
    same kind of thing, so a midterm arrives as `kind='due'` exactly like a
    discussion-board post. A student scanning a month needs to see which two
    squares matter, and `kind` alone would show them none.

    _NOT_AN_EXAM still applies -- "midterm review session" and "practice final"
    are not the sitting, and flagging them is how a calendar stops being
    trusted.
    """
    title = str(title or "")
    if _NOT_AN_EXAM.search(title):
        return ""
    kind = str(kind or "").strip().lower()
    for label, pattern in _MAJOR_WORDS:
        if pattern.search(title):
            return label
    if kind in _MAJOR_KINDS:
        return "EXAM" if kind == "exam" else kind.upper()
    return ""


# Eight, because past eight a student cannot hold the mapping in their head and
# the legend is doing all the work anyway.
COURSE_COLOUR_COUNT = 8


def _course_colour_map(codes):
    """One colour per course, and the SAME colour on every screen.

    Assigned by position in the sorted list rather than by hashing the code.
    The hash version (`sum(ord(ch) for ch in course) % 8`) collided: any two
    codes whose letters happen to sum alike shared a colour, so a student with
    four courses could see two of them in the same paint and a legend would be
    a lie. Sorted position cannot collide while there are eight or fewer.

    Stable within a term, which is what matters -- a colour shifts only when
    the set of courses itself changes, and the legend on the page shows it.
    """
    return {code: i % COURSE_COLOUR_COUNT
            for i, code in enumerate(sorted({str(c or "").strip() for c in codes if str(c or "").strip()}))}


def _student_course_colours(user_id, extra=()):
    """The colour map, identical on every screen that paints a course.

    One definition of the FUNCTION was not enough. The calendar built its map
    from the courses with dated work and the study plan from its own mix of
    enrolments, sessions and timetable blocks, so the two sorted lists differed
    and SYSC2004 came out blue on one page and green on the other -- caught by
    reading both in the browser, one screenshot apart.

    The primary set is courses with dated work, because that is what both pages
    are actually about. Anything else -- a course on the timetable that has no
    deadlines yet -- is appended AFTER, so it can never shift a colour that is
    already in use somewhere else.
    """
    primary = sorted({str(t.course_full or "").strip()
                      for t in Task.query.filter_by(user_id=user_id).all()
                      if str(t.course_full or "").strip()})
    out = {code: i % COURSE_COLOUR_COUNT for i, code in enumerate(primary)}
    for code in sorted({str(c or "").strip() for c in extra if str(c or "").strip()} - set(out)):
        out[code] = len(out) % COURSE_COLOUR_COUNT
    # A choice the student made outranks the automatic map, and is applied HERE
    # rather than at any one call site -- this function exists because the
    # calendar and the study plan once built their own maps and disagreed about
    # what colour SYSC2004 was. An override honoured on one screen and not the
    # other would be that same bug, reintroduced by the feature meant to fix it.
    for row in CourseColour.query.filter_by(user_id=user_id).all():
        code = str(row.course_full or "").strip()
        if code:
            out[code] = int(row.colour_index or 0) % COURSE_COLOUR_COUNT
    return out


def _exam_title(text, course):
    """A short name for the entry: the exam phrase itself, plus the number that
    tells two of them apart.

    Reading forward from the match with a character class was too eager -- for
    "Unit 1 Test in week 5" it stopped at "Unit 1" and dropped the word that
    said what it was.
    """
    match = _EXAM_WORDS.search(str(text or ""))
    if not match:
        return "Exam"
    name = re.sub(r"\s+", " ", match.group(0)).strip(" -")
    # "Midterm 2" -- the number follows the phrase rather than sitting inside it.
    trailing = re.match(r"\s*#?\s*(\d{1,2}|[IVX]{1,4})\b", str(text)[match.end():])
    if trailing and not re.search(r"\d", name):
        name = f"{name} {trailing.group(1)}"
    return name.title()[:120] or "Exam"


def _detect_course_exams(user, course, profile_json, reference=None):
    """Candidates from one course's analysed outline and its announcements."""
    found = []

    try:
        analysis = json.loads(profile_json or "{}")
    except Exception:
        analysis = {}
    for item in (analysis.get("important_dates") or [])[:60]:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or item.get("title") or "")
        if not _looks_like_an_exam(description):
            continue
        when = _find_date(str(item.get("date") or ""), reference) or _find_date(description, reference)
        if not when:
            continue
        found.append({
            "title": _exam_title(description, course), "course": course,
            "date": when.isoformat(), "time": _find_time(description) or "",
            "location": _find_location(description), "kind": "exam",
            "source": "Course outline", "detail": description[:300],
        })

    announcements = CourseContent.query.filter_by(
        user_id=user.id, course_full=course).filter(
        CourseContent.content_type.in_(["announcement", "news"])).order_by(
        CourseContent.updated_at.desc()).limit(40).all()
    for row in announcements:
        text = f"{row.title or ''}. {(row.content_text or '')[:1200]}"
        if not _looks_like_an_exam(text):
            continue
        when = _find_date(text, reference)
        if not when:
            continue
        found.append({
            "title": _exam_title(text, course), "course": course,
            "date": when.isoformat(), "time": _find_time(text) or "",
            "location": _find_location(text), "kind": "exam",
            "source": "Announcement", "detail": (row.title or "")[:300],
        })
    return found


def _existing_exam_keys(user):
    """What is already on the calendar, so a suggestion is never a duplicate."""
    keys = set()
    for row in Task.query.filter_by(user_id=user.id).all():
        if (row.sync_state or "") == "dismissed":
            continue
        keys.add((str(row.course_full or "").upper(),
                  str(row.due_raw or ""),
                  re.sub(r"[^a-z0-9]+", "", str(row.title or "").lower())[:24]))
    return keys


def _exam_suggestions(user, reference=None):
    profiles = CourseOutlineProfile.query.filter_by(user_id=user.id).all()
    by_course = {p.course_full: p.grading_json for p in profiles}
    courses = {c.course_code for c in SyncedCourse.query.filter_by(user_id=user.id).all()
               if c.course_code and not bool(getattr(c, "is_hidden", False))}
    courses.update(by_course.keys())

    existing = _existing_exam_keys(user)
    today = reference or date.today()
    out, seen = [], set()
    for course in sorted(courses):
        for item in _detect_course_exams(user, course, by_course.get(course), reference):
            when = parse_iso_date(item["date"])
            if not when or when < today:
                continue      # a sitting that has already happened is not news
            key = (course.upper(), item["date"],
                   re.sub(r"[^a-z0-9]+", "", item["title"].lower())[:24])
            if key in existing or key in seen:
                continue
            seen.add(key)
            out.append(item)
    out.sort(key=lambda x: (x["date"], x["course"]))
    return out[:25]


@app.route("/api/exams/suggestions")
@login_required
def exam_suggestions():
    items = _exam_suggestions(current_user)
    return jsonify({"ok": True, "count": len(items), "suggestions": items})


@app.route("/api/exams/accept", methods=["POST"])
@login_required
@requires_active
def accept_exam_suggestions():
    """Create the ones the student ticked. Never automatic: these are read out
    of prose by a regex, and a wrong exam date is worse than no exam date."""
    data = request.get_json(silent=True) or {}
    wanted = data.get("events")
    if not isinstance(wanted, list) or not wanted:
        return jsonify({"error": "Nothing was selected."}), 400
    existing = _existing_exam_keys(current_user)
    created = []
    for item in wanted[:25]:
        if not isinstance(item, dict):
            continue
        fields, error = _event_fields({
            "title": item.get("title"), "date": item.get("date"),
            "time": item.get("time") or "", "course": item.get("course") or "OTHER",
            "kind": "exam", "location": item.get("location") or "",
            "notes": item.get("detail") or "",
        })
        if error:
            continue
        key = (str(fields.get("course_full", "OTHER")).upper(), fields["due_raw"],
               re.sub(r"[^a-z0-9]+", "", fields["title"].lower())[:24])
        if key in existing:
            continue
        existing.add(key)
        base = fields["title"]
        task = Task(
            id=uuid4().hex, user_id=current_user.id,
            group_id=make_group_id(fields.get("course_full", "OTHER"), base),
            base_title=base, is_deadline=True, source="manual", sync_state="active",
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **fields,
        )
        db.session.add(task)
        created.append(_event_json(task))
    db.session.commit()
    return jsonify({"ok": True, "created": created, "count": len(created)})


# ============================
# The study planner
# ============================
# Nexora can do this properly because it already knows three things a generic
# planner never does: when the student is in class, when each piece of work is
# due, and what each piece is worth. So time goes where it moves the grade,
# instead of being spread evenly over courses.
#
# The scheduling is arithmetic, not a model. A plan has to be the same plan
# when you look at it twice, has to explain why each block is there, and must
# not cost quota to redraw. Gemini reads the timetable image; nothing more.

STUDY_HORIZON_DAYS = 14
# Days before an exam that revision may start. Matches the top rung of
# _EXAM_LADDER: starting earlier than the first stage has nothing to say.
EXAM_REVISION_WINDOW = 14
MAX_CONSECUTIVE_SAME = 2       # sessions in a row on one thing before switching

# Rough hours of work a thing deserves, before weight is taken into account.
# A student can always drag more or less at it; these only set proportions.
_BASE_EFFORT = {
    "exam": 6.0, "final": 8.0, "midterm": 5.0, "test": 3.0, "quiz": 1.5,
    "assignment": 3.0, "project": 6.0, "lab": 2.0, "report": 3.0,
    "essay": 5.0, "presentation": 3.0, "reading": 1.5, "event": 0.0,
}

_KIND_WORDS = [
    ("final", re.compile(r"\bfinal\b", re.I)),
    ("midterm", re.compile(r"\bmid[- ]?term\b", re.I)),
    ("exam", re.compile(r"\bexam(?:ination)?\b", re.I)),
    ("test", re.compile(r"\btest\b", re.I)),
    ("quiz", re.compile(r"\bquiz\b", re.I)),
    ("project", re.compile(r"\bproject|milestone\b", re.I)),
    ("essay", re.compile(r"\bessay|paper\b", re.I)),
    ("presentation", re.compile(r"\bpresentation|seminar\b", re.I)),
    ("lab", re.compile(r"\blab\b", re.I)),
    ("report", re.compile(r"\breport\b", re.I)),
    ("reading", re.compile(r"\breading|chapter\b", re.I)),
    ("assignment", re.compile(r"\bassignment|problem\s+set|homework\b", re.I)),
]

# What to actually do in the block. "Study BIOL for 90 minutes" is the advice
# that makes plans useless; a verb and an object is something a person can start.
_FOCUS = {
    "final": "Work through past questions and the topics you score worst on",
    "midterm": "Redo the hardest problems from the covered modules, closed-book",
    "exam": "Practise questions under time, then review what you got wrong",
    "test": "Practise questions from the covered material",
    "quiz": "Skim the module and answer the practice questions",
    "assignment": "Draft the next section and note what you are stuck on",
    "project": "Move one deliverable forward and commit what you finish",
    "essay": "Outline, then write one section without editing",
    "presentation": "Build the next two slides and say them out loud once",
    "lab": "Read the manual section and pre-answer the questions",
    "report": "Write up the results section from your data",
    "reading": "Read actively and write five questions on it",
    "work": "Make progress and note where you got stuck",
}


def _item_kind(title, category=""):
    hay = f"{title} {category}"
    for name, pattern in _KIND_WORDS:
        if pattern.search(hay):
            return name
    return "work"


def _assessment_stem(name):
    """A key that matches a numbered instance to its group.

    An outline lists "Unit tests" worth 30; the deadlines are "Unit 1 Test",
    "Unit 2 Test", "Unit 3 Test". Plain containment misses that in both
    directions -- neither string contains the other -- so the number and the
    plural come off before comparing.
    """
    key = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    key = re.sub(r"\b\d+\b", " ", key)                 # "unit 2 test" -> "unit test"
    key = re.sub(r"\b([ivx]{1,4})\b", " ", key)         # "midterm II"  -> "midterm"
    words = [re.sub(r"e?s$", "", w) if len(w) > 3 else w for w in key.split()]
    return " ".join(w for w in words if w)


def _match_assessment(title, table):
    """Find the entry in `table` that this piece of work belongs to."""
    if not table:
        return None
    if title in table:
        return table[title]
    stem = _assessment_stem(title)
    if not stem:
        return None
    for name, value in table.items():
        other = _assessment_stem(name)
        if other and (other == stem or other in stem or stem in other):
            return value
    return None


def _course_study_brief(user_id, course):
    """{assessment name -> what to actually do for it}, read from the outline.

    This is the difference between "study BIOL for 90 minutes" and "Unit 2
    Test: cellular respiration and photosynthesis -- work the module 4-6
    problems". The outline already lists the topics; nothing else in Nexora was
    reading them.

    Cached on the outline profile. Read-only here: building is a separate,
    explicit request, because it costs a Gemini call.
    """
    profile = CourseOutlineProfile.query.filter_by(user_id=user_id, course_full=course).first()
    if not profile or not profile.study_brief_json:
        return {}
    try:
        data = json.loads(profile.study_brief_json)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _build_course_brief(user_id, course):
    """Ask Gemini, once, what each assessment in this course covers."""
    profile = CourseOutlineProfile.query.filter_by(user_id=user_id, course_full=course).first()
    if not profile:
        return None, "Nexora has not read an outline for this course yet."
    try:
        analysis = json.loads(profile.grading_json or "{}")
    except Exception:
        analysis = {}
    components = [c.get("name") for c in (analysis.get("input_components")
                                          or analysis.get("components") or [])
                  if isinstance(c, dict) and c.get("name")]
    if not components:
        return None, "That outline has no assessments to plan around yet."

    outline = CourseContent.query.filter_by(
        user_id=user_id, course_full=course, content_type="outline").order_by(
        CourseContent.updated_at.desc()).first()
    outline_text = _sanitize_extracted_text((outline.content_text or "") if outline else "")
    # Module and lecture titles say what the course is actually covering, which
    # an outline's topic list sometimes does not.
    modules = [f"- {row.module_name or row.title}" for row in CourseContent.query.filter_by(
        user_id=user_id, course_full=course).filter(
        CourseContent.content_type.in_(["lecture", "resource", "outline"])).order_by(
        CourseContent.updated_at.desc()).limit(40).all()]
    rules = [str(r) for r in (analysis.get("rules") or [])][:8]

    if len(outline_text.strip()) < 200 and not modules:
        return None, "There is not enough course material saved yet for this to be worth doing."

    schema = {"assessments": [{
        "name": "Unit 2 Test",
        "topics": ["cellular respiration", "photosynthesis"],
        "how": "Work the module 4-6 problems closed-book, then review what you missed",
    }]}
    instructions = (
        "You are planning revision for one university course.\n"
        f"The assessments are exactly: {json.dumps(components, ensure_ascii=False)}\n"
        "1. Return one entry per assessment in that list, using its name verbatim. "
        "Do not invent assessments and do not rename them.\n"
        "2. topics: at most four short topic names, taken from the outline or the module "
        "titles. If the outline does not say what an assessment covers, return an empty list "
        "rather than guessing.\n"
        "3. how: one sentence, an instruction the student can start on. Name the thing to do "
        "and the material to do it with. Never say \"study\" or \"review the material\".\n"
        "4. Say nothing about dates, weights or marks. Nexora already knows those."
    )
    try:
        parsed = _gemini_json(
            "You turn a course outline into concrete revision instructions. Output only JSON.",
            f"Return strict JSON matching this schema:\n{json.dumps(schema, indent=2)}\n\n"
            f"{instructions}\n\nOUTLINE:\n{outline_text[:24000]}\n\n"
            "MODULES AND MATERIAL:\n" + "\n".join(modules[:40]) + "\n\n"
            "RULES THE OUTLINE STATES:\n" + "\n".join(rules),
            max_tokens=2200)
    except Exception as exc:
        app.logger.warning("Study brief failed for %s: %s", course, exc)
        if _looks_like_quota_error(exc):
            return None, ("Nexora's AI has hit its usage limit for now. Your plan still works "
                          "from weights and deadlines; try this again later.")
        return None, "Nexora could not read that outline for topics right now."

    known = {re.sub(r"[^a-z0-9]+", " ", c.lower()).strip(): c for c in components}
    brief = {}
    for entry in (parsed.get("assessments") if isinstance(parsed, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", str(entry.get("name") or "").lower()).strip()
        name = known.get(key) or next((v for k, v in known.items() if key and (key in k or k in key)), None)
        if not name:
            continue          # a component this course does not have
        topics = [str(t).strip()[:60] for t in (entry.get("topics") or [])
                  if str(t).strip()][:4]
        how = str(entry.get("how") or "").strip()[:300]
        if not how and not topics:
            continue
        brief[name] = {"topics": topics, "how": how}

    if not brief:
        return None, "Nexora could not match that outline to your assessments."
    profile.study_brief_json = json.dumps(brief, ensure_ascii=False)
    profile.brief_built_at = datetime.utcnow()
    db.session.commit()
    return brief, None


def _study_preference(user_id):
    row = StudyPreference.query.filter_by(user_id=user_id).first()
    if not row:
        row = StudyPreference(user_id=user_id)
        db.session.add(row)
        db.session.commit()
    return row


def _days_off(pref):
    out = set()
    for piece in str(pref.days_off or "").split(","):
        piece = piece.strip()
        if piece.isdigit() and 0 <= int(piece) <= 6:
            out.add(int(piece))
    return out


def _block_occurs_on(block, day):
    """Whether a recurring commitment actually falls on this date.

    Weeks are compared Monday-to-Monday so the anchor does not have to be on
    the same weekday as the block -- a student answering "my next lab is
    Thursday the 17th" for a Thursday lab and one answering with the Monday of
    that week both get the same schedule.

    A biweekly block with no anchor is treated as happening EVERY week, on
    purpose. Losing a study slot costs an hour; scheduling revision over a lab
    the student then misses costs the lab.
    """
    if int(block.weekday or 0) != day.weekday():
        return False
    if (getattr(block, "repeat", "") or "weekly") != "biweekly":
        return True
    anchor = parse_iso_date(getattr(block, "anchor_date", "") or "")
    if not anchor:
        return True
    week_of_day = day - timedelta(days=day.weekday())
    week_of_anchor = anchor - timedelta(days=anchor.weekday())
    return ((week_of_day - week_of_anchor).days // 7) % 2 == 0


def _free_slots(user_id, pref, start, days, from_now=False):
    """Windows of a day with nothing already in them.

    Classes are subtracted with a buffer either side, because a block that
    begins the minute a lecture ends is a block nobody attends.
    """
    blocks = ClassBlock.query.filter_by(user_id=user_id).all()
    by_day = {}
    for block in blocks:
        by_day.setdefault(int(block.weekday or 0), []).append(block)

    off = _days_off(pref)
    buffer_min = max(0, int(pref.buffer_min or 0))
    # A plan built at 3 PM must not offer 9 AM today. The first day's window
    # opens at the next quarter-hour after now, on the student's clock; every
    # later day is whole. Only when the caller is planning from the REAL
    # clock (`from_now`): a fixed date handed in -- a test, the insight page
    # -- is a whole day even when it happens to be today, or a test written
    # around 10 September silently changed meaning on 17 September.
    now_min = 0
    if from_now:
        now = _student_now(user_id)
        if start == now.date():
            now_min = (now.hour * 60 + now.minute + 14) // 15 * 15
    slots = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        if day.weekday() in off:
            continue
        busy = []
        for block in by_day.get(day.weekday(), []):
            # Per date, not per weekday: a biweekly lab is only in the way on
            # the weeks it actually runs.
            if not _block_occurs_on(block, day):
                continue
            busy.append((max(0, int(block.start_min or 0) - buffer_min),
                         min(24 * 60, int(block.end_min or 0) + buffer_min)))
        busy.sort()
        cursor = int(pref.earliest_min or 0)
        if offset == 0:
            cursor = max(cursor, now_min)
        limit = int(pref.latest_min or 24 * 60)
        for begin, finish in busy:
            if finish <= cursor or begin >= limit:
                continue
            if begin > cursor:
                slots.append((day, cursor, min(begin, limit)))
            cursor = max(cursor, finish)
        if cursor < limit:
            slots.append((day, cursor, limit))
    return [(d, a, b) for d, a, b in slots if b - a >= 25]


def _component_weights(user_id):
    """Course code -> {normalised assessment name: weight}."""
    weights = {}
    for row in GradeEntry.query.filter_by(user_id=user_id).all():
        key = re.sub(r"[^a-z0-9]+", " ", str(row.name or "").lower()).strip()
        if key:
            weights.setdefault(row.course_full or "", {})[key] = float(row.weight or 0)
    return weights


def _weight_for(item_title, course, weights):
    """What this piece of work is worth, if the outline said.

    Matched loosely: a deadline called "Assignment 3 - Collections" should find
    the outline's "Assignments" row rather than falling back to a default.
    """
    table = weights.get(course) or {}
    if not table:
        return None
    key = re.sub(r"[^a-z0-9]+", " ", str(item_title or "").lower()).strip()
    if key in table:
        return table[key]
    for name, weight in table.items():
        if name and (name in key or key in name):
            return weight
    # A numbered piece belongs to its group: "Unit tests" worth 30 over three
    # tests is 10 each, which is a better guess than "unknown". The trailing-
    # digit strip missed "Unit 2 Test", where the number is in the middle.
    stem = _assessment_stem(key)
    for name, weight in table.items():
        other = _assessment_stem(name)
        if stem and other and (other == stem or stem in other or other in stem):
            return weight
    return None


def _course_standing(user_id):
    """How each course is actually going, from the marks already entered.

    The point of a plan is to put hours where they are needed, and "needed" is
    not only about weight and deadlines: a course someone is scoring 45% in
    needs more of the week than one they are acing, and Nexora is holding both
    numbers already. Without this, a plan gives the same time to a course you
    are failing and one you have locked up.
    """
    standing = {}
    for row in GradeEntry.query.filter_by(user_id=user_id).all():
        if row.score is None or not row.max_score:
            continue
        # A bonus is optional, so it is not evidence about how a course is
        # going. Counting an unearned one dragged the average down and sent
        # hours to a course the student is acing; worse, it became the `worst`
        # assessment, so the plan told them to go and revise extra credit.
        if _is_bonus_row(row):
            continue
        pct = max(0.0, min(100.0, float(row.score) / float(row.max_score) * 100))
        weight = max(0.0, float(row.weight or 0))
        bucket = standing.setdefault(row.course_full or "", {"points": 0.0, "weight": 0.0, "worst": None})
        if weight > 0:
            bucket["points"] += pct * weight
            bucket["weight"] += weight
        if bucket["worst"] is None or pct < bucket["worst"][1]:
            bucket["worst"] = (row.name or "", pct)

    out = {}
    for course, bucket in standing.items():
        average = (bucket["points"] / bucket["weight"]) if bucket["weight"] else None
        out[course] = {"average": average, "worst": bucket["worst"],
                       "marked_weight": bucket["weight"]}
    return out


def _at_risk_courses(user_id):
    """Courses where a mandatory rule is currently NOT satisfied.

    _compute_official_result is the only place the pass-rule maths lives, so it
    is asked rather than reimplemented. A rule that cannot be evaluated yet is
    not a failure and must not pull time toward the wrong course.
    """
    risky = {}
    for profile in CourseOutlineProfile.query.filter_by(user_id=user_id).all():
        try:
            result, error = _compute_official_result(user_id, profile.course_full)
        except Exception:
            continue
        if error or not result:
            continue
        failing = [r for r in (result.get("pass_rules") or []) if r.get("passed") is False]
        if failing:
            risky[profile.course_full] = failing[0].get("message") or "A pass rule is not satisfied"
    return risky


# What counts as an exam when a student says "midterms and finals only". A
# "test" belongs here: in most courses it is a midterm under another name, and
# leaving it out would drop the very thing they are worried about.
_EXAM_KINDS = {"final", "midterm", "exam", "test"}


def _known_course_codes(user_id):
    """Every course code this student actually has, from any source.

    Not just SyncedCourse: a student who connected only the Brightspace
    calendar feed has deadlines and no course rows, and naming one of those
    courses was being silently discarded as "unknown" -- so "only MATH" left
    the plan unchanged and looked broken.
    """
    codes = {c.course_code.upper() for c in SyncedCourse.query.filter_by(
        user_id=user_id).all() if c.course_code}
    codes |= {t.course_full.upper() for t in Task.query.filter_by(
        user_id=user_id).with_entities(Task.course_full).distinct() if t.course_full}
    codes |= {p.course_full.upper() for p in CourseOutlineProfile.query.filter_by(
        user_id=user_id).with_entities(CourseOutlineProfile.course_full).distinct()
        if p.course_full}
    return {c for c in codes if c and c != "OTHER"}


def _focus_courses_set(pref):
    raw = (getattr(pref, "focus_courses", "") or "").strip()
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


# How many finished items of a kind Nexora needs before it trusts its own
# measurement over the generic estimate. Two is noise; one long night with a
# broken laptop should not redefine what a lab costs.
LEARNED_EFFORT_MIN_SAMPLES = int(os.environ.get("LEARNED_EFFORT_MIN_SAMPLES", "3"))


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _learned_effort(user_id):
    """How long this student's work actually takes, per course and kind.

    The median rather than the mean, because one abandoned six-hour session
    would drag every future estimate with it. Grouped per ITEM, not per block:
    an assignment is several sessions and what matters is what the assignment
    cost in total.

    Returns {(course, kind): hours}. Only pairs with at least
    LEARNED_EFFORT_MIN_SAMPLES finished items appear -- below that the generic
    estimate is the honest answer, and pretending otherwise is how a planner
    starts confidently telling students the wrong thing.
    """
    rows = StudySession.query.filter_by(user_id=user_id, state="done").all()
    per_item = {}
    for row in rows:
        minutes = int(row.actual_minutes or 0)
        if minutes <= 0:
            continue          # the student did not say; never guess for them
        key = (row.course_full or "OTHER", row.item_kind or "work")
        item = row.task_id or f"{key[0]}|{row.title or ''}"
        per_item.setdefault(key, {}).setdefault(item, 0)
        per_item[key][item] += minutes
    learned = {}
    for key, items in per_item.items():
        totals = list(items.values())
        if len(totals) < LEARNED_EFFORT_MIN_SAMPLES:
            continue
        middle = _median(totals)
        if middle:
            learned[key] = max(0.25, min(12.0, middle / 60.0))
    return learned


def _study_items(user_id, today=None, pref=None):
    """Everything worth spending time on, with a priority and an effort budget.

    `pref` narrows it. Without that, the plan schedules every deadline a
    student has, and the small stuff wins on count: a term of pre-lab quizzes
    and milestones filled the week while the exams that decide the grade got
    nothing until they were close.
    """
    today = today or _student_today(user_id)
    focus = (getattr(pref, "focus", "") or "all") if pref is not None else "all"
    only_courses = _focus_courses_set(pref) if pref is not None else set()
    weights = _component_weights(user_id)
    standing = _course_standing(user_id)
    at_risk = _at_risk_courses(user_id)
    learned = _learned_effort(user_id)
    briefs = {}
    horizon = today + timedelta(days=STUDY_HORIZON_DAYS + 21)
    items = []
    for card in sort_cards(build_grouped_cards(load_tasks())):
        due = parse_iso_date(card.get("main_due_raw") or "")
        if not due or due > horizon:
            continue
        title = card.get("title") or "Coursework"
        course = card.get("course_full") or "OTHER"
        kind = card.get("kind") if card.get("kind") in _BASE_EFFORT else None
        kind = kind or _item_kind(title)
        if kind == "event":
            continue
        if focus == "exams" and kind not in _EXAM_KINDS:
            continue
        if only_courses and course.upper() not in only_courses:
            continue
        weight = _weight_for(title, course, weights)
        hours = _BASE_EFFORT.get(kind, 2.0)
        measured = learned.get((course, kind))
        if measured:
            # What this student's own finished work of this kind actually cost.
            # Beats both the generic estimate and the weight-scaled one: a
            # student who needs 3h15m for a SYSC lab needs 3h15m for the next.
            hours = measured
        elif weight:
            # Weight is the honest signal when the outline gave one: a 30%
            # midterm earns three times the hours of a 10% one.
            hours = max(1.0, min(12.0, hours * (float(weight) / 12.0) ** 0.7))
        days_left = (due - today).days
        # Value per remaining day. Something worth 25% due in two days beats
        # something worth 25% due in three weeks, which is the whole point.
        urgency = 1.0 / max(0.75, days_left + 0.75)
        priority = (float(weight) if weight else 6.0) * urgency
        if days_left < 0:
            priority *= 2.5          # overdue work is the first thing to clear

        # Where the student actually stands in this course. Capped both ways:
        # a weak course earns more of the week, not the whole of it.
        note = ""
        course_standing = standing.get(course) or {}
        average = course_standing.get("average")
        if average is not None and course_standing.get("marked_weight", 0) >= 5:
            if average < 50:
                priority *= 1.6
                note = f"you are at {average:.0f}% in this course"
            elif average < 65:
                priority *= 1.25
                note = f"you are at {average:.0f}% in this course"
            elif average >= 85:
                priority *= 0.8
                note = f"you are at {average:.0f}% here already"
        if course in at_risk:
            # A mandatory rule that is currently failing outranks arithmetic.
            priority *= 1.9
            note = at_risk[course]
            if len(note) > 90:
                note = note[:87] + "..."
        extra_hours = 0.0
        if average is not None and average < 55 and hours:
            extra_hours = hours * 0.4
        hours += extra_hours

        # What the outline says this particular assessment covers, if the
        # student has had Nexora read it.
        if course not in briefs:
            briefs[course] = _course_study_brief(user_id, course)
        entry = _match_assessment(title, briefs[course])

        items.append({
            "brief": entry or None,
            "task_id": card.get("task_ids", [""])[0] if card.get("task_ids") else "",
            "title": title, "course": course, "kind": kind, "due": due,
            "days_left": days_left, "weight": weight,
            "minutes_needed": int(round(hours * 60)),
            "priority": priority,
            "standing_note": note,
            "worst": (course_standing.get("worst") or (None, None))[0] or "",
        })
    items.sort(key=lambda x: -x["priority"])
    return items


# Revision has a shape, and it is not "study for two hours" six times over.
# Each rung is (days before the exam this stage starts, what the stage is for).
# Read from the widest gap down, so the first rung a day qualifies for wins.
_EXAM_LADDER = [
    (11, "read back through the material and rebuild your notes"),
    (8, "work the topics you are weakest on, not the ones you enjoy"),
    (5, "problem sets end to end, timed, no notes"),
    (3, "a past paper under exam conditions"),
    (2, "repair whatever the past paper exposed"),
    (0, "light review only — sleep is worth more than another hour now"),
]


def _exam_stage(days_left):
    """Which rung of the revision ladder a day sits on.

    A student asked for exactly this and it is also simply how revision works:
    reading, then weak topics, then problems, then a past paper, then repair,
    then leave it alone. Deterministic on the gap to the exam, so the same day
    always gets the same stage and the plan can explain itself.
    """
    for threshold, purpose in _EXAM_LADDER:
        if days_left >= threshold:
            return purpose
    return _EXAM_LADDER[-1][1]


def _focus_for(item, day=None):
    """What to do in this block.

    The outline's own topics when Nexora has read them, and the generic verb
    for the kind of assessment otherwise. Naming the topics is most of what
    makes a block something a person can start rather than dread.
    """
    brief = item.get("brief") or {}
    how = str(brief.get("how") or "").strip()
    topics = [t for t in (brief.get("topics") or []) if t]
    # For an exam, WHAT to study comes from the outline and HOW depends on how
    # close it is. Four sessions all saying "revise" is the advice that makes
    # plans useless.
    if day is not None and item.get("kind") in _EXAM_KINDS and item.get("due"):
        # Three different things, and a block needs all of them: the topics are
        # WHAT, the outline's own pointer is WHERE, and the ladder rung is HOW.
        # Dropping the outline's "module 4-6" for the rung lost the only part
        # that tells a student which pages to open.
        stage = _exam_stage((item["due"] - day).days)
        head = ", ".join(topics[:3])
        if head and how:
            return f"{head} ({how}) — {stage}"[:500]
        if head:
            return f"{head} — {stage}"[:500]
        if how:
            return f"{how} — {stage}"[:500]
        return stage[:500]
    if topics:
        listed = ", ".join(topics[:3])
        return (f"{listed} — {how}" if how else
                f"{listed} — {_FOCUS.get(item['kind'], _FOCUS['work'])}")[:500]
    if how:
        return how[:500]
    return _FOCUS.get(item["kind"], _FOCUS["work"])


def _reason_for(item, today):
    bits = []
    if item.get("standing_note"):
        # The most useful half of "why now" is usually where they stand, not
        # the weight -- so it goes first.
        bits.append(item["standing_note"])
    if item["weight"]:
        bits.append(f"worth {item['weight']:g}% of the course")
    if item["days_left"] < 0:
        bits.append(f"overdue by {abs(item['days_left'])} day{'' if abs(item['days_left']) == 1 else 's'}")
    elif item["days_left"] == 0:
        bits.append("due today")
    else:
        bits.append(f"due in {item['days_left']} day{'' if item['days_left'] == 1 else 's'}")
    return "; ".join(bits)[:255]


def _build_plan(user_id, today=None, days=None):
    """Fill the free time with the work that matters most, soonest.

    Deliberately simple and greedy. A cleverer optimiser would produce a plan
    nobody could argue with -- and nobody could follow, because they could not
    see why any block was where it was.
    """
    from_now = today is None
    today = today or _student_today(user_id)
    days = days or STUDY_HORIZON_DAYS
    pref = _study_preference(user_id)
    slots = _free_slots(user_id, pref, today, days, from_now=from_now)
    items = _study_items(user_id, today, pref)
    if not items or not slots:
        return []

    session_len = max(20, int(pref.session_min or 50))
    break_len = max(0, int(pref.break_min or 0))
    daily_cap = max(session_len, int(pref.max_daily_min or 240))
    remaining = {id(i): i["minutes_needed"] for i in items}
    # Revision has to be SPREAD, not spent. The greedy loop poured a midterm's
    # whole five hours into the first two free days, so the plan finished
    # revising eleven days early and never reached the past-paper stage --
    # _EXAM_LADDER existed and could not be climbed. Cap an exam at roughly its
    # fair share of the days it may be revised over.
    exam_daily_cap = {}
    # Which days-before-the-exam an exam may be revised on. Capping the daily
    # minutes alone was not enough: a five-hour midterm still finished in six
    # consecutive days and stopped eleven days out, so the problem-set and
    # past-paper rungs were never reached. Spacing the same six sessions across
    # the window is both what a student asked for and how revision works.
    exam_gaps = {}
    for item in items:
        if item["kind"] not in _EXAM_KINDS or not item.get("due"):
            continue
        window = max(1, min(EXAM_REVISION_WINDOW, (item["due"] - today).days))
        exam_daily_cap[id(item)] = max(session_len, int(item["minutes_needed"] / window))
        needed = max(1, -(-item["minutes_needed"] // max(1, session_len)))
        if needed >= window:
            gaps = set(range(1, window + 1))
        else:
            step = window / float(needed)
            gaps = {max(1, int(round(1 + index * step))) for index in range(needed)}
        # The last two days are always open, so the final light-review rung
        # cannot be lost to a day with no free time earlier in the week.
        exam_gaps[id(item)] = gaps | {1, 2}
    spent_per_item_day = {}
    # Work that is already late still has to fit somewhere. Without this the
    # "never schedule past the deadline" rule silently excluded exactly the
    # work the student is most behind on -- every day is past its deadline.
    OVERDUE_GRACE = 3
    latest_day = {id(i): (i["due"] if i["due"] >= today else today + timedelta(days=OVERDUE_GRACE))
                  for i in items}
    spent_on_day = {}
    plan = []
    last_item, run = None, 0

    for day, begin, end in slots:
        cursor = begin
        while cursor + session_len <= end:
            used = spent_on_day.get(day, 0)
            if used + session_len > daily_cap:
                break

            choices = []
            for item in items:
                if remaining[id(item)] <= 0:
                    continue
                # Never after the deadline, and revision for a distant exam
                # does not start now just because there is a gap in the week.
                if day > latest_day[id(item)]:
                    continue
                if item["kind"] in _EXAM_KINDS and item.get("due") and \
                        (item["due"] - day).days > EXAM_REVISION_WINDOW:
                    continue
                cap = exam_daily_cap.get(id(item))
                if cap is not None and spent_per_item_day.get((day, id(item)), 0) >= cap:
                    # Its share of today is used. Something else takes this slot
                    # and this exam gets a later day -- which is what turns six
                    # blocks of "revise" into a sequence of stages.
                    continue
                gaps = exam_gaps.get(id(item))
                if gaps is not None and (item["due"] - day).days not in gaps:
                    continue
                score = item["priority"]
                if item is last_item and run >= MAX_CONSECUTIVE_SAME:
                    # Interleaving beats blocking for retention, and stops one
                    # course eating a whole evening.
                    score *= 0.35
                # Bring work forward when its deadline is inside the plan.
                if (latest_day[id(item)] - day).days <= 1:
                    score *= 1.6
                choices.append((score, item))
            if not choices:
                break
            choices.sort(key=lambda x: -x[0])
            item = choices[0][1]

            length = min(session_len, remaining[id(item)])
            if length < 20:
                length = min(session_len, max(20, remaining[id(item)]))
            cap = exam_daily_cap.get(id(item))
            if cap is not None:
                room = cap - spent_per_item_day.get((day, id(item)), 0)
                length = min(length, max(20, room))
            plan.append({
                "on_date": day.isoformat(), "start_min": cursor,
                "end_min": cursor + length, "course_full": item["course"],
                "title": item["title"], "item_kind": item["kind"],
                "task_id": item["task_id"] or "",
                "focus": _focus_for(item, day),
                "reason": _reason_for(item, today),
            })
            remaining[id(item)] -= length
            spent_on_day[day] = used + length
            key = (day, id(item))
            spent_per_item_day[key] = spent_per_item_day.get(key, 0) + length
            run = run + 1 if item is last_item else 1
            last_item = item
            cursor += length + break_len
    return plan


def _close_off_yesterday(user_id, today=None):
    """Retire plan blocks whose day has gone by without them being touched.

    They used to stay `planned` for ever: `_save_plan` only replaces sessions
    from today forward, so last week's untouched blocks accumulated as ghosts
    that no rebuild could clear and no count could trust.

    Returns what was missed, so the page can say what it is doing about it.
    Deliberately not framed as a failing: the work still has a deadline, and
    the only useful response is to put it back in the week that is left.
    """
    today = (today or _student_today(user_id)).isoformat()
    rows = StudySession.query.filter_by(user_id=user_id, state="planned").filter(
        StudySession.on_date < today).all()
    if not rows:
        return {"count": 0, "minutes": 0, "courses": [], "dates": []}
    minutes = 0
    courses, dates = set(), set()
    for row in rows:
        row.state = "missed"
        minutes += max(0, int(row.end_min or 0) - int(row.start_min or 0))
        if row.course_full:
            courses.add(row.course_full)
        if row.on_date:
            dates.add(row.on_date)
    db.session.commit()
    return {"count": len(rows), "minutes": minutes,
            "courses": sorted(courses), "dates": sorted(dates)}


def _catch_up_message(missed, rebuilt):
    """What changed, in one sentence, with no scolding in it.

    Every planner fails the same way: the moment a student misses Tuesday the
    plan is wrong, stays wrong, and quietly becomes something they avoid
    opening. Saying plainly what moved is what makes it survivable.
    """
    if not missed.get("count"):
        return ""
    hours, mins = divmod(max(0, int(missed["minutes"])), 60)
    spent = f"{hours}h{mins:02d}" if hours else f"{mins} min"
    where = ", ".join(missed.get("courses") or [])
    tail = (f" It is back in your plan{f' across {rebuilt} blocks' if rebuilt else ''}, "
            "ahead of the lighter work.") if rebuilt else \
        " There was no free time left to move it into, so shorten something or free up a day."
    return (f"{spent} of planned study did not happen"
            + (f" ({where})" if where else "") + "." + tail)


def _save_plan(user_id, plan):
    """Replace the plan from today forward, keeping what is already done.

    A regenerate must not quietly erase the evidence that someone did the work,
    and must not touch a week they have already lived through.
    """
    today = _student_today(user_id).isoformat()
    stale = StudySession.query.filter_by(user_id=user_id).filter(
        StudySession.on_date >= today, StudySession.state == "planned").all()
    for row in stale:
        db.session.delete(row)
    db.session.flush()
    kept = {(r.on_date, r.start_min) for r in
            StudySession.query.filter_by(user_id=user_id).filter(
                StudySession.on_date >= today).all()}
    made = 0
    for entry in plan:
        if (entry["on_date"], entry["start_min"]) in kept:
            continue      # that slot already holds a session they acted on
        db.session.add(StudySession(id=uuid4().hex, user_id=user_id, **entry))
        made += 1
    db.session.commit()
    return made


def _session_links(rows):
    """task_id -> LMS link, for a batch of plan blocks in one query.

    A lookup per block would be N+1 against a week that is routinely twenty
    sessions long.
    """
    ids = {r.task_id for r in rows if r.task_id}
    if not ids:
        return {}
    found = Task.query.filter(Task.id.in_(list(ids)[:400])).all()
    return {t.id: (t.source_link or "") for t in found if t.source_link}


def _sessions_json(rows):
    """A list of plan blocks, resolving every link in one query rather than
    one per block."""
    links = _session_links(rows)
    return [_session_json(r, links) for r in rows]


def _session_json(row, links=None):
    # A single-row response resolves its own link; the list endpoints pass a
    # batch so they do not query once per block.
    if links is None:
        links = _session_links([row])
    return {
        "id": row.id, "date": row.on_date, "start": row.start_min, "end": row.end_min,
        "start_label": f"{row.start_min // 60:02d}:{row.start_min % 60:02d}",
        "end_label": f"{row.end_min // 60:02d}:{row.end_min % 60:02d}",
        "minutes": max(0, row.end_min - row.start_min),
        "course": row.course_full, "title": row.title, "focus": row.focus,
        "reason": row.reason, "kind": row.item_kind, "state": row.state,
        "task_id": row.task_id or "",
        # Where the work itself lives, so a plan block is one click from the
        # assignment rather than a reminder to go and find it.
        "link": links.get(row.task_id or "", ""),
        "actual_minutes": int(row.actual_minutes or 0),
    }


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_INDEX = {name: i for i, name in enumerate(_WEEKDAYS)}
_WEEKDAY_INDEX.update({name[:3]: i for i, name in enumerate(_WEEKDAYS)})
_WEEKDAY_INDEX.update({"tues": 1, "thur": 3, "thurs": 3, "r": 3, "m": 0, "t": 1, "w": 2, "f": 4})

CLASS_KINDS = {"lecture", "lab", "tutorial", "seminar", "work", "commute", "other"}


def _parse_clock(value):
    """Minutes from midnight, from anything a timetable writes a time as.

    Accepts a real number as minutes-from-midnight, because that is what
    `/api/timetable/analyze` emits and the confirm step posts straight back.
    It did not, so every class read out of an uploaded timetable was rejected
    on save -- and the page still said "Saved. Building your plan...". The
    upload path had never once worked end to end; only "Add a class by hand"
    did, because an <input type=time> sends "09:00".

    A numeric STRING stays rejected on purpose: "540" could be 540 minutes or
    5:40, and guessing wrong moves a class by hours.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        minutes = int(value)
        return minutes if 0 <= minutes <= 24 * 60 - 1 else None
    # `str(value or "")` turned 0 into "" -- midnight is a real time.
    raw = ("" if value is None else str(value)).strip().lower().replace(".", ":")
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(a|p)?m?", raw)
    if not match:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if not match:
            return None
        hour, minute, meridiem = int(match.group(1)), int(match.group(2)), None
    else:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)
    if meridiem == "p" and hour < 12:
        hour += 12
    if meridiem == "a" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _parse_weekday(value):
    """0-6 from a name or a number. Monday is 0, and 0 is falsy.

    `str(value or "")` turned Monday into an empty string, so every Monday
    class posted as a number was refused with "Choose a day of the week."
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        index = int(value)
        return index if 0 <= index <= 6 else None
    raw = ("" if value is None else str(value)).strip().lower()
    if raw.isdigit() and 0 <= int(raw) <= 6:
        return int(raw)
    return _WEEKDAY_INDEX.get(raw[:9]) if raw[:9] in _WEEKDAY_INDEX else _WEEKDAY_INDEX.get(raw[:3])


def _block_json(row):
    return {
        "id": row.id, "course": row.course_full or "", "label": row.label or "",
        "weekday": int(row.weekday or 0), "start": int(row.start_min or 0),
        "end": int(row.end_min or 0),
        "start_label": f"{int(row.start_min or 0) // 60:02d}:{int(row.start_min or 0) % 60:02d}",
        "end_label": f"{int(row.end_min or 0) // 60:02d}:{int(row.end_min or 0) % 60:02d}",
        "location": row.location or "", "kind": row.kind or "lecture",
        "repeat": getattr(row, "repeat", "") or "weekly",
        "anchor_date": getattr(row, "anchor_date", "") or "",
        "source": row.source or "manual",
    }


def _block_fields(data, partial=False):
    fields = {}
    if not partial or "label" in data:
        label = str(data.get("label") or "").strip()
        if not label:
            return None, "Give the class a name."
        fields["label"] = label[:200]
    if not partial or "weekday" in data:
        weekday = _parse_weekday(data.get("weekday"))
        if weekday is None:
            return None, "Choose a day of the week."
        fields["weekday"] = weekday
    if not partial or "start" in data:
        start = _parse_clock(data.get("start"))
        if start is None:
            return None, "That start time could not be read. Use something like 11:35."
        fields["start_min"] = start
    if not partial or "end" in data:
        end = _parse_clock(data.get("end"))
        if end is None:
            return None, "That end time could not be read. Use something like 12:55."
        fields["end_min"] = end
    if "course" in data:
        fields["course_full"] = _normalize_course_code(str(data.get("course") or "")) or ""
    if "location" in data:
        fields["location"] = str(data.get("location") or "").strip()[:255]
    if "kind" in data:
        kind = str(data.get("kind") or "lecture").strip().lower()
        fields["kind"] = kind if kind in CLASS_KINDS else "other"
    if "repeat" in data:
        repeat = str(data.get("repeat") or "weekly").strip().lower()
        fields["repeat"] = "biweekly" if repeat in {"biweekly", "fortnightly",
                                                    "alternate", "every_other_week"} else "weekly"
    if "anchor_date" in data or "anchor" in data:
        raw = str(data.get("anchor_date") or data.get("anchor") or "").strip()[:10]
        fields["anchor_date"] = raw if parse_iso_date(raw) else ""

    start = fields.get("start_min")
    end = fields.get("end_min")
    if start is not None and end is not None and end <= start:
        return None, "The class has to end after it starts."
    return fields, None


def _plan_runway(user_id, sessions, today, colours):
    """What the student is actually working toward, and how far along they are.

    The plan rendered as a flat list of blocks -- "SYSC2004, 50 min" over and
    over -- which says what to do in the next hour and nothing about why, or
    what it is building to, or whether it is enough. The owner's words were
    "doesn't make a proper study plan, just a list of what to do for 50 mins
    for each thing".

    This is the missing half: for each deadline the plan is feeding, the hours
    already done, the hours still booked, and how long there is. Built from the
    sessions themselves rather than from a second pass over the deadlines,
    because the sessions ARE the plan -- anything else could disagree with what
    is on the page directly underneath it.

    `done` counts `actual_minutes`, the minutes the clock really recorded, on
    the same rule `_learned_effort` follows: a block marked done with no
    duration is not evidence of anything.
    """
    items = {}
    rows = StudySession.query.filter_by(user_id=user_id).filter(
        StudySession.on_date >= (today - timedelta(days=28)).isoformat()).all()
    for row in rows:
        key = (row.course_full or "OTHER", (row.title or "").strip())
        if not key[1]:
            continue
        it = items.setdefault(key, {
            "course": key[0], "title": key[1], "kind": row.item_kind or "",
            "planned": 0, "done": 0, "blocks": 0, "done_blocks": 0,
        })
        minutes = max(0, (row.end_min or 0) - (row.start_min or 0))
        if row.state == "done":
            it["done"] += int(row.actual_minutes or 0)
            it["done_blocks"] += 1
        elif row.state == "planned" and row.on_date >= today.isoformat():
            it["planned"] += minutes
            it["blocks"] += 1

    # When is each of these actually due? The deadline lives on Task, and the
    # plan's own title is what links them.
    due_by_title = {}
    for task in Task.query.filter_by(user_id=user_id, completed=False).all():
        stem = (task.title or "").strip()
        if stem:
            due_by_title.setdefault(((task.course_full or "OTHER"), stem), task.due_raw or "")

    out = []
    for key, it in items.items():
        if it["planned"] <= 0 and it["done"] <= 0:
            continue
        due = due_by_title.get(key, "")
        days = days_until(due) if due else None
        # _major_assessment_label is the ONLY authority, and deliberately has
        # no `or kind in _EXAM_KINDS` fallback beside it. The planner files a
        # revision block under an exam kind -- that is what it is revision FOR
        # -- so an `or` on the kind put an EXAM tag on "Midterm review session"
        # and on "Weekly Quiz 4", re-flagging the exact titles _NOT_AN_EXAM
        # exists to exclude. Caught in the browser one screenshot after this
        # strip first rendered.
        label = _major_assessment_label(it["kind"], it["title"])
        if days is not None and days < -1:
            continue
        total = it["planned"] + it["done"]
        out.append({**it,
                    "major": bool(label),
                    "label": label,
                    "days_left": days,
                    "due": due,
                    "colour": colours.get(it["course"], 0),
                    "total": total,
                    "pct": int(round(100 * it["done"] / total)) if total else 0})
    # Soonest first, and an exam ahead of ordinary work due the same day --
    # which is the order the hours should be read in.
    out.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 999,
                            0 if x["major"] else 1, -x["total"]))
    return out[:6]


@app.route("/study-plan")
@login_required
def study_plan_page():
    user_id = current_user.id
    pref = _study_preference(user_id)
    blocks = ClassBlock.query.filter_by(user_id=user_id).order_by(
        ClassBlock.weekday.asc(), ClassBlock.start_min.asc()).all()
    today = _student_today(user_id)
    sessions = StudySession.query.filter_by(user_id=user_id).filter(
        StudySession.on_date >= today.isoformat()).order_by(
        StudySession.on_date.asc(), StudySession.start_min.asc()).all()

    courses = sorted({c.course_code for c in SyncedCourse.query.filter_by(user_id=user_id).all()
                      if c.course_code and not bool(getattr(c, "is_hidden", False))})
    # ONE colour map, built once and handed to everything on this page and to
    # the runway. Computing it twice from two different inputs is how two parts
    # of the same screen end up painting one course two colours.
    colours = _student_course_colours(
        user_id,
        extra=list(courses)
        + [s.course_full for s in sessions if s.course_full]
        + [b.course_full for b in blocks if getattr(b, "course_full", None)])
    return render_template(
        "study_plan.html",
        # With no timetable saved, uploading one is the only thing worth doing
        # on this page -- and it was the second card, under an empty plan. The
        # student who asked "where is the study plan option where I add my
        # timetable" was looking straight at it.
        has_timetable=bool(blocks),
        blocks=[_block_json(b) for b in blocks],
        sessions=_sessions_json(sessions),
        preferences={
            "earliest_min": pref.earliest_min, "latest_min": pref.latest_min,
            "session_min": pref.session_min, "break_min": pref.break_min,
            "max_daily_min": pref.max_daily_min, "buffer_min": pref.buffer_min,
            "days_off": [int(d) for d in str(pref.days_off or "").split(",") if d.strip().isdigit()],
            "focus": pref.focus or "all",
            "focus_courses": sorted(_focus_courses_set(pref)),
        },
        courses=courses,
        weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        today_iso=today.isoformat(),
        # The same rule the calendar follows. Two screens painting a course
        # differently is the same class of bug as two disagreeing about a
        # grade, and this page sits one rail item away from that one.
        course_colours=colours,
        runway=_plan_runway(user_id, sessions, today, colours),
    )


@app.route("/api/timetable", methods=["GET", "POST"])
@login_required
def timetable_api():
    if request.method == "GET":
        rows = ClassBlock.query.filter_by(user_id=current_user.id).order_by(
            ClassBlock.weekday.asc(), ClassBlock.start_min.asc()).all()
        return jsonify({"blocks": [_block_json(r) for r in rows]})

    data = request.get_json(silent=True) or {}
    # A confirmed extraction posts the whole week at once; the editor posts one.
    if isinstance(data.get("blocks"), list):
        if data.get("replace"):
            ClassBlock.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
        made, skipped = 0, 0
        for entry in data["blocks"][:80]:
            fields, error = _block_fields(entry if isinstance(entry, dict) else {})
            if error:
                skipped += 1
                continue
            fields.setdefault("course_full", "")
            db.session.add(ClassBlock(id=uuid4().hex, user_id=current_user.id,
                                      source="import", **fields))
            made += 1
        db.session.commit()
        rows = ClassBlock.query.filter_by(user_id=current_user.id).order_by(
            ClassBlock.weekday.asc(), ClassBlock.start_min.asc()).all()
        if not made and skipped:
            # Reporting ok:true here is what hid the round-trip bug: the page
            # said "Saved. Building your plan..." over an empty week for as
            # long as this feature has existed. A save that saved nothing is a
            # failure and has to read as one.
            return jsonify({"error": f"None of those {skipped} classes could be saved. "
                                     "Check the days and times, or add them by hand.",
                            "skipped": skipped,
                            "blocks": [_block_json(r) for r in rows]}), 422
        return jsonify({"ok": True, "added": made, "skipped": skipped,
                        "blocks": [_block_json(r) for r in rows]})

    fields, error = _block_fields(data)
    if error:
        return jsonify({"error": error}), 400
    fields.setdefault("course_full", "")
    row = ClassBlock(id=uuid4().hex, user_id=current_user.id, source="manual", **fields)
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "block": _block_json(row)}), 201


@app.route("/api/timetable/<block_id>", methods=["POST", "DELETE"])
@login_required
def timetable_block(block_id):
    row = ClassBlock.query.filter_by(user_id=current_user.id, id=block_id).first()
    if not row:
        return jsonify({"error": "That class is no longer there."}), 404
    if request.method == "DELETE":
        db.session.delete(row)
        db.session.commit()
        return jsonify({"ok": True})
    fields, error = _block_fields(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify({"error": error}), 400
    for key, value in fields.items():
        setattr(row, key, value)
    if int(row.end_min or 0) <= int(row.start_min or 0):
        return jsonify({"error": "The class has to end after it starts."}), 400
    db.session.commit()
    return jsonify({"ok": True, "block": _block_json(row)})


@app.route("/api/timetable/analyze", methods=["POST"])
@ai_limit
@login_required
@requires_active
def timetable_analyze():
    """Read a timetable screenshot or PDF into proposed class blocks.

    Proposed, not saved. A misread class time silently poisons every study
    block for the rest of term, so the student confirms the week first.
    """
    uploads = [u for u in (request.files.getlist("files") or []) if u and u.filename]
    text = ""
    if uploads:
        if len(uploads) > 5:
            return jsonify({"error": "Upload at most 5 pages at once."}), 400
        try:
            parts = []
            for index, upload in enumerate(uploads, 1):
                name, extracted, _hash, _size = _extract_uploaded_course_file(upload)
                extracted = _sanitize_extracted_text(extracted)
                if not extracted:
                    raise ValueError(f"Nexora could not read any text from {name}.")
                parts.append(f"\n===== PAGE {index}: {name} =====\n{extracted}")
            text = "\n".join(parts)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        except Exception:
            app.logger.exception("Timetable extraction failed")
            return jsonify({"error": "Nexora could not read that timetable file."}), 422
    else:
        text = _sanitize_extracted_text(str((request.get_json(silent=True) or {}).get("text") or ""))

    if len(text.strip()) < 15:
        return jsonify({"error": "That timetable looks empty. Upload a clearer screenshot, or add your classes by hand."}), 400

    schema = {"blocks": [{"label": "BIOL 1103 Lecture", "course": "BIOL1103",
                          "weekday": "monday", "start": "11:35", "end": "12:55",
                          "location": "Southam Hall", "kind": "lecture",
                          "repeat": "weekly"}]}
    rules = (
        "Read this university timetable and return every recurring class.\n"
        "1. One entry per day a class meets. A class listed as Monday & Wednesday is TWO entries.\n"
        "2. weekday is the full day name in English. start and end are 24-hour HH:MM.\n"
        "3. A range like 11:35-12:55 gives start 11:35 and end 12:55. Times before 8:00 in a "
        "university timetable are almost always afternoons -- 4:35 means 16:35.\n"
        "4. course is the course code with no space, e.g. BIOL1103, or \"\" if there is none.\n"
        "5. kind is one of lecture, lab, tutorial, seminar, other.\n"
        "6. Never invent a class, a time or a room. Omit anything you cannot read.\n"
        "7. Return only classes and other recurring commitments, never one-off events, "
        "deadlines or holidays.\n"
        # Biweekly labs are common and the timetable usually says so in a word.
        # The planner blocks the wrong weeks without this, either stealing study
        # time or scheduling over the lab.
        "8. repeat is \"biweekly\" when the timetable says a class runs every other week, "
        "on alternating, odd or even weeks, fortnightly, or in a repeating A/B week "
        "pattern. Otherwise repeat is \"weekly\". Do not guess which specific weeks it "
        "falls on -- the student confirms that afterwards."
    )
    try:
        parsed = _gemini_json(
            "You read university timetables into strict JSON. Output only JSON.",
            f"Return strict JSON matching this schema:\n{json.dumps(schema, indent=2)}\n\n"
            f"{rules}\n\nTIMETABLE:\n{text[:40000]}",
            max_tokens=3000)
    except Exception as exc:
        app.logger.warning("Timetable analysis failed: %s", exc)
        message = ("Nexora's AI has hit its usage limit for now, so it could not read this "
                   "timetable. Add your classes by hand below, or try again later."
                   if _looks_like_quota_error(exc) else
                   "Nexora could not read that timetable. Add your classes by hand below.")
        return jsonify({"error": message, "retryable": True}), 503

    proposed, dropped = [], 0
    for entry in (parsed.get("blocks") if isinstance(parsed, dict) else None) or []:
        fields, error = _block_fields(entry if isinstance(entry, dict) else {})
        if error:
            dropped += 1
            continue
        proposed.append({
            "label": fields["label"], "course": fields.get("course_full", ""),
            "weekday": fields["weekday"], "start": fields["start_min"],
            "end": fields["end_min"], "location": fields.get("location", ""),
            "kind": fields.get("kind", "lecture"),
            # Carried through the confirm step, or a biweekly lab the model
            # correctly spotted silently becomes a weekly one on save.
            "repeat": fields.get("repeat", "weekly"),
            "anchor_date": fields.get("anchor_date", ""),
            "start_label": f"{fields['start_min'] // 60:02d}:{fields['start_min'] % 60:02d}",
            "end_label": f"{fields['end_min'] // 60:02d}:{fields['end_min'] % 60:02d}",
        })
    proposed.sort(key=lambda b: (b["weekday"], b["start"]))
    if not proposed:
        return jsonify({"error": "Nexora could not find any classes in that file. Add them by hand below."}), 422
    return jsonify({"ok": True, "blocks": proposed, "dropped": dropped,
                    "notice": "Check every day and time before you save these. A wrong class "
                              "time quietly moves every study block for the rest of term."})


@app.route("/api/study-preferences", methods=["GET", "POST"])
@login_required
def study_preferences_api():
    pref = _study_preference(current_user.id)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        numbers = {
            "earliest_min": (0, 23 * 60), "latest_min": (60, 24 * 60),
            "session_min": (20, 180), "break_min": (0, 60),
            "max_daily_min": (30, 12 * 60), "buffer_min": (0, 120),
        }
        for key, (low, high) in numbers.items():
            if key in data:
                try:
                    setattr(pref, key, max(low, min(high, int(float(data[key])))))
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key.replace('_', ' ')} needs to be a number."}), 400
        if pref.latest_min - pref.earliest_min < pref.session_min:
            return jsonify({"error": "That leaves no room for a session. Widen the hours or shorten the session."}), 400
        if "days_off" in data:
            days = data["days_off"] if isinstance(data["days_off"], list) else str(data["days_off"]).split(",")
            clean = sorted({int(d) for d in days if str(d).strip().isdigit() and 0 <= int(d) <= 6})
            if len(clean) >= 7:
                return jsonify({"error": "Keep at least one day available to study."}), 400
            pref.days_off = ",".join(str(d) for d in clean)
        if "focus" in data:
            pref.focus = "exams" if str(data.get("focus") or "").lower() == "exams" else "all"
        if "focus_courses" in data:
            wanted = data.get("focus_courses") or []
            if isinstance(wanted, str):
                wanted = [c for c in wanted.split(",")]
            known = _known_course_codes(current_user.id)
            # Only real courses: an unknown code would silently empty the plan.
            keep = [str(c).strip().upper() for c in wanted if str(c).strip().upper() in known]
            pref.focus_courses = ",".join(sorted(set(keep)))[:400]
        db.session.commit()
    return jsonify({"ok": True, "preferences": {
        "earliest_min": pref.earliest_min, "latest_min": pref.latest_min,
        "session_min": pref.session_min, "break_min": pref.break_min,
        "max_daily_min": pref.max_daily_min, "buffer_min": pref.buffer_min,
        "days_off": [int(d) for d in str(pref.days_off or "").split(",") if d.strip().isdigit()],
        "focus": pref.focus or "all",
        "focus_courses": sorted(_focus_courses_set(pref)),
    }})


@app.route("/api/study-plan", methods=["GET"])
@login_required
def study_plan_api():
    today = _student_today(current_user)
    rows = StudySession.query.filter_by(user_id=current_user.id).filter(
        StudySession.on_date >= today.isoformat()).order_by(
        StudySession.on_date.asc(), StudySession.start_min.asc()).all()
    return jsonify({
        "ok": True,
        "sessions": _sessions_json(rows),
        "has_timetable": ClassBlock.query.filter_by(user_id=current_user.id).count() > 0,
    })


@app.route("/api/study-plan/brief", methods=["POST"])
@ai_limit
@login_required
@requires_active
def study_plan_brief():
    """Read every analysed outline for what each assessment actually covers.

    Separate from generating a plan on purpose: this is the only part that
    costs a Gemini call, and the answer does not change between rebuilds. One
    call per course, cached on the outline profile.
    """
    profiles = CourseOutlineProfile.query.filter_by(user_id=current_user.id).all()
    if not profiles:
        return jsonify({"error": "Analyse a course outline first — that is where the topics come from."}), 400
    wanted = _normalize_course_code(str((request.get_json(silent=True) or {}).get("course") or ""))
    if wanted:
        profiles = [p for p in profiles if p.course_full == wanted] or profiles

    done, failed, last_error = [], [], ""
    for profile in profiles[:12]:
        brief, error = _build_course_brief(current_user.id, profile.course_full)
        if brief:
            done.append({"course": profile.course_full, "assessments": len(brief)})
        else:
            failed.append(profile.course_full)
            last_error = error or last_error
    if not done:
        return jsonify({"error": last_error or "Nexora could not read topics out of your outlines."}), 503
    return jsonify({"ok": True, "read": done, "skipped": failed,
                    "notice": last_error if failed else ""})


@app.route("/api/study-plan/ask", methods=["POST"])
@ai_limit
@login_required
@requires_active
def study_plan_ask():
    """Say what you want the plan to be for, in your own words.

    The AI reads the REQUEST; it does not write the schedule. That split is
    deliberate and is the same rule as the rest of the planner: a plan has to
    be the same plan when you look at it twice, has to explain why every block
    is where it is, and must not cost quota to redraw. A model laying out the
    week directly satisfies none of those. So a sentence becomes settings --
    what to plan for, which courses, how many days, how long a day -- and the
    existing arithmetic builds it.

    It answers with what it understood, so a misreading is visible and
    correctable rather than silently reshaping someone's term.
    """
    question = str((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question:
        return jsonify({"error": "Tell Nexora what you want the plan to be for."}), 400
    if len(question) > 500:
        return jsonify({"error": "Keep it under 500 characters."}), 400

    pref = _study_preference(current_user.id)
    codes = sorted(_known_course_codes(current_user.id))
    schema = {"focus": "all", "courses": [], "days": 14,
              "max_daily_minutes": 240, "understood": ""}
    rules = [
        "Turn a student's request about their study plan into settings.",
        'focus is "exams" when they want only tests, midterms and finals, otherwise "all".',
        "courses is the subset of the student's own course codes they named, or [] for all.",
        "Their courses are: " + (", ".join(codes) or "none imported yet") + ".",
        "days is how far ahead to plan, 3 to 60, default 14.",
        "max_daily_minutes is 30 to 720. Keep the current value unless they asked to "
        f"change it; it is currently {int(pref.max_daily_min or 240)}.",
        "understood is one short sentence, addressed to the student, saying what you changed.",
        "Never invent a course code that is not in their list.",
    ]
    prompt = (
        "Return strict JSON matching this schema:\n"
        + json.dumps(schema, indent=2)
        + "\n\n" + "\n".join(rules)
        + "\n\nREQUEST: " + question
    )
    try:
        parsed = _gemini_json("You convert study-plan requests into JSON settings. "
                              "Output only JSON.", prompt, max_tokens=700)
    except Exception as exc:
        app.logger.warning("Study-plan ask failed: %s", exc)
        message = ("Nexora's AI is at its limit right now. Use the buttons above to "
                   "pick what the plan is for."
                   if _looks_like_quota_error(exc) else
                   "Nexora could not read that. Use the buttons above instead.")
        return jsonify({"error": message}), 503
    if not isinstance(parsed, dict):
        parsed = {}

    focus = "exams" if str(parsed.get("focus") or "").lower() == "exams" else "all"
    wanted = [str(c).strip().upper() for c in (parsed.get("courses") or []) if str(c).strip()]
    # Only codes the student actually has. A hallucinated course would silently
    # empty their plan.
    known = {c.upper() for c in codes}
    chosen = sorted({c for c in wanted if c in known})
    try:
        days = max(3, min(60, int(parsed.get("days") or STUDY_HORIZON_DAYS)))
    except (TypeError, ValueError):
        days = STUDY_HORIZON_DAYS
    try:
        daily = max(30, min(720, int(parsed.get("max_daily_minutes") or pref.max_daily_min)))
    except (TypeError, ValueError):
        daily = int(pref.max_daily_min or 240)

    pref.focus = focus
    pref.focus_courses = ",".join(chosen)
    pref.max_daily_min = daily
    db.session.commit()

    plan = _build_plan(current_user.id, days=days)
    made = _save_plan(current_user.id, plan)
    rows = StudySession.query.filter_by(user_id=current_user.id).filter(
        StudySession.on_date >= _student_today(current_user).isoformat()).order_by(
        StudySession.on_date.asc(), StudySession.start_min.asc()).all()
    return jsonify({
        "ok": True,
        "understood": str(parsed.get("understood") or "")[:300],
        "focus": focus, "courses": chosen, "days": days,
        "max_daily_minutes": daily,
        "created": made,
        "sessions": _sessions_json(rows),
        "reason": "" if rows else ("No tests, midterms or finals are coming up in that "
                                   "window." if focus == "exams" else
                                   "Nothing is due in that window."),
    })


@app.route("/api/study-plan/generate", methods=["POST"])
@login_required
@requires_active
def study_plan_generate():
    # Before planning: anything whose day has passed untouched is missed, and
    # its work has to be considered again rather than left behind.
    missed = _close_off_yesterday(current_user.id)
    plan = _build_plan(current_user.id)
    if not plan:
        # Three different reasons produce an empty plan, and the student can
        # only act on the one that applies to them.
        if not ClassBlock.query.filter_by(user_id=current_user.id).count():
            reason = "Add your timetable first so Nexora knows when you are free."
        elif not _study_items(current_user.id, pref=_study_preference(current_user.id)):
            pref = _study_preference(current_user.id)
            if (pref.focus or "all") == "exams":
                reason = ("No tests, midterms or finals are coming up in this window. "
                          "Switch the plan back to all coursework, or widen the dates.")
            elif _focus_courses_set(pref):
                reason = ("Nothing is due for the courses this plan is limited to. "
                          "Add a course, or plan for all of them.")
            else:
                reason = "Nothing is due in the next two weeks, so there is nothing to plan. Sync your courses if that looks wrong."
        else:
            reason = "Your week has no free time inside your study hours. Widen the hours, or free up a day."
        return jsonify({"ok": True, "created": 0, "sessions": [], "reason": reason,
                        "missed": missed,
                        "catch_up": _catch_up_message(missed, 0)})
    made = _save_plan(current_user.id, plan)
    rows = StudySession.query.filter_by(user_id=current_user.id).filter(
        StudySession.on_date >= _student_today(current_user).isoformat()).order_by(
        StudySession.on_date.asc(), StudySession.start_min.asc()).all()
    return jsonify({"ok": True, "created": made,
                    "missed": missed,
                    "catch_up": _catch_up_message(missed, len(rows)),
                    "sessions": _sessions_json(rows)})


@app.route("/api/study-plan/<session_id>", methods=["POST"])
@login_required
def study_session_state(session_id):
    row = StudySession.query.filter_by(user_id=current_user.id, id=session_id).first()
    if not row:
        return jsonify({"error": "That session is no longer in your plan."}), 404
    data = request.get_json(silent=True) or {}
    state = str(data.get("state") or "").lower()
    if state not in {"planned", "done", "skipped"}:
        return jsonify({"error": "Unknown session state."}), 400
    row.state = state
    if state == "done":
        row.completed_at = datetime.utcnow()
        if "minutes" in data:
            try:
                # Capped at eight hours: a forgotten timer must not teach the
                # estimator that a lab takes a day and a half.
                row.actual_minutes = max(0, min(480, int(float(data.get("minutes") or 0))))
            except (TypeError, ValueError):
                return jsonify({"error": "Give the minutes as a number."}), 400
    else:
        row.completed_at = None
        row.actual_minutes = 0
    db.session.commit()
    return jsonify({"ok": True, "session": _session_json(row)})


@app.route("/api/tour", methods=["POST", "DELETE"])
@login_required
def tour_state():
    """POST marks the first-run tour as seen; DELETE arms it again.

    Finishing and skipping are the same outcome on purpose. A student who
    dismisses it has decided, and showing it again on the next visit would be
    the app arguing with them.
    """
    current_user.tour_done = request.method == "POST"
    db.session.commit()
    return jsonify({"ok": True, "tour_done": bool(current_user.tour_done)})


DAY_NOTE_MAX = 40            # per day, loud rather than silent


def _day_notes_for(user_id, today):
    """Today's list, plus anything unfinished from before.

    Something written yesterday and not done has not stopped mattering, and a
    list that empties itself overnight teaches people not to use it. Finished
    items stay on the day they were finished, so today's list does not fill up
    with last week's ticks.
    """
    rows = (DayNote.query.filter_by(user_id=user_id)
            .filter(db.or_(DayNote.day == today.isoformat(),
                           db.and_(DayNote.done == False,  # noqa: E712
                                   DayNote.day < today.isoformat())))
            .order_by(DayNote.done.asc(), DayNote.created_at.asc())
            .limit(DAY_NOTE_MAX * 2).all())
    return [{
        "id": r.id, "text": r.text, "done": bool(r.done),
        "day": r.day or "",
        # Shown so a carried-over item reads as carried over rather than as
        # something the student wrote this morning and forgot.
        "carried": bool(r.day and r.day < today.isoformat() and not r.done),
    } for r in rows]


@app.route("/api/course-colour", methods=["POST"])
@login_required
def set_course_colour():
    """Pick the colour a course is drawn in, everywhere.

    The index is validated against the palette rather than taken on trust: it
    reaches `style="--ev:var(--c{{ index }})"` in three templates, and a value
    outside the ramp would render the course with no colour at all -- an entry
    that looks broken rather than one that looks wrong.

    Going back to the automatic colour is the DELETE below rather than a value
    here, so a student is never pinned to a choice they did not make.
    """
    data = request.get_json(silent=True) or {}
    # The raw string is checked BEFORE normalising: _normalize_course_code("")
    # returns "OTHER", which is a real bucket in the legend -- so an empty
    # field would quietly recolour it instead of being refused.
    raw = str(data.get("course") or "").strip()
    course = _normalize_course_code(raw) if raw else ""
    if not course:
        return jsonify({"error": "That is not a course."}), 400
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "That is not a colour."}), 400
    if not 0 <= index < COURSE_COLOUR_COUNT:
        return jsonify({"error": "That is not one of the colours."}), 400

    row = CourseColour.query.filter_by(user_id=current_user.id, course_full=course).first()
    if row is None:
        row = CourseColour(id=uuid4().hex, user_id=current_user.id,
                           course_full=course, colour_index=index)
        db.session.add(row)
    else:
        row.colour_index = index
    db.session.commit()
    return jsonify({"ok": True, "course": course, "index": index,
                    "colours": _student_course_colours(current_user.id)})


@app.route("/api/course-colour/<path:course>", methods=["DELETE"])
@login_required
def clear_course_colour(course):
    """Back to the automatic colour for this course."""
    code = _normalize_course_code(str(course or ""))
    row = CourseColour.query.filter_by(user_id=current_user.id, course_full=code).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()
    return jsonify({"ok": True, "colours": _student_course_colours(current_user.id)})


@app.route("/api/day-notes", methods=["POST"])
@limiter.limit("120 per hour")
@login_required
def add_day_note():
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Write something first."}), 400
    today = _student_today(current_user)
    existing = DayNote.query.filter_by(
        user_id=current_user.id, day=today.isoformat()).count()
    if existing >= DAY_NOTE_MAX:
        return jsonify({"error": f"That is {DAY_NOTE_MAX} things for one day, which "
                                 "is already more than a day holds."}), 400
    row = DayNote(id=uuid4().hex, user_id=current_user.id, text=text[:500],
                  day=today.isoformat())
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "notes": _day_notes_for(current_user.id, today)}), 201


@app.route("/api/day-notes/<note_id>", methods=["POST", "DELETE"])
@login_required
def update_day_note(note_id):
    row = DayNote.query.filter_by(user_id=current_user.id, id=note_id).first()
    if not row:
        return jsonify({"error": "That is no longer on your list."}), 404
    today = _student_today(current_user)
    if request.method == "DELETE":
        db.session.delete(row)
    else:
        row.done = not row.done
        row.done_at = datetime.utcnow() if row.done else None
        # A carried-over item that gets ticked belongs to the day it was
        # actually done, or tomorrow's list shows it again as unfinished.
        if row.done:
            row.day = today.isoformat()
    db.session.commit()
    return jsonify({"ok": True, "notes": _day_notes_for(current_user.id, today)})


@app.route("/api/start-my-day", methods=["GET"])
@login_required
def start_my_day():
    # The SAME list the dashboard queue shows. Without view_filter this planned
    # from every task ever imported, oldest first -- so an account that imported
    # first year got "Lab 0 Pre-lab quiz, Sep 07 2025" as the next thing to do
    # today, five items of it, while the dashboard beside it correctly said one
    # thing was due. Two screens disagreeing about what to do next is the same
    # class of bug as two disagreeing about a grade.
    cards = sort_cards(view_filter(build_grouped_cards(load_tasks()), "all"))
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



# Every column holding one of these is String(40). A longer value is silently
# kept by SQLite and rejected outright by Postgres, so the cap belongs here,
# once, rather than at each of the call sites that forgets it.
COURSE_CODE_MAX = 40

# A term reads exactly like a course code: three to six letters, four digits.
# "Winter 2026" is not a subject.
_TERM_WORD = re.compile(r"^(WINTER|SPRING|SUMMER|FALL|AUTUMN|TERM|SEM|SEMESTER|YEAR|SECTION)$", re.I)


def _normalize_course_code(value):
    detected = extract_course_from_text(value or "")
    if detected and not _TERM_WORD.match(re.sub(r"\d.*$", "", detected)):
        return detected[:COURSE_CODE_MAX]
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    if _TERM_WORD.match(re.sub(r"\d.*$", "", cleaned)):
        # Nothing here identifies a course, and inventing one from the term
        # would file every unrelated org unit under the same made-up code.
        return "OTHER"
    return cleaned[:COURSE_CODE_MAX] or "OTHER"


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


def _html_to_text(value):
    """Flatten an LMS description to readable text.

    Canvas returns assignment and announcement bodies as HTML. Everything
    downstream -- the AI pipeline, search, the study tools -- wants plain text,
    and storing markup would also mean storing script tags from a course page.
    """
    text = str(value or "")
    if not text.strip():
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\u2022 ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:120000]


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
    update_rows = (AcademicUpdate.query
                   .filter_by(user_id=current_user.id, course_full=course)
                   .order_by(AcademicUpdate.created_at.desc()).limit(12).all())
    # An update said "New announcement: Midterm exam date" and stopped there --
    # not a word of what the announcement SAID, and no way to open it. The body
    # and the link were both already on the content row it points at; nothing
    # ever joined them. One query for the lot, not one per update.
    _uids = [u.related_uid for u in update_rows if u.related_uid]
    _bodies = {}
    if _uids:
        for row in (CourseContent.query
                    .filter_by(user_id=current_user.id)
                    .filter(CourseContent.source_uid.in_(_uids)).all()):
            _bodies[row.source_uid] = row
    updates = []
    for u in update_rows:
        row = _bodies.get(u.related_uid)
        body = re.sub(r"\s+", " ", (getattr(row, "content_text", "") or "")).strip()
        updates.append({
            "title": u.title, "detail": u.detail,
            "excerpt": body[:220] + ("…" if len(body) > 220 else ""),
            "link": content_href(getattr(row, "source_url", "") or ""),
            "when": local_date_iso(getattr(row, "published_at", "") or "", _user_zone(current_user)),
        })

    lectures = [c for c in contents if c.content_type == "lecture"]
    lectures = sorted(lectures, key=_lecture_sequence_key)
    # `lectures` is still the raw bucket -- the study-tool picker and the
    # summariser read it. What the PAGE shows is re-sectioned below.
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
    # Re-sectioned from the title here rather than trusted from
    # `content_type`, because the published extension promotes everything in a
    # week-numbered module to `lecture` -- the Piazza link, the welcome note,
    # the integrity policy. Doing it on the server fixes the rows already in
    # the database; the extension fix only helps what is scraped after it.
    _sections = _section_course_material(lectures, resources)
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
        # Grouped by the TOP of the breadcrumb rather than the whole of it --
        # twenty headings over twenty-two lectures was the complaint.
        # Four headings in the order a student reaches for them: the slides,
        # the recording of the same lecture, the lab or tutorial, and then
        # everything that is not teaching material at all.
        lecture_groups=_flatten_thin_groups(
            _organise_course_items(_sections["lecture"], course)),
        recording_groups=_flatten_thin_groups(
            _organise_course_items(_sections["recording"], course)),
        recording_count=len(_sections["recording"]),
        lab_groups=_flatten_thin_groups(
            _organise_course_items(_sections["lab"], course)),
        lab_count=len(_sections["lab"]),
        # The stat counts what the tab actually SHOWS as teaching material.
        # Left as the raw bucket it would read 18 above a list of seven --
        # the same "37 courses in the extension, 11 on the dashboard" bug,
        # one screen down.
        lecture_count=len(_sections["lecture"]) + len(_sections["recording"]),
        resource_groups=_flatten_thin_groups(
            _organise_course_items(_sections["other"], course)),
        other_count=len(_sections["other"]),
        slide_count=len(_sections["lecture"]),
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


def _grade_key(name):
    return re.sub(r"[^a-z0-9]+"," ",str(name or "").lower()).strip()


def _scheme_component_names(analysis):
    out=[]
    for comp in (analysis.get("input_components") or analysis.get("components") or []):
        if isinstance(comp,dict) and (comp.get("name") or comp.get("source")):
            out.append(str(comp.get("name") or comp.get("source")))
    return out


def _expand_group_members(analysis,components,_seen=None):
    """Flatten a rule's component list down to names the scheme actually holds.

    An outline that splits a course in two -- "lecture 60, lab 40, you must
    pass the combined mark and also the lecture" -- invites the model to
    describe the combined requirement in terms of the two halves, which are
    themselves derived components and match no grade row. Left unexpanded the
    group resolved to nothing, which the evaluator scored as a failure, and a
    student with every mark at full marks was shown FAIL and an F.
    """
    _seen=set() if _seen is None else _seen
    derived={_grade_key(g.get("name")):g for g in (analysis.get("derived_components") or [])
             if isinstance(g,dict) and g.get("name")}
    out=[]
    for name in components or []:
        key=_grade_key(name)
        if not key or key in _seen: continue
        group=derived.get(key)
        if group:
            _seen.add(key)
            out.extend(_expand_group_members(analysis,group.get("components") or group.get("includes") or [],_seen))
            continue
        out.append(str(name))
    # Order-preserving dedupe: two halves of a course can name the same shared
    # component, and counting it twice would skew the weighted average.
    seen=set(); unique=[]
    for name in out:
        key=_grade_key(name)
        if key in seen: continue
        seen.add(key); unique.append(name)
    return unique


def _group_state(analysis,row_map,components,final_override=None):
    """Weighted percent for a group of components, and how far it can be trusted.

    State is 'ok' when every member carries a mark, 'pending' when at least one
    does not, and 'unbound' when no member names anything in the scheme. Only
    'ok' may fail a student: a requirement that cannot be evaluated yet is not
    a requirement they have broken.
    """
    weights=_component_weight_lookup(analysis)
    known=[_grade_key(n) for n in _scheme_component_names(analysis)]
    members=_expand_group_members(analysis,components)
    # Only one member is the final exam. Applying the hypothetical mark to
    # every member whose name contains "final" gave a group holding both a
    # lecture final and a lab final two overridden components. See
    # _final_name_score.
    final_member=_pick_final_name((n,weights.get(_grade_key(n),0.0)) for n in members)
    values=[]; pending=False; bound=False
    for name in members:
        key=_grade_key(name)
        row=_find_grade_row(row_map,name)
        if row is None and not any(k and key and (key in k or k in key) for k in known):
            continue
        bound=True
        override=final_override if (final_member is not None and name==final_member) else None
        pct=_row_percent(row,override)
        if pct is None:
            pending=True
            continue
        weight=weights.get(key,0.0)
        if not weight:
            for wk,wv in weights.items():
                if key and (key in wk or wk in key):
                    weight=wv; break
        values.append((pct,weight))
    if not bound:
        return None,'unbound'
    if not values:
        return None,'pending'
    weighted=[x for x in values if x[1]>0]
    if weighted:
        total=sum(w for _,w in weighted)
        percent=sum(pct*w for pct,w in weighted)/total if total else None
    else:
        percent=sum(pct for pct,_ in values)/len(values)
    return percent,('pending' if pending else 'ok')


def _group_percent(analysis,row_map,components,final_override=None):
    percent,state=_group_state(analysis,row_map,components,final_override)
    return percent if state=='ok' else None


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
    # A derived component carrying a minimum IS a pass rule. Only the
    # course-workspace route used to convert them, so every aggregate
    # requirement the standalone analyzer found was silently dropped.
    # from_derived keeps this idempotent: _compile_pass_rules rewrites the
    # member list afterwards, so a second pass would not recognise the rewritten
    # rule as the same one and would append a duplicate.
    already_derived={str(r.get("from_derived")) for r in parsed["pass_rules"] if isinstance(r,dict) and r.get("from_derived")}
    for group in parsed.get("derived_components") or []:
        if not isinstance(group,dict) or group.get("minimum_percent") is None:
            continue
        tag=_grade_key(group.get("name")) or _grade_key(json.dumps(group,sort_keys=True,default=str))
        if tag in already_derived:
            continue
        already_derived.add(tag)
        parsed["pass_rules"].append({
            "type":"aggregate_minimum","from_derived":tag,
            "components":group.get("components") or group.get("includes") or [],
            "minimum_percent":group.get("minimum_percent"),
            "message":group.get("message") or f"{group.get('name','Derived component')} must meet the minimum.",
        })
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
    return _compile_pass_rules(parsed)


def _compile_pass_rules(parsed):
    """Bind every pass rule to components the scheme actually contains.

    This is the deterministic check between what the model read in an outline
    and the arithmetic the calculator is about to run on it. Three repairs, in
    order: expand a group written in terms of other derived components; treat a
    group that covers the whole scheme as the overall minimum, which is a
    number the calculator already holds; and mark whatever is still unmatched
    so it is shown to the student rather than enforced against them.

    Without this, an outline saying "you must pass the course overall and also
    pass the lecture half" produced two aggregate rules naming groups that
    matched no component, and a student with every mark at full marks was told
    FAIL, 100%, F.
    """
    names=_scheme_component_names(parsed)
    if not names:
        return parsed
    known={}
    for name in names:
        key=_grade_key(name)
        if key: known.setdefault(key,name)
    weights=_component_weight_lookup(parsed)
    scheme_total=sum(w for w in weights.values() if w>0)
    whole_course=re.compile(r"combined|overall|whole course|course total|total course|final grade|course as a whole",re.I)

    def _match(name):
        key=_grade_key(name)
        if not key: return None
        if key in known: return known[key]
        return next((v for k,v in known.items() if key in k or k in key),None)

    def _unenforceable(rule):
        rule["unbound"]=True
        parsed.setdefault("ambiguities",[]).append(
            f"\u201c{rule.get('message') or 'A pass requirement'}\u201d could not be matched to any component "
            "above, so Nexora is not enforcing it. Name the components it applies to if it matters.")

    compiled=[]
    for rule in parsed.get("pass_rules") or []:
        if not isinstance(rule,dict):
            continue
        rule=dict(rule); rule.pop("unbound",None)
        typ=str(rule.get("type") or "")
        if typ in {"aggregate_minimum","derived_component_minimum","group_minimum"}:
            bound=[]
            for member in _expand_group_members(parsed,rule.get("components") or rule.get("includes") or []):
                match=_match(member)
                if match and match not in bound: bound.append(match)
            covered=sum(max(0.0,weights.get(_grade_key(b),0.0)) for b in bound)
            if whole_course.search(str(rule.get("message") or "")) or (scheme_total and covered>=scheme_total*0.95):
                minimum=rule.get("minimum_percent") if rule.get("minimum_percent") is not None else rule.get("minimum")
                compiled.append({"type":"overall_minimum","minimum_percent":minimum,
                                 "message":rule.get("message") or "Overall course minimum"})
                continue
            if bound:
                rule["components"]=bound; rule.pop("includes",None)
            else:
                _unenforceable(rule)
        elif typ=="component_minimum":
            match=_match(rule.get("component"))
            if match: rule["component"]=match
            else: _unenforceable(rule)
        compiled.append(rule)

    seen=set(); unique=[]
    for rule in compiled:
        sig=json.dumps(rule,sort_keys=True,default=str)
        if sig in seen: continue
        seen.add(sig); unique.append(rule)
    parsed["pass_rules"]=unique
    # Two runs over the same model would otherwise repeat the same warning.
    notes=[]; seen_notes=set()
    for note in parsed.get("ambiguities") or []:
        key=str(note)
        if key in seen_notes: continue
        seen_notes.add(key); notes.append(note)
    parsed["ambiguities"]=notes
    return parsed


def _component_source_name(comp):
    """The grade-row name a scheme component refers to.

    Gemini and the local parsers variously call it `source`, `name`,
    `component` or `label`; normalising in one place is what lets an
    alternative scheme reuse the student's single saved mark.
    """
    return str(comp.get('source') or comp.get('name') or comp.get('component')
               or comp.get('label') or '').strip()


def _evaluate_option(option,row_map,final_override=None,analysis=None):
    points=0.0; missing=[]; details=[]
    analysis=analysis or {}
    virtual=_apply_selection_rules(analysis,row_map)
    # Exactly one component in this option is the final exam. Testing each
    # component for the word "final" handed the hypothetical mark to both
    # "Final Exam" and "Final Lab Exam" at once, so the required-final solver
    # for an alternative scheme answered a question nobody asked. See
    # _final_name_score.
    option_final=_pick_final_name(
        (_component_source_name(c), c.get('weight'))
        for c in (option.get('components') or []) if isinstance(c,dict))
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
            source=_component_source_name(comp)
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
                override=final_override if (option_final and source==option_final) else None
                pct=_row_percent(r,override)
            if pct is None:
                missing.append(f'{source or "Assessment"} is not entered.')
        if pct is not None:
            contribution=pct/100*weight; points+=contribution
            details.append({
                'name': source if not comp.get('source_group') else (comp.get('name') or 'Tests'),
                'percent':round(pct,2),'weight':weight,'points':round(contribution,2)
            })
    return {'option_name':option.get('option_name') or 'Grading option','grade':round(points,2),'eligible':eligible,'complete':not missing,'missing':missing,'details':details,'final_source':option_final}


def _evaluate_pass_rules(analysis,row_map,course_grade,final_override=None):
    """Score every mandatory rule as passed, failed, or not yet answerable.

    `ok` is deliberately tri-state. Scoring "cannot be evaluated" as False --
    which is what `actual is not None and actual>=required` did for every rule
    type here -- turns a rule Nexora failed to bind, or one whose marks are not
    in yet, into a course failure. A student who had entered every component at
    full marks was shown FAIL, 100% and an F, because two aggregate rules named
    groups that resolved to no components at all. A requirement nobody can
    check is not a requirement the student has broken.
    """
    results=[]; failed=False
    # A component_minimum on "Final Lab Exam" is not a rule about the final
    # exam, and must not be answered with the hypothetical final mark.
    scheme_final=_scheme_final_name(analysis)
    rules=analysis.get('pass_rules') or []
    if not rules:
        for name,minimum in _outline_rule_minimums(analysis).items():
            rules.append({'type':'component_minimum','component':name,'minimum_percent':minimum,'message':f'{name} minimum'})
        rules.append({'type':'overall_minimum','minimum_percent':float(analysis.get('pass_mark') or 50),'message':'Overall course minimum'})
    known=[_grade_key(n) for n in _scheme_component_names(analysis)]
    def _in_scheme(name):
        key=_grade_key(name)
        return bool(key) and any(k and (key in k or k in key) for k in known)
    unmatched='Not matched to a component in this scheme, so it is not being enforced.'
    for rule in rules:
        typ=rule.get('type'); ok=None; actual=None; required=None; note=''
        if rule.get('unbound'):
            # _compile_pass_rules could not match this requirement to anything
            # in the scheme. It stays visible so the student can correct it,
            # and stays unenforced, because enforcing it can only fail them.
            results.append({'message':rule.get('message') or typ,'passed':None,'state':'pending',
                            'actual':None,'required':rule.get('minimum_percent'),'note':unmatched})
            continue
        if typ=='component_minimum':
            name=rule.get('component'); row=_find_grade_row(row_map,name)
            actual=_row_percent(row, final_override if (scheme_final and _names_same_component(name,scheme_final)) else None)
            required=float(rule.get('minimum_percent') or 0)
            if actual is not None: ok=actual>=required
            elif row is None and not _in_scheme(name): note=unmatched
        elif typ=='any_component_minimum':
            vals=[_row_percent(_find_grade_row(row_map,n)) for n in rule.get('components') or []]
            vals=[v for v in vals if v is not None]; actual=max(vals) if vals else None
            required=float(rule.get('minimum_percent') or 0)
            if actual is not None: ok=actual>=required
        elif typ=='raw_minimum':
            row=_find_grade_row(row_map,rule.get('component')); actual=None if not row or row.score is None else float(row.score)
            required=float(rule.get('minimum_raw') or 0)
            if actual is not None: ok=actual>=required
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
            required=float(rule.get('minimum_percent') or 0)
            if actual is not None: ok=actual>=required
        elif typ in {'aggregate_minimum','derived_component_minimum','group_minimum'}:
            actual,group_state=_group_state(analysis,row_map,rule.get('components') or rule.get('includes') or [],final_override)
            required=float(rule.get('minimum_percent') or rule.get('minimum') or 0)
            if group_state=='ok' and actual is not None: ok=actual>=required
            elif group_state=='unbound': note=unmatched
        elif typ=='overall_minimum':
            actual=None if course_grade is None else float(course_grade)
            required=float(rule.get('minimum_percent') or 0)
            if actual is not None: ok=actual>=required
        else:
            continue
        if ok is False: failed=True
        results.append({'message':rule.get('message') or typ,'passed':ok,
                        'state':'pass' if ok is True else 'fail' if ok is False else 'pending',
                        'actual':None if actual is None else round(actual,2),'required':required,
                        'note':note or ('' if ok is not None else 'Waiting on a mark that has not been entered yet.')})
    return (not failed),results


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

def _final_name_score(name):
    """How much a component name means *the* final exam. 0 means it does not.

    `\bfinal\b` also matches "Final Lab Exam", and BIOL 1103 has both. Wherever
    that mattered, every matching component was treated as the final at once:
    the single-scheme solver excluded both from term work and computed against
    whichever came last, and the alternative-scheme solver applied one
    hypothetical mark to two different components. A course with a lecture
    final and a lab final is ordinary; only one of them is what "what do I need
    on the final" means.

    Four sites need this answer, so there is one definition of it.
    """
    text = str(name or "")
    if not re.search(r"\bfinal\b", text, re.I):
        return 0
    lowered = text.lower()
    if re.fullmatch(r"\s*final\s+exam(ination)?\s*", lowered):
        return 4
    if "final exam" in lowered and "lab" not in lowered:
        return 3
    if "lab" not in lowered:
        return 2
    return 1


def _pick_final_name(candidates):
    """The best "final exam" name from (name, weight) pairs, or None.

    A tie goes to the heavier component: between two names that read equally
    like the final, the one worth more of the course is the one a student is
    asking about. Never guesses -- with no candidate the caller simply offers
    no required-final figure.
    """
    best = None
    best_score = 0
    best_weight = 0.0
    for name, weight in candidates:
        score = _final_name_score(name)
        if not score:
            continue
        try:
            value = float(weight or 0)
        except (TypeError, ValueError):
            value = 0.0
        if score > best_score or (score == best_score and value > best_weight):
            best, best_score, best_weight = name, score, value
    return best


def _scheme_final_name(analysis):
    """The one component in this scheme that means *the* final exam."""
    weights = _component_weight_lookup(analysis)
    return _pick_final_name((n, weights.get(_grade_key(n), 0.0))
                            for n in _scheme_component_names(analysis))


def _names_same_component(left, right):
    """Whether two component names refer to the same thing.

    A pass rule names its component in the outline's words, which are not
    always the scheme's exact spelling, so this is the same tolerant containment
    match _find_grade_row uses rather than equality.
    """
    a, b = _grade_key(left), _grade_key(right)
    return bool(a) and bool(b) and (a in b or b in a)


def _pick_final_row(rows):
    """Which saved mark is *the* final exam, for the required-final solver."""
    chosen = _pick_final_name((r.name, r.weight) for r in rows)
    if chosen is None:
        return None
    # Identity, not a name match: two rows can share a name and the object is
    # what the caller compares against.
    for row in rows:
        if row.name == chosen:
            return row
    return None


def _is_bonus_row(row):
    """Bonus and extra-credit weight is additive, so it never sits below the line.

    Same signal _check_total uses on analysis components, applied to the saved
    grade rows. Keeping one definition matters: if the two ever disagree, an
    outline flagged as totalling 100% would be marked out of 103.
    """
    return bool(re.search(r'bonus|extra\s*credit',
                          f"{row.name or ''} {row.category or ''}", re.I))


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
        # None, not 0: with nothing entered yet the overall minimum is unanswered,
        # and passing 0 made it read as a failed requirement on a blank calculator.
        pass_ok,rule_results=_evaluate_pass_rules(analysis,row_map,official_grade)
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
                # The override satisfies exactly one component, so only that
                # one's "is not entered" message may be ignored here. Matching
                # the words "final exam" also swallowed a missing "Final Lab
                # Exam" and solved a target the student could not reach.
                option_final=ev.get('final_source')
                nonfinal_missing=[m for m in ev['missing']
                                  if not (option_final and m.startswith(f'{option_final} '))]
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
    # The denominator is the BASE scheme -- every weighted row except a bonus.
    # Dividing by base+bonus was wrong three separate ways on the same outline:
    # a student perfect on all seven graded components of BIOL 1103 who declined
    # the 2% bonus was shown 97.09% rather than 100%, so optional work he never
    # had to do cost him marks; a student who earned it in full was shown 100
    # while _evaluate_option -- which sums points without normalising -- said
    # 103, so the two paths disagreed about the same student; and an unmarked
    # bonus row kept `remaining` above zero for ever, which left official_grade
    # None and the whole result panel stuck on INCOMPLETE. A bonus is additive:
    # it belongs in the numerator only.
    scheme_total=sum(max(0.0,float(r.weight or 0)) for r in rows if not _is_bonus_row(r)) or float(analysis.get('total_weight') or 100)
    earned=0.0; completed_weight=0.0; base_completed=0.0
    # Separate the final from all other work. Current standing uses every saved
    # mark, but the What-do-I-need solver intentionally ignores a saved final
    # so students can run a hypothetical target at any time.
    nonfinal_earned=0.0; nonfinal_completed_weight=0.0
    # Decided once, up front. Testing each row against a regex made every row
    # containing the word "final" the final exam -- see _pick_final_row.
    final_row=_pick_final_row(rows)
    for r in rows:
        is_final=(r is final_row)
        if r.score is not None and r.weight>0:
            contribution=(_row_percent(r)/100)*float(r.weight)
            is_bonus=_is_bonus_row(r)
            # Earned bonus points count towards the mark and towards what is
            # still needed on the final; bonus WEIGHT never counts towards how
            # much of the course is outstanding, because none of it is owed.
            earned+=contribution; completed_weight+=float(r.weight)
            if not is_bonus: base_completed+=float(r.weight)
            if not is_final:
                nonfinal_earned+=contribution
                if not is_bonus: nonfinal_completed_weight+=float(r.weight)
    remaining=max(0.0,scheme_total-base_completed); normalized_target=target/100*scheme_total
    needed_average=None if remaining<=0 else (normalized_target-earned)/remaining*100
    current_on_completed=earned/base_completed*100 if base_completed else 0
    official_grade=earned/scheme_total*100 if remaining<=0 and scheme_total else None
    pass_ok,rule_results=_evaluate_pass_rules(analysis,row_map,official_grade)
    required_final=None; max_with_perfect_final=None
    if final_row and final_row.weight>0:
        other_unfinished=max(0.0, scheme_total - nonfinal_completed_weight - float(final_row.weight))
        assumed=max(0.0,min(100.0,float(assumed_other_average or 100)))
        # Where the course lands if the final is perfect. Without this the panel
        # had no way to say a target is unreachable and printed "you need
        # 158.33% on the final exam" -- arithmetically the right answer to the
        # wrong question, and it reads as the page being broken.
        if scheme_total:
            max_with_perfect_final=(nonfinal_earned+(assumed/100)*other_unfinished
                                    +float(final_row.weight))/scheme_total*100
        required_final=(normalized_target-nonfinal_earned-(assumed/100)*other_unfinished)/float(final_row.weight)*100
        final_min=analysis.get('final_minimum',profile.final_minimum if profile else None)
        if final_min is not None: required_final=max(required_final,float(final_min))
    decorated=_decorate_grade_result(user_id,analysis,official_grade)
    official_status='INCOMPLETE' if official_grade is None else ('PASS' if pass_ok and decorated['course_letter_rules_passed'] else 'FAIL')
    if official_status=='FAIL':
        decorated['letter_grade']=_fail_letter(user_id)
    return {'mode':'single_scheme','target':target,'official_grade':official_grade,'official_status':official_status,'pass_rules':rule_results,'earned_course_points':round(earned,2),'completed_weight':round(completed_weight,2),'remaining_weight':round(remaining,2),'current_on_completed':round(current_on_completed,2),'needed_average':None if needed_average is None else round(needed_average,2),'required_final':None if required_final is None else round(required_final,2),'max_with_perfect_final':None if max_with_perfect_final is None else round(max_with_perfect_final,2),'best_possible':round((earned+remaining)/scheme_total*100,2) if scheme_total else 0,'worst_possible':round(earned/scheme_total*100,2) if scheme_total else 0,**decorated}, None


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


# Words that mean "the mark I am aiming for" rather than "a mark I already
# have". "What do I need to get 80" is a target; "I got 80 on the midterm" is
# not, and answering the second as though it were the first would hand a
# student the wrong number with a straight face.
_TARGET_CUE = re.compile(
    r"(?:need|needed|require[sd]?|get|getting|achieve|score|earn|end\s+up\s+with|"
    r"finish\s+with|aiming\s+for|target|to\s+reach|for\s+an?)\b[^.?!]{0,40}?$",
    re.I)
_PASS_CUE = re.compile(r"\b(pass|passing|not\s+fail|avoid\s+failing)\b", re.I)


def _target_from_question(user_id, question, analysis):
    """What mark the student is asking about, read out of their own words.

    The model is never allowed to do grade arithmetic -- see `ask_course_ai` --
    so the only way it can answer "what do I need for a B+" is to be handed
    numbers that were computed FOR a B+. `_compute_official_result` takes a
    target and does that maths already; all that was missing was reading the
    target out of the sentence, which is a job for a regex and not for a model.
    Before this, every question was answered with the numbers for a bare pass,
    so "what do I need to get 80" got the mark needed for 50 -- confidently,
    and with the app's own arithmetic behind it.

    Deterministic on purpose. A model reading "85" as "58" once in a hundred
    times is a student planning a term around a number nobody checked.

    Returns {"target", "label", "basis"} or None when the question names no
    mark, in which case the caller keeps the pass-mark default.
    """
    text = str(question or "")
    if not text.strip():
        return None

    # 1. An explicit percentage, but only where the sentence is asking FOR it.
    for match in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*(%|percent)?", text):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if not (0 <= value <= 100):
            continue
        before = text[:match.start()]
        # A number glued to letters is a course code (SYSC2004), not a mark.
        if before[-1:].isalpha():
            continue
        # A percent sign is NOT enough on its own: "is the final worth 40%" is a
        # question about a weight, and answering it with the numbers needed to
        # finish on 40 would be a confident non-sequitur. The sentence has to be
        # asking FOR the mark.
        if _TARGET_CUE.search(before):
            return {"target": value, "label": f"{value:g}%", "basis": "the mark you named"}

    # 2. A letter grade, matched against the student's OWN scale. Uppercase as
    #    written, so the "a" in "what do I need" is never read as an A.
    profile = _get_user_grading_scale(user_id)
    bands = sorted(profile.get("scale") or [], key=lambda b: -len(str(b.get("letter") or "")))
    for band in bands:
        letter = str(band.get("letter") or "").strip()
        if not letter or letter.upper() == "F":
            continue
        if re.search(r"(?<![A-Za-z0-9+-])" + re.escape(letter) + r"(?![A-Za-z0-9+-])", text):
            return {"target": float(band["minimum"]),
                    "label": f"{letter} ({band['minimum']:g}%)",
                    "basis": "your saved grading scale"}

    # 3. Passing, which is the course's own pass mark when the outline gave one.
    if _PASS_CUE.search(text):
        mark = analysis.get("pass_mark")
        try:
            value = float(mark)
        except (TypeError, ValueError):
            value = 50.0
        return {"target": value, "label": f"a pass ({value:g}%)", "basis": "this course's pass mark"}

    return None


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
@ai_limit
@login_required
@requires_active
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

    # "What do I need for a B+" can only be answered with numbers computed FOR
    # a B+. The model must never do the arithmetic, so the target is read out
    # of the question here and handed to the engine that already owns it.
    asked = _target_from_question(current_user.id, question, analysis)
    official_result,official_error=_compute_official_result(
        current_user.id, course, target=asked['target'] if asked else 50.0)
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
        + (f"The numbers below were computed for a target of {asked['label']}, taken from "
           f"{asked['basis']}. `required_final` is the mark needed on the final exam to reach "
           f"exactly that, and `max_with_perfect_final` is the best possible finish even with "
           f"100% on the final. If max_with_perfect_final is below the target, say plainly "
           f"that it is out of reach and give that best possible figure.\n\n"
           if asked else
           "The student named no particular mark, so the numbers below are for a bare pass.\n\n")
        + "=== PRE-COMPUTED OFFICIAL GRADE RESULT (already calculated by the app's grading "
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
        # What Nexora understood the question to be asking for, shown on the
        # page. A misread target is the one failure here that would look
        # completely authoritative, so it is never invisible -- the same reason
        # /api/study-plan/ask returns `understood`.
        'asked_about':asked['label'] if asked else '',
        'asked_basis':asked['basis'] if asked else '',
        # The numbers the answer was built from, so the student can check the
        # sentence against the arithmetic rather than taking prose on trust.
        'numbers':{
            'target':official_summary.get('target') if official_summary else None,
            'official_grade':official_summary.get('official_grade') if official_summary else None,
            'required_final':official_summary.get('required_final') if official_summary else None,
            'max_with_perfect_final':official_summary.get('max_with_perfect_final') if official_summary else None,
        } if official_summary else None,
    })


@app.route('/api/course/<course_code>/outline/upload', methods=['POST'])
@ai_limit
@login_required
@requires_active
def upload_course_outline(course_code):
    course=_normalize_course_code(course_code)
    uploads=request.files.getlist('files') or request.files.getlist('file')
    uploads=[u for u in uploads if u and u.filename]
    # Pasted text takes the same road as an uploaded file. It used to go to the
    # generic analyzer instead, which keeps nothing: on the grade calculator a
    # student picked a course, pasted their outline, watched the components
    # appear -- and then vanish, as the page re-read the (empty) saved copy.
    # Storing it here means the analyze route, the confirm step, the official
    # result and Ask all find it, exactly as they would an upload.
    pasted=''
    if not uploads:
        body=request.get_json(silent=True) or {}
        pasted=_sanitize_extracted_text(str(body.get('text') or ''))
        if not pasted:
            return jsonify({'error':'Choose one or more outline files or screenshots, or paste the outline text.'}),400
        if len(pasted)<200:
            return jsonify({'error':'That is too short to be a course outline. Paste the grading section at least.'}),422
    if len(uploads)>8:
        return jsonify({'error':'Upload at most 8 screenshots/pages at once.'}),400
    extracted_parts=[]; hashes=[]; total_size=0; names=[]
    if pasted:
        names=['Pasted course outline']
        hashes=[hashlib.sha256(pasted.encode('utf-8')).hexdigest()]
        total_size=len(pasted.encode('utf-8'))
        extracted_parts=[pasted]
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
@requires_active
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
    # An outline that never states its weights must not come back with any.
    # See _scheme_is_uncorroborated: this is the one failure mode where the
    # model is both wrong and confident, so it is checked arithmetically
    # rather than asked for in the prompt.
    uncorroborated = _scheme_is_uncorroborated(final, excerpt)
    if uncorroborated:
        final["components"] = []
        final["grading_options"] = []

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
    if uncorroborated:
        ambiguities.append(
            "This outline does not state the weight of each assessment -- it says the "
            "weighting will be announced separately. Nexora has not guessed them. Add "
            "each weight yourself once your instructor gives them, and the grade "
            "calculator will work normally."
        )
    # The model chain falls back to flash tiers when a Pro model is not
    # available on this key. That silently lowers reasoning quality on the one
    # task where it matters most, so say so rather than hiding it.
    if models_used and not any("pro" in str(m).lower() for m in models_used):
        # The operator half of this -- which model, which variable to set -- goes
        # to the log and /admin/health. A student cannot act on an environment
        # variable, and "needs your review" is a list of facts about their own
        # outline, not a place to file someone else's configuration.
        app.logger.warning(
            "CONFIG: outline analysed with %s because no Pro-tier Gemini model was available "
            "for this key. Accuracy on complex grading schemes is lower than usual; set "
            "GEMINI_REASONING_MODEL to a Pro model to restore it.", models_used[0])
        ambiguities.append(
            "This outline was read by a smaller model than usual, so the weights and rules "
            "below are worth checking more carefully than normal before you confirm them."
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
    # A refused scheme is not a confident reading, whatever the auditor scored.
    if uncorroborated:
        score = min(score, 40)
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

def _scheme_is_uncorroborated(model, source_text):
    """True when not one extracted weight appears as a number in the outline.

    The case this exists for: an outline that says the weighting "will be
    announced in class during the first week". The three-stage pipeline read
    the four assessment names, gave them plausible weights, and scored itself
    high confidence -- a fabricated grading scheme presented as fact. A student
    plans a term around that and never finds out it was invented.

    No prompt rule can be trusted to prevent this, because the failure is the
    model being confident. This is arithmetic instead: a real outline always
    states at least one of its weights verbatim.

    All-or-nothing on purpose. A single weight can legitimately be derived and
    never appear -- "4 assignments at 6% each" is a 24% component and the
    outline never writes 24 -- so dropping components individually would delete
    real ones. Only a scheme with no numeric support anywhere is refused.
    """
    components = [c for c in (model.get("components") or []) if isinstance(c, dict)]
    for option in (model.get("grading_options") or []):
        if isinstance(option, dict):
            components += [c for c in (option.get("components") or []) if isinstance(c, dict)]
    weights = []
    for comp in components:
        try:
            weight = float(comp.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            weights.append(weight)
    if not weights:
        return False
    tokens = re.findall(r"\d+(?:\.\d+)?", source_text or "")
    stated = {t for t in tokens}
    stated |= {t.rstrip("0").rstrip(".") for t in tokens if "." in t}
    values = []
    for t in tokens[:120]:            # bounded: outlines carry dates and room numbers too
        try:
            value = float(t)
        except ValueError:
            continue
        if value > 0 and value not in values:
            values.append(value)
    for weight in weights:
        token = str(int(weight)) if float(weight).is_integer() else str(weight)
        if token in stated:
            return False
        # A syllabus priced in points never writes the percentage: "homework
        # 100 points out of 500" is a 20% component and 20 appears nowhere.
        # Converting it is correct, so the arithmetic has to be allowed for or
        # this guard would blank a scheme that was read perfectly.
        for part in values:
            for whole in values:
                if whole > 0 and abs(part / whole * 100 - weight) < 0.05:
                    return False
    return True


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
2. Some outlines define MULTIPLE ALTERNATIVE grading schemes (e.g. "Option 1: Final exam 100%", "Option 2: best test 15% + tutorials 5% + final 80%") where the student's final grade is whichever option gives them the best result. If you see this pattern, put EACH option as its own entry in grading_options (each with its own components list summing to 100), and set the top-level `components` to the option that asks the student for the most separate marks -- the one with the most components, and if several tie, the one listed LAST, since outlines almost always order options from the simplest to the most complete. Choose it by that rule every time rather than by judgement: the top-level list is the card set the student is shown by default, and it must not change between two analyses of the same outline. Promoting the fullest option also means the student enters every mark, so every other option can be calculated for them. Add a rule noting that alternate options exist and the best one is applied automatically.
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
16. An alternative scheme is sometimes open only to students who actually sat some number of a group of assessments, e.g. "options 3 and 4 require that you have written at least two and three of the written tests respectively". Record that on the option itself as eligibility, naming the group in from_components using the same component names you used elsewhere, the count in requires_completed, and a plain-language message. Leave eligibility off an option that every student can use. This is about whether an option is available at all, which is not the same as a minimum mark inside it -- a mark floor belongs in component_minimums.
17. Every name inside a derived_components entry must be one of the components you listed above, spelled the same way. Never name one derived component inside another, and never create a derived component for the course as a whole -- a requirement to pass the course overall is pass_mark, not a group."""


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
@ai_limit
@login_required
@requires_active
def analyze_course_outline(course_code):
    course = _normalize_course_code(course_code)
    # Bound once, here: the login proxy is only valid for the life of a request,
    # and this work is about to be handed to a thread that outlives it.
    user_id = current_user.id
    # No longer a gate. This used to refuse with a 428 until the student had
    # saved a grading scale in Settings -- which is every new account, and the
    # error surfaced three cards below the button, so the owner of the app
    # uploaded his outline four times and reported the button as broken. The
    # scale is only needed to turn a LETTER rule ("C- or better") into a
    # number, and `_letter_requirement_result` already returns `passed: None`
    # when it cannot -- pending, never failing. So: analyze with the scale on
    # file (the default when none is saved), and if the outline actually
    # contains a letter rule, say so in the response and let the page point at
    # Settings. A student whose outline is all percentages never hears of it.
    scale_profile = _get_user_grading_scale(user_id)
    data = request.get_json(silent=True) or {}
    source_uid = str(data.get('source_uid') or '')
    query = CourseContent.query.filter_by(user_id=user_id, course_full=course)
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
    # Both word orders. "A grade of C- or better is required to proceed" is
    # the commonest way a prerequisite is written and the first pattern -- verb
    # first, letter after -- never saw it, so the fallback dropped the one rule
    # that decides whether a student can take next year's course.
    # A letter grade is one of A-F with an optional sign, standing alone. Not
    # `\b([ABCDF][+-]?)\b`: the closing \b cannot sit between "-" and a space
    # (both non-word), so a signed letter never matched and "C- or better"
    # was read as no rule at all -- the sign is the whole difference between
    # C- and C, and it was the case that was silently dropped.
    LETTER=r'(?<![A-Za-z0-9])([ABCDF][+-]?)(?![A-Za-z0-9+-])'
    letter_patterns=[
        r'(?:required|must|need(?:ed)?)\s+(?:to\s+)?(?:achieve|obtain|earn)?[^.\n]{0,90}?'+LETTER+r'\s+or\s+(?:better|higher|above)',
        LETTER+r'\s+or\s+(?:better|higher|above)[^.\n]{0,40}?(?:is\s+|are\s+)?(?:required|needed|necessary|mandatory)',
        r'(?:minimum|at\s+least)\s+(?:a\s+|an\s+|of\s+)?(?:grade\s+of\s+)?'+LETTER,
    ]
    seen_spans=[]
    for pat in letter_patterns:
        for match in re.finditer(pat,text_for_rules,re.I):
            if any(a<=match.start()<b for a,b in seen_spans): continue
            seen_spans.append((match.start(),match.end()))
            letter=match.group(1).upper()
            sentence=re.split(r'[\n.]',text_for_rules[match.start():match.start()+240])[0].strip()
            nearby=text_for_rules[max(0,match.start()-100):match.start()+240].lower()
            scope='prerequisite' if re.search(r'prerequisite|proceed|continue|advance|next|admission|enter',nearby) else 'course_pass'
            if not any(str(x.get('minimum_letter','')).upper()==letter and x.get('scope')==scope for x in letter_requirements if isinstance(x,dict)):
                letter_requirements.append({'minimum_letter':letter,'scope':scope,'message':sentence or f'{letter} or better required'})
    parsed['letter_requirements']=letter_requirements
    if not parsed.get('input_components'): parsed['input_components']=clean
    parsed['components']=clean; parsed['total_weight']=round(float(parsed.get('total_weight') or total or 100),2)
    # Bind after the component list is settled: the pipeline's list and the
    # deterministic parser's differ, and a rule can only be matched against the
    # one that will actually be shown.
    parsed=_compile_pass_rules(parsed)
    parsed['confirmed']=False
    profile = CourseOutlineProfile.query.filter_by(user_id=user_id, course_full=course).first()
    if not profile:
        profile=CourseOutlineProfile(id=uuid4().hex,user_id=user_id,course_full=course)
        db.session.add(profile)
    profile.source_uid=row.source_uid; profile.outline_title=row.title; profile.grading_json=json.dumps(parsed,ensure_ascii=False)
    try: profile.pass_mark=float(parsed.get('pass_mark') or 50)
    except Exception: profile.pass_mark=50.0
    try: profile.final_minimum=None if parsed.get('final_minimum') in (None,'') else float(parsed.get('final_minimum'))
    except Exception: profile.final_minimum=None
    profile.analysis_notes='; '.join(str(x) for x in (parsed.get('rules') or []))[:4000]
    profile.updated_at=datetime.utcnow()
    # A confirmed new outline fully replaces the previous calculator. Never mix two courses/outlines.
    GradeEntry.query.filter_by(user_id=user_id,course_full=course).delete(synchronize_session=False)
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
            id=uuid4().hex,user_id=user_id,course_full=course,
            name=str(comp.get('name') or 'Assessment')[:255],
            weight=float(comp.get('weight') or 0),score=None,
            max_score=float(comp.get('default_max') or 100),
            category=str(comp.get('category') or _component_category(comp.get('name')))[:80],
            notes=str(comp.get('notes') or '')[:255],
        )
        db.session.add(row_entry)
    # Keep only the outline that produced the active profile when this was a manual upload.
    if str(row.source_uid or '').startswith('manual-outline:'):
        stale=CourseContent.query.filter_by(user_id=user_id,course_full=course,content_type='outline').filter(CourseContent.id!=row.id).all()
        for old_outline in stale: db.session.delete(old_outline)
    db.session.commit()
    scale_note = ""
    if not scale_profile.get("configured") and (parsed.get("letter_requirements") or []):
        letters = ", ".join(sorted({str(x.get("minimum_letter") or "") for x in parsed["letter_requirements"] if isinstance(x, dict) and x.get("minimum_letter")}))
        scale_note = (f"This outline has a letter-grade rule ({letters}). Nexora is using a standard "
                      "scale for now; save your university's own in Settings so it is read exactly.")
    return jsonify({"ok":True,"fallback":used_fallback,"analysis":parsed,"pass_mark":profile.pass_mark,
                    "final_minimum":profile.final_minimum,
                    "scale_configured":bool(scale_profile.get("configured")),"scale_note":scale_note})


def _scheme_groups(analysis, components):
    """The derived components of a scheme with the weight each one carries --
    the sum of its real members, since a group has no weight of its own."""
    out=[]
    for group in (analysis.get('derived_components') or []) if isinstance(analysis,dict) else []:
        if not isinstance(group,dict) or not group.get('name'): continue
        members=_expand_group_members(analysis,group.get('components') or group.get('includes') or [])
        total=0.0
        for member in members:
            key=_grade_key(member)
            row=next((c for c in components if isinstance(c,dict) and _component_keys_match(key,_grade_key(c.get('name')))),None)
            if row: total+=float(row.get('weight') or 0)
        out.append({"name":str(group.get('name')),"components":[str(m) for m in members],"weight":round(total,2)})
    return out


def _scheme_part_weight(analysis, components, name):
    """(name, weight of the course) for the part of the scheme `name` denotes:
    a derived group if one matches, else a single component, else (name, 0)."""
    key=_grade_key(name)
    for group in _scheme_groups(analysis,components):
        if _component_keys_match(key,_grade_key(group['name'])) and group['weight']>0:
            return group['name'],group['weight']
    row=next((c for c in components if isinstance(c,dict) and c.get('name')
              and _component_keys_match(key,_grade_key(c.get('name')))),None)
    if row and float(row.get('weight') or 0)>0:
        return str(row.get('name')),float(row.get('weight'))
    return str(name),0.0


def _apply_outline_edits(user_id, course, profile, analysis, data):
    """Apply a set of structured edits to a saved grading policy and keep the
    grade rows in step: weights, renames, removals, additions, pass rules.
    Returns the list of changes made (empty when nothing in `data` applied),
    committing only when there was something to commit.

    The body of the correction route, factored out so an amendment read from
    an announcement takes exactly the same road as a student's own edit --
    including the part that never deletes an entered mark."""
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
        # A weight stated against a part of the course -- "a bonus 1% is added
        # to your final lab grade", where the lab is 40% of the course -- is
        # converted HERE. The model that read the sentence names the component
        # it applies to; the server does the sum, and the note shows it.
        raw_basis=item.get('weight_of')
        bases=[str(b).strip() for b in (raw_basis if isinstance(raw_basis,list) else [raw_basis]) if str(b or '').strip()]
        bases=[b for b in bases if _norm_key(b) not in ('course','final grade','course grade','final course grade')]
        basis_note=''
        if bases:
            # A group first: "your final lab grade" in a 60/40 course is the
            # whole lab component (reports + lab exam), not its largest row.
            # A part the scheme never named as a group arrives as a list of
            # the rows that make it up, and is summed here.
            parts=[_scheme_part_weight(analysis,components,b) for b in bases]
            parts=[(n,w) for n,w in parts if w>0]
            parent_weight=round(sum(w for _,w in parts),2)
            if parent_weight>0:
                parent_name=' + '.join(n for n,_ in parts)
                course_weight=round(weight*parent_weight/100.0,2)
                basis_note=f"{weight:g}% of {parent_name} ({parent_weight:g}% of the course) = {course_weight:g}% of the course grade."
                weight=course_weight
        category=str(item.get('category') or _component_category(name))[:80]
        if re.search(r'bonus|extra credit',f'{name} {category}',re.I): category='bonus'
        notes=(str(item.get('notes') or '').strip()+(' '+basis_note if basis_note else '')).strip()[:255]
        components.append({"name":name[:255],"weight":weight,"category":category,"notes":notes})
        existing_keys.add(key)
        changed.append(f"Added: {name} ({weight:g}%)"+(f" — {basis_note}" if basis_note else ''))

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

    # A condition with no number in it -- "quizzes are open for two weeks",
    # "the survey opens late November" -- is still part of the policy and is
    # kept in the outline's own words, next to the rules the analysis found.
    rules=[str(r) for r in (analysis.get('rules') or []) if str(r).strip()] if isinstance(analysis.get('rules'),list) else []
    for sentence in (data.get('rules') if isinstance(data.get('rules'),list) else []):
        sentence=str(sentence or '').strip()[:500]
        if sentence and sentence not in rules:
            rules.append(sentence); changed.append(f"Noted: {sentence}")
    analysis['rules']=rules

    if pass_mark_edit is not None:
        try:
            new_pass_mark=max(0.0,min(100.0,float(pass_mark_edit)))
            if round(new_pass_mark,2)!=round(float(analysis.get('pass_mark') or 50),2):
                changed.append(f"Overall pass mark: {analysis.get('pass_mark')}% → {new_pass_mark}%")
            analysis['pass_mark']=new_pass_mark
        except (TypeError,ValueError):
            pass

    if not changed:
        return []

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
    rows=GradeEntry.query.filter_by(user_id=user_id,course_full=course).all()
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
            db.session.add(GradeEntry(id=uuid4().hex,user_id=user_id,course_full=course,
                                       name=new_name[:255],
                                       weight=float(comp.get('weight') or 0),score=None,max_score=100.0,
                                       category=str(comp.get('category') or 'assessment')[:80],
                                       notes=str(comp.get('notes') or '')[:255]))
    db.session.commit()
    return changed


@app.route("/api/course/<course_code>/outline/correct", methods=["POST"])
@ai_limit
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
    changed=_apply_outline_edits(current_user.id,course,profile,analysis,data)
    if not changed:
        return jsonify({'error':'No valid corrections were provided.'}),400
    return jsonify({'ok':True,'changed':changed})


# ---- Adding to an outline that is already on file ---------------------------
#
# A grading policy changes after the outline is read: a bonus announced on
# Brightspace, an email moving weight from a cancelled quiz onto the final, a
# minimum stated in a lab manual. The student's move is to paste it and say
# "add this to the outline" -- and the tutor answered that it had no access
# to edit the outline, which is the app refusing the one thing it is for.
#
# The model reads the addition and returns the EDITS it makes, as structured
# data; the server applies them through the same code as a student's own
# correction, so an entered mark is never lost, and any amount stated against
# a part of the course is converted here rather than by the model.

_AMEND_INTENT = re.compile(
    r"\b(add|put|include|record|apply|save|update|attach)\b[^.?!\n]{0,60}\b(outline|syllabus|grading|grade (?:scheme|policy|calculator)|course policy|my grades?)\b"
    r"|\b(outline|syllabus)\b[^.?!\n]{0,40}\b(add|update|include|record)\b", re.I)


def _wants_outline_amendment(prompt):
    return bool(_AMEND_INTENT.search(prompt or ""))


def _outline_amendment_edits(course, analysis, text):
    """Ask the model what a piece of text changes about the scheme on file --
    only what it states, in the correction route's own shape."""
    components = [{"name": c.get("name"), "weight": c.get("weight"), "category": c.get("category")}
                  for c in (analysis.get("components") or []) if isinstance(c, dict)]
    system = (
        "You read a short addition to a university course's grading policy -- an announcement, an email, "
        "a Brightspace page -- and return ONLY the edits it makes to the grading scheme Nexora already holds, "
        "as strict JSON in the return_shape given.\n"
        "1. Record only what the text states. Never invent a weight, a threshold or a component.\n"
        "2. A bonus with a stated amount is a component with category \"bonus\". Two bonuses are two components.\n"
        "3. Weights are percent of the FINAL COURSE GRADE. If the text states an amount against a part of the "
        "course instead (\"1% added to your final lab grade\"), give that number as weight and put the EXACT "
        "existing component OR group (see current_groups -- a whole lab or theory component) it applies to in "
        "weight_of; Nexora converts it. If that part is not one component or group -- \"the lab grade\" when "
        "the scheme has In-Lab Assignments and Final Lab Exam -- make weight_of a list of every existing "
        "component that makes it up. Otherwise weight_of is \"course\".\n"
        "4. A stated change to an existing component's weight goes in components, keyed by its exact existing name.\n"
        "5. A stated minimum (\"must score at least 50% on the final\") goes in add_pass_rules.\n"
        "6. A condition that has no number, or that only says when something is available, goes in rules, "
        "one short sentence each, in the text's own words. Do not repeat what the scheme already holds.\n"
        "7. notes on a component is the condition for earning it, in the text's own words.\n"
        "8. summary is one plain sentence to the student saying what changed -- or that nothing in the text "
        "changes the grading scheme, if so. Return empty lists rather than guessing.\n"
        "9. The same marks must never count twice. If the scheme already holds a general entry for what the "
        "text spells out (\"Bonus Opportunities 2%\" when the text describes two 1% bonuses), put that general "
        "entry's exact name in remove_components and add the specific ones. If the text only repeats what the "
        "scheme already holds, return empty lists."
    )
    shape = {
        "add_components": [{"name": "string", "weight": 0, "weight_of": "course | an existing component or group name | [several component names]",
                            "category": "bonus | assignment | quiz | lab | exam | other", "notes": "string"}],
        "components": {"existing component name": 0},
        "add_pass_rules": [{"components": ["existing component name"], "minimum_percent": 0, "message": "string"}],
        "remove_components": ["existing component name the text replaces with more specific ones"],
        "rules": ["string"],
        "summary": "string",
    }
    user = json.dumps({"course": course, "current_components": components,
                       "current_groups": _scheme_groups(analysis, analysis.get("components") or []),
                       "current_rules": (analysis.get("rules") or [])[:30],
                       "pass_mark": analysis.get("pass_mark"),
                       "text_to_add": text[:12000], "return_shape": shape}, ensure_ascii=False)
    parsed = _gemini_json(system, user, 2500, 0.1, mode="fast")
    # flash-lite now and then wraps the object in a one-element list. Read
    # through that rather than treating it as "nothing changes", which is
    # the one answer here that would look like the feature not working.
    if isinstance(parsed, list):
        parsed = next((x for x in parsed if isinstance(x, dict)), None)
    if not isinstance(parsed, dict):
        raise ValueError("amendment reply was not an object")
    return parsed


def _amend_course_outline(user_id, course, text, label="Added by the student"):
    """Add `text` to a course's outline on file and apply what it changes.
    Returns {"changed": [...], "summary": str}. Raises ValueError with a
    sentence for the student when it cannot."""
    course = _normalize_course_code(course)
    text = _sanitize_extracted_text(text or "").strip()
    if len(text) < 20:
        raise ValueError("Paste the text you want added to the outline -- the announcement or the sentence -- and say what course it is for.")
    profile = CourseOutlineProfile.query.filter_by(user_id=user_id, course_full=course).first()
    if not profile:
        raise ValueError(f"{course} has no outline on file yet. Add the outline first (grade calculator → add the course outline), then add this to it.")
    try:
        analysis = json.loads(profile.grading_json or "{}")
    except Exception:
        analysis = {}
    if not isinstance(analysis, dict):
        analysis = {}
    try:
        edits = _outline_amendment_edits(course, analysis, text)
    except Exception as exc:
        app.logger.warning("Outline amendment read failed for %s: %s", course, exc)
        if _looks_like_quota_error(exc):
            raise ValueError("Nexora's AI has hit its usage limit for now, so it could not read this. Please try again later.")
        raise ValueError("Nexora could not read that just now. Please try again in a minute.")
    # The addendum itself is kept, in the student's words, on the policy and
    # on the outline text -- so Ask can quote it and a later re-analysis of
    # the outline still sees it.
    stamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    addenda = analysis.get("addenda") if isinstance(analysis.get("addenda"), list) else []
    addenda.append({"at": stamp, "label": label, "text": text[:6000]})
    analysis["addenda"] = addenda[-20:]
    row = None
    if profile.source_uid:
        row = CourseContent.query.filter_by(user_id=user_id, course_full=course, source_uid=profile.source_uid).first()
    if not row:
        row = (CourseContent.query.filter_by(user_id=user_id, course_full=course, content_type="outline")
               .order_by(CourseContent.updated_at.desc()).first())
    if row is not None:
        marker = f"\n\n===== {label.upper()} ({stamp[:10]}) =====\n"
        if text not in (row.content_text or ""):
            row.content_text = (row.content_text or "") + marker + text
            row.updated_at = datetime.utcnow()
    data = {
        "add_components": edits.get("add_components") if isinstance(edits.get("add_components"), list) else [],
        "components": edits.get("components") if isinstance(edits.get("components"), dict) else {},
        "add_pass_rules": edits.get("add_pass_rules") if isinstance(edits.get("add_pass_rules"), list) else [],
        "remove_components": edits.get("remove_components") if isinstance(edits.get("remove_components"), list) else [],
        "rules": edits.get("rules") if isinstance(edits.get("rules"), list) else [],
    }
    # A row the amendment replaces keeps its grade row if a mark was entered
    # on it (the correction code never destroys a mark). The calculator reads
    # rows, so the student is told rather than left with two entries.
    kept = []
    for name in data["remove_components"]:
        row = next((r for r in GradeEntry.query.filter_by(user_id=user_id, course_full=course).all()
                    if _grade_key(r.name) == _grade_key(name)), None)
        if row is not None and row.score is not None:
            kept.append(f"{row.name} still carries the mark you entered ({row.score:g}/{row.max_score:g}); "
                        f"remove it in the course workspace if the new entries now cover it")
    changed = _apply_outline_edits(user_id, course, profile, analysis, data)
    if changed:
        changed.extend(kept)
    if not changed:
        # Nothing structural, but the words are on file: save that much.
        profile.grading_json = json.dumps(analysis, ensure_ascii=False)
        profile.updated_at = datetime.utcnow()
        db.session.commit()
    summary = str(edits.get("summary") or "").strip()[:400]
    return {"changed": changed, "summary": summary}


@app.route("/api/course/<course_code>/outline/amend", methods=["POST"])
@ai_limit
@login_required
@requires_active
def amend_course_outline(course_code):
    """Add a piece of text -- an announcement, an email -- to the outline on
    file and apply whatever it changes about the grading scheme."""
    course = _normalize_course_code(course_code)
    body = request.get_json(silent=True) or {}
    try:
        result = _amend_course_outline(current_user.id, course, str(body.get("text") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    profile = CourseOutlineProfile.query.filter_by(user_id=current_user.id, course_full=course).first()
    try:
        analysis = json.loads(profile.grading_json or "{}") if profile else {}
    except Exception:
        analysis = {}
    return jsonify({"ok": True, "changed": result["changed"], "summary": result["summary"], "analysis": analysis})


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


_STUDY_TOOL_BRIEFS = {
    "summary": (
        "Write the summary a strong teaching assistant would hand a student the night before a test. "
        "Follow the material's own order. For each key concept: name it the way the lecture does, explain it in "
        "plain words as if to a student meeting it for the first time, say why it matters or where it is used, and "
        "give the material's own example when there is one. Define every technical term the first time it appears. "
        "Keep the slide or page reference the material gives. The overview is three or four sentences that say what "
        "the lecture was about and how its parts connect. Takeaways are the six to ten things most likely to be "
        "examined, each one sentence, each something a student could be asked."
    ),
    "flashcards": (
        "Write flashcards a student would actually drill with. One idea per card. The front is a question or a term, "
        "never a heading; the back is the answer in one to three sentences, in plain words, with the material's own "
        "example where it helps. Cover every concept in the material, not only the first half. Include cards for "
        "definitions, for 'why' and 'when' questions, and for the distinctions the lecture draws between similar "
        "things -- those are what tests ask. Keep the slide or page reference on each card."
    ),
    "quiz": (
        "Write a quiz that tests understanding, not recall of phrasing. Four options each, exactly one correct, and "
        "the wrong options must be plausible -- the mistakes a student would actually make. Mix the kinds of question: "
        "define, apply to a small scenario, pick the exception, predict what happens. Cover the whole material. The "
        "explanation says why the right answer is right AND why the tempting wrong one is wrong, in plain words. "
        "Keep the slide or page reference."
    ),
    "study_guide": (
        "Write a study guide a student can revise from without the slides. Sections follow the material's order; each "
        "one explains its topic in plain words, with definitions, the material's own examples, and how it connects to "
        "the sections around it. Key takeaways are the ideas to know cold. Practice prompts are questions a student "
        "should be able to answer from memory before an exam -- write ones that need understanding, not lookup."
    ),
}


def _study_tool_prompt(artifact_type, schema, course, title):
    """The system prompt for one study tool.

    It was one sentence -- "create a high-quality summary" -- and the output
    read like one: a list of headings with a clause each. A good summary,
    flashcard set or quiz is a specific thing, and the model has to be told
    what it is. The rules that hold for all four: every item grounded in the
    material, nothing invented, plain words, the student's level, and the
    material's own slide or page references kept so a card can be checked
    against the source.
    """
    brief = _STUDY_TOOL_BRIEFS.get(artifact_type, _STUDY_TOOL_BRIEFS["summary"])
    return (
        f"You are a strong university teaching assistant for {course}. The material is titled \"{title}\". "
        f"{brief} "
        "Write for a student who attended the lecture but is not yet confident with it: plain words, no filler, "
        "no praise of the material, and never a fact that is not in it. If the material is a slide deck, use its "
        "slide numbers; if a document, its section or page. "
        f"Return strict JSON matching exactly this shape and nothing else: {schema}"
    )


@app.route("/api/course/<course_code>/study-tools/generate", methods=["POST"])
@ai_limit
@login_required
@requires_active
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
        # Canvas can be read here and now. Only Brightspace needs the browser.
        if not _canvas_fill_content(current_user, row):
            return jsonify({"needs_content":True,"content_request":_outline_request(row),"answer":f"Reading {row.title} from Brightspace…"}),409
    schemas={
      'flashcards':'{"title":"","cards":[{"front":"","back":"","source":"Slide 1"}]}',
      'quiz':'{"title":"","questions":[{"question":"","options":["A","B","C","D"],"answer":0,"explanation":"","source":"Slide 1"}]}',
      'study_guide':'{"title":"","sections":[{"heading":"","content":"","source":"Slide 1"}],"key_takeaways":[],"practice_prompts":[]}',
      'summary':'{"title":"","overview":"","key_concepts":[{"concept":"","explanation":"","source":"Slide 1"}],"takeaways":[]}'
    }
    instruction=_study_tool_prompt(artifact_type, schemas[artifact_type], course, row.title)
    used_fallback = False
    try:
        # "fast": flash-lite first. A summary does not need a thinking model,
        # and the default chain was measured at 46.8s for one lecture.
        parsed=_gemini_json(instruction, _sanitize_extracted_text(row.content_text)[:120000], 5000, 0.35, mode="fast")
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
    access = _entitlement(user)
    if not access["active"]:
        # This is the read-only state, seen from the extension's side: what is
        # already imported stays, and nothing new arrives.
        return jsonify({"error": access["message"], "subscription_required": True}), 402

    payload = request.get_json(silent=True) or {}
    courses = payload.get("courses") or []
    if not isinstance(courses, list):
        return jsonify({"error": "courses must be a list."}), 400

    result = _import_courses(user, courses)
    # A discovery run reports everything the student asked Nexora to hold, so
    # it is authoritative -- but only for the LMS it came from, and only when
    # the extension could read the enrolments API. See _prune_courses.
    if payload.get("replace") and payload.get("complete") and courses:
        result.update(_prune_courses(user, courses, payload.get("origin")))
    return jsonify({"ok": True, **result})


def _prune_courses(user, courses, origin):
    """Make an import authoritative for the LMS it came from.

    Importing only added and updated, so choosing "only Fall 2026" left every
    course a previous sync had stored -- a student who asked for one term kept
    four years of courses and could not tell the choice had been honoured at
    all. Worse, the content sync then walked all of them, because it reads the
    course list back from the server rather than from what was just discovered.

    Scoped by origin so a Brightspace sync never deletes a Canvas course, and
    gated on `complete` by the caller so the DOM fallback -- which can only see
    one term -- can never delete the rest.

    GradeEntry and CourseOutlineProfile are deliberately left alone. Those are
    the student's own marks and the policy they confirmed; re-importing the
    term reattaches them. Only LMS-derived rows go.
    """
    origin = str(origin or "").rstrip("/")
    if not origin:
        return {"removed": 0}
    keep = {str(c.get("source_uid") or "") for c in courses if isinstance(c, dict)}
    doomed = []
    for row in SyncedCourse.query.filter_by(user_id=user.id).all():
        url = str(row.brightspace_url or "")
        if url != origin and not url.startswith(origin + "/"):
            continue
        if str(row.source_uid or "") in keep:
            continue
        doomed.append(row)
    if not doomed:
        return {"removed": 0}
    codes = {row.course_code for row in doomed}
    for row in doomed:
        db.session.delete(row)
    db.session.flush()
    # A code can be shared by two org units (a merged section); only clear the
    # material once nothing still holds that code.
    for code in codes:
        if SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first():
            continue
        CourseContent.query.filter_by(user_id=user.id, course_full=code).delete(synchronize_session=False)
        AcademicUpdate.query.filter_by(user_id=user.id, course_full=code).delete(synchronize_session=False)
    db.session.commit()
    return {"removed": len(doomed)}


def _import_courses(user, courses):
    """Upsert a list of courses for one student.

    Also where a referral is paid out: the friend has now imported their
    courses, which is the first thing that proves the account is a person
    rather than an address someone typed to farm free months.

    Called by the extension's sync route and by the Canvas sync, so a course
    imported from either LMS is stored identically.
    """
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    imported = []
    if courses:
        _credit_referrer(user)
    for item in courses[:COURSE_IMPORT_MAX]:
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
        # An org unit is the identity; a course CODE is not. Matching on the
        # code as a fallback meant every offering of the same code collapsed
        # into ONE row -- a student with 37 enrolments kept 11, and whichever
        # offering happened to be imported last won the name and the term. It
        # was silent, and it cost the content sync too, because that walks the
        # course list read back from the server: a course with no row is never
        # scraped, so its outline, materials and announcements never arrive.
        #
        # `_prune_courses` already assumed this ("a code can be shared by two
        # org units"), and every consumer groups by code anyway, so two
        # offerings still show as one card on the dashboard.
        existing = SyncedCourse.query.filter_by(user_id=user.id, source_uid=source_uid).first()
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

    # What the extension reported as "62 imported" was the number it discovered,
    # while the dashboard showed the rows that exist -- fewer whenever two org
    # units normalise to one code, or a course carries a removal tombstone. Both
    # were true and they answered different questions, so the count the student
    # will see on their dashboard is returned here and reported instead.
    hidden = {h.course_code for h in HiddenCourse.query.filter_by(user_id=user.id).all()}
    stored = sum(1 for c in SyncedCourse.query.filter_by(user_id=user.id).all()
                 if not bool(getattr(c, "is_hidden", False)) and c.course_code not in hidden)

    # `found` is what the LMS offered, `stored` is what Nexora now holds. They
    # are allowed to differ -- a hidden course, a tombstone -- but a student
    # pressing sync must be able to SEE that they differ. Reporting only one of
    # them is how "37 courses" sat in the extension for weeks beside a
    # dashboard showing 11.
    result = {**counts, "courses": imported, "found": len(courses or []),
              "stored": stored}
    # Silence is what made the last one expensive. If a payload was too large to
    # take whole, the answer says so instead of letting the student wonder why a
    # course they can see in Brightspace is not in Nexora.
    if len(courses or []) > COURSE_IMPORT_MAX:
        result["truncated"] = len(courses) - COURSE_IMPORT_MAX
    return result


@app.route("/api/sync/status")
def extension_sync_status():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401
    access = _entitlement(user)
    if not access["active"]:
        # This is the read-only state, seen from the extension's side: what is
        # already imported stays, and nothing new arrives.
        return jsonify({"error": access["message"], "subscription_required": True}), 402
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
    access = _entitlement(user)
    if not access["active"]:
        # This is the read-only state, seen from the extension's side: what is
        # already imported stays, and nothing new arrives.
        return jsonify({"error": access["message"], "subscription_required": True}), 402

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("items") or [], list):
        return jsonify({"error": "items must be a list."}), 400

    try:
        return jsonify({"ok": True, **_import_content(user, payload)})
    except Exception as exc:                                   # noqa: BLE001
        # S6: the failure has to be recorded OUTSIDE the transaction that
        # failed. `_import_content` commits once at the end, so an exception on
        # item 900 of 1,200 rolled back all 900 -- including the rows that
        # would have said the sync failed. The course simply stopped updating,
        # with no explanation anywhere. Same split `last_sync_attempt` already
        # uses for the calendar feed, and for the same reason.
        db.session.rollback()
        app.logger.exception("Content sync failed for user %s", user.id)
        _record_sync_failure(user, payload, exc)
        return jsonify({"error": "Nexora could not store this sync. It has been "
                                 "recorded; try again from the extension."}), 500


def _adopt_reported_timezone(user, name):
    """Take a timezone a synced client reported, if the account has none.

    The extension and the Canvas API both know the student's zone, and both
    reach the server before the student has necessarily opened a Nexora page.
    Waiting for a page view meant the very first import -- the one that creates
    every deadline -- was dated in UTC. This is not guesswork: the browser and
    Canvas are each stating their own setting.

    An account that already has a zone is left alone. A student who has told
    Nexora where they are outranks a client reporting where it is.
    """
    valid = _valid_timezone(name)
    if not valid or (user.timezone or "").strip():
        return ""
    user.timezone = valid
    db.session.commit()
    return valid


def _import_deadlines(user, items, default_course=""):
    """Turn the due dates the extension scraped into calendar deadlines.

    The extension reads D2L's own `DueDate` for every assignment and quiz and
    sends it as `due_at`. Nothing consumed it: `CourseContent` has no column
    for a date, so it was dropped, and the only source of deadlines was the
    Brightspace *calendar feed* URL -- a separate step on the connect page that
    is easy to skip. A student who did exactly what the page told them to do
    ("install the extension, press Import my courses") got a calendar with no
    assignments or quizzes in it at all, and no indication anything was
    missing. That is the worst possible shape for this bug: an empty calendar
    looks like a calm week.

    Deliberately mirrors the feed importer rather than inventing a second
    scheme: same `group_id`, so an assignment that arrives from both the feed
    and the extension collapses into one card, and the same
    `sync_state == 'dismissed'` check, so deleting something the LMS gave you
    is not undone on the next sync.
    """
    zone = _user_zone(user)
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    now_text = datetime.utcnow().isoformat()
    for item in items[:CONTENT_IMPORT_MAX]:
        kind_name = str(item.get("content_type") or "").strip().lower()
        if kind_name not in {"assignment", "quiz"}:
            continue
        title = str(item.get("title") or "").strip()
        due_iso = local_date_iso(item.get("due_at") or item.get("due_date"), zone)
        if not title or not due_iso:
            continue
        # The envelope's course is the fallback. `/api/sync/content` takes
        # `{"course": X, "items": [...]}` and the extension puts the code on
        # every item as well, but a caller that only fills the envelope used to
        # get every deadline filed under OTHER -- which silently costs the
        # outline's weights, the course standing, and the plan's ability to say
        # which course a block is for.
        course_full = _normalize_course_code(
            item.get("course") or item.get("course_full") or default_course or "OTHER")
        base = base_title(title)
        gid = make_group_id(course_full, base)
        # Stable across re-syncs and distinct from the feed's uids, so the two
        # sources update their own row instead of fighting over one.
        # The same definition the content path uses, minus the URL step --
        # see _source_uid: adding it here would re-key existing rows, which is
        # S4's job and needs a backup.
        source_uid = f"ext:{_source_uid(item, course_full, title, use_url=False)}"[:255]
        source_hash = hashlib.sha256(f"{title}|{due_iso}|{course_full}".encode("utf-8")).hexdigest()

        task = Task.query.filter_by(user_id=user.id, source_uid=source_uid).first()
        if task and task.sync_state == "dismissed":
            counts["unchanged"] += 1
            continue
        if task:
            changed = task.source_hash != source_hash
            task.group_id = gid; task.title = title; task.base_title = base
            task.course_full = course_full; task.course_prefix = course_prefix(course_full)
            task.due_raw = due_iso; task.kind = "due"; task.is_deadline = True
            task.source_due_at = str(item.get("due_at") or item.get("due_date") or "")[:40]
            task.due_time = local_time_label(task.source_due_at, zone)
            task.source_hash = source_hash; task.sync_state = "active"
            # Assigned outside the `changed` check on purpose: the link is not
            # in source_hash, so an unchanged deadline still picks one up the
            # first time a student syncs after this shipped.
            task.source_link = _lms_link(item.get("source_url"))
            task.last_seen_sync = now_text
            counts["updated" if changed else "unchanged"] += 1
        else:
            db.session.add(Task(
                id=uuid4().hex, user_id=user.id, group_id=gid, title=title,
                base_title=base, course_full=course_full,
                course_prefix=course_prefix(course_full), due_raw=due_iso,
                source_due_at=str(item.get("due_at") or item.get("due_date") or "")[:40],
                due_time=local_time_label(item.get("due_at") or item.get("due_date"), zone),
                kind="due", is_deadline=True, source="extension",
                source_link=_lms_link(item.get("source_url")),
                created_at=now_text, source_uid=source_uid,
                source_hash=source_hash, sync_state="active",
                last_seen_sync=now_text,
            ))
            counts["added"] += 1
    return counts


# How much scraped body text is worth keeping, per kind.
#
# Every cap here is set ABOVE what any feature actually reads, so the store is
# never the binding constraint -- the tutor reads 45,000 characters of a
# matched item, outline analysis excerpts at 90,000, study tools at 120,000.
# The caps exist to bound the pathological row, not to ration the useful one.
#
# Setting them below what a feature reads would be worse than it sounds: the
# only thing that makes Nexora fetch a body again from Brightspace is it being
# SHORTER THAN ~120 CHARACTERS (see the needs_content branches). A body cut to
# a few thousand is far above that, so it would never re-fetch -- it would be
# silently used truncated, which is this app's worst failure mode wearing a
# different hat. Truncation is therefore also marked, below, so whatever reads
# it can see that it is not the whole thing.
_CONTENT_TEXT_CAP = {
    "outline": 120000,     # analysed, quoted by Ask, re-read on every re-analysis
    "lecture": 120000,     # summarised, explained, turned into flashcards
    "page": 48000,         # real course material; the tutor reads 45,000 of it
    "practice": 48000,
    "resource": 48000,
    "assignment": 12000,   # a submission blurb; the deadline is the point
    "quiz": 12000,
    "announcement": 12000, # the dashboard shows the title and the date
    "": 12000,
}
_TRUNCATED_NOTE = "\n\n[Nexora stored only the first part of this item.]"


def _cap_content_text(text, kind):
    """Trim a scraped body to its kind's cap, and say so when it trims.

    The note matters: a silently shortened lecture reads as a complete one to
    every summariser and to the student, and nothing downstream can tell the
    difference.
    """
    text = str(text or "")
    cap = _CONTENT_TEXT_CAP.get(kind, _CONTENT_TEXT_CAP[""])
    if len(text) <= cap:
        return text
    return text[:cap - len(_TRUNCATED_NOTE)] + _TRUNCATED_NOTE


def _source_uid(item, course, title, use_url=True):
    """The stable id for one scraped item. ONE definition, both importers.

    The pre-pass that loads what has already been announced has to derive
    exactly the id the loop will then look up, so two copies of this rule would
    silently stop matching and every item would be announced again -- the bug
    it exists to prevent. The deadline importer had its own copy anyway, with a
    shorter fallback chain, which is the drift this codebase keeps paying for:
    two sibling paths reading the same payload differently.

    `use_url` is not a style choice and is deliberately not removed yet. The
    content path has always fallen back to the URL and the deadline path never
    has, so unifying the CHAIN would re-key every existing row that used the
    short one -- the item would arrive as new, beside itself. That is finding
    S4 in SYNC_AUDIT_V1.md, it rewrites stored identities, and it needs a
    backfill and a database backup. This unifies the definition now and leaves
    the identity change to that pass, where it can be migrated honestly.
    """
    parts = [item.get("source_uid")]
    if use_url:
        parts.append(str(item.get("source_url") or "")[:4000])
    parts.append(f"{course}|{title}")
    return str(next((p for p in parts if p), ""))[:255]


def _content_source_uid(item, course, title):
    return _source_uid(item, course, title, use_url=True)


def _record_sync_failure(user, payload, exc):
    """Mark every course in a failed payload, in a transaction of its own.

    The import commits once at the end, so anything it wrote about its own
    failure is rolled back with the failure. This runs afterwards, on a clean
    session, and must never raise -- a logger that takes down the error path is
    worse than the error.
    """
    try:
        scan_course = _normalize_course_code(str(payload.get("course") or "").strip() or "OTHER")
        codes = {scan_course} if scan_course and scan_course != "OTHER" else set()
        for item in (payload.get("items") or [])[:CONTENT_IMPORT_MAX]:
            if isinstance(item, dict):
                codes.add(_normalize_course_code(
                    item.get("course") or item.get("course_full") or scan_course or "OTHER"))
        stamped = datetime.utcnow()
        for code in codes:
            row = SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first()
            if not row:
                continue
            row.last_sync_at = stamped
            row.last_sync_ok = False
            row.last_sync_state = "error"
            row.last_sync_error = ("Nexora could not store the last sync for this "
                                   "course: %s" % exc)[:2000]
        db.session.commit()
    except Exception:                                          # noqa: BLE001
        db.session.rollback()
        app.logger.exception("Could not record the sync failure")


def _import_content(user, payload):
    """Upsert course content for one student, from either LMS.

    `payload` keeps the extension's shape -- items, replace_courses, scan_meta --
    so the Canvas sync builds the same dict rather than a second format.
    """
    received = len(payload.get("items") or [])
    # S3: the cap was silent. `items[:1200]` appeared three times and nothing
    # counted what fell off the end, which is the rule this codebase already
    # states -- a limit a student cannot see is indistinguishable from a bug --
    # and which `COURSE_IMPORT_MAX` already honours by returning `truncated`.
    items = (payload.get("items") or [])[:CONTENT_IMPORT_MAX]
    dropped = received - len(items)
    hidden_codes = {h.course_code for h in HiddenCourse.query.filter_by(user_id=user.id).all()}
    replace_courses = [_normalize_course_code(c) for c in (payload.get("replace_courses") or [])]
    replace_courses = [c for c in replace_courses if c not in hidden_codes]
    scan_meta = payload.get("scan_meta") if isinstance(payload.get("scan_meta"), dict) else {}
    scan_course = str(payload.get("course") or (replace_courses[0] if replace_courses else "") or "").strip()
    if scan_course:
        scan_course = _normalize_course_code(scan_course)

    # How many items this payload actually carries for each course, which is
    # the number the replace floor is judged against.
    incoming_by_course = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _normalize_course_code(item.get("course") or item.get("course_full")
                                      or scan_course or "OTHER")
        incoming_by_course[code] = incoming_by_course.get(code, 0) + 1

    # S1: a partial scrape must not replace a whole course.
    #
    # The extension only asks for a replace when its scan says ok, complete,
    # authenticated and non-empty, which is the right shape and stops the empty
    # case. But `complete` is the extension's OWN opinion: if D2L returns 3 of
    # 40 topics and still answers 200, the scan looks complete, 37 items are
    # deleted and never come back, and the course page says the sync was fine.
    #
    # The server has a second opinion and was not using it -- it knows how many
    # rows that course had a moment ago. A payload carrying less than
    # REPLACE_FLOOR of what is on file is refused the delete. The items it did
    # bring are still upserted by the loop below, so this can only ever keep
    # rows, never lose them; what it costs is a stale row surviving, which is
    # recoverable, against a silent deletion, which is not.
    partial_courses = {}
    for code in list(replace_courses):
        have = CourseContent.query.filter_by(user_id=user.id, course_full=code).count()
        arriving = incoming_by_course.get(code, 0)
        if have and arriving < have * REPLACE_FLOOR:
            replace_courses.remove(code)
            partial_courses[code] = (have, arriving)
    if replace_courses:
        CourseContent.query.filter(CourseContent.user_id == user.id, CourseContent.course_full.in_(replace_courses)).delete(synchronize_session=False)
        # Smart Sync refreshes metadata only; preserve any user-requested cached files.
        db.session.flush()
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    # Before any date is derived: the extension reports the browser's zone, and
    # this is usually the request that creates the student's first deadlines.
    _adopt_reported_timezone(user, payload.get("timezone"))
    # Deadlines are handled in their own pass, on purpose: the content loop
    # short-circuits an item whose hash has not changed, so a deadline attached
    # to already-imported content would never be created retroactively.
    counts["deadlines"] = _import_deadlines(user, items, scan_course)

    # What this student has already been told about, for THESE items only.
    #
    # `replace_courses` above DELETES a course's CourseContent rows before the
    # loop re-imports them, so the `existing` lookup below always misses, the
    # content_hash comparison that exists to detect "unchanged" is unreachable,
    # and every item takes the "new" branch and announces itself again. On
    # every sync. That made academic_update 2,581,147 rows across 33,795 real
    # items -- one Brightspace page had 638 byte-identical rows -- which was
    # 786 MB, 96% of the database, and why Render suspended it. The content row
    # is gone by this point; the update row is what remembers.
    #
    # Ask only about the uids in this payload. The first version of this loaded
    # every update row the student had, which on the account that caused the
    # outage is 2.9 MILLION tuples -- about half a gigabyte of Python objects,
    # on a worker that has less than that. A fix for a table that grew too big
    # must not itself need the table to be small.
    wanted = set()
    for item in items[:CONTENT_IMPORT_MAX]:
        item_title = str(item.get("title") or "").strip()
        if not item_title:
            continue
        wanted.add(_content_source_uid(item, _normalize_course_code(
            item.get("course") or item.get("course_full") or scan_course or "OTHER"), item_title))
    announced = set()
    uid_list = list(wanted)
    for start in range(0, len(uid_list), 300):       # bounded IN(), not one huge parameter list
        announced.update(
            (row[0], row[1]) for row in
            db.session.query(AcademicUpdate.related_uid, AcademicUpdate.update_type)
            .filter(AcademicUpdate.user_id == user.id,
                    AcademicUpdate.related_uid.in_(uid_list[start:start + 300])).all()
        )

    def announce(course_code, update_type, title, detail, uid):
        """Tell the student about an item once, not once per sync."""
        if (uid, update_type) in announced:
            return
        announced.add((uid, update_type))
        db.session.add(AcademicUpdate(
            id=uuid4().hex, user_id=user.id, course_full=course_code,
            update_type=update_type, title=title, detail=detail, related_uid=uid,
        ))

    for item in items[:CONTENT_IMPORT_MAX]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        course = _normalize_course_code(
            item.get("course") or item.get("course_full") or scan_course or "OTHER")
        if course in hidden_codes:
            counts["unchanged"] += 1
            continue
        source_url = str(item.get("source_url") or "")[:4000]
        source_uid = _content_source_uid(item, course, title)
        kind = str(item.get("content_type") or "resource")[:30]
        item_data = {
            "title": title,
            "content_type": kind,
            "source_url": source_url,
            # Bounded per kind, and marked when it bounds. See _CONTENT_TEXT_CAP.
            "content_text": _cap_content_text(item.get("content_text"), kind),
            "module_name": str(item.get("module_name") or "")[:1000],
            "published_at": str(item.get("published_at") or "")[:80],
            # Sent by every extension version in the wild, including the one on
            # the Web Store, and dropped here until now.
            "file_ext": str(item.get("file_ext") or "").strip().lower()[:12],
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
            existing.file_ext = item_data["file_ext"]
            existing.content_hash = fingerprint
            existing.updated_at = datetime.utcnow()
            counts["updated"] += 1
            announce(course, "content_updated", f"{title} updated",
                     f"{item_data['content_type'].title()} content changed in Brightspace.",
                     source_uid)
        else:
            db.session.add(CourseContent(
                id=uuid4().hex, user_id=user.id, course_full=course,
                title=title, content_type=item_data["content_type"],
                source_url=source_url, source_uid=source_uid,
                content_text=item_data["content_text"], module_name=item_data["module_name"],
                published_at=item_data["published_at"], file_ext=item_data["file_ext"],
                content_hash=fingerprint,
            ))
            counts["added"] += 1
            announce(course, "new_content", f"New {item_data['content_type']}: {title}",
                     item_data["module_name"] or "Imported from Brightspace.", source_uid)
            if item_data["content_type"] == "lecture":
                announce(course, "ai_recommendation", f"Review recommended: {title}",
                         "Nexora detected a new lecture. Open Study Tools to create flashcards, a quiz, or a study guide, and schedule a 30–45 minute review.",
                         source_uid)
            elif item_data["content_type"] == "outline":
                announce(course, "outline_detected", "Course outline ready for analysis",
                         "Analyze it once to build the official grading scheme and course-specific outcome calculator.",
                         source_uid)
    # AcademicUpdate gains a row for every item added or changed, on every
    # sync, and nothing reads one older than a few days -- the dashboard's
    # "Recent updates" reads the content rows instead (_recent_updates).
    # Left alone it is a table that only ever grows.
    cutoff = datetime.utcnow() - timedelta(days=ACADEMIC_UPDATE_KEEP_DAYS)
    AcademicUpdate.query.filter(AcademicUpdate.user_id == user.id,
                                AcademicUpdate.created_at < cutoff).delete(synchronize_session=False)

    # Keep course cards aware of how much content was imported.
    touched_courses = { _normalize_course_code((item or {}).get("course") or (item or {}).get("course_full") or "OTHER") for item in items if isinstance(item, dict) }
    for code in touched_courses:
        course_row = SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first()
        if course_row:
            course_row.content_count = CourseContent.query.filter_by(user_id=user.id, course_full=code).count()
            course_row.updated_at = datetime.utcnow()
    # S2: stamp EVERY course this payload touched, not just the one named on
    # the envelope. `last_sync_at` was written inside `if scan_course:`, so a
    # payload carrying several courses updated `content_count` for all of them
    # and the timestamp for one -- and a payload whose envelope named no course
    # stamped nothing at all. The column existed, was populated, and was
    # rendered in no template.
    errors = scan_meta.get("diagnostics", {}).get("errors") if isinstance(scan_meta.get("diagnostics"), dict) else {}
    stamped = datetime.utcnow()
    for code in sorted(touched_courses | ({scan_course} if scan_course else set())):
        course_row = SyncedCourse.query.filter_by(user_id=user.id, course_code=code).first()
        if not course_row:
            continue
        arriving = incoming_by_course.get(code, 0)
        course_row.last_sync_at = stamped
        course_row.last_sync_items = arriving
        if code in partial_courses:
            have, got = partial_courses[code]
            course_row.last_sync_state = "partial"
            course_row.last_sync_ok = False
            course_row.last_sync_error = (
                "This sync carried %d item%s for a course that already has %d, so "
                "Nexora kept what it had rather than replacing it. Run the sync "
                "again from the extension; if it keeps happening the course may "
                "need reconnecting." % (got, "" if got == 1 else "s", have))[:2000]
            continue
        # A totally failed scan (every Brightspace call 404ing for one course
        # while the others work) has to be visible on the course page itself.
        scan_ok = bool(arriving) or (scan_meta.get("complete") is True)
        course_row.last_sync_ok = scan_ok
        course_row.last_sync_state = "ok" if scan_ok else "error"
        if not scan_ok and isinstance(errors, dict) and errors:
            parts = [f"{section}: {msg}" for section, msg in list(errors.items())[:4]]
            course_row.last_sync_error = ("Brightspace didn't return any content for this course. " + "; ".join(parts))[:2000]
        elif not scan_ok:
            course_row.last_sync_error = "Brightspace didn't return any content for this course on the last sync attempt."[:2000]
        else:
            course_row.last_sync_error = ""
    db.session.commit()
    result = {**counts, "received": received, "stored": len(items),
              "replaced_courses": replace_courses}
    if dropped:
        # Loud, the way COURSE_IMPORT_MAX already is.
        result["truncated"] = True
        result["dropped"] = dropped
    if partial_courses:
        result["partial_courses"] = {c: {"had": h, "received": g}
                                     for c, (h, g) in partial_courses.items()}
    return result


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


# A day cell is read top to bottom, so it has to run earliest first. The month
# grid appended in `sort_cards` order, which is the right order for the task
# QUEUE -- by urgency -- and inside a single day it put a 19:00 test above a
# 14:00 midterm. Reported from the calendar with both times on screen.
_NO_TIME_OF_DAY = 24 * 60 + 1          # sorts after every real time


def _time_of_day_key(item):
    """Minutes from midnight, or past the end of the day when none is known.

    An entry with no time is not an all-day event -- it is one whose time Nexora
    was never told -- so it cannot claim a place in the chronology and follows
    the entries that can. Python's sort is stable, so among equal times, and
    among the untimed, `sort_cards`' own ordering survives untouched.
    """
    raw = str(item.get("time") or "").strip()
    match = _TIME_RE.match(raw)
    if not match:
        return _NO_TIME_OF_DAY
    return int(match.group(1)) * 60 + int(match.group(2))


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

    # One colour per course. `cards` is every dated item the student has, not
    # just this month's, so the mapping does not change as they page through
    # the months -- a course that is green in September must be green in
    # October or the legend promises something it does not keep.
    #
    # Deliberately NOT every course they have ever enrolled in: a fourth-year
    # student has thirty, and courses with no work at all would eat the most
    # distinct colours and push the five they are actually taking onto shades
    # of each other. That was visible immediately -- SYSC2310 came out tan
    # against BIOL1103's amber because two work-free courses held green and
    # blue.
    colours = _student_course_colours(current_user.id)

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
            color_index = colours.get(course, 0)
            major = _major_assessment_label(c.get("kind"), c.get("title", ""))

            task_ids = [i for i in (c.get("task_ids") or []) if i]
            tasks_by_date[iso].append({
                "title": c.get("title", "Task"),
                "course": course,
                "color_index": color_index,
                "status": status_for(c.get("main_due_raw", ""))[1],
                # Enough for the editor to open on this entry without a second
                # request. `id` is any row of the group; delete and edit resolve
                # the rest of the group from it server-side.
                "id": task_ids[0] if task_ids else "",
                "kind": c.get("kind") or "due",
                # "" for ordinary work; FINAL / MIDTERM / TEST / EXAM for the
                # three or four squares in a term that decide the grade.
                "major": major,
                "days_left": dleft,
                "time": c.get("due_time") or "",
                "location": c.get("location") or "",
                "notes": c.get("notes") or "",
                "imported": (c.get("source") or "manual") != "manual",
                "link": c.get("source_link") or "",
            })

    # Chronological within each day, once every entry for it is collected.
    for day_items in tasks_by_date.values():
        day_items.sort(key=_time_of_day_key)

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

    # The course list the "add an event" form offers, so a student picks rather
    # than types a code that has to match one Nexora already knows.
    known_courses = sorted({
        c.course_code for c in SyncedCourse.query.filter_by(user_id=current_user.id).all()
        if c.course_code and not bool(getattr(c, "is_hidden", False))
    } | {c.get("course_full") for c in cards if c.get("course_full") and c.get("course_full") != "OTHER"})

    # The existing feed address, if there is one; the page creates it on the
    # first press of Sync rather than on every visit.
    feed_token = _calendar_token(current_user)
    # Whether to offer the scan at all, decided WITHOUT a model: candidates are
    # plain SQL and a regex, so a calendar with nothing announced costs nothing.
    # Only when there is something new, and only once every few hours, does the
    # page spend a call on reading it.
    _scan_at = getattr(current_user, "announce_scan_at", None)
    _stale = _scan_at is None or (datetime.utcnow() - _scan_at) > timedelta(hours=ANNOUNCE_RESCAN_HOURS)
    _today = _today_for_request()
    _zone = _user_zone(current_user)
    # Outline dates are free -- already extracted, no model -- so they are worth
    # offering whenever one is pending, throttle or no throttle. Only reading
    # ANNOUNCEMENTS costs a call, and only that is rate limited.
    _outline_pending = any(
        _today <= f["date"] <= _today + timedelta(days=ANNOUNCEMENT_HORIZON_DAYS)
        and not _already_on_the_calendar(current_user.id, f["course"], f["date"], f["title"])
        for f in _outline_dated_assessments(current_user.id, _today, _zone))
    _ann_pending = bool(_announcement_candidates(current_user.id, _today, _zone))

    return render_template(
        "calendar.html",
        announce_scan_due=bool(_outline_pending or (_ann_pending and _stale)),
        weeks=weeks,
        calendar_feed_url=url_for("calendar_feed", token=feed_token, _external=True) if feed_token else "",
        month_title=current_month.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m"),
        this_month=date.today().strftime("%Y-%m"),
        is_this_month=(year, month) == (date.today().year, date.today().month),
        today_iso=date.today().isoformat(),
        known_courses=known_courses,
        # The legend. A colour nobody can decode is just decoration, and he
        # asked for this by name: "so everyone knows what each color is".
        # Only courses that actually appear this month, or the legend lists a
        # first-year course against an empty grid.
        course_legend=[{"course": code, "color_index": colours.get(code, 0)}
                       for code in sorted({t["course"] for day in tasks_by_date.values() for t in day
                                           if t.get("course") and t["course"] != "OTHER"})],
        # The palette the recolour control offers, so the page cannot drift from
        # the ramp the templates actually define.
        course_colour_count=COURSE_COLOUR_COUNT,
        user=current_user,
    )


# ============================
# Calendar subscription (.ics)
# ============================
# Google Calendar cannot sign in to Nexora, so "connect my calendar" is a URL
# it polls rather than an OAuth grant. That needs no Google Cloud project and
# no verification, and it works in Apple Calendar and Outlook unchanged. The
# trade is that Google decides how often to refresh -- usually a few hours --
# and the events are read-only on their side.

ICS_WINDOW_PAST = 60      # days of finished work worth keeping visible
ICS_WINDOW_AHEAD = 400    # a full academic year plus a margin


def _ics_escape(value):
    """RFC 5545 text escaping. A comma or semicolon left raw ends the property
    early and Google silently drops the whole event."""
    return (str(value or "")
            .replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n")
            .replace("\n", "\\n").replace("\r", "\\n"))


def _ics_fold(line):
    """Lines are limited to 75 octets; longer ones continue with a leading
    space. Folding on characters would split a multi-byte one in half."""
    raw = line.encode("utf-8")
    if len(raw) <= 73:
        return line
    chunks = []
    while raw:
        head, raw = raw[:73], raw[73:]
        # never cut inside a UTF-8 sequence
        while raw and (raw[0] & 0xC0) == 0x80:
            head, raw = head + raw[:1], raw[1:]
        chunks.append(head.decode("utf-8"))
    return "\r\n ".join(chunks)


def _calendar_token(user, create=False):
    token = (user.calendar_token or "").strip()
    if not token and create:
        token = secrets.token_urlsafe(24)
        user.calendar_token = token
        db.session.commit()
    return token


def _feed_events(user, only=""):
    """Every dated, unfinished-or-recent item, as (task, date) pairs.

    `only="exams"` narrows it to the sittings that decide a grade.

    Everything was the only option, and on a real term that is unusable: a
    single September had ten items on one day -- "Lab Plagiarism Quiz",
    "Submission Folder", "Lab 1 Quiz - Available", "Modules 1 and 2 Quiz" --
    which buries the two dates a student keeps a calendar FOR. Reported with a
    screenshot of a Google Calendar month with no white space left in it.

    A personal calendar is not the same surface as Nexora's own. Nexora is
    where you go to see everything; a calendar is what you glance at, and it
    only works while a glance is worth taking.
    """
    today = date.today()
    rows = Task.query.filter_by(user_id=user.id).filter(
        (Task.sync_state.is_(None)) | (Task.sync_state != "dismissed")).all()
    seen = {}
    for row in rows:
        if (row.kind or "") == "junk":
            continue
        if only == "exams" and not _major_assessment_label(row.kind, row.title or ""):
            continue
        due = parse_iso_date(row.due_raw or "")
        if not due:
            continue
        if not (today - timedelta(days=ICS_WINDOW_PAST) <= due <= today + timedelta(days=ICS_WINDOW_AHEAD)):
            continue
        # An assignment can be three rows -- opens, due, closes. The calendar
        # shows the group once, and so should the feed.
        key = (row.group_id or row.id, due.isoformat())
        if key not in seen or (row.kind or "") == "due":
            seen[key] = (row, due)
    return list(seen.values())


def _build_ics(user, events, only=""):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    # The subscribed calendar's own name, because a student may end up with
    # both and has to be able to tell which is which in Google's sidebar --
    # and, when they want it gone, which one to unsubscribe from.
    name = "Nexora exams" if only == "exams" else "Nexora"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Nexora//Academic calendar//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{name}",
        "X-WR-CALDESC:" + ("Tests, midterms and finals from Nexora"
                           if only == "exams" else "Deadlines and exams imported into Nexora"),
        # A hint only: Google honours its own refresh schedule regardless.
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for row, due in events:
        title = row.title or "Untitled"
        course = row.course_full or ""
        summary = f"{course} - {title}" if course and course != "OTHER" else title
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{row.id}@nexora")
        lines.append(f"DTSTAMP:{stamp}")
        time_raw = (row.due_time or "").strip()
        if _TIME_RE.match(time_raw):
            hour, minute = time_raw.split(":")
            start = f"{due.strftime('%Y%m%d')}T{hour}{minute}00"
            end_hour = (int(hour) + 1) % 24
            # A one-hour block is a guess, but a zero-length event is invisible
            # in Google's month view, which defeats the point of the feed.
            end_day = due + timedelta(days=1) if int(hour) == 23 else due
            lines.append(f"DTSTART:{start}")
            lines.append(f"DTEND:{end_day.strftime('%Y%m%d')}T{end_hour:02d}{minute}00")
        else:
            # All-day. DTEND is exclusive in iCalendar, so it is the next day.
            lines.append(f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(due + timedelta(days=1)).strftime('%Y%m%d')}")
        lines.append(f"SUMMARY:{_ics_escape(summary)}")
        if row.location:
            lines.append(f"LOCATION:{_ics_escape(row.location)}")
        detail = row.notes or ""
        if (row.kind or "") == "exam":
            detail = (detail + "\n" if detail else "") + "Exam"
        if detail:
            lines.append(f"DESCRIPTION:{_ics_escape(detail)}")
        if row.completed:
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


@app.route("/calendar/feed/<token>.ics")
@limiter.limit("60 per hour")
def calendar_feed(token):
    """Deliberately unauthenticated: the token is the credential, because the
    calendar app fetching this has no way to sign in. Nothing here is writable
    and nothing but this student's own dated items is returned."""
    token = (token or "").strip()
    if len(token) < 20:
        return Response("Not found", status=404, mimetype="text/plain")
    user = User.query.filter_by(calendar_token=token).first()
    if not user:
        return Response("Not found", status=404, mimetype="text/plain")
    # A query parameter rather than a second token: the token is the
    # credential, and minting one per scope would mean a student who changes
    # their mind has two live addresses to the same private data.
    only = "exams" if request.args.get("only", "").strip().lower() in ("exams", "exam", "major") else ""
    body = _build_ics(user, _feed_events(user, only), only)
    # content_type, not mimetype: Flask appends its own charset to a mimetype,
    # and "text/calendar; charset=utf-8; charset=utf-8" is not a header anyone
    # should have to parse.
    return Response(body, content_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": 'inline; filename="nexora.ics"',
        "Cache-Control": "private, max-age=900",
        # The URL is a secret; keeping it out of search engines is free.
        "X-Robots-Tag": "noindex, nofollow",
    })


@app.route("/api/calendar/feed", methods=["GET", "POST", "DELETE"])
@login_required
def calendar_feed_link():
    """GET returns the link (creating one on first ask), POST rotates it,
    DELETE turns the feed off entirely."""
    if request.method == "DELETE":
        current_user.calendar_token = ""
        db.session.commit()
        return jsonify({"ok": True, "url": ""})
    if request.method == "POST":
        current_user.calendar_token = secrets.token_urlsafe(24)
        db.session.commit()
    token = _calendar_token(current_user, create=True)
    return jsonify({"ok": True, "url": url_for("calendar_feed", token=token, _external=True)})


def _known_brightspace_home(user_id):
    """The origin this student's own courses came from, if any are imported.

    The page shipped with a hard link to brightspace.carleton.ca. The scraper is
    deliberately university-agnostic -- `location.origin` plus D2L's own API --
    so sending every student to one university's login page is both wrong and
    the kind of thing that makes an app look like it was built for one campus.
    With nothing imported yet there is nothing to know, and the page says so
    instead of guessing.
    """
    rows = (SyncedCourse.query.filter_by(user_id=user_id)
            .order_by(SyncedCourse.id.desc()).limit(40).all())
    for row in rows:
        url = str(row.brightspace_url or "").strip()
        m = re.match(r"(https://[^/]+)/", url + "/")
        if m:
            return m.group(1) + "/d2l/home"
    return ""


@app.route("/extension/nexora-sync.zip")
@login_required
def extension_download():
    """The packaged extension, for installing without the Chrome Web Store.

    Signed in only: it is not secret, but it is the thing that pushes a
    student's courses into an account, and an unauthenticated download link on
    a public page is a slightly different object -- one that gets indexed,
    hotlinked and served to people who never saw the instructions beside it.

    The version comes from the manifest inside the zip rather than a constant
    here, because a constant is a second place to forget.
    """
    if not EXTENSION_ZIP_AVAILABLE:
        return Response("The extension package is not available on this deploy.",
                        status=404, mimetype="text/plain")
    version = ""
    try:
        with zipfile.ZipFile(EXTENSION_ZIP) as bundle:
            version = str(json.loads(bundle.read("manifest.json")).get("version") or "")
    except Exception:
        version = ""
    name = f"nexora-sync-{version}.zip" if version else "nexora-sync.zip"
    return send_file(EXTENSION_ZIP, as_attachment=True, download_name=name,
                     mimetype="application/zip")


def _setup_state(user_id):
    """How far through setting up this account is.

    Everything a new student needs was already written on the connect pages --
    install, pin, import, calendar feed -- and nothing ever SENT them there. An
    account landed on an empty dashboard and had to find "Brightspace" in the
    rail on its own, which is the step that was being covered by Bassem telling
    people what to do in person. That does not scale past his friends.
    """
    courses = SyncedCourse.query.filter_by(user_id=user_id).count()
    deadlines = (Task.query.filter_by(user_id=user_id)
                 .filter(Task.source != "manual").count())
    return {"courses": courses, "deadlines": deadlines,
            "connected": bool(courses or deadlines)}


@app.route("/start")
@login_required
def start_here():
    """One question, then the page that answers it.

    A student who has connected something does not need this and is not shown
    it -- an onboarding screen that keeps appearing after you are set up reads
    as the app being broken.
    """
    state = _setup_state(current_user.id)
    if state["connected"]:
        return redirect(url_for("home"))
    return render_template("start.html", setup=state, extension_url=EXTENSION_URL)


@app.route("/connect-brightspace")
@login_required
def connect_brightspace():
    feed_url = load_feed_url()
    last_sync = load_last_sync()

    # The student's clock, not the server's. See local_stamp.
    last_sync_text = local_stamp(last_sync, "%b %d, %Y at %I:%M %p") or "Never"

    return render_template(
        "connect_brightspace.html",
        feed_url=feed_url,
        last_sync_text=last_sync_text,
        sync_status=current_user.sync_status or "never",
        sync_message=current_user.sync_message or "Not synchronized yet.",
        brightspace_home=_known_brightspace_home(current_user.id),
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


# The automatic feed sync, asked for by the dashboard once it has painted
# rather than run inside its render. POST, not GET, for the same reason
# `/api/tour` is: a prefetch or a crawler must not be able to spend it.
@app.post("/api/sync/auto")
@login_required
def sync_auto():
    """Run the due feed sync, if one really is due.

    `home()` decides whether to ASK for this, from a flag computed at render
    time; this decides whether to DO it. The two are deliberately separate,
    because the page may be a minute old by the time the fetch lands and a
    student with three tabs open fires three of these. `auto_sync_if_needed`
    re-checks `should_auto_sync` and stamps `last_sync_attempt` before the
    attempt, so the first request through here claims the slot and the rest
    return `ran: false` without touching the university.
    """
    result = auto_sync_if_needed()
    counts = result["counts"]
    changed = sum(int(counts.get(k) or 0) for k in ("added", "updated", "removed"))
    return jsonify({"ok": result["ok"], "ran": result["ran"],
                    "changed": changed, "counts": counts})


@app.route("/grade-calculator")
@login_required
def grade_calculator():
    """One page for every course's grades, rather than one page per course.

    Opening a course workspace to read a grading scheme meant a navigation per
    course, which for a full term is most of what a student does here. The
    picker puts the same thing behind a dropdown; the workspace is untouched
    and still works.
    """
    user_id = current_user.id
    profiles = {p.course_full: p for p in
                CourseOutlineProfile.query.filter_by(user_id=user_id).all()}
    synced = _newest_offering_per_code(
        c for c in SyncedCourse.query.filter_by(user_id=user_id).all()
        if c.course_code and not bool(getattr(c, "is_hidden", False)))
    graded = {row.course_full for row in
              GradeEntry.query.filter_by(user_id=user_id).with_entities(
                  GradeEntry.course_full).distinct()}

    courses = []
    for code in sorted(set(synced) | set(profiles) | graded):
        if not code or code == "OTHER":
            continue
        profile = profiles.get(code)
        confirmed = None
        if profile:
            try:
                confirmed = bool(json.loads(profile.grading_json or "{}").get("confirmed"))
            except Exception:
                confirmed = False
        courses.append({
            "code": code,
            "name": (synced.get(code).course_name if synced.get(code) else "") or code,
            "term": (synced.get(code).term if synced.get(code) else "") or "",
            "analyzed": profile is not None,
            "confirmed": confirmed,
        })

    # Grouped for the picker, newest term first, and the current term first of
    # all. A flat list of every course a student has ever taken is unusable by
    # second year -- the template renders these as <optgroup>s.
    for course in courses:
        course["term_label"] = _term_label(course["term"])
    current_label = _current_term_label()
    course_terms = sorted({c["term_label"] for c in courses},
                          key=_term_sort_key, reverse=True)
    if current_label in course_terms:
        course_terms.remove(current_label)
        course_terms.insert(0, current_label)
    course_groups = [{"label": label,
                      "courses": [c for c in courses if c["term_label"] == label]}
                     for label in course_terms]

    requested = _normalize_course_code(request.args.get("course", "") or "")
    if requested and not any(c["code"] == requested for c in courses):
        requested = ""

    # The calculator converts a running total into a letter, so it needs the
    # student's own scale rather than a hardcoded one.
    return render_template("grade_calculator.html",
                           grading_scale=_get_user_grading_scale(user_id),
                           courses=courses, course_groups=course_groups,
                           requested_course=requested)


@app.route("/api/analyze-outline", methods=["POST"])
@ai_limit
@login_required
@requires_active
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

    # An outline that never states a weight must not come back carrying any.
    # The pipeline has the same guard; both paths analyse the same documents
    # and a student must not get a fabricated scheme from whichever one they
    # happened to use. See _scheme_is_uncorroborated.
    uncorroborated = _scheme_is_uncorroborated(parsed, text)
    if uncorroborated:
        parsed = dict(parsed, components=[], grading_options=[])

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
        "error": ("" if components else
                  ("This outline does not say what each assessment is worth -- the weighting "
                   "is announced separately. Nexora will not guess weights nobody wrote; add "
                   "them yourself once you have them."
                   if uncorroborated else "No clear grade components were detected.")),
    })

@app.route("/api/sync/file", methods=["POST"])
def sync_course_file():
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401
    access = _entitlement(user)
    if not access["active"]:
        # This is the read-only state, seen from the extension's side: what is
        # already imported stays, and nothing new arrives.
        return jsonify({"error": access["message"], "subscription_required": True}), 402
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


# ============================
# Response headers
# ============================
# There were none. For an app holding someone's grades that is worth fixing
# before it is worth charging for: without X-Frame-Options any site can put
# Nexora in an invisible iframe over its own buttons, and without a CSP an
# injected <script src> can reach any host it likes.
#
# The policy names the four origins the pages actually use. 'unsafe-inline' is
# in there because every page carries its own <style> and <script> block --
# it weakens the XSS half of CSP but keeps the part that matters most here,
# which is that script can only ever come from these hosts.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    # Course images come from whichever LMS the student uses, and that domain
    # is not known ahead of time.
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    if IS_PRODUCTION:
        # Only in production: sending HSTS from a local http server would pin
        # localhost to https in the developer's browser.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


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
    """Say what ran out, in a sentence.

    Flask-Limiter's `description` is the raw rule -- "12 per 1 hour" -- and it
    was glued straight onto the sentence after it, so the calendar showed
    "12 per 1 hour This limit protects the shared AI quota every student's
    outline analysis depends on." Reported as part of a feature looking broken,
    which is fair: it reads like a stack trace.
    """
    detail = str(getattr(exc, "description", "") or "").strip()
    rule = re.match(r"^(\d+)\s+per\s+(?:1\s+)?(\w+)", detail)
    if rule:
        said = f"That is the limit of {rule.group(1)} per {rule.group(2)} for this feature. "
    else:
        said = "That is more requests than Nexora allows in a short time. "
    return _error_response(
        429, "Slow down for a moment",
        said + "It protects the Gemini quota every student's outline analysis "
               "shares. Nothing is lost, and the allowance refills on its own.",
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


@app.context_processor
def _inject_contact():
    """SUPPORT_EMAIL, everywhere. Kept out of the templates so a personal address
    can never be hardcoded onto a page the whole internet can read.

    The same reasoning applies to the trial length and the corpus size: both
    are quoted as fact on public pages, both move, and both were hardcoded.
    /signup carried "Free while you study" for one deploy after the trial went
    live -- a promise of permanent free access on the page where someone
    commits -- and it would have drifted again, because signup renders from
    five different places and only one of them passes anything.
    """
    return {"contact_email": SUPPORT_EMAIL,
            "trial_days": TRIAL_DAYS,
            # Quoted to the student on the profile form and enforced by
            # _password_problem. Two copies of a number drift.
            "min_password_length": MIN_PASSWORD_LENGTH,
            "corpus_cases": OUTLINE_CORPUS_CASES,
            "extension_url": EXTENSION_URL,
            "extension_zip": EXTENSION_ZIP_AVAILABLE}


# ============================
# Canvas
# ============================
# Brightspace has no student-facing API, which is why it needs a scraping
# extension. Canvas has one, so Canvas needs no extension at all: the student
# issues a token in their own Canvas settings and the server reads through it.

CANVAS_TIMEOUT = int(os.environ.get("CANVAS_TIMEOUT", "25"))
CANVAS_MAX_PAGES = 12
# One request per page body, so this is the ceiling on how long a sync can get.
CANVAS_MAX_PAGE_BODIES = int(os.environ.get("CANVAS_MAX_PAGE_BODIES", "60"))


def _canvas_fernet():
    """Key derived from SECRET_KEY so there is no second secret to manage.

    Rotating SECRET_KEY therefore invalidates stored Canvas tokens and every
    student reconnects. That is the safer failure: a rotated secret should not
    leave old credentials readable.
    """
    from base64 import urlsafe_b64encode
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    secret = (app.config.get("SECRET_KEY") or "").encode("utf-8")
    if not secret:
        raise RuntimeError("SECRET_KEY is required before a Canvas token can be stored.")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"nexora-canvas-v1",
               info=b"canvas-access-token").derive(secret)
    return Fernet(urlsafe_b64encode(key))


def _canvas_encrypt(token):
    return _canvas_fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _canvas_decrypt(blob):
    return _canvas_fernet().decrypt(blob.encode("ascii")).decode("utf-8")


def _canvas_base_url(raw):
    """Accept whatever a student pastes and reduce it to a scheme and host."""
    value = str(raw or "").strip()
    if not value:
        return ""
    if not re.match(r"(?i)^https?://", value):
        value = "https://" + value
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").strip().lower()
    if not host or "." not in host or parsed.scheme != "https":
        return ""
    return f"https://{host}"


def _canvas_request(base_url, token, path, params=None):
    url = f"{base_url}/api/v1/{path.lstrip('/')}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    request_obj = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request_obj, timeout=CANVAS_TIMEOUT) as response:
        body = response.read().decode("utf-8", "replace")
        link = response.headers.get("Link", "") or ""
    return json.loads(body) if body.strip() else [], link


def _canvas_next_link(link_header):
    # Canvas paginates with RFC 5988 Link headers; follow rel="next" only.
    for part in (link_header or "").split(","):
        chunk = part.strip()
        if not chunk.startswith("<"):
            continue
        url, _, rest = chunk[1:].partition(">")
        if 'rel="next"' in rest:
            return url
    return ""


def _canvas_list(base_url, token, path, params=None):
    """Every list endpoint, followed to the end of its pages."""
    merged = []
    page_params = dict(params or {})
    page_params.setdefault("per_page", 100)
    url = None
    for _ in range(CANVAS_MAX_PAGES):
        if url is None:
            data, link = _canvas_request(base_url, token, path, page_params)
        else:
            request_obj = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json"})
            with urllib.request.urlopen(request_obj, timeout=CANVAS_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "[]")
                link = response.headers.get("Link", "") or ""
        if isinstance(data, dict):
            data = [data]
        merged.extend(data or [])
        url = _canvas_next_link(link)
        if not url:
            break
    return merged


def _canvas_error_message(exc):
    """Turn a urllib failure into something a student can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return ("Canvas rejected that token. Generate a new access token in Canvas "
                    "under Account \u2192 Settings \u2192 New Access Token, and paste it again.")
        if exc.code == 404:
            return "That address does not look like a Canvas site. Check the web address you pasted."
        return f"Canvas returned an error ({exc.code})."
    if isinstance(exc, urllib.error.URLError):
        return "Could not reach that Canvas address. Check the web address and try again."
    return "Could not read from Canvas."


def _canvas_term_label(course):
    term = course.get("term") if isinstance(course.get("term"), dict) else {}
    name = str(term.get("name") or "").strip()
    # Canvas often names the default term "Default Term", which tells a student nothing.
    if name and name.lower() not in {"default term", "term", "the end of time"}:
        return name[:120]
    start = str(course.get("start_at") or term.get("start_at") or "")[:10]
    if len(start) == 10:
        month = int(start[5:7])
        season = "Winter" if month <= 4 else ("Summer" if month <= 8 else "Fall")
        return f"{season} {start[:4]}"
    return ""


def _canvas_due_date(value, zone=None):
    """Canvas sends ISO 8601 with a Z; the app stores plain YYYY-MM-DD.

    Taking `str(value)[:10]` took the UTC date, which is the day AFTER a
    late-evening local deadline -- the same defect the Brightspace paths had.
    Canvas is if anything worse for it: 11:59 PM is its default due time.
    """
    return local_date_iso(value, zone)


def canvas_sync(user, fetch_list=None, base_url=None):
    """Pull the student's Canvas into the same tables Brightspace writes to.

    `fetch_list(path, params)` is how the data arrives. By default it is an HTTP
    call carrying the student's access token. Where a university forbids students
    from issuing tokens, the extension fetches the same paths through the browser
    session and hands them over, and everything below this line is unchanged --
    the interpretation of a Canvas payload exists once, here.
    """
    canvas_zone = _user_zone(user)
    account = CanvasAccount.query.filter_by(user_id=user.id).first()

    if fetch_list is None:
        if not account:
            raise ValueError("No Canvas account is connected.")
        token = _canvas_decrypt(account.token_encrypted)
        base = account.base_url
        fetch_list = lambda path, params=None: _canvas_list(base, token, path, params)
    else:
        base = _canvas_base_url(base_url) or (account.base_url if account else "")
        if not base:
            raise ValueError("Canvas address is missing.")
        token = None

    # syllabus_body is where Canvas keeps the course outline, and it only
    # comes back if you ask for it. Without it a Canvas student had no outline
    # at all unless they found the file and uploaded it by hand -- so grade
    # intelligence, the pass rules and Ask-the-outline were all unavailable on
    # a platform that had been handing us the text the whole time.
    courses = fetch_list("courses", {
        "enrollment_state": "active",
        "include[]": ["term", "course_image", "syllabus_body"],
        "state[]": ["available"],
    })

    course_payload, content_items, task_rows, announcements = [], [], [], []
    grade_rows = []
    pages_read = 0
    seen_courses = 0

    for course in courses:
        if not isinstance(course, dict) or course.get("access_restricted_by_date"):
            continue
        cid = course.get("id")
        if not cid:
            continue
        seen_courses += 1
        raw_code = str(course.get("course_code") or course.get("name") or "OTHER")
        code = _normalize_course_code(raw_code)
        course_payload.append({
            "source_uid": f"canvas:{base}:{cid}",
            "course_code": raw_code,
            "course_name": str(course.get("name") or raw_code),
            "term": _canvas_term_label(course),
            "image_url": str(course.get("image_download_url") or ""),
            "brightspace_url": f"{base}/courses/{cid}",
        })

        # The syllabus, as an outline item. It takes the same road every other
        # outline does -- _import_content stores it, the analyzer reads it --
        # because the whole point of writing both LMSs through one importer is
        # that nothing downstream learns which one a student uses.
        syllabus = _html_to_text(str(course.get("syllabus_body") or "")).strip()
        if len(syllabus) >= 200:        # a one-line "see the PDF" is not an outline
            content_items.append({
                "course": code,
                "title": f"{raw_code} Course Syllabus",
                "content_type": "outline",
                "source_url": f"{base}/courses/{cid}/assignments/syllabus",
                "source_uid": f"canvas:{base}:{cid}:syllabus",
                "content_text": syllabus,
                "module_name": "Syllabus",
                "published_at": str(course.get("created_at") or "")[:80],
            })

        try:
            assignments = fetch_list(f"courses/{cid}/assignments",
                                     {"order_by": "due_at"})
        except Exception:
            assignments = []
        for row in assignments:
            if not isinstance(row, dict):
                continue
            title = str(row.get("name") or "").strip()
            if not title:
                continue
            uid = f"canvas:{base}:{cid}:assignment:{row.get('id')}"
            content_items.append({
                "course": code,
                "title": title,
                "content_type": "assignment",
                "source_url": str(row.get("html_url") or ""),
                "source_uid": uid,
                "content_text": _html_to_text(str(row.get("description") or "")),
                "module_name": "Assignments",
                "published_at": str(row.get("created_at") or "")[:80],
            })
            due = _canvas_due_date(row.get("due_at"), canvas_zone)
            if due:
                task_rows.append({"course": code, "title": title, "due": due,
                                  "uid": uid, "link": row.get("html_url")})

        # Canvas states the weighting outright, so unlike the Brightspace path
        # there is nothing for a model to infer.
        try:
            groups = fetch_list(f"courses/{cid}/assignment_groups",
                                {"include[]": ["assignments", "submission"]})
        except Exception:
            groups = []
        if groups:
            grade_rows.extend(_canvas_grade_rows(
                code, groups, weighted=bool(course.get("apply_assignment_group_weights"))))

        try:
            posts = fetch_list(f"courses/{cid}/discussion_topics",
                             {"only_announcements": "true"})
        except Exception:
            posts = []
        for row in posts[:25]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            announcements.append({
                "course": code,
                "title": title,
                "detail": _html_to_text(str(row.get("message") or ""))[:2000],
                "uid": f"canvas:{base}:{cid}:announcement:{row.get('id')}",
            })

        try:
            pages = fetch_list(f"courses/{cid}/modules",
                             {"include[]": ["items"]})
        except Exception:
            pages = []
        for module in pages:
            if not isinstance(module, dict):
                continue
            module_name = str(module.get("name") or "")[:1000]
            for entry in (module.get("items") or [])[:80]:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title") or "").strip()
                kind = str(entry.get("type") or "").lower()
                if not title or kind in {"subheader", "assignment"}:
                    continue

                # A page's text is what every AI feature actually needs, and it
                # is one cheap request. Bounded per course so a big module tree
                # cannot turn one sync into hundreds of calls.
                body = ""
                page_url = str(entry.get("page_url") or "")
                if token and kind == "page" and page_url and pages_read < CANVAS_MAX_PAGE_BODIES:
                    pages_read += 1
                    try:
                        page, _ = _canvas_request(base, token, f"courses/{cid}/pages/{page_url}")
                        if isinstance(page, dict):
                            body = _html_to_text(str(page.get("body") or ""))
                    except Exception:
                        body = ""   # a page we cannot read is still worth listing

                content_items.append({
                    "course": code,
                    "title": title,
                    "content_type": "lecture" if kind in {"page", "file"} else "resource",
                    "source_url": str(entry.get("html_url") or ""),
                    "source_uid": f"canvas:{base}:{cid}:item:{entry.get('id')}",
                    "content_text": body,
                    "module_name": module_name,
                    "published_at": "",
                })

    course_counts = _import_courses(user, course_payload)
    content_counts = _import_content(user, {"items": content_items})
    task_counts = _import_canvas_tasks(user, task_rows)
    update_counts = _import_canvas_announcements(user, announcements)
    grade_counts = _import_canvas_grades(user, grade_rows)

    if account:
        account.last_sync = datetime.utcnow()
        account.last_error = ""
    db.session.commit()

    return {
        "courses": seen_courses,
        "course_counts": course_counts,
        "content": content_counts,
        "tasks": task_counts,
        "announcements": update_counts,
        "grades": grade_counts,
    }


def _canvas_grade_rows(course, groups, weighted):
    """Flatten Canvas assignment groups into the app's grade components.

    Canvas offers two shapes. Either the course weights its groups -- Assignments
    20%, Midterm 30% -- in which case each assignment carries a slice of its
    group's weight proportional to what it is out of. Or it does not, and the
    final mark is simply total points earned over total points possible, which
    is the same thing as every point carrying equal weight.
    """
    rows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        assignments = [a for a in (group.get("assignments") or []) if isinstance(a, dict)]
        if not assignments:
            continue
        category = str(group.get("name") or "Assignments")[:80]
        group_weight = float(group.get("group_weight") or 0) if weighted else 0.0

        # Only graded work carries weight; a zero-point survey should not dilute it.
        pool = sum(float(a.get("points_possible") or 0) for a in assignments)

        for assignment in assignments:
            points = float(assignment.get("points_possible") or 0)
            submission = assignment.get("submission")
            score = None
            if isinstance(submission, dict) and submission.get("score") is not None:
                try:
                    score = float(submission["score"])
                except (TypeError, ValueError):
                    score = None

            if weighted and pool > 0:
                weight = group_weight * (points / pool)
            elif weighted:
                weight = 0.0
            else:
                weight = points   # unweighted courses are just points out of points

            rows.append({
                "course": course,
                "name": str(assignment.get("name") or "Assessment")[:255],
                "category": category,
                "weight": round(weight, 4),
                "score": score,
                "max_score": points or 100.0,
                "uid": f"canvas:{assignment.get('id')}",
            })
    return rows


def _import_canvas_grades(user, rows):
    """Upsert grade components, without trampling anything typed by hand.

    A mark the student entered themselves is theirs. Canvas overwrites its own
    rows -- matched on the note carrying the Canvas assignment id -- and leaves
    everything else alone.
    """
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for row in rows[:400]:
        existing = GradeEntry.query.filter_by(
            user_id=user.id, course_full=row["course"], notes=row["uid"]).first()
        if existing:
            unchanged = (existing.weight == row["weight"] and existing.score == row["score"]
                         and existing.max_score == row["max_score"] and existing.name == row["name"])
            if unchanged:
                counts["unchanged"] += 1
                continue
            existing.name = row["name"]
            existing.weight = row["weight"]
            existing.score = row["score"]
            existing.max_score = row["max_score"]
            existing.category = row["category"]
            existing.updated_at = datetime.utcnow()
            counts["updated"] += 1
            continue
        db.session.add(GradeEntry(
            id=uuid4().hex, user_id=user.id, course_full=row["course"],
            name=row["name"], weight=row["weight"], score=row["score"],
            max_score=row["max_score"], category=row["category"], notes=row["uid"],
        ))
        counts["added"] += 1
    db.session.commit()
    return counts


def _import_canvas_tasks(user, rows):
    """Assignments with a due date become the same Tasks the ICS feed creates,
    so deadlines, the calendar and the dashboard need no Canvas-specific path."""
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows[:600]:
        course = row["course"]
        base_name = base_title(row["title"])
        group_id = make_group_id(course, base_name)
        existing = Task.query.filter_by(user_id=user.id, source_uid=row["uid"]).first()
        if existing and existing.sync_state == "dismissed":
            # Deleted on purpose; a refresh must not bring it back.
            counts["unchanged"] += 1
            continue
        if existing:
            if existing.due_raw == row["due"] and existing.title == row["title"]:
                counts["unchanged"] += 1
                continue
            existing.title = row["title"]
            existing.base_title = base_name
            existing.due_raw = row["due"]
            existing.group_id = group_id
            existing.source_link = _lms_link(row.get("link"))
            counts["updated"] += 1
            continue
        db.session.add(Task(
            id=uuid4().hex, user_id=user.id, group_id=group_id,
            title=row["title"], base_title=base_name, course_full=course,
            course_prefix=course_prefix(course) if course != "OTHER" else "OTHER",
            due_raw=row["due"], kind="due", is_deadline=True, source="canvas",
            source_link=_lms_link(row.get("link")),
            created_at=stamp, source_uid=row["uid"], sync_state="active",
        ))
        counts["added"] += 1
    db.session.commit()
    return counts


def _import_canvas_announcements(user, rows):
    counts = {"added": 0, "unchanged": 0}
    for row in rows[:200]:
        if AcademicUpdate.query.filter_by(user_id=user.id, related_uid=row["uid"]).first():
            counts["unchanged"] += 1
            continue
        db.session.add(AcademicUpdate(
            id=uuid4().hex, user_id=user.id, course_full=row["course"],
            update_type="announcement", title=row["title"][:500],
            detail=row["detail"], related_uid=row["uid"],
        ))
        counts["added"] += 1
    db.session.commit()
    return counts


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "bassemtamer2016@gmail.com").strip().lower()


def _config_report():
    """What is configured, what is not, and what each gap actually costs.

    Ordered worst first. "critical" means students are getting a degraded
    product right now; "warning" means the operator is flying blind.
    """
    findings = []

    def add(level, title, detail, effect):
        findings.append({"level": level, "title": title, "detail": detail, "effect": effect})

    gemini = (os.environ.get("GEMINI_API_KEY", "").strip()
              or os.environ.get("GOOGLE_API_KEY", "").strip())
    if not gemini:
        add("critical", "Gemini is not configured",
            "GEMINI_API_KEY is not set.",
            "Outline analysis, summaries, flashcards, quizzes and the course tutor all fall "
            "back to the deterministic parser. Students get noticeably worse results and "
            "nothing tells them why.")
    else:
        add("ok", "Gemini is configured", f"Key ending {gemini[-4:]}.",
            "AI features use the model. Watch quota: a free-tier key returns 429 for "
            "Pro-tier models and every call lands on flash.")

    if not email_is_configured():
        add("critical", "Email is not configured",
            "SMTP_HOST and SMTP_FROM are not both set.",
            "Password reset cannot send. A student who forgets their password is locked "
            "out permanently, which is a refund and a lost customer once this is paid for.")
    else:
        add("ok", "Email is configured", f"Sending through {SMTP_HOST}.",
            "Password reset works.")

    if not os.environ.get("SENTRY_DSN", "").strip():
        add("warning", "Error reporting is off",
            "SENTRY_DSN is not set.",
            "A crash in production is only discovered when a student bothers to report it.")
    else:
        add("ok", "Error reporting is on", "Sentry is receiving events.",
            "Crashes surface without waiting for a report. PII is deliberately not sent.")

    limiter_store = (os.environ.get("RATE_LIMIT_STORAGE_URI", "").strip()
                     or os.environ.get("REDIS_URL", "").strip())
    if not limiter_store:
        add("warning", "Rate limits are per worker",
            "Neither RATE_LIMIT_STORAGE_URI nor REDIS_URL is set.",
            "Limits are held in each worker's memory, so a limit of N is really N per "
            "worker. Enough to bound abuse, not enough to be exact.")
    else:
        add("ok", "Rate limits are shared", "Backed by the configured store.",
            "Limits are exact across workers.")

    if IS_PRODUCTION:
        add("ok", "Running in production mode", "DATABASE_URL is set.",
            "Secure cookies are on and SECRET_KEY is required.")
    else:
        add("warning", "Running in development mode",
            "DATABASE_URL is not set, so this is the SQLite fallback.",
            "Fine locally. On a real deployment it would mean data lives on a disk that "
            "does not survive a restart.")

    if not SUPPORT_EMAIL:
        add("warning", "No contact address",
            "SUPPORT_EMAIL is not set.",
            "The privacy and help pages fall back to the feedback form, which is behind a "
            "login, so a locked-out student has no way to reach anyone.")

    try:
        _canvas_fernet()
        add("ok", "Canvas tokens can be encrypted", "Key derives from SECRET_KEY.",
            "Rotating SECRET_KEY invalidates stored Canvas tokens and everyone reconnects.")
    except Exception as exc:
        add("critical", "Canvas tokens cannot be encrypted", str(exc),
            "Connecting Canvas will fail outright.")

    order = {"critical": 0, "warning": 1, "ok": 2}
    findings.sort(key=lambda f: order.get(f["level"], 3))
    return findings


def _log_config_at_startup():
    """Say it once, loudly, in the logs where a deploy can be seen going wrong."""
    for finding in _config_report():
        if finding["level"] == "critical":
            app.logger.error("CONFIG: %s - %s %s", finding["title"], finding["detail"], finding["effect"])
        elif finding["level"] == "warning":
            app.logger.warning("CONFIG: %s - %s", finding["title"], finding["detail"])


@app.route("/admin/health")
@login_required
def admin_health():
    """Operator's view of what is and is not configured.

    Same gate as the feedback inbox. It says nothing a student could use, but it
    does name which keys are missing, so it stays behind the admin check.
    """
    if (current_user.email or "").lower() != ADMIN_EMAIL:
        return redirect(url_for("home"))
    findings = _config_report()
    return render_template(
        "admin_health.html",
        findings=findings,
        critical=sum(1 for f in findings if f["level"] == "critical"),
        warnings=sum(1 for f in findings if f["level"] == "warning"),
    )


def _canvas_reference(row):
    """The Canvas coordinates for a content row, if it came from Canvas."""
    if not str(row.source_uid or "").startswith("canvas:"):
        return None
    try:
        parts = str(row.source_uid).split(":")
        # canvas:https://host:<course_id>:item:<id>  -- rebuilt rather than stored,
        # so rows imported before this existed still resolve.
        course_id = parts[-3]
        return {"course_id": course_id}
    except Exception:
        return None


def _canvas_fill_content(user, row):
    """Fetch a Canvas item's text on demand and save it.

    Brightspace has to route this through the extension because only the
    student's browser holds that session. Canvas does not -- the server has a
    token, so it reads the page directly and the student never learns that a
    round trip existed.

    Returns True when the row now has usable text.
    """
    account = CanvasAccount.query.filter_by(user_id=user.id).first()
    reference = _canvas_reference(row)
    if not account or not reference:
        return False

    try:
        token = _canvas_decrypt(account.token_encrypted)
    except Exception:
        return False

    base = account.base_url
    course_id = reference["course_id"]
    text = ""

    # Canvas puts the page's own slug at the end of the URL it gave us, which is
    # exact. Deriving one from the title would be a guess that silently misses
    # every page whose title was edited after it was created.
    match = re.search(r"/pages/([^/?#]+)", str(row.source_url or ""))
    if match:
        try:
            page, _ = _canvas_request(base, token, f"courses/{course_id}/pages/{match.group(1)}")
            if isinstance(page, dict) and page.get("body"):
                text = _html_to_text(str(page["body"]))
        except Exception:
            text = ""

    if len(text.strip()) < 120:
        return False

    row.content_text = text
    row.content_hash = _content_payload_hash({
        "title": row.title, "content_type": row.content_type, "source_url": row.source_url or "",
        "content_text": text, "module_name": row.module_name or "",
        "published_at": row.published_at or "",
    })
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return True


@app.route("/connect-canvas")
@login_required
def connect_canvas():
    account = CanvasAccount.query.filter_by(user_id=current_user.id).first()
    return render_template("connect_canvas.html", account=account)


@app.route("/api/canvas/connect", methods=["POST"])
@limiter.limit("12 per hour")
@login_required
def canvas_connect():
    data = request.get_json(silent=True) or {}
    base = _canvas_base_url(data.get("base_url"))
    token = str(data.get("token") or "").strip()
    if not base:
        return jsonify({"error": "Enter your Canvas web address, for example canvas.yourschool.edu."}), 400
    if len(token) < 20:
        return jsonify({"error": "That does not look like a Canvas access token."}), 400

    # Prove the token works before storing it, and learn who it belongs to.
    try:
        profile, _ = _canvas_request(base, token, "users/self/profile")
    except Exception as exc:
        return jsonify({"error": _canvas_error_message(exc)}), 400
    if not isinstance(profile, dict) or not profile.get("id"):
        return jsonify({"error": "Canvas did not recognise that token."}), 400

    # Canvas states the student's own timezone, so there is no reason to wait
    # for a page view before dating their deadlines correctly.
    _adopt_reported_timezone(current_user, profile.get("time_zone"))

    account = CanvasAccount.query.filter_by(user_id=current_user.id).first()
    if not account:
        account = CanvasAccount(id=uuid4().hex, user_id=current_user.id)
        db.session.add(account)
    account.base_url = base
    account.token_encrypted = _canvas_encrypt(token)
    account.canvas_name = str(profile.get("name") or "")[:255]
    account.last_error = ""
    db.session.commit()
    return jsonify({"ok": True, "name": account.canvas_name, "base_url": base})


@app.route("/api/canvas/sync", methods=["POST"])
@limiter.limit("30 per hour")
@login_required
@requires_active
def canvas_sync_now():
    account = CanvasAccount.query.filter_by(user_id=current_user.id).first()
    if not account:
        return jsonify({"error": "Connect Canvas first."}), 400
    try:
        summary = canvas_sync(current_user)
    except Exception as exc:
        app.logger.exception("Canvas sync failed")
        message = _canvas_error_message(exc)
        account.last_error = message
        db.session.commit()
        return jsonify({"error": message}), 502
    return jsonify({"ok": True, **summary})


CANVAS_STALE_AFTER = timedelta(hours=int(os.environ.get("CANVAS_STALE_HOURS", "6")))


@app.route("/api/canvas/status", methods=["GET"])
@login_required
def canvas_status():
    """Cheap enough to call on every page load; it only reads one row."""
    account = CanvasAccount.query.filter_by(user_id=current_user.id).first()
    if not account:
        return jsonify({"connected": False})
    stale = account.last_sync is None or (datetime.utcnow() - account.last_sync) > CANVAS_STALE_AFTER
    return jsonify({
        "connected": True,
        "stale": stale,
        "last_sync": account.last_sync.isoformat() if account.last_sync else None,
    })


@app.route("/api/canvas/auto-sync", methods=["POST"])
@limiter.limit("12 per hour")
@login_required
def canvas_auto_sync():
    """The background refresh. Refuses when the data is still fresh, so a
    student opening ten pages does not cause ten syncs."""
    account = CanvasAccount.query.filter_by(user_id=current_user.id).first()
    if not account:
        return jsonify({"skipped": "not-connected"})
    if account.last_sync and (datetime.utcnow() - account.last_sync) <= CANVAS_STALE_AFTER:
        return jsonify({"skipped": "fresh"})
    try:
        summary = canvas_sync(current_user)
    except Exception as exc:
        # An automatic refresh failing must not put an error in front of someone
        # who never asked for it; record it for the Canvas page to show.
        app.logger.warning("Canvas auto-sync failed: %s", exc)
        account.last_error = _canvas_error_message(exc)
        db.session.commit()
        return jsonify({"skipped": "failed"})
    return jsonify({"ok": True, **summary})


@app.route("/api/canvas/ingest", methods=["POST"])
def canvas_ingest():
    """Take Canvas payloads the extension fetched through the browser session.

    Authenticated with the extension key, like the Brightspace sync routes,
    because this arrives from the extension rather than from a logged-in page.
    The extension is transport only: it fetched these paths and is handing over
    what came back. Nothing here trusts it to have interpreted anything.
    """
    user = _extension_user_from_request()
    if not user:
        return jsonify({"error": "Invalid or missing extension token."}), 401
    access = _entitlement(user)
    if not access["active"]:
        # This is the read-only state, seen from the extension's side: what is
        # already imported stays, and nothing new arrives.
        return jsonify({"error": access["message"], "subscription_required": True}), 402

    payload = request.get_json(silent=True) or {}
    pages = payload.get("pages")
    if not isinstance(pages, dict):
        return jsonify({"error": "pages must be an object of path -> list."}), 400

    base = _canvas_base_url(payload.get("base_url"))
    if not base:
        return jsonify({"error": "A Canvas web address is required."}), 400

    # Paths are keys into what the browser already fetched; params are ignored
    # because the extension asked for them when it made the request.
    def from_payload(path, params=None):
        rows = pages.get(path)
        return rows if isinstance(rows, list) else []

    try:
        summary = canvas_sync(user, fetch_list=from_payload, base_url=base)
    except Exception as exc:
        app.logger.exception("Canvas ingest failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, **summary})


@app.route("/api/canvas/disconnect", methods=["POST"])
@login_required
def canvas_disconnect():
    """Forget the token. Imported coursework stays; only the connection goes."""
    CanvasAccount.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/support")
def support():
    """Public for the same reason /privacy is: the person who needs it most is
    the one who cannot get the extension to connect, so it cannot sit behind a
    login. Linked as the support URL on the Chrome Web Store listing."""
    return render_template("support.html")


@app.route("/privacy")
def privacy():
    """Public, and deliberately not behind login: the Chrome Web Store listing
    has to link to it, and a reviewer reads it without an account."""
    return render_template("privacy.html", updated="31 August 2026")


def _public_budget_left():
    row = PublicUsage.query.get(date.today().isoformat())
    return PUBLIC_ANALYSES_PER_DAY - (row.count if row else 0)


def _spend_public_budget():
    today = date.today().isoformat()
    row = PublicUsage.query.get(today)
    if not row:
        row = PublicUsage(day=today, count=0)
        db.session.add(row)
    row.count = (row.count or 0) + 1
    db.session.commit()


@app.route("/what-do-i-need")
def public_analyzer():
    """The shop window: read a real course outline, no account.

    This is the only honest way to sell a grading engine. A student who has
    never heard of Nexora will not create an account to find out whether it
    reads their outline correctly -- but they will paste one in. Nothing is
    stored, and the result is theirs to keep only if they sign up.
    """
    return render_template("public_analyzer.html",
                           trial_days=TRIAL_DAYS,
                           budget_left=max(0, _public_budget_left()))


@app.route("/api/public/analyze-outline", methods=["POST"])
@limiter.limit(PUBLIC_ANALYSES_PER_IP)
def public_analyze_outline():
    """One Gemini call, no account, nothing written down.

    Deliberately the single-call analyzer rather than the three-stage pipeline:
    a third of the cost, several times faster, and this is a demonstration
    rather than the policy someone will rely on all term. The saved version a
    signed-in student gets is the careful one.
    """
    if _public_budget_left() <= 0:
        message = ("Nexora has read its limit of outlines for today. "
                   + ("An account gets its own allowance instead of sharing this one."
                      if PUBLIC_CAP_MENTIONS_SIGNUP else "Try again tomorrow."))
        return jsonify({"error": message, "capped": True,
                        "signup": PUBLIC_CAP_MENTIONS_SIGNUP}), 429

    text = ""
    uploads = [u for u in (request.files.getlist("files") or []) if u and u.filename]
    if uploads:
        if len(uploads) > 3:
            return jsonify({"error": "Upload at most 3 pages here. An account takes 8."}), 400
        try:
            parts = []
            for upload in uploads:
                name, extracted, _hash, _size = _extract_uploaded_course_file(upload)
                extracted = _sanitize_extracted_text(extracted)
                if not extracted:
                    raise ValueError(f"Nexora could not read any text from {name}.")
                parts.append(extracted)
            text = "\n\n".join(parts)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        except Exception:
            app.logger.exception("Public outline extraction failed")
            return jsonify({"error": "Nexora could not read that file. Try a PDF, or paste the text."}), 422
    else:
        text = _sanitize_extracted_text(str((request.get_json(silent=True) or {}).get("text") or ""))

    if len(text.strip()) < 60:
        return jsonify({"error": "Paste the grading section of your outline, or upload the file."}), 400

    try:
        parsed = _gemini_json(
            "You extract university grading schemes as strict JSON. Output only JSON.",
            _outline_analysis_prompt(text, "this course"),
            max_tokens=3500)
    except Exception as exc:
        app.logger.warning("Public outline analysis failed: %s", exc)
        if _looks_like_quota_error(exc):
            return jsonify({"error": "Nexora's AI is at its limit right now. Try again in a little while.",
                            "capped": True}), 503
        return jsonify({"error": "Nexora could not read that outline. Try pasting just the grading table."}), 503

    _spend_public_budget()
    parsed = _normalize_advanced_grading_model(parsed if isinstance(parsed, dict) else {})
    # Same guard as the two signed-in paths. This one is the shop window, so a
    # fabricated scheme here is also the first thing a stranger ever sees
    # Nexora do.
    if _scheme_is_uncorroborated(parsed, text):
        return jsonify({"error": "This outline does not say what each assessment is worth -- "
                                 "it says the weighting will be announced separately. Nexora "
                                 "will not guess weights nobody wrote."}), 422

    components = []
    total = 0.0
    for comp in (parsed.get("components") or []):
        if not isinstance(comp, dict):
            continue
        try:
            weight = float(comp.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if weight <= 0 or weight > 100:
            continue
        components.append({"name": str(comp.get("name") or "Assessment")[:120],
                           "weight": round(weight, 2),
                           # The page separates bonus weight from the base, and
                           # the name alone is not always enough to spot one.
                           "category": str(comp.get("category") or "")[:50],
                           "notes": str(comp.get("notes") or "")[:200]})
        total += weight
    if not components:
        return jsonify({"error": "Nexora could not find a grading breakdown in that. "
                                 "Paste the section with the percentages in it."}), 422

    return jsonify({
        "ok": True,
        "components": components,
        "total_weight": round(total, 2),
        "pass_mark": parsed.get("pass_mark"),
        "rules": [str(r)[:300] for r in (parsed.get("rules") or [])][:6],
        "options": [str(o.get("option_name") or "") for o in (parsed.get("grading_options") or [])
                    if isinstance(o, dict)][:4],
        "ambiguities": [str(a)[:300] for a in (parsed.get("ambiguities") or [])][:3],
        "notice": "Read by AI, and not guaranteed correct. Check it against your outline "
                  "before relying on it.",
        "budget_left": max(0, _public_budget_left()),
    })


@app.route("/api/timezone", methods=["POST"])
@login_required
def save_timezone():
    """The browser reports its own IANA zone, once, and that is what is stored.

    Every deadline is dated in this zone. Guessing it from an IP or assuming the
    server's would silently date a student's work wrongly, which is the one
    mistake that makes them miss it -- so an unrecognised name is refused and
    the app stays on UTC rather than storing something it cannot honour.
    """
    name = _valid_timezone((request.get_json(silent=True) or {}).get("timezone"))
    if not name:
        return jsonify({"ok": False, "error": "Unrecognised timezone."}), 400
    if (current_user.timezone or "") == name:
        return jsonify({"ok": True, "timezone": name, "recomputed": 0})
    current_user.timezone = name
    db.session.commit()
    # Fix what is already stored rather than waiting for the next sync. No
    # network: every deadline keeps the instant it came with.
    recomputed = _recompute_task_dates(current_user)
    if recomputed:
        app.logger.info("Timezone set to %s; re-dated %d deadlines", name, recomputed)
    return jsonify({"ok": True, "timezone": name, "recomputed": recomputed})


@app.route("/terms")
def terms():
    """Public, like /privacy: Stripe and the app stores both link to it, and
    someone deciding whether to pay reads it before they have an account."""
    return render_template("terms.html", updated="4 September 2026")


@app.route("/healthz")
@limiter.exempt
def healthz():
    """Cheap liveness probe: confirms the process is up and the database answers.

    It also reports which commit is running. Every deploy so far has been
    confirmed by guessing at some string that happened to change on a public
    page, which fails entirely when a release only touches pages behind a
    login -- as the one fixing the dashboard course cap did.
    """
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok", "build": BUILD_REVISION})
    except Exception as exc:
        app.logger.exception("Health check failed: %s", exc)
        return jsonify({"status": "degraded", "database": "unreachable"}), 503



# ============================
# OUTLOOK FEASIBILITY TEST
# ============================
# Built to answer ONE question before any of Phase 4 is designed: can a
# Carleton student grant Nexora read access to their own mail, or does the
# tenant answer "Need admin approval"? Nothing here is a feature. It reads
# five subject lines and throws them away.
#
# Three rules it keeps, and the reasons are not decoration:
#
# - NO TOKEN IS STORED ANYWHERE, not even in the session. Flask's session is a
#   signed COOKIE and browsers drop one over ~4 KB; a Graph access token is
#   1-2 KB and a refresh token more, so stashing them there would silently
#   break the session rather than fail loudly. The callback exchanges the
#   code, calls Graph, renders, and lets the token fall out of scope. That is
#   stricter than "session only" and avoids the bug entirely.
# - NO EMAIL CONTENT TOUCHES THE DATABASE. Subjects and senders are rendered
#   into one response and never written.
# - The client id and secret come from the environment and are never rendered,
#   logged, or included in an error message.
OUTLOOK_TEST_ENABLED = os.environ.get("OUTLOOK_TEST_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "").strip()
# "Accounts in any organizational directory" is /organizations. /common would
# also admit personal Microsoft accounts, which is not what a university is.
MS_AUTHORITY = os.environ.get(
    "MS_AUTHORITY", "https://login.microsoftonline.com/organizations").strip()
MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "").strip()
# RESOURCE scopes only. MSAL *rejects* offline_access, openid and profile as
# reserved -- `ValueError: You cannot use any scope value that is reserved` --
# and adds all three itself. Verified before this was written, because passing
# them raises before any network call and reads as the sign-in being broken.
MS_SCOPES = ["User.Read", "Mail.Read"]

try:                                    # pragma: no cover - import shape
    import msal
except Exception:                       # pragma: no cover
    # Same posture as Pillow/pytesseract in requirements.txt: a missing wheel
    # degrades this one route instead of taking every worker down at import.
    msal = None


def _outlook_test_gate():
    """404 unless the flag is on. Called first on all three routes.

    With the flag off the route does not exist -- not for the admin, not for
    anyone. A throwaway probe that answers 200 with a configuration checklist
    is still telling an unauthenticated prodder that Nexora has a mail
    integration behind a flag, and the checklist lives in
    OUTLOOK_FEASIBILITY_V1.md where it is more use anyway. The flag is the
    switch; off means gone.
    """
    if not OUTLOOK_TEST_ENABLED:
        abort(404)


def _outlook_test_unavailable():
    """Why this test cannot run even with the flag on, or "" when it can."""
    if msal is None:
        return "The msal package is not installed in this environment."
    missing = [name for name, value in (("MS_CLIENT_ID", MS_CLIENT_ID),
                                        ("MS_CLIENT_SECRET", MS_CLIENT_SECRET),
                                        ("MS_REDIRECT_URI", MS_REDIRECT_URI))
               if not value]
    if missing:
        return "Not configured: " + ", ".join(missing) + " must be set in the environment."
    return ""


def _ms_client():
    return msal.ConfidentialClientApplication(
        MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET, authority=MS_AUTHORITY)


def _aadsts_code(*texts):
    """The AADSTS id out of whatever Microsoft said.

    It is the only part of the message that is stable enough to look up, and
    AADSTS65001 / AADSTS90094 are the two that decide whether Phase 4 is
    possible at all.
    """
    # Not a fixed width: AADSTS7000215 is seven digits and a {5,6} bound
    # matched the first six, reporting a code that does not exist.
    match = re.search(r"AADSTS\d+", " ".join(str(t or "") for t in texts))
    return match.group(0) if match else ""


def _graph_get(path, token):
    """GET one Graph endpoint. Returns (data, error_text)."""
    request_obj = urllib.request.Request(
        "https://graph.microsoft.com/v1.0" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request_obj, timeout=25) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        # Verbatim. A paraphrased permission error is the one thing that would
        # make this test worthless.
        return None, "Graph %s %s: %s" % (exc.code, exc.reason, body[:1500])
    except Exception as exc:
        return None, "Graph request failed: %s" % exc


def _outlook_page(**kwargs):
    return render_template("outlook_test.html", user=current_user,
                           redirect_uri=MS_REDIRECT_URI, **kwargs)


@app.get("/outlook-test")
@login_required
def outlook_test():
    _outlook_test_gate()
    if (current_user.email or "").lower() != ADMIN_EMAIL:
        return redirect(url_for("home"))
    return _outlook_page(unavailable=_outlook_test_unavailable())


@app.get("/outlook-test/start")
@login_required
def outlook_test_start():
    _outlook_test_gate()
    if (current_user.email or "").lower() != ADMIN_EMAIL:
        return redirect(url_for("home"))
    unavailable = _outlook_test_unavailable()
    if unavailable:
        return _outlook_page(unavailable=unavailable)
    # CSRF: the state is minted here, kept in the session (it is 32 characters,
    # not a token) and must come back unchanged.
    state = uuid4().hex
    session["ms_state"] = state
    try:
        url = _ms_client().get_authorization_request_url(
            MS_SCOPES, redirect_uri=MS_REDIRECT_URI, state=state,
            # Force the consent screen so the ANSWER to this test is visible
            # rather than silently reused from an earlier grant.
            prompt="consent")
    except Exception as exc:
        return _outlook_page(error_title="Could not build the sign-in URL",
                             error_detail=str(exc))
    return redirect(url)


@app.get("/outlook-test/callback")
@login_required
def outlook_test_callback():
    _outlook_test_gate()
    if (current_user.email or "").lower() != ADMIN_EMAIL:
        return redirect(url_for("home"))
    unavailable = _outlook_test_unavailable()
    if unavailable:
        return _outlook_page(unavailable=unavailable)

    expected = session.pop("ms_state", None)
    if not expected or request.args.get("state") != expected:
        return _outlook_page(
            error_title="The sign-in did not come back from where it went",
            error_detail="The state parameter did not match. Start again from "
                         "the Connect button.")

    # Microsoft refused before any code was issued. THIS is the interesting
    # path: AADSTS65001 and AADSTS90094 are what "Need admin approval" looks
    # like on the wire.
    if request.args.get("error"):
        detail = request.args.get("error_description") or ""
        return _outlook_page(
            error_title="Microsoft refused the request: " + request.args["error"],
            error_detail=detail,
            aadsts=_aadsts_code(request.args.get("error"), detail))

    code = request.args.get("code") or ""
    if not code:
        return _outlook_page(error_title="Microsoft returned no authorization code",
                             error_detail="No `code` and no `error` on the callback.")

    try:
        result = _ms_client().acquire_token_by_authorization_code(
            code, scopes=MS_SCOPES, redirect_uri=MS_REDIRECT_URI)
    except Exception as exc:
        return _outlook_page(error_title="The token exchange raised",
                             error_detail=str(exc))

    token = (result or {}).get("access_token")
    if not token:
        detail = (result or {}).get("error_description") or json.dumps(result or {})[:1500]
        return _outlook_page(
            error_title="Microsoft refused the token: " + str((result or {}).get("error") or "unknown"),
            error_detail=detail,
            aadsts=_aadsts_code((result or {}).get("error"), detail))

    me, err = _graph_get("/me", token)
    if err:
        return _outlook_page(error_title="Signed in, but /me was refused",
                             error_detail=err, aadsts=_aadsts_code(err))
    messages, err = _graph_get(
        "/me/messages?$top=5&$select=subject,from,receivedDateTime"
        "&$orderby=receivedDateTime%20desc", token)
    if err:
        # Worth separating: this is consent granted for the sign-in and
        # refused for the mailbox, which means Mail.Read specifically is the
        # blocked one rather than the app as a whole.
        return _outlook_page(
            error_title="Signed in, but the mailbox was refused",
            error_detail=err, aadsts=_aadsts_code(err),
            display_name=(me or {}).get("displayName", ""))

    rows = []
    for item in (messages or {}).get("value", [])[:5]:
        sender = ((item.get("from") or {}).get("emailAddress") or {})
        rows.append({
            "subject": item.get("subject") or "(no subject)",
            "who": sender.get("name") or sender.get("address") or "(unknown sender)",
            "address": sender.get("address") or "",
            "when": item.get("receivedDateTime") or "",
        })
    # The token goes out of scope here. Nothing is written anywhere.
    return _outlook_page(display_name=(me or {}).get("displayName", ""),
                         upn=(me or {}).get("userPrincipalName", ""),
                         granted_scopes=" ".join(sorted((result or {}).get("scope", "").split())),
                         messages=rows, succeeded=True)


_log_config_at_startup()


if __name__ == "__main__":
    app.run(debug=True)
