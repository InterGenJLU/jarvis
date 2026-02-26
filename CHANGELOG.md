# JARVIS Changelog

## [2026-02-25] - Social Introductions + People Manager + TTS Pronunciation

### Major Features
- **Social Introductions Skill** — `skills/personal/social_introductions/`
  - "Meet my niece Arya" triggers multi-turn butler-style introduction flow
  - 5 semantic intents: meet, who-is, recall, forget, update
  - Multi-turn state machine: name confirmation → pronunciation check → fact gathering → complete
  - Relationship extraction (niece, friend, brother, etc.)
- **PeopleManager** — `core/people_manager.py`
  - SQLite-backed people + person_facts database
  - TTS pronunciation overrides (phonetic spellings injected via tts_normalizer)
  - LLM context injection for known contacts mentioned in utterances
- **Persona Expansion** — `core/persona.py`
  - 7 new response pools for introduction flows (~25 templates)
  - Helper functions: intro_name_confirm, intro_pron_check, intro_complete, etc.
- **Router P2.6** — Introduction state machine intercept in ConversationRouter priority chain

### Other Changes
- Pipeline + console: PeopleManager initialization and wiring
- config.yaml: people.enabled + db_path configuration
- TTS pronunciation fixes (6 items) and RAM reporting accuracy
- llama-server health check retry for boot race condition
- Edge case tests expanded to 270 (112 unit + 130 routing + 28 LLM), 100% pass rate

---

## [2026-02-24] - Qwen3.5-35B-A3B Upgrade + LLM Test Suite + Voice Fixes

### Major Features
- **Qwen3.5-35B-A3B Model Upgrade** — `llama-server.service`
  - MoE architecture: 35B total params, 256 experts, 8+1 active (~3B active)
  - Q3_K_M quantization (unsloth, imatrix-calibrated), 16GB model file
  - 48-63 tok/s (comparable to old 8B dense), IFEval 91.9 (was ~70s)
  - VRAM: 19.5/20.5 GB (~1 GB headroom), `--parallel 1` for single-user
  - Replaces Qwen3-VL-8B Q5_K_M

### Testing
- **Tier 4 LLM test suite** — 28 automated tests validating live model responses
  - System prompt adherence (5): no-filler, imperial units, brevity, date awareness
  - Personality & opinions (3): no "as an AI", humor, warmth
  - Tool calling (4): search when needed, restrain when not
  - Structured output (2): clean JSON extraction
  - Multi-turn context (2): fact recall, topic tracking
  - Safety & refusal (3): phishing, prompt injection, malware
  - Hallucination resistance (2): fake entities correctly rejected
  - Technical knowledge (4): cybersecurity, Python, Linux, code gen
  - Voice assistant fitness (3): conversational flow, brevity, imperial
  - Total suite: 180/180 (Tier 1: 39, Tier 2: 113, Tier 4: 28)
  - Requires llama-server running; skips gracefully when unavailable

### Bug Fixes
- **Web search routing** — removed "search" keyword alias that hard-routed all search commands to browser instead of LLM web research pipeline. Site-specific searches (YouTube, Google) still route correctly via keyword/semantic matching
- **Ack speaker-to-mic bleed** — added 0.35s acoustic settling delay in `resume_listening()` to prevent TTS echo from re-triggering intents
- **Whisper correction** — added "quinn" → "qwen" transcription correction

---

## [2026-02-23] - Demo Prep + LLM Metrics + 4 Bug Fixes

### Major Features
- **LLM Metrics Dashboard (5 phases)** — `core/metrics_tracker.py`, `jarvis_web.py`, `web/metrics.html`
  - Real-time tracking of LLM calls: tokens, latency, local vs API, quality gate retries
  - Web dashboard at `/metrics` with charts, tables, and summary stats
  - Persistent SQLite storage with 30-day retention
  - Per-skill and per-model breakdowns
- **jarvis-web.service** — systemd user service for web UI, auto-start after jarvis.service

### Bug Fixes
- **Web research page fetch timeout** — 20s+ → ~5s. Replaced `trafilatura.fetch_url()` with `requests.get(timeout=4)` + manual `pool.shutdown(wait=False, cancel_futures=True)`
- **Desktop manager init order** — skills loaded before desktop manager was created → "not available" for all window management. Moved init before skill loading
- **Health check PipeWire mic detection** — `sounddevice.query_devices()` misses PipeWire USB devices. Added `pactl list sources short` fallback
- **Audio output PipeWire routing** — changed `output_device: plughw:0,0` → `output_device: default` for PipeWire compatibility (enables OBS coexistence)

