# JARVIS Priority Development Roadmap

**Created:** February 19, 2026 (session 6)
**Updated:** February 19, 2026 (session 10 — news urgency filtering completed)
**Method:** Exhaustive sweep of all docs, archives, memory files, code comments, and design documents
**Ordering:** Genuine ROI for effort — difficulty/complexity vs real-world payoff

---

## Tier 0: Quick Wins — Minutes of Work, Immediate Payoff

*All Tier 0 items completed!*

### Completed (Tier 0)
- ~~Rotate OpenWeather API key~~ — Done (Feb 19, `25b5f0a`). Updated `redact.conf` with old+new patterns
- ~~Qwen sampling params~~ — Done (Feb 19, `25b5f0a`). top_p=0.8, top_k=20 in all 6 llama.cpp payloads
- ~~Install wl-clipboard~~ — Done (Feb 19)
- ~~Enable GNOME extension~~ — Done (Feb 19, logout/login completed)
- ~~Enroll the user's voice~~ — Done (Feb 16). Secondary user has profile entry but no voice embedding yet

---

## Tier 1: High ROI, Low-Medium Effort — Do Soon

| # | Item | Effort | ROI | Source |
|---|------|--------|-----|--------|
| 3 | **Whisper retraining** — 35-40 new phrases from log analysis, wake word reinforcement, background noise samples | 2-3 hours | Fix misrecognitions (fullscreen, follow-ups), improve wake word reliability from 60%→90%+ | TODO + whisper_retraining_data.md |

### Completed (Tier 1)
- ~~Keyword routing improvements~~ — Done (Feb 18-19). All 5 skills updated per audit: system_info (12 keywords), conversation (9), news (12), reminders (11), weather (15). Generic blocklist in skill_manager.py.
- ~~Topic shift threshold tuning~~ — Already done. config.yaml has 0.35 (down from 0.45 default). Confirmed live in production logs.
- ~~News urgency filtering~~ — Done (Feb 19, `1d447c2` + `e1c4611`). max_priority param in news_manager, _detect_urgency() in skill, 9 urgency keywords, 5 semantic examples.

---

## Tier 2: High ROI, Medium Effort — Next Wave

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 7 | **Inject user facts into web research** — surface stored facts (location, preferences) during `stream_with_tools()` | 3-4 hours | Personalized search results ("best coffee near me" uses stored location) | Risk: history poisoning needs careful scoping |
| ~~8~~ | ~~**Minimize web search latency**~~ | ~~3-4 hours~~ | ~~Reduce 5-8s forced search overhead~~ | Done (Feb 19-20). Parallel page fetches (`c93670a`), embedding cache (`56f5037`), rate limit 2s→1s |
| 9 | **Email skill (Gmail)** — voice-composed email via Gmail API + OAuth | 6-8 hours | Major productivity — compose, read, reply, search, archive by voice | Same OAuth pattern as Calendar. Full schema in MASTER_DESIGN.md |
| 10 | **Google Keep integration** — shared grocery/todo lists with secondary user | 4-6 hours | Daily household utility — "add milk to the grocery list" | Shared access w/ secondary user's account |
| 11 | **"Onscreen please" — retroactive visual display** — buffer last raw output, display on command | 2-3 hours | Bridge voice→visual gap. "Show me that" after JARVIS speaks an answer | TODO |
| 12 | **Profile-aware commands (multi-user)** — "my calendar" loads the user's vs secondary user's based on who spoke | 3-4 hours | Infrastructure already built (speaker ID + profiles). Just needs skill-level integration | MASTER_DESIGN.md |
| 41 | **Web UI session sidebar** — replace infinite scroll with session-based navigation, collapsible sidebar, timestamp-gap detection | 6-8 hours | Much better UX than scrolling through all history. Session = >30min gap. See `memory/plan_web_chat_ui.md` Phase 5 | Web UI Phase 5 |

---

## Tier 3: Medium ROI, Higher Effort — When Ready

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 13 | **Audio recording skill** — voice-triggered recording, date-based playback, 6 intents | 4-6 hours | Meeting notes, voice memos, dictation | skills/personal/audio_recording/ |
| 14 | **Music control (Apple Music)** — playlist learning, volume via pactl | 6-10 hours | Entertainment integration. Apple Music web interface is finicky | Per-user playlists in MASTER_DESIGN.md |
| 15 | **Screenshot via GNOME extension** — add screenshot D-Bus method, bypass portal dialog | 2-3 hours | Developer tools "show me" integration, visual debugging | Phase 5c from desktop plan |
| 16 | **Unknown speaker / guest mode** — unknown voice → limited access, no personal data | 3-4 hours | Security + graceful handling of guests. Emergency override for voice re-enrollment | MASTER_DESIGN.md |
| 17 | **LLM news classification** — activate `_llm_classify()` in news_manager.py | 2-3 hours | Better urgency classification than keyword rules. Method already exists (reserved) | news_manager.py:378 |
| 40 | ~~**News headline trimming**~~ — DONE (Feb 20). Keep 25 most recent per category, replaces 7-day retention | ✅ | `_cleanup_old()` rewritten, `max_per_category: 25` in config | news_manager.py |
| 18 | **Bare ack as answer** — detect "yeah"/"no" as answers to JARVIS questions vs new commands | 3-4 hours | Conversational naturalness. Currently "yeah" after a question = treated as command | TODO |
| 19 | **Web query memory** — SQLite DB of last 100 web queries + results, "what did we look up?" | 3-4 hours | Some functionality in conversational memory already. Dedicated lookup is cleaner | MASTER_DESIGN.md |

---

