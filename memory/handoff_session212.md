# Session 212 Handoff — March 9, 2026

## What Was Done

### Applied 5 Fixes from Session 211 Analysis
All fixes from `tests/iterative_results/run_010_analysis.md` were implemented:

1. **Fix 1: Confidence floor bypass (C08/T3)** — CONFIRMED WORKING
   - Added post-execution floor re-check in `core/conversation_router.py:2155-2162`
   - After `execute_intent()`, re-reads `_last_match_info["confidence"]` and falls through to LLM if below 0.60
   - Journal confirmed: `P4: post-exec confidence 0.43 < floor 0.60 — falling through`

2. **Fix 2: Anaphoric tool selection (C02, C07)** — IMPLEMENTED, NOT YET VALIDATED
   - Added `last_tools_called: list` to `core/conversation_state.py:44`
   - Track tool names in `jarvis_web.py:1365-1375` and `jarvis_console.py:352-358` during tool chains
   - Added `_apply_anaphoric_carryover()` method in `core/conversation_router.py:2116-2137`
   - Applied to all 3 return paths in `_select_tools_for_command()`
   - **PROBLEM FOUND:** Carryover not triggered for C02 T2 because `_select_tools_for_command` returned `None` (deferred to P4 via non-migrated guard for `social_introductions` scoring 0.50). The `return None` deferral paths skip carryover entirely. Needs fix: either apply carryover in the LLM fallback path in `route()`, or change the deferral paths.

3. **Fix 3: Readback over-offering** — IMPLEMENTED, NOT YET VALIDATED (crash before testing)
   - Merged Rules 14-15 into single ordered-decision rule (self-check first, walkthrough second, catch-all third)
   - Applied to all 3 injection points: `persona.py:500-507`, `llm_router.py:1274-1282`, `llm_router.py:1291-1302`
   - Old Rule 16→15, old Rule 17→16 in persona.py
   - Renumbered rules in both llm_router.py synthesis paths

4. **Fix 4: Web search failure recovery (C19/T1)** — IMPLEMENTED, NOT YET VALIDATED
   - Added fallback rule to both synthesis paths in `llm_router.py`
   - Multi-tool path: Rule 3 (after readback rule)
   - Single-tool path: replaced Rule 5 with enhanced version including training knowledge fallback

5. **Fix 5: Debug JSONL hygiene** — IMPLEMENTED
   - Changed `core/debug_logger.py:28` from `open(path, 'a')` to `open(path, 'w')`

### SentenceTransformer Moved to CPU
- `core/skill_manager.py:80` — added `device='cpu'` to `SentenceTransformer('all-MiniLM-L6-v2', device='cpu')`
- Eliminates GPU VRAM contention that caused the run 011 OOM crash

### Run 011 — PARTIAL (crashed after 2 of 10 conversations)
- **Crash cause:** GPU VRAM OOM on RX 7600 (display GPU). `take_screenshot` during C02 T3 loaded vision processing onto cuda:0, evicting embedding model. Re-load at 01:11:04 doubled VRAM. By C07, soft OOM in tool pruner (caught), then hard HIP OOM at C07 T2 killed process.
- **C02 T2 still failing:** Anaphoric carryover code didn't fire because `_select_tools_for_command` returned `None` (non-migrated guard deferral). The carryover only applies to code paths that build tool lists, not the `return None` deferral paths.
- **Fix 1 confirmed working** in journal logs

---

## Uncommitted Changes

### Main repo (`~/jarvis`)
**Modified:**
- `core/conversation_router.py` — post-exec floor check + anaphoric carryover method + carryover on 3 return paths
- `core/conversation_state.py` — `last_tools_called` field + close_window reset
- `core/persona.py` — Rules 14-15 merged into single Rule 14, renumbered 16→15, 17→16
- `core/llm_router.py` — readback rules merged in both synthesis paths + web search fallback rule
- `core/debug_logger.py` — append→write mode
- `core/skill_manager.py` — SentenceTransformer device='cpu'
- `jarvis_web.py` — tool call tracking for anaphoric carryover
- `jarvis_console.py` — tool call tracking for anaphoric carryover
- `scripts/test_conversations.py` — connection retry loop (from session 210)
- `scripts/test_edge_cases.py` — UnitTestDebugLogger + SameFileError fix (from session 210)
- `scripts/unit_tests.sh` — help text updated (from session 210)

**Untracked (from previous sessions):**
- `memory/handoff_session202-211.md` — handoff notes
- `tests/iterative_results/UNIT_MANIFEST.md` — unit manifest
- `tests/iterative_results/run_010_*` — run 010 artifacts
- `tests/iterative_results/run_011_*` — run 011 artifacts (partial)
- `tests/iterative_results/unit_run_001_*` — unit test artifacts

**New this session:**
- `memory/handoff_session212.md` — this handoff
- `tests/iterative_results/run_010_analysis.md` — root cause analysis (from session 211)

### Skills repo — 3 modified (from session 210)
- `system/web_navigation/skill.py` — threshold 0.50→0.65
- `system/file_editor/metadata.yaml` — added "open" keyword
- `system/app_launcher/skill.py` — check diff

---

## Next Steps (for session 213)

1. **Fix the anaphoric carryover gap** — The `return None` deferral paths in `_select_tools_for_command()` skip carryover. Options:
   - Apply carryover in `route()` at the LLM fallback path (lines 329-352) where deferred tools are restored
   - Or change the deferral paths to return tools with carryover instead of `None`

2. **Restart jarvis-web** (SentenceTransformer CPU fix needs restart)

3. **Re-run targeted test** — same IDs as run 011:
   ```
   python3 scripts/test_conversations.py --verbose --ids C02,C07,C08,C17,C19,C21,C22,C28,C30,C33 --save tests/iterative_results/run_012_results.json > tests/iterative_results/run_012_raw_output.txt 2>&1
   ```

4. **Unit tests** — 6 files modified in core/. Need unit_run_002 before committing.

---

## Test State
- **Run 011:** PARTIAL — 2/10 conversations, crashed on GPU OOM. Results saved but incomplete.
- **Conversation manifest:** CURRENT through run 011. Next = **run 012**.
- **Unit manifest:** CURRENT through unit_run_001. Next = **unit_run_002**.
- **Unit tests:** 314/314 PASS as of unit_run_001. Code changes require re-run.

---

## Key References
- **Run 010 analysis:** `tests/iterative_results/run_010_analysis.md`
- **Run 011 results (partial):** `tests/iterative_results/run_011_results.json`
- **Run 011 raw output:** `tests/iterative_results/run_011_raw_output.txt`
- **Anaphoric carryover code:** `core/conversation_router.py:2113-2137` (method), `jarvis_web.py:1365-1375` (tracking)
- **Post-exec floor check:** `core/conversation_router.py:2155-2162`
- **Readback rules (merged):** `core/persona.py:500-507`, `core/llm_router.py:1274-1282`, `core/llm_router.py:1291-1302`
