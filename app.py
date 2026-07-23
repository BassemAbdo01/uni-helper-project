import os
import re
import urllib.request
import urllib.error
import json
import hashlib
from datetime import datetime, date, timedelta
import calendar as calendar_module
from uuid import uuid4

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()


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


def save_last_sync():
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
    r"\b(ECOR|SYSC|MATH|PHYS|CHEM|STAT|ELEC|COMP)\s*[-]?\s*(\d{4})([A-Z]?)\b",
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
def import_calendar_string(ics_text):
    tasks = load_tasks()

    existing_keys = {
        (
            (t.get("title", "") or "").lower(),
            t.get("due_raw", ""),
            (t.get("course_full", "") or "").upper(),
            t.get("kind", ""),
            t.get("source", ""),
        )
        for t in tasks
    }

    cal = Calendar(ics_text)
    imported = 0

    for event in cal.events:
        title = (getattr(event, "name", "") or "").strip()

        if not title:
            continue

        desc = (getattr(event, "description", "") or "")
        kind = detect_kind(title + " " + desc)

        if kind == "junk":
            continue

        due_raw = get_event_date_iso(event)

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

        add_task({
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

    return imported


def fetch_feed_ics(feed_url):
    if not feed_url.startswith("https://brightspace.carleton.ca/"):
        return ""

    req = urllib.request.Request(feed_url, headers={"User-Agent": "UniHelper/1.0"})

    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()

    return data.decode("utf-8", errors="ignore")


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
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/api/copilot/status", methods=["GET"])
@login_required
def copilot_status():
    """Report AI configuration without exposing any secret value."""
    connected = bool((
        os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    ).strip())
    return jsonify({
        "connected": connected,
        "provider": "Gemini",
        "model": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    })


@app.route("/api/copilot", methods=["POST"])
@login_required
def copilot():
    """Conversational, semester-aware AI with a useful local fallback."""
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("message") or "").strip()
    if not prompt:
        return jsonify({"error": "Ask me something first."}), 400

    tasks = build_grouped_cards(load_tasks())
    tasks = sort_cards(tasks)
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
    lower = prompt.lower()
    first_name = (current_user.name or "Student").split()[0]

    # Real Gemini conversation. Only recent chat and task metadata are sent.
    api_key = (
        os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    ).strip()
    if not api_key:
        return jsonify({
            "error": "Gemini is not connected to this running service. Link the environment group containing GEMINI_API_KEY, then rebuild and deploy."
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

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": (
            "You are Nexora Copilot, a smart conversational AI for university students. "
            "Answer naturally and directly like a high-quality general assistant. Explain academic "
            "topics, brainstorm, write, reason, and help plan the semester. Use the supplied student "
            "context only when relevant. Never invent course dates or deadlines. Be concise by default "
            "but give detail when requested, and never repeat a canned introduction.\n\n"
            f"CURRENT STUDENT CONTEXT:\n{json.dumps(semester_context, ensure_ascii=False)}"
        )}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1200},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
        answer = "".join(
            part.get("text", "")
            for part in result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        ).strip()
        if not answer:
            raise ValueError("Gemini returned no text")
        return jsonify({
            "answer": answer,
            "mode": "gemini",
            "meta": {"upcoming": len(upcoming), "overdue": len(overdue), "tasks": len(tasks)},
        })
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:1200]
        app.logger.error("Gemini API HTTP %s: %s", exc.code, details)
        message = "Gemini rejected the request. Check that the key is valid and the free quota is available."
        if exc.code == 429:
            message = "Gemini's free quota is temporarily exhausted. Wait a moment and try again."
        return jsonify({"error": message}), 502
    except Exception as exc:
        app.logger.exception("Gemini Copilot error: %s", exc)
        return jsonify({"error": "Gemini could not respond right now. Please try again."}), 502

    def task_line(task):
        due = pretty_date(task.get("main_due_raw", "")) or "no date"
        return f"{task.get('title', 'Task')} ({task.get('course_full', 'OTHER')}) — {due}"

    # Use a real language model when OPENAI_API_KEY is configured on Render.
    # Only the student's task titles/course codes/due dates and recent chat are sent.
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        semester_context = {
            "student_first_name": first_name,
            "today": date.today().isoformat(),
            "tasks": [
                {
                    "title": task.get("title", "Task"),
                    "course": task.get("course_full", "OTHER"),
                    "due": task.get("main_due_raw", ""),
                }
                for task in tasks[:40]
            ],
        }
        messages = [{
            "role": "system",
            "content": (
                "You are Nexora Copilot, a capable conversational AI for university students. "
                "Respond naturally like ChatGPT: directly answer any general or academic question, "
                "and use the supplied semester context when relevant. Be concise by default, warm but "
                "not fake, never repeat a canned introduction, and ask one useful follow-up only when "
                "needed. Do not claim a deadline or course fact that is absent from context.\n\n"
                f"SEMESTER CONTEXT:\n{json.dumps(semester_context, ensure_ascii=False)}"
            ),
        }]
        for item in history:
            role = item.get("role") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content[:4000]})
        messages.append({"role": "user", "content": prompt[:6000]})
        body = json.dumps({
            "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 700,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
            answer = result["choices"][0]["message"]["content"].strip()
            return jsonify({
                "answer": answer,
                "mode": "ai",
                "meta": {"upcoming": len(upcoming), "overdue": len(overdue), "tasks": len(tasks)},
            })
        except Exception as exc:
            app.logger.warning("Copilot API fallback: %s", exc)

    if any(word in lower for word in ("deadline", "due", "upcoming", "next")):
        selected = (overdue + upcoming)[:5]
        if selected:
            answer = "Here are the deadlines that need attention first:\n" + "\n".join(
                f"{i + 1}. {task_line(task)}" for i, task in enumerate(selected)
            )
        else:
            answer = "You have no deadlines in the next 14 days. Add tasks or sync Brightspace and I’ll track them here."
    elif any(word in lower for word in ("plan", "today", "study", "schedule", "priorit")):
        selected = (overdue + upcoming)[:3]
        if selected:
            blocks = []
            for i, task in enumerate(selected):
                minutes = 50 if i == 0 else 35
                blocks.append(f"{i + 1}. {minutes} min — {task.get('title', 'Task')} ({task.get('course_full', 'OTHER')})")
            answer = f"Here’s a focused plan for you, {first_name}:\n" + "\n".join(blocks) + "\nFinish with a 10-minute review and update anything you completed."
        else:
            answer = f"You’re clear right now, {first_name}. Try one 45-minute review block for your hardest course, then use 10 minutes to plan tomorrow."
    elif any(word in lower for word in ("overdue", "urgent", "behind")):
        if overdue:
            answer = "These are overdue, in recovery order:\n" + "\n".join(
                f"{i + 1}. {task_line(task)}" for i, task in enumerate(overdue[:5])
            ) + "\nStart with the quickest high-impact item, then contact the instructor if submission is closed."
        else:
            answer = "Nothing is overdue. Your dashboard is currently clear."
    elif any(word in lower for word in ("course", "class")):
        courses = sorted({t.get("course_full") for t in tasks if t.get("course_full") and t.get("course_full") != "OTHER"})
        answer = ("Your active courses are: " + ", ".join(courses) + ". Ask me to plan one of them by name.") if courses else "I don’t see course data yet. Connect Brightspace or add a task with a course code."
    else:
        answer = (
            "I can use your live dashboard data to build today’s plan, list deadlines, find overdue work, "
            "or summarize your active courses. Try: “Plan my study day” or “What is due next?”"
        )

    return jsonify({
        "answer": answer,
        "mode": "local",
        "meta": {"upcoming": len(upcoming), "overdue": len(overdue), "tasks": len(tasks)},
    })


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
    import_calendar_string(raw)

    return redirect(url_for("home", view="all"))


@app.post("/save_feed")
@login_required
def save_feed():
    feed_url = (request.form.get("feed_url") or "").strip()

    if feed_url:
        save_feed_url(feed_url)

    return redirect(url_for("connect_brightspace"))


@app.get("/sync")
@login_required
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
