# Nexora (uni-helper-project)

Flask app — an "academic OS" for university students. Brightspace sync, course
workspaces, AI course-outline analysis, grade calculation, study tools.

"Nexora" is the product name. The repo/folder is `uni-helper-project`; there is
no folder called `nexora`.

## Layout

| Path | What |
|---|---|
| `app.py` | Everything — ~5,040 lines, 45 routes, models, AI calls. Single file by design. |
| `templates/` | Jinja pages. Nine extend `app_base.html`; `study_room` and `course_workspace` deliberately do not (see UI). |
| `static/nexora-app.css` | The design system: tokens, rail, cards, buttons, forms. Change a token here and every page follows. |
| `static/` | `style.css` and `nexora-target.css` are dead since the redesign -- nothing links them |
| `extension/` | Chrome extension that scrapes Brightspace and pushes to `/api/sync/*` |
| `tests/` | pytest — 311 tests. `conftest.py` points the suite at a throwaway DB. |
| `evals/` | live-API outline eval. Not run by pytest; costs quota. |
| `*_V*.md` | Changelogs, newest version number = most recent work |

## Running locally

```bash
venv/Scripts/python.exe app.py     # http://127.0.0.1:5000
venv/Scripts/python.exe -m pytest tests/ -q
```

Python 3.12.10 at `C:\Users\Bassem\AppData\Local\Programs\Python\Python312\`.
`pytest` is installed in the venv but deliberately not in `requirements.txt`.

**`.env` is not auto-loaded** — `app.py` doesn't use python-dotenv. Source it
first when a task needs Gemini:

```bash
set -a; . ./.env; set +a
```

## Config

- `DATABASE_URL` unset locally → falls back to SQLite (`app.py:50`). Leave it
  unset. The live Postgres (`uni-helper-db` on Render) must never be the local
  target.
- `GEMINI_API_KEY` in `.env` (gitignored). Currently a **free-tier** key, so
  Pro-tier models return 429 and every AI call lands on flash.
- `SECRET_KEY` has a dev fallback.

## Security configuration

`SECRET_KEY` is **required in production and the app refuses to boot without
it**. `IS_PRODUCTION` is derived from `DATABASE_URL` being set, which Render
does and local development deliberately does not. The old behaviour -- falling
back to a constant that is public in this repository -- meant anyone could
forge a session cookie for any user id, so failing to start is the safer error.

Session and remember-me cookies are Secure in production, HttpOnly, SameSite
Lax, with a 14-day lifetime. `SESSION_COOKIE_SECURE` has to be switched off for
tests because the test client speaks http; `tests/conftest.py` does that.

Rate limits come from Flask-Limiter keyed on the account when signed in and the
address otherwise. Storage follows `RATE_LIMIT_STORAGE_URI` or `REDIS_URL`,
falling back to per-worker memory -- in that state a limit of N is really N per
worker, so point it at Render Key Value to make the numbers exact. `AI_LIMIT` guards every Gemini-backed route because the
shared quota is the scarce resource; `/login` and `/signup` have their own
tighter limits. Storage is in-memory, so a limit of N is really N per worker --
good enough to bound abuse, not a substitute for Redis.

Passwords need 8+ characters (`_password_problem`). Emails are shape-checked
with `_EMAIL_RE`. Password reset exists but **SMTP is unconfigured in production**, so a student
who forgets their password is locked out permanently until it is set up.

Anything rendered through `innerHTML` in the templates must go through `esc()`.
Model output, uploaded filenames and text pulled from course documents all
reach the DOM that way.

## Concurrency

`gunicorn.conf.py` runs `gthread` workers, not the default sync worker. An
outline analysis spends 37-39 seconds waiting on Google, and a sync worker
serves one request start to finish, so N simultaneous analyses previously
occupied N workers and every other request -- dashboard, landing page, login --
queued behind workers that were idle but unavailable. Threads let a worker
waiting on a socket serve other requests. 2 workers x 8 threads by default,
overridable with `WEB_CONCURRENCY` and `GUNICORN_THREADS`.

This is a mitigation. The real fix is to run the analysis outside the request
and have the browser poll, which the front end is already shaped for. That
refactor has to move a 172-line route off `current_user` and add a jobs table,
so it needs its own pass with the UI actually verified.

Set `SENTRY_DSN` to turn on error reporting; without it the integration is
inert. PII is deliberately not sent -- grades and outline text are the
student's private record.

## Deploying

Push to `main` → Render auto-deploys `uni-helper-project` via `gunicorn app:app`
→ https://uni-helper-project.onrender.com

There is no CI and no staging. **A push is a production deploy — always ask
first.** Bassem does not need to pull; his local clone is what gets edited.

`gunicorn.conf.py` raises the worker timeout to 300s because outline analysis
chains several sequential Gemini calls.

## Gemini

Called over raw REST (`generativelanguage.googleapis.com`), no SDK.
`_gemini_json()` (`app.py:975`) is the single choke point for JSON calls;
`_gemini_model_candidates()` (`app.py:927`) builds the fallback chain.

Gotchas that have already bitten:

- **Verify a model ID exists before adding it.** `gemini-3.6-pro` was hardcoded
  as the top reasoning candidate and 404'd on every call, silently downgrading
  outline analysis to flash. List real IDs with `GET /v1beta/models?key=…`.
- **Flash models are thinking models.** Internal reasoning bills against
  `maxOutputTokens`, so a small budget yields `finishReason: MAX_TOKENS` and
  truncated, unparseable JSON. `_gemini_json` now detects this and retries with
  a doubled budget.
- **Keep `except ValueError` in the retry loop.** `JSONDecodeError` used to
  escape it, so one malformed reply killed every remaining retry and model
  candidate instead of falling through.

## Email and password reset

SMTP is configured entirely from the environment (`SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS`), so any provider
works without a code change -- Gmail with an app password is free and plenty at
student volume. With nothing configured, `/forgot-password` says so plainly
instead of accepting a request it cannot fulfil.

Reset tokens are stored as a SHA-256 hash, never in the clear, on the same
reasoning as `password_hash`: reading the table must not let anyone reset an
account. They are single-use, expire after an hour, and issuing a new one
invalidates the rest. `/forgot-password` returns an identical response whether
or not the address exists -- anything else turns it into a way to discover who
has an account.

## Gemini quota behaviour

The free key cannot serve Pro-tier models, so `gemini-pro-latest` and
`gemini-3.1-pro-preview` return 429 on every call. `_gemini_json` now parks a
model that answers 429 for ten minutes and one that answers 404 for a day, and
skips parked models on later calls -- roughly eight doomed requests per outline
analysis, against the very quota the pipeline needs. `_usable_gemini_models`
falls back to the full list when everything is parked, so a wrong guess can
never be why a student gets nothing.

When the pipeline fails and the deterministic fallback is too thin to be
trusted -- no components, or a weight total nowhere near 100 -- the route
returns 503 with an honest message rather than rendering a near-empty policy
with a Confirm button under it. `_fallback_is_usable` draws that line, and
`_looks_like_quota_error` decides whether the student is told to wait or told
something broke. A clean table outline still uses the fallback; the gate only
catches the cases where it understood nothing.

## Bonus marks

Grade points are summed without normalising (`contribution = pct/100 * weight`
in `_evaluate_option`), so a component weighted 4 adds exactly 4 points. That
makes a bonus work correctly as an ordinary component: a student who aces a
scheme whose base totals 100 and also earns a 4% bonus finishes at 104, which is
right. `_check_total` already knows this and excludes bonus weight, detected by
`bonus|extra credit` in the name or category, before flagging a total.

So a bonus must be RECORDED, never dropped. Dropping one silently under-reports
every student who earned it. Rule 3 briefly said to ignore bonuses, which was
harmless only while the rules never reached the pipeline; delivering them made
the app start discarding earned marks. Record any bonus whose amount the outline
states, wherever it is stated; only a bonus with no stated amount belongs in
`rules` instead.

## A rule Nexora cannot check must never fail a student

`_evaluate_pass_rules` is tri-state: `passed` is True, False, or **None** for a
requirement that could not be answered. Only False fails the course. It used to
score `actual is not None and actual>=required`, so "cannot be evaluated" and
"the student fell short" were the same answer, and both produced FAIL, the
numeric grade, and `_fail_letter` -- an F. A BIOL 1103 student with all seven
components entered at full marks was shown **FAIL 100% · F**.

What could not be evaluated was a group. BIOL 1103 is 60 lecture / 40 lab and
requires a pass in the combined mark and also in the lecture, so the model
emitted three derived components and described the combined one in terms of the
other two. Those two are not grade rows, `_group_percent` returned None for any
member it could not find, and both group rules failed.

Three defences, and all three are needed:

- `_expand_group_members` flattens a group written in terms of other derived
  components down to real ones, with a cycle guard.
- `_compile_pass_rules` binds every rule to the component list that will
  actually be shown, **after** that list is settled -- the pipeline's components
  and the deterministic parser's differ. A group covering ≥95% of the scheme
  weight, or whose message says combined/overall, becomes an `overall_minimum`,
  which is a number the calculator already holds. Anything still unmatched is
  marked `unbound`, surfaced in `ambiguities`, and never enforced.
- `_group_state` returns `ok` / `pending` / `unbound` instead of a bare
  percentage, so an unentered mark reads as pending rather than zero.

The UI has three states to match: `ruleHtml` renders pending as a neutral
bullet with the reason, not a red cross.

Do not "simplify" any of these back into a boolean. The failure mode is silent
and it is the worst thing this app can do to someone.

## Events, exams and the calendar feed

**An event is a `Task`.** Not its own table: the dashboard, calendar,
completion toggle and `.ics` feed already understand Tasks, and a parallel
model would have to be taught to every one of them. `kind` separates them --
`due` from an LMS, `exam` and `event` from the student. `due_time`,
`location` and `notes` were added for exams.

Two traps in that choice, both already paid for:

- `build_grouped_cards` only dated `opens`/`due`/`closes`. A kind it did not
  know left `main_due_raw` empty, so a midterm someone added by hand never
  landed on a day. The last branch now takes any dated kind.
- Delete acts on the **group**, not the row. An imported assignment can be
  three Tasks sharing a `group_id`, and the calendar shows the group once;
  deleting only the clicked row let the group fall back to its next date and
  the entry reappeared days later.

**Deleting an import is `sync_state='dismissed'`, never a row deletion.** Both
importers check it before re-adding, otherwise the next sync undoes the
student's decision. `load_tasks` filters it out.

**Exam detection is deterministic on purpose** (`_find_date`, `_find_time`,
`_find_location`, `_looks_like_an_exam`). Dates are the one thing a model must
not be creative about, and this runs on ordinary page views where a Gemini call
would eat the quota the outline pipeline needs. Suggestions are *offered*,
never inserted: a misread date that appeared by itself would be
indistinguishable from one the university published. `_NOT_AN_EXAM` is what
stops "practice midterm posted" becoming a midterm -- keep the plurals in it.

**The `.ics` feed is how Google Calendar connects**, because Google cannot sign
in to Nexora. The token in the path is the entire credential, so it is
rotatable and the route is deliberately unauthenticated. iCalendar is fussy in
ways that fail silently: an unescaped comma ends a property early, lines are
limited to 75 **octets** (fold without splitting a UTF-8 sequence), and `DTEND`
is exclusive -- an all-day event ending the same day is zero-length and Google
hides it. Use `content_type=`, not `mimetype=`, or Flask appends a second
charset.

## The grade calculator is the one place for grades

`/grade-calculator` takes `?course=`, lists the student's courses, and loads
that course's saved `GradeEntry` rows, its policy, and the official result from
`_compute_official_result` -- never its own copy of the pass-rule maths, which
is how two screens come to disagree about whether someone passed.

**Marks post as `"16/20"`, not as `16` with a separate maximum.** A bare number
is read as a percentage by `_parse_mark_entry` -- correct for the single-field
entry it was built for, wrong for the two boxes here, where 4 out of 20 was
stored as 4%.

The course workspace still owns the same data and is untouched, on Bassem's
call: it is the riskiest file in the repo and this change is about where
things live, not how they are calculated. Remove its outline/grades tabs only
once he has said the new page is better.

## Outline analysis

`_outline_schema()` returns the extraction schema; `_outline_analysis_prompt()`
builds the prompt around it. Keep the schema module-level: it briefly lived
inside the prompt builder, which put it out of scope for the route that hands
it to `_high_accuracy_outline_analysis()`. Every analysis raised NameError, the
broad `except Exception` around the pipeline reported it as "the AI pipeline was
unavailable", and students got the deterministic parser's near-empty model
behind a 35% confidence badge. `tests/test_analysis_wiring.py` runs pyflakes
over `app.py` to catch that class of bug; install it in the venv
(`pip install pyflakes`), it is not in requirements.txt, and the test skips
without it.

The sixteen numbered extraction rules live in `_outline_extraction_rules()`,
not inside the prompt builder, because the pipeline needs them too. For as long
as the pipeline existed the analyze route built a prompt and discarded it, so
only the bare schema dict reached the three stages and every rule was dead text
on the path a course workspace actually runs. Both stage one and the auditor
now receive them. Keep it that way: a rule written into
`_outline_analysis_prompt()` alone reaches only the standalone analyzer, and
the two paths will silently disagree about the same outline again.

`_outline_analysis_prompt()` is the single prompt builder. Both the course
workspace and the standalone grade-calculator analyzer call it. They used to
carry separate copies and drifted badly -- the standalone one had shrunk to
four lines and a components-only schema, so the same outline scored worse
depending on which page the student used. Keep it one definition.

Before and after touching that prompt, run the corpus:

```bash
set -a; . ./.env; set +a
venv/Scripts/python.exe evals/run_outline_eval.py
```

Eleven outlines, each isolating a different mechanism (dropped marks, overflow
cap, excluded bonus, hurdle, aggregate minimum, alternative schemes, gated
scheme, weight transfer, underspecified, multi-option with eligibility).
Currently 11/11. Add a case when a new outline shape appears; the corpus is the
only defence against tuning the prompt to whatever outline is on the desk.

A course-specific parser, `_math1104_outline_analysis()`, used to sit in front
of all of this, gated on four exact sentences from one Winter 2026 syllabus.
Removing it turned up the same overfitting in two further layers: the shared
`_evaluate_option()` counted eligibility by looking for grade rows literally
named `Test 1`..`Test 3`, and `course_workspace.html` rendered an
`eligibility.minimum_written_tests` key. Because the AI path emitted no
eligibility at all, option gating had in fact never worked for any other
course. Options now carry a general
`{requires_completed, from_components, message}`, and the old key is still read
so saved analyses keep working. Do not reintroduce a per-course branch; add a
corpus case instead.

Deleting it also exposed two defects in the deterministic fallback that the
branch had been masking: `_looks_like_grading_label()` rejected any label
starting with "the", killing "The average of best two written tests", while
accepting rule prose like "Students must obtain at least" as a component. Both
are fixed generally.

The eval itself can overfit too: the weight-transfer case originally demanded
`selection_rules` and failed a correct answer that had used `grading_options`
instead, and the multi-option case first failed because its expected component
list was short, not because the model was wrong. Check the mechanism is
represented, not that it took the shape you imagined.

**A long rule crowds out a short one.** Rule 17 was first written as a
paragraph explaining derived components, whole-course requirements and what it
did *not* restrict. `multi_option_eligibility` then failed on flash-lite with
"selection_rules empty" -- rule 10 was still there and still correct, and the
model stopped obeying it. Two shorter rewrites still failed; three sentences
passed. On a small model, prompt length is a variable in its own right. Run the
corpus after adding a rule even when the rule looks obviously safe.

Prompts are the weak point, not the model. On a real Carleton
outline, flash got all four weights right but dropped a `max of 11.5%` cap the
prompt never asked for, and flattened a "qualifies for second marking scheme"
threshold into a hard `component_minimum`. Fix prompts before spending money on
Pro-tier models.

## UI

`static/nexora-app.css` + `templates/app_base.html` are the shared shell. A page
extends `app_base.html` and sets `{% set nav_active = '<endpoint>' %}`.
Page-specific CSS belongs in that page's `{% block head %}`, never in the shared
sheet.

Two pages deliberately do not extend it. `study_room.html` is a full-bleed WebGL
stage with no scroll, so it links the shared sheet for tokens and rebuilds only
the floating chrome. `course_workspace.html` has ~230 class names and 400 lines
of JavaScript reaching into them; its palette was remapped in place rather than
rewritten, because re-deriving every grid template there risks grade
intelligence for a purely visual change.

Selectors in the shared sheet use `:where()` so page rules can override them. A
chain of `:not()` raises specificity and silently out-ranks the page — that is
why the grade calculator's readonly field once rendered as a white browser box.

The palette is a single neutral ramp. Cards must be visibly lighter than the page
behind them; the first pass sat everything within nine points of black and had to
be lifted wholesale.

## The two LMS integrations

**Brightspace has no student-facing API.** That is the only reason the Chrome
extension exists. The scraper is generic — `location.origin` plus D2L's own
`/d2l/api/le/` — so it works at any university; what once limited it to one was
Chrome's permission gate, not the code.

**Canvas has a real API**, reachable two ways:

- *Access token*, stored encrypted (Fernet, key derived from `SECRET_KEY` via
  HKDF) because it must be replayed on every sync. Rotating `SECRET_KEY`
  invalidates every stored token. This path syncs with the browser closed.
- *Browser session*, when a university forbids students from issuing tokens —
  which some do, and the Canvas error tells students to go and ask an
  administrator. Canvas honours the session cookie for same-origin requests, so
  the extension fetches and posts to `/api/canvas/ingest`.

`canvas_sync(user, fetch_list=None)` takes its fetcher as a parameter: an HTTP
call with a token by default, a dictionary lookup when the extension supplied the
payloads. **Do not reimplement the parsing in JavaScript.** The extension is
transport only; every line that interprets a Canvas payload lives on the server,
once.

Both LMSs write through `_import_courses` and `_import_content`, so the
dashboard, calendar, grade calculator and study tools never learn which one a
student uses. Adding a third should extend those, not parallel them.

## What gets imported, and what the dashboard opens on

The extension asks once, before the first import: **every term** or **one
term**, chosen from a list Brightspace itself supplies (`NEXORA_LIST_TERMS` ->
`discoverCoursesFromApi(true)` -> `termsFrom`). The answer is
`importScope: 'all' | 'term'` plus `importTerm`; `includeFinished` is the 3.3
spelling of the same choice and is still read, so an upgrade keeps its meaning.

`currentTermLabel()` decides the default from the date -- Jan-Apr Winter,
May-Aug Summer, Sep-Dec Fall. It is deliberately **not** `selectedTermLabel()`,
which reads the tab on screen: that answers a different question, and answering
with it imported Winter 2025 for a student sitting in September. Naming a term
also forces `includeFinished` on the enrolments query, because the active-only
variant cannot return a term that is over.

The dashboard has to hold up its end: `home()` computes `current_term_label`
from the same date rule and renders every term group, showing only that one,
with `#termPick` to switch and the choice kept in `localStorage`. If the
calendar's term is not one the student is enrolled in, it falls back to the
newest group -- otherwise a student between terms gets an empty page with their
courses hidden behind it. "Import everything" is only a usable answer because
of this.

