#!/usr/bin/env python3
"""Quick reminder database query tool for testing/debugging.

Usage:
    python3 scripts/query_reminders.py              # Show today's reminders
    python3 scripts/query_reminders.py all           # Show ALL reminders
    python3 scripts/query_reminders.py pending       # Show all pending
    python3 scripts/query_reminders.py fired         # Show all fired
    python3 scripts/query_reminders.py id 5          # Show reminder #5
    python3 scripts/query_reminders.py search "call" # Search by title
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = "/mnt/storage/jarvis/data/reminders.db"

def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def fmt(rows):
    if not rows:
        print("  (none)")
        return
    for r in rows:
        pri = {1: "CRITICAL", 2: "HIGH", 3: "NORMAL", 4: "LOW"}.get(r["priority"], "?")
        ack = " [needs-ack]" if r["requires_ack"] else ""
        fired = f" (fired {r['fire_count']}x)" if r["fire_count"] else ""
        snooze = f" [snoozed→{r['snooze_until']}]" if r["snooze_until"] else ""
        print(f"  #{r['id']:3d} | {r['status']:8s} | {pri:8s} | {r['reminder_time']} | {r['title']}{ack}{fired}{snooze}")
        if r["description"]:
            print(f"        desc: {r['description']}")

def main():
    if not Path(DB_PATH).exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    arg = sys.argv[1] if len(sys.argv) > 1 else "today"

    if arg == "today":
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"=== Reminders for {today} ===")
        rows = query(
            "SELECT * FROM reminders WHERE reminder_time BETWEEN ? AND ? ORDER BY reminder_time",
            (f"{today} 00:00:00", f"{today} 23:59:59")
        )
        fmt(rows)
    elif arg == "all":
        print("=== All reminders ===")
        rows = query("SELECT * FROM reminders ORDER BY reminder_time DESC")
        fmt(rows)
    elif arg == "pending":
        print("=== Pending reminders ===")
        rows = query("SELECT * FROM reminders WHERE status='pending' ORDER BY reminder_time")
        fmt(rows)
    elif arg == "fired":
        print("=== Fired reminders ===")
        rows = query("SELECT * FROM reminders WHERE status='fired' ORDER BY reminder_time DESC")
        fmt(rows)
    elif arg == "id" and len(sys.argv) > 2:
        rid = int(sys.argv[2])
        rows = query("SELECT * FROM reminders WHERE id=?", (rid,))
        if rows:
            for k, v in rows[0].items():
                print(f"  {k:20s}: {v}")
        else:
            print(f"  Reminder #{rid} not found")
    elif arg == "search" and len(sys.argv) > 2:
        term = sys.argv[2]
        print(f"=== Search: '{term}' ===")
        rows = query("SELECT * FROM reminders WHERE title LIKE ? ORDER BY reminder_time DESC", (f"%{term}%",))
        fmt(rows)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
