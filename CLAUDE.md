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
| `tests/` | pytest — 819 tests. `conftest.py` points the suite at a throwaway DB. |
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

## Which model actually answers

`_gemini_model_candidates` is ordered by **measured availability on the live
key, not by version number**, and the order matters more than it looks: the two
Pro candidates 429 on every call with a free key, so the reasoning chain lands
on the flash list in practice, and whichever id leads that list is the model
that reads a student's outline.

Re-probed **2026-10-09** -- three text passes, two vision passes against a real
screenshot. The September table below it had gone completely stale, and trusting
it cost every call the price of a timeout plus a 503 before anything answered:

| Model | Text (3) | Vision (2) | Fastest success |
|---|---|---|---|
| `gemini-pro-latest` | 429 x3 | 429 x2 | never -- free key cannot serve Pro |
| `gemini-3.1-pro-preview` | 429 x3 | 429 x2 | never |
| `gemini-3.7-flash` | timeout, 503, 503 | timeout, 503 | **never -- and it led both chains** |
| `gemini-3.8-flash` | 503, 200, 503 | timeout x2 | 4.0s text only |
| `gemini-3.6-flash` | 200, 200, 503 | 503, 200 | 1.1s / 9.1s |
| `gemini-3.5-flash-lite` | 200 x3 | 200 x2 | 0.4s / 1.8s |
| `gemini-flash-lite-latest` | 200 x3 | 200 x2 | 0.5s / 2.5s |
| `gemini-flash-latest` | 200, 200, 503 | 200 x2 | 4.0s / 13.2s |
| `gemini-3.1-flash-lite` | 503, 200, 200 | 200 x2 | 6.4s / 9.4s |

**Text and vision now have separate orders**, because one list cannot serve both:
`gemini-3.8-flash` answers text one time in three and timed out on *both* vision
passes. Reading an outline screenshot went from ~40s to 9.8-16.5s on the same
image, and a timetable read through its own lite-first list takes 4.8s.

Capability still leads where it survives: `gemini-3.6-flash` is the strongest
model that answers at all, so it stays ahead of flash-lite, which is the one
documented below as dropping `selection_rules`. Ordering purely by uptime would
put the weakest model first on the task where reasoning decides a grade.

For the record, what the September probe said -- every line of which is now
wrong, which is the point:

| Model | Result (2026-09-05) |
|---|---|
| `gemini-3.7-flash` | 200, ~1s, 3/3 |
| `gemini-3.6-flash` | 429 on 2 of 3; 24.5s on the one that answered |
| `gemini-3.8-flash` | 503 UNAVAILABLE, 6-12s wasted per attempt |
| `gemini-3.5-flash-lite` | 200, fast, reliable |

`gemini-3.6-flash` used to lead the list, so with its quota gone the chain fell
all the way to `gemini-3.5-flash-lite` -- and flash-lite is the one model that
drops `selection_rules` on a multi-option outline. The corpus was failing on
the weakest model in the chain purely because the two above it were
unavailable, which reads as a prompt regression and is not one. **Check which
model answered before believing a corpus failure**; the runner prints it.

Re-probe before trusting this table: availability moves.

```bash
set -a; . ./.env; set +a
venv/Scripts/python.exe evals/probe_model_availability.py
```

`GET /v1beta/models?key=…` lists what the key is offered at all -- every id in the chain currently exists, so a failure is
quota or overload, never a bad id.

`_OVERLOADED_COOLDOWN_SECONDS` (120s) parks a model that answers 503 twice.
Without it a persistently-overloaded candidate near the front of the chain
costs 6-12 seconds of every analysis, on a path that already takes 37.

**A paid key is worth more than Pro-tier models.** Most of the observed damage
is unavailability, not reasoning: doomed 429s, a park, and a fall-through to the
weakest model. A paid key on flash removes that. Routing everything to Pro
multiplies the per-analysis cost of a path that is already the most expensive
thing Nexora does, and CLAUDE.md's older finding still stands -- on a real
Carleton outline flash got every weight right and missed a cap the prompt never
asked for. Prompts and availability first; Pro is the last lever, and the
corpus is how to tell whether it bought anything.

## Thinking is for the outline pipeline; everything else runs fast

`_gemini_json(mode=...)`: `None` keeps the reasoning chain; `"fast"` starts
from `gemini-3.5-flash-lite` (`_FAST_MODELS`); `"quick"` starts from
`gemini-3.6-flash` with `thinkingConfig.thinkingLevel = "low"`
(`_QUICK_MODELS`). The level is sent only to models matching
`_THINKING_LEVEL_MODELS` -- `thinkingBudget: 0` is a 400 on this family, and a
config the model rejects reads as the model failing.

Measured 2026-09-14, a 1,500-word lecture summary, live key:

| Path | Time |
|---|---|
| the default chain, as a student experienced it | **46.8s** |
| `gemini-3.7-flash`, first in that chain | 503, retry, park |
| `gemini-3.6-flash`, default thinking | 10.8s (1,241 thinking tokens) |
| `gemini-3.6-flash`, thinking low | 5.0s, same eight concepts |
| `gemini-3.5-flash-lite` | 2.5s, same eight concepts |

Study tools use `"fast"`: summary 46.8s -> 3.5s, flashcards 3.3s, and with a
real brief per tool (`_STUDY_TOOL_BRIEFS`) instead of one generic sentence,
twelve concepts with full-sentence explanations instead of eight headings.

**The tutor (`/api/copilot`) was the worst case.** One hardcoded model
(`GEMINI_MODEL`, default 3.6-flash) with full thinking and an 8,192-token
budget, and on a 429 it slept and retried the *same* model three times --
4.5s of sleeping then an error, on a key where 3.6 answers 429 two calls in
three. It now walks `_QUICK_MODELS` with low thinking, parks a failing model
the way `_gemini_json` does, and moves on without sleeping. A full lecture
explanation is 13s, all of it output; a follow-up that uses the conversation
is 3.6s. What is left is the model writing 1,700 words before the page shows
any of it -- which is what streaming is for, and the one thing that still
separates this from the Gemini app.

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

## An outline that states no weights must come back with none

`_scheme_is_uncorroborated(model, source_text)`. The three-stage pipeline read
the `underspecified` corpus case -- an outline whose evaluation section names
four assessments and says the weighting "will be announced in class during the
first week" -- invented a plausible 25/25/20/30 split, and scored itself
**high confidence**. A student plans a term around that and never finds out it
was fabricated.

No prompt rule prevents this, because the failure mode *is* the model being
confident. The guard is arithmetic instead: a real outline always states at
least one of its weights, so if not one extracted weight can be found in the
source text the whole scheme is refused, the components and options are
emptied, an ambiguity says why, and confidence is capped at 40.

Two properties worth keeping:

- **All-or-nothing.** One weight is legitimately derived and never written --
  "4 assignments at 6% each" is a 24% component and no outline says 24 -- so
  dropping components individually would delete real ones. One corroborated
  weight clears the scheme.
