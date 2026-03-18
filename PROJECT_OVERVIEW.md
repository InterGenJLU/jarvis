# JARVIS — Project Overview

**Version:** 5.0.0
**Last Updated:** March 11, 2026
**Status:** Production — running 24/7 as a systemd service

---

## What Is This?

JARVIS is a voice assistant that runs entirely on local hardware. No cloud APIs in the critical path. Speech recognition, language understanding, tool calling, vision, memory, and text-to-speech all run on a single desktop with an AMD GPU.

It started as a wake-word-to-response loop in February 2026. Five weeks and ~66,000 lines of Python later, it has an 18-layer conversation router, 11 LLM tools with 100% calling accuracy, a self-managing memory system, computer vision through desktop and mobile cameras, a task planner that decomposes compound requests, and three frontends (voice, console, web) all sharing the same routing engine.

**Hardware:** Ryzen 9 5900X, 64GB RAM, RX 7900 XT (20GB, compute) + RX 7600 (display), FIFINE K669B mic
**Stack:** Python 3.12, ROCm 7.2, llama.cpp, CTranslate2, Kokoro TTS, faster-whisper, FAISS, aiohttp

---

## What Makes It Interesting

### 100% Tool Calling Accuracy on a Local 3B-Active MoE

Qwen3.5-35B-A3B is a mixture-of-experts model: 35B total parameters, but only ~3B active per token. At Q3_K_M quantization it fits in ~19.5GB VRAM with 32K context. JARVIS gives it 11 tools via OpenAI-compatible function calling through llama.cpp, and across 1,200+ test trials it has never called the wrong tool or hallucinated a tool call.

The key is a **semantic pruning layer** that runs before the LLM sees the tools. A sentence-transformer model (nomic-embed-text-v1.5, 768-dim on RX 7600 GPU — evolved from all-MiniLM-L6-v2) scores all 11 tools against the query by embedding similarity and only passes the top 4 to the LLM. This keeps the tool schema small enough that a 3B-active model handles it reliably. The pruner also scores always-included tools (web_search, recall_memory, screenshots, webcam, face enrollment) via `INTENT_EXAMPLES` on each tool module — without this, the pruner would defer them to the skill layer and they'd never reach the LLM.

### 18-Layer Conversation Router, One Router for Three Frontends

Every request — voice, console keyboard, or browser WebSocket — enters the same `ConversationRouter.route()` method. The 18-layer priority chain evaluates top-to-bottom:

- **P0-P2.8** — Deterministic fast paths: delivery mode commands, rundown acceptance, task planner interrupts (cancel/skip/pause/resume), reminder acknowledgments, memory forget confirmations, multi-turn social introduction state machine, dismissal detection ("that's all"), bare acknowledgment filtering ("yeah", "ok"). Zero LLM calls, sub-10ms.
- **P3-P3.7** — Memory and artifact resolution: recall/forget/transparency, structured readback with section navigation (next/previous/section N), artifact reference resolution ("the second result", "that recipe", "repeat that"), news article pull-up.
- **Pre-P4** — Compound request detection via 22 conjunctive regex patterns ("and then", "after that"). Triggers the task planner: LLM generates a JSON plan (max 4 steps), executes sequentially with per-step LLM evaluation, voice interrupts between steps.
- **P4-LLM** — The primary path. Semantic pruner selects top 4 tools, LLM calls one (or none), 17-domain classifier selects a specialized synthesis prompt, LLM streams the answer.
- **P4-Skill** — Stateful skills (app_launcher, file_editor, social_introductions) via 5-layer semantic intent matching.
- **Fallback** — Pure LLM conversation with quality gating and Claude API fallback.

Building it once and sharing it across three frontends eliminated an entire class of "works in console but not voice" bugs.

### Self-Managing Memory (MemGPT Pattern + CMA 6/6)

After every exchange, the LLM extracts durable facts and stores them in SQLite. The `recall_memory` tool (always available) searches stored facts via text matching and FAISS semantic similarity. The system hit CMA 6/6 (Consolidation, Mapping, Abstraction) — all six requirements of the original MemGPT evaluation framework:

1. **Importance scoring** — facts ranked by salience
2. **Retrieval-driven mutation** — facts update when contradicted
3. **Associative linking** — graph edges between related facts
4. **Consolidation** — episode-to-semantic knowledge promotion
5. **Abstraction** — pattern recognition across episodes
6. **Proactive surfacing** — relevant facts injected into LLM context before the user asks

The LLM doesn't just answer questions about memories — it proactively surfaces them. Ask about weekend plans and it'll mention your stored preference for a particular BBQ restaurant.

### Domain-Aware Anti-Hallucination

