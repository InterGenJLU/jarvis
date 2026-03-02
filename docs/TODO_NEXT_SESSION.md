# TODO — Next Session

**Updated:** March 2, 2026 (session 118)

---

## Session 117 Completed

### Reminder Staleness Guard (COMMITTED)
- **Problem:** 3 reminders with **2025 dates** fired on service restart — birthdays/anniversaries that slipped through sync import guards during a timing gap (background sync created them AFTER the mass delete in session 116)
- **Root cause:** `_check_due_reminders()` had no lower bound — matched any pending reminder with `reminder_time <= now`, no matter how old
- **Fix in `core/reminder_manager.py`:**
  - Reminders >24h overdue are **auto-cancelled** (with warning log) instead of fired
  - Firing query now has lower bound: `reminder_time >= now - 24h`
  - Belt-and-suspenders: even if bad data enters the DB through any path, stale reminders never fire
- **Also:** Manually deleted the 3 past-dated reminders from the DB
- **Verified:** 777 pending (all future-dated, earliest 2026-03-05), 0 fired, clean startup, no loop
- **Commit:** `2575a75`

## Session 116 Completed

### Google Calendar Multi-Notification Support (COMMITTED)
- **Context:** User migrating ALL recurring events (birthdays, anniversaries, meetings, appointments) to the JARVIS calendar. Most events have 2 notification offsets (e.g., 1 week + 4 days, or 1 hour + 30 min).
- **Problem:** Previous code only honored the earliest notification (`max(popup_minutes)`), silently dropping all other offsets. Also, the loop fix from session 115 was uncommitted.
- **Changes in `core/google_calendar.py`:**
  - `_parse_google_event()` now returns `reminder_minutes_list` (all offsets) instead of single `reminder_minutes`
  - Poll loop calls `_on_new_event()` once per offset, creating separate JARVIS reminders for each
- **Changes in `core/reminder_manager.py`:**
  - Composite key `base_event_id:offset` in `google_event_id` column for per-offset dedup
  - `_find_all_by_google_event_id()` — prefix match for cancellation (cancels all offsets at once)
  - `_base_google_event_id()` — strips `:offset` suffix before Google API calls (delete, update, snooze)
  - Past-event guard (>1hr) and LIMIT 3 firing cap from session 115 now committed
- **Verified:** 250 events → 530 reminders (2 per event), all composite keys, zero errors, no firing loop
- **DB cleaned:** Deleted 605 old-format reminders, deleted sync token, fresh re-sync with new format

## Session 115 Completed

### Bug Fixes (both were live-affecting)

**1. Calendar Notification Loop — FIXED (committed in session 116)**
- **Symptom:** JARVIS stuck firing reminders every ~6 seconds in an endless TTS loop
- **Root cause:** `singleEvents=True` expanded ALL recurring instances (including past). No time filter.
- **Fix:** Past-event guard + LIMIT 3 cap + multi-notification composite keys (see session 116)

**2. Volume Slider Unresponsive — PERMANENTLY FIXED (3rd occurrence)**
- **Permanent fix:** WirePlumber config `~/.config/wireplumber/wireplumber.conf.d/51-realtek-analog-output.conf` forces duplex profile
- **Note:** JARVIS TTS unaffected (direct ALSA `plughw:2,0` bypasses PipeWire)

---

## Session 118 In Progress

### Multi-User + Email + Memory Page (4-feature plan approved)
- **Full plan:** `memory/plan_multiuser_email_memory.md` + `.claude/plans/serene-chasing-candle.md`
- **DB migration PARTIAL:** `created_by`, `origin_endpoint`, `caldav_event_id` columns + indexes added to `_init_db()` in `core/reminder_manager.py`. Wiring (add_reminder params, push-back routing, tool/router passthrough) NOT done yet.
- **Memory page:** Not started. Expanded from original plan — now includes DB health table for all data stores.
- **CalDAV:** Not started. Needs secondary user's Apple ID validation (see prereqs in plan).
- **Email skill:** Not started. Gmail (OAuth) + AOL (IMAP). Read-only Phase 1 with junk filtering.

---

## Up Next

### 0. Voice Enrollment for secondary user (at lunch)
```bash
cd ~/jarvis
python3 scripts/enroll_speaker.py --user erica   # 3 clips x 3s, needs her at mic
systemctl --user restart jarvis                    # reload embedding
python3 scripts/enroll_speaker.py --test           # verify both speakers recognized
```

### 1. Multi-User Calendar Integration (PLAN NEEDED)
**Priority:** HIGH — Plan before build
**What:** Tie secondary user's Apple Calendar into the reminder system alongside the user's Google Calendar.
- **Per-user calendar ownership:** Add `created_by` column (or similar) to reminders DB so the system knows which user created each reminder
- **Two-way sync per user:** Google Calendar for the user, Apple Calendar (iCloud/CalDAV) for secondary user — each user's phone additions sync to JARVIS
- **Push-back routing:** When syncing reminders back to external calendars, only push to the originating user's calendar
- **Local announcement:** ALL reminders fire locally regardless of who created them (simple cumulative approach from the shared DB)

### 2. Mobile iOS App (#60) — `memory/plan_mobile_ios_app.md`
**Priority:** HIGH — Starting shortly after enrollment + calendar planning

### 3. Memory & Awareness Dashboard (plan ready)
**Priority:** MEDIUM — Plan saved to `memory/plan_memory_dashboard.md`
**What:** New `/memory` web page. 6 summary cards, 3 charts, facts + interaction explorer. Dark theme.

### 4. Presentation Engine Overhaul (#49 extended)
**Priority:** MEDIUM — Plan saved to `memory/plan_presentation_engine.md`

### 5. "Onscreen please" — retroactive visual display (#11)
### 6. Document refinement follow-ups (#49)
### 7. Vision pipeline integration — console/web image input