### Other Changes
- Preferred-mic hot-swap recovery — device monitor recovers from wrong-mic fallback after boot race
- Ack speaker-to-mic bleed fix — mic paused during ack playback
- Whisper brand-name corrections — "and videos"→"amd's", "in video"→"nvidia"
- WebUI health check spoken/on-screen mismatch fixed
- Edge case tests expanded to 152 (from 144)

---

## [2026-02-22] - Document Generation + Qwen3-VL-8B + Smart Ack Suppression

### Major Features
- **Document Generation** — `skills/system/file_editor/document_generator.py`
  - PPTX, DOCX, PDF generation via two-stage LLM pipeline
  - Optional web research for content enrichment
  - Pexels stock image integration with per-slide relevance scoring
  - Saved to `share/` directory with notification
- **Qwen3-VL-8B Model Upgrade** — `llama-server.service`
  - Self-quantized Q5_K_M from F16 source (llama-quantize)
  - llama.cpp rebuilt with ROCm (`GGML_HIP=ON`)
  - 80.2 tok/s generation, vision-capable (mmproj encoder downloaded)
- **Smart Ack Suppression** — `core/pipeline.py`
  - Skip acknowledgments for fast/conversational queries
  - Reduces unnecessary "one moment, sir" for instant responses

### Bug Fixes
- Doc gen prompt overhaul (prescriptive depth rules for Qwen)
- publish.sh README protection (prevents overwrite of curated public README)
- 7 doc gen live testing bugs fixed during demo prep
- Edge case tests expanded to 144 (Phase 1E)

---

## [2026-02-21] - Conversational Flow Refactor + Whisper v2

### Major Features
- **Conversational Flow Refactor (4 phases)** — `core/persona.py`, `core/conversation_state.py`, `core/conversation_router.py`, `core/pipeline.py`
  - Phase 1: Persona module — 10 response pools (~50 templates), system prompts, honorific injection
  - Phase 2: ConversationState — turn tracking, intent history, question detection, research context
  - Phase 3: ConversationRouter — shared priority chain for voice/console/web (one router, three frontends)
  - Phase 4: Response flow polish — contextual ack selection (10 tagged phrases), smarter follow-up windows (adaptive 4-7s), conversation timeout cleanup, suppress LLM opener collision
- **Router Test Suite** — `scripts/test_router.py`, 38 tests validating routing decisions without live LLM/mic
- **Whisper v2 Fine-Tuning** — 198 training phrases (up from 149), FIFINE K669B mic, GPU fp16, 94.4% live accuracy
- **Response Pool Expansion** — reminder_ack 4→6, dismissal 5→7, greeting 6→8, news_pullup 3→5, ack_cache 8→10
- **6 Conversational Bug Fixes** — transparency pattern, MCU roleplay removal, double honorific, UnboundLocalError, raw extraction text, dismissal courtesy suffix

---

## [2026-02-20] - Web UI + File Editor + Ambient Filter + Edge Case Testing

### Major Features
- **Web Chat UI (5 phases)** — `jarvis_web.py`, `web/index.html`, `web/style.css`, `web/app.js`
  - Phase 1: MVP — aiohttp WebSocket server, dark theme, stats header, send/receive
  - Phase 2: Streaming + file handling — token-by-token LLM delivery, drag/drop files, slash commands, quality gate
  - Phase 3: History + notifications — paginated `/api/history`, scroll-to-load-more, floating announcement banners
  - Phase 4: Polish — markdown rendering with XSS protection, code blocks + copy, timestamps, Ctrl+L, responsive breakpoints
  - Phase 5: Session sidebar — 30-min gap detection, hamburger toggle, session rename, pagination, LIVE badge
- **File Editor Skill** — `/mnt/storage/jarvis/skills/system/file_editor/`
  - 5 intents: write_file, edit_file, read_file, delete_file, list_share_contents
  - Confirmation flow for destructive operations, LLM-generated content
- **Ambient Wake Word Filter** — `core/continuous_listener.py`, `core/pipeline.py`
  - Multi-signal: position check, copula detection, threshold 0.70→0.80, length filter
  - Eliminates false triggers from ambient conversation about JARVIS
