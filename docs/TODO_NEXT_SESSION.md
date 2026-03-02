# TODO — Next Session

**Updated:** March 1, 2026 (session 116)

---

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

## Up Next

### 0. Memory & Awareness Dashboard (plan ready)
**Priority:** HIGH — Plan saved to `memory/plan_memory_dashboard.md`
**What:** New `/memory` web page. 6 summary cards, 3 charts, facts + interaction explorer. Dark theme.

### 1. Presentation Engine Overhaul (#49 extended)
**Priority:** HIGH — Plan saved to `memory/plan_presentation_engine.md`

### 2. Voice Enrollment for Second User
```bash
cd ~/jarvis
python3 scripts/enroll_speaker.py --user erica   # 3 clips x 3s, needs her at mic
systemctl --user restart jarvis                    # reload embedding
python3 scripts/enroll_speaker.py --test           # verify both speakers recognized
```

### 3. "Onscreen please" — retroactive visual display (#11)
### 4. Document refinement follow-ups (#49)
### 5. Vision pipeline integration — console/web image input
### 6. Mobile iOS App (#60) — `memory/plan_mobile_ios_app.md`