- **Points count as corroboration.** A syllabus priced in points ("homework
  100 points out of 500") states no percentage at all, and converting it to 20%
  is correct. The check allows any `part/whole*100` drawn from numbers in the
  source, or the guard would blank a scheme that was read perfectly.

It runs on all three paths -- the pipeline, the standalone analyzer, and the
public reader -- because a student must not get a fabricated scheme from
whichever one they happened to open.

## Bonus marks

Grade points are summed without normalising (`contribution = pct/100 * weight`
in `_evaluate_option`), so a component weighted 4 adds exactly 4 points. That
makes a bonus work correctly as an ordinary component: a student who aces a
scheme whose base totals 100 and also earns a 4% bonus finishes at 104, which is
right. `_check_total` already knows this and excludes bonus weight, detected by
`bonus|extra credit` in the name or category, before flagging a total.

**A bonus never appears below the line.** It is additive, so its weight goes
into the points earned and never into the weight divided by. Dividing by
base+bonus was wrong three ways at once, all silent, all on the same outline:

- A student perfect on all seven graded components of BIOL 1103 who declined
  the 2% bonus was shown **97.09%**. Optional work nobody required cost him
  marks for not doing it.
- A student who earned it in full was shown 100 while `_evaluate_option` --
  which sums points without normalising -- said 103. Two paths, two answers,
  same student. Same class of bug as two screens disagreeing about a pass.
- An unmarked bonus row kept `remaining` above zero for ever, so
  `official_grade` stayed None and the result panel was stuck on
  **INCOMPLETE** for every student who ignored the extra credit.

`_is_bonus_row` is the row-level half of the signal `_check_total` already used
on analysis components; keep one definition, because if they disagree an
outline flagged as totalling 100% gets marked out of 103. `_course_standing`
skips bonus rows too: an unearned one dragged the planner's average down, so a
course the student was acing pulled hours, and it became the `worst`
assessment, so the plan told them to go and revise extra credit.

Three front ends do this arithmetic as well and all three had to be taught the
same rule -- `grade_calculator.html` and `public_analyzer.html` each carry an
`isBonus` helper mirroring the server's, and `course_workspace.html` needed
nothing because it asks the server. A page that says 97.09% beside a result
panel saying 100% is worse than both being wrong.

So a bonus must be RECORDED, never dropped. Dropping one silently under-reports
every student who earned it. Rule 3 briefly said to ignore bonuses, which was
harmless only while the rules never reached the pipeline; delivering them made
the app start discarding earned marks. Record any bonus whose amount the outline
states, wherever it is stated; only a bonus with no stated amount belongs in
`rules` instead.

## Which number is the final-exam minimum

`_local_outline_analysis` reads component minimums with three regexes per
component, in both word orders. The gap between a threshold and the word
"final" is `NO_PCT` -- a run of characters that contains no percentage --
because a plain `[^.
]{0,90}?` gap matched from the FIRST "at least" in the
sentence and skipped over the second: "at least 50% overall AND at least 40%
on the final examination" became a **50% final minimum**, and a student at 45%
on the final would have been told they failed. Found in the browser while
pasting an outline; the parser is the fast path for a clean table and the
fallback when Gemini is unavailable, so it is not an edge case.
`tests/test_final_minimum_phrasing.py` pins six orderings of that sentence.

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

## Every date is the student's day, not UTC

`User.timezone` holds an IANA name the browser reports once; `_user_zone` falls
back to `DEFAULT_TIMEZONE` and then UTC. `local_date_iso(value, zone)` turns a
source timestamp into the calendar day it falls on **for that student**.

This was missing entirely, and it is the worst bug this project has had. Dates
were taken in UTC, and a Brightspace deadline of **11:59 PM Eastern** is
`03:59Z the next day` -- so the commonest deadline time in a university was
shown a day late. Not blank, not obviously broken: confidently wrong, on the
screen a student checks to decide what to do tonight.

**A 10:00 PM Brightspace deadline was landing on the next day.** Reported with
screenshots: Brightspace said Tuesday Oct 6, 10:00 PM; Nexora said Wednesday
Oct 7. 22:00 Eastern is 02:00Z the following morning, and the UTC date was what
got stored. Nearly every SYSC 2004 deadline in that student's term was a day
late.

**A feed timestamp has three shapes and only one of them is UTC.**

| DTSTART | Means | Handling |
|---|---|---|
| `...T020000Z` | an instant | convert to the student's zone |
| `;TZID=America/Toronto:...` | an instant | the `ics` library resolves it; convert |
| `...T003000` (no zone) | **local wall clock** | take the day as written, never convert |
| `;VALUE=DATE:20261026` | a calendar day | never convert |

The floating case is the trap: the `ics` library parses a zone-less DTSTART and
labels it UTC, so after parsing it is indistinguishable from a real `Z`.
Converting it pulled a 00:30 deadline back to 20:30 the previous evening -- a
day early, in the opposite direction to the original bug. `_ics_floating_uids`
reads the raw text once, because that is the only place the difference
survives, and `floating=True` then means "the day is what the feed wrote".

**`due_time` is populated on every path and shown on the calendar.** A student
can compare Nexora against Brightspace at a glance -- a day that agrees but a
time that does not is a bug worth seeing, and silence is what let a whole term
of dates be wrong. `/settings` has a visible picker (`COMMON_TIMEZONES`, plus
whatever the browser reports if it is not in the list) so this is auditable
rather than magic.

Rules that keep it correct:

- **A bare date is never shifted.** `2026-09-10` is already a calendar day, and
  applying an offset it never had would move every all-day event.
- **A timestamp with no offset is read as UTC.** Brightspace and Canvas both
  send UTC; assuming local would shift a correct date the wrong way.
- **An unrecognised zone is refused, not stored.** `/api/timezone` returns 400
  and the account stays on UTC. Storing something the app cannot honour would
  date deadlines wrongly, and there is no guessing from an IP address.
- The reporter is inside `{% if current_user.is_authenticated %}` -- an
  anonymous page has no account to store it on and would only 401.

Existing rows keep whatever date they were imported with until the next sync
re-writes them. That is acceptable because a sync fixes them; a migration
cannot, since the original timestamp was never stored.

## What day it is NOW is also the student's, not the server's

`local_date_iso` fixed what day a deadline falls ON. Nothing fixed what day it
IS, so `days_until` kept measuring from `date.today()` -- the server's date,
UTC on Render. For a student in Eastern that is already tomorrow from 8pm,
which is exactly when someone opens Nexora to decide what to do tonight:

- work due **at 11:59 tonight** computed to `-1` and rendered **Overdue**
- "due in 2 days" read as tomorrow, every evening, on eleven call sites

`_today_for_request()` asks the signed-in student and falls back to the
server's date outside a request -- which is what the `.ics` feed and the public
reader want, since neither has a student to ask. `days_until(iso, today=None)`
takes an explicit day for callers that already know it.

**A test for this cannot be written against "now".** The two clocks agree for
most of the day and on any machine whose zone matches the server's, so a test
that compares them passes by accident until the evening. `test_a_countdown_runs_-
on_the_students_clock_not_the_servers` uses Pacific/Kiritimati (UTC+14) and
Pacific/Niue (UTC-11): 25 hours apart, so they are on different calendar days
at every instant of the year.

## A feed that cannot succeed must back off

`should_auto_sync` read `last_sync`, and `save_sync_result` only advances that
when the sync WORKS. So a feed answering 403 -- which is what an expired
Brightspace token does -- retried on **every page load, for ever**: every
dashboard render waited on a network call that could not succeed, and the
university was hit once per page view. Seen in the local log as seven failures
in two minutes of ordinary browsing.

`User.last_sync_attempt` is the last TRY. It is stamped and committed *before*
the attempt, because a timeout may never reach the `except`. `last_sync` still
means what it says, so a failure can never be rendered as a successful sync.

Backing off is not giving up: once the interval passes it tries again, so a
student who repairs the link in Brightspace is picked up without touching
anything in Nexora.

The comparison is `datetime.utcnow()`, not `datetime.now()`. `last_sync` is
written with `utcnow`, and comparing it against local time is harmless only on
a server that is already UTC -- which is why it survived. The same mistake was
found and fixed in the writer (`save_sync_result`) and left in the reader.

## The dashboard never waits for the university

`home()` called `auto_sync_if_needed()` before rendering, so once every
`AUTO_SYNC_MINUTES` (30) the page fetched the `.ics` from Carleton, parsed it
and imported every event **before sending a single byte**. Measured here:

| | |
|---|---|
| `/dashboard`, no sync due | 0.39 / 0.48 / 0.40s |
| the sync it ran inline | **12.13 / 11.61s** |

So twenty-nine minutes of loads were instant and one took twelve seconds.
Reported as *"when i click open dashboard it takes SO LONG to load and open"* --
and the intermittency is the tell, not a detail. This is the shape of fix
CLAUDE.md already prescribes for outline analysis: run it outside the request.

`home()` now computes `sync_due` and the page asks for `POST /api/sync/auto`
once it has painted. **The flag decides whether to ASK; the endpoint decides
whether to DO.** They are separate because the page may be an hour old by the
time the fetch lands, and three open tabs each render with the flag set --
`auto_sync_if_needed` re-checks `should_auto_sync` and stamps
`last_sync_attempt` before the attempt, so the first request claims the slot
and the rest return `ran: false` without touching the university. A stale page
cannot force a sync.

POST, not GET, for the reason `/api/tour` is: a prefetch must not spend it.

**`ran` and `ok` are separate.** It returned a bare `None` for both "nothing
was due" and "it tried and failed", which is the `_evaluate_pass_rules`
mistake -- an answer that could not be given and a bad answer are not the same
fact. Found while measuring in the browser: a feed pointed at an unreachable
address answered `ran: false` in 0.32 seconds, which reads as "not due" and was
really the connection failing. A student whose Brightspace token has expired
has to be distinguishable from one who synced ten minutes ago, and this is the
only place that knows.

A sync that changed nothing says nothing; one that did offers a reload rather
than taking one, because a page that reorders itself under someone already
reading it is its own bug.

## A deadline links to the work, and only ever to a link it was given

`Task.source_link`, filled by all three importers and rendered on the
dashboard, the calendar editor and the study plan. The URL was already being
scraped and thrown away: `extension/background.js` builds D2L's own
`folder_submit_files.d2l?db=...&ou=...` for every assignment and
`quiz_summary.d2l?qi=...&ou=...` for every quiz, Canvas hands over `html_url`,
and a Brightspace ICS feed may carry a `URL:` property (the `ics` library
parses it into `event.url`). `_import_deadlines` read none of them because
`Task` had no column.

**`_lms_link(url)` is the single gate, and it is a security boundary.** Every
one of these arrives as scraped or fed text, so none may reach an `href`
unchecked:

- Only http(s). A `javascript:` URL rendered into an anchor is cross-site
  scripting on our own dashboard, delivered by the university page we scraped.
- Userinfo is refused. `https://brightspace.example.ca@evil.tld/` reads as the
  university and resolves to evil.tld -- the whole phishing trick.
- No host, no link. A relative or malformed URL is dropped rather than rendered
  as a link to nowhere.

**Nexora never constructs one.** If the source gave no link the answer is `""`
and the UI shows no button. A link that 404s or opens the wrong quiz is the
same class of failure as a wrong date: confidently wrong on the screen a
student trusts, and worse than an honest absence.

The link is deliberately **not** in `source_hash`, so an otherwise unchanged
deadline picks one up on the first sync after this shipped -- which is every
row that already exists. Both importers assign it outside their `changed`
check for the same reason, and the counters stay honest about what really
changed.

`_sessions_json(rows)` resolves a whole week's links in one query;
`_session_json` alone would be N+1 against a twenty-block week. The plan block
reaches the work through `StudySession.task_id`, so no column was needed there.

No extension release is required: `source_url` and `due_at` already travel on
the same item, in both `background.js` and `content.js`.

## Three D2L URLs for one lecture, and only one of them saves it

Reported three times, wrong for a different reason each time, and the third
reason was that nobody had asked D2L what it was offering.

| URL | As a fetch | As a link |
|---|---|---|
| `/d2l/api/le/<v>/<ou>/content/topics/<id>/file` | 200, `attachment` | **`{ Errors: [ {Message: "Forbidden"} ] }`** on a blank page |
| `topic.Url`, e.g. `/content/enforced/<ou>-<n>.<term>/Lecture_3.pdf` | 200, no disposition | opens the browser's PDF **viewer** |
| `/d2l/le/content/<ou>/topics/files/download/<id>/DirectFileTopicDownload` | 200, `attachment` | **saves the file** |

Measured against a real Carleton lecture (ELEC2501B, topic 4894237) before any
of it was written down. The first round fixed the Forbidden page by rewriting
the API URL into D2L's **viewer** route (`_browsable_d2l`) -- which opens, so
the error went away and the lecture still did not download. The owner reported
it as still broken, correctly.

**The TOC was already handing over everything needed.** Each topic carries
`TypeIdentifier: "File"` and a real `Url`; what threw it away was
`source_url: file.download_url || file.source_url`, which preferred the API
endpoint for **every topic that had an id**. So a stored `/file` URL says
nothing about whether the item is a file, which is exactly why the server
cannot repair old rows: `CourseContent` has no `file_ext` or `download_url`
column -- the extension sends both and `_import_content` drops them -- so for a
stale row it cannot tell a PDF from an HTML page. `_browsable_d2l` therefore
still points at the viewer, which works for every topic type. **Do not make it
guess**; a download link that 404s on a discussion board is worse than a viewer
link that opens.

`fileInfoFromTopic` builds the download route **only** when D2L itself says
`TypeIdentifier` is `File`, and `classify_url` carries the extension-bearing URL
alongside it, because `classifyContent` reads the file extension off the link
and the download route contains no `.pdf` -- handing it the download URL files
every lecture as a `page`, which hides it rather than losing it.

**An empty href is not a link.** `new URL('', base)` resolves to the base and
that string is truthy, so a topic with no `Url` of its own linked to
Brightspace's front page from the worker, and to whatever page the scraper
happened to be on from the content script. Found by *running*
`fileInfoFromTopic` against a topic with no `Url`, not by reading it -- which is
the point of `extension/topicfile-check.html`, where the real function runs
against the real TOC JSON Brightspace returned.

This needs an extension release: the link is built during the scrape, so
**existing rows keep the link they were imported with until the next sync
rewrites them.** Same shape as the deadline-date fix -- a sync repairs them, a
migration cannot.

## The server CAN turn a lecture link into a download, and here is what it took

Reported a fourth time: *"same issue when i click it doesnt download, it just
redirects"*. Measured on the live site before touching anything -- all 36 links
on one course workspace were D2L `viewContent` URLs, which is exactly what
`_browsable_d2l` renders a stale row as. It opens, which is why the Forbidden
page went away and the lecture still did not download.

The section above concluded the server could never repair a stale row, because
`CourseContent` could not tell a PDF from a web page. **That was true and the
fix was to make it able to.**

Probed against the student's own Brightspace, with their session:

| D2L `TypeIdentifier` | `DirectFileTopicDownload` answers |
|---|---|
| `File` | 200, `application/pdf`, `attachment` |
| `Link` | **403** |
| `ContentService` | **403** |

So it can never be applied blindly -- on that one course it would have broken 10
of 36 links. `content_type` is no help either: measured against D2L's truth,
`lecture` covered 18 Files, 11 Links and 3 ContentService topics.

**`file_ext` is what separates them, and the extension has always sent it.**
`_import_content` dropped it, exactly as it once dropped `due_at`. Replicating
the published build's own `file_ext` logic and scoring it against D2L's
`TypeIdentifier` across **ten** of that student's real courses, 210 topics:

- **83** File topics correctly turned into downloads
- **0** non-File topics wrongly turned into downloads
- 127 File topics left alone, **100 of them `.html`** -- course pages, which a
  student clicking wants to read

Zero false positives is the property that matters, because the failure mode is
a 403 and a link that 403s is worse than one that opens. `_DOWNLOADABLE_EXT` is
documents only; `html` and video are excluded deliberately.

`content_href(url, file_ext)` runs the security gate FIRST and the rewrite over
what the gate allowed, so a scraped `javascript:` URL can never reach it. A row
with no extension, an unknown one, or a URL that is not a topic view is returned
untouched -- this can only ever ADD a download.

**The point of all this: it needs no extension release.** `file_ext` is on the
payload of the build already published on the Web Store, so one ordinary
re-import fixes every lecture. The 3.5.3 extension change is still right and
still worth shipping, but it is no longer what the fix depends on.

## A new column needs a line in ensure_schema_columns, or the deploy 500s

`db.create_all()` creates missing TABLES and **never adds a column to a table
that already exists**. `ensure_schema_columns()` is the migration: an explicit
`{table: {column: definition}}` map, run at startup, that `ALTER TABLE ... ADD
COLUMN`s anything missing.

`CourseContent.file_ext` was added to the model and not to that map. Caught
while checking the deploy rather than after it, and the consequence would have
been immediate and total: the model would reference a column Postgres does not
have, so **every** query against `CourseContent` raises `UndefinedColumn` -- the
course workspace, the dashboard's Recent updates, the study tools -- on a push
that otherwise looked like a safe read-only change. SQLite would have carried
on locally, which is exactly how it reaches production unnoticed.

The check that proves it is a simulation, not a reading: build an OLD-shaped
table in a throwaway SQLite file, point `DATABASE_URL` at it by **absolute**
path, import `app`, and look at the columns afterwards.

    file_ext added to the EXISTING table: True
    the row that was already there survives: True | file_ext = ''
    course_colour created alongside: True

An absolute path matters. A first attempt used `sqlite:///uni_helper.db`, which
Flask resolves against the **instance folder** -- so the app opened the repo's
own dev database instead of the fixture, found the column already there from
earlier local runs, and reported success while testing nothing.

A new TABLE (`CourseColour`) needs no entry: `create_all()` handles those. It is
only new columns on existing tables.

## A fixture dated in the past is a test with a timer on it

Four files in one week failed for reasons that had nothing to do with what they
tested, each one on a different day:

| File | Dated | Went off |
|---|---|---|
| `test_timezone_repair` | 7 Oct | 14 Oct |
| `test_deadline_links` | 7 Oct | 14 Oct |
| `test_all_day_events` | 7 Oct | 16 Oct |
| `test_calendar_feed` | 15 Oct | one day away when caught |
| `test_calendar_events` | 9 Oct | 17 Oct, mid-way through unrelated work |

Nexora deliberately stops carrying work far enough in the past -- the calendar
drops `dleft < -7`, the importer declines old events, the feed leaves them out.
So a fixture written "today" quietly stops being rendered, created or exported
once the real date moves, and the assertion fails somewhere unrelated to the
feature.

Every one of them is now anchored to `date.today()`: a `_SHIFT` from the day the
fixture was written, or `_SOON` for something that must be ahead. **What those
tests pin is a relationship** -- that `VALUE=DATE` is never shifted while a real
timestamp is, that a `URL:` property survives an import, that an all-day entry
gets an exclusive `DTEND`. Which week it falls in was never part of it.

Same lesson as the model table and the `15 requests` rate-limit test: pin
invariants, never measurements -- and a date written into a fixture is a
measurement of when it was written.

## A stored timestamp is UTC; a displayed one is the student's clock

`local_stamp(value, fmt, user)`, registered as the Jinja filter
`|local_stamp`. Every datetime column in this app holds UTC, and four
templates were calling `.strftime()` straight on one -- so a sync that
happened at **10:04 PM in Ottawa displayed as 2:04 AM**, beside a line
claiming it was the last sync. Reported as "THE TIMEZONE IS SO WRONG".

Exactly the deadline-date bug in a place nobody thought to look: a value that
has to be derived for the student was never derived. **Never call `.strftime()`
on a model datetime in a template.** `save_sync_result` also stored
`datetime.now()` while every other writer stored `datetime.utcnow()`; on Render
both are UTC so it looked fine, and locally it was silently four hours out.

## A course with work in it is never "Other courses"

`_term_from_dates(dates)`. `_term_meta` reads a term from what Brightspace
stated, and Brightspace does not always state one -- a student who connected
only the calendar feed has deadlines and no course rows at all. Those courses
fell into the "Other courses" group, and because the dashboard **opens on the
current term**, the five courses a student was actually taking were the ones
hidden behind the picker while finished enrolments filled the screen.

Their own deadlines answer it: the same season rule as `_current_term_label`,
over dates already held. Most common wins; a tie goes to the later term,
because a course straddling December and January is the one starting. With no
dates it returns `""` -- inventing a term is worse than admitting to none --
and **a stated term always outranks the guess**, since a course can carry work
outside its own term.

This is grouping only. It never edits `SyncedCourse.term`, so the next sync
still gets the last word.

## The import scope is remembered, and a stale one hides a whole term

The extension stores `importScope` / `importTerm`. With scope `term`,
`content.js` filters enrolments to that exact label
(`fromApi.filter(c => c.term === term)`), so a term named in a previous
semester keeps being the only one imported -- the student's current courses
never arrive, and nothing says so. Seen live: 109/190/187 items across an old
Winter and Summer, and **no course rows at all** for the term in progress,
whose deadlines had come from the calendar feed instead.

The popup does re-ask on every visit to Brightspace (that was fixed in 3.4),
so the recovery is one click -- pick "every term", or name the current one --
but the failure is silent until someone notices their courses are unnamed.

## A scraped due date is worthless until something consumes it

`_import_deadlines(user, items)` turns the extension's `due_at` into real
deadlines. Before it existed, the extension read D2L's own `DueDate` for every
assignment and quiz, sent it, and the server **dropped it**: `CourseContent`
has no date column, and nothing else looked at the field.

So the only source of deadlines was the Brightspace *calendar feed* URL, a
separate step on the connect page that is easy to skip. A student who did
exactly what the page told them -- install the extension, press Import my
courses -- got a calendar with **no assignments or quizzes in it at all**, and
nothing said so. An empty calendar looks like a calm week.

It deliberately mirrors the feed importer rather than inventing a second
scheme: the same `group_id`, so an assignment arriving from both the feed and
the extension collapses into one card, and the same
`sync_state == 'dismissed'` check, so deleting something the LMS gave you is
not undone on the next sync. Its `source_uid` is prefixed `ext:` so the two
sources update their own row instead of fighting over one.

**It runs in its own pass, before the content loop.** That loop
short-circuits an item whose hash has not changed, so a deadline attached to
already-imported content would never be created retroactively -- which is every
student who synced before this shipped.

## A final is not a test with a different word on it

A term has two or three, they are worth more than everything else put together,
and in December the month is nothing but finals -- so inside the set already
marked `major` they have to be the ones that read loudest. `data-major` is on
the card now (the badge text was always rendered; the CSS had nothing to hook
on), `.ev[data-major="FINAL"]` carries a heavier border, fill and title, and the
day cell gets `hasfinal` on top of `hasmajor` so a December grid reads as "these
four squares" before anything is opened.

Everything that changes is WEIGHT, never hue. The course colour is the one thing
the legend promises, and a final that stopped being its course's colour would
break that promise to make a different point.

**A university's exam schedule names no activity.** Carleton Central prints the
course, the date, the start time and the room -- so the prompt's default
("title it what the row calls the activity") titled every row with a course
code, and `_major_assessment_label` saw no word meaning an exam. The FINAL
badge, the outlined day and the planner's revision ladder all hang off that word
being present, so the image prompt now recognises the shape and titles those
rows "Final Exam". Measured on the live key against a schedule in that layout:
five of five rows FINAL, right dates, right start times, rooms into `location`,
and every course code resolved to the student's own.

## The student picks the colours

`CourseColour` holds an INDEX into the existing ramp, never a hex value, so no
course can be made illegible against the dark theme -- and it is per ACCOUNT,
because the colour map is how a month is read at a glance and it has to be the
same on a phone.

**The override is applied inside `_student_course_colours`**, not at any call
site. That function exists because the calendar and the study plan once built
their own maps and disagreed about what colour SYSC2004 was; an override
honoured on one screen and not the other would be that same bug, reintroduced
by the feature meant to fix it. Verified by reading the study plan after
recolouring from the calendar.

The dot in the legend is its own control and the name beside it keeps hiding the
course, because they answer different questions: the colour is a setting kept on
the account, hiding is a glance forgotten on reload. They are siblings in a
`.lgwrap` rather than nested, since a button inside a button is not valid.

Two things found by running it:

- **`--c0..--c7` live on `.cal`, not on `:root`.** The palette popover is
  appended to the body so it can escape the grid's overflow, so
  `background: var(--c3)` resolved to nothing and it opened as eight
  transparent circles. It reads the values off `.cal` with
  `getPropertyValue` -- resolving from the one definition rather than repeating
  the ramp, which is the drift this codebase keeps paying for.
- **An empty course string is not nothing.** `_normalize_course_code("")`
  returns `"OTHER"`, which is a real bucket in the legend, so the route checks
  the raw string before normalising or an empty field quietly recolours it.

## A filter must never swallow what was just added

Reported as **"i cant even add manually now"**. It added perfectly, every time.
The exams-only filter survives a reload in `sessionStorage`, saving an event
reloads the month, and anything that is not an exam came back `display:none` --
on the server, in the DOM, and invisible. From where the student is sitting that
is the Save button doing nothing.

It hits the AI paths harder: a schedule import adds Labs as `event`, so half of
what the preview had just promised would vanish on apply.

`unhideForKinds(kinds)` runs before both reloads and clears the filter **only**
when the new work would actually be hidden by it -- the filter is a deliberate
choice, and overriding it for a row it would have shown anyway is Nexora
deciding for the student. It is cleared with a sentence, never silently.

**The sentence was lost the first time, to a temporal dead zone.** `showToast`
is a hoisted function declaration, so it is callable early in the script -- but
its body touches `undo` and `undoTimer`, which are `let` several hundred lines
below. Calling it from the top threw "Cannot access 'undo' before
initialization", the `catch` swallowed it, and the note had already been removed
from storage: the filter cleared correctly and the explanation never appeared.
The block is deferred with `setTimeout(..., 0)` and removes the note only after
the toast has actually been shown.

## A week is not a day, and which day it is is arithmetic

A great many course schedules are printed by WEEK -- "Week of 19 October" --
because the lab or tutorial falls on whichever day that student is enrolled for.
The instruction then says which: *"my day in the week is Tuesday so if it says
week 19th october then my test is tuesday of that week"*.

Measured: asked exactly that, the model answered **2026-10-21 (a Wednesday)**
from a table and **2026-10-20 (correct)** from the same facts written as prose.
Two answers to one question, one of them a day out, on the screen a student
checks to decide what to do.

So the model reports and the server counts, the same split as the year.
`names_a_week` says the row gives a week rather than a day, `weekday` carries
the day the instruction asked for, and `_day_in_stated_week` does the
arithmetic -- anchored on the week's **Monday**, so "week of Sunday 18 October"
and "week of Monday 19 October" mean the same week, which is what they mean to
the student reading the schedule. It runs after `_anchor_undated_year`, because
a week is usually printed without a year. Three runs after the change: every
date a Tuesday, all three identical.

## The AI allowance, and what it is allowed to count

`AI_LIMIT` was `"40 per day; 12 per hour"` -- plenty for a student using a
feature that works, and nowhere near enough for one troubleshooting a feature
that does not. Twelve attempts is about fifteen minutes of trying different
wordings, and the reward for persevering was being locked out. It is
`"150 per day; 40 per hour"` now and reads from the environment, so the ceiling
moves without a deploy. Every route goes through `ai_limit` rather than fifteen
hand-written decorators.

**Not charging for failed calls was tried and backed out.** It is an appealing
idea -- a call that gave the student nothing should not spend their allowance --
and it is wrong here: a vision call that reads an image and then answers 422
"nothing dated in that picture" has ALREADY spent the Gemini quota the limit
exists to protect. Exempting it puts a hole in the guarantee rather than a
kindness in the budget. Two existing tests caught it by hammering a route with
failing requests and never being stopped. The reported lock-out was the ceiling
being too low, not what it counted.

**The 429 said "12 per 1 hour This limit protects the shared AI quota every
student's outline analysis depends on."** Flask-Limiter's `description` is the
raw rule, glued straight onto the sentence after it, and it reads like a stack
trace. `handle_rate_limited` parses it into "That is the limit of 12 per hour
for this feature."

Raising the limit also broke a test that sent **15** requests and expected a
429 -- it only ever worked because the hourly limit happened to be 12. It
derives the count from `AI_LIMIT` now. Pin invariants, never measurements;
this is the same lesson the model table taught.

## The calendar's model tier was measured, not argued

Asked why the calendar was "not using proper Gemini who can think". The honest
answer, with numbers, is that **the model tier is not the bottleneck** -- and the
measurement was run specifically because the opposite looked obvious.

What the two routes actually use: the assistant `mode="quick"` (3.6-flash,
thinkingLevel low), the image reader `mode="fast"` (flash-lite, no thinking).
The image route being on `fast` genuinely was a mistake in the making: it was
set when the route only TRANSCRIBED a table, which is mechanical, and the
instruction-following added later ("treat every Tutorial as a Test for elec") is
reasoning. Raising it looked obviously right.

Measured **2026-10-12**, live key, three passes each. OCR runs once and the
transcript is reused, so only the step that applies the instruction is timed:

| mode | time | tutorials renamed | labs kept | kind | course |
|---|---|---|---|---|---|
| `fast` (flash-lite) | 3.1 / 3.4 / 3.4s | 5/5 | 5 | exam | ELEC2501B |
| `quick` (3.6-flash, low) | 21.0 / 3.3 / 2.9s | 5/5 | 5 | exam | ELEC2501B |
| reasoning chain | 58.2 / 36.3 / 17.8s | 5/5 | 5 | exam | ELEC2501B |

Identical answers. Reasoning costs 5-18x the latency for nothing. The same held
on the assistant: the reported request was right 3/3 at every tier, ~1s on fast
and 22-45s on the reasoning chain.

**So the tier stays where it is.** Raising it would have spent quota and made
every calendar edit twenty seconds slower while changing no answer. A paid key,
not a bigger model, is still the lever CLAUDE.md names.

Two things the probe got wrong first, both worth knowing:

- **It measured Nexora's own rate limits and read them as model failures.**
  `AI_LIMIT` answered 429 for the slower tiers, and `/login`'s tighter limit
  then answered 302 for everything after. Set `limiter.enabled = False` in any
  probe that hammers a route.
- **A scorer can fail a correct answer.** "Push every ELEC2501B test back by 7
  days" was marked BAD at every tier, because the fixture had no test on that
  course -- matching nothing was right. Check what the app was asked before
  believing it got it wrong.

That second one found the real defect. `_calendar_match` returning no rows hit a
bare `continue`, so the reply promised both halves of a two-part request,
delivered one, and left `notes` empty -- correct behaviour, completely
invisible, and indistinguishable from the app ignoring you. `_match_description`
now says what was looked for and not found.

## An entry on OTHER is invisible, and the assistant could not fix it

Reported with a screenshot: tests read out of a schedule landed under **OTHER**,
and asking the assistant *"change the course names for Test 2,3,4 and 5 from
OTHER to ELEC2501"* produced the sentence *"I will update the course for Test 2,
Test 3, Test 4, and Test 5 from OTHER to ELEC2501"* directly above *"Nexora found
nothing to change"*. A confident promise and a flat contradiction, on one screen.
Read as the AI not thinking; the model had understood perfectly.

`_calendar_plan` accepted add, move, delete, retime and rename. **There was no
verb for it**, and an op the server did not recognise hit a bare `continue` -- no
note, no error. The missing feature and the silence are two separate bugs and
the silence is the worse one: an operation this server cannot carry out now
produces a note naming it and listing what it can do.

`recourse` is that verb. Three things it has to get right:

- **The code is resolved against the student's own.** `_resolve_known_course`:
  someone typing "ELEC2501" whose course is ELEC2501B gets ELEC2501B. Storing
  the string as typed moves the entry off OTHER onto a code that still matches
  no course they have -- so the outline, the grades and the planner go on
  ignoring it, and the only thing that changed is the label. That reads as
  fixed and is not. A code that uniquely extends, or is uniquely extended by,
  one of theirs wins; two offerings that both match (ELEC2501A and ELEC2501B)
  leave the literal alone rather than picking a section for them.
- **The whole group moves.** `group_id` is derived from the course, so an
  imported assignment's three rows would otherwise split across two courses and
  the calendar would draw it twice.
- **The destination is re-resolved on apply**, not trusted: the plan makes a
  round trip through the browser and this writes the key every other feature
  joins on.

**"Not given" and "literally OTHER" were the same filter**, which is why the
entries could not even be selected. `_normalize_course_code("")` returns
`"OTHER"`, so the guard that stopped an unset field filtering everything also
threw away a deliberate `course: "OTHER"` -- and OTHER is the one course a
student ever asks about by name, because it is where every entry lands that
nothing named a course for. `_calendar_match` now tests the raw string first.

**The add label never said which course.** The image path named it; the
assistant's own add path did not, so a series of tests was confirmed and only
found to be on OTHER afterwards. The course is part of what is being agreed to.

The image route resolves through `_resolve_known_course` too. Leaving the match
to the prompt gave back "ELEC" from "for elec", which is the same dead end as
OTHER wearing a better label.

## A picture and a sentence are one request

`/api/calendar/from-image` takes an `instruction` beside the image, and the
calendar assistant's sheet is where both are typed -- one box, not two flows.

The table that prompted it is a weekly lab/tutorial schedule, and what was
wanted from it was *"for everything thats written as tutorial here, treat it as
Test, for example Tutorial 2 would be Test 2 for elec"*. The word **Test**
appears nowhere on that image, and neither does the course. An image reader with
no instruction can only ever hand back what the table already says, which the
student is looking straight at -- so the route read the picture, titled
everything "Exam", and was useless for the commonest schedule a student owns.

The split is the one the rest of the calendar uses: the model decides what each
row MEANS, the server decides every date, and nothing is written until the
preview is confirmed. `understood` comes back and is shown, because a misread
instruction reshapes a whole term and would look completely authoritative doing
it -- the same reason `/api/study-plan/ask` returns it.

**The year is never the model's to choose.** A course schedule is written for
people who already know which term it is, so it prints `07-Sep` and nothing
else, and the year is then the one thing on the row nobody wrote down. This is
exactly what got the old "exams found in your course material" panel deleted: it
offered a first-year MATH 1004 test as "Tue, Sep 29" this September. The model
returns `year_printed`, and when it is false `_anchor_undated_year` takes the
**nearest occurrence to the student's own today** -- right in both directions for
a term read during that term, and at worst six months out where a guess is a
whole year out. Where it cannot anchor at all (29 February, with no leap year
within reach) it returns None and the model's date stands, because a row
vanishing is worse than a year that may be off.

**The course is as often named in the instruction as in the table**, and named
loosely: "for elec" has to resolve to ELEC2501B against the student's own codes,
or the entries land under a code that joins to no outline, no grades and no study
plan. Measured on the live key: "for sysc 2310" resolved to SYSC2310, and "skip
the labs" dropped them.

**Every preview row carries a tick, and the past ones start off.** A term
schedule always holds weeks that have already happened -- the reported table runs
from 07-Sep and half of it had gone. "Add all 14" is not an answer to that, and
dropping them silently is worse, because the student can see them in their own
picture. They are listed, marked `past`, and unticked. Apply sends only what is
ticked; the server re-checks every change regardless, so the ticks decide what is
sent and never what is allowed.

Two things found by running it rather than reading it:

- **`.sheet input` is the full-width form field for that dialog**, and a
  checkbox is an input -- width, padding, border and surface all applied, so the
  tick rendered as a 355x344 grey panel covering the entire preview. Every one
  of those has to be undone by hand on `.pick`.
- **A second `const AI_VERB` killed the whole page.** It was moved up so the
  shared preview renderer could use it and the original was left in place;
  `Uncaught SyntaxError: Identifier 'AI_VERB' has already been declared` takes
  out every handler on the calendar, not just the new one. Read the console
  after touching this file -- the page looks completely normal until clicked.

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

**The "exams found in your course material" panel is gone from the calendar.**
It read dates out of announcements with a regular expression and offered them
for the calendar. Once the extension imported every term, it read every old
announcement too, and the dates it finds carry no year -- so a MATH 1004 test
from first year was offered as "Tue, Sep 29" this September, ten of them at
the top of the page, above the calendar itself. The owner called it useless
and asked for it removed, and he was right: a suggested date that is wrong is
the same class of thing as a stored date that is wrong, just one click
further away. The deterministic helpers (`_find_date`, `_find_time`,
`_find_location`, `_looks_like_an_exam`, `_NOT_AN_EXAM`) and the two routes
(`/api/exams/suggestions`, `/api/exams/accept`) remain, tested, called by
nothing. If it ever comes back it has to be current-term only, future-dated
only, and year-aware -- and probably not at all.

**The `.ics` feed is how Google Calendar connects**, because Google cannot sign
in to Nexora. **It is offered on the calendar page itself** -- a Sync button
beside "+ Add event" whose sheet carries Google's own subscribe link
(`calendar.google.com/calendar/r?cid=webcal://...`), a `webcal://` link for
Apple and Outlook, and the address. For a long time the only way to it was a
six-step copy-and-paste on Settings, and nobody found it. Visiting the
calendar never mints a token -- the address is a credential -- so the page
ships an existing one in and creates it on the first press of Sync through
the same `/api/calendar/feed` route Settings uses. The sheet says Google
refreshes on its own schedule, up to once a day, because "auto sync" on
Google's side means that and not live; the feed already sends
`REFRESH-INTERVAL` as a hint. Google will only fetch an https feed, which
production is and localhost is not. The token in the path is the entire credential, so it is
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

**An outline pasted or uploaded WITH a course selected takes two calls:**
store it (`/outline/upload`, which accepts files or JSON `{text}`), then
analyze the stored copy (`/outline/analyze` with the returned `source_uid`),
then re-read the grade sheet -- the same two the course workspace makes.
Neither intake path on this page worked once a course was picked: an upload
made only the first call, so nothing was analyzed under a message saying it
had been, and pasted text went to the generic analyzer that keeps nothing, so
the components appeared and were then wiped by re-reading the empty saved copy.
Without a course the page is a demonstration and the generic analyzer is right.

**The policy is confirmable here.** After analysis `confirmed` is False and
`grade-outcomes` answers 409; the official panel now renders a Confirm button
on that status rather than telling the student to go to another page with no
link to it. This page is the one place for grades, so the one button that
unlocks the result has to be on it.

**The page shows only what is actionable.** With no outline on file it is
the course picker and the intake, nothing else; it used to show an official
result of nothing, pass rules for nothing, an empty results card, a target box
saying "add assessments first" and the Ask panel, and the app's owner said he
was lost. `applyStage()` is the one place that decides. Once an outline is on
file the intake folds to a line ("outline is on file" + "Replace the outline")
and the results lead. "No outline? Add the assessments by hand" keeps the
manual path reachable.

**An analysis message renders beside the buttons that caused it**
(`intakeStatus` -> `#analysisMeta`), never in the alert at the foot of Step
Three. The grading-scale error used to land three cards below the click, so he
uploaded four times and reported the button as broken.

**Analysis does not require a saved grading scale.** `/outline/analyze`
answered 428 until Settings had a scale -- every new account. The scale only
turns a LETTER rule into a number, and `_letter_requirement_result` already
returns `passed: None` when it cannot, so the analysis runs with the scale on
file (the default when none is saved) and returns `scale_note` when the outline
actually has a letter rule. The course workspace carried a client-side copy of
the gate; both are gone.

**A signed letter never matched.** The fallback's letter-rule regex was
`\b([ABCDF][+-]?)\b`, and a word boundary cannot sit between "-" and a space,
so "C- or better" was read as no rule at all -- the sign being the whole
difference between C- and C. `LETTER` uses lookarounds, and reads both word
orders ("required ... C- or better" and "C- or better is required").

**The standing panel defers to the official result.** It computed its own
letter and its own pass/fail colour from the number alone, so the page showed
**FAIL · F** in the official panel and **D-** in the standing panel below it --
same student, same 51.24% -- because only the server knows that a 49 on the
lecture component fails the course. `calculate()` takes the letter and the
verdict from the last official result whenever one has a grade, names the
failed rule in the sub-line, and `refreshOfficial` re-runs it. During the
one-second save debounce the letter holds the last official verdict rather
than flashing a number-only guess; the percent updates instantly.

**Marks post as `"16/20"`, not as `16` with a separate maximum.** A bare number
is read as a percentage by `_parse_mark_entry` -- correct for the single-field
entry it was built for, wrong for the two boxes here, where 4 out of 20 was
stored as 4%.

**"Final Lab Exam" is not the final exam.** `\bfinal\b` matches it, and BIOL 1103
has both. Every matching row was excluded from the term-work total and the last
one won the `final_row` slot, so the required-final solver worked against the
12% lab exam while treating the 20% final as unmarked and assuming 100% on it.
**`_final_name_score(name)` is the single definition**, and `_pick_final_name`
picks the best of a set of `(name, weight)` pairs: exact "Final Exam" beats
"final exam" without "lab", beats "final" without "lab", beats anything else
containing "final", with ties going to the heavier component. It never guesses
— with no candidate the caller simply offers no required-final figure. A course
with a lecture final and a lab final is ordinary; only one of them is what
"what do I need on the final" means.

Five places asked that question and every one of them asked it loosely:

| Site | What it got wrong |
|---|---|
| `_compute_official_result`, single scheme | Excluded both from term work, and `final_row` ended as whichever matched last |
| `_evaluate_option` | Applied one hypothetical mark to two components at once |
| the same function's caller | `'final exam' not in m.lower()` also swallowed a missing **Final Lab Exam**, so it solved targets that were not reachable |
| `_evaluate_pass_rules`, `component_minimum` | Answered a rule *about the lab exam* with the final's hypothetical mark |
| `_group_state` | An aggregate holding both finals got two overridden members |

`_evaluate_option` now returns `final_source`, so the caller filters on the
component that was actually overridden instead of sniffing message text.
`_pick_final_row` is identity-based, and `_component_source_name` is the one
place that normalises `source`/`name`/`component`/`label`.

`tests/test_two_finals.py` pins each site, and all five of its behavioural
tests were confirmed to fail against the old matching before the fix landed —
a test that passes either way pins nothing. The corpus case is
`two_finals_two_schemes`.

**A required mark above 100% is not advice.** The panel printed "You need
158.33% on the final exam" — the right answer to the wrong question, and the
student reported it as the page being broken. `_compute_official_result` now
also returns `max_with_perfect_final`, so both front ends can say "91% is out
of reach; even 100% on the final finishes at 79%". The grade calculator had the
mirror-image failure: with everything marked it said "nothing left to need" and
ignored the target just typed, so the box looked dead. It answers the target
now, reachable or not.

The course workspace still owns the same data and is untouched, on Bassem's
call: it is the riskiest file in the repo and this change is about where
things live, not how they are calculated. Remove its outline/grades tabs only
once he has said the new page is better.

## Adding to an outline that is already on file

`/api/course/<code>/outline/amend` and, on the tutor, "add this to the
outline". A grading policy changes after the outline is read -- a bonus
announced on Brightspace, weight moved off a cancelled test, a minimum in a
lab manual. The student pasted the announcement into the tutor, said "add it
to the course outline", and the tutor answered that it had no access to edit
the outline. That is the app refusing the one thing it exists for, and it
was reported as extremely important.

**The model reads the addition; the server changes the scheme.**
`_outline_amendment_edits` asks a fast model what the text changes, in the
correction route's own shape -- components to add, weights to change, pass
rules, sentences for `rules`, entries to replace -- and
`_apply_outline_edits` applies it. That helper is the body of
`/outline/correct`, factored out so an amendment takes exactly the road a
student's own edit does, including the part that never deletes an entered
mark. The tutor does not call the chat model for this turn at all
(`_wants_outline_amendment`, `_copilot_amend_outline`): it finds the course
(workspace prefix, a code in the message, a code earlier in the
conversation) and the text (in the message, in an attached screenshot, or a
turn earlier when the student says "add it"), amends, and replies with the
change list.

**An amount stated against part of the course is converted on the server.**
"A bonus 1% is added to your final lab grade" in a course whose lab is 40%
is 0.4% of the course. The model names what the amount applies to
(`weight_of`: a component, a derived group, or a list of the rows that make
up the part); `_scheme_part_weight` looks the weight up, group first, and
the note on the component shows the sum. The measured failure this guards
against: with only single rows offered, flash-lite picked the largest lab
row (24%) as "the lab grade" four times out of four.

**The same marks must never count twice.** The BIOL 1103 outline already
carries "Bonus Opportunities 2%"; the announcement is the detail of it. The
model may put the general entry in `remove_components` when it adds the
specific ones, and does so consistently. A removed row with a mark on it is
kept -- the calculator reads rows -- and the change list says so.

The addendum text is kept twice: on the policy (`addenda`) and appended to
the outline row, so Ask can quote it and a re-analysis still sees it. It
does not un-confirm a confirmed policy; the student asked for it.

The grade calculator has the same thing without the tutor: **Add to it**
beside **Replace the outline**. Measured on the live key: ~1.2s per read,
correct on five of five passes for the bonus text and four of four for the
demo scheme. flash-lite occasionally wraps its object in a one-element
list; `_outline_amendment_edits` unwraps that rather than reading it as
"nothing changes", which is the one wrong answer that looks like the feature
not working.

## Ask the outline a question

`/api/course/<code>/ask`, on the **course workspace and the grade calculator**.
A student types a question in their own words -- "how much do I need to pass?",
"what do I need for a B+?" -- and gets an answer grounded in their own outline.

The rule is the same one the study planner follows: **the model reads the
question, the server does the arithmetic.** `ask_course_ai` hands it the outline
text and the numbers `_compute_official_result` already produced, and forbids it
from computing anything. Every factual claim must carry a verbatim quote, and
`_verify_quote_in_source` checks each one against the real outline before it is
shown -- a citation the model could not have pulled is worse than none, because
it looks trustworthy.

**It was target-blind.** It always asked for a bare pass, so "what do I need to
get 80" was answered with the numbers for 50 -- and the model, correctly barred
from doing its own maths, presented them anyway. `_target_from_question` reads
the mark out of the sentence and hands it to `_compute_official_result`:

- an explicit percentage, but only where the sentence is **asking for** it. A
  `%` alone is not enough -- "is the final worth 40%" is about a weight, and
  "I got 80 on the midterm" is a statement, not a goal.
- a letter, matched against the student's **own** saved scale, so a university
  whose A starts at 80 is never answered with someone else's 85. Matched
  uppercase-as-written, or the "a" in "what do I need" becomes an A.
- passing, using the course's own `pass_mark` rather than a hardcoded 50.
- a number glued to letters is a course code, not a mark.

Deterministic on purpose: a model misreading "85" as "58" once in a hundred
times is a student planning a term around a number nobody checked. The response
carries `asked_about` and `asked_basis`, shown on the page, because a misread
target is the one failure here that would look completely authoritative -- the
same reason `/api/study-plan/ask` returns `understood`. It also returns the
`numbers` the answer was built from, so the sentence can be checked against the
arithmetic instead of taken on trust.

**It was also the only Gemini-backed route with no `AI_LIMIT`**, which mattered
much more once it moved onto a page a student can sit and retype questions into.

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

Sixteen outlines, each isolating a different mechanism (dropped marks,
overflow cap, excluded bonus, hurdle, aggregate minimum, alternative schemes,
gated scheme, weight transfer, underspecified, multi-option with eligibility,
two-column schemes with a bonus, a syllabus priced in points, a page where
most percentages are penalties rather than weights, and an unweighted
pass/fail hurdle, and two finals inside alternative schemes). 16/16 on the
single-call path; the pipeline path passes
every case it has been run against since the invention guard landed.

`multi_option_eligibility` is **model-dependent**: it passes on
`gemini-3.6-flash` and fails on `gemini-3.5-flash-lite` with "selection_rules
empty". That matters beyond the score -- with no selection rule and no
`source_group`, `_evaluate_option` cannot find a row for "Average of best two
written tests", marks the option incomplete and drops it, so those schemes
silently never apply. A failing corpus run on that case is a signal about which
model answered, not necessarily a regression. Add a case when a new outline shape appears; the corpus is the
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

The third instance was `underspecified`. Its scorer flagged *any* component
when it expected none, so it failed the best answer the app has ever given that
outline: four named assessments at **weight 0**, each noted "weighting to be
announced", an explicit ambiguity, and `confidence: low`. Nothing was
fabricated, and naming the assessments is more use to a student than a blank
page -- they fill the numbers in the week the instructor gives them. The
mechanism is *did it invent a weight*, so that is what the case now scores, via
`_weight_of`. It keeps its teeth: the pipeline's 25/25/20/30 are positive
weights and would still fail if `_scheme_is_uncorroborated` were removed.

The two paths give different right answers to that outline and both pass. Worth
remembering before "fixing" one to match the other.

The runner also applies `_scheme_is_uncorroborated` before scoring, because
every route does and the pipeline does it internally -- scoring the raw reply
would measure something no student is ever shown.

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

**The rail's LINKS are `_railnav.html`, once, and all three pages include it.**
That exemption turned into drift: both of those pages hand-wrote their own rail,
the workspace had lost Study plan, Canvas and Feedback, the study room had lost
Canvas and Feedback, and both called the grade calculator "Grades". A student
who clicked into a course could not reach the planner without going back to the
dashboard first. Reported as "the sidebar doesn't show everything". The include
carries no container, brand or CSS -- each page keeps its own `<nav>` and its
own styling, and only the list is shared. `rail_extra` is the trailing item the
workspace uses for the course it is on. Add a rail item in one place.

**A week is when a course happened; it does not say what a thing IS.** The
Lectures tab was reported as *"so many types recordings, then labs, then
lectures again which arent lectures like theres SO much useless stuff"*.

Run against the published `classify.js` itself, with a module of "Week 1":

| Title | Filed as |
|---|---|
| Piazza | **lecture** |
| Welcome! | **lecture** |
| Grades FAQ | **lecture** |
| Academic Integrity Policy | **lecture** |
| Java Install Guide | **lecture** |
| Zoom link for help sessions | **lecture** |

`LECTURE_NUMBERED` was tested against the item's text **plus its module name**,
and `"Week 1"` matches `week\s*\d+` -- so every item a course filed under a
week-numbered module became a lecture. The existing 32-case corpus never caught
it because every one of its junk titles happened to carry a `NOT_THE_MATERIAL`
word; "Piazza" and "Welcome!" carry nothing at all. Labs were the other half:
there is no lab rule, so "Lab 3" and "Tutorial 7" fell through to `page`.

Both halves are fixed, and **the server half is the one that matters today**:
`_material_section` re-sections the tab from the title, so the rows already in
the database are fixed without an extension release. Same reasoning as
`file_ext` and the deadline dates -- a classifier change only helps what is
scraped after it ships, and the store is still on 3.5.1.

**The section is decided on a POSITIVE signal, and the default is `other`.**
Never a blacklist of junk words: a list of things to exclude is endless and
wrong the first time a course invents a new kind of link, while a list of
things that mean "this is a lecture" is short. Anything unrecognised lands
under "Other course material", still on the page and still one click away --
this can only ever MOVE a row, never hide one.

A module may still vouch for an item, but only one that names the MATERIAL
("Lectures", "Course material") **and** only for an item that is itself
numbered or a real file. Left to vouch alone it has the same fault as the week
module, just rarer: a course keeping everything in one "Course material" folder
would empty that folder straight back into the Lectures list. A bare link with
neither a number nor a file behind it is the one shape that is never a lecture.

The stat beside the tab counts what the tab SHOWS. Left as the raw bucket it
read 21 above a list of four -- the "37 courses in the extension, 11 on the
dashboard" bug, one screen down.

**`_flatten_thin_groups` drops a sub-heading that is not organising anything.**
A week holds one lecture, so four lectures rendered as "Week 1 (1)", "Week 2
(1)", "Week 3 (1)", "Week 4 (1)" -- more heading than content, with the week
already in every title. The test is whether the grouping pays for itself: more
groups than half the items means the average group holds fewer than two. The
heading then moves onto the row, where it still says which week without
claiming to be structure. Same complaint `_organise_course_items` exists to
answer, arriving by a different road.

**`classify-check.html` cache-busts its own script tag.** A plain
`src="classify.js"` is served from the browser's HTTP cache, so after editing
the classifier the page re-ran the OLD file and reported six failures that were
already fixed on disk -- which reads exactly like the fix not working.

**A recording is not a lecture with a different word on it.** Reported from a
real ELEC2501B workspace: "Lecture_3" sat directly above "Lecture 3 Recording",
as did Lecture 4, Lecture 05 and both parts of Lecture 06 -- eighteen rows
alternating between the thing you read and the thing you watch. The extension
classifies both as `lecture` and they carry the same breadcrumb, so nothing
downstream could tell them apart. `_split_recordings` divides them and the page
renders "Lecture recordings" as its own heading **below** the slides, which are
what a student opens first.

It is a server-side split on purpose. A new `content_type` from the extension
would need a Web Store release and would leave every row already imported mixed
up for ever -- the same trap as the deadline dates and the lecture links.

`_is_lecture_recording` is deliberately narrow. A video or audio **file
extension** is the half that cannot be wrong: a video sitting in the lecture
list is the recording whatever it is called. The word **"video" is not
matched**, because it is a topic as often as it is a format -- "Lecture 5:
Video Transmission" is a slide deck -- and misfiling a deck buys nothing, since
a real video lecture carries a video file. The stat beside the tab still counts
both: splitting the LIST must not quietly halve the NUMBER next to it.

**One group under a heading that names the section repeats itself.** The first
version rendered "Lecture recordings (5)" directly above the breadcrumb group
"Lectures (5)" -- the same noise the breadcrumb grouping below exists to
remove. The inner heading is shown only when there is more than one group.

**Material is grouped on the TOP of the breadcrumb, not all of it.**
`_organise_course_items` -- Brightspace nests "Lectures › Topic 04 › ..." and
`groupby('module_name')` made a heading per topic: thirteen headings over
twenty-two lectures on a real course, most holding one item, each one repeating
the title directly underneath. The topic becomes a label on the row, where it
reads as context rather than structure. `_natural_key` sorts Topic 2 before
Topic 10, `_strip_course_prefix` drops the "SYSC2004 - " every row repeated on
the one page that can only be about that course, and inside a topic the lecture
itself leads its own code samples.

**A CSS id selector immediately after `{` is a Jinja comment.**
`@media (max-width:900px){#tourRing{...}}` contains `{#`, which opens a comment
that swallows the template as far as the next `#}` -- in this case several
hundred lines away, reported as an unrelated "unknown tag 'endfor'". Put a
space after the brace.

Selectors in the shared sheet use `:where()` so page rules can override them. A
chain of `:not()` raises specificity and silently out-ranks the page — that is
why the grade calculator's readonly field once rendered as a white browser box.

**A class that sets `display` out-ranks `[hidden]`.** `#courseState` shipped as
`class="note meta show" hidden`, and `.meta.show{display:block}` beat the
attribute, so an empty bordered box sat under the course picker for ever. The
element is toggled with `el.hidden` while its sibling `#analysisMeta` uses the
class, so both mechanisms have to keep working: the id rule carries the display
and `#courseState[hidden]` out-ranks it. When an element is toggled with
`el.hidden`, never give it a class that sets `display`.

**Two empty states on one screen read as two broken things.** The calculator
opened with a result panel showing "—" above the component list's own "No
assessments yet". The result panel is hidden until there is something to report.

The palette is a single neutral ramp. Cards must be visibly lighter than the page
behind them; the first pass sat everything within nine points of black and had to
be lifted wholesale.

## One definition of what term a course is in

`_term_meta`, `_term_label`, `_term_sort_key` and `_current_term_label` are
module-level. `_term_meta` was a closure inside `home()`, so the grade
calculator listed every course a student had ever taken in one flat dropdown —
thirty-three rows by third year, with no way to find this semester's. The picker
renders `<optgroup>`s now, newest term first with the current term promoted to
the top, and the term is the group heading rather than a suffix on every row.

Two screens grouping courses by different rules is the same class of bug as two
screens disagreeing about a grade. Keep one definition.

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

**The Canvas syllabus only arrives if you ask for it.** `syllabus_body` comes
back from `courses` solely when it is named in `include[]`, and nothing named
it -- so a Canvas student had no outline at all, and therefore no grading
scheme, no pass rules and no Ask-the-outline, on a platform that had been
offering the text the whole time. It is stored as an ordinary `outline` content
item so every downstream reader is unchanged. Under 200 characters it is
ignored: plenty of courses put "see the attached PDF" in that field, and
analysing that would invent a scheme.

Both LMSs write through `_import_courses` and `_import_content`, so the
dashboard, calendar, grade calculator and study tools never learn which one a
student uses. Adding a third should extend those, not parallel them.

## What gets imported: everything, always

**Nexora imports every course in every term, and there is no question about
it.** The extension used to ask once and remember the answer, which failed the
way remembered answers do: a term named in first year stayed named, `content.js`
filtered enrolments to `c.term === term`, and the student's current courses
simply stopped arriving. Silently -- a sync that imports nothing looks exactly
like one that worked.

`getConfig()` returns `importScope: 'all'` unconditionally; the popup has no
scope screen and the advanced pane rewrites the stored keys to `'all'` on every
save, so an install carrying an old one-term choice repairs itself. The keys are
still written so a downgrade behaves.

This costs a student nothing, because `home()` already opens on the current term
(`_current_term_label`) and keeps the rest behind `#termPick`. "Import
everything" was only ever unusable without that, and it has it.

`currentTermLabel()` and the term-listing plumbing survive for the popup's
copy and for Canvas; `selectedTermLabel()` no longer decides anything.

## A course code is a label, not an identity

`_import_courses` matched on `source_uid` and then **fell back to matching on
`course_code`**, so every offering of the same code collapsed into one row and
whichever was imported last won the name and the term. Reported as **37 courses
in the extension, 11 on the dashboard**, with a third-year student's current
courses wearing first-year names.

The missing cards were the smaller half. The content sync walks the course list
it reads back from `/api/sync/status`, so **a course with no row is never
scraped** -- its outline, materials and announcements never arrive, and the
outline analyser and study planner have nothing to work with for exactly the
courses that matter.

An org unit is the identity. `_prune_courses` already assumed this ("a code can
be shared by two org units"), and every consumer groups by code anyway, so two
offerings still render as one card -- owned by the **newest term**, because a
student looking at ECOR1031 means the one they are in now.

`/api/sync/courses` now returns **`found` and `stored`**. They are allowed to
differ -- a hidden course, a tombstone -- but a student pressing sync has to be
able to see that they do. Reporting one number and showing the other is how 37
sat beside 11 for weeks.

## One definition of which offering a course code means

`_newest_offering_per_code(rows)`. Storing one row per offering means a course
taken twice is two rows sharing a code, and every screen that lists courses has
to pick one. Left to a dict comprehension the winner is whichever row the query
yielded last -- found in the browser right after the import change shipped:
ECOR1031 appeared under **Fall 2025 with its first-year title**, in the grade
calculator's term picker, on the page a student uses to enter *this* term's
marks. The study room had it too.

The newest term wins. The dashboard, the grade calculator and the study room
all call the same helper, because two screens disagreeing about which offering
a code means is the same class of bug as two disagreeing about a grade.

**A placeholder code is not printed.** An org unit with no course code in its
name gets `COURSE<orgUnitId>` from the extension's parser -- Carleton's "(MS
LAP) Math and Stats Learning Assistance Program" is a real one, and so is a
co-op shell. The code has to stay, because it is the join key for content,
deadlines and grades, but `_display_code` blanks it on the card so a student
sees the course's name rather than a number that reads as a fault.

## The envelope's course is a fallback, on both paths

`/api/sync/content` takes `{"course": X, "items": [...]}`. `_import_content`
read the code off each item and ignored the envelope, and `_import_deadlines`
did the same -- so a caller that filled only the envelope got **every deadline
filed under OTHER**, which costs the outline's weights, `_course_standing`, and
the plan's ability to say which course a block is for. Caught by a plan block
coming back as `course='OTHER'` during an audit. The extension puts the code on
every item, so this was latent, not live -- but two sibling paths reading the
same payload differently is exactly how the last one happened.

## What fills the database

Render **suspended the production database for exceeding its 1 GB storage**,
which took the whole app down. Measured 2026-09-25, before any cleanup:

| Table | Size | Share | Rows |
|---|---|---|---|
| `academic_update` | 786.0 MB | 96.1% | 2,581,147 |
| `course_content` | 20.8 MB | 2.5% | 25,682 |
| everything else | 2.5 MB | 0.4% | |

**The first diagnosis was wrong, and it is worth knowing how.**
`course_content.content_text` -- the scraped body of every item, capped at a
flat 120,000 characters -- was blamed from reading the schema, because it is
the only column that *could* hold a gigabyte. It holds 20.8 MB. The largest
single body in the entire database is a 20 KB outline. Two commits were spent
capping it before anything was measured, and a cleanup built on that theory
would have freed 2.5% of the problem.

`pg_stat_user_tables` lied too: `n_live_tup` read **4,758** against a real
**2,581,147**, because ANALYZE had never run on the table. That is what made
786 MB across "4,758 rows" look like enormous rows rather than enormous
numbers of them. **Count the big tables for real** -- `scripts/db_usage.py`
does.

**An item was announced once per sync, for ever.** 2,581,147 rows across only
33,795 distinct (user, item, type) combinations -- one Brightspace page,
"Knowledge Check - Q2", had 638 byte-identical rows, differing only in their
timestamp. `_import_content` deletes a course's `CourseContent` rows when the
payload carries `replace_courses`, then re-imports each item; the `existing`
lookup on the next line therefore always misses, the `content_hash`
comparison that exists to detect "unchanged" is **unreachable code in
practice**, and every item takes the "new" branch and writes a fresh
`AcademicUpdate`. Lectures and outlines write a second one. The giveaway was
`content_updated`: 928 rows in two months, because nothing ever survives to
be compared against.

`announce()` is the fix: a set of `(related_uid, update_type)` already on
file for that student, read **once per sync** rather than per item, since the
loop runs over 1,200 of them. The content row is deleted by `replace_courses`
and so cannot be what remembers; the update row is what remembers.
`tests/test_update_duplication.py` pins it, and all four of its tests were
confirmed to fail against the old code first.

**Retention was not the answer here, and nearly hid the real one.**
`ACADEMIC_UPDATE_KEEP_DAYS` (45) prunes on each sync, which is worth having,
but 2,516,112 of the 2,581,147 rows were *inside* 45 days. A retention window
bounds a table that grows slowly; it does nothing about one growing by
150,000-220,000 rows a day. Deleting the duplicates without fixing the insert
would have bought about three months.

**The content caps are still right, on their own merits.** Every cap sits
ABOVE what any feature reads (`_CONTENT_TEXT_CAP`), because the only thing
that makes Nexora fetch a body again from Brightspace is it being **shorter
than ~120 characters** (the `needs_content` branches). A body cut to a few
thousand is far above that, so it would never re-fetch -- it would be read as
the whole item, by the summariser and by the student. The first pass at this
capped announcements at 4,000 and would have done exactly that.
`_cap_content_text` therefore also **marks** a body it trims.

`scripts/db_usage.py` answers "what is taking the space" with numbers -- per
table, with real row counts -- and with `--plan` / `--apply` removes the
duplicate update rows, keeping the newest per (user, item, type). Nothing a
screen shows is lost: `/api/updates` is called by nothing, the course
workspace reads the newest twelve for a course (twelve copies of one item
before, twelve different items after), the Canvas announcement check only
needs one row per uid to exist, and the dashboard's "Recent updates" panel
does not read this table at all -- `_recent_updates` reads the content rows.
It deletes in batches and commits each one: a single transaction holding 2.5M
deletes builds one enormous WAL record and holds every dead tuple locked
until it commits, which is the one shape to avoid on a database that has just
run out of disk.

Two things about Postgres that make this worse than it looks: a `DELETE` or a
blanking `UPDATE` writes a new row version, so usage rises before it falls,
and the space only returns to the disk after `VACUUM FULL`. That rewrites the
table into a new file sized to **what survives**, not to the original -- so
after the delete it needs almost no free space, and the order matters: delete
first, vacuum second. On a database already at its limit with nothing yet
deleted, raise the storage one step first.

**Render's storage increase is permanent.** Compute is reversible and storage
is not ("you can increase storage later, but you can't decrease it"), and
from 1 GB the next step is 5 GB. At $0.30/GB/month that is $1.50 rather than
$0.30 -- small, but it never comes back down, so clean before buying. A
suspended database also has to be brought back by hand: raise the storage,
then **Restart Database** on the Info page. Until it restarts, the port
answers and TLS completes while Postgres never responds, and a deliberately
wrong password returns the identical "SSL connection has been closed
unexpectedly" -- which is how to tell "not running" from "not authorised".

## Caps must be loud

`COURSE_IMPORT_MAX` and `DASHBOARD_COURSE_LIMIT` are both 400, raised from 80
and 60 once offerings stopped collapsing: a four-year degree is past both, and
the only thing a low cap bought was courses vanishing without a word. A payload
past the cap now returns `truncated` on the import result. The rule that keeps
coming back: a limit a student cannot see is indistinguishable from a bug.

## Extension

Injection is programmatic (`chrome.scripting.executeScript`), not a static
`content_scripts` entry, so it works on any university's domain with a permission
the student grants for their own origin.

- **The extension follows whoever is signed in on the site.** It used to keep
  the key from the one time the connect page paired it, so switching accounts
  on the site left it pushing courses into the previous account -- and on a
  shared computer, into someone else's. `app_base.html` offers the session's
  own key (`GET /api/sync/extension-key`, which never rotates) on every
  signed-in page; the worker's `NEXORA_PAIR` returns `unchanged` when nothing
  differs and, on a real switch, clears `detectedCourses`, `lastFullSync`,
  `lastSync` and the sync status so the popup does not describe the last
  account and the next sync is a first sync.
- **`allFrames: true` is required.** Brightspace renders My Courses in a widget
  iframe; injecting only into the top document finds nothing and reports "no
  course cards found" on a page full of course cards.
- Several frames answer a discovery request, so **a frame that finds nothing
  stays silent** and only the top document reports failure, after waiting longer
  than a subframe needs to succeed.
- **Course discovery uses the enrolments API, not the DOM.** The My Courses
  widget renders one term tab at a time, so scraping returns whichever term
  happens to be mounted. The DOM is merged in for images and as a fallback.
- **The permission prompt destroys the popup.** `await
  chrome.permissions.request(...)` never resumes: the student pressed Allow, saw
  nothing happen, and had to reopen the popup and press Import again — on the
  first thing anyone ever does with this extension. The popup writes
  `pendingImport` to storage *before* asking, and the worker's
  `chrome.permissions.onAdded` finishes the job; whichever side gets there first
  removes the key, so a popup that does survive cannot start a second run. The
  popup already renders a `running` state, so reopening it shows progress.
- **A Manifest V3 worker is collected after ~30s without an event**, and a sync
  runs for minutes. `holdWorkerAwake()` / `releaseWorker()` bracket long work;
  without them, clicking away kills the sync mid-run.
- `extension/*-check.html` are browser-run checks for the parser, the term
  helpers, the popup state machine and the topic-file link. They are excluded from the package.
  `fetch()` needs an origin, so serve the folder first --
  `venv/Scripts/python.exe -m http.server 8777 --directory extension` -- and
  open `http://localhost:8777/term-check.html`. There is no node on this
  machine; `new Function(src)` in the browser is how a whole file gets a syntax
  check.
- The store package is rebuilt from **seven** files plus `icons/`
  (`classify.js` joined them); rebuild `store/nexora-sync.zip` whenever the
  version changes, and never include the check pages.
- **`classifyContent` lives in `classify.js`, once.** It decides where an item
  appears in a workspace, how much of its text the server keeps, and whether
  the study tools will ever offer to summarise it -- getting it wrong does not
  lose the item, it hides it where the student will not look. There were two
  identical copies, in `background.js` and `content.js`; the worker
  `importScripts` it and every `executeScript` lists it before `content.js`
  (there are two such call sites -- `background.js` and `popup.js` -- and
  missing one is a ReferenceError on the sync path only).
- **The old lecture rule wanted the number on the wrong side.**
  `slides?\s*[-_#:]?\s*\d+` requires a digit AFTER the keyword, and real
  courses number the other way: "01 SLIDES", "10. Gas NOTES", "15 Stoichiometry
  Video LECTURES", "10: Video Lecture: Gases". None matched, so all of them
  were filed as `resource` or `page` and never reached the Lectures list --
  CHEM1101 held 3 lectures against 90 resources. `notes` was not a keyword at
  all, and it is the commonest name a lecturer gives their own material.
  `slides` is plural on purpose ("Wet-Mounted Slide Preparation" is a lab
  protocol), and a `.pptx` is lecture material whatever it is called.
- **Anything naming an assessment stops there and never falls through.** A
  title carrying "guideline" sets `isSupportingDoc`, which skipped the quiz
  branch; once the lecture rules got better, "Week 5 Quiz ... Assignment
  Guidelines" then matched a lecture number and twelve real quizzes were filed
  as lectures. A supporting doc is a `resource` -- a rubric is not the
  assignment.
- **An empty array is TRUTHY, and it cost whole subtrees.** The TOC walker ran
  `walk(node.Modules || node.Children || [])`, so a module carrying
  `Modules: []` beside a filled `Children` walked the empty one and everything
  under it vanished -- no error, no count to notice it by. Reported as
  "sometimes it doesn't even put the lecture at all". All four child arrays are
  walked now (`Modules`, `Children`, `Topics`, `Content`), deduped by
  `source_uid` because a node can now be reached twice. Both copies of
  `flattenToc` had it.
- `extension/classify-check.html` runs the classifier against 32 titles taken
  from a real account, and the TOC walker against the shapes that lost items. Replaying stored rows through it is NOT a faithful
  simulation: the classifier is handed `file.source_url`, which carries a file
  extension, while the row stores `file.download_url`
  (`/content/topics/<id>/file`), which does not -- so a replay sees no
  extensions and invents hundreds of `resource -> page` moves that will not
  happen.

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

## The study planner

`/study-plan`. **The timetable card is promoted above the plan for a student
who has not uploaded one** (`has_timetable` -> `.lead{order:-1}`, and the
heading becomes "Start here / Add your timetable"). It shipped underneath an
empty plan card, which made the only useful action on the page the second thing
on it; a student asked where the timetable upload was while looking straight at
the page.

Three tables: `ClassBlock` (a recurring commitment -- a lecture,
a lab, a work shift, a commute; all block study time the same way),
`StudyPreference` (when this student will actually work), and `StudySession`
(the generated blocks).

**A biweekly lab is ordinary, and a weekly-only model is wrong both ways.**
Entered as a weekly class it charged the student its hours on every off week;
left out so the plan could use that time, the planner scheduled revision
straight over the lab. `ClassBlock.repeat` is `weekly` | `biweekly` and
`_block_occurs_on(block, day)` decides per DATE rather than per weekday --
`_free_slots` already had the real date in hand.

`anchor_date` is a date the class **does** fall on, deliberately not "odd or
even weeks": whose numbering, ISO, the term's, the department's? A student can
always answer when their next lab is. Weeks are compared Monday-to-Monday, so
answering with the Monday of that week works as well as answering with the
lab's own day.

A biweekly block with **no** anchor is treated as running every week. Losing a
study slot costs an hour; scheduling revision over a lab the student then
misses costs the lab. Measured effect on a 9-12 Thursday lab: 8h15 free on lab
weeks, 11h30 on off weeks -- 3h15 per fortnight that used to be discarded.

The timetable reader is told to return `repeat: "biweekly"` for alternating,
odd/even, fortnightly or A/B week wording, and told **not** to guess which
weeks -- the student confirms the anchor. The week grid labels those chips
"every other week", because an unmarked biweekly block looks weekly and the
student cannot tell whether Nexora understood it.

Times are **minutes from midnight**, not time columns. The planner does
arithmetic on them constantly and a weekly repeat has no date to anchor a
timestamp to.

**The scheduling is arithmetic, not a model.** Gemini reads the timetable image
and nothing else. A plan has to be the same plan when you look at it twice, has
to explain why every block is where it is, and must not cost quota to redraw --
`test_the_same_week_generated_twice_is_the_same_week` pins the first of those.

## The planner runs on the student's clock

`_student_now(user)` / `_student_today(user)`, used by `_build_plan`,
`_study_items`, `_free_slots`, `_close_off_yesterday`, `_save_plan` and every
study-plan route. They took `date.today()` -- the server's date, UTC on Render.
At 8 PM in Ottawa on a Tuesday, UTC is already Wednesday: the plan started
tomorrow, Tuesday's evening slots were simply gone, and `_close_off_yesterday`
marked the untouched Tuesday blocks **missed** while the student still had the
whole evening. "Every date is the student's day, not UTC", missing from the one
feature that is entirely about which day it is.

**Today's window opens at the next quarter-hour after now** -- only when the
caller is planning from the real clock. `_build_plan` passes
`from_now=(today is None)` to `_free_slots`; a fixed date handed in (a test,
the insight page) is a whole day even when it happens to be today. The first
version cut on "start equals today", and a biweekly-lab test written around
10 September silently changed meaning on 17 September when its second week
landed on the real date.

## Nexora measuring what actually happened

`StudySession.state` was written by Done/Skip and read by **nothing**. Nexora
recorded no fact about what a student actually did, and that one gap blocked
every "learn the student" idea: "this student needs 3h15m for a SYSC lab"
requires having recorded six labs.

`actual_minutes` and `completed_at` fix it, and `_learned_effort(user_id)`
turns them into estimates keyed by (course, kind). Four rules keep it honest:

- **Only real answers count.** A session marked done with no duration records
  `0` and is skipped. Recording the *planned* length as the actual one would
  teach the estimator its own guess back for ever, and it would never improve.
- **Per item, not per block.** An assignment is several sessions; what it cost
  is the total.
- **The median, not the mean.** One abandoned six-hour session would drag every
  future estimate with it.
- **`LEARNED_EFFORT_MIN_SAMPLES` (3) before it trusts itself.** Below that the
  generic estimate is the honest answer. Measured effect: a lab guessed at 2h
  becomes 3h15m once six real ones are in.

Durations are capped at 8h -- a forgotten timer must not teach it that a lab
takes a day and a half.

## The day that went by

`_close_off_yesterday` retires plan blocks whose date has passed untouched,
from `planned` to **`missed`**. They used to stay `planned` for ever, because
`_save_plan` only replaces sessions from today forward, so missed work
accumulated as ghosts no rebuild could clear and no count could trust.

`skipped` is a decision the student made; `missed` is a day that went by. They
are different facts.

`_catch_up_message` says what changed in one sentence with no scolding in it --
*"2h30 of planned study did not happen (BIOL1103, SYSC2100). It is back in your
plan across 4 blocks, ahead of the lighter work."* Every planner fails the same
way: the moment someone misses Tuesday the plan is wrong, stays wrong, and
becomes a thing they avoid opening. There is a test asserting the message
contains none of "should have", "failed", "behind schedule".

## Revision has stages

`_EXAM_LADDER` and `_exam_stage(days_left)`: read and rebuild notes, then weak
topics, then timed problem sets, then a past paper, then repair what it
exposed, then light review only. Deterministic on the gap to the exam, so the
same day always gets the same stage and the plan can explain itself.

**Capping the daily minutes was not enough to make it reachable.** The greedy
loop poured a five-hour midterm into the first two free days and stopped eleven
days out, so the past-paper rung existed and could never be climbed.
`exam_gaps` spaces the same six sessions across the window -- one every ~2 days,
finishing near the exam -- with the last two days always open so the final rung
cannot be lost to a busy week.

A block's focus line carries three different things and needs all of them: the
topics are WHAT, the outline's own pointer ("module 4-6") is WHERE, and the rung
is HOW. The first version of this dropped the pointer, and
`test_a_session_names_the_topics_the_outline_gave` caught it.

## What the plan is FOR

`StudyPreference.focus` is `all` or `exams`, and `focus_courses` narrows it
further. Planning everything sounds generous and is the opposite: a term has
twenty small deadlines and two exams, so the small ones win on count. Measured
on a realistic term -- 27 blocks with everything on, of which one project
milestone took **8** and each pre-lab quiz took 2. Aimed at exams: 12 blocks,
all revision. A student following the first plan reaches their midterm having
revised nothing, which is what was reported.

`_EXAM_KINDS` includes `test` deliberately: in most courses a "test" is a
midterm under another name, and dropping it would lose the thing the student is
actually worried about.

**`/api/study-plan/ask` lets the AI read the REQUEST, never write the
schedule.** A sentence becomes settings -- focus, courses, days, daily minutes
-- and the existing arithmetic builds the week. That is the same rule as the
rest of the planner: a plan has to be the same plan when you look at it twice,
has to explain why every block is where it is, and must not cost quota to
redraw. A model laying out the week satisfies none of those, and
`test_the_plan_it_returns_matches_a_plain_rebuild` pins the split.

Two guards on that endpoint. A course code the student does not have is
dropped, because a hallucinated one silently empties their plan; and the
response carries `understood`, shown on the page, so a misreading is visible
instead of quietly reshaping someone's term. The buttons do the common case
with no AI at all, so a quota outage degrades to "use the buttons" rather than
killing the feature.

`_known_course_codes` unions SyncedCourse, Task and CourseOutlineProfile. A
student who connected only the Brightspace calendar feed has deadlines and no
course rows, and naming one of those courses was being discarded as unknown --
so "only MATH" appeared to do nothing.

What makes it worth using is data Nexora already has: `_study_items` reads the
outline's component weight for each piece of work, so a 30% midterm earns three
times the hours of a 10% quiz instead of time being spread evenly per course.
Priority is weight ÷ days remaining, so 25% due Tuesday outranks 25% due in
three weeks.

Four traps, each with a test:

- **Overdue work could never be scheduled.** The "never schedule past the
  deadline" rule excluded it, because for late work every day is past the
  deadline -- exactly the work a student most needs time for. `latest_day`
  gives it a three-day grace window.
- `_free_slots` leaves `buffer_min` either side of a class. A block starting
  the minute a lecture ends is one nobody attends.
- Revision for an exam more than `EXAM_REVISION_WINDOW` days away does not
  start now just because there is a gap, and revision for a near one is spread
  over days rather than crammed.
- `MAX_CONSECUTIVE_SAME` switches subjects after two blocks *while another item
  still has work*. Consecutive runs at the end of a plan are correct, not a bug
  -- there is nothing else left to schedule.

`_save_plan` replaces only `planned` sessions from today forward, so
regenerating never erases what someone has ticked off or lived through.

**The plan reads the student, not just the calendar.** `_course_standing`
averages the marks already entered, and a course under 50% earns 1.6x the
priority and 40% more hours while one above 85% earns 0.8x -- capped both ways,
and ignored below 5% of marked weight, because one 2% quiz is not evidence
about a course. `_at_risk_courses` asks `_compute_official_result` (never its
own copy of the pass-rule maths) which courses have a rule currently failing,
and those outrank arithmetic. A rule that is *pending* must not pull time:
unknown is not failing, the same rule as the grade panel.

**`study_brief_json` on CourseOutlineProfile is the outline's own words.**
`_build_course_brief` asks Gemini once, per course, what each assessment
covers, and caches it -- the answer does not change between two rebuilds of the
same week, and a call per rebuild would spend the quota the outline pipeline
needs. `test_a_rebuild_never_reaches_gemini` pins that. It is handed the exact
assessment list and anything it returns outside that list is dropped, so a
hallucinated "Comprehensive Viva" never becomes a session.

**`_assessment_stem` is why "Unit 2 Test" finds "Unit tests".** An outline
lists a group; the deadlines are numbered instances of it, and plain
containment misses that in both directions -- so every numbered test was
falling back to a generic focus line *and* to a default weight. Digits and
plurals come off before comparing. `_weight_for` had the same blind spot by a
different route.

Every session carries `focus` (a verb and an object) and `reason` (what it is
worth, when it is due). "Study BIOL for 90 minutes" is the advice that makes
plans useless.

## Access: a free month, then paid

`_entitlement(user)` is the only thing any gate consults. Two screens
disagreeing about whether someone has paid is the same class of bug as two
disagreeing about whether they passed, and here it costs money both ways.

- A new signup gets `TRIAL_DAYS` (30) with **no card**. Card-up-front converts
  better per start and collapses the number of starts, which for an app nobody
  has heard of is the wrong trade.
- Expiry makes the account **read-only, not dark**. `requires_active` guards
  only what spends Gemini quota or reaches an LMS. Reading imported work,
  entering marks by hand, exporting and deleting all keep working. Taking away
  work a student already did is how you get written about.
- The extension and Canvas paths authenticate with a token, not a session, so
  they check `_entitlement` inline beside the auth check rather than using the
  decorator.
- **Every number is an environment variable** (`TRIAL_DAYS`, `PRICE_LABEL`,
  `REFERRAL_*`) because the price is not decided. Nothing quotes an amount
  until `PRICE_LABEL` is set.
- `grandfather_existing_accounts()` runs at startup and gives any account
  predating this a year. The people who used it while it was free, including
  the testers who found the bugs, do not meet a paywall.

**Referrals pay on activation, never on signup.** `_credit_referrer` fires from
`_import_courses` -- the friend has actually imported something -- and is
one-shot per referred account, capped at `REFERRAL_MONTHS_MAX`. Paying on
signup is the entire exploit: ten throwaway addresses, a free year.
`_grant_days` extends from whichever expiry is later, so crediting someone on
day two of their trial adds a month instead of costing them 28 days.

## The public outline reader

`/what-do-i-need` and `/api/public/analyze-outline`, both unauthenticated. It
exists because nobody makes an account to find out whether an app reads their
outline correctly -- but they will paste one in. It is the top of the funnel and
the proof at the same time.

Deliberately the **single-call** analyzer, not the three-stage pipeline: about a
third of the cost, several times faster, and it is a demonstration rather than a
policy anyone will rely on all term.

**The cap is the important part.** A per-IP limit does not bound the bill -- a
thousand people asking once each is a thousand calls. `PublicUsage` counts the
day in the **database**, because in-memory counting gives every gunicorn worker
its own allowance and every redeploy a fresh one. A failed analysis spends
nothing, and hitting the ceiling makes no Gemini call at all.

Measured cost per analysis, for sizing that ceiling: the saved three-stage
analysis is ~20,500 input and ~5,000 output tokens (3.4c on Flash, 1.9c on
Flash-Lite); the public single call is ~6,250 in and ~2,500 out (1.4c / 0.8c).
A whole paying student is well under a dollar a term. Flash prices double on
1 Jan 2027; Flash-Lite has no announced rise.

`PUBLIC_CAP_MENTIONS_SIGNUP=0` makes the cap message say "try again tomorrow"
instead of mentioning an account. A rate limit dressed as a sales pitch reads as
manufactured scarcity.

## Ticking a task off refills the list, and is reversible

"What to do first" renders twelve cards and shows five; ticking one reveals
the next (`settleQueue`). `.task[hidden]{display:none}` is what makes the
hiding real: `.task{display:grid}` out-ranks the attribute (the `#courseState`
trap again), and the first version shipped with all twelve painted while a
test that checked the *attribute* passed. Verify visibility by computed
`display`, never by `el.hidden`. It sent six and showed five, so two ticks left a
list of three with nothing moving up -- reported as the feature looking
broken.

**A completed task has a way back.** `build_grouped_cards(completed=True)`
builds the list of what has been ticked off, rendered under the queue as
"Done recently" with a Put back button per row. Before that, a completed task
was dropped before any view was built, a sync never un-completes one
(`_import_deadlines` does not touch `completed`), and the undo toast lasts
nine seconds -- so a student testing the feature who clicked Done on six
things had lost them from Nexora for good. Put back re-reads the page rather
than guessing where the task belongs in the queue: order is the server's.

## "Recent updates" is recent by Brightspace's date

`_recent_updates(user, limit, within_days)`. The panel used to show the four
newest `AcademicUpdate` rows, which are stamped at **sync** time. Once the
extension imported every term, first-year CHEM 1101 announcements arrived on
the same afternoon as this week's and led the panel dated "Sep 21", above
anything from the term in progress. Reported with a screenshot.

It reads the content rows now: `published_at` (what Brightspace said), on
the student's day via `local_date_iso`, newest first, and each one links to
the page Brightspace gave through `_lms_link`. Only reading material counts
(`_UPDATE_CONTENT_TYPES`): an assignment's `published_at` is its **due
date** in the extension, and a deadline belongs to the deadline lists. A row
with no date is not "recent" -- dating it by the sync is the bug. **A date
from another year shows the year**; "Sep 12" on a 2024 notice reads as this
September, which is the confusion that was reported.

The stat cell counts items published in the last seven days
(`updates_this_week`), not the panel's length.

## A task has two actions: tick it, or go and do it

"What to do first" was one `<button>` per row: the whole card ticked the
task off, and there was no way to reach the work. It is a `div.task` now
with a `button.box` that ticks and, when the LMS gave a link, the title is
an anchor into the LMS (`t.source_link`, `_lms_link`-checked, never
constructed). The row's click handler returns early on a click inside an
anchor, so opening the work is not ticking it off. `aria-pressed` lives on
the row (the CSS reads it) and is mirrored onto the box for a screen reader.
The toast label strips the link's arrow glyph.

## The study room is where a plan block gets done

`templates/study_room.html`, `/study-room`. A real room in three.js --
PBR materials under a `RoomEnvironment` map, procedural wood, plaster,
paper and sky drawn on canvases at load (the page owns no image files),
soft shadows from a desk lamp and moonlight through the window, bloom on
the screen and the lamp, ACES tone mapping, a cinematic entry. Everything
on the desk is the student's real work with the identity the drawer needs
to act on it: the notebook ticks a task through
`/api/tasks/<group>/complete`, the wall calendar links into the LMS, the
shelf opens course workspaces, the monitor shows the plan.

**The desk clock is the point of the room.** A block is started from the
monitor's plan, the clock counts it, and when it ends the session is posted
`done` with the minutes that really passed -- the fact `_learned_effort`
turns into better estimates. Nothing about the schedule is decided in this
file; the planner's arithmetic still owns the week.

**The screen shows the next day that has blocks.** At 23:50 with nothing
left today it says "Tomorrow" rather than "no plan" -- the route sends
today's blocks and the next two days', and `planDay()` picks. That is also
why the page never says "Tonight" when it means tomorrow morning.

**The room is lit by many small warm sources, not by one lamp.** Two
rounds of "extremely poor quality" came down to the same thing: the walls
were unlit, so the room read as objects floating in a void, and no amount
of anti-aliasing fixes that. The third pass copied what every cosy-desk
photograph does -- **LED strips under every shelf edge**, under the desk
and on the bookcase, glowing orb lamps, a monitor light bar, a candle, and
a **dusk city skyline** outside instead of a black night. `strip()` makes
one: an emissive bar plus the point light it throws. The strips and orbs
stay lit in every mood, because they are what makes the room a room.
`skyTexture('dusk')` is the default -- deep blue through mauve to amber,
two ranks of buildings so the skyline has depth, and lit windows.

**The shot is composed, not just pointed.** The reference the owner sent
frames it in from the left: window and a floor plant on one side, the
bookcase wall on the other, the desk across the middle, an armchair
cropped into the bottom corner. Getting everything inside a 54-degree
frame took arithmetic, not guesswork -- project a candidate position with
`v.project(camera)` and read off the screen percentage, which is how the
corkboard and the cabinet were found to be sitting outside the frame
entirely while looking fine in the scene graph. The bookcase was narrowed
to 1.9m and pushed into the back corner to make wall room for them.

**The earlier report, for the record:** It rendered at 1.6x with no anti-aliasing inside the
post chain, 1K textures across a whole desk, no ambient occlusion (things
floated), and nothing moved. What changed: full device pixel ratio (to 2x),
MSAA on the composer's own target (`samples: 4`, half-float), a `GTAOPass`
before bloom, 2K wood with a normal map generated from the grain
(`normalFrom`), every text surface drawn at 2x, and a room that breathes --
dust in the two lights, steam off the mug, twinkling fairy lights and
stars, the plant swaying, a blinking cursor and a live progress ring on the
screen, a slow camera drift after four idle seconds, rain behind the glass
when the rain sound is on. And more of the student's own data on the walls:
a corkboard on the right wall with deadlines as sticky notes, a whiteboard
on the left with the week's plan as a wall planner (the route now sends a
week of blocks), a bookcase in the corner. The overview camera looks in
from the left so both side walls are in frame on a wide screen.

