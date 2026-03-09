# Session 211 Handoff — March 9, 2026

## What Was Done

### Run 010 Deep Analysis — COMPLETE
Performed full root cause analysis of all 4 failures + 9 inappropriate readback offers from run 010. Reviewed git commits between runs 008-010, examined debug JSONL pipeline data, traced code paths for all issues. Analysis and recommendations saved as durable artifact at `tests/iterative_results/run_010_analysis.md`.

### Key Discovery: Debug JSONL Contamination
The `run_010_results_debug.jsonl` file contains data from **7 different test runs** due to append mode in `core/debug_logger.py:28`. Run 010's actual debug data could not be reliably isolated. This is a tooling fix (Priority 5 below).

---

## Corrections to Implement (Owner-Approved, Priority Order)

### Fix 1: Confidence Floor Bypass (C08/T3) — CODE FIX
**File:** `core/conversation_router.py` (around line 2138, after `execute_intent()`)

**Problem:** `match_intent()` sets `confidence: None` for keyword-layer matches (skill_manager.py:320). The floor check at line 2130 (`if conf is not None and conf < 0.60`) passes because `None is not None` = False. The real confidence (0.44 from `_disambiguate_suffix`) is computed during `execute_intent()` — AFTER the floor check.

**Fix:** Add a post-execution floor re-check. After `execute_intent()` returns, re-read `self.skill_manager._last_match_info["confidence"]`. If it's now a real number < 0.60, return `None` to fall through to LLM.

```python
# After the existing execute_intent() call in _handle_skill_routing:
match_info = self.skill_manager._last_match_info
final_conf = match_info.get("confidence")
if final_conf is not None and final_conf < self._SKILL_CONFIDENCE_FLOOR:
    logger.info("P4: post-exec confidence %.2f < floor %.2f — falling through",
                final_conf, self._SKILL_CONFIDENCE_FLOOR)
    return None
```

**Test:** Run C08 targeted: `python3 scripts/test_conversations.py --verbose --ids C08 --save tests/iterative_results/run_011_results.json`
C08/T3 "create a comparison document" should route to LLM (not file_editor).

---

### Fix 2: Anaphoric Tool Selection (C02, C07) — CODE FIX
**File:** `core/conversation_router.py` (tool selection/pruning logic)

**Problem:** Tool pruning selects tools based on the CURRENT utterance's keywords only. Anaphoric references ("list them", "which ones", "what's in there now") have no domain keywords, so filesystem tools (`find_files`, `developer_tools`) are excluded even when the prior turn used them. In C02, "list" even false-positives to the reminders skill keyword.

**Fix:** Add conversation-context awareness to tool selection. Track which tools were used in the prior turn. If the prior turn used filesystem/developer tools, retain them in the available toolset for the follow-up turn regardless of keyword matching.

**Key code location:** Find the tool selection method that builds the tool list (the method that returns `ALWAYS_INCLUDED_TOOLS + matched_tools` around line 2104). Add logic to include prior-turn tools.

**Test:** Run C02 and C07 targeted: `--ids C02,C07`
- C02/T2 "list them for me" should use `find_files` (not `manage_reminders`)
- C07/T4 "what's in there now" should use `find_files` or `developer_tools` (not `web_search`)

---

### Fix 3: Readback Over-Offering (9 conversations) — PROMPT REWRITE
**Files:** `core/persona.py` (Rules 14-15), `core/llm_router.py` (both synthesis paths)

**Problem:** Current split Rule 14 (when to offer) + Rule 15 (when not to offer) doesn't work because the positive trigger (list detection) overpowers the negative guard (self-check + category exclusion). The LLM fires Rule 14 first and doesn't reach Rule 15.

**Fix:** Merge Rules 14-15 into a single ordered-decision rule that puts the self-check FIRST:

```
Rule 14. READBACK DECISION — follow this in order:
(a) Check your response. Does it ALREADY list the specific items, steps, prices, names, or details the user asked for? If YES → STOP. Do not offer a readthrough. You already provided the information.
(b) Does the content require a WALKTHROUGH to be useful — step-by-step cooking instructions, assembly procedures, installation guides? If YES → give a 1-2 sentence summary (count of steps, source) and offer a readthrough. Do NOT list any items.
(c) For everything else — product comparisons, recommendations, rankings, factual answers, code, travel, general knowledge — just answer completely and stop. Never offer a readthrough.
```

