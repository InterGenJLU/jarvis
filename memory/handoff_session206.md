# Session 206 Handoff — March 8, 2026

## What Was Done

1. **Validated next steps from session 205** — eliminated unnecessary work:
   - Run 010 (full retest): **SKIP** — iterative runs 005→009 already validated readback fixes
   - Rule 16 strengthening: **SKIP** — speculative, no evidence it's needed
   - C02 T2 routing: **Already fixed** (cross-session cleanup in `25f5b1e`)
   - C07 T4 routing: **Already fixed** (web search guard in `d11a5da`)
   - C03 T3 and C05 T3: **Still broken** — fixed this session (see below)

2. **Fixed C03 T3: "now open it" → app_launcher (should be file_editor)**
   - Root cause: THREE interacting issues:
     a. `match_intent` returns keyword layer (confidence=null) → floor check bypassed
     b. `execute_intent` takes different path (keyword_semantic_relaxed at 0.52) → lower confidence never checked
     c. file_editor lacked "open" keyword → never competed with app_launcher
   - Fix A: **Post-execution confidence floor check** in `conversation_router.py:2152-2163` — after `execute_intent`, re-check `_last_match_info.confidence` against floor (0.60). Catches match/execute divergence.
   - Fix B: **Added "open" to file_editor keywords** in `metadata.yaml` — creates keyword tie with app_launcher → falls to semantic matching → file_editor's "open it" intent wins at 0.85 confidence.
   - Result: "now open it" → `skill:file_editor | conf:0.85` → "Opening top_languages_2026.pptx, sir."

3. **Fixed C05 T3: "tell me more about the first one" → web_navigation (should be LLM)**
   - Root cause: web_navigation's `select_result` intent had overly broad examples ("the second one", "number three") and permissive threshold (0.50). "tell me more about the first one" matched at 0.67.
   - Fix A: **Raised select_result threshold** from 0.50 → 0.65 and removed bare ordinal examples ("the second one", "number three", "the first video").
   - Fix B: **select_result returns None when no results** — instead of "I'm still loading..." the skill declines, letting LLM handle with conversation context.
   - Result: "tell me more about the first one" → `llm:Qwen3.5-35B-A3B-Q3_K_M` → detailed CVE breakdown via web_search.

4. **Confirmed speaker ID working** — logs show consistent identification at desk (0.78-0.83), correctly declining at distance (0.38-0.50). Not broken, distance-dependent as expected.

## Files Modified

| File | Change |
|------|--------|
| `core/conversation_router.py` | Post-execution confidence floor check (lines ~2152-2163) |
| `/mnt/storage/jarvis/skills/system/web_navigation/skill.py` | select_result: threshold 0.50→0.65, removed 3 broad examples, return None when no results |
| `/mnt/storage/jarvis/skills/system/file_editor/metadata.yaml` | Added "open" keyword |

## NOT YET COMMITTED — MUST TEST FIRST

### Immediate Action on Next Session
Run full test suite to check for regressions, then commit if clean:
```bash
cd ~/jarvis && python3 -m pytest tests/ --verbose > /tmp/test_output.txt 2>&1
```
Then read `/tmp/test_output.txt`. If all 314 pass, commit all three changes with message like:
```
Fix 2 routing bugs: C03 open→file_editor, C05 ordinal→LLM fallback
```
Then publish: `./scripts/publish/publish.sh --auto`

### What to Watch For in Tests
- **Tier 3 skill tests** — app_launcher and file_editor tests may be affected by keyword/threshold changes
- **web_navigation tests** — select_result threshold change could affect existing tests
- Any test that checks skill routing confidence values

## Still Pending (from session 205)

1. **Enable audio watchdog** — `systemctl --user daemon-reload && systemctl --user enable --now jarvis-audio-watchdog.service`
2. **System upgrade** — Phase 1 apt upgrade (plan in `.claude/plans/plan_system_upgrade.md`)

## Updated Routing Bug Status

| Bug | Status |
|-----|--------|
| C02 T2 | Fixed (session ~194) |
| C03 T3 | **Fixed this session** — pending commit |
| C05 T3 | **Fixed this session** — pending commit |
| C07 T4 | Fixed (session ~194) |

All 4 routing bugs from session 202 are now resolved.
