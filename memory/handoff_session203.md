# Session 203 Handoff — March 8, 2026

## What Was Done

System prompt refinement for content quality issues identified in run 005. Three new/modified rules in `core/persona.py`, plus critical fix to conflicting synthesis prompts in `core/llm_router.py`. Run 006 completed (40/40, 174 turns). Qwen model research completed. Voice service listener freeze diagnosed.

---

## Prompt Changes Made

### 1. Rule 14 Rewritten (persona.py:500-508) — Readback Offer Scope
**Old:** Triggered on "recipe, instructions, how-to, or steps" — Qwen over-generalized to ALL responses.
**New:** Readback offer ONLY for long-form sequential content (lists with 5+ items, recipes, assembly, setup guides). Explicit exclusion list: factual answers, product comparisons, code examples, rankings, conversions, general knowledge. Guard clause: NEVER offer readback if full answer already provided.

### 2. Rule 15 Added (persona.py:509-513) — Hide Internal Search Process
DO NOT narrate search retries, quality, or attribution. No "the search results didn't yield", "based on the search results", "let me try a more specific query". Present information as though you simply know it.

### 3. Rule 16 Added (persona.py:514-515) — Memory Relevance Gate
ONLY cite recalled memory when it DIRECTLY informs the answer. No shoehorning.

### 4. CRITICAL FIX: llm_router.py Synthesis Prompts (lines 1274-1280, 1287-1298)
**Root cause of Rule 14 failure in run 006.** The synthesis prompts in `llm_router.py:continue_after_tool_call()` had HARDCODED OLD Rule 14 text that overrode the system prompt on every web search response. Both the multi-tool and single-tool synthesis paths told Qwen: "ASK 'Would you like me to read through it all for you?'" on ANY recipe/instructions/how-to/steps query — with NO exclusions.

**Fixed both paths** to match the new Rule 14 from persona.py. Also added Rule 15 reinforcement to the single-tool synthesis path: "YOU MUST present information as though you simply know it — DO NOT reference 'search results'."

**THIS FIX HAS NOT BEEN TESTED YET.** Run 006 was done BEFORE this fix. Next step: targeted test of the 42 conversations that had inappropriate readback offers.

---

## Run 006 Results (WITH persona.py changes, WITHOUT llm_router.py fix)

- **40/40 conversations**, 174 turns, 38 minutes
- **Readback offers:** 51 total, ~42 inappropriate — **virtually no improvement** (now explained by the conflicting synthesis prompts)
- **Search narration:** 10 instances (down from 17 in run 005) — **41% reduction, Rule 15 partially working**
- **Memory shoehorning:** 0 clear instances (down from 1) — **Rule 16 working**
- **Hallucinations:** C30 T2 Power of the Dog attribution FIXED. C29 T4 improved.
- **Error responses:** 4 (up from 2) — C17 T7, C18 T2, C19 T3 new failures on math/planning queries
- **Results saved:** `tests/iterative_results/run_006_results.json`, `run_006_raw_output.txt`

---

## Targeted Test Plan (NEXT STEP)

Run ONLY the conversations that had inappropriate readback offers in run 006. These are the conversations to test:

**Conversations with inappropriate readback offers (run 006):**
C02, C03, C05, C08, C10, C11, C14, C15, C16, C17, C18, C21, C22, C23, C26, C28, C29, C31, C32, C33, C34, C35, C36, C37, C39, C40

That's 26 unique conversations containing the 42 inappropriate readback offers. Run with `--conversations` flag if available, or run the full suite.

**What to measure:**
1. Count of readback offers — should drop dramatically
2. Any "based on the search results" preambles — should also drop (Rule 15 reinforced in synthesis path)
3. Any new regressions — recipes/setup guides should still get readback offers

---

## Voice Service Issue

**Symptom:** User spoke "Jarvis" multiple times, no wake beep heard. VAD never triggered.
**Diagnosis:** Service had been running 26+ hours. Last activity at 08:16:43. No log output at all after that — listener appears frozen (stale audio thread or hung sounddevice callback).
**Fix:** Restart `jarvis.service` — DO THIS AFTER THE TARGETED TEST COMPLETES (test hits jarvis-web, not voice, but both use llama-server).
**Future:** Build audio watchdog service (see MEMORY.md "Planned: Audio Watchdog Service").

---

## Qwen Model Research (Completed, No Action Yet)

### Current: Qwen3.5-35B-A3B @ Q3_K_M (16 GB, MoE, 3B active)

### Option A: Q4_K_S of same model (already on disk at /mnt/models/llm/)
- 19 GB, tight fit on 20 GB RX 7900 XT — may need 32K context or --parallel 1
- Same speed, better per-token quality

### Option B: Qwen3.5-27B Dense @ Q4_K_M (17 GB download)
- Beats 35B-A3B on EVERY quality benchmark (IFEval 95.0 vs 91.9, MMLU-Pro 86.1 vs 85.3)
- 3-5x slower (27B active params vs 3B)
- Fits comfortably on RX 7900 XT at Q4_K_M with room for 65K context

### Not Viable
- 122B-A10B: doesn't fit at any usable quant
- Dual-GPU: ROCm only supports layer splitting, PCIe overhead negates gains

### Decision: Deferred
Iron out prompt/routing issues on current model first, then run comparison tests with the conversation test suite.

---

## Files Modified This Session
| File | Changes |
|------|---------|
| `core/persona.py` | Rule 14 rewritten, Rules 15-16 added (system_prompt) |
| `core/llm_router.py` | Both synthesis prompts updated (continue_after_tool_call) — old Rule 14 removed, new Rule 14 + Rule 15 reinforcement added |

## Commits This Session
*None yet — changes not committed. Commit after targeted test validates the llm_router.py fix.*

## State
- **jarvis-web.service:** Running but serving OLD code (needs restart to pick up llm_router.py fix)
- **jarvis.service:** Running but listener frozen — needs restart
- **Memory DB:** 26 active facts (unchanged)
- **Working tree:** Modified (persona.py, llm_router.py)
- **Run 006 results preserved** at `tests/iterative_results/run_006_*`