**The monitor is a screen, not a picture of one.** Its nav rail is
clickable: the pointer handler reads `intersection.uv` off the screen plane,
`railHit` maps that to a rail item, and the screen switches view -- Home,
Focus, Tasks, Plan, Stats. A click that lands on the rail does not open the
drawer, because that is not what clicking a screen does.

**Stats is the room paying back what the clock recorded.** `_room_stats`
reads `StudySession.actual_minutes`, which until now was written by Done
and read only by `_learned_effort`: hours this week, a day streak, four
weeks of sessions, a seven-day bar chart and a per-course split. Only
finished sessions with a real duration count, the same rule the estimator
follows, and **the streak does not break on a morning with nothing done
yet** -- counting strictly back from today would read every morning as a
lost streak. No other screen in Nexora tells a student what their week
actually was.

**Never call `setSize` on a pass the composer owns.** `EffectComposer.setSize(w, h)`
already resizes every pass at `w x h x pixelRatio`. The room's own `resize()`
then called `gtao.setSize(w, h)` and `bokeh.setSize(w, h)` with the CSS size and
set `bloom.resolution` to half of it -- so on a 1352x900 window the image was
rendered at 2230x1485 while **ambient occlusion ran at 1352x900 and the bloom
chain at 338x225**, composited over it. A low-resolution AO and glow smeared
across a sharp render is exactly what "it looks like 360p" is, and it survived
three rounds of raising the pixel ratio because the pixel ratio was never the
problem. Measure the actual buffers -- `composer.renderTarget1`,
`gtao.gtaoRenderTarget`, `bloom.renderTargetBright` -- before touching the
resolution again. The composer also caches `_pixelRatio` from its constructor,
so a quality change has to call `composer.setPixelRatio` or the targets stay at
the old size.