When the LLM calls a tool, the response doesn't just get dumped into a generic "summarize this" prompt. A 17-domain regex classifier categorizes the query (math, veterinary, medical, nutrition, finance, legal, gaming, sports, automotive, real estate, programming, science/tech, history, travel, factual, geo), and one of 14 domain-specific synthesis prompts is injected. Medical queries get disclaimer language. Programming responses specify language versions. Legal responses cite jurisdiction limitations. Sports queries avoid speculation on games not yet played.

This turns out to matter more than expected. Without it, a local model confidently tells you the wrong drug interaction or invents a Supreme Court ruling.

### Computer Vision — Desktop, Mobile, and Presence

JARVIS sees through three paths:

- **Desktop webcam** — ffmpeg MJPEG singleton (v4l2, 1280x720, 15fps) with auto-start/stop and 30s idle shutdown. "What do you see?" captures a frame, downscales via PIL, and sends it to Qwen3.5's multimodal pipeline (mmproj-F16.gguf on CPU, 90s timeout).
- **Mobile camera relay** — When accessed from a phone, the server sends a `frame_request` over WebSocket, the browser captures from `getUserMedia`, returns a base64 JPEG `frame_response`. Same LLM pipeline, but the camera is in your pocket.
- **`take_screenshot`** — Captures the desktop via gnome-screenshot with optional window cropping. "What's on my screen?" works.
- **Face enrollment** — `enroll_face` tool stores face embeddings for presence-based greetings. Walk into the room and JARVIS greets you by name.

All vision runs on the local Qwen3.5 multimodal model. No cloud vision APIs.

### 5-Phase Interaction Artifact Cache

Every tool result is stored as a typed artifact in a hot/warm/cold tiered cache:

1. **Typed storage** — weather, search, reminder, news, system, file, dev_tools, memory artifacts
2. **Reference resolution** — "the second result", "that recipe", "repeat that" resolve to cached artifacts
3. **Sub-item navigation** — on-demand LLM decomposition of complex results
4. **Memory promotion** — session-end summarization pushes artifacts to long-term memory
5. **Cross-session retrieval** — FAISS semantic search across cold-tier artifacts

This means JARVIS remembers what it told you, can refer back to specific results, and can retrieve information from previous sessions without being explicitly asked.

### Streaming Everything

The LLM streams tokens. As they arrive, a sentence chunker buffers until it hits sentence-ending punctuation, then hands complete sentences to Kokoro TTS. Kokoro generates audio in a background thread. A single persistent `aplay` process plays audio segments with zero gaps between sentences. The user hears the first sentence while the LLM is still generating the third.

The contextual acknowledgment cache (10 pre-synthesized phrases tagged as neutral/checking/working/research) plays an appropriate "working on it" phrase if the LLM hasn't responded within 300ms, so there's never dead air.

### Persona Engine — Not Just Templates

38 response pool categories with ~184 templates. But it's more than random selection:

- **Style-tagged acks** — "checking" acks for factual queries, "research" acks for web searches, "working" acks for tool calls
- **Dynamic honorifics** — "sir" for the primary user, "ma'am" and "Ms. Guest" (formal on greetings/farewells) for the secondary user, "friend" for unknown speakers
- **Guest mode** — Unknown speakers get a security boundary: HAL 9000 easter egg greetings, only `get_weather` and `web_search` tools, no memory access, no personal tools
- **Domain disclaimers** — Dry-humor disclaimers for medical and legal queries
- **Multi-speaker tracking** — Per-speaker history labels, rapid-switch detection (3 switches in 60s triggers a butler-style retort)

### One-File Tool Plugin System

Adding a new tool to JARVIS:

```python
# core/tools/your_tool.py
TOOL_NAME = "your_tool"
SKILL_NAME = "your_skill"       # or None
SCHEMA = { ... }                 # OpenAI function schema
SYSTEM_PROMPT_RULE = "..."       # When the LLM should use this tool
def handler(args):
    return "result"
```

Drop it in `core/tools/`. The registry auto-discovers it at startup, builds the schema, injects runtime dependencies, and makes it available to the LLM. No imports to update, no wiring changes, no registry edits.

---

## The Numbers

