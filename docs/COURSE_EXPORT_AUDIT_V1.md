# Course export and archive: what we actually hold

Audit first, as asked. Queued behind the study planner — nothing started.

The headline: **we store almost no files, the few we do store are deleted by
every deploy, and Brightspace grades are not synced at all.** Two of the three
things `grades.pdf` promises do not exist in the database.

---

## Part 1 — What we sync today

### Text, yes. Files, almost never.

Every synced item is a `CourseContent` row holding **extracted text**, capped
per kind by `_CONTENT_TEXT_CAP`:

| Kind | Cap | Why |
|---|---|---|
| `outline`, `lecture` | 120,000 | analysed, quoted, turned into flashcards |
| `page`, `practice`, `resource` | 48,000 | the tutor reads ~45,000 of it |
| `assignment`, `quiz`, `announcement` | 12,000 | the date is the point |

The whole `course_content` table was **20.8 MB across 25,682 rows** at the last
measurement, and the largest single body in the entire database is a 20 KB
outline. That is a study guide's worth of material. It is not an export.

### File bodies exist as a path, not as a habit

`/api/sync/file` → `SyncedFile` → `static/nexora_uploads/<user>/<course>/`
works, with a 25 MB cap, SHA-256 dedupe and per-user folders. **Nothing calls
it during a sync.**

It fires only **on demand**, when a feature hits a row whose `content_text` is
under ~120 characters:

```
tutor / outline analyzer / study tools
  → 409 {"needs_content": true, "content_request": {...}}
  → page asks the extension (NEXORA_FETCH_AND_PROCESS_LECTURE)
  → fetchAndProcessLecture() downloads the blob, POSTs /api/sync/file
```

So a typical course holds **zero** stored files until someone opens the tutor
on a specific lecture. "All synced files" in a zip is not a packaging job; the
files are not there.

### A live bug this audit found

Files are written to `app.root_path/static/nexora_uploads/...`. **Render's
filesystem is ephemeral** — wiped on every deploy and every restart unless a
persistent disk is attached, and none is.

It is worse than losing a cache, because the upload route rewrites the row:

```python
content.source_url = stored_url      # /static/nexora_uploads/...
```

So after the next deploy that row points at a file that no longer exists — a
**broken link where a working Brightspace link used to be**. Every deploy today
silently breaks every file a student has ever had the tutor read.

Recoverable: `SyncedFile.original_source_url` still holds the Brightspace URL,
so a repair pass can put the links back. Worth doing regardless of whether we
build any of this.

### Grades: hand-entered, and Brightspace supplies none

`GradeEntry` is written from three places: the student typing a mark, the
outline analysis creating the component rows, and `_import_canvas_grades`.

**There is no Brightspace grade sync.** For a Carleton student every score in
Nexora is one they typed in themselves.

And there is no feedback anywhere. `GradeEntry.notes` is a 255-character field
the *student* writes. Nothing stores instructor comments, rubric results, quiz
attempts or submission feedback — not in any model, not in either importer.

### Gap table against your spec

| `grades.pdf` wants | Status |
|---|---|
| components, weights | **have** — outline analysis |
| scores | **have for Canvas; hand-entered for Brightspace** |
| feedback | **missing entirely** — no model, no sync, no scrape |
| final grade | **have** — `_compute_official_result` |

| `export.zip` wants | Status |
|---|---|
| folders per module/week | **have** — `module_name` breadcrumbs, `_organise_course_items` |
| sensible file names | **have** — `title` + `file_ext` |
| all synced files | **missing** — on-demand only, and wiped each deploy |
| study-guide.pdf | **buildable now** — from `content_text` |

| Archive wants | Status |
|---|---|
| past courses don't disappear | **mostly have** — `_term_meta` groups by term, dashboard already opens on the current one |
| content stays searchable | **have** — text is retained |
| tutor keeps working | **have** — it reads `content_text` |
| cross-course tutor | **missing** — the tutor is course-scoped |

One thing that already protects the archive: the **S1 replace floor** shipped
this week refuses to delete a course's content when a sync arrives with less
than half of what is on file. A past course the student has lost Brightspace
access to can no longer be emptied by a re-sync. That was not the reason for
building it, but it is load-bearing here.