**The bookshelf flickered because faces were coplanar.** Every book's
bottom face sat at exactly the shelf's top face, as did the storage boxes
and the flat-laid stacks. Two surfaces at the same depth have no order, so
the GPU picks a different winner per pixel per frame and the shelf
shimmers. `REST` (1.2 tenths of a millimetre) is what anything resting on a
surface is lifted by. Watch for this whenever one box is positioned off
another's height rather than off its own.

**Nothing in the room reflected anything, which is what "cheap" meant.**
Every material was matte 0.4-0.9 with a low environment intensity, so there
were no highlights anywhere and every surface sat in the same brown value.
The floor and the desk are `MeshPhysicalMaterial` with clearcoat, the metal
is metalness .92-.95, `scene.environmentIntensity` went from .42 to .9, and
the wall was darkened so objects have something to separate from. Highlights
are most of what reads as expensive.

**Depth of field is off by default.** It softens every pixel it touches,
which is right in a photograph and the opposite of what "clean and sharp"
asks for. It is a switch in Ambience now.

**The quality tiers are what a GPU will actually hold.** Ultra at 2.6x on a
1600-wide window is 8.2 megapixels through ambient occlusion and bloom --
enough that the desktop app could not even composite a screenshot of it.
2.1 / 1.65 / 1.2.