| Metric | Value | How Measured |
|--------|-------|-------------|
| Tool calling accuracy | 100% | 1,200+ trials, 10-category taxonomy, `--sweep` mode |
| Unit tests | 314/314 | 4 tiers: edge cases, routing, tool calling, LLM quality |
| Conversation tests | 62 conversations | End-to-end behavioral suite via WebSocket |
| STT accuracy | 94%+ | Fine-tuned Whisper v2, 198 phrases, Southern accent |
| STT latency | 0.1-0.2s | CTranslate2 on RX 7900 XT via ROCm |
| LLM throughput | 48-63 tok/s | Qwen3.5-35B-A3B Q3_K_M, full GPU offload |
| End-to-end latency | 2-4s | Wake word to first spoken word (streaming) |
| VRAM usage | ~19.5 / 20 GB | 32K context, all layers offloaded |
| TTS normalization | 22 passes | Markdown, heteronyms, IPs, ports, currencies, measurements, etc. |
| Response pools | 38 categories, ~184 templates | Style-tagged, honorific-injected |
| Domain classifiers | 17 domains | Regex-based, feeds 14 synthesis prompt blocks |
| Codebase | ~66,000 lines Python | ~40 modules + 11 skills + test suites |

---

## Architecture at a Glance

```
Voice/Console/Web
        │
        ▼
ConversationRouter ─── 18-layer priority chain
        │
        ├── P0-P2.8: Deterministic fast paths (sub-10ms)
        ├── P3-P3.7: Memory, artifacts, readback
        ├── Pre-P4:  Task planner (compound requests)
        ├── P4-LLM:  Semantic pruner → Qwen3.5 tool calling → domain synthesis
        ├── P4-Skill: Stateful skills (desktop, doc gen, introductions)
        └── Fallback: LLM streaming (Qwen → Claude)
                │
                ▼
        Persona Engine ─── 38 pools, ~184 templates
                │
                ▼
        Kokoro TTS ─── StreamingAudioPipeline ─── aplay (gapless)
```

### Core Module Map

| Module | Lines | Role |
|--------|-------|------|
| `conversation_router.py` | ~3,003 | 18-layer priority chain, shared across 3 frontends |
| `pipeline.py` | ~2,054 | Event-driven Coordinator, STT/TTS workers |
| `interaction_cache.py` | ~1,993 | 5-phase artifact cache with cross-session retrieval |
| `llm_router.py` | ~1,814 | Qwen/Claude routing, tool calling, 14 domain synthesis prompts |
| `task_planner.py` | ~1,097 | Compound detection, LLM plan gen, voice interrupts |
| `tts_normalizer.py` | ~1,072 | 22-pass text normalization for spoken output |
| `skill_manager.py` | ~931 | 5-layer semantic intent matching |
| `health_check.py` | ~908 | 5-layer system diagnostic |
| `continuous_listener.py` | ~885 | VAD, wake word, ambient filter, conversation windows |
| `persona.py` | ~726 | Response pools, system prompts, guest mode |
| `tts.py` | ~722 | Kokoro + Piper, streaming pipeline, ack cache |
| `jarvis_web.py` | ~3,875 | aiohttp WebSocket server, vision, file handling |

### 11 LLM Tools

| Tool | What | Always Available |
|------|------|:---:|
| `get_weather` | OpenWeatherMap forecast | |
| `get_system_info` | CPU, RAM, disk, GPU, processes (8 sub-handlers) | |
| `find_files` | File search, line counting, dir sizes (11 actions) | |
| `developer_tools` | Git, codebase search, shell, system admin (13 actions, 3-tier safety) | |
| `manage_reminders` | Add, list, cancel, ack, snooze + Google Calendar sync | |
| `get_news` | 16 RSS feeds, urgency classification, semantic dedup | |
| `web_search` | DuckDuckGo + trafilatura multi-source synthesis | Yes |
| `recall_memory` | SQLite text + FAISS semantic search across stored facts | Yes |
| `take_screenshot` | gnome-screenshot + window crop → LLM vision | Yes |
| `capture_webcam` | Desktop webcam or mobile camera relay → LLM vision | Yes |
| `enroll_face` | Face detection + embedding storage for presence greetings | Yes |

### 3 Stateful Skills

| Skill | Intents | Why Not a Tool |
|-------|---------|---------------|
| **App Launcher** | 16: launch, close, fullscreen, minimize, maximize, volume, workspace, focus, clipboard | Needs D-Bus + desktop integration |
| **File Editor** | 5: write, edit, read, delete, list + PPTX/DOCX/PDF generation | Two-stage LLM pipeline, confirmation flows |
| **Social Introductions** | 5: meet, who-is, recall, forget, update | Multi-turn state machine with pronunciation checks |

---

## Development Timeline (Condensed)

