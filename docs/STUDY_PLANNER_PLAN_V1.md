# Study plan: what it is today, and what the planner adds

Read before building. Queued after the job system — nothing here is started.

---

## Part 1 — The audit

### What `/study-plan` is today

**A generated plan, not an authored one.** Nexora decides the week; the student
tunes it. That distinction is the whole audit.

Six sections on the page:

| Section | What it is |
|---|---|
| **Next two weeks** | the generated blocks, by day |
| **The runway** | what is coming and how much of it is left |
| **Aim it** | focus (all / exams), which courses, and the sentence box |
| **Timetable** | `ClassBlock` rows — lectures, labs, shifts, commutes |
| **When you work** | `StudyPreference` — earliest, latest, session length, breaks, daily cap, days off |
| **Add a class** | the sheet behind the timetable |

Three tables:

- **`ClassBlock`** — a recurring commitment. `weekday`, `start_min`, `end_min`,
  `repeat` (weekly/biweekly), `anchor_date`. It does not schedule anything; it
  *removes* time from the week.
- **`StudyPreference`** — one row per student. When they will actually work.
- **`StudySession`** — the generated blocks. `on_date`, `start_min`, `end_min`,
  `course_full`, `focus`, `reason`, `state`, `actual_minutes`.

Five routes: `GET /study-plan`, `GET /api/study-plan`, `POST
/api/study-plan/brief`, `POST /api/study-plan/ask`, `POST
/api/study-plan/generate`, `POST /api/study-plan/<session_id>` (done/skip with
real minutes).

### The thing that decides the design

**`_save_plan` deletes every `planned` session from today forward, on every
rebuild.**

```python
stale = StudySession.query.filter_by(user_id=user_id).filter(
    StudySession.on_date >= today, StudySession.state == "planned").all()
for row in stale:
    db.session.delete(row)
```

That is correct for generated blocks — a regenerate should replace them — and
it is fatal for authored ones. If a study block lived in `StudySession`, then
pressing **Generate**, or typing any sentence into **Aim it**, or changing a
preference, would silently delete the plan the student wrote. Not on sync. On
the planner's own buttons.

So the new table is not tidiness, it is the feature surviving its first click.
Your "never delete during sync" rule is right and **not sufficient**: the
danger is the planner, not the sync.

### You already have a sentence box, and it does something different

`POST /api/study-plan/ask` takes plain language today. It converts it into
**settings** — `focus`, `courses`, `days`, `max_daily_minutes` — writes them to
`StudyPreference`, and then rebuilds arithmetically. The model never produces a
schedule. `test_the_plan_it_returns_matches_a_plain_rebuild` pins exactly that,
and the rule in CLAUDE.md is explicit: *the AI reads the REQUEST, never writes
the schedule.*

"Plan SYSC2004 and ELEC2501 for the next ten days" already works. What does
**not** work is *ordering* — "SYSC first, then ELEC, then CCDP" has nowhere to
go, because the generator decides order from weight ÷ days remaining.

**That gap is the actual feature.** Worth saying plainly so we build the gap
and not a second copy of the box.

### What is not there at all

- The **calendar does not render study sessions**. Zero references. Item 5 is
  new work, not a style change.
- The **`.ics` feed already exists** — it is not "when we build it". Study
  blocks can go in it immediately, with one consequence in Part 3.
- The **Today screen does not exist yet** (Phase 2). Item 6 depends on it.

---

## Part 2 — The plan

### The model

A study block is **coarser than a session**: a course for a span of days, not a
50-minute slot. "SYSC 2004 for 2 days" is a theme for two days, not a time.

```
StudyBlock
  id, user_id
  course_full            the student's own code, resolved via _resolve_known_course
  position               order in the sequence
  days                   how long it runs
  anchor_date            "" = floats after the previous block
  start_date, end_date   DERIVED and stored, recomputed on every reorder
  part_of_day            "" | morning | afternoon | evening
  note
  state                  planned | done | skipped
  source                 typed | manual
  created_at, updated_at
```

**The one modelling decision worth your call.** You want both "drag to reorder
and the rest shift" *and* "adjust dates". Those fight unless a block knows
which it is:

- a **floating** block starts the day after the previous one ends — reordering
  recomputes it;
- an **anchored** block has a date the student set, and reordering flows around
  it.

Dragging sets order; editing a date sets the anchor. Without the distinction,
either reorder overwrites a date the student typed, or a date freezes the
sequence. I would build it with the distinction, and show anchored blocks with
a small pin.

### Parsing — the same split everything else uses

The model reads the **request**; the server computes every **date**. The
pattern is already in the codebase three times (`_anchor_undated_year`,
`_day_in_stated_week`, `/api/calendar/from-image`).

So Gemini returns **intent**, not dates:

```json
{"blocks": [{"course": "SYSC 2004", "days": 2, "when": "reading week"},
            {"course": "ELEC 2501", "days": 2, "when": ""}],
 "understood": "Two days on SYSC2004 in reading week, then ELEC2501, then CCDP2100.",
 "ambiguities": ["Which week is reading week?"]}
```

and `_resolve_block_dates()` turns that into real dates. Consequences:

- **Course codes resolve against the student's own** (`_resolve_known_course`).
  "SYSC 2004" → SYSC2004A. Storing the literal is the OTHER-bucket bug again:
  it joins to no outline, no deadlines, no content, and looks fixed.
- **"Reading week" is read from settings, never guessed.** Two dates on `User`
  (additive). With them unset, the block comes back in the preview *asking*,
  which is what you specified — and it is the same reason
  `_scheme_is_uncorroborated` refuses an invented weighting.
- `mode="fast"` (flash-lite). This is parsing, not reasoning — the calendar
  measurement showed the reasoning chain costs 5–18× for identical answers.
- `AI_LIMIT` on the route, like every other Gemini-backed one.

Preview always. Nothing saves from the model.

### Smart checks — arithmetic, no model

Three, all over data already held:

1. **Block after the deadline.** `end_date` later than that course's next
   deadline. Uses `load_tasks` + `days_until(iso, today=_student_today(user))`
   — the student's clock, never the server's.
2. **A course with work due soon and no block.** Reuses `_study_items`, which
   already knows weight ÷ urgency.
3. **Nothing on a day the timetable says is full.** `ClassBlock` already
   answers this via `_block_occurs_on`.

Warnings, never refusals. A student is allowed to revise after a deadline —
sometimes the exam is what matters.

### Suggested topics

Reuse `_build_course_brief`, which already asks Gemini **once per course and
caches it** in `study_brief_json`. `test_a_rebuild_never_reaches_gemini` pins
that it is not re-asked per rebuild, and this must not break that. Lecture
titles come from `CourseContent` through `_material_section`, so the Lectures /
Recordings / Labs split we just shipped is reused rather than re-derived.

### How the two planners coexist

This is the part most likely to go wrong. **Authored blocks are commitments,
generated sessions are suggestions.** The cleanest integration reuses the
machinery that already exists:

- a `StudyBlock` **biases the generator** for its dates — on those days, that
  course's work is planned first;
- the generated plan still fills the actual time slots, because it is the thing
  that knows about `_free_slots`, buffers and the daily cap;
- `_save_plan` never sees `StudyBlock` rows, so a regenerate cannot touch them.

The alternative — authored blocks simply *replace* the plan for their days — is
simpler and I think worse: it throws away the weights, the at-risk boost and
the revision ladder for exactly the weeks the student cared enough to plan.

**Your call, and it is the one decision I would not make alone.**

### The page

The existing page already carries six sections and the grade calculator taught
us what happens when a page shows everything at once. I would put **the
authored plan at the top** when one exists, with the generated plan under it,
and a real empty state above both: what the box is for, one example sentence,
and the manual form one click away.

### Calendar and feed

- Study blocks render on `/calendar` **distinct by treatment, never by hue** —
  the course colour is a promise the legend makes, and the FINAL badge work
  established that emphasis changes weight, not colour. A dashed edge and a
  "yours" label.
- The `.ics` feed exists, so they can be exported now. **One consequence:** the
  feed URL is the whole credential and is deliberately unauthenticated, so
  anything in it is readable by anyone holding the link. Deadlines are already
  there; study blocks are a student's own notes about their time. I would
  include them and say so on the Sync sheet, but it is worth a sentence from
  you.

### Tests

- Parsing: ordering without dates, "2 days" vs "2 weeks", an unknown course,
  reading week unset, a course named loosely ("sysc 2004", "ELEC"), a sentence
  with no course at all, and the model returning a one-element list wrapped in
  a list (flash-lite does this — `_outline_amendment_edits` already unwraps it).
- Conflicts: block after the deadline, block before it, course with work and no
  block, an exam inside a block's range.
- Survival: **a regenerate, an Ask sentence and a sync all leave `StudyBlock`
  untouched.** That is the test that matters most.
- Reorder: moving block 1 to position 3 shifts the floating blocks and leaves
  an anchored one where it was.

### Migration

`StudyBlock` is a new TABLE, so `create_all()` handles it — no
`ensure_schema_columns` entry needed (that map is only for new columns on
existing tables). The reading-week dates **are** new columns on `user`, so they
need two lines in the map, and the proof is a simulation against an old-shaped
table, not a reading of the diff.

---

## Part 3 — What I need from you

1. **Do authored blocks bias the generator, or replace the plan for their
   days?** I recommend bias.
2. **Study blocks in the `.ics` feed?** The feed is already built and already
   public-by-token.
3. Item 6 (Today screen) lands with Phase 2, not with this. Confirm that is
   the order you want.

Everything else I would build as specified.