**Sharpness is supersampling, and DOF costs it.** The renderer draws at
`devicePixelRatio x QUALITY[quality]` (Ultra 2.6, High 1.9, Battery 1.35)
and lets the browser scale down -- on a 1432x900 window at High that is
2720x1710 real pixels. The `BokehPass` had been set wide enough to soften
the whole frame, which reads as low resolution rather than as focus; it is
`aperture: .00035, maxblur: .0035` now, enough for the far wall and no
more. The quality switch lives in Ambience and prints the numbers.

**A room lit in one colour reads flat however bright it is.** The dusk
through the window is a cool source (`cool`, `coolFill`) and the lamps and
strips are warm; the contrast between them is what makes either read. Every
mood carries a `coolI` alongside its warm values.

**A test dates its fixtures on the student's clock**, via
`_student_today`, not `date.today()`. The two differ across midnight and on
any account whose timezone is not the server's, and three of these tests
failed at 00:0x for a reason that had nothing to do with what they test.

**The monitor carries a real UI.** Laid out like
the reference the owner sent: a rail of nav items, "Good evening, <name>.",
today's blocks as circular checkboxes with their times, and the focus ring
that fills as the clock runs. It is a `MeshBasicMaterial`, not a lit one --
a screen emits and does not reflect the room, and as a standard material
the lamp and the light bar landed on the white UI text and bloomed it into
solid bars.

