# Session 208 Handoff — March 8, 2026

## What Was Done

### 1. Ran unit tests — 308/314 (6 failures)
```bash
scripts/unit_tests.sh --all --verbose > /tmp/test_output.txt 2>&1
```
Full output in `/tmp/test_output.txt`.

### 2. Root cause analysis — CONFIRMED

**Root cause: Post-execution confidence floor check** (lines 2156-2169 in `conversation_router.py`, now REMOVED).

The flow that caused failures:
1. `match_intent()` finds skill via **keyword** matching → `confidence=None` → passes pre-check (0.60 floor)
2. `execute_intent()` internally calls `_try_keyword_direct_match()` — tries to match keywords to handler name suffixes (e.g., keyword `"presentation"` → handler `create_presentation`)
3. If no suffix match, falls to `_try_keyword_semantic_fallback()` which sets a **real confidence score** from embedding similarity
4. **Post-exec check** reads the NEW `_last_match_info` with confidence (e.g., 0.52) → 0.52 < 0.60 → **DISCARDS the valid result**

Why some tests PASS and others FAIL with the same keywords:
- **PASS**: keyword matches a handler suffix (e.g., "presentation" → `create_presentation`) → keyword_direct layer, confidence=None → post-exec check skipped
- **FAIL**: no handler suffix match (e.g., "write" doesn't match `write_file` because suffix check looks for `xxx_write`) → falls to keyword_semantic → real confidence set → post-exec floor catches it

### 3. Removed post-exec confidence floor — DONE
Deleted lines 2156-2169 from `conversation_router.py`. The pre-check at lines 2127-2138 still protects against low-confidence pure-semantic matches. The keyword_semantic layers have their own threshold logic that accounts for keyword count.

### 4. Started building unit test debug logger — PARTIALLY DONE
Added `UnitTestDebugLogger` class to `test_edge_cases.py` (after `TestResults` class, before `TTSStub`). This class:
- Writes JSONL events for every test (pass AND fail)
- Captures full RouteResult with match_info (layer, confidence, skill, handler, intent_id)
- Activates pipeline's `debug_logger.py` sentinel for inline routing events
- Has `log_test()` and `log_summary()` methods
- Added `import shutil` to imports

**STILL NEEDS (in order):**

#### 4a. Add `--save` argument to `parse_args()` (line ~4118)
```python
parser.add_argument("--save", type=str, help="Save results JSON + debug JSONL to this path")
```

#### 4b. Modify `run_routing_test()` (line ~1859) to return RouteResult
Currently returns `(bool, str)`. Needs to return `(bool, str, RouteResult)` so the debug logger can capture it. Change the return statements:
```python
# At end of function, change:
return False, f"...", r    # add r
return True, f"...", r     # add r
```

#### 4c. Modify `run_test()` (line ~2179) to log every test
After `results.record(case.id, passed, detail)`, add:
```python
if _unit_debug:
    route_result = extra if isinstance(extra, RouteResult) else None
    _unit_debug.log_test(case, passed, detail, route_result, duration_ms)
```
Need to capture `duration_ms` with `time.time()` around the test call. Also need to handle the 3-tuple return from `run_routing_test` vs 2-tuple from other runners.

#### 4d. Modify `main()` (line ~4141) to:
1. Initialize `_unit_debug` when `--save` is used
2. Activate sentinel at start, deactivate at end
3. Call `_unit_debug.log_summary()` after tests complete
4. Copy debug JSONL to `tests/iterative_results/` (like conversation tests do)
5. Close logger

Model after conversation test suite's approach (lines 1097-1149 in `test_conversations.py`):
```python
# In main(), after parse_args:
global _unit_debug
if args.save:
    _debug_path = args.save.replace('.json', '_debug.jsonl')
    _unit_debug = UnitTestDebugLogger(_debug_path)
    print(f"Debug logger: {_debug_path}")

# After test loop, before summary:
if _unit_debug:
    _unit_debug.log_summary(results, tier_counts, time.time() - start_time)
    _unit_debug.close()
    # Copy to iterative_results if it exists
    if os.path.exists(_debug_path):
        dest_dir = os.path.join(PROJECT_ROOT, "tests", "iterative_results")
        if os.path.isdir(dest_dir):
            shutil.copy2(_debug_path, os.path.join(dest_dir, os.path.basename(_debug_path)))
```

#### 4e. Also capture Tier 3 and Tier 4 metadata
- Tier 3 (`run_execution_test`): capture response text and match_info from `skill_manager._last_match_info`
- Tier 4 (`run_llm_test`): capture LLM response, tokens, tool calls
- Tier 1: capture test-specific data (ambient result, noise result, etc.)

### 5. Test logging gap discovered
The existing `JARVIS_LOG_FILE_ONLY=1` env var in the test harness was NOT producing usable logs — `logs/console.log` wasn't updated during test runs. The new `UnitTestDebugLogger` bypasses this by directly activating the pipeline's debug sentinel and writing its own JSONL.

## Files Modified This Session

| File | Change | Status |
|------|--------|--------|
| `core/conversation_router.py` | Removed post-exec confidence floor (lines 2156-2169) | **Done** |
| `scripts/test_edge_cases.py` | Added `UnitTestDebugLogger` class + `import shutil` | **Partial** |

## Uncommitted Changes (cumulative — 3 files from session 206 + 2 modified this session)

| File | Change |
|------|--------|
| `core/conversation_router.py` | Post-exec confidence floor REMOVED (this session) + still has pre-check floor from session 206 |
| `scripts/test_edge_cases.py` | `UnitTestDebugLogger` class added (partially wired up) |
| `/mnt/storage/jarvis/skills/system/web_navigation/skill.py` | select_result: threshold 0.50→0.65, removed 3 broad examples, return None when no results |
| `/mnt/storage/jarvis/skills/system/file_editor/metadata.yaml` | Added "open" keyword |

## The 6 Failing Tests (should be fixed by removing post-exec floor)

| Test | Input | Expected | Root Cause |
|------|-------|----------|------------|
| 5C-R1 | "write a file called test.txt" | file_editor | keyword_semantic confidence < 0.60 |
| 1E-01 | "look up top 5 LLMs...create a 7 slide PowerPoint..." | file_editor | keyword_semantic confidence < 0.60 |
| 1E-07 | "look up AI market growth...create a slide deck..." | file_editor | keyword_semantic confidence < 0.60 |
| 1E-09 | "create a PDF report comparing Python and Rust..." | file_editor | keyword_semantic confidence < 0.60 |
| 1E-16 | "research quantum computing...generate a PDF report..." | file_editor | keyword_semantic confidence < 0.60 |
| 9C-02 | "meet my friend Aiko" | social_introductions | keyword_semantic confidence < 0.60 |

## NEXT STEPS (in order — NO SKIPPING)

### Step 1: Finish unit test debug logger wiring (4b-4e above)
The `UnitTestDebugLogger` class is written but not yet connected to the test loop. Follow the detailed instructions in section 4 above.

### Step 2: Run unit tests to verify post-exec floor removal fixes the 6 failures
```bash
scripts/unit_tests.sh --all --verbose --save tests/iterative_results/unit_run_001_results.json > /tmp/test_output.txt 2>&1
```
(The `--save` flag won't work until step 1 is complete. If not ready, run without it first to verify the fix, then add logging.)

### Step 3: If unit tests pass (314/314), restart jarvis-web and run conversation tests as RUN 010
```bash
systemctl --user restart jarvis-web.service
python3 scripts/test_conversations.py --verbose --save tests/iterative_results/run_010_results.json > tests/iterative_results/run_010_raw_output.txt 2>&1
```

### Step 4: Update MANIFEST.md with run 010 results — IMMEDIATELY

### Step 5: If clean, commit all changes + publish
Commit message: "Fix 2 routing bugs (C03/C05) + add unit test debug logger"

### Step 6: Still pending from prior sessions
- Enable audio watchdog: `systemctl --user daemon-reload && systemctl --user enable --now jarvis-audio-watchdog.service`
- System upgrade (plan at `.claude/plans/plan_system_upgrade.md`)

## Key Code Locations for Debug Logger Work

| File | Lines | What |
|------|-------|------|
| `scripts/test_edge_cases.py` | ~150-260 | `UnitTestDebugLogger` class (NEW, written) |
| `scripts/test_edge_cases.py` | ~1859 | `run_routing_test()` — needs to return RouteResult |
| `scripts/test_edge_cases.py` | ~2179 | `run_test()` — needs to call `_unit_debug.log_test()` |
| `scripts/test_edge_cases.py` | ~4116 | `parse_args()` — needs `--save` |
| `scripts/test_edge_cases.py` | ~4141 | `main()` — needs logger init/teardown |
| `scripts/test_conversations.py` | 1097-1149 | Reference implementation of save/sentinel pattern |
| `core/debug_logger.py` | Full file | Pipeline debug logger (activated via sentinel) |

## Routing Architecture Reference (for future debugging)

The skill matching pipeline in `execute_intent()` (skill_manager.py:796):
1. Pending confirmations (3-tuple check)
2. `match_intent()` → keyword or semantic match → returns `(skill_name, pattern, entities)`
3. Direct semantic intent: if `pattern` is an intent_id → call handler directly
4. **4a: `_try_keyword_direct_match()`** — matches keywords to handler name suffixes. Generic keywords (`_generic_keywords` set) are SKIPPED. If exactly 1 suffix match → `keyword_direct` layer, `confidence=None`. If >1 → `_disambiguate_suffix()` with semantic similarity, `confidence=score`.
5. **4b: `_try_keyword_semantic_fallback()`** — semantic similarity within keyword-matched skill. Uses intent threshold (default 0.55). If below, tries relaxed threshold based on `_keyword_count` (3+: 0.20, 2: 0.30, 1: 0.40). Always sets `confidence=best_score`.
6. `_try_global_semantic_fallback()` — cross-skill semantic search

**`_generic_keywords`**: `{"search", "open", "find", "look", "browse", "navigate", "web", "file", "code", "directory", "count", "analyze", "amazon"}`

**Handler suffix matching quirk**: keyword `"write"` does NOT match handler `write_file` because suffix check looks for `_write` (end of name), not `write_` (start). This is why "write a file..." falls through to keyword_semantic instead of keyword_direct.
