# TODO — Next Session

**Updated:** March 1, 2026 (session 115)

---

## Session 115 Completed

### Bug Fixes (both were live-affecting)

**1. Calendar Notification Loop — FIXED**
- **Symptom:** JARVIS stuck firing "Chris - Paycheck" every ~6 seconds in an endless TTS loop (reached reminder #107+ before we killed it)
- **Root cause:** Moving a recurring Google Calendar event into the JARVIS calendar triggered `singleEvents=True` sync, which expanded ALL instances back to 2019. `_on_google_new_event()` had no time filter — it created 175 local reminders with past dates. `_check_due_reminders()` found all 154 pending ones as "due" and fired them sequentially.
- **Fix 1:** Added past-event guard in `_on_google_new_event()` — skips events with `start_time > 1hr in the past`
- **Fix 2:** Added `LIMIT 3` cap in `_check_due_reminders()` as safety net against future floods
- **Data cleanup:** Marked 188 historical reminders as completed. 74 future reminders (birthday through 2099) left intact.
- **Files:** `core/reminder_manager.py` (lines 1335-1338, 488)

**2. Volume Slider Unresponsive — PERMANENTLY FIXED (3rd occurrence)**
- **Symptom:** GNOME volume slider did nothing. Only HDMI sink in PipeWire, no analog output.
- **Root cause:** Realtek ALCS1200A has broken jack detect. PipeWire/WirePlumber sees output ports as "not available" and auto-selects `input:analog-stereo` profile (no output sink). Previous fixes were ephemeral `pactl` commands.
- **Permanent fix:** Created WirePlumber config `~/.config/wireplumber/wireplumber.conf.d/51-realtek-analog-output.conf` that forces `output:analog-stereo+input:analog-stereo` profile. Verified it survives WirePlumber restart.
- **Note:** JARVIS TTS always worked (direct ALSA `plughw:2,0` bypasses PipeWire). This only affected the system volume slider.

### Memory Notes Saved
- `memory/fix_realtek_audio_sink.md` — full diagnosis, quick fix, permanent fix, verification steps
- `memory/MEMORY.md` — added "Known Recurring Issues" section with both bugs
- `memory/codebase_cheat_sheet.md` — gotchas #17 (calendar sync) and #18 (Realtek sink)

### Code Changes (uncommitted)
- `core/reminder_manager.py` — past-event guard + LIMIT 3 safety cap
- `~/.config/wireplumber/wireplumber.conf.d/51-realtek-analog-output.conf` — permanent audio fix (not in git, system config)

### ⚠️ CRITICAL: Session ended with recurring loop NOT fully resolved
- The first fix (past-event guard + LIMIT 3) was correct but the service wasn't actually restarted after the edit — PID 167850 was the SAME process from before the code change.
- "Dogs due for Simparica" triggered a second loop. Cleaned 8 more historical reminders and restarted service (should now have the fix loaded).
- **VERIFY ON NEXT SESSION**: Check `journalctl --user -u jarvis.service -n 30` to confirm no looping. If it loops again, the Google Calendar sync_token may need to be invalidated so it stops re-syncing old events. File: `/mnt/storage/jarvis/data/google_sync_token.json`
- The root problem is `sync_from_google()` with `singleEvents=True` expanding ALL instances of recurring events. The past-event guard in `_on_google_new_event()` should filter them, but verify it's working.
- If still broken: delete sync token to force full re-sync (which uses `timeMin=now` and won't return past events), then the incremental sync should only return future changes.

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
