# TODO — Next Session

**Updated:** February 18, 2026

---

## Next Up

### 1. Whisper Retraining — Scheduled Feb 21
**Priority:** HIGH
**Plan:**
1. Analyze logs Feb 14-21 for misheard phrases
2. Generate training data from corrections
3. Retrain with expanded dataset (149 original + new)
4. Convert to CTranslate2 and deploy
**Files:** `core/stt.py`, `/mnt/models/voice_training/`
**Note:** Remove `_debug_save_audio()` from `stt.py` after retraining

### 2. Voice Testing — Remaining Items
**Priority:** HIGH
**Status:** Most tests passed this session. Remaining:
1. ~~Metric bypass~~ PASSED — distance query returned km via web search
2. ~~Bare ack noise filter~~ PASSED — "yeah" during conv window filtered
3. Bare ack as answer — JARVIS asks question → "yeah" → treated as answer (needs reliable trigger)

### 3. Audio Recording Skill
**Priority:** MEDIUM
**Location:** `skills/personal/audio_recording/`
**Concept:** Voice-triggered recording with natural playback queries.
- "Record audio" → tone → capture → "Stop recording" → saved as WAV
- "Play the recording from yesterday" → date-based lookup + playback
- 6 semantic intents (start, stop, play, query, list, export)

### 4. App Launcher Skill
**Priority:** MEDIUM
**Location:** `skills/system/app_launcher/`
**Concept:** "Open Chrome. Fullscreen please. Thanks."
- Config-driven alias map (natural names → executables)
- Window management via `wmctrl`/`xdotool` (fullscreen, maximize, move to monitor)
- Open questions: Wayland vs X11 tools, directory navigation, monitor naming

---

## Active Bugs

None! 🎉

---

## Minor Loose Ends

- **Batch extraction (Phase 4) untested** — conversational memory batch fact extraction needs 25+ messages in one session to trigger
- **Console logging** — `JARVIS_LOG_FILE_ONLY=1` not producing logs in file (deferred, not urgent)

---

## Future Enhancements

- **Inject user facts into web research** — JARVIS should reason about what it knows about the user (location, preferences) during `stream_with_tools()`. Needs careful scoping to avoid history poisoning.
- **Minimize web search latency** — forced search adds ~5-8s; explore caching, parallel fetch, snippet-only mode.
- **Qwen sampling params** — Qwen team recommends temp=0.7, top_p=0.8, top_k=20 for non-thinking mode. Current: temp=0.6 only. Not urgent — prescriptive prompt works without them.
- **News urgency filtering** — "Read critical headlines" should filter by urgency level. Currently matches count intent with no urgency parameter.

---

---

## Designed Features — Not Yet Started

### Email Skill (Gmail Integration)
**Priority:** MEDIUM
**Concept:** Voice-composed email via Gmail API + OAuth (same pattern as Calendar).

### Google Keep Integration
**Priority:** MEDIUM
**Concept:** "Add milk to the grocery list." Shared access with secondary user.

### Skill Editing System
**Priority:** HIGH (Phase 2)
**Design:** `docs/SKILL_EDITING_SYSTEM.md`
**Concept:** "Edit the weather skill" → LLM code gen → review → apply with backup.

### "Onscreen Please" — Retroactive Visual Display
**Priority:** MEDIUM
**Concept:** Buffer last raw output. "Onscreen please" displays it retroactively.

### Web Dashboard
**Priority:** LOW (demo/showoff feature)
**Concept:** Local Flask/FastAPI web UI for JARVIS management.
- System stats page (live health check data, GPU/CPU/RAM, pipeline status)
- Configuration page (config.yaml rendered as a clean editable form)
- Skill manager (browse installed skills, add from template, view metadata)
- Database viewer (CRUD for memory.db facts, topic_segments, chat history)
- Separate process, read-only by default, localhost only

---

## Long-Term Roadmap

### Phase 3: Entertainment & Control
- **Music Control (Apple Music)** — playlist learning, volume via PulseAudio
- **Automated Skill Generation** — Q&A → build → test → review → deploy

### Phase 4: Security & Threat Hunting
- **Malware Analysis Framework** — QEMU sandbox, VirusTotal/Any.run API, CISA-format reports
- **Video / Face Recognition** — webcam → people/pets/objects
- **Tor / Dark Web Research** — Brave Tor mode, safety protocols

