# TODO — Next Session

**Updated:** February 22, 2026

---

## Tier 1: High ROI — Do Now/Soon

### ~~1. Edge Case Testing Phase 2~~ — COMPLETE (28/30 automated)
**Status:** DONE. 132 tests total (39 unit + 93 routing). 4 deferred to future feature (mid-rundown interruption).
**See:** `docs/EDGE_CASE_TESTING.md` for full results.

### 2. Inject User Facts into Web Research
**Priority:** HIGH
**Concept:** JARVIS reasons about what it knows about the user (location, preferences) during `stream_with_tools()`.
**Risk:** History poisoning — needs careful scoping.

### ~~3. Document Generation Skill (#42)~~ — COMPLETE
**Status:** DONE (Feb 22). All 3 phases: PPTX + DOCX + PDF with web research + Pexels images.
**Needs:** Live console test with full multi-step command.

---

## Tier 2: Medium ROI — Next Wave

### 4. Email Skill (Gmail Integration)
**Priority:** MEDIUM
**Concept:** Voice-composed email via Gmail API + OAuth (same pattern as Calendar).
**Design:** `.archive/docs/MASTER_DESIGN.md` has contact DB schema and feature list.

### 5. Google Keep Integration
**Priority:** MEDIUM
**Concept:** "Add milk to the grocery list." Shared access with secondary user.

### 6. Audio Recording Skill
**Priority:** MEDIUM
**Location:** `skills/personal/audio_recording/`
**Concept:** Voice-triggered recording with natural playback queries.
- "Record audio" → tone → capture → "Stop recording" → saved as WAV
- "Play the recording from yesterday" → date-based lookup + playback
- 6 semantic intents (start, stop, play, query, list, export)

---

## Tier 3: Lower Effort — When Ready

### 7. "Onscreen Please" — Retroactive Visual Display
**Priority:** MEDIUM
**Concept:** Buffer last raw output. "Onscreen please" displays it retroactively.

### 8. Music Control (Apple Music)
**Priority:** MEDIUM
**Concept:** Playlist learning, volume via PulseAudio. Apple Music web interface is finicky.
**Design:** `.archive/docs/MASTER_DESIGN.md` has playlist DB schema.

### 9. LLM-Centric Architecture Migration
**Priority:** MEDIUM (wait for Qwen 3.5 release)
**Design:** `docs/DEVELOPMENT_VISION.md`
**Concept:** Skills become tools, not destinations. Incremental migration.

---

## Tier 4: Backlog

### Skill Editing System
**Design:** `docs/SKILL_EDITING_SYSTEM.md`
**Concept:** "Edit the weather skill" → LLM code gen → review → apply with backup.
**Note:** VS Code + Claude Code is faster for editing skills in practice.

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

- **CONSOLE TEST: Document generation** — Implemented (Feb 22). Restart JARVIS, test: "Look up the current top 5 LLMs in home use today, compare the pros and cons of each, and prepare a 7 slide PowerPoint that outlines what you've found. Name it llm_review.pptx and leave it in the share for me"
- **VOICE TEST: Smart ack suppression** — Implemented (Feb 22). Needs live voice restart + verification: conversational queries → no ack, research/complex → ack fires. See `memory/handoff_session40_feb22.md`.
- **Voice testing: bare ack as answer** — JARVIS asks question → "yeah" → treated as answer (needs reliable trigger)
- **Batch extraction (Phase 4) untested** — conversational memory batch fact extraction needs 25+ messages in one session to trigger

---

## Completed (Feb 10-22)

*Brief summary. Full details in git history.*

| Feature | Date | Notes |
|---------|------|-------|
| Document Generation (3 phases) | Feb 22 | PPTX/DOCX/PDF with web research + Pexels images. Two-stage LLM pipeline, 10 new keywords, 132/132 tests pass. Needs live console test. |
| Smart Ack Suppression | Feb 22 | Skip acks for fast queries (<=5 words, in-conversation <=12 words, answering JARVIS question). Research/working never suppressed. Needs live voice testing. |
| Edge Case Phase 2 Complete (132 tests) | Feb 22 | +10 tests: reminder ack, forget edge cases, compound dismissal, file editor. 28/30 Phase 2 automated, 4 deferred (mid-rundown = future feature) |
| Automated Test Suite (122 tests) | Feb 21 | Tier 1: 39 unit + Tier 2: 83 routing. Post-test cleanup (process guard + file removal + state reset). Phase 2 automated: 18/30 |
| Conversational Flow Refactor (4 phases) | Feb 21 | Persona → State → Router → Polish. 10 response pools, 38 router tests, contextual acks, adaptive windows |
| Response Pool Expansion | Feb 21 | reminder_ack 4→6, dismissal 5→7, greeting 6→8, news_pullup 3→5, ack_cache 8→10 |
| Whisper v2 Fine-Tuning | Feb 21 | 198 phrases, FIFINE K669B, GPU fp16, 94.4% live accuracy |
| 6 Conversational Bug Fixes | Feb 21 | Transparency, MCU removal, double honorific, UnboundLocalError, extraction text, dismissal suffix |
| Web Chat UI (5 phases) | Feb 20 | aiohttp WS, streaming, file handling, history, markdown, sessions sidebar |
| File Editor Skill | Feb 20 | 5 intents, confirmation flow, LLM-generated content |
| Ambient Wake Word Filter | Feb 20 | Position, copula, threshold 0.80, length. 8/8 commands pass, 8/8 ambient blocked |
| Edge Case Testing Phase 1 | Feb 20 | 37/40 pass (92.5%). 14 failures fixed across 4 rounds |
| Embedding Cache + STT Warm-up | Feb 20 | Pre-computed semantic embeddings, STT dummy transcription warm-up |
| Publish Script Non-Interactive | Feb 20 | `--auto` flag for CI-friendly publish |
| Console Web Research + Prompt v2 | Feb 19 | `stream_with_tools()` in console, deflection safety net, prescriptive prompt v2 |
| Qwen Sampling Params + API Key Rotation | Feb 19 | top_p=0.8, top_k=20 in all 6 llama.cpp payloads |
| Document Ingestion (3 phases) | Feb 19 | prompt_toolkit, /paste /file /clipboard /append /context /clear |
| GNOME Desktop Integration (5 phases) | Feb 19 | Extension (D-Bus bridge), desktop_manager, app launcher v2.0 — 16 intents |
| Google Calendar Sync Token Fix | Feb 19 | Removed `orderBy` from initial sync, incremental sync works |
| Web Research (5 phases) | Feb 18 | Qwen 3-8B tool calling + DuckDuckGo + trafilatura |
| GitHub Publishing System | Feb 18 | Automated PII redaction, `--auto` publish |
| Bug Squashing Blitz (27+ fixes) | Feb 18 | Ack collision, keyword greediness, dismissal, decimal TTS, aplay lazy open |
| Gapless TTS Streaming | Feb 17 | StreamingAudioPipeline, single persistent aplay, zero-gap playback |
| Hardware Failure Graceful Degradation | Feb 17 | Startup retry, device monitor, degraded mode |
| Conversational Memory (6 phases) | Feb 17 | SQLite facts + FAISS + recall + batch + proactive + forget |
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