## Tier 4: High Effort, Transformative — Strategic Investments

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 20 | **LLM-centric architecture migration** — skills become tools, not destinations | 20-40 hours (4 phases) | Eliminates fragile semantic routing for simple skills. WAIT for Qwen 3.5-9B release | DEVELOPMENT_VISION.md |
| 21 | **Skill editing system** — "edit the weather skill" → LLM code gen → review → apply with backup | 10-15 hours (5 phases) | Voice-controlled code modification. Full design exists at SKILL_EDITING_SYSTEM.md | Note: VS Code + Claude Code is faster in practice |
| 22 | **Automated skill generation** — Q&A → build → test → review → deploy | 15-20 hours | End-to-end skill creation by voice. Depends on skill editing system (#21) | MASTER_DESIGN.md |
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
| ~~29~~ | ~~**Console logging fix**~~ | ~~1-2 hours~~ | ~~Developer convenience~~ | Done (Feb 19). logger.py override now always uses console.log |
| 30 | **Multi-speaker conversation tracking** — who said what when both speak | 4-6 hours | Speaker ID Phase 3+. Requires reliable speaker identification first | MASTER_DESIGN.md |

---

## Tier 6: Aspirational — Someday/Maybe

| # | Item | Effort | ROI | Notes |
|---|------|--------|-----|-------|
| 31 | **Malware analysis framework** — QEMU sandbox, VirusTotal/Any.run, CISA reports, threat intel DB | 30-50 hours | Professional threat hunting. Build when a specific engagement needs it | MASTER_DESIGN.md |
| 32 | **Video / face recognition** — webcam → people/pets/objects, security cameras | 20-40 hours | Hardware-dependent. Qwen 3.5 vision could simplify this dramatically | MASTER_DESIGN.md + DEVELOPMENT_VISION.md |
| 33 | **Tor / dark web research** — Brave Tor mode, VPN verification, session logging, sandboxed | 15-20 hours | Specialized professional use. Safety protocols critical | MASTER_DESIGN.md |
| 34 | **Emotional context awareness** — voice-based frustration/distress/laugh detection | Research-level | Could enable health monitoring, age verification, adaptive tone | MASTER_DESIGN.md |
| 35 | **Voice cloning (Paul Bettany)** — Coqui rejected, StyleTTS2 rejected, F5-TTS worth evaluating | 10-20 hours | The dream. Must be <500ms RTF. Revisit when open-source matures | TTS_VOICE_OPTIONS.md |
| 36 | **Proactive AI** — suggest actions based on usage patterns | 10-20 hours | Needs significant usage data first. "You usually check headlines at 8am…" | MASTER_DESIGN.md |
| 37 | **Self-modification** — JARVIS proposes and implements own improvements | Far future | The ultimate goal. Depends on skill editing + reliable code gen | MASTER_DESIGN.md |
| 38 | **Home automation / IoT** — RING/NEST/SimpliSafe, smart home control | Hardware-dependent | Requires IoT hardware investment. Tied to video/camera work | MASTER_DESIGN.md |
| 39 | **Collaborative threat intelligence sharing** — TLP-compliant data sharing | 10-15 hours | Part of professional framework. Depends on malware analysis (#31) | MASTER_DESIGN.md |

---

## Active Bugs / Loose Ends

| # | Item | Severity | Notes |
|---|------|----------|-------|
| ~~B1~~ | ~~"Fullscreen" Whisper misrecognition~~ | ~~Resolved~~ | Fixed by mic upgrade (FIFINE K669B). No longer reproduces |
| B2 | Batch extraction (Phase 4) untested | Low | Needs 25+ messages in one session to trigger |
| ~~B3~~ | ~~Console logging broken~~ | ~~Resolved~~ | Fixed (Feb 19, logger.py). Was writing to jarvis.log instead of console.log |
| ~~B4~~ | ~~Topic shift threshold~~ | ~~Resolved~~ | Already set to 0.35 in config.yaml, confirmed in production |
| B5 | `_in_development/web_navigation/skill.py` has TODO: load prefs from YAML | None (archived prototype) | Not in production code |
| ~~B6~~ | ~~Google Calendar sync token not saving~~ | ~~Resolved~~ | `orderBy` in initial sync prevented `nextSyncToken`. Fixed (Feb 19). Also: always restart JARVIS after DB migration commits |

---

## Sources Consulted

- `docs/TODO_NEXT_SESSION.md` — current tier-based TODO
- `docs/DEVELOPMENT_VISION.md` — LLM-centric architecture plan
- `docs/SKILL_EDITING_SYSTEM.md` — full 5-phase skill editor design
- `docs/STT_WORKER_PROCESS.md` — GPU isolation architecture
- `docs/GITHUB_PUBLISHING_PLAN.md` — post-publish tasks
- `.archive/docs/MASTER_DESIGN.md` — original comprehensive design (email, music, malware, IoT, profiles, voice auth, backup, etc.)
- `memory/keyword_routing_audit.md` — per-skill keyword gap analysis
- `memory/whisper_retraining_data.md` — log analysis + recording plan
- `memory/plan_desktop_integration.md` — Phase 5c screenshot item
- `memory/pytorch_styletts_migration.md` — TTS alternatives (F5-TTS)
- `memory/kokoro_tts_testing.md` — TTS evaluation history
- `core/news_manager.py` — reserved `_llm_classify()` method
- `skills/system/_in_development/web_navigation/skill.py` — only code TODO found
- All `core/*.py`, `*.py`, `skills/**/*.py`, `scripts/**` — searched for TODO/FIXME/HACK/XXX (codebase is extremely clean)

---

**Total: 40 active development ideas + 5 bugs, sourced from 12+ documents across the entire project.**