**A leaf is a silhouette with holes in it.** The plants were flattened
spheres, which read as green pebbles. `leafTexture('monstera')` draws the
split-frond shape and cuts the splits back out with
`destination-out`; the leaves are alpha-tested planes. That one change does
more for "is this a real room" than any amount of extra polygons.

**Depth of field is most of what separates a render from a screenshot.** A
`BokehPass` after bloom, with the focus distance eased toward
`camera.position.distanceTo(controls.target)` each frame, so it follows
whatever the view is pointed at rather than sitting on a fixed plane.

An exact match for an AI-generated reference image is not reachable in
real-time WebGL -- that image has photographic focus, subsurface scattering
through the leaves and woven fabric. What is reachable is the composition,
the lighting scheme, the props and the UI, which is what these passes went
after.

Things that were found in the browser and are worth keeping:

- **A screen must be matte.** A rough-.28 emissive plane under a 14 cd spot
  rendered a hard specular disc that read as a sun on the monitor. Roughness
  .78 with the environment turned down; the glow comes from the emissive map
  and bloom, not from reflection.
- **Paper blows out under the lamp.** The page and the calendar carry a
  darker `color` multiplier so the texture stays legible at the lamp's
  brightness.
- **The window is on the back wall** so the overview camera can see it. On
  the side wall it was invisible from every default view, and the four
  lighting moods looked identical.
