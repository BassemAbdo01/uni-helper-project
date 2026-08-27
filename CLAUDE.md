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

Outline-analysis prompts are the weak point, not the model. On a real Carleton
outline, flash got all four weights right but dropped a `max of 11.5%` cap the
prompt never asked for, and flattened a "qualifies for second marking scheme"
threshold into a hard `component_minimum`. Fix prompts before spending money on
Pro-tier models.

## Conventions

- Match the surrounding style in `app.py` — it is dense and compact in places;
  don't reformat neighbouring code.
- Comments explain *why*, usually referencing a bug that motivated the line.
- New assessment logic gets a test in `tests/`.
