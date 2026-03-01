# JARVIS Priority Development Roadmap

**Created:** February 19, 2026 (session 6)
**Updated:** February 27, 2026 (session 104 — Phase 2 COMPLETE + tool-connector plugin system)
**Method:** Exhaustive sweep of all docs, archives, memory files, code comments, and design documents
**Ordering:** Genuine ROI for effort — difficulty/complexity vs real-world payoff

---

## Tier 0: Quick Wins — All Complete

*Nothing remaining. See Completed Items below.*

---

## Tier 1: High ROI, Low-Medium Effort — All Complete

*Nothing remaining. See Completed Items below.*

---

## TOP PRIORITY: LLM-Centric Architecture Migration

*ALL other development is deprioritized behind this. Promoted from Tier 4 (session 82, Feb 26).*

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 20 | **LLM-centric architecture migration** — skills become tools, not destinations. Qwen3.5 receives every request, decides which tools to call | 20-40 hours (4 phases) | Eliminates fragile 13-tier routing, unlocks full Qwen3.5 capabilities (coding, vision, reasoning), solves conversation constraints | DEVELOPMENT_VISION.md. Research plan: `memory/research_qwen35_prompt_control.md`. **No longer waiting for smaller model** — Qwen3.5-35B-A3B has coding (SWE-bench 69.2) + vision + tool calling proven. Primary hurdle: prompt engineering for reliable tool selection |

**Blocking research:** COMPLETE. See `memory/research_qwen35_prompt_control.md` (72 sources, Sections A-O).

**Phase sequence (from DEVELOPMENT_VISION.md):**
1. ~~Phase 1: Low-stakes skills as tools (system_info, filesystem, time)~~ — **COMPLETE Feb 26.** 100% accuracy (600/600 trials), 822ms avg latency, 266/266 existing tests pass. Commit `06dd741`.
2. ~~Phase 2: API-backed + complex skills as tools~~ — **COMPLETE Feb 27.** 7 tools total (6 domain + web_search). 1,200+ trials, 100% on domain categories, 99.6% overall (523/525). Tool-connector plugin system built (`ba80e5a`). Sub-phases: 2.1a weather (`1be0cb1`), 2.1b reminders (`49eca5c`), 2.2 conversation disabled (`aa2f524`), 2.3 developer_tools (`a6ae616`), 2.4 news (`578e3c9`). get_time removed post-Phase 2 (time/date handled by TimeInfoSkill via semantic matching).
3. Phase 3: Vision-enabled (mmproj activation, screen reading, web nav with vision) — **NEXT.** mmproj smoke-tested at server level, needs console/web input wiring.
4. ~~Phase 4: Routing layer evaluation~~ — **RESOLVED Mar 1.** Hybrid architecture retained — skills and tools coexist by design.

---

## Tier 2: High ROI, Medium Effort — Next Wave