## Extension

Injection is programmatic (`chrome.scripting.executeScript`), not a static
`content_scripts` entry, so it works on any university's domain with a permission
the student grants for their own origin.

- **`allFrames: true` is required.** Brightspace renders My Courses in a widget
  iframe; injecting only into the top document finds nothing and reports "no
  course cards found" on a page full of course cards.
- Several frames answer a discovery request, so **a frame that finds nothing
  stays silent** and only the top document reports failure, after waiting longer
  than a subframe needs to succeed.
- **Course discovery uses the enrolments API, not the DOM.** The My Courses
  widget renders one term tab at a time, so scraping returns whichever term
  happens to be mounted. The DOM is merged in for images and as a fallback.
- **A Manifest V3 worker is collected after ~30s without an event**, and a sync
  runs for minutes. `holdWorkerAwake()` / `releaseWorker()` bracket long work;
  without them, clicking away kills the sync mid-run.
- `extension/*-check.html` are browser-run checks for the parser, the term
  helpers and the popup state machine. They are excluded from the package.
  `fetch()` needs an origin, so serve the folder first --
  `venv/Scripts/python.exe -m http.server 8777 --directory extension` -- and
  open `http://localhost:8777/term-check.html`. There is no node on this
  machine; `new Function(src)` in the browser is how a whole file gets a syntax
  check.
