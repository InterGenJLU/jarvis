# TODO — Next Session

**Updated:** February 19, 2026

---

## Tier 1: High ROI, Low Effort — Do Now/Soon

### 1. Whisper Retraining — Scheduled Feb 21
**Priority:** HIGH
**Plan:**
1. Analyze logs Feb 14-21 for misheard phrases
2. Generate training data from corrections
3. Retrain with expanded dataset (149 original + new)
4. Convert to CTranslate2 and deploy
**Files:** `core/stt.py`, `/mnt/models/voice_training/`
**Note:** Remove `_debug_save_audio()` from `stt.py` after retraining

### 2. Quick Wins — All Complete!
- ~~News urgency filtering~~ — Done (Feb 19, `1d447c2` + `e1c4611`). "read critical headlines" works
- ~~Qwen sampling params~~ — Done (Feb 19, `25b5f0a`). top_p=0.8, top_k=20
- ~~Rotate OpenWeather API key~~ — Done (Feb 19, `25b5f0a`)

---

## Tier 2: High ROI, Medium Effort — Next Wave

### 4. Inject User Facts into Web Research
**Priority:** HIGH
**Concept:** JARVIS reasons about what it knows about the user (location, preferences) during `stream_with_tools()`.
**Risk:** History poisoning — needs careful scoping.

### 5. Minimize Web Search Latency
**Priority:** MEDIUM
**Concept:** Forced search adds ~5-8s; explore caching, parallel fetch, snippet-only mode.

### 6. Email Skill (Gmail Integration)
**Priority:** MEDIUM
**Concept:** Voice-composed email via Gmail API + OAuth (same pattern as Calendar).
**Design:** `.archive/docs/MASTER_DESIGN.md` has contact DB schema and feature list.

### 7. Google Keep Integration
**Priority:** MEDIUM
**Concept:** "Add milk to the grocery list." Shared access with secondary user.

---

## Tier 3: Medium ROI, Higher Effort — When Ready

### 8. Audio Recording Skill
**Priority:** MEDIUM
**Location:** `skills/personal/audio_recording/`
**Concept:** Voice-triggered recording with natural playback queries.
- "Record audio" → tone → capture → "Stop recording" → saved as WAV
- "Play the recording from yesterday" → date-based lookup + playback
- 6 semantic intents (start, stop, play, query, list, export)

### 9. "Onscreen Please" — Retroactive Visual Display
**Priority:** MEDIUM
**Concept:** Buffer last raw output. "Onscreen please" displays it retroactively.

### 10. Music Control (Apple Music)
**Priority:** MEDIUM
**Concept:** Playlist learning, volume via PulseAudio. Apple Music web interface is finicky.
**Design:** `.archive/docs/MASTER_DESIGN.md` has playlist DB schema.

### 11. LLM-Centric Architecture Migration
**Priority:** MEDIUM (wait for Qwen 3.5 release)
**Design:** `docs/DEVELOPMENT_VISION.md`
**Concept:** Skills become tools, not destinations. Incremental migration.

---

## Tier 4: Lower Priority — Backlog

### Skill Editing System
**Design:** `docs/SKILL_EDITING_SYSTEM.md`
**Concept:** "Edit the weather skill" → LLM code gen → review → apply with backup.
**Note:** VS Code + Claude Code is faster for editing skills in practice.

### Web Dashboard
**Priority:** LOW (demo/showoff feature)
**Concept:** Local Flask/FastAPI web UI for JARVIS management.

### STT Worker Process
**Design:** `docs/STT_WORKER_PROCESS.md`
**Concept:** GPU isolation via separate process. Only needed if GPU conflicts resurface.

### Automated Skill Generation
**Concept:** Q&A → build → test → review → deploy. Depends on Skill Editing.

### Mobile Access
**Concept:** Remote command via phone. Different tech stack entirely.

---

## Tier 5: Aspirational — Someday/Maybe

- **Malware Analysis Framework** — QEMU sandbox, VirusTotal/Any.run API, CISA-format reports. Build when a specific engagement needs it.
- **Video / Face Recognition** — webcam → people/pets/objects. Hardware-dependent.
- **Tor / Dark Web Research** — Brave Tor mode, safety protocols. Specialized professional use.
- **Emotional context awareness** — laugh/frustration/distress detection. Research-level ML.
- **Voice cloning (Paul Bettany)** — tested Coqui, rejected. Revisit when open-source matures.
- **Proactive AI** — suggest actions based on patterns. Needs significant usage data first.
- **Self-modification** — JARVIS proposes own improvements. Far future.
- **Home automation** — IoT integration. Hardware-dependent.

---

## Active Bugs

None!

---

## Minor Loose Ends

- **Voice testing: bare ack as answer** — JARVIS asks question → "yeah" → treated as answer (needs reliable trigger)
- **Batch extraction (Phase 4) untested** — conversational memory batch fact extraction needs 25+ messages in one session to trigger
- ~~Console logging~~ — Fixed (Feb 19). Was writing to jarvis.log instead of dedicated console.log
- ~~Topic shift threshold tuning~~ — Already set to 0.35 in config.yaml, confirmed live

---

## Completed (Feb 10-19)

*Brief summary. Full details in `memory/` files and git history.*

| Feature | Date | Notes |
|---------|------|-------|
| Console Web Research + Prompt v2 | Feb 19 | `stream_with_tools()` in console, deflection safety net, prescriptive prompt v2 ("verifiable answer" + numbered rules + anti-deflection) |
| Qwen Sampling Params + API Key Rotation | Feb 19 | top_p=0.8, top_k=20 in all 6 llama.cpp payloads. Rotated OpenWeather API key, updated redact.conf |
| Document Ingestion Phase 3 | Feb 19 | Tab completion (slash commands + /file paths), doc-aware LLM hint, dynamic max_tokens=600 |
| Document Ingestion Phase 2 | Feb 19 | /file (binary reject, --tail, 500KB warn), /clipboard (wl-paste), /append, drag-and-drop auto-detect |
| Document Ingestion Phase 1 | Feb 19 | prompt_toolkit, DocumentBuffer, /paste + /context + /clear + /help, LLM injection |
| Embedding Cache + STT Warm-up | Feb 20 | Pre-compute semantic embeddings at load time (`_semantic_embedding_cache`), STT dummy transcription warm-up. Grok review items #8+#9 (`56f5037`) |
| Publish Script Non-Interactive | Feb 20 | `--auto` flag for CI-friendly publish (auto-generate commit msg + push) |
| GNOME Desktop Integration (5 phases) | Feb 19 | Extension (D-Bus bridge), desktop_manager, app launcher migration, volume, workspace, clipboard, notifications — 16 intents |
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
