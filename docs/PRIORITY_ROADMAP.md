# JARVIS Priority Development Roadmap

**Created:** February 19, 2026 (session 6)
**Updated:** March 12, 2026 (session 254 — restructured: completed work collapsed, active items lead)
**Method:** Exhaustive sweep of all docs, archives, memory files, code comments, and design documents
**Ordering:** Genuine ROI for effort — difficulty/complexity vs real-world payoff

---

## Priority Tier 1: Owner-Directed Priority Sequence

*the user's ordered priority list. Work these in sequence.*
*Strategic direction (session 166): prioritize features with highest cross-platform utility (desktop web UI + mobile). Vision and email are force multipliers.*

| # | Item | Effort | Status | Notes |
|---|------|--------|--------|-------|
| 20P7c | **Vision 7c live test** — enroll faces, enable presence detection + greetings | 1-2 hours | **NEXT** | Code complete + 180 tests. Enable `vision.presence.enabled: true` |
| — | **IMAP email via MCP** — read, search, archive email by voice/web/mobile | Variable | NOT STARTED | Config stub + MCP bridge ready. the user=Gmail, secondary=AOL |
| 60 | **Mobile app** — web UI phase 1 done. Native iOS app planned (6 phases) | 5-8 days | **PHASE 1 DONE** | Plan: `memory/plan_mobile_ios_app.md` |
| — | **CalDAV calendar (secondary user)** — Apple Calendar integration via CalDAV | 4-6 hours | BLOCKED — waiting on app-specific password | DB column exists (`caldav_event_id`), zero CalDAV code |
| 61 | **Concurrent multi-user support** — handle two simultaneous mobile users | 4-8 hours | NOT STARTED | Depends on #60. Needs `--parallel 2`, per-user history, STT/TTS queuing |
| 11 | **"Onscreen please" — retroactive visual display** | 2-3 hours | PARTIAL | Opens generated docs. Retroactive display of arbitrary artifacts NOT implemented |

---

## Priority Tier 2: High Value, Ready to Build

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 17 | **LLM news classification** — activate `_llm_classify()` in news_manager.py | 2-3 hours | Better urgency classification than keyword rules | Dead code at `news_manager.py:393` — never called |
| 44 | **Reminder snooze in P2 chain** — distinguish "got it" (ack) vs "snooze 10 min" (snooze) vs "what reminder" (query) | 2-3 hours | Currently blanket ack — loses snooze/query intent | Zero snooze references in conversation_router.py |
| 54 | **Reduce `_open_aplay` 150ms sleep** — PipeWire device-ready wait may be reducible to 50ms | 1-2 hours | Saves 150ms per aplay open (300ms with ack + response) | Still `time.sleep(0.15)` at `tts.py:387`. Risk: too short causes broken audio |
| 7 | **Inject user facts into web search** — surface stored facts (location, preferences) during `stream_with_tools()` | 3-4 hours | Personalized search results ("best coffee near me" uses stored location) | Memory context passed to LLM for response gen, NOT injected into search queries |

---

## Priority Tier 3: Medium Value

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 43 | **Mid-rundown interruption** — item-by-item delivery with "continue"/"skip"/"stop"/"defer" | 4-6 hours | Currently `deliver_rundown()` blocks on single TTS call | Needs item-at-a-time loop + active listener |
| 53 | **Merge ack + response into single audio stream** — one aplay lifecycle | 3-4 hours | Saves ~150ms + eliminates audible gap | Challenges: timing, lock contention, quality gate |
| 55 | **Network awareness skill** — device discovery, anomaly detection, threat alerts | 4-8 hours | Fits threat hunting background | Natural skill: `skills/system/network/` |
| 50 | **AI image generation (FLUX.1-schnell)** — local image gen for doc gen, hybrid with Pexels | 4-6 hours | Pexels fails for tech/abstract topics | Research complete. FLUX FP8 fits 20GB VRAM, ~12-20s/image |
| 10 | **Google Keep integration** — shared grocery/todo lists | 4-6 hours | Daily household utility | Shared access with secondary user |
| 13 | **Audio recording skill** — voice-triggered recording, date-based playback, 6 intents | 4-6 hours | Meeting notes, voice memos, dictation | skills/personal/audio_recording/ |
| 14 | **Music control (Apple Music)** — playlist learning, volume via pactl | 6-10 hours | Entertainment integration | Per-user playlists. Apple Music web interface finicky |

---

## Priority Tier 4: Larger Investments

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 21 | **Skill editing system** — "edit the weather skill" → LLM code gen, review, apply with backup | 10-15 hours (5 phases) | Voice-controlled code modification | Full design at SKILL_EDITING_SYSTEM.md. Note: VS Code + Claude Code is faster in practice |
| 22 | **Automated skill generation** — Q&A, build, test, review, deploy | 15-20 hours | End-to-end skill creation by voice. Depends on #21 | MASTER_DESIGN.md |
| 23 | **Backup automation skill** — voice-triggered, SHA256 checksums, manifest, rotation | 6-8 hours | "Jarvis, backup the system." Automated 2 AM daily | MASTER_DESIGN.md |
| 24 | **Voice authentication for sensitive ops** — re-verify voice before threat hunting, system changes | 4-6 hours | Security layer. Speaker ID Phase 3+ | MASTER_DESIGN.md |
| 25 | **Web dashboard** — local Flask/FastAPI web UI for JARVIS management | 10-15 hours | Demo/showoff feature. Low daily utility | TODO |
| 47 | **Docker container (web UI mode)** — community deployment, web UI only (no mic) | 3-5 days | Lowest barrier to community adoption | See `memory/plan_erica_voice_windows_port.md` |
| 48 | **Windows native port** — full JARVIS on Windows, abstraction layers | 2-3 weeks | Biggest community audience. Requires platform abstractions | See `memory/plan_erica_voice_windows_port.md` |
| 62 | **Usage data pipeline + CI/CD** — nightly metric extraction → analysis → regression testing | 1-2 days | Automated quality tracking at scale | Metrics tracker records to SQLite, no extraction/reporting. Depends on #60 + #61 |

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

