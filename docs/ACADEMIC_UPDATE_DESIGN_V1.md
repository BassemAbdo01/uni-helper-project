# AcademicUpdate: dedupe and retention, before Phase 2a writes to it

This table reached **786 MB / 2,581,147 rows**, 96% of the database, and Render
suspended production. Phase 2a proposes writing two new event types into it.
This is the design that has to be agreed first.

---

## 1. The current dedupe is wrong for Phase 2a, and the fault is hidden

`announce()` keys on **`(related_uid, update_type)`**. That means an item can
announce `content_updated` **exactly once, ever**. The second time a lecture
changes, nothing is written and nothing is shown.

Today that is invisible, because nothing reads these rows for display — the
dashboard's "Recent updates" reads the content rows instead. The moment "What
changed since your last visit" reads this table, it becomes a silent
correctness bug: the second change to anything never appears.

So the key cannot stay as it is. But it also cannot simply be loosened, because
"announce every time" is precisely what caused the outage.

**Proposed key: `(user_id, update_type, related_uid, change_hash)`**

`change_hash` is a short hash of **what the change was**, not that one happened:

| update_type | change_hash covers |
|---|---|
| `new_content` | nothing (`""`) — an item is new once |
| `content_updated` | the content fingerprint already computed (`content_hash`) |
| `grade_changed` | course, component, the new score and max |
| `deadline_moved` | course, task group, the new date and time |
| `outline_detected` | nothing — a course's outline is detected once |

This is the property that matters: **re-sending the same state is free, a
genuinely new state is one row.** Growth is bounded by how often a student's
work actually changes, not by how often they sync — which was the whole defect.

It needs one additive column, `change_hash VARCHAR(40) DEFAULT ''`, and the
existing lookup widens to three fields. The pre-pass already loads only the
uids in the current payload, so the cost does not change.

---

## 2. What counts as "the same" update

Two rows are the same update when all four match. Concretely:

- The same lecture re-scraped unchanged on 40 syncs → **1 row**.
- A lecture edited by the instructor twice → **2 rows**, correctly.
- A mark entered, then corrected → **2 rows**; the student wants to see both.
- A mark re-saved with the same value (the calculator's debounce) → **1 row**.
- A deadline that moves Tue → Thu → Tue → **3 rows**; the last is a real move
  back, not a duplicate.

---

## 3. Retention: a window is not enough, and we already know why

`ACADEMIC_UPDATE_KEEP_DAYS` (45) prunes on every sync. **2,516,112 of the
2,581,147 rows were inside that window.** A time window bounds a table that
grows slowly; it does nothing to one growing by 150,000–220,000 rows a day.

So retention is two rules, not one:

1. **Time** — keep 45 days. Unchanged.
2. **A hard per-user row cap** — `ACADEMIC_UPDATE_MAX_PER_USER`, default
   **400**, keeping the newest. Pruned on the same pass as the time window.

The cap is what makes the table's size a function of the number of users rather
than of anyone's behaviour. It cannot be defeated by a loop, a bad extension
build, or a dedupe that stops working — which is the failure mode that actually
happened.

400 is sized against what the screen reads: "What changed since your last
visit" shows a handful of items, and a student returning after a fortnight has
far fewer than 400 real changes. If it is ever hit, the oldest rows go first,
and they are the ones already past the point of being useful.

---

## 4. Expected size

Per user per term, under the proposed key, taking the real figures from the
account that caused the outage (~1,200 content items, 6 courses):

| Event | Per term | Why |
|---|---|---|
| `new_content` | ~1,200 | one per item, once |
| `ai_recommendation` | ~240 | lectures only |
| `content_updated` | ~240 | real instructor edits, not syncs |
| `outline_detected` | ~6 | one per course |
| `grade_changed` **(new)** | ~150 | ~8 components × 6 courses, plus corrections |
| `deadline_moved` **(new)** | ~25 | genuinely rare |
| | **~1,860** | before any pruning |

The per-user cap holds the stored figure at **400**, so:

| Users | Stored rows | At ~200 B/row |
|---|---|---|
| 10 (today) | 4,000 | ~0.8 MB |
| 1,000 | 400,000 | ~80 MB |
| 10,000 | 4,000,000 | ~800 MB |

Against the 5 GB the database now has, that is safe to ten thousand students.
The old behaviour reached 786 MB on **one** account's syncs.

The two new types add ~175 rows per user per term — under 10% — and the cap
means they cannot add anything to the stored total at all.

---

## 5. What I would build, in order

1. `change_hash` column (additive) + widen the `announce()` key.
2. The per-user cap, pruned beside the existing time window.
3. A test that re-running an identical sync N times writes zero new rows, and
   that a genuine second change to one item writes exactly one. Both were true
   in neither direction before: the first was the outage, the second is the
   silent bug in §1.

Only then does Phase 2a write `grade_changed` and `deadline_moved`.

**One open question for you.** `created_at` is stamped at sync time, not when
the thing happened (finding S7). "Since your last visit" must diff on the
source's own date or a student returning after a week sees their entire term as
new. For grades and deadline moves Nexora *is* the source, so its own clock is
correct; for content it is not. I plan to store an explicit `happened_at`
(additive) and diff on that, falling back to `created_at`. Say if you would
rather I reuse `published_at` from the content row instead — it is the same
value, but it costs a join on the one screen that has to be fast.
