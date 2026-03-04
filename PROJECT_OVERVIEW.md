# JARVIS - Personal AI Assistant

**Version:** 4.0.0 (Production Ready)
**Last Updated:** March 4, 2026
**Status:** ✅ Stable, Feature-Rich, Voice-Controlled

---

## 📋 Table of Contents
- [What is JARVIS?](#what-is-jarvis)
- [Current Capabilities](#current-capabilities)
- [System Architecture](#system-architecture)
- [Technology Stack](#technology-stack)
- [Progress Timeline](#progress-timeline)
- [Design Philosophy](#design-philosophy)
- [Roadmap](#roadmap)
- [Getting Started](#getting-started)

---

## 🤖 What is JARVIS?

JARVIS (Just A Rather Very Intelligent System) is a fully offline, voice-controlled AI assistant. Unlike commercial assistants, JARVIS runs entirely on your local hardware with:

- ✅ **Complete Privacy** - No cloud, no data collection
- ✅ **Custom Voice Training** - Learns YOUR accent
- ✅ **Modular Skills** - Easy to extend
- ✅ **Natural Conversation** - Semantic understanding
- ✅ **Production Ready** - Stable, tested, reliable

**Hardware:** Runs on consumer-grade PC (Ryzen 9 5900X, AMD RX 7900 XT compute + RX 7600 display)
**OS:** Ubuntu 24.04 LTS
**Latency:** 300-600ms for skill queries, 2-4s for LLM tool calling (streaming)

---

## 🎯 Current Capabilities

### Core Features
- **Wake Word Detection** - Porcupine "Jarvis" with 100% accuracy
- **Speech Recognition** - Fine-tuned Whisper v2 (CTranslate2, GPU-accelerated, 94%+ accuracy, 198 phrases, Southern accent)
- **Natural Language Understanding** - Semantic intent matching (sentence-transformers)
- **Conversational Flow Engine** - Persona module (24 response pools, ~90 templates), ConversationState (turn tracking), ConversationRouter (shared priority chain)
- **Text-to-Speech** - Kokoro 82M (primary, CPU, fable+george blend) + Piper ONNX fallback
- **LLM Intelligence** - Qwen3.5-35B-A3B (Q3_K_M, MoE, 3B active params) via llama.cpp + Claude API fallback with quality gating
- **LLM-Centric Tool Calling (Phases 1-2)** - Skills migrated to native Qwen3.5 tool calling. 8 tools (7 domain + web_search). Semantic pruning selects tools; LLM decides. Time/date handled by TimeInfoSkill (instant response). 100% accuracy across 1,200+ trials
- **Interaction Artifact Cache** - 5-phase hot/warm/cold tier system for caching interaction results, structured readback ("say that again"), delivery modes (brief/detailed/bullet/full), cross-session retrieval
- **Self-Managing Memory** - Per-turn fact extraction (MemGPT pattern), recall_memory tool for semantic search, CMA 6/6 (consolidation & abstraction + associative linking)
- **MCP Bridge** - Bidirectional Model Context Protocol: outbound server exposing 7 native tools + inbound client consuming external MCP servers as native tools
- **Web Research** - Qwen3.5 native tool calling + DuckDuckGo + trafilatura, multi-source synthesis
- **Event-Driven Pipeline** - Coordinator with STT/TTS workers, streaming LLM, contextual ack cache (10 tagged phrases)
- **Gapless TTS Streaming** - StreamingAudioPipeline with single persistent aplay, background Kokoro generation
- **Adaptive Conversation Windows** - 4-7s duration, extends with conversation depth, timeout cleanup, noise filtering, dismissal detection
- **Ambient Wake Word Filter** - Multi-signal: position, copula, threshold 0.80, length — blocks ambient mentions
- **Self-Awareness** - Capability manifest + system state injected into LLM context — JARVIS knows what it can do, its current state, and skill reliability
- **Task Planner** - Compound request detection (22 signals), LLM plan generation, sequential execution with per-step LLM evaluation, pause/resume/cancel/skip voice interrupts, predictive timing announcements, error-aware + context-budget-aware planning
- **LLM Metrics Dashboard** - Real-time tracking (latency, tokens, errors), web dashboard at `/metrics`, persistent SQLite, per-skill breakdowns
- **People Manager + Social Introductions** - "Meet my niece Arya" triggers multi-turn intro flow (name confirmation, pronunciation check, fact gathering). SQLite people database with TTS pronunciation overrides and LLM context injection for known contacts
- **Three Frontends** - Voice (production), console (debug/hybrid), web UI (browser-based chat with streaming + sessions)
- **Web UI** - aiohttp WebSocket server, streaming LLM, markdown rendering, session sidebar, health HUD, file handling

### Skills (11 Active — conversation disabled, handled natively by LLM)

#### 🌤️ Weather
- Current conditions, forecasts, rain probability
- *"Jarvis, what's the weather like?"*

#### ⏰ Time & Date
- Current time, date, day of week
- *"Jarvis, what time is it?"*

#### 💻 System Information
- CPU, memory, disk, uptime, network
- *"Jarvis, what's my CPU usage?"*

#### 🗂️ Filesystem
- File search, code line counting, script analysis
- *"Jarvis, how many lines of code in your codebase?"*

#### 📝 File Editor
- Write, edit, read, delete files + list share contents
- LLM-generated content, confirmation flow for destructive operations
- *"Jarvis, write a backup script"*
- *"Jarvis, delete temp.txt"*

#### 🛠️ Developer Tools
- 13 intents: codebase search, git multi-repo, system admin, general shell
- "Show me" visual output, 3-tier safety (allowlist → confirmation → blocked)
- *"Jarvis, show me the git status"*

#### 🌐 Web Navigation
- Playwright-based search, result selection, page navigation
- Scroll pagination (YouTube/Reddit), window management
- *"Jarvis, search for Python async tutorials"*

#### 📰 News
- 16 RSS feeds, urgency classification, semantic dedup
- Voice headline delivery, category filtering
- *"Jarvis, read me the tech headlines"*

#### 🔔 Reminders
- Priority tones, nag behavior, acknowledgment tracking
- Google Calendar two-way sync, dedicated JARVIS calendar
- Daily & weekly rundowns (state machine: offered → re-asked → deferred → retry)
- *"Jarvis, remind me to call the dentist at 3pm"*

#### 🖥️ Desktop Control (App Launcher)
- 16 intents: launch/close apps, fullscreen/minimize/maximize, volume up/down/mute, workspace switch/move, focus app, list windows, clipboard read/write
- GNOME Shell extension D-Bus bridge for Wayland-native window management
- *"Jarvis, open Chrome"*
- *"Jarvis, volume up"*
- *"Jarvis, switch to workspace 2"*

#### 💬 Conversation (DISABLED — LLM handles natively)
- Greetings, small talk, acknowledgments — handled directly by Qwen3.5
- *"Jarvis, how are you?"*

#### 🤝 Social Introductions
- Multi-turn butler-style introduction flow with name confirmation and pronunciation checks
- People database: relationship tracking, fact storage, TTS pronunciation overrides
- *"Jarvis, meet my niece Arya"*
- *"Jarvis, who is Arya?"*
- *"Jarvis, forget Arya"*

### Additional Systems
- **Conversational Memory** - SQLite fact store + FAISS semantic search, recall, batch LLM extraction, proactive surfacing, forget/transparency, self-managing per-turn extraction (MemGPT pattern)
- **CMA (Consolidation, Mapping, Abstraction)** - 6/6 phases: episode-to-semantic knowledge promotion + associative linking (graph edges between related artifacts)
- **Interaction Artifact Cache** - 5-phase hot/warm/cold tier system, structured readback, delivery modes, cross-session retrieval
- **Context Window** - Topic-segmented working memory, relevance-scored assembly, cross-session persistence, background Qwen summarization
- **MCP Bridge** - Bidirectional: outbound server (7 tools) + inbound client (external servers as native tools)
- **User Profiles** - Speaker identification (resemblyzer d-vectors), dynamic honorifics, voice enrollment, multi-user DB (created_by, origin_endpoint)
- **Google Calendar** - OAuth, event CRUD, incremental sync, background polling, multi-notification composite keys
- **Cross-Session Memory** - Last 32 messages loaded from persistent history
- **Health Check** - 5-layer system diagnostic (ANSI terminal report + voice summary)
- **Hardware Failure Handling** - Startup retry, device monitoring, degraded mode, graceful recovery
- **GNOME Desktop Bridge** - Custom GNOME Shell extension (D-Bus), Wayland-native window management, wmctrl fallback
- **GitHub Publishing** - Automated redaction pipeline, PII verification, non-interactive `--auto` publish
- **Automated Test Suite** - 270+ tests (112 unit + 130 routing + 28 LLM) across 9 phases

---

## 🏗️ System Architecture
```
┌─────────────────────────────────────────────────────────┐
│                       USER VOICE                         │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  AUDIO INPUT (FIFINE K669B USB Mic - Mono 48kHz)        │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  VAD (Voice Activity Detection) - WebRTC VAD             │
│  • Detects speech vs silence                            │
│  • Triggers transcription                               │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  STT (Speech-to-Text) - Custom Whisper Model v2          │
│  • Fine-tuned on user's Southern accent                 │
│  • 198 training phrases, 94%+ accuracy                  │
│  • GPU-accelerated: 0.1-0.2s transcription              │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  WAKE WORD DETECTION + AMBIENT FILTER                   │
│  • Checks for "Jarvis" in transcript                    │
│  • Fuzzy matching (threshold: 0.80)                     │
│  • Ambient filter: position, copula, length             │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  CONVERSATION ROUTER - 16-Layer Priority Chain          │
│  P0:      Wake-word-only utterance filter               │
│  P1:      Rundown acceptance/deferral                   │
│  P1.5:    Delivery mode commands                        │
│  P2:      Reminder acknowledgment                       │
│  P2.5:    Memory forget confirmation                    │
│  P2.6:    Social introductions (multi-turn)             │
│  P2.7:    Dismissal detection                           │
│  P2.8:    Bare acknowledgment filter                    │
│  P3:      Memory operations (forget/recall/transparency)│
│  P3.1:    Readback / "say that again"                   │
│  P3.5:    Research follow-up                            │
│  P3.7:    News article pull-up                          │
│  Pre-P4:  Task planner (compound detection)             │
│  P4-LLM:  ★ TOOL CALLING — 8 tools (PRIMARY PATH)      │
│  P4:      Skill routing (stateful skills)               │
│  P5:      News continuation                             │
│  Fallback: LLM streaming (Qwen3.5 → Claude API)        │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  SKILL EXECUTION                                         │
│  • Modular skill system                                 │
│  • Semantic intent handlers                             │
│  • Error handling & logging                             │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  LLM - Qwen3.5-35B-A3B via REST API + Claude fallback   │
│  • MoE: 256 experts, 8+1 active (~3B active params)    │
│  • Web research via native tool calling                 │
│  • Conversational responses + technical reasoning       │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│  TTS (Text-to-Speech) - Kokoro 82M + Piper fallback     │
│  • 50/50 fable+george voice blend                       │
│  • Natural intonation, streaming output                 │
│  • CPU-only, low latency                                │
└────────────────────┬────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────┐
│                    AUDIO OUTPUT                          │
└─────────────────────────────────────────────────────────┘
```

### Data Flow Example
```
User: "Jarvis, how many lines of code in your codebase?"
  ↓ (Audio captured)
VAD: Speech detected
  ↓ (Transcription triggered)
Whisper: "jarvis, how many lines of code in your codebase?"
  ↓ (Wake word check)
Wake Word: ✅ Detected "jarvis" (similarity: 1.00)
  ↓ (Strip wake word)
Intent Matching: "how many lines of code in your codebase"
  ↓ (Semantic match)
Semantic Matcher: 0.95 score → FilesystemSkill.count_code_lines
  ↓ (Execute handler)
Filesystem Skill: Count Python files, exclude venv
  ↓ (Return response)
Response: "My codebase contains 320,388 lines of Python code across 40 files, sir."
  ↓ (TTS)
Kokoro: Generates audio
  ↓ (Playback)
User: Hears response
```

---

## 🛠️ Technology Stack

### Core Components
| Component | Technology | Purpose |
|-----------|-----------|---------|
| **STT** | faster-whisper (CTranslate2, fine-tuned) | Speech recognition |
| **TTS** | Kokoro 82M (primary) + Piper (fallback) | Speech synthesis |
| **LLM** | Qwen3.5-35B-A3B (Q3_K_M via llama.cpp) + Claude API | Language understanding + web research |
| **VAD** | WebRTC VAD | Voice activity detection |
| **Wake Word** | Porcupine | Trigger detection |
| **Embeddings** | sentence-transformers | Intent matching |

### Infrastructure
- **OS:** Ubuntu 24.04 LTS
- **Python:** 3.12
- **Service Manager:** systemd (user services)
- **LLM Server:** llama-server (REST API)
- **Storage:**
  - Code: `~/jarvis/`
  - Skills: `/mnt/storage/jarvis/skills/`
  - Models: `/mnt/models/` (4TB dedicated drive)
  - Public repo: `~/jarvis-public/` → `github.com/InterGenJLU/jarvis`

### Key Libraries
- `torch` (ROCm) - PyTorch for ML models (CPU-only for TTS)
- `ctranslate2` - GPU-accelerated Whisper inference
- `sentence-transformers` - Semantic intent matching
- `kokoro` - Primary TTS engine (82M model)
- `sounddevice` - Audio I/O
- `playwright` - Headless web navigation
- `faiss-cpu` - Vector search for conversational memory
- `numpy` - Array operations
- `requests` - HTTP client for LLM API
- `pyyaml` - Configuration

---

## 📈 Progress Timeline

### Phase 1: Foundation (Days 1-3)
- ✅ Basic voice loop (wake word → command → response)
- ✅ Whisper integration (base model)
- ✅ Piper TTS setup
- ✅ Simple command patterns

### Phase 2: Skills System (Days 4-7)
- ✅ Modular skill architecture
- ✅ Weather skill (OpenWeatherMap API)
- ✅ Time/date skill
- ✅ System info skill
- ✅ Conversation skill

### Phase 3: Intelligence (Days 8-10)
- ✅ Semantic intent matching (90% pattern reduction)
- ✅ LLM integration (Mistral 7B → Qwen 2.5-7B → Qwen 3-8B → Qwen3-VL-8B → Qwen3.5-35B-A3B)
- ✅ Conversation context window
- ✅ Intent confidence scoring

### Phase 4: Production Ready (Feb 9-10)
- ✅ Git version control (3 repositories)
- ✅ Automated backups (daily, systemd)
- ✅ 4TB model storage setup
- ✅ Paul Bettany voice cloning (proof-of-concept)
- ✅ Comprehensive documentation

### Phase 5: Major Upgrades (Feb 11) 🚀
- ✅ **Qwen 3-8B LLM** (better reasoning)
- ✅ **Custom Whisper training** (88%+ accuracy)
- ✅ **Filesystem skill** (semantic file operations)
- ✅ **Audio optimization** (no overflow)
- ✅ **Skill development guide** (comprehensive docs)

### Phase 6: GPU + CTranslate2 (Feb 12-13) 🚀
- ✅ **GPU-Accelerated STT** — CTranslate2 with ROCm on RX 7900 XT (0.1-0.2s)
- ✅ **PyTorch + CTranslate2 coexistence** — torch 2.10.0+rocm7.1 + CT2 4.7.1
- ✅ **Three-repo architecture** — code, skills, models on separate drives

### Phase 7: Feature Explosion (Feb 14-17) 🚀
- ✅ **12 critical bug fixes** — Whisper pre-buffer, semantic routing, keyword greediness, VAD overlap, etc.
- ✅ **News headlines** — 16 RSS feeds, urgency classification, semantic dedup, voice delivery
- ✅ **Reminder system** — priority tones, nag behavior, ack tracking, Google Calendar 2-way sync
- ✅ **Web Navigation Phase 2** — result selection, page nav, scroll pagination, window management
- ✅ **Developer tools skill** — 13 intents, codebase search, git multi-repo, system admin, safety tiers
- ✅ **Console mode** — text/hybrid/speech modes with stats panel
- ✅ **FIFINE K669B mic upgrade** — udev rule, config updated

### Phase 8: Polish + Advanced Systems (Feb 15-17) 🚀
- ✅ **Kokoro TTS** — 82M model, 50/50 fable+george blend, Piper fallback
- ✅ **Latency refactor (4 phases)** — streaming TTS, ack cache, streaming LLM, event pipeline
- ✅ **User profile system (5 phases)** — honorific, ProfileManager, SpeakerIdentifier, pipeline, enrollment
- ✅ **Honorific refactoring** — ~470 "sir" instances → dynamic `{honorific}` across 19 files
- ✅ **Conversational memory (6 phases)** — SQLite facts, FAISS indexing, recall, batch extraction, proactive surfacing, forget/transparency
- ✅ **Context window (4 phases)** — topic-segmented working memory, relevance-scored assembly, cross-session persistence
- ✅ **System health check** — 5-layer diagnostic, ANSI terminal + voice summary
- ✅ **Gapless TTS streaming** — StreamingAudioPipeline, single persistent aplay, zero-gap playback
- ✅ **Hardware failure graceful degradation** — startup retry, device monitor, degraded mode

### Phase 9: Web Research + Hardening (Feb 17-18) 🚀
- ✅ **Web research (5 phases)** — Qwen native tool calling + DuckDuckGo + trafilatura, multi-source synthesis
- ✅ **Prescriptive prompt design** — explicit rules for Qwen tool-use decisions, 150/150 correct test decisions
- ✅ **Streaming delivery fixes** — sentence-only chunking, per-chunk metric stripping, context flush on shutdown
- ✅ **27 bug fixes** — ack collision, keyword greediness, dismissal detection, decimal TTS, aplay lazy open, chunker decimal split, and more
- ✅ **Scoped TTS subprocess control** — replaced global `pkill -9` with tracked subprocess kill
- ✅ **GitHub publishing system** — automated redaction, PII verification, public repo sync

### Phase 10: Desktop Integration + Tooling (Feb 19-20) 🚀
- ✅ **GNOME Desktop Integration (5 phases)** — Custom GNOME Shell extension with D-Bus bridge, 14 D-Bus methods
- ✅ **Desktop Manager** — Singleton module with lazy D-Bus, wmctrl fallback, pactl, notify-send, wl-clipboard
- ✅ **App Launcher Skill v2.0** — 16 intents: launch/close, fullscreen/minimize/maximize, volume, workspace, focus, clipboard
- ✅ **Desktop notifications** — Wired into reminder system via notify-send
- ✅ **Publish script non-interactive mode** — `--auto` flag for CI-friendly publish (auto-generate commit msg + push)

### Phase 11: Web Chat UI (Feb 20) 🚀
- ✅ **5-phase implementation** — aiohttp WebSocket server, vanilla HTML/CSS/JS, zero new dependencies
- ✅ **Streaming LLM** — Token-by-token delivery with quality gate (buffers first sentence, retries if gibberish)
- ✅ **File handling** — Drag/drop, /file, /clipboard, /append, /context slash commands
- ✅ **History + notifications** — Paginated `/api/history`, scroll-to-load-more, floating announcement banners
- ✅ **Polish** — Markdown rendering with XSS protection, code blocks + copy, responsive breakpoints
- ✅ **Session sidebar** — 30-min gap detection, hamburger toggle, session rename, pagination, LIVE badge

### Phase 12: File Editor + Edge Case Testing (Feb 20) 🚀
- ✅ **File Editor Skill** — 5 intents (write, edit, read, delete, list share), confirmation flow, LLM-generated content
- ✅ **Ambient Wake Word Filter** — Multi-signal: position, copula, threshold 0.80, length — blocks ambient mentions
- ✅ **Edge Case Testing Phase 1** — ~200 test cases across 9 phases, 37/40 pass (92.5%), 14 routing failures fixed

### Phase 13: Conversational Flow Refactor (Feb 21) 🚀
- ✅ **Phase 1: Persona** — 10 response pools (~50 templates), system prompts, honorific injection
- ✅ **Phase 2: ConversationState** — Turn counting, intent history, question detection, research context
- ✅ **Phase 3: ConversationRouter** — Shared priority chain for voice/console/web (one router, three frontends)
- ✅ **Phase 4: Response Flow Polish** — Contextual ack selection (10 tagged phrases), smarter follow-up windows, timeout cleanup, suppress LLM opener collision
- ✅ **38 router tests** — `scripts/test_router.py` validates routing decisions without live LLM/mic

### Phase 14: Whisper v2 Fine-Tuning (Feb 21) 🚀
- ✅ **198 training phrases** (up from 149), FIFINE K669B USB condenser mic
- ✅ **GPU fp16 training** — 89 seconds on RX 7900 XT
- ✅ **94.4% live accuracy** — wake word 100%, contraction handling 100%

### Phase 15: Document Generation + Demo Prep (Feb 22) 🚀
- ✅ **Document Generation** — PPTX/DOCX/PDF via two-stage LLM pipeline, web research integration, Pexels images
- ✅ **Smart Ack Suppression** — Skip acknowledgments for fast/conversational queries
- ✅ **Doc gen prompt overhaul** — Prescriptive depth rules for Qwen
- ✅ **7 live testing bugs fixed** — publish.sh README protection
- ✅ **Edge case tests expanded** — 144 tests (Phase 1E)

### Phase 16: LLM Metrics Dashboard + Bug Fixes (Feb 23) 🚀
- ✅ **LLM Metrics Dashboard (5 phases)** — Real-time tracking, web dashboard at `/metrics`, persistent SQLite, per-skill breakdowns
- ✅ **jarvis-web.service** — Systemd user service for web UI, auto-start after jarvis.service
- ✅ **4 bug fixes** — Web research timeout, desktop manager init order, health check PipeWire, audio PipeWire routing
- ✅ **Preferred-mic hot-swap recovery** — Device monitor recovers from wrong-mic fallback after boot race
- ✅ **Edge case tests expanded** — 152 tests (150/152 pass, 98.7%)

### Phase 17: Qwen3.5-35B-A3B Model Upgrade (Feb 24) 🚀
- ✅ **Qwen3.5-35B-A3B** — MoE (256 experts, 8+1 active, ~3B active params), Q3_K_M quantization, 48-63 tok/s, IFEval 91.9
- ✅ **llama.cpp rebuilt** — ROCm build 8146, `GGML_HIP=ON`, 19.5/20.5 GB VRAM
- ✅ **3 voice test fixes** — Web search routing (removed stale keyword alias), ack bleed (0.35s settling delay), Whisper "quinn"→"qwen"
- ✅ **Full doc overhaul** — 12 files updated to reflect Qwen3.5-35B-A3B across all references
- ✅ **Tier 4 LLM tests** — 28 tests validating model response quality, tool calling, safety, hallucination resistance

### Phase 18: Self-Awareness + Task Planner (Feb 24-25) 🚀
- ✅ **Self-Awareness Layer** — Capability manifest + system state injected into LLM context. JARVIS knows its own skills, latency, error rates, memory usage, and uptime
- ✅ **Task Planner Phase 2** — Compound request detection (22 conjunctive signals), LLM plan generation (JSON steps, max 4), sequential execution via skill_manager
- ✅ **Task Planner Phase 3** — Guardrails: destructive operation confirmation, failure-breaks, voice interrupts (cancel/skip via event queue)
- ✅ **Task Planner Phase 4** — Predictive timing announcements, error-aware planning (unreliable skill warnings), context-budget-aware planning (>80% warning), LLM per-step evaluation (continue/adjust/stop), pause/resume with 120s timeout
- ✅ **5 bug fixes** — Pause/resume guards (voice-only), eval timeout (10s), dead "skip that" phrase removed, 12 new tests

### Phase 19: Social Introductions + People Manager (Feb 25) 🚀
- ✅ **PeopleManager** — `core/people_manager.py`: SQLite-backed people + facts database, TTS pronunciation overrides via normalizer, LLM context injection for known contacts
- ✅ **Social Introductions Skill** — 5 semantic intents (meet, who-is, recall, forget, update), multi-turn state machine (name confirm → pronunciation check → fact gathering → complete)
- ✅ **Persona expansion** — 7 new response pools (~25 templates) for introduction flows
- ✅ **Router P2.6** — Introduction state machine intercept in ConversationRouter priority chain
- ✅ **Edge case tests expanded** — 270 tests (112 unit + 130 routing + 28 LLM), 100% pass rate

### Phase 20: LLM-Centric Tool Calling — Phase 1 (Feb 26) 🚀
- ✅ **Native tool calling** — 3 skills (time_info, system_info, filesystem) migrated from hard-coded routing to Qwen3.5 native tool calling
- ✅ **Tool executor** — `core/tool_executor.py`: Pure data dispatch, no TTS/skill dependencies
- ✅ **P4-LLM routing** — Semantic pruning selects relevant tools; LLM decides which to call. Non-migrated skill guard prevents over-capture
- ✅ **All 3 frontends updated** — Voice pipeline, console, web UI all handle tool-calling path
- ✅ **100% accuracy** — 600/600 trials across 60 queries × 10 runs, ~822ms average latency
- ✅ **Test harness** — `scripts/test_tool_calling.py`: 60 queries, 7-category taxonomy, `--sweep` for sampling matrix
- ✅ **266/266 existing tests pass** — Zero regressions from migration

### Phase 21: LLM-Centric Tool Calling — Phase 2 + Tool-Connector (Feb 27) 🚀
- ✅ **Phase 2 complete** — weather, reminders, conversation (disabled), developer_tools, news migrated to tools
- ✅ **7 tools total** (6 domain + web_search), 1,200+ trials, 99.6% accuracy overall
- ✅ **Tool-connector plugin system** — one-file tool definitions in `core/tools/`, auto-discovery registry
- ✅ **5-6 tool cliff DEBUNKED** — tested up to 8 tools, zero XML fallback
- ✅ **Time injection** — all 5 LLM prompt injection points, correct time-of-day greetings

### Phase 22: Dual GPU + 32K Context (Feb 28) 🚀
- ✅ **RX 7600 display offload** — GNOME compositor on RX 7600, RX 7900 XT dedicated compute
- ✅ **ctx-size 7168→32768** — 4.6x context expansion, SSM hybrid verified 9/9 at 25K tokens, peak VRAM 95.6%
- ✅ **Context enrichment** — user profile + memory injection into LLM prompts
- ✅ **Doc gen fix** — structured output formatting

### Phase 23: Unified Awareness + Calendar Fixes (Mar 1) 🚀
- ✅ **Unified awareness layer** — capability manifest + system state injected into LLM context
- ✅ **Phase 4 routing evaluation RESOLVED** — hybrid architecture retained, skills and tools coexist by design
- ✅ **Calendar multi-notification support** — composite keys, per-offset dedup
- ✅ **2 bug fixes** — calendar notification loop, volume slider

### Phase 24: Multi-User + Memory Dashboard (Mar 2) 🚀
- ✅ **Active user selection (#63)** — console `--user` flag + web UI `<select>` + WebSocket `set_user`
- ✅ **Multi-user DB migration** — `created_by`, `origin_endpoint` columns, 780 rows corrected
- ✅ **Memory dashboard** — web page with facts/interactions explorer
- ✅ **Formal address system** — secondary user honorifics ("ma'am" / "Ms. Guest")
- ✅ **Readback flow** — structured readback for skill responses
- ✅ **Rundown bug fixes** — event time dedup, weekly re-offer, missed events
- ✅ **Reminder staleness guard** — auto-cancel reminders >24h overdue, 5 additional bug fixes

### Phase 25: Artifact Cache + Readback + Delivery Modes (Mar 3) 🚀
- ✅ **Interaction artifact cache** — 5 phases: hot/warm/cold tiers, cross-session retrieval via embeddings
- ✅ **Structured readback** — "say that again" replays cached artifact
- ✅ **Delivery modes** — brief/detailed/bullet/full output styles
- ✅ **Tool artifact wiring** — all 7 tools centralized in pipeline.py + interaction_cache.py
- ✅ **Kokoro G2P overrides** — pronunciation corrections for proper nouns

### Phase 26: CMA 6/6 + Self-Managing Memory (Mar 3) 🚀
- ✅ **CMA 6/6** — Consolidation & Abstraction (episode-to-semantic promotion) + Associative Linking (graph edges)
- ✅ **Self-managing memory** — per-turn extraction + recall_memory tool (MemGPT pattern)
- ✅ **recall_memory** — 8th LLM tool, semantic memory search

### Phase 27: MCP Bridge (Mar 3) 🚀
- ✅ **MCP Bridge Phase 1** — outbound MCP server exposing 7 native tools to external clients
- ✅ **MCP Bridge Phase 2** — inbound MCP client consuming external servers as native tools

---

## 🎨 Design Philosophy

### 1. Privacy First
All processing happens locally. No data leaves your machine. No telemetry, no cloud dependencies.

### 2. Modular & Extensible
Skills are independent modules. Add new capabilities without touching core code.

### 3. Natural Interaction
Semantic matching allows flexible phrasing. Say it naturally, JARVIS understands.

### 4. Production Quality
- Comprehensive error handling
- Extensive logging
- Graceful degradation
- Auto-recovery mechanisms

### 5. Hardware Efficient
Optimized for consumer hardware. No expensive GPUs required (though AMD GPU supported).

### 6. Maintainable
- Clean code structure
- Comprehensive documentation
- Version controlled
- Automated backups

---

## 🗺️ Roadmap

### Recently Completed
- MCP Bridge — bidirectional, outbound server + inbound client (Mar 3)
- Self-managing memory + CMA 6/6 + recall_memory tool (Mar 3)
- Interaction artifact cache — 5 phases, readback, delivery modes (Mar 3)
- Multi-user DB + active user selection + memory dashboard (Mar 2)
- Unified awareness layer + calendar fixes (Mar 1)
- Dual GPU display offload + ctx-size 32768 (Feb 28)
- LLM-centric tool calling — Phases 1-2 + tool-connector (Feb 26-27)
- Social introductions + People Manager (Feb 25)
- Self-awareness + task planner — 4 phases (Feb 24-25)
- Qwen3.5-35B-A3B model upgrade — MoE, Q3_K_M (Feb 24)

### Up Next (Owner-Directed Sequence)
- [ ] Profile-aware skill routing (#12) — skills ignore current_user
- [ ] Mobile iOS app (#60) — plan exists (6 phases)
- [ ] CalDAV calendar (secondary user) — blocked on credentials
- [ ] Dual-model STT (#46) — secondary user voice
- [ ] "Onscreen please" (#11) — retroactive visual display
- [ ] IMAP email via MCP

### Medium Term
- [ ] Vision Phase 3 — mmproj activation, image input wiring
- [ ] LLM news classification (#17)
- [ ] Reminder snooze in P2 chain (#44)
- [ ] Vision/OCR skill (Phase 1 Tesseract)

### Long Term
- [ ] Concurrent multi-user support (#61)
- [ ] Mobile iOS app deployment
- [ ] Threat hunting / malware analysis framework
- [ ] Home automation
- [ ] Emotional context awareness

---

## 🚀 Getting Started

### Prerequisites
- Ubuntu 24.04 LTS (or similar Linux)
- Python 3.11+
- 16GB+ RAM recommended
- GPU optional (AMD/NVIDIA for acceleration)

### Installation
```bash
# Clone repository
git clone <repo-url> ~/jarvis
cd ~/jarvis

# Install dependencies
pip install -r requirements.txt --break-system-packages

# Set up models directory
sudo mkdir -p /mnt/models
sudo chown $USER:$USER /mnt/models

# Download models (automated script coming soon)
# For now, manually place models in /mnt/models/

# Configure
cp config.yaml.example config.yaml
# Edit config.yaml with your settings

# Install services
cp jarvis.service ~/.config/systemd/user/
cp llama-server.service /etc/systemd/system/
systemctl --user daemon-reload
sudo systemctl daemon-reload

# Enable and start
systemctl --user enable --now jarvis
sudo systemctl enable --now llama-server

# Check status
systemctl --user status jarvis
```

### Quick Start
```bash
# Start JARVIS
startjarvis

# Stop JARVIS
stopjarvis

# Restart JARVIS
restartjarvis

# View logs
journalctl --user -u jarvis -f
```

### Basic Usage (Voice)
1. Say "Jarvis" to wake
2. Ask your question naturally
3. JARVIS responds
4. 4-7s adaptive window for follow-up (extends with conversation depth)

### Console Mode
```bash
python3 jarvis_console.py              # Text mode (type commands)
python3 jarvis_console.py --hybrid     # Text input + spoken output
```
Stats panel shows match layer, skill, confidence, timing, and LLM token counts after each command.

**Example Interactions:**
```
You: "Jarvis, what's the weather?"
JARVIS: "Currently 45 degrees and partly cloudy, sir."

You: "How about tomorrow?"
JARVIS: "Tomorrow's forecast shows 52 degrees with scattered showers, sir."

You: "How many lines of code in your codebase?"
JARVIS: "My codebase contains 320,388 lines of Python code across 40 files, sir."
```

---

## 📚 Documentation

- **[SKILL_DEVELOPMENT.md](docs/SKILL_DEVELOPMENT.md)** - How to create skills
- **[DEVELOPMENT.md](docs/DEVELOPMENT.md)** - Development workflows
- **[CHANGELOG.md](CHANGELOG.md)** - Version history
- **[TODO_NEXT_SESSION.md](docs/TODO_NEXT_SESSION.md)** - Current priorities

---

## 🤝 Contributing

JARVIS is a personal project, but ideas and improvements are welcome!

### Adding Skills
1. Read [SKILL_DEVELOPMENT.md](docs/SKILL_DEVELOPMENT.md)
2. Create skill in `skills/` directory
3. Test thoroughly
4. Document in skill README

### Reporting Issues
Include:
- What you said
- What JARVIS responded
- Expected behavior
- Relevant logs

---

## 📊 Performance Metrics

### Accuracy
- Wake word detection: 100% (Porcupine)
- Speech recognition: 94%+ (fine-tuned Whisper v2, 198 phrases, Southern accent)
- Intent matching: 95%+ (semantic embeddings)
- Routing tests: 38/38 pass (`scripts/test_router.py`)
- Edge case testing: 100% (270+ across 112 unit + 130 routing + 28 LLM tests)

### Latency
- Wake word detection: <100ms
- Speech transcription: 0.1-0.2s (GPU-accelerated CTranslate2)
- Intent matching: <100ms (pre-computed semantic embedding cache)
- Skill-handled queries: 300-600ms total
- LLM fallback: 2-4s total (streaming)
- TTS generation: <1s (Kokoro streaming)

### Resource Usage
- RAM: ~4GB (with all models loaded)
- CPU: 10-30% during processing
- GPU: RX 7900 XT via ROCm (STT + LLM, ~19.1/20.0 GB VRAM peak) + RX 7600 (display)
- Disk: ~25GB (models + code)

---

## 🎓 What I've Learned

### Technical Insights
1. **Custom training beats generic models** - 94%+ vs 50% accuracy (198 phrases, 2 training rounds)
2. **REST APIs > subprocess calls** - More reliable for LLM
3. **Semantic matching is powerful** - Reduces pattern count 90%
4. **Preload heavy models** - Prevents audio thread blocking
5. **Log everything** - Makes debugging 10x easier
6. **One router, three frontends** - ConversationRouter eliminates routing duplication across voice/console/web
7. **Prescriptive > permissive for small LLMs** - Explicit numbered rules followed more reliably than prose instructions
8. **Substring `in` for keyword matching is a trap** - `"no" in "diagnostic"` is True. Always use word-boundary matching

### Development Practices
1. **Iterate quickly** - Small changes, frequent testing
2. **Test with real voice** - Keyboard input hides issues
3. **Monitor audio pipeline** - Overflow warnings are critical
4. **Version control everything** - Git saved me multiple times
5. **Document as you go** - Future you will thank you

### Design Decisions
1. **Offline first** - Privacy and reliability
2. **Modular skills** - Easy to extend and maintain
3. **Semantic intents** - Natural language flexibility
4. **British voice** - Character and professionalism
5. **Conservative responses** - Concise, helpful, polite

---

## 🏆 Achievements

- ✅ Fully functional voice assistant with gapless streaming TTS
- ✅ Custom accent training (fine-tuned Whisper v2, Southern accent, 94%+, 198 phrases)
- ✅ Production-ready event-driven architecture
- ✅ Web research via local LLM tool calling (no cloud required)
- ✅ Conversational memory with semantic recall across sessions
- ✅ Speaker identification and dynamic user profiles
- ✅ 11 modular skills + 8 LLM tools with semantic intent matching (16-intent desktop control + social introductions)
- ✅ 16-layer priority chain with LLM tool calling as primary path (P4-LLM)
- ✅ Interaction artifact cache with structured readback and delivery modes
- ✅ Self-managing memory (MemGPT pattern) + CMA 6/6 (consolidation & associative linking)
- ✅ Bidirectional MCP bridge (outbound server + inbound client)
- ✅ Self-awareness layer with capability manifest + system state for LLM context
- ✅ Task planner with compound detection, LLM planning, per-step evaluation, pause/resume
- ✅ Conversational flow engine with persona, state tracking, and shared router
- ✅ Three frontends: voice, console, web UI (all sharing one router)
- ✅ Web Chat UI with streaming, markdown, session sidebar, and health HUD
- ✅ Ambient wake word filter (multi-signal, blocks false triggers)
- ✅ 38 router tests + 270+ edge case tests (100%) including 28 live LLM validation tests
- ✅ Dual GPU setup — RX 7900 XT compute + RX 7600 display, ctx-size 32768
- ✅ Hardware failure graceful degradation
- ✅ Sub-600ms skill responses (300-600ms)
- ✅ Open source on GitHub with automated PII redaction

**JARVIS is a legitimate, production-ready AI assistant!**

---

## 📞 Support

For issues or questions:
1. Check logs: `journalctl --user -u jarvis -n 100`
2. Review documentation
3. Test with simple commands first
4. Verify all services running

**Common Issues:**
- **No audio input:** Check microphone permissions
- **No TTS output:** Verify Kokoro/Piper installation
- **Intent not matching:** Lower threshold or add examples
- **LLM not responding:** Check llama-server status

---

**Built with ❤️ and lots of coffee ☕**

*Built with care, tested obsessively, improved daily.*
