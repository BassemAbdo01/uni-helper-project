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

    components = [c for c in (parsed.get("components") or []) if isinstance(c, dict)]

    for needle, weight in expect["components"]:
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
    elif components:
        problems.append(f"invented {len(components)} component(s) from an outline with no weights")

    if expect.get("total") is not None:
        total = sum(float(c.get("weight") or 0) for c in components)
        if abs(total - expect["total"]) > 0.01:
            problems.append(f"weights total {total} != {expect['total']}")

    for banned in expect.get("must_not_contain", []):
        if _find(components, banned) is not None:
            problems.append(f"counted excluded item ~{banned!r}")

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

    if expect.get("low_confidence") and parsed.get("confidence") == "high":
        problems.append("confidence 'high' on an outline that omits its weights")

    return problems


def run(cases, show_raw=False):
    results = []
    for case in cases:
        used = []
        try:
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
    args = ap.parse_args()

    selected = [c for c in CASES if not args.case or c["id"] == args.case]
    if not selected:
        sys.exit(f"no such case: {args.case}")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY not set -- source .env first")
    run(selected, show_raw=args.raw)
