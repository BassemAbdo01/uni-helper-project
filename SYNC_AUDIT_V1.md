# Brightspace sync reliability audit

Extension → backend → database, read end to end on 2026-10-17 against
`092b439`. Every finding below names the line that causes it. Nothing here is
speculative: where I could not prove a thing from the code I have said so.

The question asked was: **how can this fail silently, serve stale data, or
create duplicates.**

Ranked by what it costs a student.

---

## S1 — A partial scrape replaces a whole course

`app.py:11939` deletes every `CourseContent` row for a course before the loop
re-imports what arrived:

```python
if replace_courses:
    CourseContent.query.filter(..., CourseContent.course_full.in_(replace_courses)).delete(...)
```

`extension/background.js:791` only sets `replace_courses` when the scan says
`ok && complete && authenticated && items.length > 0`, which is a real guard
and stops the empty case. **But `complete` is the extension's own opinion.** If
D2L returns 3 of 40 topics and still answers 200, the scan looks complete, 37
items are deleted and never come back, and the course page shows a short list
with `last_sync_ok = True` beside it. The student has no way to know.

The server trusts the flag without a second opinion. It has one available and
does not use it: it knows how many rows that course had a moment ago.

**Fix.** Refuse the replace when the incoming payload is drastically smaller
than what is on file — a floor, not a guess. Keep the old rows, record the
discrepancy on the course, and say so on the page. A short sync that is
*visible* is recoverable; a silent one is not.

---

## S2 — `last_sync_at` is written for one course per payload, and shown nowhere

`app.py:12089` stamps `last_sync_at` only inside `if scan_course:` — a single
code taken from `payload["course"]` or the first entry of `replace_courses`.
A payload carrying items for several courses updates `content_count` for all of
them (`touched_courses`, line 12080) and `last_sync_at` for **one**.

If the envelope names no course and `replace_courses` is empty, no course is
stamped at all.

It is also rendered in **zero templates** — I grepped. The column exists, is
populated, and is invisible.

**Fix.** Stamp every course the payload touched, and surface it. This is the
Phase 1 deliverable and it is mostly a display task.

---

## S3 — Content is truncated at 1,200 items in silence

`items[:1200]` at `app.py:11972` and again at `12005`, and once more in
`_import_deadlines`. Nothing counts what was dropped and nothing tells anyone.

This is the rule CLAUDE.md already states — *"a limit a student cannot see is
indistinguishable from a bug"* — and `COURSE_IMPORT_MAX` already honours it by
returning `truncated`. The content path never got the same treatment.

A real account in this repo's own history carried ~1,200 items for one term.
This cap is not theoretical.

**Fix.** Return `truncated` and the dropped count, the way the course import
already does.

---

## S4 — A renamed-URL item can duplicate, and 3.5.3 is exactly that change

`_content_source_uid` (`app.py:11920`) resolves in this order:

```
item["source_uid"]  →  item["source_url"][:4000]  →  f"{course}|{title}"
```

`extension/background.js:538` builds a real uid from D2L's topic id for every
topic that has one, so the common case is stable. **Items with no topic id fall
through to the URL** — and 3.5.3 changes exactly those URLs
(`DirectFileTopicDownload` instead of the API `/file` route). Those rows get a
new uid, so they arrive as *new* items beside the old ones, and re-announce.

`replace_courses` masks it when it fires, because the old rows are deleted
first. Where it does not fire, the duplicates persist.

**Fix.** Prefer a uid that cannot move. Where only a URL is available, key on
the topic id parsed out of it rather than the whole URL.

---

## S5 — The two importers derive the same item's identity differently

`_import_deadlines` (`app.py` ~11850):

```python
source_uid = f"ext:{item.get('source_uid') or f'{course_full}|{title}'}"
```

`_content_source_uid`: `source_uid → source_url → course|title`.

Same item, two fallback chains. They agree today only because the extension
sends `source_uid` for everything that carries a due date. This is the shape of
bug CLAUDE.md keeps recording — *"two sibling paths reading the same payload
differently is exactly how the last one happened"*.

**Fix.** One helper, both callers.

---

## S6 — A failed sync loses the whole payload and stamps nothing

`sync_course_content` (`app.py:11756`) has no `try`. `_import_content` commits
once at the end, so an exception on item 900 of 1,200 rolls back all 900, the
route 500s, and `last_sync_at` / `last_sync_error` are never written — the one
place that would have recorded the failure is inside the transaction that was
rolled back.

From the student's side the course simply does not update, with no explanation
anywhere.

**Fix.** Record the attempt outside the import transaction, the same split
`last_sync_attempt` already uses for the feed.

---

## S7 — "Recent" still has a stale-date trap waiting in Phase 2

`AcademicUpdate.created_at` is stamped at **sync** time, not when the thing
happened. `_recent_updates` was already repaired for content by reading
Brightspace's own `published_at`, but the table itself still carries sync time.

This is not a bug today, because nothing reads `created_at` for display. It
becomes one the moment "What changed since your last visit" diffs against it:
a student who has been away a week syncs, and the entire term reads as new.

**Fix (Phase 2).** Diff on the source's own date, never on the row's.

---

## S8 — `AcademicUpdate` is the table that took production down

786 MB, 2,581,147 rows, 96% of the database, Render suspended it. It is bounded
today only by `announce()`'s dedupe and a 45-day prune.

Phase 2a proposes writing **more** event types into it.

**This is the largest risk in the roadmap and it is not in Phase 1.** Anything
added here needs its dedupe designed before it is written, not after.

---

## What is sound

Worth saying, because it bounds where to look:

- **Deadline dedupe.** Keyed on `ext:` + uid, honours `sync_state='dismissed'`
  so a student's delete survives the next sync, and runs in its own pass before
  the content loop so a deadline on already-imported content is still created.
- **The announce dedupe.** Correct, and correctly bounded — it asks only about
  the uids in the current payload rather than loading the student's whole
  history, which is what made the first attempt at this unusable.
- **The replace guard on the extension side.** `ok && complete && authenticated
  && items.length > 0` is the right shape; S1 is about the server trusting it
  alone.
- **Per-course scan errors.** `last_sync_ok` / `last_sync_error` are recorded
  and do reach the course page.
- **Timezone adoption.** Taken from the client before any date is derived, and
  never overrides a zone the student set.

---

## Proposed order

| | Finding | Touches existing data? |
|---|---|---|
| 1 | S3 truncation counter | no |
| 2 | S2 `last_sync_at` per course + UI | no |
| 3 | S6 attempt recorded outside the transaction | no |
| 4 | S5 one uid helper | no |
| 5 | S1 replace floor | no — refuses a delete |
| 6 | S4 uid stability | **yes** — needs a backfill, and a backup first |

S4 is the only one that rewrites rows. Everything above it is additive or
refuses an action, so it can ship without a backup.