- **Edge Case Testing Phase 1** — `docs/EDGE_CASE_TESTING.md`
  - ~200 test cases across 9 phases, 37/40 pass (92.5%)
  - 14 routing failures fixed across 4 rounds: bare word guard, tie-breaking, priority chain, noise filter, intent_id collision

### Bug Fixes
- Rundown substring bug (`"no" in "diagnostic"` → word-boundary matching)
- 3 file editor routing bugs (keyword ownership, global semantic fallback, confirmation interception)
- Semantic embedding cache for faster routing (`56f5037`)
- STT warm-up to eliminate cold start latency (`56f5037`)

---

## [2026-02-18/19] - Web Research + Desktop Integration + Document Ingestion + GitHub Publishing

### Major Features
- **Web Research (5 phases)** — `core/web_research.py`, `core/llm_router.py`
  - Qwen 3-8B native tool calling with `tool_choice=auto`
  - DuckDuckGo search + trafilatura page extraction + multi-source synthesis
  - Prescriptive prompt v2: "verifiable answer" + numbered rules + anti-deflection
  - Console web research with deflection safety net
- **GNOME Desktop Integration (5 phases)** — `extensions/jarvis-desktop@jarvis/`, `core/desktop_manager.py`
  - Custom GNOME Shell extension with D-Bus bridge (14 methods)
  - Desktop manager: lazy D-Bus, wmctrl fallback, pactl, notify-send, wl-clipboard
  - App Launcher v2.0: 16 intents (launch/close, fullscreen/minimize/maximize, volume, workspace, focus, clipboard)
- **Document Ingestion (3 phases)** — `jarvis_console.py`
  - prompt_toolkit console with /paste, /file, /clipboard, /append, /context, /clear
  - DocumentBuffer with token budget, binary rejection, tab completion
  - `<document>` XML injection into LLM context
- **GitHub Publishing System** — `scripts/publish/publish.sh`
  - Automated PII redaction (47 patterns), verification, non-interactive `--auto` mode
  - rsync-based sync from dev → public repo

### Bug Fixes
- 27+ fixes: ack collision, keyword greediness, dismissal detection, decimal TTS, aplay lazy open
- Streaming delivery: sentence-only chunking, per-chunk metric stripping, context flush on shutdown
- Scoped TTS subprocess control (replaced global `pkill -9` with tracked subprocess kill)
- News urgency filtering, Google Calendar reminder offsets, parallel web search page fetches

---

## [2026-02-17] - Conversational Memory + Health Check

### Major Features
- **Conversational Memory System (6 phases)** — `core/memory_manager.py`
  - Phase 1: SQLite fact store, 11 regex extraction patterns, CRUD with dedup/supersede
  - Phase 2: FAISS semantic indexing, backfill 1,225 messages (0.9s)
  - Phase 3: Semantic search + recall detection (9 recall patterns, topic extraction)
  - Phase 4: LLM batch extraction (background Qwen analysis every 25 messages)
  - Phase 5: Proactive memory surfacing (confidence-gated, 1 fact per conversation window)
  - Phase 6: Forgetting + transparency commands (pending forget with 30s expiry)
- **System Health Check** — `core/health_check.py`
  - 5-layer diagnostic (hardware, services, models, skills, connectivity)
  - ANSI-colored terminal report + brief voice summary

### Bug Fixes
- Forget confirmation matching fix
- Improved response tone for memory operations

---

## [2026-02-16] - Kokoro TTS + User Profiles + Latency Refactor

### Major Features
- **Kokoro TTS Integration** — 82M model, CPU, 50/50 fable+george voice blend, Piper as fallback
- **User Profile System (5 phases)** — `core/user_profile.py`, `core/speaker_id.py`, `core/honorific.py`
  - Phase 1: Honorific infrastructure (~470 hardcoded "sir" → dynamic `{honorific}` across 19 files)
  - Phase 2: ProfileManager + SpeakerIdentifier (resemblyzer d-vectors)
  - Phase 3: Pipeline integration (real-time speaker identification)
  - Phase 4: Voice enrollment (`scripts/enroll_speaker.py`)
  - Phase 5: Enrollment flow + dynamic honorific per identified speaker
