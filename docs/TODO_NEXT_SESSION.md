# TODO — Next Session

**Updated:** February 26, 2026

---

## Tier 1: High ROI — Do Now/Soon

### 0. Mobile iOS App — COMING VERY SOON (#60)
**Priority:** HIGHEST — Active Development
**Concept:** Native Swift iPhone app with full JARVIS access from anywhere.
**Key Features:**
- Always-listening "Jarvis" wake word (Porcupine iOS SDK, on-device, background mode)
- Real-time voice via WebRTC (server-side Whisper STT + Kokoro TTS — same voice as desktop)
- Full chat UI via WKWebView (existing web UI, zero rebuild)
- Secure remote access via Tailscale VPN (no public exposure)
- Apple Shortcuts integration ("Hey Siri, ask Jarvis...")
- iOS 18 Vocal Shortcuts as zero-battery wake word fallback
**Phases:** 6 phases, ~5-8 days total
**Prerequisites:** Apple Developer account ($99/yr), Mac with Xcode, Tailscale (free), Picovoice (free)
**Plan:** `memory/plan_mobile_ios_app.md`

### 1. Inject User Facts into Web Research
**Priority:** HIGH
**Concept:** JARVIS reasons about what it knows about the user (location, preferences) during `stream_with_tools()`.
**Risk:** History poisoning — needs careful scoping.

---

## Tier 2: Medium ROI — Next Wave

### 2. Email Skill (Gmail Integration)
**Priority:** MEDIUM
**Concept:** Voice-composed email via Gmail API + OAuth (same pattern as Calendar).
**Design:** `.archive/docs/MASTER_DESIGN.md` has contact DB schema and feature list.

### 3. Google Keep Integration
**Priority:** MEDIUM
**Concept:** "Add milk to the grocery list." Shared access with secondary user.

### 4. Dual-Model Voice Recognition (Speaker Routing)
**Priority:** MEDIUM (waiting for secondary user enrollment)
**Concept:** Speaker-ID d-vector comparison (~5ms) routes to primary user's fine-tuned Whisper vs stock whisper-base. Two CTranslate2 models loaded simultaneously (~300MB total).
**Design:** `memory/plan_erica_voice_windows_port.md`
**Timing:** After secondary user can do 10-min enrollment session. Half-day implementation.

### 5. Document Refinement Follow-ups (#49)
**Priority:** MEDIUM
**Concept:** Cache last doc structure/research, add `refine_document` intent for iterative revision.
- "Make slide 3 more detailed" / "add more statistics" after doc gen
- Cache structure JSON + research context on skill instance, 10-min expiry

### 6. AI Image Generation — FLUX.1-schnell (#50)
**Priority:** MEDIUM
**Concept:** Hybrid Pexels + FLUX for document generation. LLM decides per-slide.
- Pexels fails for tech/abstract topics. FLUX FP8 fits 20GB VRAM, ~12-20s/image
- Research complete: `memory/research_image_gen_ocr_selfprompt.md`

### 7. Vision/OCR Skill — Phase 1 Tesseract (#51)
**Priority:** MEDIUM
**Concept:** "Read this" / "what does this say" via screenshot + Tesseract OCR.
- CPU-only, 95-98% accuracy, 0.5-2s/page. Input via clipboard/screenshot/file
- 4 intents: read_screen, describe_image, read_document, read_chart

### 8. "Onscreen Please" — Retroactive Visual Display (#11)
**Priority:** MEDIUM
**Concept:** Buffer last raw output. "Onscreen please" displays it retroactively.

### 9. Audio Recording Skill
**Priority:** MEDIUM
**Location:** `skills/personal/audio_recording/`
**Concept:** Voice-triggered recording with natural playback queries.
- "Record audio" -> tone -> capture -> "Stop recording" -> saved as WAV
- "Play the recording from yesterday" -> date-based lookup + playback
- 6 semantic intents (start, stop, play, query, list, export)

---

## Tier 3: Lower Effort — When Ready

### 10. Music Control (Apple Music)
**Priority:** MEDIUM
**Concept:** Playlist learning, volume via PulseAudio. Apple Music web interface is finicky.
**Design:** `.archive/docs/MASTER_DESIGN.md` has playlist DB schema.

### 11. LLM-Centric Architecture Migration
**Priority:** MEDIUM (wait for Qwen 3.5 release)
**Design:** `docs/DEVELOPMENT_VISION.md`
**Concept:** Skills become tools, not destinations. Incremental migration.

### 12. Docker Container (Web UI Mode)
**Priority:** MEDIUM
**Concept:** Lowest-barrier community deployment. Web UI only (no mic). Proves the concept for community adoption.
**Effort:** 3-5 days
**Design:** `memory/plan_erica_voice_windows_port.md`

---

## Tier 4: Backlog

### Skill Editing System
**Design:** `docs/SKILL_EDITING_SYSTEM.md`
**Concept:** "Edit the weather skill" -> LLM code gen -> review -> apply with backup.
**Note:** VS Code + Claude Code is faster for editing skills in practice.

