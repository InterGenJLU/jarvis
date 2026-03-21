# JARVIS Consolidated Development Inventory

**Created:** March 12, 2026 (session 256)
**Rebuilt:** March 17, 2026 (session 309 — 6 parallel agents scoured handoffs, docs, plans, code, sessions, memory)
**Validated:** Against live codebase March 17, 2026
**Method:** Exhaustive sweep of all handoff archives, documentation, plans (INCOMPLETE+COMPLETED), code TODOs, session logs, memory files, system maps, analysis, and research archives. Each item validated as IMPLEMENTED, PARTIAL, or NOT STARTED.

---

## Active Bugs (Fix Before New Features)

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| ~~B2~~ | ~~Memory recall 768/384 dimension mismatch~~ | — | **FIXED** (session 309 — dimension check skips stale embeddings) |
| ~~B3~~ | ~~"my name is X" → SocialIntroductions loop~~ | — | **FIXED** (session 309 — self-identification handled before skill routing) |
| ~~B4~~ | ~~TTS honorific split~~ | — | **FIXED** (session 309 — honorific folded into last TTS chunk) |
| B8 | FAISS index empty after 384→768 upgrade — run backfill_memory.py | Low | Open |

---

## Tier 1: Owner-Directed Priority Sequence

| # | Item | Effort | Status | Notes |
|---|------|--------|--------|-------|
| 1 | **IMAP Email via MCP** — read, search, archive by voice/web/mobile | Variable | NOT STARTED | Config stub commented out. MCP bridge ready. Primary user=Gmail, secondary user=AOL |
| 2 | **Mobile iOS App** — native app with wake word + WebRTC voice | 5-8 days | PHASE 1 DONE | Plan at `.claude/PLANS/INCOMPLETE/plan_mobile_ios_app.md`. Requires Apple Dev account |
| 3 | **CalDAV Calendar (secondary user)** — Apple Calendar integration | 4-6 hrs | PARTIAL | Code complete (`caldav_calendar.py`), disabled — waiting on app-specific password |
| 4 | **Concurrent multi-user support** — two simultaneous mobile users | 4-8 hrs | NOT STARTED | Depends on #2. Needs `--parallel 2`, per-user history, STT/TTS queuing |
| 5 | **"Onscreen please" — retroactive visual display** | 2-3 hrs | PARTIAL | Opens generated docs. Retroactive display of arbitrary artifacts NOT implemented |

---

## Tier 2: High Value, Ready to Build

