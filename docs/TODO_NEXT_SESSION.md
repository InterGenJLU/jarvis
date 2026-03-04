# TODO — Next Session

**Updated:** March 4, 2026 (session 154)

---

## Current State

- **8 LLM tools** active (7 domain + web_search), tool-connector plugin system, MCP bridge (bidirectional)
- **16-layer priority chain** in conversation_router.py (P0 through LLM fallback)
- **Self-managing memory** — per-turn extraction + recall_memory tool (MemGPT pattern)
- **CMA 6/6** — Consolidation & Abstraction + Associative Linking complete
- **Interaction artifact cache** — 5 phases (hot/warm/cold), structured readback, delivery modes
- **Dual GPU** — RX 7600 display, RX 7900 XT compute, ctx-size 32768
- **Session 152 codebase audit** — verified outstanding items at `memory/verified_outstanding_items.md`
- **Session 153-154** — documentation refresh (10 docs updated)

---

## Owner-Directed Priority Queue

Work these in sequence (from `verified_outstanding_items.md`):

### 1. Profile-Aware Skill Routing (#12)
**Status:** PARTIAL — speaker ID works, skills ignore `current_user`
**What:** Skills need to check who's speaking and load the correct user's data (calendar, reminders, preferences)

### 2. Mobile iOS App (#60)
**Status:** NOT STARTED — plan exists (6 phases)
**Plan:** `memory/plan_mobile_ios_app.md`
**Prereqs:** Apple Developer account ($99/yr) + Mac with Xcode

### 3. Secondary User Integration
- **CalDAV calendar** — DB column exists (`caldav_event_id`), zero CalDAV code. Blocked on credentials
- **Dual-model STT (#46)** — single model only, enrollment infrastructure ready
- **Concurrent multi-user (#61)** — single global `current_user`, no session isolation

### 4. "Onscreen Please" (#11)
**Status:** NOT STARTED
**What:** Buffer last raw output, display on command — bridge voice-to-visual gap

### 5. IMAP Email via MCP
**Status:** Config stub at `config.yaml:299-318` only. MCP bridge infrastructure done
**What:** Email access for both users (the user=Gmail, secondary=AOL)

### 6. Vision Phase 3 Wiring
**Status:** mmproj model downloaded, zero code for image input in console/web/LLM router
**What:** Activate mmproj, add image input paths

---

## Open Bug

| # | Severity | Item |
|---|----------|------|
| B2 | Low | Batch extraction (Phase 4) untested — needs 25+ messages to trigger |

---

## Test Gaps

- Routing integration tests — exist but no adversarial/conflict coverage
- Web UI automation — zero automated tests for WebSocket flows
- Skill execution tests — edge case suite has no skill handler execution tests

---

## Session History (Recent)

### Sessions 153-154 (Mar 4) — Documentation Refresh
Updated 10 documents: CHANGELOG, README, CLAUDE.md, codebase_cheat_sheet, PRIORITY_ROADMAP, DEVELOPMENT_VISION, TODO_NEXT_SESSION, priority_development_roadmap (memory), PROJECT_OVERVIEW

### Session 152 (Mar 4) — Codebase Audit
5-agent sweep + manual spot-checks. Verified all outstanding items. Ground truth: `memory/verified_outstanding_items.md`

### Session 151 (Mar 3) — MCP Bridge Phase 2
Inbound MCP client consuming external servers as native tools

### Session 150 (Mar 3) — MCP Bridge Phase 1
Outbound MCP server exposing 7 native tools

### Sessions 147-149 (Mar 3) — Self-Managing Memory + CMA 6/6
Per-turn extraction, recall_memory tool, consolidation & abstraction, associative linking

### Sessions 140-146 (Mar 3) — Artifact Cache + Readback + Delivery Modes
5-phase interaction artifact cache, structured readback, delivery modes, tool artifact wiring, Kokoro G2P

### Sessions 130-139 (Mar 2) — Multi-User + Memory Dashboard
Active user selection (#63), multi-user DB migration, memory dashboard, formal address, readback flow, rundown bug fixes, reminder staleness guard

### Sessions 115-120 (Mar 1-2) — Calendar Fixes + Unified Awareness
Calendar multi-notification support, notification loop fix, unified awareness layer, Phase 4 routing evaluation resolved

### Sessions 105-114 (Feb 28 - Mar 1) — Dual GPU + 32K Context
RX 7600 display offload, ctx-size 7168→32768, context enrichment, doc gen fix