- **Latency Refactor (4 phases)** — all complete
  - Phase 1: Streaming TTS (chunked audio output)
  - Phase 2: Ack cache (pre-generated acknowledgments, no LLM call)
  - Phase 3: Streaming LLM (token-by-token output)
  - Phase 4: Event-driven pipeline (Coordinator + STT/TTS workers)
- **TODO_NEXT_SESSION.md overhaul** — 1,805 → 175 lines, living document

### Bug Fixes
- Speaker-to-mic bleed mitigation (TTS mutes mic during playback)
- TTS pronunciation fixes
- LLM prompt leakage fix
- Rundown routing priority fix

---

## [2026-02-15] - Developer Tools + Console Mode + PyTorch Unification

### Major Features
- **Developer Tools Skill** — 13 semantic intents
  - Codebase search, git multi-repo (status/log/diff/branch), system admin
  - General shell access, "show me" visual output, 3-tier safety system
- **Console Mode** — `jarvis_console.py`
  - Text mode (type commands), hybrid mode (type + spoken), speech mode
  - Rich stats panel (match layer, skill, confidence, timing, LLM tokens)
- **PyTorch + CTranslate2 Coexistence** — torch 2.10.0+rocm7.1 + CT2 4.7.1
  - Both use `/opt/rocm-7.2.0/lib/` — no more version mismatch
- **FIFINE K669B Microphone Upgrade** — udev rule (`scripts/99-fifine-k669b.rules`)

---

## [2026-02-14] - Bug Fix Marathon + News + Reminders + Web Nav Phase 2

### Major Features
- **News Headlines System** — `core/news_manager.py`
  - 16 RSS feeds, 15min poll, urgency classification, semantic dedup, voice delivery
- **Reminder System** — `core/reminder_manager.py`
  - Priority tones, nag behavior, acknowledgment tracking
  - Google Calendar two-way sync (dedicated JARVIS calendar)
  - Interactive daily & weekly rundowns (state machine)
- **Web Navigation Phase 2** — result selection, page navigation, scroll pagination (YouTube/Reddit), window management
- **Conversation Windows** — timer-based auto-close, multi-turn, noise filter

### Bug Fixes (12 total)
- Whisper silence pre-buffer (`_trim_leading_silence()` in `core/stt.py`)
- Semantic intent routing broken since day one (direct handler call in `execute_intent`)
- Keyword routing Layer 4a too greedy (generic keywords blocklist)
- VAD pre-buffer overlap (snapshot ring buffer at speech detection)
- Double-speak bug, keyword greediness, and 8 others

---

## [2026-02-12/13] - GPU Breakthrough + CTranslate2 🚀

### Major Features
- **GPU-Accelerated STT** - CTranslate2 with ROCm 7.2 on RX 7900 XT
  - 0.1-0.2s transcription (10-20x faster than CPU)
  - Custom-built CTranslate2 with HIP/ROCm support
  - Fine-tuned Whisper model converted to CTranslate2 format
- **Three-repo architecture** - Code, skills, and models on separate drives

### Technical Details
- Built CTranslate2 from source with `-DWITH_HIP=ON` for AMD GPU
- Resolved ROCm 6.2 vs 7.2 conflict (PyTorch bundles ROCm 6.2, system uses 7.2)
- Critical rule: NO torch imports in `core/stt.py` (import order determines loaded ROCm version)
- 18 hours of debugging to isolate the root cause

### Infrastructure
- Added third git repo for models (`/mnt/models/`)
- Automated daily backups to `/mnt/models/backups/`
- Systemd service updated with ROCm environment variables

---

## [Unreleased] - 2026-02-10

### Major Features Added
- **Semantic Intent Matching** - ML-based intent recognition using sentence transformers
  - 90% reduction in exact patterns (218 → 22)
  - ~100ms matching latency
  - Works offline, no API calls needed

### Skills Updated
- **system_info** - Migrated to semantic matching (78 → 1 intent)
- **conversation** - Migrated to semantic matching (86 → 10 intents)
- **weather** - Hybrid approach: semantic + exact overrides (39 → 6 + 3)
- **time_info** - Migrated to semantic matching (15 → 2 intents)

### Bug Fixes
- Fixed LLM echo bug (was returning user input verbatim)
- Fixed LLM timeout (reduced context 8192 → 2048, added repeat penalty)
- Optimized VAD settings (reduced phantom noise detections by 90%+)
- Removed noisy debug logs from skill_manager