### STT Worker Process
**Design:** `docs/STT_WORKER_PROCESS.md`
**Concept:** GPU isolation via separate process. Only needed if GPU conflicts resurface.

### Automated Skill Generation
**Concept:** Q&A -> build -> test -> review -> deploy. Depends on Skill Editing.

### Windows Native Port
**Concept:** Full JARVIS on Windows. ~2-3 weeks effort. Biggest audience for community adoption.
**Design:** `memory/plan_erica_voice_windows_port.md`

### ~~Mobile Access~~ — PROMOTED to Tier 1 as #60 (native iOS app)
See Tier 1 above.

---

## Tier 5: Aspirational — Someday/Maybe

- **Malware Analysis Framework** — QEMU sandbox, VirusTotal/Any.run API, CISA-format reports. Build when a specific engagement needs it.
- **Video / Face Recognition** — webcam -> people/pets/objects. Hardware-dependent. Qwen3.5 mmproj vision encoder downloaded but not loaded; image processing is future.
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

## Pending Live Tests

- **Social introductions** — restart JARVIS, test "Meet my niece Arya" end-to-end via voice, verify TTS pronunciation overrides
- **Task planner compound requests** — verify predictive timing announcement, LLM step evaluation, pause/resume ("wait" → "continue") end-to-end via voice
- **Batch extraction (Phase 4)** — needs 25+ messages in one session to trigger
- **Qwen3.5 vision features** — mmproj encoder downloaded, not yet integrated. Future: image understanding via `--mmproj` flag (#52)

## Deferred — Revisit After Extended Usage

*Task planner enhancements shelved Feb 25. Check mid-March 2026.*

- **Plan templates (#56)** — cache successful plan structures. Revisit when repeated identical compound requests are observed
- **Plan feedback (#57)** — post-execution LLM eval + pattern storage. Revisit when per-step eval data shows recurring failures
- **Parallel step execution (#58)** — concurrent independent steps. Revisit when plans regularly exceed 4-5 steps (April 2026+)

---

## Completed (Feb 10-25)

*Brief summary. Full details in git history and `docs/PRIORITY_ROADMAP.md`.*

| Feature | Date |
|---------|------|
| WebUI health check spoken/on-screen mismatch fix | Feb 23 |
| jarvis-web.service — systemd service for web UI auto-start | Feb 23 |
| Preferred-mic hot-swap recovery — device monitor recovers from wrong-mic fallback | Feb 23 |
| Whisper brand-name corrections — "and videos"→"amd's", "in video"→"nvidia" | Feb 23 |
| Ack speaker-to-mic bleed fix — pause listening during ack playback | Feb 23 |
| Edge Case Tests expanded to 152 (Phase 1E + routing) | Feb 22-23 |
| Doc gen prompt overhaul (prescriptive depth) | Feb 22 |
| Qwen3-VL-8B Model Upgrade (ROCm rebuild, self-quantized Q5_K_M) | Feb 22 |
| Qwen3.5-35B-A3B Upgrade (MoE Q3_K_M, unsloth, 48-63 tok/s) | Feb 24 |
| Task Planner — 4 phases complete (self-awareness → compound detection → guardrails → advanced) | Feb 24-25 |
| Task Planner bug fixes — pause/resume guards, eval timeout, skip-that | Feb 25 |
| Social introductions — PeopleManager, multi-turn intro skill, P2.6 router, 270 tests | Feb 25 |
| Phase 9C: Intro state machine tests — 10 multi-turn tests (happy path, correction, P2.6, timeout, etc.), 294 total tests | Feb 26 |
| TTS pronunciation fixes (6 items), RAM reporting, llama-server boot race fix | Feb 25 |
| Document Generation (PPTX/DOCX/PDF with web research + Pexels) | Feb 22 |
| Smart Ack Suppression | Feb 22 |
| Document generation live tested (7 bugs fixed during session 45) | Feb 22 |
| Smart ack suppression live tested | Feb 22 |
| Automated Test Suite (122 tests) | Feb 21 |
| Conversational Flow Refactor (4 phases) | Feb 21 |
| Whisper v2 Fine-Tuning (198 phrases, 94.4%) | Feb 21 |
| Web Chat UI (5 phases) | Feb 20 |
| File Editor Skill | Feb 20 |
| Ambient Wake Word Filter | Feb 20 |
| Edge Case Testing Phase 1 | Feb 20 |
| Console Web Research + Prompt v2 | Feb 19 |
| Document Ingestion (3 phases) | Feb 19 |
| GNOME Desktop Integration (5 phases) | Feb 19 |
| Web Research (5 phases) | Feb 18 |
| GitHub Publishing System | Feb 18 |
| Gapless TTS Streaming | Feb 17 |
| Conversational Memory (6 phases) | Feb 17 |
| Context Window (4 phases) | Feb 17 |
| User Profile System (5 phases) | Feb 16 |
| Kokoro TTS Integration | Feb 16 |
| Latency Refactor (4 phases) | Feb 16 |
| Developer Tools (13 intents) | Feb 15 |
| News Headlines System | Feb 14 |
| Reminder System + Calendar | Feb 14 |

---

**Created:** Feb 10, 2026
