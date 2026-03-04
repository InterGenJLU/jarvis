# Verified Outstanding Development Items
**Last verified:** March 4, 2026 (Session 152)
**Method:** 5-agent codebase sweep + manual spot-checks against actual code

---

## PRIORITY TIER 1 — Directed Sequence (the user's Order)

| # | Item | Effort | Status |
|---|------|--------|--------|
| ~~63~~ | ~~Active user selection (web UI)~~ | ~~1-2h~~ | **DONE** — both console + web |
| 12 | Profile-aware skill routing | 3-4h | PARTIAL — speaker ID works, skills ignore `current_user` |
| 60 | Mobile iOS app | 5-8 days | NOT STARTED — plan exists (6 phases) in `memory/plan_mobile_ios_app.md` |
| — | CalDAV calendar (secondary user) | 4-6h | DB column exists (`caldav_event_id`), zero CalDAV code. Blocked on credentials |
| 46 | Dual-model STT (secondary user voice) | 4-6h | Single model only. Enrollment infrastructure ready |
| 61 | Concurrent multi-user support | 4-8h | Single global `current_user`, no session isolation |
| 11 | "Onscreen please" — retroactive display | 2-3h | NOT STARTED |

---

## PRIORITY TIER 2 — High Value, Ready to Build

| # | Item | Effort | Status |
|---|------|--------|--------|
| 17 | LLM news classification | 2-3h | Dead code — `_llm_classify()` at `news_manager.py:393` never called |
| 44 | Reminder snooze in P2 chain | 2-3h | Ack works, zero snooze references in conversation_router.py |
| 54 | Reduce aplay 150ms sleep | 1-2h | Still `time.sleep(0.15)` at `tts.py:387` |
| 7 | Inject user facts into web search | 3-4h | Memory context passed to LLM for response gen, NOT injected into search queries |
| — | IMAP email via MCP | Variable | Config stub at `config.yaml:299-318` only. MCP bridge infrastructure done. |
| — | Vision Phase 3 wiring | 1-2 days | mmproj model downloaded, zero code for image input in console/web/LLM router |

---

## PRIORITY TIER 3 — Medium Value

| # | Item | Effort | Status |
|---|------|--------|--------|
| 43 | Mid-rundown interruption | 4-6h | Single blocking `tts.speak()` in `deliver_rundown()` |
| 49 | Document refinement follow-ups | 3-4h | `_generate_structure()` makes fresh LLM call every time, no cache |
| 53 | Merge ack + response audio | 3-4h | Separate aplay subprocesses |
| 51 | Vision/OCR — Phase 1 Tesseract | 1-2 days | NOT STARTED |
| 52 | Vision/OCR — Phase 2-3 Qwen3-VL | 3-5 days | NOT STARTED |
| 55 | Network awareness skill | 4-8h | NOT STARTED |
| 50 | AI image generation (FLUX) | 4-6h | Research complete, NOT STARTED |
| 10 | Google Keep integration | 4-6h | NOT STARTED |
| 13 | Audio recording skill | 4-6h | NOT STARTED |
| 14 | Music control (Apple Music) | 6-10h | NOT STARTED |
| 15 | Screenshot via GNOME extension | 2-3h | NOT STARTED |
| 16 | Unknown speaker / guest mode | 3-4h | PARTIAL — speaker ID returns None, no distinct guest behavior |
| 30 | Multi-speaker conversation tracking | 4-6h | Basic ID per turn, no persistent "who said what" history |

---

## PRIORITY TIER 4 — Larger Investments

| # | Item | Effort | Status |
|---|------|--------|--------|
| 21 | Skill editing system | 10-15h | NOT STARTED — full design at `docs/SKILL_EDITING_SYSTEM.md` |
| 22 | Automated skill generation | 15-20h | NOT STARTED — depends on #21 |
| 23 | Backup automation skill | 6-8h | Shell script at `tools/backup_jarvis.sh`, no voice skill |
| 24 | Voice authentication for sensitive ops | 4-6h | NOT STARTED |
| 25 | Web dashboard | 10-15h | NOT STARTED |
| 47 | Docker container (web UI mode) | 3-5 days | NOT STARTED, no Dockerfile |
| 48 | Windows native port | 2-3 weeks | NOT STARTED |
| 62 | Usage data pipeline + CI/CD | 1-2 days | Metrics tracker records to SQLite, no extraction/reporting |

---

## PRIORITY TIER 5 — Deferred (Revisit When Conditions Met)

| # | Item | Revisit When |
|---|------|-------------|
| 56 | Plan templates | Repeated identical compound requests observed |
| 57 | Plan feedback | Per-step eval data shows recurring failures |
| 58 | Parallel step execution | Plans exceed 4-5 steps or latency complaints |
| 64 | 4-user concurrent inference | Mid-2026, once 2-user data exists |
| 26 | STT worker process | Only if GPU conflicts resurface |

---

## PRIORITY TIER 6 — Aspirational

| # | Item | Effort |
|---|------|--------|
| 31 | Malware analysis framework | 30-50h |
| 32 | Video / face recognition | 20-40h |
| 33 | Tor / dark web research | 15-20h |
| 34 | Emotional context awareness | Research-level |
| 35 | Voice cloning (Paul Bettany) | 10-20h |
| 36 | Proactive AI | 10-20h |
| 37 | Self-modification | Far future |
| 38 | Home automation / IoT | Hardware-dependent |
| 39 | Collaborative threat intelligence | 10-15h |

---

## OPEN BUGS

| # | Severity | Item |
|---|----------|------|
| B2 | Low | Batch extraction (Phase 4) untested — needs 25+ messages to trigger |

---

## TEST GAPS

| Item | Notes |
|------|-------|
| Routing integration tests | Exist (`test_router.py`, 9 functions) but no adversarial/conflict coverage |
| Web UI automation | Zero automated tests for WebSocket flows |
| Tier 3 skill execution tests | Edge case suite has no skill handler execution tests |

---

## ITEMS VERIFIED COMPLETE (removed from outstanding)

These were confirmed done via codebase inspection on March 4, 2026:

- Tool Artifact Wiring — ALL 7 tools (centralized in `pipeline.py:1152-1192` + `interaction_cache.py:1833-1873`)
- Context Window Phase 3 — background Qwen summarization (`context_window.py:610-663`)
- Context Window Phase 4 — SQLite persistence (`context_window.py:385-549`)
- Bug B7 — Calendar sync reminder_time overwrite (`google_calendar.py:545-565`)
- Speech Chunker Regex — intentional design, `flush()` handles end-of-stream
- Web Query Memory (#19) — recall handler + artifact cache Phase 5 makes it redundant
- Bare Ack as Answer (#18) — P2.8 handler with `jarvis_asked_question` context
- Memory _pending_forget Phase 6 — full confirm/cancel at P2.5
- Routing Integration Tests — 9 functions in `test_router.py`
- Active User Selection (#63) — console `--user` flag + web UI `<select>` + WebSocket `set_user`

---

## NOTES

- Priority chain is now **16 layers** (P0, P1, P1.5, P2, P2.5, P2.6, P2.7, P2.8, P3.1, P3, P3.5, P3.7, Pre-P4, P4-LLM, P4, P5, LLM fallback)
- Documentation lags behind implementation in several areas — doc update session needed
- NEVER remove items from this list without owner approval