- The store package is rebuilt from six files plus `icons/`; rebuild
  `store/nexora-sync.zip` whenever the version changes, and never include the
  check pages.

## Operating it

`/healthz` reports the running commit from `RENDER_GIT_COMMIT`. Use it to confirm
a deploy — guessing at some string that changed on a public page fails entirely
when a release only touches pages behind a login.

`/admin/health` (gated on `ADMIN_EMAIL`) lists what is configured and what each
gap costs a student, and the same findings are logged at startup. Nexora degrades
quietly by design — no Gemini key means a deterministic fallback, no SMTP means a
polite message — which is right for the student and invisible to whoever runs it.

`SUPPORT_EMAIL` is the one public contact address. It is injected into every
template as `contact_email`; never hardcode an address into a page, because
`/privacy` and `/support` are public and were once carrying a personal Gmail.

Course codes are capped at `COURSE_CODE_MAX` (40) to match every column that
stores one. SQLite keeps an over-long value quietly; Postgres raises
`StringDataRightTruncation` and takes the whole import down — a 500 that exists
only in production.

## Conventions

- Match the surrounding style in `app.py` — it is dense and compact in places;
  don't reformat neighbouring code.
- Comments explain *why*, usually referencing a bug that motivated the line.
- New assessment logic gets a test in `tests/`.