*Deprioritized behind LLM-centric migration. Many items simplify or become obsolete once migration completes.*

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 7 | **Inject user facts into web research** — surface stored facts (location, preferences) during `stream_with_tools()` | 3-4 hours | Personalized search results ("best coffee near me" uses stored location) | Risk: history poisoning needs careful scoping |
| 9 | **Email skill (Gmail)** — voice-composed email via Gmail API + OAuth | 6-8 hours | Major productivity — compose, read, reply, search, archive by voice | Same OAuth pattern as Calendar. Full schema in MASTER_DESIGN.md |
| 10 | **Google Keep integration** — shared grocery/todo lists | 4-6 hours | Daily household utility — "add milk to the grocery list" | Shared access w/ secondary user |
| 11 | **"Onscreen please" — retroactive visual display** — buffer last raw output, display on command | 2-3 hours | Bridge voice-to-visual gap. "Show me that" after JARVIS speaks an answer | TODO |
| 12 | **Profile-aware commands (multi-user)** — "my calendar" loads correct user's data based on who spoke | 3-4 hours | Infrastructure already built (speaker ID + profiles). Just needs skill-level integration | MASTER_DESIGN.md |
| 46 | **Dual-model voice recognition** — speaker-ID routes to user-specific fine-tuned vs stock Whisper | 4-6 hours | Multi-user STT without degrading primary user's accuracy | Waiting for secondary user enrollment. See `memory/plan_erica_voice_windows_port.md` |
| 49 | **Document refinement follow-ups** — cache last structure/research, add `refine_document` intent for iterative revision | 3-4 hours | "Make slide 3 more detailed" / "add more statistics" after doc gen. Currently stumps JARVIS — no pipeline state persistence | Cache structure JSON + research context on skill instance, 10-min expiry, LLM modifies cached structure on follow-up |
| 50 | **AI image generation (FLUX.1-schnell)** — local image gen for doc gen, hybrid with Pexels | 4-6 hours | Pexels fails for tech/abstract topics. LLM decides per-slide: Pexels (photographic) vs FLUX (conceptual). Pre-load during research phase to hide cold start | Research complete: `memory/research_image_gen_ocr_selfprompt.md`. FLUX FP8 fits 20GB VRAM, ~12-20s/image. SDXL Lightning as faster fallback |
| 51 | **Vision/OCR skill — Phase 1 Tesseract** — "read this" / "what does this say" via screenshot + OCR | 1-2 days | Immediate daily utility. CPU-only, 95-98% accuracy, 0.5-2s/page. Input via clipboard/screenshot/file | Proposed: `skills/system/vision/`. 4 intents: read_screen, describe_image, read_document, read_chart |
| 52 | **Vision/OCR skill — Phase 2-3 Qwen3-VL** — full image understanding via mmproj vision encoder | 3-5 days | Chart reading, diagram understanding, visual Q&A, slide verification, web UI file upload. mmproj already downloaded (718MB) | Dynamic mmproj loading (0.6GB) only for image tasks. Supersedes earlier Qwen2.5-VL plans |
| 55 | **Network awareness skill** — "what's on my network?" / "anything suspicious?" via voice | 4-8 hours | Device discovery (arp-scan/nmap), baseline known vs unknown devices, port/service anomaly detection, traffic analysis summaries, threat alerts | Fits threat hunting background. Natural skill: `skills/system/network/`. Intents: scan_network, list_devices, check_threats, network_status |
| 60 | **Mobile iOS app — COMING VERY SOON** — native Swift app with always-listening wake word + full chat UI + real-time voice | 5-8 days | JARVIS from anywhere via iPhone. Porcupine "Jarvis" wake word (on-device), WebRTC voice streaming to server (Whisper STT + Kokoro TTS), WKWebView chat UI, Tailscale VPN, Apple Shortcuts integration | Plan: `memory/plan_mobile_ios_app.md`. 6 phases. Requires Apple Developer account ($99/yr) + Mac with Xcode |
| 61 | **Concurrent multi-user support** — handle two simultaneous mobile users (primary + secondary) | 4-8 hours | REQUIRED before dual-phone deployment. llama-server `--parallel 2`, per-user chat history, STT/TTS request queuing, session isolation, speaker-ID-based context separation | Depends on #60. Current bottlenecks: `--parallel 1`, single chat_history.jsonl, shared context window |
| 62 | **Usage data pipeline + CI/CD** — nightly automated collection of interaction data → analysis → regression testing | 1-2 days | Two active mobile users = real usage data at scale. Automated: metric extraction, pattern analysis, test case generation, quality regression, routing accuracy tracking. Builds on existing metrics_tracker.py + chat_history.jsonl | Depends on #60 + #61. Cron/systemd timer for nightly runs. Output: daily report + auto-generated edge case tests |

---

## Tier 3: Medium ROI, Higher Effort — When Ready

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 13 | **Audio recording skill** — voice-triggered recording, date-based playback, 6 intents | 4-6 hours | Meeting notes, voice memos, dictation | skills/personal/audio_recording/ |
| 14 | **Music control (Apple Music)** — playlist learning, volume via pactl | 6-10 hours | Entertainment integration. Apple Music web interface is finicky | Per-user playlists in MASTER_DESIGN.md |
| 15 | **Screenshot via GNOME extension** — add screenshot D-Bus method, bypass portal dialog | 2-3 hours | Developer tools "show me" integration, visual debugging | Phase 5c from desktop plan |
| 16 | **Unknown speaker / guest mode** — unknown voice leads to limited access, no personal data | 3-4 hours | Security + graceful handling of guests. Emergency override for voice re-enrollment | MASTER_DESIGN.md |
| 17 | **LLM news classification** — activate `_llm_classify()` in news_manager.py | 2-3 hours | Better urgency classification than keyword rules. Method already exists (reserved) | news_manager.py:378 |
| 18 | **Bare ack as answer** — detect "yeah"/"no" as answers to JARVIS questions vs new commands | 3-4 hours | Conversational naturalness. Currently "yeah" after a question = treated as command | TODO |
| 19 | **Web query memory** — SQLite DB of last 100 web queries + results, "what did we look up?" | 3-4 hours | Some functionality in conversational memory already. Dedicated lookup is cleaner | MASTER_DESIGN.md |
| 43 | **Mid-rundown interruption** — item-by-item delivery with "continue"/"skip"/"stop"/"defer" commands | 4-6 hours | Currently `deliver_rundown()` blocks on single TTS call. Needs item-at-a-time loop + active listener during delivery | Identified during Phase 2 testing (2A-05..08) |
| 44 | **Reminder ack intent parsing** — distinguish "got it" (ack) vs "snooze 10 min" (snooze) vs "what reminder" (query) at P2 | 2-3 hours | Currently P2 `_handle_reminder_ack()` is a blanket ack — loses snooze/query intent | Identified during Phase 2 testing (2B-02..03) |
| 47 | **Docker container (web UI mode)** — community deployment, web UI only (no mic) | 3-5 days | Lowest barrier to community adoption. Proves concept for external users | See `memory/plan_erica_voice_windows_port.md` |
| 53 | **Merge ack + response into single audio stream** — feed ack PCM as first bytes into StreamingAudioPipeline | 3-4 hours | Saves ~150ms + eliminates audible gap. One aplay lifecycle instead of two. User flagged: "I don't want to forget about this" | Challenges: timing (ack at 0.3s, pipeline at ~0.5-1s), lock contention, quality gate |
| 54 | **Reduce `_open_aplay` 150ms sleep** — PipeWire device-ready wait at tts.py:319 may be reducible to 50ms | 1-2 hours | Saves 150ms per aplay open (300ms when ack + response both play). Test: change to 0.05, run 50 commands, count retry warnings | Risk: too short causes broken audio mid-sentence |

