"""Which saved analyses could report a grade from a scheme that does not apply.

B1: the official grade is `max(grade)` over every option that is complete and
eligible, and a conditional scheme -- "Missed Midterm Scheme", "Deferred Exam
Scheme" -- carried no eligibility the machinery could read. `requires_completed`
asks whether a mark is PRESENT and a `scheme_gate` compares another option's
GRADE; neither can say "only when this is absent". So such an option was always
eligible, and because it omits an assessment its weight is redistributed --
which is exactly why it scores HIGHER and wins.

The code fix gates those options on the student's own "I missed this" flag.
This says who was affected before it landed, and whether any student is
currently being shown the wrong number.

READ-ONLY. It opens a session, runs queries and writes nothing. There is no
--apply, deliberately: nothing here should be repaired automatically, because
the right answer per course is the student's to give.

    python scripts/audit_scheme_eligibility.py --plan
    python scripts/audit_scheme_eligibility.py --plan --verbose

Run it against production the way the repair script is run:

    DATABASE_URL="<render postgres url>" SECRET_KEY="anything" \\
        venv/Scripts/python.exe scripts/audit_scheme_eligibility.py --plan
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as nexora  # noqa: E402


def _examine(profile):
    """(course, conditional_names, currently_wrong, best_name, standard_name).

    `currently_wrong` is the one that matters: with the marks on file TODAY,
    would a conditional scheme have been the winner?
    """
    try:
        analysis = json.loads(profile.grading_json or "{}")
    except Exception:
        return None
    options = analysis.get("grading_options") or []
    if len(options) < 2:
        return None

    rows = nexora.GradeEntry.query.filter_by(
        user_id=profile.user_id, course_full=profile.course_full).all()
    row_map = nexora._grade_row_map(rows)

    conditional, evaluated = [], []
    for option in options:
        is_conditional, _eligible, _note = nexora._missed_scheme_state(
            option, analysis, row_map)
        name = str(option.get("option_name") or "Grading option")
        if is_conditional:
            conditional.append(name)
        ev = nexora._evaluate_option(option, row_map, analysis=analysis)
        evaluated.append((name, ev, is_conditional))

    if not conditional:
        return None

    # What the OLD code would have picked: complete, ignoring the missed gate.
    old_pool = [(n, e) for n, e, _c in evaluated if e["complete"]]
    old_best = max(old_pool, key=lambda x: x[1]["grade"])[0] if old_pool else None
    # What it picks now.
    new_pool = [(n, e) for n, e, _c in evaluated if e["complete"] and e["eligible"]]
    new_best = max(new_pool, key=lambda x: x[1]["grade"])[0] if new_pool else None

    was_wrong = bool(old_best and old_best in conditional and old_best != new_best)
    return {
        "user_id": profile.user_id,
        "course": profile.course_full,
        "conditional": conditional,
        "options": [(n, e["grade"], e["complete"], e["eligible"]) for n, e, _c in evaluated],
        "old_best": old_best,
        "new_best": new_best,
        "was_wrong": was_wrong,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="required; this script only reads")
    parser.add_argument("--verbose", action="store_true", help="print every affected course")
    args = parser.parse_args()
    if not args.plan:
        parser.error("pass --plan. There is no --apply; nothing here is repaired automatically.")

    with nexora.app.app_context():
        profiles = nexora.CourseOutlineProfile.query.all()
        multi, flagged, wrong = 0, [], []
        for profile in profiles:
            try:
                found = _examine(profile)
            except Exception as exc:                       # noqa: BLE001
                print("  could not read %s for user %s: %s"
                      % (profile.course_full, profile.user_id, exc))
                continue
            if found is None:
                continue
            multi += 1
            flagged.append(found)
            if found["was_wrong"]:
                wrong.append(found)

        print("outline policies on file                    : %d" % len(profiles))
        print("  with two or more grading schemes          : %d" % multi)
        print("  carrying a conditional (missed/deferred)  : %d" % len(flagged))
        print("  SHOWING THE WRONG SCHEME WITH TODAY'S MARKS: %d" % len(wrong))

        if wrong:
            print("\nThese students were being shown a grade from a scheme that")
            print("does not apply. The fix corrects them with no further action.\n")
            for f in wrong:
                print("  user %s | %s" % (f["user_id"], f["course"]))
                print("      was: %s   ->   now: %s" % (f["old_best"], f["new_best"]))
                for name, grade, complete, eligible in f["options"]:
                    print("        %-34s %6.2f  complete=%-5s eligible=%s"
                          % (name[:34], grade, complete, eligible))

        if args.verbose and flagged:
            print("\nEvery course carrying a conditional scheme:")
            for f in flagged:
                print("  user %s | %-12s | %s" % (f["user_id"], f["course"],
                                                  ", ".join(f["conditional"])))

        if not flagged:
            print("\nNo saved analysis carries a conditional scheme.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