**CRITICAL:** Apply identically to ALL THREE prompt injection points:
1. `persona.py` system prompt (current Rules 14-15 → merge into single Rule 14)
2. `llm_router.py` multi-tool synthesis path (current Rules 2-3 → merge)
3. `llm_router.py` single-tool synthesis path (current Rules 1-2 → merge)

Renumber subsequent rules in each location. Grep for old phrasing across `core/` to confirm no stale copies remain.

**Test:** Run the 9 affected conversations: `--ids C08,C17,C21,C22,C28,C30,C33`
All 9 readback offers should disappear.

---

### Fix 4: Web Search Failure Recovery (C19/T1) — PROMPT ADDITION
**File:** `core/llm_router.py` (both synthesis paths)

**Problem:** Three web searches on "Gardendale to Moab" returned generic calculator homepages without extractable data. The LLM kept searching instead of falling back to training knowledge. The synthesis prompt's multi-part rule ("call the next tool NOW") encouraged retry instead of synthesis.

**Fix:** Add a fallback rule to both synthesis prompt paths:

```
If you have searched the web 2+ times for the same topic and the results do not contain a clear answer, answer from your training knowledge and note that you could not find current data to confirm.
```

**Test:** Run C19 targeted: `--ids C19`
C19/T1 should produce a real answer (distance + time from training data), not an error.

---

### Fix 5: Debug JSONL Hygiene — 1-LINE CODE FIX
**File:** `core/debug_logger.py` line 28

**Change:** `self._fh = open(output_path, 'a')` → `self._fh = open(output_path, 'w')`

Each test run already gets a unique filename via `--save`, so truncation on open ensures clean per-run data.

---

## Execution Plan

1. Apply all 5 fixes
2. Restart jarvis-web: `systemctl --user restart jarvis-web`
3. Run targeted test against affected conversations:
   ```
   python3 scripts/test_conversations.py --verbose --ids C02,C07,C08,C17,C19,C21,C22,C28,C30,C33 --save tests/iterative_results/run_011_results.json > tests/iterative_results/run_011_raw_output.txt 2>&1
   ```
4. Analyze results — all 4 failures and 9 readback offers should be resolved
5. If targeted pass: run full 40-conversation suite as run 012 for regression check
6. Update MANIFEST.md after each run
7. Commit all fixes + results

---

## Uncommitted Changes (carried from session 210 + new)

### Main repo (`~/jarvis`)
**Modified (from session 210):**
- `core/conversation_router.py` — post-exec floor removed + pre-check floor 0.60
- `scripts/test_conversations.py` — connection retry loop
- `scripts/test_edge_cases.py` — UnitTestDebugLogger + SameFileError fix
- `scripts/unit_tests.sh` — help text updated

**Untracked (from session 210):**
- `memory/handoff_session202-210.md` — handoff notes
- `tests/iterative_results/UNIT_MANIFEST.md` — unit manifest
- `tests/iterative_results/run_010_*` — run 010 artifacts
- `tests/iterative_results/unit_run_001_*` — unit test artifacts

**New this session:**
- `tests/iterative_results/run_010_analysis.md` — root cause analysis artifact
- `memory/handoff_session211.md` — this handoff note

### Skills repo — 3 modified (from session 210)
- `system/web_navigation/skill.py` — threshold 0.50→0.65
- `system/file_editor/metadata.yaml` — added "open" keyword
- `system/app_launcher/skill.py` — check diff

---

## Test State
- **Conversation manifest:** CURRENT through run 010. Next = **run 011** (targeted fixes).
- **Unit manifest:** CURRENT through unit_run_001. Next = **unit_run_002** (after code fixes).
- **Unit tests:** 314/314 PASS as of unit_run_001. Code fixes in this session will require re-run.

---

## Key References
- **Run 010 analysis:** `tests/iterative_results/run_010_analysis.md`
- **Run 010 results:** `tests/iterative_results/run_010_results.json`
- **Current readback rules:** `core/persona.py` Rules 14-15 (lines 500-513), `core/llm_router.py` synthesis prompts (lines 1268-1320)
- **Confidence floor check:** `core/conversation_router.py:2130`
- **Tool selection:** `core/conversation_router.py:2104` area
- **Debug logger:** `core/debug_logger.py:28`
