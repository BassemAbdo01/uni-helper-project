# Nexora (uni-helper-project)

Flask app — an "academic OS" for university students. Brightspace sync, course
workspaces, AI course-outline analysis, grade calculation, study tools.

"Nexora" is the product name. The repo/folder is `uni-helper-project`; there is
no folder called `nexora`.

## Layout

| Path | What |
|---|---|
| `app.py` | Everything — ~5,040 lines, 45 routes, models, AI calls. Single file by design. |
| `templates/` | 13 Jinja pages (landing, dashboard, course_workspace, grade_calculator, study_room, calendar, connect_brightspace, settings, profile, feedback, auth) |
| `static/` | `style.css`, `landing-v6.css`, `nexora-target.css` |
| `extension/` | Chrome extension that scrapes Brightspace and pushes to `/api/sync/*` |
| `tests/` | pytest — 67 tests. `test_routes.py` covers requests; `conftest.py` points the suite at a throwaway DB. |
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
with `_EMAIL_RE`. There is still **no password reset flow**, so a student who
forgets their password cannot recover their account.

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

Known and unfixed: the route builds `prompt` and never sends it. The fourteen
numbered rules therefore never reach the model on the course-workspace path --
only the schema dict is passed to the three-stage pipeline. This predates the
refactor. One visible consequence is that bonus components are excluded on the
standalone path (rule 3) but survive in the pipeline's output, so a Scheme with
a 4% bonus totals 104%.

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

Prompts are the weak point, not the model. On a real Carleton
outline, flash got all four weights right but dropped a `max of 11.5%` cap the
prompt never asked for, and flattened a "qualifies for second marking scheme"
threshold into a hard `component_minimum`. Fix prompts before spending money on
Pro-tier models.

## Conventions

- Match the surrounding style in `app.py` — it is dense and compact in places;
  don't reformat neighbouring code.
- Comments explain *why*, usually referencing a bug that motivated the line.
- New assessment logic gets a test in `tests/`.