| # | Item | Effort | Status | Notes |
|---|------|--------|--------|-------|
| 6 | **Face Enrollment Voice Flow** — multi-turn interactive enrollment by voice | 3-4 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/face_enrollment_voice_flow.md`. Direct P3 skill handler for voice (tool stays for web UI). Upgraded to InsightFace (session 309) |
| 7 | **Compound Query Decomposition** — "Does X? If so, what about Y?" | 4-6 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/compound_query_decomposition.md` |
| 8 | **Comparative Analysis Engine** — structured side-by-side evaluation | 6-8 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/comparative_analysis.md`. 28 papers researched. Subsumes web nav Phase 2 |
| 9 | **LLM news classification** — activate `_llm_classify()` | 2-3 hrs | DEAD CODE | Function exists at news_manager.py:420, never called |
| 10 | **Reminder snooze in P2 routing** — voice distinguish ack/snooze/query | 2-3 hrs | NOT STARTED | Backend `snooze_reminder()` exists. P2 chain doesn't distinguish voice intent |
| 11 | **Inject user facts into web search queries** | 3-4 hrs | NOT STARTED | Facts pass to LLM for response gen, NOT injected into search queries |
| 12 | **Secondary user re-enrollment** — speaker ID with ECAPA-TDNN | 15 min | NOT STARTED | Must use `HIP_VISIBLE_DEVICES=0`, 7 clips x 5s |

---

## Tier 3: Medium Value

| # | Item | Effort | Status | Notes |
|---|------|--------|--------|-------|
| 13 | **Mid-rundown interruption** — item-by-item delivery with continue/skip/stop | 4-6 hrs | NOT STARTED | `deliver_rundown()` currently blocks on single TTS call |
| 14 | **Network awareness skill** — device discovery, anomaly detection, threat alerts | 4-8 hrs | NOT STARTED | Fits cybersecurity background. Natural skill path |
| 15 | **Google Keep integration** — shared grocery/todo lists | 4-6 hrs | NOT STARTED | Shared with secondary user. Daily household utility |
| 16 | **Audio recording skill** — voice-triggered recording, date-based playback | 4-6 hrs | NOT STARTED | Meeting notes, voice memos, dictation |
| 17 | **Music control (Apple Music)** — playlist learning, volume via pactl | 6-10 hrs | NOT STARTED | Per-user playlists. Apple Music web interface finicky |
| 18 | **News skill multi-user rework** — per-user categories, expanded for secondary user | 4-6 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/project_news_skill_rework.md` |
| 19 | **Domain-specific grounding** — TMDB, USDA, openFDA, ESPN APIs | 6-10 hrs | NOT STARTED | Research complete. Prevents LLM hallucination in factual domains. Prompt-level grounding already done |
| 20 | **Flux scanner placeholder animation** — visual feedback during image gen | 3-4 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/plan_flux_scanner_placeholder.md` |
| 21 | **Habit extraction from conversation context** | 3-4 hrs | NOT STARTED | Needs full conversation context, not individual turns. LLM-path only |

---

## Tier 4: Larger Investments

| # | Item | Effort | Status | Notes |
|---|------|--------|--------|-------|
| 22 | **Skill editing system** — voice-controlled code modification | 10-15 hrs | NOT STARTED | Full design at `docs/SKILL_EDITING_SYSTEM.md`. VS Code + Claude Code faster in practice |
| 23 | **Automated skill generation** — end-to-end voice-driven skill creation | 15-20 hrs | NOT STARTED | Depends on #22 |
| 24 | **Backup automation skill** — voice-triggered, SHA256 checksums, rotation | 6-8 hrs | NOT STARTED | "Jarvis, backup the system." + automated 2 AM daily |
| 25 | **Voice authentication for sensitive ops** — re-verify before threat hunting | 4-6 hrs | NOT STARTED | Uses ECAPA-TDNN. Security layer |
| 26 | **Docker container (web UI mode)** — community deployment | 3-5 days | NOT STARTED | Lowest barrier to community adoption |
| 27 | **Windows native port** — full JARVIS on Windows | 2-3 weeks | NOT STARTED | Biggest community audience. Requires platform abstractions |
| 28 | **Usage data pipeline + CI/CD** — nightly metric extraction + regression testing | 1-2 days | NOT STARTED | Depends on #2 + #4 |
| 29 | **System upgrade (Ubuntu 24.04)** — apt upgrade, venv migration, kernel | 2-4 hrs | NOT STARTED | Plan at `.claude/PLANS/INCOMPLETE/plan_system_upgrade.md` |

---

## Tier 5: Deferred — Revisit When Conditions Met

| # | Item | Effort | Revisit When |
|---|------|--------|-------------|
| 30 | **Plan templates** — cache successful plan structures | 3-4 hrs | Repeated compound patterns observed |
| 31 | **Plan feedback** — post-execution LLM evaluation | 4-6 hrs | Per-step eval data shows recurring failures |
| 32 | **Parallel step execution** — ThreadPoolExecutor + dependency graph | 6-8 hrs | Plans exceed 4-5 steps or latency complaints |
| 33 | **4-user concurrent inference** — expand llama-server to 4 slots | Research + 2-4 hrs | Mid-2026, once 2-user data exists |
| 34 | **STT worker process** — GPU isolation via separate subprocess | 2-3 hrs | Only if GPU conflicts resurface |
| 35 | **GitHub publishing cleanup** — CONTRIBUTING.md, INSTALLATION.md, setup.sh | 3-4 hrs | Community adoption |

---

## Tier 6: Aspirational — Someday/Maybe

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 36 | **Malware analysis framework** — QEMU sandbox, VirusTotal, threat intel DB | 30-50 hrs | Professional threat hunting |
| 37 | **Tor / dark web research** — Brave Tor mode, sandboxed | 15-20 hrs | Specialized professional use |
| 38 | **Emotional context awareness** — voice frustration/distress detection | Research | Health monitoring, adaptive tone |
| 39 | **Voice cloning (Paul Bettany)** — F5-TTS evaluation | 10-20 hrs | Must be <500ms RTF. Revisit when open-source matures |
| 40 | **Proactive AI** — suggest actions from usage patterns | 10-20 hrs | Needs significant usage data first |
| 41 | **Self-modification** — JARVIS proposes own improvements | Far future | Depends on skill editing + reliable code gen |
| 42 | **Home automation / IoT** — smart home control | Hardware-dependent | Requires IoT hardware investment |
| 43 | **Collaborative threat intelligence sharing** — TLP-compliant | 10-15 hrs | Part of professional framework |
| 44 | **Pet detection in vision** — dogs/cats in webcam | 4-6 hrs | Needs different model than face recognition |
| 45 | **Model splitting across dual GPUs** — same-arch RDNA 3 | Research | Worth testing after workload stabilizes |

---

## Code-Level Future Work (from TODOs/Phase markers)

| Module | Item | Status |
|--------|------|--------|
| context_window.py | Phase 3: Background Qwen summarization of closed segments | IMPLEMENTED |
| context_window.py | Phase 4: SQLite persistence across restarts | IMPLEMENTED |
| interaction_cache.py | Phase 3: Sub-item decomposition for structured content | IMPLEMENTED |
| interaction_cache.py | Phase 5: Cross-session retrieval | IMPLEMENTED |
| task_planner.py | Phase 3: Destructive step confirmation, voice interrupts | PARTIAL |
| developer_tools | Phase 3-5: File operations, destructive confirmation, display management | IMPLEMENTED |
| conversation_router.py | Pre-LLM guard for unimplemented capabilities (email, SMS) | IMPLEMENTED (guard) |

---

## Test Gaps

| Item | Status | Notes |
|------|--------|-------|
| Skill execution tests | OPEN | Routing tested, no handler execution |
| Speaker ID live benchmarks | OPEN | Threshold 0.30, no formal accuracy benchmarks |

---

## Recently Completed (Session 309, March 17 2026)

- **Speaker ID upgrade:** Resemblyzer → SpeechBrain ECAPA-TDNN (192-dim, 0.80% EER)
- **Face Recognition upgrade:** dlib/Haar → InsightFace ArcFace (512-dim, 99.83% LFW)
- **Embeddings upgrade:** all-MiniLM-L6-v2 → nomic-embed-text-v1.5 (768-dim, RX 7600 GPU)
- **VAD upgrade:** WebRTC → Silero v6.2.1 ONNX (neural, stateful, 16% fewer errors)
- **GPU routing:** HIP_VISIBLE_DEVICES=0 — Whisper+Nomic on RX 7600, LLM isolated on 7900 XT
- **Speaker ID threshold:** Calibrated to 0.30 (SpeechBrain default 0.25, clean speech 0.6-0.7)

---

---

## Ideas / Notes (Capture for future consideration)

- **Conversational Awareness / Contextual Greeting Response** — When the user responds to a presence greeting ("good morning Jarvis"), instead of a dead-end dismissal or double-greeting, JARVIS surfaces anything relevant: critical news, pending reminders, active weather alerts, today's calendar, tracked items. If nothing notable, the window closes silently. Research complete, plan synthesized from 8 AI proposals. Plan at `.claude/PLANS/INCOMPLETE/conversational_awareness_layer.md`.

- **Kokoro TTS metrics capture for dashboard** — TTS timing (time to first chunk, RTF, audio duration, generation time) is only in journal logs, not in the metrics DB. Needs to be captured per-utterance so it appears on the dashboard alongside LLM metrics. Currently flying blind on TTS performance trends.

- **Web search metrics panel not updating** — Dashboard web search performance panel appears stale. Needs investigation — either data isn't being recorded or the panel query is broken.

- **Dashboard restructure** — Reorganize dashboard to present all meaningful metrics (LLM, TTS, web search, speaker ID, presence detection) in a cohesive layout. Currently has room for more panels.

- **Latency optimization pass** — RFP drafted and ready for submission to multiple AIs. Targets: LLM TTFT (P50 3.9s → <2s), tool-calling round-trip (8-18s → 3-5s), TTS first chunk (avg 1.66s → <0.5s), conversational shortcuts for simple exchanges. RFP at `research/latency_optimization_rfp_20260318.md`.

---

**Total: 45 open development items + 1 active bug + 2 test gaps across 6 priority tiers.**
**Sourced from: 50+ handoff archives, 15 docs, 20+ plans, 24 core modules, 15 session logs, 10 memory/system map files.**
