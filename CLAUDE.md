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

Probed 2026-09-05, three passes each:

| Model | Result |
|---|---|
| `gemini-pro-latest` | 429 every time -- free key cannot serve Pro |
| `gemini-3.1-pro-preview` | 429 every time |
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
`([ABCDF][+-]?)`, and a word boundary cannot sit between "-" and a space,
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

**"Final Lab Exam" is not the final exam.** `final` matches it, and BIOL 1103
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
