# JARVIS Priority Development Roadmap

**Created:** February 19, 2026 (session 6)
**Updated:** March 28, 2026 (session 314 — full reprioritization against PRIME DIRECTIVE + ROI)
**Method:** Exhaustive sweep of all docs, archives, memory files, code comments, and design documents
**Ordering:** PRIME DIRECTIVE (serves the household?) + ROI (effort vs impact)

---

## Immediate — Do Next

*Foundation & bug fixes that directly improve daily experience or prevent future regressions.*

| # | Item | Effort | Why Now |
|---|------|--------|---------|
| H12 | ~~**SYSTEM_MAPS refresh**~~ | 2-3 hrs | **DONE** (session 314). All 8 maps refreshed + new self_evolution_stack.md created. All maps now have "Last validated" dates. |
| S7 | ~~**Self-evolution Step 7: Validation layer**~~ | 2-3 hrs | **DONE** (session 314). Smoke tests run before/after every config change. Auto-rollback on regression. Tested end-to-end. |
| B9 | ~~**Weather tool ignores non-local locations**~~ | 30 min | **FIXED** (session 314, 7 minutes). Weather skill now declines non-local queries → LLM tool path geocodes correctly. Prompt updated, geocoding errors no longer silently fall back to home. |
| H5 | **Ack bleed — JARVIS hears own speech as commands** | 2-3 hrs | Confuses the household, degrades trust. |

---

## Near-Term — High PRIME DIRECTIVE Value

*Daily household interactions that are currently broken or degraded.*

| # | Item | Effort | Why |
|---|------|--------|-----|
| — | **IMAP email via MCP** — read, search, archive email by voice/web/mobile | Variable | Force multiplier across all platforms. Config stub + MCP bridge ready. Owner-promoted. |
| 44 | **Reminder snooze in P2 chain** — "snooze 10 min" vs "got it" vs "what reminder" | 2-3 hrs | Daily interaction, currently loses snooze/query intent. |
| 17 | **LLM news classification** — activate dead `_llm_classify()` code | 2-3 hrs | Better urgency = better morning briefings. Code exists, just needs wiring. |
| H8 | **Greeting latency review** — 11s face-detect-to-briefing pipeline | 1-2 hrs | Noticeable delay every time someone walks in. Prosody half addressed. |
| 7 | **Inject user facts into web search** — location, preferences in search queries | 3-4 hrs | "Best coffee near me" should use stored location. Personalization multiplier. |

---

## Medium-Term — Important, Bigger Scope

*Significant features that expand what JARVIS can do for the household.*

| # | Item | Effort | Why |
|---|------|--------|-----|
| 43 | **Mid-rundown interruption** — "skip"/"go back"/"stop" during briefings | 4-6 hrs | Currently blocks on single TTS call. Major daily UX improvement. |
| H11 | **Filesystem index service** — background poller, SQLite index, instant file queries | Research + 4-8 hrs | Research existing tools first. PRIME DIRECTIVE: JARVIS should know his own filesystem. |
| — | **CalDAV calendar (secondary user)** — Apple Calendar for secondary user | 4-6 hrs | BLOCKED — waiting on app-specific password. Code ready. |
| 60 | **Mobile app Phase 2+** — native iOS app | 5-8 days | Phase 1 done. Expands household reach. |
| 61 | **Concurrent multi-user** — two simultaneous mobile users | 4-8 hrs | Depends on #60. |
| 11 | **"Onscreen please"** — retroactive visual display | 2-3 hrs | Partial. Retroactive display of arbitrary artifacts not implemented. |

---

## Longer-Term — Build When Ready

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 55 | **Network awareness skill** — device discovery, anomaly detection | 4-8 hrs | Fits threat hunting background |
| 10 | **Google Keep integration** — shared grocery/todo lists | 4-6 hrs | Daily household utility |
| 13 | **Audio recording skill** — voice memos, meeting notes | 4-6 hrs | |
| 14 | **Music control (Apple Music)** | 6-10 hrs | Per-user playlists |
| 23 | **Backup automation skill** | 6-8 hrs | "Jarvis, backup the system." |
| 24 | **Voice authentication for sensitive ops** | 4-6 hrs | Speaker ID Phase 3+ |
| 62 | **Usage data pipeline + CI/CD** | 1-2 days | Depends on #60 + #61 |
| 21 | **Skill editing system** | 10-15 hrs | VS Code + Claude Code is faster in practice |
| 22 | **Automated skill generation** | 15-20 hrs | Depends on #21 |
| 47 | **Docker container (web UI mode)** | 3-5 days | Community deployment |
| 48 | **Windows native port** | 2-3 weeks | Biggest community audience |