- **Portrait widens the field of view (72°) rather than backing the camera
  up** -- a phone held upright otherwise sees a slit of desk, and there is a
  wall behind the camera.
- **A `<label>` is a form caption in the design system**; the notebook's
  rows reset it or every task prints in uppercase mono.
- **The flat room.** `?flat=1`, or no WebGL2, lays the same panels out on
  the page. The 3D half of the module is skipped with a never-resolving
  top-level `await`, so the panels, the clock and the sound engine -- which
  are defined first -- keep working. `textures` is declared before that
  branch on purpose: it was declared after it once and the clock threw in
  flat mode. A 15 s watchdog offers the flat room if three.js never loads.
- **The render loop pauses when the tab is hidden**, so a measurement taken
  while the pane is collapsed reads a stale frame, not the room.
- Rain and brown noise are synthesised in WebAudio (filtered and integrated
  noise); nothing to host. The context is only created on a gesture.

`window.__room` exposes the renderer, timer, ambience and sound for checks
from the console. `tests/test_study_room.py` pins the data contract; the
rendering is verified in a browser, and `.claude/launch.json` has a
`nexora-ai` entry (`scripts/run_with_env.py`) that starts the app with
`.env` loaded so the tutor works in the preview.

## The first-run tour

**Ten steps, and one of them is the study planner.** For a long time there
were nine and none of them mentioned it: a new student was walked past
connecting, the grading scale, tasks, courses, grades, the calendar and the
study room, and never told about the feature that turns all of that into a
week. Add a step whenever a rail item earns one.

