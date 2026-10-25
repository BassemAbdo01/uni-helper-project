"""Put back the university's link on any row that had it overwritten.

`/api/sync/file` used to replace `CourseContent.source_url` with a path under
`static/nexora_uploads/`, and replace `content_text` with a JSON stub. Render's
filesystem is wiped by every deploy, so such a row points at a file that no
longer exists -- a broken link where a working Brightspace link used to be --
and has lost the extracted text that the tutor, the summariser and the outline
analyser read.

The route no longer does either, and writes to `CourseContent.stored_url`
instead. This repairs rows written before that.

**It may well find nothing.** The extension posts to
`/api/sync/process-content`, which extracts text and stores no file, so on a
database that has only ever been synced by a current extension there is
nothing to repair and `--plan` prints zeroes. Older builds did call the other
route, which is why this exists and why it runs read-only first.

    python scripts/repair_stored_urls.py --plan     # count, change nothing
    python scripts/repair_stored_urls.py --apply    # repair, in batches

Safety:

- A row is only repaired when its `SyncedFile` carries an
  `original_source_url` that passes `_lms_link` -- a real http(s) LMS link. A
  row with no recoverable original is LEFT ALONE rather than blanked: a dead
  link at least names the item, and an empty one renders no link at all.
- The static path is moved into `stored_url`, never discarded, so nothing is
  lost by running this.
- The JSON stub is cleared only when `content_text` really is that stub. Any
  real text is untouched. Clearing it puts the row back under the ~120
  character threshold, so the next time a feature needs the body the
  `needs_content` path re-fetches it from Brightspace.
- Commits in batches, for the reason `scripts/db_usage.py` does: one
  transaction holding every row builds a single enormous WAL record.

This rewrites rows. Take a backup first.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as nexora  # noqa: E402

STATIC_PREFIX = "/static/nexora_uploads/"
BATCH = 200


def _is_stub(text):
    """True only for the exact shape the old route wrote."""
    body = (text or "").strip()
    if not body.startswith("{") or "stored_file" not in body:
        return False
    try:
        parsed = json.loads(body)
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("stored_file") is True


def _plan():
    rows = nexora.CourseContent.query.filter(
        nexora.CourseContent.source_url.like(STATIC_PREFIX + "%")).all()
    repairable, orphaned, stubs = [], [], 0
    for row in rows:
        record = nexora.SyncedFile.query.filter_by(
            user_id=row.user_id, source_uid=row.source_uid).first()
        original = nexora._lms_link((record.original_source_url if record else "") or "")
        if _is_stub(row.content_text):
            stubs += 1
        (repairable if original else orphaned).append((row, original))
    return rows, repairable, orphaned, stubs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="count, change nothing")
    parser.add_argument("--apply", action="store_true", help="repair the rows")
    args = parser.parse_args()
    if not (args.plan or args.apply):
        parser.error("pass --plan or --apply")

    with nexora.app.app_context():
        rows, repairable, orphaned, stubs = _plan()
        print("rows whose link points at Nexora's own copy : %d" % len(rows))
        print("  repairable (a real LMS link survived)     : %d" % len(repairable))
        print("  orphaned   (no original; left untouched)  : %d" % len(orphaned))
        print("  carrying the JSON stub instead of text    : %d" % stubs)
        if orphaned:
            print("\n  orphaned rows, left exactly as they are:")
            for row, _ in orphaned[:10]:
                print("    %s | %s" % (row.course_full, (row.title or "")[:60]))
            if len(orphaned) > 10:
                print("    ... and %d more" % (len(orphaned) - 10))
        if not args.apply:
            print("\n--plan only. Nothing was changed.")
            return 0
        if not repairable:
            print("\nNothing to repair.")
            return 0

        done = 0
        for start in range(0, len(repairable), BATCH):
            for row, original in repairable[start:start + BATCH]:
                # Keep the copy's path rather than dropping it on the floor.
                if not (row.stored_url or "").strip():
                    row.stored_url = row.source_url
                row.source_url = original
                if _is_stub(row.content_text):
                    row.content_text = ""
                row.updated_at = nexora.datetime.utcnow()
                done += 1
            nexora.db.session.commit()
            print("  committed %d / %d" % (done, len(repairable)))
        print("\nrepaired %d rows. %d left alone for having no original." %
              (done, len(orphaned)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