### Long-Term Vision
- Proactive AI (suggest actions based on patterns)
- Self-modification (JARVIS proposes own improvements)
- Home automation (IoT integration)
- Mobile access (remote command via phone)
- GitHub open source release (`docs/GITHUB_PUBLISHING_PLAN.md`)
- Emotional context awareness (laugh/frustration/distress detection)
- Voice cloning — Paul Bettany JARVIS

### Architecture Improvements
- **STT Worker Process** — GPU isolation via separate process, IPC via JSON stdin/stdout. Design: `docs/STT_WORKER_PROCESS.md`

---

## Completed (Feb 10-19)

*Brief summary. Full details in `memory/` files and git history.*

| Feature | Date | Notes |
|---------|------|-------|
| Developer Tools Polish | Feb 19 | HAL 9000 Easter eggs for blocked commands, smart port summary, conversational process summary |
| Scoped TTS subprocess control | Feb 18 | Replaced global `pkill -9 aplay/piper` with tracked subprocess kill — `tts.kill_active()` |
| Prescriptive Prompt + tool_choice=auto | Feb 18 | Rewrote vague prompt to explicit rules, removed tool_choice=required pattern matching. 150/150 test decisions correct (`8ae35ce`) |
| Ack Cache Trim | Feb 18 | 7→4 neutral time-based phrases per the user's preference (`0b9c017`) |
| Ack Cache Generic Fix | Feb 18 | Web-themed phrases replaced with generic for all-query ack cache (`046a275`) |
| tool_choice=required Default | Feb 18 | Force web search for factual queries — later replaced by prescriptive prompt (`1b50b0e`) |
| Web Research Follow-up Bug Fixes | Feb 18 | `_spoke` reset, aplay retry, nested context, regex scope, bare ack filter (`fd30984`) |
| Decimal TTS + Chunker Fix + Lazy aplay | Feb 18 | `normalize_decimals()`, `[.!?]\s` chunker, deferred `_open_aplay()` (`b2c63ec`, `54fdcac`) |
| Person Queries + Future-Date Detection | Feb 18 | Political neutrality in synthesis, date comparison in prompt (`18ce66e`) |
| Web Nav Phase 3: Qwen 3-8B + Tool Calling | Feb 18 | `web_research.py`, `stream_with_tools()`, DuckDuckGo + trafilatura (`8c153de`) |
| Bug Squashing Blitz (8 fixes) | Feb 18 | Audio cues, ack collision, browse/filesystem keywords, dismissal, TTS filenames, news spew |
| Gapless TTS Streaming | Feb 17 | `StreamingAudioPipeline` — single persistent aplay, zero-gap playback (`df7d498`) |
| Hardware Failure Graceful Degradation | Feb 17 | Startup retry, device monitor, degraded mode, health check |
| Conversational Memory (6 phases) | Feb 17 | SQLite facts + FAISS + recall + batch + proactive + forget |
| Streaming Delivery Fixes (5 bugs) | Feb 17 | Chunker simplification, metric stripping, context flush |
| Context Window (4 phases) | Feb 17 | Topic segmentation, relevance scoring, persistence |
| User Profile System (5 phases) | Feb 16 | Speaker ID, d-vectors, dynamic honorific |
| Kokoro TTS Integration | Feb 16 | 82M model, 50/50 fable+george, Piper fallback |
| Latency Refactor (4 phases) | Feb 16 | Streaming TTS, ack cache, streaming LLM, event pipeline |
| Developer Tools (13 intents) | Feb 15 | Codebase search, git multi-repo, system admin, safety tiers |
| PyTorch + ROCm Unification | Feb 15 | torch 2.10.0+rocm7.1 + ctranslate2 4.7.1 coexistence |
| Web Navigation Phase 2 | Feb 14 | Result selection, page nav, scroll pagination |
| News Headlines System | Feb 14 | 16 RSS feeds, urgency classification, semantic dedup |
| Reminder System + Calendar | Feb 14 | Priority tones, nag behavior, Google Calendar 2-way sync |
| 12 Critical Bug Fixes | Feb 14 | Whisper pre-buffer, semantic routing, keyword greediness |

---

**Created:** Feb 10, 2026
