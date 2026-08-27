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
| `tests/` | pytest — 31 tests, all passing |
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

## Outline analysis

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
