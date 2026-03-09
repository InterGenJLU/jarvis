#!/usr/bin/env bash
# JARVIS Edge Case Test Suite — convenience wrapper
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_SCRIPT="$SCRIPT_DIR/test_edge_cases.py"

usage() {
    cat <<'EOF'
JARVIS Edge Case Test Suite

Usage:
    scripts/unit_tests.sh              # Tiers 1+2 (default)
    scripts/unit_tests.sh --tier 1     # Unit tests only (<1s)
    scripts/unit_tests.sh --tier 2     # Routing tests only (~5s load)
    scripts/unit_tests.sh --phase 1A   # Single phase
    scripts/unit_tests.sh --id 1A-01   # Single test
    scripts/unit_tests.sh --verbose    # Show all tests (not just failures)
    scripts/unit_tests.sh --json       # JSON output

Options:
    --tier N      Run only tier N (1=unit, 2=routing, 3=execution, 4=pipeline)
    --phase XX    Run only phase XX (e.g. 1A, 3C, 7B, 2F, 5D)
    --id XX-NN    Run single test by ID (e.g. 1A-01, 5C-R2)
    --all         Run all tiers (default: 1+2)
    --verbose     Show all tests, not just failures
    --json        Output results as JSON
    --save PATH   Save results JSON + debug JSONL (default: /tmp/unit_test_results.json)
    --no-save     Disable saving results and debug JSONL
    -h, --help    Show this help message
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

exec python3 "$TEST_SCRIPT" "$@"
