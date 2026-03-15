"""
Human-readable summary report for JARVIS Test Suite V3.
"""

from __future__ import annotations

from datetime import datetime


def generate_summary(run_data: dict) -> str:
    """Generate a human-readable summary from run results."""
    lines = []

    lines.append("JARVIS Test Suite V3 — Run Summary")
    lines.append("=" * 60)
    lines.append("")

    # Run info
    lines.append(f"Run ID:      {run_data['run_id']}")
    lines.append(f"Timestamp:   {run_data['timestamp']}")
    lines.append(f"Version:     {run_data['suite_version']}")
    duration_s = run_data['total_duration_ms'] / 1000
    lines.append(f"Duration:    {duration_s:.1f}s")
    lines.append("")

    # Overall results
    total = run_data['conversations_total']
    p = run_data['conversations_pass']
    m = run_data['conversations_mixed']
    f = run_data['conversations_fail']
    lines.append(f"Conversations: {total} total")
    lines.append(f"  PASS:  {p:3d} ({p/total*100:.0f}%)" if total else "  PASS:  0")
    lines.append(f"  MIXED: {m:3d} ({m/total*100:.0f}%)" if total else "  MIXED: 0")
    lines.append(f"  FAIL:  {f:3d} ({f/total*100:.0f}%)" if total else "  FAIL:  0")
    lines.append("")

    # Assertions
    at = run_data['assertions_total']
    ap = run_data['assertions_passed']
    af = run_data['assertions_failed']
    lines.append(f"Assertions: {ap}/{at} passed ({ap/at*100:.0f}%)" if at else "Assertions: 0")
    if af:
        lines.append(f"  Failed: {af}")
    lines.append("")

    # Cleanup
    cr = run_data.get('cleanup_report', {})
    if cr:
        lines.append(f"Cleanup: {cr.get('leaks', 0)} API leak(s)")
        deep = cr.get('deep_cleaned', {})
        if deep:
            total = sum(deep.values())
            lines.append(f"Deep cleanup: {total} artifacts purged ({', '.join(f'{k}={v}' for k, v in sorted(deep.items()))})")
        else:
            lines.append("Deep cleanup: nothing to purge")
    lines.append("")

    # Per-category breakdown
    categories = {}
    for conv in run_data.get('conversations', []):
        cat = conv.get('category', 'unknown')
        if cat not in categories:
            categories[cat] = {"pass": 0, "mixed": 0, "fail": 0, "total": 0}
        categories[cat]["total"] += 1
        grade = conv.get("grade", "FAIL").lower()
        if grade in categories[cat]:
            categories[cat][grade] += 1

    if categories:
        lines.append("By Category:")
        lines.append(f"  {'Category':<25} {'P':>3} {'M':>3} {'F':>3} {'Total':>5}")
        lines.append(f"  {'-'*25} {'-'*3} {'-'*3} {'-'*3} {'-'*5}")
        for cat in sorted(categories.keys()):
            c = categories[cat]
            lines.append(f"  {cat:<25} {c['pass']:>3} {c['mixed']:>3} "
                         f"{c['fail']:>3} {c['total']:>5}")
        lines.append("")

    # Failed / Mixed conversations detail
    non_pass = [c for c in run_data.get('conversations', []) if c.get('grade') != 'PASS']
    if non_pass:
        lines.append("Non-PASS Conversations:")
        for conv in non_pass:
            lines.append(f"  {conv['id']}: {conv['name']} — {conv['grade']}")
            lines.append(f"    Assertions: {conv['assertions_passed']}/{conv['assertions_total']} "
                         f"({conv['assertions_failed']} failed)")
            turn_grades = conv.get('turn_grades', [])
            for ti, tg in enumerate(turn_grades):
                if tg != "PASS":
                    lines.append(f"    Turn {ti+1}: {tg}")
        lines.append("")

    return "\n".join(lines)
