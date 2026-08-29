"""Score the outline analyzer against structurally varied outlines.

Not part of the pytest suite: every case is a live Gemini call, so this costs
quota and takes minutes. Run it deliberately, before and after touching the
prompt, and compare.

    set -a; . ./.env; set +a
    venv/Scripts/python.exe evals/run_outline_eval.py
    venv/Scripts/python.exe evals/run_outline_eval.py --case overflow_cap --raw

A case passes only when the weights are right AND the mechanism that case
exists to test was actually recorded. Extracting four numbers while dropping
the rule that governs them is scored as a miss, because that is exactly the
failure a student would never notice until their grade was wrong.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from evals.outline_corpus import CASES  # noqa: E402


def _norm(name):
    return re.sub(r"[^a-z ]", " ", str(name).lower())


def _find(components, needle):
    """Loose name match -- 'Final Examination' must satisfy an expected 'final'."""
    for comp in components:
        if needle in _norm(comp.get("name")):
            return comp
    return None


def _is_bonus(comp):
    """Match the same signal app.py uses to separate bonus weight from the base."""
    blob = f"{comp.get('name') or ''} {comp.get('category') or ''}"
    return bool(re.search(r"bonus|extra\s*credit", blob, re.I))


def _walk(node):
    """Yield every string in a nested structure, for signal keyword checks."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def score(case, parsed):
    expect = case["expect"]
    problems = []

    all_components = [c for c in (parsed.get("components") or []) if isinstance(c, dict)]
    bonus_components = [c for c in all_components if _is_bonus(c)]
    # Everything below reasons about the base scheme; a bonus sits on top of it
    # and legitimately pushes the achievable total past 100.
    components = [c for c in all_components if not _is_bonus(c)]

    for needle, weight in (expect["components"] or []):
        match = _find(components, needle)
        if match is None:
            problems.append(f"missing component ~{needle!r}")
            continue
        try:
            got = float(match.get("weight") or 0)
        except (TypeError, ValueError):
            got = -1.0
        if abs(got - weight) > 0.01:
            problems.append(f"{match.get('name')!r} weight {got} != {weight}")

    if expect["components"]:
        extra = len(components) - len(expect["components"])
        if extra > 0:
            names = [c.get("name") for c in components]
            problems.append(f"{extra} unexpected component(s): {names}")
    elif expect["components"] is not None and components:
        # An empty list means the outline states no weights, so any component is
        # invented. None means the top-level list is not what this case is about
        # -- for a multi-scheme outline it is legitimately ambiguous which scheme
        # belongs there, and the per-option checks below carry the real assertion.
        problems.append(f"invented {len(components)} component(s) from an outline with no weights")

    if expect.get("total") is not None:
        total = sum(float(c.get("weight") or 0) for c in components)
        if abs(total - expect["total"]) > 0.01:
            problems.append(f"weights total {total} != {expect['total']}")

    for banned in expect.get("must_not_contain", []):
        if _find(components, banned) is not None:
            problems.append(f"counted excluded item ~{banned!r} in the base scheme")

    if expect.get("bonus"):
        needle, amount = expect["bonus"]
        match = _find(bonus_components, needle)
        if match is None:
            problems.append(f"bonus ~{needle!r} was dropped; a student who earned it would be under-reported")
        else:
            got = float(match.get("weight") or 0)
            if abs(got - amount) > 0.01:
                problems.append(f"bonus {match.get('name')!r} weighted {got}, expected {amount}")
    elif bonus_components and expect.get("no_bonus_expected"):
        problems.append(f"invented a bonus: {[c.get('name') for c in bonus_components]}")

    for signal in expect.get("signals", []):
        if signal == "cap":
            # No dedicated field exists yet; accept the cap surfacing anywhere.
            blob = " ".join(_walk(parsed)).lower()
            if not re.search(r"11\.5|maximum|max of|cap", blob):
                problems.append("cap never recorded anywhere in the output")
        elif not parsed.get(signal):
            problems.append(f"{signal} empty -- mechanism not recorded")

    if expect.get("options_count"):
        got = len(parsed.get("grading_options") or [])
        if got != expect["options_count"]:
            problems.append(f"{got} grading_options, expected {expect['options_count']}")

    if expect.get("options_base_total") is not None:
        # A bonus is additive: it sits on top of a scheme whose other components
        # already total 100, so the raw total legitimately exceeds 100 and only
        # the base is worth asserting.
        for option in parsed.get("grading_options") or []:
            comps = [c for c in (option.get("components") or []) if isinstance(c, dict)]
            if not comps:
                continue
            base = sum(float(c.get("weight") or 0) for c in comps if not _is_bonus(c))
            if abs(base - expect["options_base_total"]) > 0.01:
                problems.append(f"{option.get('option_name')!r} base weight {base}, expected {expect['options_base_total']}")
            for banned in expect.get("must_not_contain", []):
                if _find(comps, banned) is not None:
                    problems.append(f"{option.get('option_name')!r} counted excluded item ~{banned!r}")

    for needle in expect.get("some_option_contains", []):
        options = parsed.get("grading_options") or []
        if not any(_find(o.get("components") or [], needle) for o in options):
            problems.append(f"no scheme contains ~{needle!r}, which a student can actually earn")

    if expect.get("low_confidence") and parsed.get("confidence") == "high":
        problems.append("confidence 'high' on an outline that omits its weights")

    return problems


_TEST_SCALE = {
    "configured": True,
    "university_name": "Test University",
    "scale": [{"letter": "A", "min": 80}, {"letter": "B", "min": 70}, {"letter": "C", "min": 60},
              {"letter": "C-", "min": 57}, {"letter": "D", "min": 50}, {"letter": "F", "min": 0}],
}


def run(cases, show_raw=False, pipeline=False):
    """pipeline=True exercises the three-stage compiler a course workspace uses.

    The single call and the pipeline build different prompts, so a rule can be
    right on one path and missing on the other -- which is exactly how bonus
    components survived into a scheme and made its weights total 104%.
    """
    results = []
    for case in cases:
        used = []
        try:
            if pipeline:
                parsed = app._high_accuracy_outline_analysis(
                    "TEST 1000", case["text"], _TEST_SCALE, app._outline_schema())
                used = ["pipeline"]
            else:
                parsed = app._gemini_json(
                    "You are a precise university course-outline parser. Output only valid JSON.",
                    app._outline_analysis_prompt(case["text"], "TEST 1000"),
                    4000,
                    0.1,
                    reasoning=True,
                    used_model=used,
                )
        except Exception as exc:  # a crash is itself a result worth recording
            print(f"[ERROR] {case['id']}: {type(exc).__name__}: {exc}")
            results.append((case["id"], ["call failed"]))
            continue

        problems = score(case, parsed)
        mark = "PASS" if not problems else "FAIL"
        print(f"[{mark}] {case['id']:<22} ({used[0] if used else '?'})")
        for problem in problems:
            print(f"         - {problem}")
        if show_raw:
            print(json.dumps(parsed, indent=2)[:4000])
        results.append((case["id"], problems))

    passed = sum(1 for _, p in results if not p)
    print(f"\n{passed}/{len(results)} cases clean")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", help="run a single case by id")
    ap.add_argument("--raw", action="store_true", help="dump the parsed JSON")
    ap.add_argument("--pipeline", action="store_true",
                    help="run the three-stage compiler instead of a single call")
    args = ap.parse_args()

    selected = [c for c in CASES if not args.case or c["id"] == args.case]
    if not selected:
        sys.exit(f"no such case: {args.case}")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY not set -- source .env first")
    run(selected, show_raw=args.raw, pipeline=args.pipeline)