## Active Bugs / Loose Ends

| # | Item | Severity | Notes |
|---|------|----------|-------|
| B2 | Batch extraction (Phase 4) untested | Low | Feature works, zero test coverage |
| B8 | EventTTSProxy `speak()` returns None | Medium | `done.wait()` return not captured. Causes reminder retry false positives. Nag cap mitigates. Zero tests |
| B9 | Speaker ID no accuracy benchmarks | Low | Threshold 0.75, re-enrolled with better clips. No dedicated tests |

---

## Test Gaps (validated Mar 7, session 190)

| Item | Status | Notes |
|------|--------|-------|
| Routing integration tests | **CLOSED** | `test_router.py` — 718 lines, 12 categories, adversarial priority conflicts, guest mode, multi-speaker |
| Web UI automation | **CLOSED** | `test_web_handler.py` — 61 tests, 5 phases (handler smoke, mobile routing, client detection, tool overrides, WS dispatch) |
| Skill execution tests | **OPEN** | `test_edge_cases.py` Tier 3-4 marked "future" — routing tested but no actual handler execution |
| EventTTSProxy tests | **OPEN** | Zero tests for speak() return value / timeout behavior |
| Batch extraction tests | **OPEN** | Feature implemented (Phase 4), zero test coverage |
| Speaker ID tests | **OPEN** | Zero dedicated tests, no accuracy benchmarks |

---

## Completed Items

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
- 20P7c: Vision Phase 7c — presence detection + face recognition greetings, PresenceDetector, enroll_face tool, 180 tests (Mar 6). NOT YET LIVE — needs face enrollment

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

### Post-Phase 20 (Feb 27 — Mar 3)
- Dual GPU display offload — RX 7600 display, RX 7900 XT dedicated compute (Feb 28)
- ctx-size 7168→32768 — 4.6x context expansion, SSM hybrid verified 9/9 at 25K tokens (Feb 28)
- Context enrichment — user profile + memory injection into LLM prompts (Feb 28)
- Doc gen fix — structured output formatting (Feb 28)
- Unified awareness layer — capability manifest + system state in LLM context (Mar 1)
- Calendar multi-notification support — composite keys, per-offset dedup (Mar 1)
- Memory dashboard — web page with facts/interactions explorer (Mar 2)
- Multi-user DB migration — `created_by`, `origin_endpoint` columns, 780 rows corrected (Mar 2)
- Formal address system — secondary user honorifics ("ma'am" / "Ms. Guest") (Mar 2)
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

### Other Completed (non-roadmap enhancements)
- Time injection into LLM system prompts — all 5 prompt injection points, correct time-of-day greetings (Feb 27)
- Smart ack suppression — skip acknowledgements for fast/conversational queries (Feb 22)
- Doc gen prompt overhaul — prescriptive depth, publish.sh README protection (Feb 22)
- Edge case tests expanded — Phase 1E: 144 tests (Feb 22), then 236 tests (Feb 25), then 270 tests (Feb 25)
- Ack speaker-to-mic bleed fix — pause listening during ack playback (Feb 23)
- Whisper brand-name corrections — "and videos"→"amd's", "in video"→"nvidia" (Feb 23)
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
- Memory extraction names — uses real display names (the user/secondary user) instead of "User" (Mar 7)
- Memory transient filtering — extraction prompts filter transient state ("at desk", "on screen") (Mar 7)

### Tier 3
- #40: News headline trimming — 25 per category (Feb 20)
- #15: Screenshot via GNOME — `gnome-screenshot` integration with monitor/window/all targets, D-Bus monitor detection (Feb-Mar)
- #49: Document refinement follow-ups — `_pipeline_cache` stores structure/research/images, `edit_presentation` handler for "make slide 3 more detailed" type edits (Mar)
- #51/#52: Vision/OCR — superseded by Qwen3.5 mmproj vision (Phases 1-7). Image understanding, text reading, chart analysis all handled natively via multimodal LLM (Mar 4-6)

### Tier 5
- #29: Console logging fix (Feb 19)

### Resolved Bugs
- B1: "Fullscreen" Whisper misrecognition — fixed by mic upgrade + retraining (Feb 21)
- B3: Console logging broken — fixed logger.py (Feb 19)
- B4: Topic shift threshold — already set to 0.35 (Feb 19)
- B6: Google Calendar sync token — removed `orderBy` from initial sync (Feb 19)
- B7: Calendar sync overwrites local reminder_time — fixed by multi-notification composite keys + past-event guard (Mar 1)

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