### Performance Improvements
- LLM generation: 30s timeout → 5-10s response time
- Audio transcription accuracy improved significantly
- VAD false positive rate reduced dramatically

### Infrastructure
- Added git version control (2 repos: core + skills)
- Created semantic handler validation script
- Added audio configuration validation/fix scripts
- Disabled RNNoise (was causing audio artifacts)

### Configuration Changes
- Audio: `use_rnnoise: false`
- VAD: `aggressiveness: 3`, `speech_frames_threshold: 15`, `silence_frames_threshold: 30`
- LLM: `context_size: 2048`, `repeat_penalty: 1.1`, `top_p: 0.9`

### Known Issues
- Whisper struggles with "tell me" pronunciation (southern accent)
- Some conversation responses feel unnatural (documented for future work)
- Voice sounds too young (need to find better Piper voice model)

### Development Tools Added
- `validate_semantic_handlers.sh` - Verify semantic intent handlers exist
- `check_audio_config.sh` - Validate audio settings
- `fix_audio_config.sh` - Auto-fix audio configuration
- `diagnose_audio_gremlins.sh` - Audio quality diagnostics

---

## [Previous] - Before 2026-02-10
- Initial JARVIS implementation with exact pattern matching
- Basic skills: system_info, weather, time_info, conversation
- Whisper STT, Piper TTS, Mistral-7B LLM (since migrated to Qwen)

## [2026-02-11] - LLM Upgrade + Whisper Training + Filesystem Skill 🚀

### Major Features
- **Qwen 2.5-7B LLM Integration** - Upgraded from Mistral via llama-server REST API
- **Custom Whisper Model** - Fine-tuned on 149 phrases for Southern accent (98% accuracy)
- **Filesystem Skill** - Semantic file search and code analysis capabilities
- **Semantic Intent Matching** - AI-powered intent recognition using embeddings

### Added
- `llama-server.service` - Systemd service for LLM REST API
- `core/stt.py` - Custom Whisper model via faster-whisper (CTranslate2)
- `/mnt/storage/jarvis/skills/system/filesystem/` - New filesystem skill
- `core/skill_manager._match_semantic_intents()` - Semantic matching layer
- `/mnt/models/voice_training/` - Training dataset and scripts
- `docs/SKILL_DEVELOPMENT.md` - Comprehensive skill creation guide
- `docs/SESSION_2026-02-11.md` - Detailed session documentation

### Changed
- `core/llm_router.py` - Switched from subprocess to REST API calls
- `config.yaml` - Added Qwen model path and fine-tuned Whisper settings
- `jarvis_continuous.py` - Fixed TTS output for skill responses
- `core/skill_manager.py` - Preload embedding model to prevent audio overflow

### Fixed
- Wake word recognition now 100% accurate (was ~50%)
- Audio overflow warnings eliminated (preloaded embedding model)
- Skill responses now properly spoken via TTS
- File counting excludes virtual environments (accurate results)
- LLM responses clean and concise (proper ChatML handling)

### Performance
- Whisper transcription: 2s (unchanged)
- LLM response: 1-2s via REST (improved from 3-5s)
- Semantic matching: <1s (preloaded model)
- Total command latency: 3-5s (acceptable)

### Technical Details
- **Whisper Model:** Fine-tuned openai/whisper-base (290MB)
  - Training: 149 phrases, 10 epochs, 15 minutes on Ryzen 9 5900X
  - WER: 2.1% (98% accuracy)
- **LLM:** Qwen2.5-7B-Instruct-Q5_K_M (5.1GB)
  - Served via llama-server on localhost:8080
  - Auto-starting systemd service
- **Semantic Matching:** sentence-transformers/all-MiniLM-L6-v2
  - Cosine similarity with 0.70+ threshold
  - Typical match scores: 0.90-0.95

### Documentation
- Created comprehensive skill development guide
- Documented entire session with troubleshooting steps
- Added examples and best practices
- Testing checklist for new skills

### Known Issues
- None currently - system stable and performant

### Migration Notes
If updating from previous version:
1. Install llama-server systemd service
2. Enable fine-tuned Whisper in config.yaml
3. Restart JARVIS to load new filesystem skill
4. Verify llama-server is running: `systemctl --user status llama-server`

---

