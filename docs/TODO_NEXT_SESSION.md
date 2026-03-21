# TODO — Next Session

**Updated:** March 12, 2026 (session 256)

---

## Current State

- **11 LLM tools** active (6 domain + 5 always-included), tool-connector plugin system, MCP bridge (bidirectional)
- **18-layer priority chain** in conversation_router.py (P0 through LLM fallback)
- **Self-managing memory** — per-turn extraction + recall_memory tool (MemGPT pattern)
- **CMA 6/6** — Consolidation & Abstraction + Associative Linking complete
- **Interaction artifact cache** — 5 phases (hot/warm/cold), structured readback, delivery modes
- **Dual GPU** — RX 7600 display, RX 7900 XT compute, ctx-size 32768
- **Vision complete** (all 7 phases) — webcam, mobile camera, presence detection, face recognition. 7c NOT YET LIVE (needs face enrollment)
- **314/314 unit tests pass** (Tier 1: 122, Tier 2: 151, Tier 3: 13, Tier 4: 28)
- **62 conversation tests** (V2 suite) — 27 categories, 231 turns
- **Docs refreshed** — README v5.0.0, PROJECT_OVERVIEW rewrite (Mar 11)

---

## Owner-Directed Priority Queue

Work these in sequence (strategic direction: cross-platform utility — desktop + mobile force multipliers):

### 1. Vision 7c Live Test
**Status:** Code complete, NOT YET LIVE
**What:** Enroll faces, enable `vision.presence.enabled: true`, validate presence detection + greetings

### 2. IMAP Email via MCP
**Status:** Config stub at `config.yaml:299-318` + MCP bridge infrastructure ready
**What:** Email access for both users (primary user=Gmail, secondary user=AOL)

### 3. Mobile iOS App (#60)
**Status:** Phase 1 DONE (web UI responsive). Native iOS = 6 phases, not started
**Plan:** `memory/plan_mobile_ios_app.md`
**Prereqs:** Apple Developer account ($99/yr) + Mac with Xcode

### 4. CalDAV Calendar (Secondary User)
**Status:** BLOCKED — waiting on app-specific password
**What:** Apple Calendar integration. Full `caldav_calendar.py` exists, DB column exists, config present but `enabled: false`

### 5. Concurrent Multi-User (#61)
**Status:** NOT STARTED — depends on #60
**What:** Handle two simultaneous mobile users. Needs per-user history, STT/TTS queuing

### 6. "Onscreen Please" (#11)
**Status:** PARTIAL — opens generated docs. Retroactive visual display of arbitrary artifacts NOT implemented

---

## Open Bugs

| # | Severity | Item |
|---|----------|------|
| B2 | Low | Batch extraction (Phase 4) untested — needs 25+ messages to trigger |
| B8 | Medium | EventTTSProxy `speak()` returns None — causes reminder retry false positives. Nag cap mitigates |
| B9 | Low | Speaker ID no accuracy benchmarks — threshold 0.75, `test_speaker_id.py` exists but no live benchmarks |

---

## Test Gaps

| Item | Status | Notes |
|------|--------|-------|
| Skill execution tests | OPEN | Tier 3-4 routing tested, no actual handler execution |
| EventTTSProxy tests | OPEN | Zero tests for speak() return value / timeout behavior |
| Batch extraction tests | OPEN | Feature implemented, zero coverage |
| Speaker ID tests | **CLOSED** | `test_speaker_id.py` — 5-part suite. No live accuracy benchmarks |

---

## Pending Investigations

- **Search backend logging** — not visible in journald from web frontend
- **LLM model comparison** — Q4_K_S vs 27B Dense vs current Q3_K_M (not urgent)
- **Reboot validation** — mic retry (18s), SQLite WAL, delayed startup health check (30s)
- **Watch & Notify concept** — discussed, not yet added to roadmap

---

## Session History (Recent)

### Session 256 (Mar 12) — Memory/Roadmap Cleanup
CLAUDE.md trimmed (98→68 lines), MEMORY.md trimmed (123→111 lines), 47 old handoff files archived, full development idea inventory (64+ items validated against codebase), 20 items confirmed complete, roadmap updated (#53/#54/#25 moved to Completed, Speaker ID tests closed, CalDAV notes corrected)

### Sessions 245-255 (Mar 11-12) — Documentation, Testing & Routing Fix
README v5.0.0 rewrite, PROJECT_OVERVIEW rewrite, vision screenshots, token exposure remediation (auth_token rotated, image scrubbed from git history), run 037 complete (62/62 PASS), routing confidence bypass fix

### Sessions 205-244 (Mar 8-10) — Vision Phase 7 & Testing
Vision Phase 7a-7c (webcam, mobile camera, presence detection), web handler test suite (61 tests), mobile routing fixes A-D, HUD context %, memory extraction improvements, conversation test runs 033-036

### Sessions 155-204 (Mar 4-7) — Vision Phases 1-6 & Roadmap
Vision Phases 1-6 (multimodal LLM through thumbnails), profile-aware routing (#12), dual-model STT (#46), priority roadmap validation sweep (session 190), test coverage to 314/314

### Sessions 147-154 (Mar 3-4) — Memory, MCP, Documentation
Self-managing memory (MemGPT), CMA 6/6, MCP bridge Phases 1-2, interaction artifact cache, documentation refresh (10 docs)

### Sessions 130-146 (Mar 2-3) — Multi-User & Artifacts
Active user selection (#63), multi-user DB migration, memory dashboard, formal address, readback flow, artifact cache 5 phases, Kokoro G2P, rundown bug fixes
