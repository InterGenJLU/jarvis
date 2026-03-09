# Session 213 Handoff — March 9, 2026

## What Was Done

### Run 012 — All 10 Conversations Completed
- Same targeted IDs as run 011: C02,C07,C08,C17,C19,C21,C22,C28,C30,C33
- **10/10 completed** (vs 2/10 in run 011 before OOM crash)
- Duration: 13m 2s, 47 turns, avg 14,943ms latency, avg 66 words
- Results saved: `tests/iterative_results/run_012_results.json`, `run_012_raw_output.txt`, `run_012_results_debug.jsonl`
- MANIFEST.md updated with full run 012 entry

### Fix Validation from Run 012
1. **Fix 1 (confidence floor post-check):** ✅ CONFIRMED — C08 T3 routed to LLM+web_search, NOT file_editor@0.44
2. **Fix 2 (anaphoric carryover):** ❌ STILL NOT WORKING — gap confirmed (see below)
3. **Fix 3 (readback merge):** ✅ LIKELY WORKING — no inappropriate readback offers across all 10 conversations
4. **Fix 4 (web search fallback):** ✅ CONFIRMED — C19 T1 produced response (was error in run 010)
5. **Fix 5 (debug hygiene):** ✅ WORKING — fresh JSONL

### SentenceTransformer CPU Fix — NOT YET LIVE
- **CRITICAL FINDING:** Journal logs show `Use pytorch device_name: cuda:0` — the running process loaded BEFORE the `device='cpu'` edit was saved to `skill_manager.py:80`.
- Run 012 got lucky — take_screenshot was called once without triggering OOM cascade.
- Owner is restarting jarvis-web NOW to pick up the fix. Verify after restart with:
  ```
  journalctl --user -u jarvis-web --since "$(date '+%H:%M:%S' -d '2 min ago')" | grep -i "device_name"
  ```
  Should show `device_name: cpu` instead of `cuda:0`.

### Anaphoric Carryover Fix Applied (Option A)
- **Root cause:** `_apply_anaphoric_carryover()` only ran inside `_select_tools_for_command()` on return-tools paths. All three `return None` deferral paths skipped it. The LLM fallback in `route()` also didn't call it.
- **Fix:** Added `always_on = self._apply_anaphoric_carryover(always_on)` at `conversation_router.py:347`, in the LLM fallback path of `route()`, right after deferred tools are restored and guest/mobile filtering.
- This ensures that when `_select_tools_for_command` defers to P4 (returns None) and P4 also fails, the prior turn's domain tools (find_files, developer_tools, get_system_info) are injected into the tool list.

**The fix at line 347 in context:**
```python
# line 329-354 in route()
if not result.use_tools and not result.handled:
    always_on = list(ALWAYS_INCLUDED_TOOLS.values())
    deferred = getattr(self, '_deferred_domain_tools', None)
    if deferred:
        ...restore deferred...
    if self._is_guest:
        ...filter...
    if self._is_mobile:
        ...filter...
    always_on = self._apply_anaphoric_carryover(always_on)  # <-- NEW LINE 347
    if always_on:
        result.use_tools = always_on
        ...
```

---

## Uncommitted Changes

### Main repo (`~/jarvis`)
**Modified (same as session 212 + new):**
- `core/conversation_router.py` — **NEW:** anaphoric carryover in LLM fallback path (line 347). Previous: post-exec floor check + carryover method + carryover on 3 return paths
- `core/conversation_state.py` — `last_tools_called` field + close_window reset
- `core/persona.py` — Rules 14-15 merged into single Rule 14, renumbered
- `core/llm_router.py` — readback rules merged in both synthesis paths + web search fallback rule
- `core/debug_logger.py` — append→write mode
- `core/skill_manager.py` — SentenceTransformer device='cpu'
- `jarvis_web.py` — tool call tracking for anaphoric carryover
- `jarvis_console.py` — tool call tracking for anaphoric carryover
- `scripts/test_conversations.py` — connection retry loop
- `scripts/test_edge_cases.py` — UnitTestDebugLogger + SameFileError fix
- `scripts/unit_tests.sh` — help text updated

**Untracked:**
- `memory/handoff_session202-213.md` — handoff notes
- `tests/iterative_results/UNIT_MANIFEST.md`
- `tests/iterative_results/run_010_*`, `run_011_*`, `run_012_*`
- `tests/iterative_results/unit_run_001_*`

### Skills repo — 3 modified (unchanged from session 212)
- `system/web_navigation/skill.py` — threshold 0.50→0.65
- `system/file_editor/metadata.yaml` — added "open" keyword
- `system/app_launcher/skill.py` — check diff

---

## Next Steps (for session 214)

1. **Verify SentenceTransformer on CPU** after restart:
   ```
   journalctl --user -u jarvis-web --since "$(date '+%H:%M:%S' -d '2 min ago')" | grep -i "device_name"
   ```
   Must show `cpu`, not `cuda:0`.

2. **Run targeted C02 test** to validate anaphoric carryover fix:
   ```
   python3 scripts/test_conversations.py --verbose --ids C02 --save tests/iterative_results/run_013_results.json > tests/iterative_results/run_013_raw_output.txt 2>&1
   ```
   - T2 "list them" should now use find_files (not manage_reminders)
   - T3 "which ones are the biggest" should use find_files (not web_search)
   - T4 "delete the largest one" should use find_files (not web_search)

3. **If C02 passes:** Run full 10-conversation targeted test as run 013 (or 014 if C02-only was 013):
   ```
   python3 scripts/test_conversations.py --verbose --ids C02,C07,C08,C17,C19,C21,C22,C28,C30,C33 --save tests/iterative_results/run_013_results.json > tests/iterative_results/run_013_raw_output.txt 2>&1
   ```

4. **Unit tests** — multiple core files modified. Need unit_run_002:
   ```
   scripts/unit_tests.sh --all --verbose > /tmp/test_output.txt 2>&1
   ```

5. **Commit all fixes** after both test suites pass.

---

## Test State
- **Conversation manifest:** CURRENT through run 012. Next = **run 013**.
- **Unit manifest:** CURRENT through unit_run_001. Next = **unit_run_002**.
- **Unit tests:** 314/314 PASS as of unit_run_001. Code changes require re-run.

---

## Key References
- **Anaphoric carryover fix (Option A):** `core/conversation_router.py:347`
- **Carryover method:** `core/conversation_router.py:2116-2139`
- **Carryover eligible tools:** `find_files`, `developer_tools`, `get_system_info`
- **Tool tracking:** `jarvis_web.py:1365-1375`, `jarvis_console.py:352-358`
- **Run 012 results:** `tests/iterative_results/run_012_results.json`
- **Run 012 manifest entry:** `tests/iterative_results/MANIFEST.md` (lines 511-579)