---

## Priority Tier 5: Deferred — Revisit When Conditions Met

| # | Item | Effort | Revisit When |
|---|------|--------|-------------|
| 56 | **Plan templates** — cache successful plan structures for common compound patterns | 3-4 hours | Repeated identical compound requests observed |
| 57 | **Plan feedback** — post-execution LLM evaluation + store successful patterns | 4-6 hours | Per-step eval data shows recurring failures |
| 58 | **Parallel step execution** — ThreadPoolExecutor + dependency graph for concurrent steps | 6-8 hours | Plans exceed 4-5 steps or latency complaints |
| 64 | **4-user concurrent inference** — expand llama-server to 4 parallel slots | Research + 2-4h | Mid-2026, once 2-user data exists |
| 26 | **STT worker process** — GPU isolation via separate subprocess | 2-3 hours | Only if GPU conflicts resurface |
| 28 | **GitHub publishing cleanup** — CONTRIBUTING.md, INSTALLATION.md, API_KEYS.md, setup.sh | 3-4 hours | Community adoption |
| 65 | **CAL-L0 Option 1 consolidation** — merge P1 dismissals + P2.8 bare acks into L0 category system, remove redundant handlers, re-enable conversation skill as single backend | 3-4 hours | Daily usage confirms L0 coverage with no false intercepts reported |
| 66 | **TTS prosody variants** — multiple tonal variations per cached phrase (warm/neutral/urgent), context-aware mood selection at playback | 4-6 hours | Ack cache rework (H2) complete, mood schema column already in TTSCache DB |

---

