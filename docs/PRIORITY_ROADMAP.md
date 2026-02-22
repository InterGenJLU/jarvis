# JARVIS Priority Development Roadmap

**Created:** February 19, 2026 (session 6)
**Updated:** February 22, 2026 (session 43 — Qwen3-VL-8B upgrade, ROCm rebuild, doc gen complete)
**Method:** Exhaustive sweep of all docs, archives, memory files, code comments, and design documents
**Ordering:** Genuine ROI for effort — difficulty/complexity vs real-world payoff

---

## Tier 0: Quick Wins — All Complete

*Nothing remaining. See Completed Items below.*

---

## Tier 1: High ROI, Low-Medium Effort — All Complete

*Nothing remaining. See Completed Items below.*

---

## Tier 2: High ROI, Medium Effort — Next Wave

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 7 | **Inject user facts into web research** — surface stored facts (location, preferences) during `stream_with_tools()` | 3-4 hours | Personalized search results ("best coffee near me" uses stored location) | Risk: history poisoning needs careful scoping |
| 9 | **Email skill (Gmail)** — voice-composed email via Gmail API + OAuth | 6-8 hours | Major productivity — compose, read, reply, search, archive by voice | Same OAuth pattern as Calendar. Full schema in MASTER_DESIGN.md |
| 10 | **Google Keep integration** — shared grocery/todo lists | 4-6 hours | Daily household utility — "add milk to the grocery list" | Shared access w/ secondary user |
| 11 | **"Onscreen please" — retroactive visual display** — buffer last raw output, display on command | 2-3 hours | Bridge voice-to-visual gap. "Show me that" after JARVIS speaks an answer | TODO |
| 12 | **Profile-aware commands (multi-user)** — "my calendar" loads correct user's data based on who spoke | 3-4 hours | Infrastructure already built (speaker ID + profiles). Just needs skill-level integration | MASTER_DESIGN.md |
| 46 | **Dual-model voice recognition** — speaker-ID routes to user-specific fine-tuned vs stock Whisper | 4-6 hours | Multi-user STT without degrading primary user's accuracy | Waiting for secondary user enrollment. See `memory/plan_erica_voice_windows_port.md` |

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

---

## Tier 4: High Effort, Transformative — Strategic Investments

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 20 | **LLM-centric architecture migration** — skills become tools, not destinations | 20-40 hours (4 phases) | Eliminates fragile semantic routing for simple skills. WAIT for Qwen 3.5-9B release | DEVELOPMENT_VISION.md |
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
| 27 | **Mobile access** — remote command via phone | 20+ hours | Entirely different tech stack | TODO |
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
- `docs/GITHUB_PUBLISHING_PLAN.md` — post-publish tasks
- `.archive/docs/MASTER_DESIGN.md` — original comprehensive design (email, music, malware, IoT, profiles, voice auth, backup, etc.)
- `memory/plan_erica_voice_windows_port.md` — dual-model voice + Windows portability plans

---

**Total: 48 development ideas + 4 bugs, sourced from 12+ documents across the entire project.**
