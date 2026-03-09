# Session 205 Handoff — March 8, 2026

## What Was Done

1. **Committed split-rule change** from session 204 (Rule 14→14+15). Published.
2. **Ran test run 008** (26 conversations, same IDs as run 007). Results: 8 of 11 run 007 inappropriate offers GONE. 3 persisted + 8 new offers appeared (most legitimate summaries).
3. **Refined readback rules (Rules 14-15):**
   - Rule 14: changed "summarize" to explicit "SHORT summary (1-3 sentences, WITHOUT listing items)". Added "NOT code snippets or command examples" exclusion.
   - Rule 15: added self-check ("does your response already contain the details? If YES, stop"). Added travel recommendations and sightseeing to exclusion list.
4. **Ran test run 009** (7 conversations with offers from run 008). Results: LLM now properly summarizes first — C36 T5 went from 186 words (full list) to 53 words (proper summary + offer). Major behavioral improvement.
5. **Ran C18 solo retest** after adding travel/sightseeing exclusion. No inappropriate readback offer. Fixed.
6. **Built audio watchdog service:**
   - `scripts/audio_watchdog.py` — monitors mic, output sink, listener frame flow
   - `~/.config/systemd/user/jarvis-audio-watchdog.service` — BindsTo jarvis.service
   - 3-min check interval, 15-min silence threshold, 3 restarts/hour limit
   - **NOT YET ENABLED** — needs `systemctl --user daemon-reload && systemctl --user enable --now jarvis-audio-watchdog.service`
7. **Installed `python-is-python3`** — unversioned `python` now works.
8. **Saved system upgrade plan** to `.claude/plans/plan_system_upgrade.md` — apt upgrade + venv migration phases.
9. **Speaker ID investigated** — not broken, user was speaking from living room (far-field). Scores expected to be low at distance from desk mic.

## Commits This Session

| Hash | Description |
|------|-------------|
| `1ff3a30` | Split readback Rule 14 into Rules 14+15 — separate guard clause |
| `66fb0b0` | Refine readback rules + add audio watchdog service |

## Readback Progress (Run 005 → 009)

| Run | Convs | Inappropriate Offers | Notes |
|-----|-------|---------------------|-------|
| 005 | 40 | ~42 | Baseline |
| 007 | 26 | 11 | 74% reduction — synthesis prompt fix |
| 008 | 26 | 3-4 | Split rule + summary-first |
| 009 | 7 | 0-1 (C18 borderline) | Code exclusion + travel exclusion |

## NEXT STEPS

1. **Enable audio watchdog:** `systemctl --user daemon-reload && systemctl --user enable --now jarvis-audio-watchdog.service`
2. **Run full 26-conversation retest** (run 010) with all readback fixes to get final numbers
3. **System upgrade** — plan saved in `.claude/plans/plan_system_upgrade.md`. Safe to do apt upgrade (no Python 3.12 or kernel in queue). Venv migration deferred.
4. **Routing bug fixes** (C02 T2, C05 T3, C03 T3, C07 T4) — still pending from session 204
5. **Strengthen Rule 16** if search narration persists in run 010

## Services State
- **jarvis-web.service:** Running (restarted during session for test runs)
- **jarvis.service:** Running (restarted 10:23)
- **jarvis-audio-watchdog.service:** EXISTS but NOT ENABLED

## Files Modified/Created

| File | Status |
|------|--------|
| `core/persona.py` | Committed — Rules 14-15 refined |
| `core/llm_router.py` | Committed — both synthesis paths updated |
| `scripts/audio_watchdog.py` | Committed — new file |
| `~/.config/systemd/user/jarvis-audio-watchdog.service` | Created, not committed (user config) |
| `.claude/plans/plan_system_upgrade.md` | Created, not committed |