## Priority Tier 6: Aspirational — Someday/Maybe

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 31 | **Malware analysis framework** — QEMU sandbox, VirusTotal/Any.run, CISA reports, threat intel DB | 30-50 hours | Professional threat hunting. Build when a specific engagement needs it | MASTER_DESIGN.md |
| 32 | **Video / face recognition** — webcam for people/pets/objects, security cameras | 20-40 hours | Hardware-dependent. Qwen3-VL vision could simplify this | MASTER_DESIGN.md + DEVELOPMENT_VISION.md |
| 33 | **Tor / dark web research** — Brave Tor mode, VPN verification, session logging, sandboxed | 15-20 hours | Specialized professional use. Safety protocols critical | MASTER_DESIGN.md |
| 34 | **Emotional context awareness** — voice-based frustration/distress/laugh detection | Research-level | Could enable health monitoring, age verification, adaptive tone | MASTER_DESIGN.md |
| 35 | **Voice cloning (Paul Bettany)** — Coqui rejected, StyleTTS2 rejected, F5-TTS worth evaluating | 10-20 hours | The dream. Must be <500ms RTF. Revisit when open-source matures | TTS_VOICE_OPTIONS.md |
| 36 | **Proactive AI** — suggest actions based on usage patterns | 10-20 hours | Needs significant usage data first. "You usually check headlines at 8am..." | MASTER_DESIGN.md |
| 37 | **Self-modification** — JARVIS proposes and implements own improvements | Far future | The ultimate goal. Depends on skill editing + reliable code gen | MASTER_DESIGN.md |
| 38 | **Home automation / IoT** — RING/NEST/SimpliSafe, smart home control | Hardware-dependent | Requires IoT hardware investment. Tied to video/camera work | MASTER_DESIGN.md |
| 39 | **Collaborative threat intelligence sharing** — TLP-compliant data sharing | 10-15 hours | Part of professional framework. Depends on malware analysis (#31) | MASTER_DESIGN.md |

---

## Open Bugs & Housekeeping

*Items promoted to the priority tiers above are not repeated here. This section tracks lower-priority items.*

| # | Item | Severity | Notes |
|---|------|----------|-------|
| B2 | Batch extraction (Phase 4) untested | Low | Feature works, zero test coverage |
| B12 | **Wake word stripping too aggressive mid-conversation** | Low | Edge case in wake word removal logic. |
| B13 | **Context utilization hallucination** — LLM fabricates numbers | Low | Qwen behavior. May need grounding prompt fix. |

### Resolved (Session 314)
- ~~H7~~ find_files `du -sh` skip — **DONE**
- ~~H9~~ Observation collector — **NOT A BUG** (logs to web.log, not journald)
- ~~H10~~ Deprecate TODO_NEXT_SESSION.md — **DONE**
- ~~B10~~ LLM answers from training data — **FIXED** (3-layer: prompt + temporal regex + conflict resolution)
- ~~B11~~ Missing ack on LLM fallback — **FIXED** (never suppress ack in streaming path)

---

## Open Decisions (awaiting owner input)

| # | Item | Context | Notes |
|---|------|---------|-------|
| D1 | ~~**Approval flow authentication mechanism**~~ | Current 2FA (web review + sudo console password) is sufficient for Phase 1 (config-only). | **DECIDED** (session 314). Revisit if/when Phase 2 (prompt) or Phase 3 (code) modification is enabled. |
| D2 | ~~**API budget for Claude consultation**~~ | ~$0.075/cycle, ~$27/month at current volume. No cap needed. | **DECIDED** (session 314). J5 updated: no limitations, owner reviews periodically, JARVIS cannot override any future constraints. |
| D3 | ~~**Three Rule Sets — owner review**~~ | Claude↔Owner (6 rules), JARVIS↔Claude (6 rules), Ten Commandments (10). | **RATIFIED** (session 314). Owner reviewed and approved all three rule sets as written. |
| D4 | **Hardware build timing** | 4 options researched. Recommended: AM5 2-GPU ($11,491). 60-90 day purchase window (late May / mid-June 2026). | Owner decision on performance vs cost tradeoff pending. |

---

## Test Gaps (updated Mar 21, session 311)

| Item | Status | Notes |
|------|--------|-------|
| Routing integration tests | **CLOSED** | `tests/routing/test_routing.py` — 87 tests, CAL-L0 + tool gate + latency |
| Web UI automation | **CLOSED** | `tests/unit/test_web_handler.py` — 61 tests, 5 phases |
| Speaker ID tests | **CLOSED** | `tests/integration/test_speaker_id.py` — 5-part suite |
| V3 conversation tests | **CLOSED** | `scripts/test_suite_v3/` — 52 conversations, dual-model dispatch tracking |
| Skill execution tests | **OPEN** | `tests/unit/test_edge_cases.py` Tier 3-4 marked "future" — routing tested but no actual handler execution |
| EventTTSProxy tests | **OPEN** | speak() return value fixed but zero formal tests |
| Batch extraction tests | **OPEN** | Feature implemented (Phase 4), zero test coverage |
| CAL briefing tests | **OPEN** | Composer tested manually during session 311, no automated suite |

---

## Completed Items

### Latency Optimization Phase 1 (Session 310, Mar 18-19)
- **Phase 1a:** Tool gate — binary classifier (324 training queries) + keyword override. Skips ~1,500 tool schema tokens on non-tool queries. 55% TTFT reduction (3,800ms -> 1,650ms)
- **Phase 1b:** CAL-L0 reflexive layer — 13 categories, ~200 patterns from ISO/SWBD/CLINC/Dialogflow/Rasa research. P2.9 routing checkpoint with compound utterance guard. 87/87 tests. ~12ms routing
- **Phase 1c:** TTS audio cache — 308 pre-generated response phrases (sir + mum variants). Zero TTS latency on cache hit
- **Phase 1d:** KV cache reuse — `--cache-reuse 256` on llama-server. 40-60% TTFT reduction on follow-up turns
- **Webcam contention fix** — voice service frame server (localhost:8089), web service proxies instead of opening own ffmpeg
- **Logging infrastructure fix** — 18 files migrated to get_logger(), _ensure_handlers() added to Logger class
- **VRAM audit** — RX 7600 baselined (4,198 MB used, 3,832 MB free at peak). Kokoro GPU benchmarked and ruled out (CPU 4.7x faster)
- **Routing test harness** — `tests/routing/test_routing.py` (87 tests, CAL-L0 + tool gate + latency)

### Latency Optimization Phase 2+3 (Session 311, Mar 19)
- **Phase 2: Dual-model dispatch** — tool result synthesis routed to Qwen3.5-4B (port 8081) with transparent fallback to 35B. Internal generate() calls (topic extraction, summarization) also routed to 4B. Call chain accumulator tracks all LLM calls per pipeline run.
- **Phase 3: Focused synthesis prompt** — 4B synthesis uses persona.system_prompt_brief() instead of full tool-calling system prompt. 56-60% synthesis TTFT reduction (10-11s -> 4-5s).
- **Observability** — V3 test suite captures dual-model data: llm_calls, llm_provider, llm_routing_model, routing_ttft_ms, synthesis_ttft_ms. Report includes Model Dispatch section. Console shows [4B synth] / [35B] tags.

### CAL Phases 1-6 (Session 311, Mar 19-20)
- **Phase 1: Greeting flow foundation** — window_source tagging on ConversationState, presence greeting absorption in CAL-L0, face ID -> speaker identity propagation. Validated live.
- **Phase 2: Awareness Accumulator** — always-on priority queue with calendar + weather adapters. Deterministic scoring (urgency x 0.3 + time_pressure x 0.3 + novelty x 0.2 + user_relevance x 0.2). Delivery log SQLite dedup. Calendar fix: queries both primary + JARVIS calendars. All-day event detection + pre-naturalized summaries.
- **Phase 3: Briefing Composer** — 4B-powered natural language synthesis. Split prompt (single-item tight/12w vs multi-item weaving/35w). User identity flows through full pipeline. Extensive prompt engineering: Qwen prompt leakage discovery, token/word dual constraints.
- **Phase 4: Reminder + News adapters** — pending acks, upcoming reminders within 2h, critical/high news headlines.
- **Phase 5: Moment expansion** — return-from-absence trigger (budget 2/0.4), "catch me up" explicit request (budget 5/0.1), post-task nudge (budget 1/0.6, gated to substantive tasks only).
- **Phase 6: Ambient awareness** — critical items (score >= 0.85) spoken unprompted when user PRESENT, conversation inactive, 60s cooldown.
- **Calendar bug fix** — get_upcoming_context() was querying primary calendar only; events on JARVIS calendar were invisible. Now queries both.
- **Face ID -> speaker identity** — presence detector sets conversation.current_user from face recognition, voice pipeline inherits identity.

### ROCm Stack Rebuild + 4B Model (Session 310, Mar 19)
- **PyTorch from source** — v2.10.0 built against ROCm 7.2.0, `PYTORCH_ROCM_ARCH="gfx1100;gfx1102"`, installed to venv
- **llama.cpp rebuilt** — `-DGGML_HIP_ROCWMMA_FATTN=ON` for RDNA3 flash attention, dual GPU targets
- **Qwen3.5-4B deployed** — infrastructure model on RX 7600 (port 8081). For synthesis/summarization, not tool routing.
- **TTSCache** — persistent disk cache (281 phrases, 39MB). Startup load: 11ms (was 170s CPU spike)
- **Web embeddings -> CPU** — config-driven `embeddings.voice_device`/`web_device`. Freed ~1GB VRAM on RX 7600
- **GFX targets corrected** — RX 7600 services use `11.0.2` (native gfx1102), RX 7900 XT stays `11.0.0`
- **Venv created** — `/home/user/jarvis/.venv`, all services use venv Python
- **Audio diagnostics** — callback heartbeat, stream health check, cache generation throttle

### Component Upgrade Wave (Session 309, Mar 17)
- Speaker ID: Resemblyzer -> SpeechBrain ECAPA-TDNN (192-dim, 0.80% EER, 10x accuracy)
- Face Recognition: dlib/Haar -> InsightFace ArcFace (512-dim, 99.83% LFW, single-pass)
- Embeddings: all-MiniLM-L6-v2 -> nomic-embed-text-v1.5 (768-dim, RX 7600 GPU, +6 MTEB)
- VAD: WebRTC -> Silero v6.2.1 ONNX (neural, stateful, 16% fewer errors)
- GPU routing: HIP_VISIBLE_DEVICES=0 (Whisper+Nomic on RX 7600, LLM isolated on 7900 XT)
- Speaker ID threshold: 0.30 (SpeechBrain default is 0.25; clean speech scores 0.6-0.7)

### LLM-Centric Architecture Migration (#20) — ALL PHASES COMPLETE
- **Phase 1** (Feb 26): 3 skills as tools (system_info, filesystem, time). 100% accuracy (600/600), 822ms avg. Commit `06dd741`
- **Phase 2** (Feb 27): 7 tools total (6 domain + web_search). 1,200+ trials, 99.6% overall. Tool-connector plugin system (`ba80e5a`). Sub-phases: weather, reminders, conversation (disabled), developer_tools, news. Now 11 tools (6 domain + 5 always-included)
- **Phase 3** (Mar 4-7): Vision complete — all 7 phases (multimodal LLM, web/mobile upload, console /image + /screenshot, voice take_screenshot, thumbnails + lightbox, webcam + mobile camera, presence detection + face recognition)
- **Phase 4** (Mar 1): RESOLVED — hybrid architecture retained, skills and tools coexist by design
- **Research:** `memory/research_qwen35_prompt_control.md` (72 sources, Sections A-O)

### Vision (Phases 1-7) — ALL COMPLETE
- 20P3: Vision Phases 1-3 — multimodal LLM + web/mobile image upload (Mar 4)
- 20P4: Vision Phase 4 — console `/image` + `/screenshot` commands (Mar 4)
- 20P5: Vision Phase 5 — `take_screenshot` LLM tool for voice mode (Mar 4)
- 20P6: Vision Phase 6 — image thumbnails in web chat with lightbox + session persistence (Mar 5)
- 20P7a: Vision Phase 7a — desktop webcam capture, WebcamManager ffmpeg MJPEG, web endpoints (Mar 6)
- 20P7b: Vision Phase 7b — mobile camera capture, MobileCameraRelay WS protocol, auto-routing (Mar 6)
- 20P7c: Vision Phase 7c — presence detection + face recognition greetings, PresenceDetector, enroll_face tool, 180 tests (Mar 6). **LIVE** — face enrolled, presence detection active (Mar 18)
- Face Enrollment Voice Flow — P2.56 direct handler, multi-turn with pose instructions + shutter sound, "I'm ready" prompt (Mar 18)

### Housekeeping (Completed)
- **H1:** Test directory consolidation (session 311) — 24 scripts -> tests/{unit,integration,memory,components}
- **H2:** Ack cache rework (session 311) — contextual 4B ack generation replaces generic cached phrases
- **H3:** Startup greeting timing — CLOSED. Log-vs-TTS delta only, not user-facing. Normal startup time.
- **H4:** Disable router/llm DEBUG logging (session 311) — both commented out in config.yaml
- **H6:** News feeds (session 311) — 3 new categories (AI, LLM, local_llm), 9 new feeds, max_headlines_per_feed 20→3

### Resolved Bugs
- **B2-new:** Memory recall 768/384 dimension mismatch — RESOLVED (session 311). FAISS rebuilt with 768-dim nomic embeddings, backfill script fixed for dynamic dimension.
- **B3-new:** "my name is X" → SocialIntroductions loop — RESOLVED (session 311). P2.58 self-identification pre-filter routes to memory system.
- **B4-new:** TTS honorific split — RESOLVED. Honorific appended to final text chunk before TTS, not as separate call (pipeline.py:1535)
- **B8:** EventTTSProxy speak() returns None — RESOLVED. `return done.wait(timeout=60)` propagates True/False to caller (pipeline.py:300)
- **B8-new:** FAISS index empty after upgrade — RESOLVED (session 311). Backfill rebuilt 299 vectors at 768-dim.
- **#50:** AI image generation — DONE (session 285+). FLUX.2-klein-4B on RX 7900 XT via GPU swap. Text-to-image + img2img. ~12-20s warm, ~90-200s cold.
- **B1:** "Fullscreen" Whisper misrecognition — fixed by mic upgrade + retraining (Feb 21)
- **B3:** Console logging broken — fixed logger.py (Feb 19)
- **B4:** Topic shift threshold — already set to 0.35 (Feb 19)
- **B6:** Google Calendar sync token — removed `orderBy` from initial sync (Feb 19)
- **B7:** Calendar sync overwrites local reminder_time — fixed by multi-notification composite keys + past-event guard (Mar 1)

### Validated Complete (session 256, Mar 12)
- #25: Web dashboard — `web/dashboard.html`, `dashboard.js`, `dashboard.css` with metrics, charts, live WebSocket status
- #53: Merge ack + response audio — both serialized through same `_tts_worker()` queue, single persistent aplay
- #54: Reduce aplay sleep — optimized to 0.15s with smart 5-retry exponential backoff

### Tier 0 (Quick Wins)
- Rotate OpenWeather API key (Feb 19)
- Qwen sampling params — top_p=0.8, top_k=20 (Feb 19)
- Install wl-clipboard (Feb 19)
- Enable GNOME extension (Feb 19)
- Enroll primary user voice (Feb 16)

### Tier 1 (High ROI)
- Whisper retraining — 198 phrases, 94%+ accuracy (Feb 21)
- Keyword routing improvements — all 5 skills updated (Feb 18-19)
- Topic shift threshold tuning — 0.35 confirmed (Feb 19)
- News urgency filtering (Feb 19)
- #12: Profile-aware skill routing (Mar 4, session 158)
- #46: Dual-model STT (Mar 4, session 159)

### Tier 2 (Medium Effort)
- #8: Minimize web search latency — parallel fetches, embedding cache (Feb 19-20)
- #41: Web UI session sidebar — all 5 phases complete (Feb 20)
- #42: Document generation — PPTX/DOCX/PDF with web research + Pexels images (Feb 22)
- #45: Qwen3-VL-8B model upgrade — ROCm rebuild, self-quantized Q5_K_M, 80.2 tok/s (Feb 22)
- #59: Social introductions — butler-style greeting, PeopleManager + SQLite contacts, TTS pronunciation overrides (Feb 25)
- #63: Active user selection — console `--user` flag + web UI `<select>` + WebSocket `set_user` (Mar 2)

### Post-Phase 20 (Feb 27 - Mar 3)
- Dual GPU display offload — RX 7600 display, RX 7900 XT dedicated compute (Feb 28)
- ctx-size 7168->32768 — 4.6x context expansion, SSM hybrid verified 9/9 at 25K tokens (Feb 28)
- Context enrichment — user profile + memory injection into LLM prompts (Feb 28)
- Doc gen fix — structured output formatting (Feb 28)
- Unified awareness layer — capability manifest + system state in LLM context (Mar 1)
- Calendar multi-notification support — composite keys, per-offset dedup (Mar 1)
- Memory dashboard — web page with facts/interactions explorer (Mar 2)
- Multi-user DB migration — `created_by`, `origin_endpoint` columns, 780 rows corrected (Mar 2)
- Formal address system — secondary user honorifics (Mar 2)
- Readback flow — structured readback for skill responses (Mar 2)
- Interaction artifact cache — 5 phases: hot/warm/cold tiers, cross-session retrieval (Mar 3)
- Structured readback + delivery modes (Mar 3)
- Tool artifact wiring — all 7 tools centralized in pipeline.py + interaction_cache.py (Mar 3)
- Kokoro G2P overrides — pronunciation corrections (Mar 3)
- CMA 6/6 — Consolidation & Abstraction + Associative Linking in memory system (Mar 3)
- Self-managing memory — per-turn extraction + recall_memory tool (MemGPT pattern) (Mar 3)
- MCP bridge Phase 1 + 2 — outbound server (7 tools) + inbound client (external servers as native tools) (Mar 3)
- recall_memory tool — 8th LLM tool, memory search via self-managing memory (Mar 3)
- #18: Bare ack as answer — P2.8 handler with `jarvis_asked_question` context (verified Mar 4)
- #19: Web query memory — superseded by artifact cache Phase 5 cross-session retrieval (verified Mar 4)
- Context window Phase 3 — background Qwen summarization (`context_window.py:610-663`) (verified Mar 4)
- Context window Phase 4 — SQLite persistence (`context_window.py:385-549`) (verified Mar 4)
- Memory _pending_forget Phase 6 — full confirm/cancel at P2.5 (verified Mar 4)
- Mobile routing fixes A-D — web_navigation semantic tightening, web_search guardrails, mobile skill/tool filtering, always-on tool fallback, pre-exec skill blocking (Mar 5)

### Tier 3
- #40: News headline trimming — 25 per category (Feb 20)
- #15: Screenshot via GNOME — `gnome-screenshot` integration with monitor/window/all targets, D-Bus monitor detection (Feb-Mar)
- #49: Document refinement follow-ups — `_pipeline_cache` stores structure/research/images, `edit_presentation` handler (Mar)
- #51/#52: Vision/OCR — superseded by Qwen3.5 mmproj vision (Phases 1-7). Image understanding, text reading, chart analysis all handled natively via multimodal LLM (Mar 4-6)

### Tier 5
- #29: Console logging fix (Feb 19)

### Other Completed (non-roadmap enhancements)
- Time injection into LLM system prompts — all 5 prompt injection points, correct time-of-day greetings (Feb 27)
- Smart ack suppression — skip acknowledgements for fast/conversational queries (Feb 22)
- Doc gen prompt overhaul — prescriptive depth, publish.sh README protection (Feb 22)
- Edge case tests expanded — Phase 1E: 144 tests (Feb 22), then 236 tests (Feb 25), then 270 tests (Feb 25)
- Ack speaker-to-mic bleed fix — pause listening during ack playback (Feb 23)
- Whisper brand-name corrections — "and videos"->"amd's", "in video"->"nvidia" (Feb 23)
- Preferred-mic hot-swap recovery — device monitor recovers from wrong-mic fallback (Feb 23)
- jarvis-web.service — systemd service for web UI, enabled for auto-start (Feb 23)
- WebUI health check brief mismatch — spoken vs on-screen now consistent (Feb 23)
- Task Planner — 4 phases: self-awareness, compound detection + LLM planning + execution, guardrails, advanced features (Feb 24-25)
- Task Planner bug fixes — pause/resume guards, eval timeout, skip-that, 12 new tests (Feb 25)
- Rundown bug fixes — event time dedup, weekly re-offer, missed events (Mar 2)
- Reminder staleness guard — auto-cancel reminders >24h overdue (Mar 2)
- 5 additional bug fixes (Mar 2)
- Web handler test suite — 61 tests, 5 phases (handler smoke, mobile routing, client detection, tool overrides, WS dispatch) (Mar 6)
- HUD context % — shows usage percentage instead of raw segment/token counts (Mar 7)
- Memory extraction names — uses real display names instead of "User" (Mar 7)
- Memory transient filtering — extraction prompts filter transient state ("at desk", "on screen") (Mar 7)

---

## Sources Consulted

- `docs/TODO_NEXT_SESSION.md` — current tier-based TODO
- `docs/DEVELOPMENT_VISION.md` — LLM-centric architecture plan
- `docs/SKILL_EDITING_SYSTEM.md` — full 5-phase skill editor design
- `docs/STT_WORKER_PROCESS.md` — GPU isolation architecture
- `.archive/docs/GITHUB_PUBLISHING_PLAN.md` — pre-publication plan (completed Feb 18)
- `.archive/docs/MASTER_DESIGN.md` — original comprehensive design (email, music, malware, IoT, profiles, voice auth, backup, etc.)
- `memory/plan_erica_voice_windows_port.md` — dual-model voice + Windows portability plans
- `memory/verified_outstanding_items.md` — ground truth from session 152 codebase audit

---

**Total: 64 development ideas + 30+ non-roadmap enhancements completed, sourced from 12+ documents across the entire project.**