| Period | What Happened |
|--------|-------------|
| **Feb 9-13** | Foundation: voice loop, Whisper, Piper TTS, basic skills, GPU CTranslate2 on ROCm |
| **Feb 14-17** | Feature explosion: 12 bug fixes, news, reminders, web nav, developer tools, console mode, Kokoro TTS, streaming pipeline, user profiles, conversational memory, context window, health check |
| **Feb 18-20** | Web research, prescriptive prompt design, 27 bug fixes, GNOME desktop integration, web chat UI, file editor, ambient wake word filter, edge case testing |
| **Feb 21** | Conversational flow refactor: persona engine, ConversationState, shared ConversationRouter, contextual acks. Whisper v2 fine-tuning (94%+) |
| **Feb 22-23** | Document generation (PPTX/DOCX/PDF), LLM metrics dashboard, systemd services |
| **Feb 24-25** | Qwen3.5-35B-A3B upgrade, self-awareness layer, task planner (4 phases), social introductions + people manager |
| **Feb 26-27** | LLM-centric tool calling migration: Phase 1 (3 skills, 600/600 trials) + Phase 2 (7 tools, 1,200+ trials, tool-connector plugin system) |
| **Feb 28** | Dual GPU setup (RX 7900 XT compute + RX 7600 display), ctx-size 7168 → 32768 |
| **Mar 1-3** | Unified awareness, MCP bridge (bidirectional), interaction artifact cache (5 phases), self-managing memory (CMA 6/6), recall_memory tool |
| **Mar 4-7** | Vision: 7 phases (multimodal LLM, web/mobile upload, console commands, screenshot tool, webcam tool, mobile camera relay, presence detection). 180 vision tests. |
| **Mar 7-11** | 62-conversation behavioral test suite, 10 iterative fix rounds, 17-domain classifier, 14 synthesis prompts, 314 unit tests |

---

## Roadmap

### Current Priority (Owner-Directed)

1. **IMAP email via MCP** — Read, search, archive email by voice/web/mobile. Config stub + MCP bridge ready.
2. **Mobile iOS app** — Web UI works on mobile now. Native iOS app planned (6 phases).
3. **CalDAV calendar** — Apple Calendar integration for secondary user. Blocked on app-specific password.
4. **Concurrent multi-user** — Handle two simultaneous mobile users. Depends on mobile app.

### Medium Term

- LLM news classification (activate dead code in news_manager.py)
- Reminder snooze in P2 chain
- Reduce aplay 150ms sleep (PipeWire device-ready)
- Inject user facts into web search queries

### Long Term

- Threat hunting / malware analysis framework
- Home automation integration
- Emotional context awareness

---

## Performance

### Latency Breakdown

| Stage | Time |
|-------|------|
| Wake word detection | <100ms |
| Speech transcription | 0.1-0.2s (GPU) |
| Semantic intent matching | <100ms (cached embeddings) |
| Skill-handled queries | 300-600ms total |
| LLM tool calling | 2-4s (streaming, first spoken word) |
| TTS generation | <1s per sentence (Kokoro, CPU) |

### Resource Usage

| Resource | Usage |
|----------|-------|
| RAM | ~4GB (all models loaded) |
| CPU | 10-30% during processing |
| GPU VRAM | ~19.5 / 20 GB (RX 7900 XT) |
| Disk | ~25GB (models + code) |

### Test Coverage

| Suite | Count | Pass Rate |
|-------|-------|-----------|
| Unit tests (4 tiers) | 314 | 100% |
| Conversation tests | 62 | Iterative (run 037 in progress) |
| Tool calling | 1,200+ trials | 100% |
| Tool artifacts | 175 | 100% |
| Vision pipeline | 180 | 100% |
| Web handler | 61 | 100% |
| Memory management | 43 | 100% |

---

## Design Principles

1. **Local first, cloud never** — Claude API exists as a quality fallback. In practice it fires <1% of the time. Everything else is on-box.
2. **The LLM decides** — Qwen3.5 picks which tools to call. Skills exist only for things that need deterministic control flow (desktop integration, multi-turn state machines, nested LLM pipelines).
3. **Stream everything** — LLM tokens stream to TTS, TTS streams to audio. No buffering full responses.
4. **One router, three frontends** — Voice, console, and web share the same 18-layer ConversationRouter. Add a frontend, not a routing layer.
5. **One file, one tool** — Drop a `.py` file in `core/tools/` and it's live. No wiring, no imports, no registry edits.
6. **Degrade gracefully** — GPU → CPU. Kokoro → Piper. Qwen → Claude. Desktop webcam → mobile camera. Every component has a fallback.
7. **Test like a user** — The 62-conversation behavioral suite sends natural language over WebSocket and validates the full pipeline. Unit tests are necessary but not sufficient.

---

## Getting Started

See the [README](README.md) for full installation instructions, model download links, and configuration reference.

```bash
# Quick start — console mode (no mic needed)
python3 jarvis_console.py

# Voice mode (requires mic + wake word setup)
systemctl --user start jarvis

# Web UI
python3 jarvis_web.py
# Open http://127.0.0.1:8088
```

---

*Built with care, tested obsessively, improved daily.*