---

## Tier 3.5: Deferred Task Planner Enhancements — Revisit After Extended Usage

*Deferred Feb 25, 2026 (session 70). All three shelved after discussion — low ROI for current 2-4 step plans. Revisit after 2-4 weeks of real-world planner usage to see if patterns emerge that justify the complexity.*

| # | Item | Effort | ROI | Revisit When |
|---|------|--------|-----|--------------|
| 56 | **Plan templates** — cache successful plan structures for common compound patterns | 3-4 hours | Avoid re-planning identical requests. Currently negligible savings since plans are short and LLM generation is fast | When you notice repeated identical compound requests (e.g., same 3-step pattern daily). Check usage logs mid-March 2026 |
| 57 | **Plan feedback** — post-execution LLM evaluation + store successful patterns | 4-6 hours | Learn from what worked. Already have per-step eval (Phase 4D); this adds whole-plan post-hoc pass + pattern storage | When per-step evaluation data shows recurring plan failures that a feedback loop could prevent. Mid-March 2026 |
| 58 | **Parallel step execution** — ThreadPoolExecutor + dependency graph for concurrent steps | 6-8 hours | Execute independent steps simultaneously. Saves 2-3s on 3-step independent plans. Complex: concurrent TTS, error propagation, interrupt handling | When plans regularly exceed 4-5 independent steps OR latency becomes a user complaint. April 2026+ |

---

