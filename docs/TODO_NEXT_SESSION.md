# TODO — Next Session

**Updated:** March 2, 2026 (session 120)

---

## Session 120 Completed

### DB Migration Wiring — Feature 1 Complete (COMMITTED — 2a45733)

Completed the wiring half of the multi-user reminders DB migration (schema was already applied in session 118).

**Changes:**
- `add_reminder()` now accepts `created_by` (default `'christopher'`) and `origin_endpoint` (default `'voice'`) — both written to DB INSERT
- `_on_google_new_event()` explicitly passes `created_by='christopher'`, `origin_endpoint='google_calendar'` (prevents push-back loop, correctly tags source)
- `core/tools/manage_reminders.py` — added `current_user_fn` dependency; `_reminders_add()` gets `created_by` from it at call time
- `core/tool_executor.py` — added `set_current_user_fn()` shim (parallel to `set_reminder_manager`)
- All 3 frontends (`pipeline.py`, `jarvis_console.py`, `jarvis_web.py`) wire a lambda over `conversation.current_user`
- DB fix: 780 existing Google Calendar rows updated from `origin_endpoint='voice'` (migration default) to `'google_calendar'` (correct source)

**Also:** Added `secondary user's Apple` (bare form without suffix) to `redact.conf` — previous patterns only covered `secondary user's Apple ID`, `secondary user's Apple Calendar`, `secondary user's iCloud`. The plain form appeared in TODO session notes.

---

## Session 119 Completed

### Rundown Bug Fixes (COMMITTED — bb57ed1)

Three bugs in `core/reminder_manager.py`:

**Bug 1 — Wrong event times + same-event duplicates:**
- **Root cause:** `get_daily_rundown()` and `get_weekly_rundown()` used `reminder_time` (offset fire time, e.g. 2:30 PM for a 3 PM event) instead of `event_time` (actual event start). With 2 reminder offsets per event (530 total reminders = 265 events × 2), the same event appeared twice at different fire times.
- **Fix:** New `_query_rundown_reminders()` helper; both rundowns now deduplicate by base google event ID and display at `event_time`.

**Bug 2 — Immediate re-offer after weekly rundown:**
- **Root cause:** `deliver_rundown()` called `_offer_rundown()` immediately after weekly speech — jarring "Good morning, are you ready for the rundown?" right after the full week summary.
- **Fix:** Dropped the daily re-offer entirely. Weekly rundown already covers Monday grouped by day. Monday = one rundown only.

**Bug 3 — Missed events (tomorrow / Friday not announced):**
- **Root cause:** Weekly DB query filtered by `reminder_time BETWEEN monday AND sunday`. Events with a 7-day-ahead reminder had their `reminder_time` *last* Monday — outside the window, invisible.
- **Fix:** `_query_rundown_reminders()` now queries calendar-synced events by `event_time` (actual event day), so all events *occurring* this week always appear regardless of when their reminder fires.

**Also:** Added 4 new redact.conf patterns for "secondary user's Apple" variants that were leaking into public repo via TODO notes.

---

## Session 118 Completed

### Multi-User + Email + Memory Page — DB Migration Schema

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

## Still In Progress

### Multi-User + Email + Memory Page (4-feature plan approved)
- **Full plan:** `memory/plan_multiuser_email_memory.md` + `.claude/plans/serene-chasing-candle.md`
- **DB migration COMPLETE (commit 2a45733):** `created_by`, `origin_endpoint`, `caldav_event_id` columns migrated. All wiring done: `add_reminder()` params, `_on_google_new_event()` explicit values, `manage_reminders` tool passes `created_by` via `current_user_fn` dependency, all 3 frontends wire the getter. 780 existing Google Calendar rows corrected to `origin_endpoint='google_calendar'`.
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