---

## Part 2 — Storage, with numbers

Sizing from the real figures in CLAUDE.md: ~36 items in one real course
workspace, and of 210 topics across ten courses, **83 were downloadable
documents** — about 40%.

- ~14 downloadable files per course
- mixed PDF/PPTX average ≈ 4 MB
- → **~56 MB per course per term**
- → **250–500 MB per student per term**, call it 350 MB

For scale: that is **more than ten times the entire current database, per
student, per term** — and the archive requirement means it never shrinks.

| | Files | Render disk @ $0.25/GB | Cloudflare R2 @ $0.015/GB |
|---|---|---|---|
| 10 testers, 1 term | 3.5 GB | $0.88/mo | $0.05/mo |
| 1,000 students, 1 term | 350 GB | **$88/mo** | $5/mo |
| 1,000 students, 4 terms archived | 1.4 TB | **$350/mo** | $21/mo |

A Render persistent disk also costs something that is not money: attaching one
**pins the service to a single instance and disables zero-downtime deploys**.
That is a large architectural price for a feature nobody has asked for twice.

### What I would do instead, and why it is not a compromise

**Do not store files at all. Collect them for the duration of an export.**

The files can only be fetched while the student's browser has a live
Brightspace session — a background job on Render cannot reach Carleton, ever.
That constraint looks like a problem and is actually the answer:

1. Press **Export course**. The page asks the extension to fetch that course's
   files, exactly as the tutor already does, with a progress bar — this part
   must be foreground, because only the browser can do it.
2. Each file is POSTed to the existing `/api/sync/file`, into a
   **per-export scratch area**, not the permanent one.
3. The **background job** zips what arrived, adds `grades.pdf` and
   `study-guide.pdf`, and writes the archive.
4. The download link expires — and the expiry is not decoration, it is the
   **storage policy**. When it lapses, the scratch files and the zip are
   deleted.

Steady-state storage: **one in-progress export**, not 350 MB per student per
term. The archive promise is still kept, because what makes a past course
useful — the text, the outline, the grades, the tutor — is already retained and
costs 20 MB across every account we have.

If you later decide files must persist, the same pipeline writes to R2 instead
of scratch, and the only change is the retention rule.

---

## Part 3 — The rest of it

**Export job.** Uses the job system, so this is correctly queued behind it. It
needs the same heartbeat, retry cap and idempotency you specified there — a
re-run must not produce two zips. One extra rule: the zip is built from files
already uploaded, so the job never makes a network call to a university.

**Download link.** Same shape as the `.ics` feed token: unauthenticated,
credential-in-path, rotatable, expiring. Unlike the feed it must also be
**single-course and single-user** — your rule, and the right one. A course file
arrives through one student's authenticated session; serving it to another
student would redistribute the instructor's material and leak who fetched it.
This is the opposite of the outline cache, where what is shared is Nexora's
*analysis*, not the document.

**Past courses.** Cheap, because `_term_meta`, `_term_sort_key` and
`_current_term_label` already exist and the dashboard already hides finished
terms behind `#termPick`. Mostly a surface, not a mechanism.

**Cross-course tutor.** Real work. The tutor is course-scoped today; widening
it means selecting across courses within a prompt budget — it already reads
~45,000 characters for one course. Needs a retrieval step, not a bigger prompt.

**End-of-term nudge.** Depends on the Today screen (Phase 2) *and* on knowing
when term ends. `_term_meta` derives a label, never dates. So it needs term
dates — the same settings shape as reading week in the study planner plan, and
worth doing once for both.

---

## Part 4 — What I need from you

1. **Transient export storage, or persistent file storage?** I recommend
   transient, for the numbers in Part 2.
2. **`grades.pdf` without feedback** — ship it with components, weights, scores
   and the final grade, and leave feedback out until something syncs it? Or
   hold the PDF? Scraping Brightspace grades is its own project.
3. **The ephemeral-disk bug** — want me to fix that independently and soon? It
   is breaking links today, on every deploy, and the repair is small because
   `original_source_url` survived.

Everything else I would build as specified, after the job system and the
planner.
