# Session 204 Handoff — March 8, 2026

## What Was Done

1. **Restarted jarvis-web.service** — picks up the llm_router.py synthesis prompt fix from session 203.
2. **Added `--ids` flag to test_conversations.py** — enables targeted multi-conversation runs (comma-separated IDs).
3. **Ran targeted test (run 007)** — 26 conversations that had inappropriate readback offers in run 006. Results: 32 minutes, 118 turns.
4. **Analyzed results — massive improvement confirmed:**
   - Inappropriate readback offers: ~42 → 11 (**74% reduction**)
   - Appropriate readback offers: ~9 → 7 (stable)
   - Total readback offers: 51 → 18 (**65% reduction**)
   - Search narration (Rule 15): 6 instances (slightly worse rate than run 006, but run 007 was 26 convs vs 40)
5. **Committed and published** the llm_router.py fix, persona.py Rules 14-16, and --ids test flag.
6. **Split Rule 14 into two rules** — owner-directed. Rule 14 = when TO offer readback. Rule 15 = when NOT TO offer readback. Old Rules 15-16 bumped to 16-17. Applied to all three prompt locations (persona.py, both llm_router.py synthesis paths). **NOT YET COMMITTED OR TESTED.**
7. **Restarted both services** — jarvis-web.service and jarvis.service (frozen listener fixed). Both running as of 09:30 CDT.

---

## Current State

- **Working tree:** Modified (persona.py, llm_router.py — split rule changes, uncommitted)
- **Both services:** Running with the split rule changes loaded
- **Run 007 results saved:** `tests/iterative_results/run_007_raw_output.txt`, `run_007_results.json`

---

## NEXT STEPS (in order)

1. **Commit the split-rule change** (persona.py + llm_router.py)
2. **Run targeted test** of the same 26 conversations: `--ids C02,C03,C05,C08,C10,C11,C14,C15,C16,C17,C18,C21,C22,C23,C26,C28,C29,C31,C32,C33,C34,C35,C36,C37,C39,C40`
3. **Compare against run 007** — target: further reduction in the 11 remaining inappropriate readback offers
4. If improved: commit + publish
5. If search narration (Rule 16) still present: consider strengthening

---

## The 11 Remaining Inappropriate Readback Offers (from run 007)

The dominant pattern: Qwen delivers the full answer, then still asks if the user wants a readthrough. The "already provided" guard clause wasn't biting hard enough — hence the split into its own rule.

| Conv | Turn | User Question | Issue |
|------|------|--------------|-------|
| C15 | T3 | OWASP top 10 for Flask | 8-item list already fully written out |
| C16 | T3 | cron job for weekly run | Code example already provided |
| C17 | T7 | total cost estimate | Cost breakdown already provided |
| C18 | T4 | what's worth seeing along the way | 5 stops already described |
| C32 | T2 | equipment package + cost | Product/pricing already provided |
| C34 | T1 | making house smarter | General advice already provided |
| C35 | T4 | Wrangler mods for trails | Product recs already provided |
| C37 | T2 | overlanding setup | Equipment list already provided |
| C39 | T1 | concerts this summer | Event listing already provided |
| C39 | T2 | touring bands | Tour info already provided |
| C40 | T2 | rock/metal festival lineups | Festival listing already provided |

---

## Files Modified (uncommitted)

| File | Changes |
|------|---------|
| `core/persona.py` | Rule 14 split into 14 (when to offer) + 15 (when not to). Old 15→16, 16→17. |
| `core/llm_router.py` | Both synthesis paths: same split applied, rule numbers updated. |

---

## Other Actions This Session

- **ChatGPT LLM landscape research reviewed** — owner asked for analysis of Qwen competitors. Conclusion: stay on Qwen3.5, modular architecture is an advantage, no model switch justified yet. GLM-4V-9B worth watching as lightweight vision alternative.
- **System update discussion** — owner hasn't updated since project start. Recommended doing it during maintenance window, holding kernel package if it bumps (ROCm compatibility risk), pinning venv before system Python updates.

## State
- **jarvis-web.service:** Running (restarted 09:30, serving split-rule code)
- **jarvis.service:** Running (restarted 09:30, listener unfrozen)
- **Memory DB:** 26 active facts (unchanged)