## Tier 4: High Effort, Transformative — Strategic Investments

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 20 | ~~**LLM-centric architecture migration**~~ — **PROMOTED to TOP PRIORITY** | — | — | See TOP PRIORITY section above |
| 21 | **Skill editing system** — "edit the weather skill" leads to LLM code gen, review, apply with backup | 10-15 hours (5 phases) | Voice-controlled code modification. Full design exists at SKILL_EDITING_SYSTEM.md | Note: VS Code + Claude Code is faster in practice |
| 22 | **Automated skill generation** — Q&A, build, test, review, deploy | 15-20 hours | End-to-end skill creation by voice. Depends on skill editing system (#21) | MASTER_DESIGN.md |
| 23 | **Backup automation skill** — voice-triggered, SHA256 checksums, manifest, rotation, restore test | 6-8 hours | "Jarvis, backup the system." Automated 2 AM daily, monthly restore tests | MASTER_DESIGN.md |
| 24 | **Voice authentication for sensitive ops** — re-verify voice before threat hunting, system changes | 4-6 hours | Security layer. Speaker ID Phase 3+ | MASTER_DESIGN.md |

---

## Tier 5: Lower Priority — Backlog

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 25 | **Web dashboard** — local Flask/FastAPI web UI for JARVIS management | 10-15 hours | Demo/showoff feature. Low daily utility | TODO |
| 26 | **STT worker process** — GPU isolation via separate subprocess | 2-3 hours | Only needed if GPU conflicts resurface. Currently stable | STT_WORKER_PROCESS.md |
| 27 | ~~**Mobile access**~~ — **PROMOTED to Tier 2 as #60** (native iOS app with wake word + voice + chat UI) | — | — | See #60 in Tier 2 |
| 28 | **GitHub publishing cleanup** — CONTRIBUTING.md, INSTALLATION.md, API_KEYS.md, setup.sh | 3-4 hours | Community-facing polish. Only matters if users adopt | GITHUB_PUBLISHING_PLAN.md |
| 30 | **Multi-speaker conversation tracking** — who said what when both speak | 4-6 hours | Speaker ID Phase 3+. Requires reliable speaker identification first | MASTER_DESIGN.md |
| 48 | **Windows native port** — full JARVIS on Windows, abstraction layers for audio/desktop/notifications | 2-3 weeks | Biggest community audience. Requires platform abstractions | See `memory/plan_erica_voice_windows_port.md` |

---

## Tier 6: Aspirational — Someday/Maybe

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
| B2 | Batch extraction (Phase 4) untested | Low | Needs 25+ messages in one session to trigger |
| B7 | Google Calendar sync overwrites local reminder_time | Low | When JARVIS pushes a reminder to GCal, sync picks it back up with `useDefault` offset (10-15min), overwrites `reminder_time` to before event → fires immediately. Fix: set `reminders: {"useDefault": false}` on push, or compare event start_time instead of offset reminder_time in `_on_google_new_event`. Pre-existing, not Phase 1 related |

---

## Completed Items

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

### Tier 2 (Medium Effort)
- #8: Minimize web search latency — parallel fetches, embedding cache (Feb 19-20)
- #41: Web UI session sidebar — all 5 phases complete (Feb 20)
- #42: Document generation — PPTX/DOCX/PDF with web research + Pexels images (Feb 22)
- #45: Qwen3-VL-8B model upgrade — ROCm rebuild, self-quantized Q5_K_M, 80.2 tok/s (Feb 22)
- #59: Social introductions — butler-style greeting, PeopleManager + SQLite contacts, TTS pronunciation overrides (Feb 25)

### TOP PRIORITY
- #20 Phase 1: LLM-centric tool calling — 3 skills (time, system, filesystem) as tools, tool_executor, P4-LLM routing, 100% accuracy (600/600), 822ms avg (Feb 26)
- #20 Phase 2: API-backed + complex skills — weather, reminders, conversation (disabled), developer_tools, news. 7 tools total (6 domain + web_search), 1,200+ trials, 99.6% overall. 5-6 tool cliff DEBUNKED (Feb 26-27). get_time later removed (time/date handled by TimeInfoSkill)
- #20 Tool-connector plugin system — one-file tool definitions in `core/tools/`, auto-discovery registry, dependency injection. `tool_executor.py` 1,057→27 lines. Adding a new tool = create one file (Feb 27)

### Other Completed (non-roadmap enhancements)
- Time injection into LLM system prompts — all 5 prompt injection points, correct time-of-day greetings (Feb 27)
- Smart ack suppression — skip acknowledgements for fast/conversational queries (Feb 22)
- Doc gen prompt overhaul — prescriptive depth, publish.sh README protection (Feb 22)
- Edge case tests expanded — Phase 1E: 144 tests (Feb 22), then 236 tests (Feb 25)
- Ack speaker-to-mic bleed fix — pause listening during ack playback (Feb 23)
- Whisper brand-name corrections — "and videos"→"amd's", "in video"→"nvidia" (Feb 23)
- Preferred-mic hot-swap recovery — device monitor recovers from wrong-mic fallback (Feb 23)
- jarvis-web.service — systemd service for web UI, enabled for auto-start (Feb 23)
- WebUI health check brief mismatch — spoken vs on-screen now consistent (Feb 23)
- Task Planner — 4 phases: self-awareness, compound detection + LLM planning + execution, guardrails, advanced features (Feb 24-25)
- Task Planner bug fixes — pause/resume guards, eval timeout, skip-that, 12 new tests (Feb 25)

### Tier 3
- #40: News headline trimming — 25 per category (Feb 20)

### Tier 5
- #29: Console logging fix (Feb 19)

### Resolved Bugs
- B1: "Fullscreen" Whisper misrecognition — fixed by mic upgrade + retraining (Feb 21)
- B3: Console logging broken — fixed logger.py (Feb 19)
- B4: Topic shift threshold — already set to 0.35 (Feb 19)
- B6: Google Calendar sync token — removed `orderBy` from initial sync (Feb 19)

---

## Sources Consulted

- `docs/TODO_NEXT_SESSION.md` — current tier-based TODO
- `docs/DEVELOPMENT_VISION.md` — LLM-centric architecture plan
- `docs/SKILL_EDITING_SYSTEM.md` — full 5-phase skill editor design
- `docs/STT_WORKER_PROCESS.md` — GPU isolation architecture
- `.archive/docs/GITHUB_PUBLISHING_PLAN.md` — pre-publication plan (completed Feb 18)
- `.archive/docs/MASTER_DESIGN.md` — original comprehensive design (email, music, malware, IoT, profiles, voice auth, backup, etc.)
- `memory/plan_erica_voice_windows_port.md` — dual-model voice + Windows portability plans

---

**Total: 61 development ideas + 11 non-roadmap enhancements completed, sourced from 12+ documents across the entire project.**
