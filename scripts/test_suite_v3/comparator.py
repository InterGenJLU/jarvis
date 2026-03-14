"""
Run-over-run regression comparator for JARVIS Test Suite V3.

Compares two result directories and reports:
  - Regressions: PASS → MIXED/FAIL
  - Improvements: MIXED/FAIL → PASS
  - New failures: assertions that failed in current but not previous
  - Honorific trend: % of honorific assertions passing
"""

from __future__ import annotations

import json
import os


def compare_runs(dir_a: str, dir_b: str) -> str:
    """Compare two V3 run directories. Returns human-readable report."""
    results_a = _load_results(dir_a)
    results_b = _load_results(dir_b)

    if not results_a or not results_b:
        return "Error: could not load results from one or both directories."

    run_a_id = results_a.get("run_id", os.path.basename(dir_a))
    run_b_id = results_b.get("run_id", os.path.basename(dir_b))

    # Index conversations by ID
    convs_a = {c["id"]: c for c in results_a.get("conversations", [])}
    convs_b = {c["id"]: c for c in results_b.get("conversations", [])}

    all_ids = sorted(set(convs_a.keys()) | set(convs_b.keys()))

    regressions = []
    improvements = []
    new_in_b = []
    removed_in_b = []

    for cid in all_ids:
        a = convs_a.get(cid)
        b = convs_b.get(cid)

        if a and not b:
            removed_in_b.append(cid)
            continue
        if b and not a:
            new_in_b.append(cid)
            continue

        grade_a = a["grade"]
        grade_b = b["grade"]

        if grade_a == "PASS" and grade_b in ("MIXED", "FAIL"):
            regressions.append((cid, a["name"], grade_a, grade_b))
        elif grade_a in ("MIXED", "FAIL") and grade_b == "PASS":
            improvements.append((cid, a["name"], grade_a, grade_b))

    # Honorific trend from JSONL logs
    hon_a = _count_honorific_assertions(dir_a)
    hon_b = _count_honorific_assertions(dir_b)

    # Build report
    lines = []
    lines.append(f"V3 Regression Report: {run_a_id} → {run_b_id}")
    lines.append("=" * 60)
    lines.append("")

    # Summary
    lines.append(f"Run A ({run_a_id}): {results_a['conversations_pass']}P / "
                 f"{results_a['conversations_mixed']}M / {results_a['conversations_fail']}F")
    lines.append(f"Run B ({run_b_id}): {results_b['conversations_pass']}P / "
                 f"{results_b['conversations_mixed']}M / {results_b['conversations_fail']}F")
    lines.append("")

    # Regressions
    if regressions:
        lines.append(f"REGRESSIONS ({len(regressions)}):")
        for cid, name, ga, gb in regressions:
            lines.append(f"  {cid}: {name} — {ga} → {gb}")
        lines.append("")
    else:
        lines.append("REGRESSIONS: none")
        lines.append("")

    # Improvements
    if improvements:
        lines.append(f"IMPROVEMENTS ({len(improvements)}):")
        for cid, name, ga, gb in improvements:
            lines.append(f"  {cid}: {name} — {ga} → {gb}")
        lines.append("")
    else:
        lines.append("IMPROVEMENTS: none")
        lines.append("")

    # New / removed
    if new_in_b:
        lines.append(f"NEW in {run_b_id}: {', '.join(new_in_b)}")
    if removed_in_b:
        lines.append(f"REMOVED in {run_b_id}: {', '.join(removed_in_b)}")

    # Honorific trend
    if hon_a["total"] > 0 or hon_b["total"] > 0:
        lines.append("")
        pct_a = (hon_a["passed"] / hon_a["total"] * 100) if hon_a["total"] else 0
        pct_b = (hon_b["passed"] / hon_b["total"] * 100) if hon_b["total"] else 0
        lines.append(f"Honorific compliance: {pct_a:.0f}% → {pct_b:.0f}%")
        lines.append(f"  A: {hon_a['passed']}/{hon_a['total']} | "
                     f"B: {hon_b['passed']}/{hon_b['total']}")

    return "\n".join(lines)


def _load_results(dir_path: str) -> dict | None:
    """Load results.json from a run directory."""
    results_path = os.path.join(dir_path, "results.json")
    if not os.path.exists(results_path):
        return None
    with open(results_path) as f:
        return json.load(f)


def _count_honorific_assertions(dir_path: str) -> dict:
    """Count honorific assertion pass/fail from log.jsonl."""
    log_path = os.path.join(dir_path, "log.jsonl")
    total = 0
    passed = 0

    if not os.path.exists(log_path):
        return {"total": 0, "passed": 0}

    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "turn":
                continue
            for assertion in entry.get("assertions", []):
                if assertion.get("type") == "has_honorific":
                    total += 1
                    if assertion.get("passed"):
                        passed += 1

    return {"total": total, "passed": passed}