A spotlight over the real dashboard, not a slideshow: nine steps, ordered by
what a new account actually needs -- connect a source, save the grading scale
that outline analysis requires, then the parts that are still empty.

`tour_done` is on **User**, not in `localStorage`. Per-browser state would show
it again on a second device and again after clearing site data. Skip and Finish
hit the same endpoint on purpose: dismissing it is a decision. `/api/tour` is
POST to put it away and DELETE to arm it again (the profile page's replay link);
GET is deliberately not allowed, so a prefetch cannot spend it.

**Do not animate the spotlight's geometry.** Transitioning top/left/width/height
made the ring's *used* position depend on the animation timeline advancing.
Where it does not -- a throttled tab, or the in-app browser pane -- the
transitions sit at `currentTime: 0` for ever, and the ring stays where it
started while the card describes something else. It snaps now, and cannot
break. `getAnimations()` reporting `running` at time 0 is how that was found.

Steps point at `data-tour="nav-*"` hooks on the rail rather than at `href`
strings. A step whose target is missing is dropped rather than lighting an
empty rectangle, which is the normal case on a fresh account.

## Conventions

- Match the surrounding style in `app.py` — it is dense and compact in places;
  don't reformat neighbouring code.
- **Never insert a helper directly above a `def` that has decorators on it.**
  `@app.route(...)` and `@login_required` sit on the lines above the function
  they decorate, so a helper dropped in between silently steals them: the route
  disappears and the helper becomes the view, which fails as
  `TypeError: missing 1 required positional argument` on every request. Put new
  helpers above the decorator line, or well away from routes.
- Comments explain *why*, usually referencing a bug that motivated the line.
- New assessment logic gets a test in `tests/`.
