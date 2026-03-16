#!/usr/bin/env bash
# Session initialization script — automates Rule #8 ceremony
# Usage: ./scripts/session_init.sh <session_number>
#
# Creates:
#   - Session directory with artifacts/ and research/ subdirectories
#   - Initial chat_log_incremental.md
#   - Appends session entry to INDEX.md
#
# Does NOT:
#   - Read the handoff (the assistant must do this for context)
#   - Ask the owner for instructions (the assistant must do this)

set -euo pipefail

SESSION_STORAGE="/mnt/models/.claude/SESSION_STORAGE"
INDEX="${SESSION_STORAGE}/INDEX.md"
DATE=$(date +%Y%m%d)
MONTH_DAY=$(date +%Y-%m-%d)

# --- Argument parsing ---
if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <session_number>"
    echo ""
    # Auto-detect next session number from INDEX.md
    if [[ -f "$INDEX" ]]; then
        LAST=$(grep -oP '^\| \K[0-9]+' "$INDEX" | sort -n | tail -1)
        NEXT=$((LAST + 1))
        echo "Next session number appears to be: ${NEXT}"
        echo "Run: $0 ${NEXT}"
    fi
    exit 1
fi

SESSION_NUM="$1"
SESSION_DIR="${SESSION_STORAGE}/session_${SESSION_NUM}_${DATE}"

# --- Guard: don't overwrite existing session ---
if [[ -d "$SESSION_DIR" ]]; then
    echo "ERROR: ${SESSION_DIR} already exists. Aborting."
    exit 1
fi

# --- Create directory structure ---
mkdir -p "${SESSION_DIR}/artifacts" "${SESSION_DIR}/research"
echo "Created: ${SESSION_DIR}/"

# --- Create initial chat log ---
cat > "${SESSION_DIR}/chat_log_incremental.md" <<EOF
# Session ${SESSION_NUM} Chat Log — $(date +"%B %d, %Y")

## Orientation
- Read handoff from previous session
- Created session directory (via session_init.sh)
- Awaiting owner instructions
EOF
echo "Created: chat_log_incremental.md"

# --- Append to INDEX.md ---
echo "| ${SESSION_NUM} | ${MONTH_DAY} | (in progress) |" >> "$INDEX"
echo "Updated: INDEX.md"

echo ""
echo "Session ${SESSION_NUM} initialized. Assistant still needs to:"
echo "  1. Read memory/handoff.md"
echo "  2. Ask the owner for instructions"
