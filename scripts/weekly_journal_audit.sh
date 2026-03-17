#!/bin/bash
# Weekly JARVIS journal audit — run via cron
# Produces a summary report of errors, warnings, and anomalies from the past week.

REPORT_DIR="/home/user/jarvis/reports/journal_audits"
mkdir -p "$REPORT_DIR"
DATE=$(date +%Y-%m-%d)
REPORT="$REPORT_DIR/audit_${DATE}.txt"

{
echo "========================================"
echo "  JARVIS Weekly Journal Audit — $DATE"
echo "========================================"
echo ""

echo "=== VOICE PIPELINE (jarvis.service) ==="
echo ""

echo "Watchdog false alarms (listener stuck):"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "Listener appears stuck"

echo "CUDA/HIP OOM:"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "out of memory"

echo "Audio device failures:"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "Failed to open audio device"

echo "Google Calendar sync errors:"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "Google Calendar sync failed\|Google Calendar poll error"

echo "Database locked:"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "database is locked"

echo "SEGVs (core dumps):"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "status=11/SEGV"

echo "ABRTs:"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "status=6/ABRT"

echo "Service timeouts (SIGTERM):"
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "stop-sigterm.*timed out"

echo ""
echo "=== WEB SERVICE (jarvis-web.service) ==="
echo ""

echo "SIGTERM timeouts:"
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "stop-sigterm.*timed out"

echo "Startup crashes:"
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "asyncio.run(run_server())"

echo "process_command crashes:"
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "result = await process_command"

echo "exit-code failures:"
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null | grep -c "Failed with result 'exit-code'"

echo ""
echo "=== UNIQUE APP-LEVEL ERRORS (voice) ==="
echo ""
journalctl --user -u jarvis.service --since "7 days ago" --no-pager 2>/dev/null \
    | grep " - ERROR - " \
    | grep -v "Listener appears stuck\|systemd" \
    | sed 's/^.*python3\[[0-9]*\]: [0-9-]* [0-9:]* - //' \
    | sort -u

echo ""
echo "=== UNIQUE APP-LEVEL ERRORS (web) ==="
echo ""
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null \
    | grep " - ERROR - " \
    | grep -v "systemd" \
    | sed 's/^.*python3\[[0-9]*\]: [0-9-]* [0-9:]* - //' \
    | sort -u

echo ""
echo "=== UNIQUE TRACEBACKS (web, last line of each) ==="
echo ""
journalctl --user -u jarvis-web.service --since "7 days ago" --no-pager 2>/dev/null \
    | grep -A1 "Traceback" \
    | grep -v "Traceback\|--" \
    | sort -u

echo ""
echo "========================================"
echo "  End of report"
echo "========================================"
} > "$REPORT" 2>&1

echo "Journal audit saved to: $REPORT"
