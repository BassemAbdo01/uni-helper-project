# Nexora roadmap

The live queue. Updated after a full production walkthrough on 2026-10-22.

**Calendar export and the study planner are built** and have been removed from
the roadmap. What replaces "study planner" is narrower: teach Study plan's
*Ask Nexora* box to order work ("SYSC 2004 for 2 days in reading week, then
ELEC 2501"), and draw study blocks and the class timetable on the Calendar.

---

# Part 1 — Bugs, ahead of everything

**Part 1 is done: B1 to B7 have all shipped.** Their sections are kept below
as the record of what was wrong and why each fix took the shape it did; the
live reasoning is in CLAUDE.md.

Two things the last three turned up that the sections below do not say:

- **B6 does not use `HiddenCourse`,** which is what B6 below prescribes. A
  tombstone means the course row is never created, and with no row there is
  nothing for the student to restore -- the roadmap's own reason for letting
  it guess. `SyncedCourse.is_hidden` keeps the name, the term and the link,
  and the dashboard lists what was filed away with a Put back button.
- **B5 was three defects, not one.** Besides the `-7` cut it turned out that
  completed work had never been drawn on the calendar at all, and that the
  route measured today from `date.today()` -- the server's clock -- while
  every entry inside the grid was already on the student's.


Diagnosed before planning. Three of the seven were already findable in the
code; those read as fact below, and the rest say what still needs measuring.

## B1 — A conditional scheme wins without its condition · **correctness**

ELEC2501B reports **83%** from "Missed Midterm Scheme" with a midterm mark
entered. The standard scheme gives **82.2%**.

**Cause, from the code.** `_compute_official_result` picks the winner with:

```python
complete = [x for x in evaluated if x['complete'] and x['eligible']]
best = max(complete, key=lambda x: x['grade'])
```

Every eligible option competes and the **highest grade wins**. Eligibility has
exactly two shapes today, and neither can express this scheme's condition:

- `eligibility.requires_completed` + `from_components` — "you must HAVE sat N
  of these". It can require a mark's **presence**, never its **absence**.
- `scheme_policies` of type `scheme_gate` — gates one option on another
  option's **grade threshold**, not on whether a component has a mark.

A "Missed Midterm" scheme therefore carries no eligibility at all, so
`required = 0`, `completed >= 0` is always true, and it is always eligible.
Omitting the midterm redistributes its weight, which is exactly why it scores
*higher* — so `max()` prefers it every time. **The scheme that wins is the one
that should be impossible.**

**Fix, in two halves, because one of them must not depend on the model.**

1. A new eligibility shape — `requires_missing` / `only_if_absent`, naming the
   components that must have **no mark**. Schema addition, extraction rule,
   `_evaluate_option` enforcement.
2. **An arithmetic guard that works with no model help at all.** A scheme
   whose name says *missed / deferred / absent / did not write* and whose
   component list omits a component that **does** have a mark is refused. This
   is the `_scheme_is_uncorroborated` pattern: the failure mode is the model
   not emitting a rule, so the guard cannot be a prompt rule. Half 1 makes it
   right; half 2 makes it safe when half 1 is absent — including on every
   analysis already saved.

**The audit you asked for** has to be a script, not a reading: I cannot query
production. `scripts/audit_scheme_eligibility.py --plan` will list every saved
`grading_options` set where an option has no eligibility and omits a marked
component, per course, read-only.

**Risk if rushed:** this decides what a student thinks their grade is.
`tests/test_two_finals.py` is the model — every behavioural test confirmed
failing against the old code first.

## B2 — "0 due this week" with work due in one and three days · **confirmed**

```python
cards = view_filter(cards, view)      # view defaults to "today"
groups = group_for_ui(cards)
urgent = len(groups["overdue"]) + len(groups["today"]) + len(groups["panic"])
week = urgent + len(groups["soon"])
```

`view_filter(cards, "today")` keeps only `dleft is None or dleft <= 0`.
Everything due **tomorrow or later is removed before the week count is
computed**. The queue beneath it is built from a different list —
`view_filter(build_grouped_cards(raw_tasks), "all")`, hardcoded — so the page
lists five items the stat says do not exist.

Same class as "37 courses in the extension, 11 on the dashboard": two numbers
from two lists on one screen. **Fix:** compute the stat from the unfiltered
list the queue already uses, over an explicit 0–7 day window on the student's
clock (`_student_today`, never `date.today()`).

## B3 — "Week 7 Quiz … Final Subtopic Report …" tagged FINAL · **confirmed**

```python
("FINAL", re.compile(r"\bfinal(\s+(exam|examination|test|assessment))?\b", re.I))
```

The qualifier is **optional**, so a bare "Final" anywhere in a title matches —
including "Final Subtopic Report", "Final Submission", "Final Draft".
`_NOT_AN_EXAM` does not save it: that list is about *practice* and *review*.

Exactly the "Final Lab Exam" problem one screen over, where `_final_name_score`
already exists to score how final-exam-like a name is. **Fix:** require the
qualifier, or the word standing alone as the assessment — and reuse
`_final_name_score` rather than writing a third opinion about the word "final".
`test_finals_and_colours.py` already pins `"Practice final" -> ""`; this adds
the report/submission/draft cases.

## B4 — An assignment and its dropbox are two deadlines · **confirmed**

"Assignment 3: Gate-level implementation" and "Assignment 3: Place to upload
your solution" are two D2L items with two ids, so `_import_deadlines` makes two
`Task` rows with different `source_uid`s and different `group_id`s.

`make_group_id(course, base_title)` is what collapses duplicates today, and
`base_title` differs, so they never merge. **Fix:** a dropbox companion
("place to upload", "submission", "submit here") whose stem matches a sibling
in the same course folds into that sibling's group rather than creating its
own. The sibling keeps the link that actually opens the work.

**Care:** this must not merge two genuinely different assessments that share a
prefix. The test is the one that matters here.

## B5 — The calendar hides the past

`view_filter` drops `dleft < -7` and the calendar has its own filter. Past
work should render **muted**, completed ones included. Needs measuring before
I promise a one-line change: the month grid, the day cell classes and the
`hasmajor`/`hasfinal` marks all read the same list.

## B6 — Non-academic Brightspace "courses"

EHS, AODA, orientation and training shells are real org units and arrive like
any course. **`HiddenCourse` already exists** — this should set it, not invent
a parallel mechanism, so a wrong guess stays reversible from the UI.

Decided on a **positive signal with a safe default**, the Lectures lesson: a
short list of what *is* non-academic, everything else stays academic. A
blacklist that hides a real course is worse than a list that misses a training
shell. Grouping by term and cleaning display names reuses `_term_meta` and
`_display_code`, which already exist.

## B7 — "New lecture" and "Review recommended" are one event

`announce()` writes two `AcademicUpdate` rows per lecture: `new_content` and
`ai_recommendation`. **Fix at the writer, not the renderer** — one row, not two
merged at display. That table reached 786 MB; writing half as many rows for
lectures is the right direction regardless of the UI.

---

# Part 2 — Quick wins, after the bugs

**All three have shipped.** The live reasoning is in CLAUDE.md. What the
sections below do not say:

- The **Carleton preset** turned out to be `DEFAULT_GRADING_SCALE` exactly --
  checked against Carleton's own published table. Nexora's built-in default has
  always been Carleton's scale, so what the preset buys is `configured`, not
  the numbers.
- The **outline-date** gate needed three states, not two. "Current term" and
  "another term" are not the only answers: a student who pasted an outline into
  the grade calculator has no course row to place it by, and excluding that
  swallowed an outline they had just uploaded. The preview offers an
  unplaceable course; only the auto-add refuses it.

- ~~**Current grade + letter on each dashboard course card.**~~ Must call
  `_compute_official_result`, never a second copy of the pass-rule maths —
  that is how two screens come to disagree about a pass.
- ~~**Carleton grading-scale preset** in Settings.~~
- ~~**Outline dates auto-added to the calendar, with undo**~~, replacing the
  review banner. Note: the *old* "exams found in your course material" panel
  was deleted for offering year-less dates from first-year announcements. This
  is different — outline dates are current-term and dated — but it must stay
  current-term only and year-aware, or it repeats that.

---

# Part 3 — The queue

1. ~~Ephemeral-disk fix~~ — **live** since 2026-10-27. The `--plan` row
   count is still unrun, but it only reports; nothing waits on it.
2. ~~**Outline finder** + instructor / email / office-hours extraction~~ --
   **done.** Both are server-side, so they fix rows already in the database
   with no extension release. The live reasoning is in CLAUDE.md; what the
   line above did not anticipate:
   - the finder had to be made **67x faster** before it could ship, and the
     cost was density, not length -- `_local_outline_analysis` is superlinear
     in grading-label-like lines, not in characters;
   - five call sites answered "where is the outline" five ways, and two of
     them meant a student who picked their outline could not then ASK it
     anything;
   - the unprompted analysis could delete hand-entered marks, which was
     already true on the narrower candidate list.
3. **Brightspace grade sync** — audit first
4. **Job system** — heartbeat, 3-attempt cap, idempotency, startup requeue, env flag off
5. **Ask Nexora ordering** + study blocks and timetable on the Calendar
6. **Export + archive** — transient storage, expiring link is the policy
7. **Email my prof** — v1 is the "mark as hard" toggle only

## Settled, so it is not re-litigated

- Analysis cache is **cross-user**, keyed on text hash + scale + prompt version;
  only validated, non-low-confidence raw AI analyses; user corrections never
  enter it.
- **No stored course files.** Export collects via the extension in the
  foreground; the archive keeps the extracted text.
- **Tutor history stays in `localStorage`.** Storing students' questions
  server-side is a privacy decision not yet made.
- **Grade → lecture mapping deferred** until Brightspace grade sync lands.
- If Microsoft blocks Outlook consent, **ask Carleton IT** rather than routing
  around them with the extension.

## Still blocking

- **`--plan` row count from production** before the ephemeral-disk fix pushes.
- **`AcademicUpdate` `happened_at`** — add the column, or join to the content
  row's `published_at`? Gates Phase 2a.
