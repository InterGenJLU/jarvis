# Grading System Overhaul — Session 276, March 13 2026

## Problem Statement

The test grading system (implemented session 274) had a critical design flaw: the auto-"sir" honorific check was applied as a pass/fail gate on every the user turn. Since Qwen3.5 omits "sir" in ~50% of free-form responses, this single check caused **103 of 118 turn failures** in Run 044, completely masking the ~13 actual routing/tool/content bugs. The test suite reported 16/62 PASS (26%) when the functional pass rate was actually ~85%.

Additionally:
- Several explicit checks failed on correct JARVIS behavior (false negatives)
- ~30 conversations had NO explicit checks beyond auto-checks, meaning routing and content bugs in knowledge/research conversations went undetected
- The secondary user honorific flow (formal greetings/farewells, informal mid-conversation) had checks that didn't match the original design intent

## What Was Changed

### 1. Honorific Separation (grade_turn architecture)

**Before:** Auto "sir" check was type `"contains"` — indistinguishable from functional checks, auto-failed every turn missing "sir".

**After:**
- Auto "sir" and auto "ma'am" checks are now type `"auto_sir"` / `"auto_mum"`, tagged with category `"honorific"`
- All verdicts are now 3-tuples: `(passed, description, category)` where category is `"functional"` or `"honorific"`
- **Turn grade is computed from functional verdicts only** — honorific failures don't affect PASS/FAIL
- Honorific compliance is tracked and reported as a separate metric in the analysis summary
- Display shows honorific failures with `⚠` warning icon, not `✗` failure icon

**Key distinction:** Explicit `_has("ma'am")` and `_has("ms. erica")` checks in secondary user conversation definitions remain **functional** — these are intentional persona requirements, not auto-checks. Only the auto-injected "sir"/"ma'am" checks are categorized as honorific.

### 2. Fixed False-Negative Checks

#### V56:T3 — "cancel the one about oil changes"
- **Old check:** `_uses_tool('manage_reminders')` — failed because `skill:reminders` handled cancellation internally
- **New check:** `_has("cancel", "confirms cancellation")` — verifies the response confirms the reminder was cancelled, regardless of whether the tool or skill handled it
- **Rationale:** In R023 (first V2 run), this turn was a genuine FAIL — fuzzy matching didn't work. By R044, `skill:reminders` handles it correctly. The check should verify the outcome, not the implementation path.

#### V31:T3 — "do you have access to my calendar"
- **Old check:** `_has("not", "calendar not available")`
- **New check:** `_any_of("not", "don't", "no ", "can't", desc="calendar not available")`
- **Rationale:** JARVIS responds with natural language like "I don't have access to your calendar yet" — the word "not" may not appear literally even though the meaning is clearly "not available". The `_any_of` helper accepts natural phrasing variations.

#### V60:T4 — "email that to me"
- **Old check:** `_has("not", "email not available")`
- **New check:** `_any_of("not", "can't", "don't", "unable", desc="email not available")`
- **Rationale:** Same as V31:T3. R044 response was "I can't do that just yet, sir" — correct behavior, but "not" wasn't literally present. R023 had the opposite problem — JARVIS claimed it could send email, which is wrong.

#### V46:T4 — "thank you, that's all" (secondary user farewell)
- **No change.** The `_has("ms. erica", "formal farewell")` check is **correct per original design intent**.
- **Owner confirmed:** Greetings and farewells always use the formal honorific "Ms. Guest". Mid-conversation uses "ma'am". The R023 analysis graded T4 as PASS with just "ma'am", but that was the reviewer being lenient — the turn note explicitly says "formal: Ms. Guest farewell".

### 3. New Check Helper

Added `_any_of(*texts, desc="")` — response must contain at least one of the given texts (case-insensitive). Implemented as check type `"any_of"` with `|`-separated values. Used for flexible content verification where multiple phrasings are acceptable.

### 4. Meaningful Checks Added to Knowledge Conversations

Previously, V10-V29 and V49-V53 had NO explicit checks — only auto-checks (sir, non-empty, no filler). These are knowledge, research, and math conversations where routing and content quality matter. Added lightweight content checks to verify:

- **On-topic response** via keyword presence (e.g., V10:T1 must contain "tls"/"handshake"/"certificate")
- **Minimum substance** via `_min_words()` for deep-dive answers
- **Correct tool usage** where expected (e.g., V26:T2 "look that up" should trigger web_search)
- **Math accuracy** via expected values (e.g., V52:T1 must contain "2 1/4" or "2.25")
- **Domain-appropriate content** (e.g., V12:T3 vet advice should mention "vet" or "avoid" for safety)

Checks are intentionally lightweight — they verify the response is on-topic and has substance, not that a 100-word explanation is factually perfect. The R023 golden reference was used to determine what "correct" looks like for each conversation.

**Conversations with new checks:** V05, V10, V11, V12, V13, V14, V15, V16, V17, V18, V19, V20, V21, V22, V23, V24, V25, V26, V27, V28, V29, V49, V50, V51, V52, V53

**Total explicit checks:** 183 (up from ~50 before)

## Results: Old Grading vs New Grading

### Conversation-Level Summary

| Metric | Run 043 OLD | Run 043 NEW | Run 044 OLD | Run 044 NEW |
|--------|-------------|-------------|-------------|-------------|
| PASS | 15/62 (24%) | 51/62 (82%) | 16/62 (26%) | 53/62 (85%) |
| MIXED | 29 | 10 | 29 | 8 |
| FAIL | 18 | 1 | 17 | 1 |
| Turn pass rate | 107/231 (46%) | 215/231 (93%) | 114/231 (49%) | 218/231 (94%) |
| Honorific compliance | (mixed in) | 111/221 (50%) | (mixed in) | 118/221 (53%) |

### Conversations Still Failing (Real Bugs)

| Conv | R043 | R044 | Root Cause |
|------|------|------|------------|
| V02 Anaphoric Chain | MIXED 2/4 | MIXED 3/4 | T2: "list them" routes to reminders instead of filesystem |
| V40 Site-Specific Searches | MIXED 1/3 | MIXED 1/3 | T2-T3: Amazon/Wikipedia go to LLM, not web_navigation |
| V43 Code Directory Analysis | MIXED 2/3 | PASS | R043 filler issue, R044 clean |
| V44 Git & Dev Tools | MIXED 3/4 | MIXED 3/4 | T4: "search codebase" uses web_search not developer_tools |
| V46 Secondary User Basic | MIXED 2/4 | MIXED 2/4 | T3: missing "ma'am", T4: missing "Ms. Guest" farewell |
| V47 Secondary User Task Request | FAIL 0/3 | FAIL 0/3 | All turns missing "ma'am" (functional persona check) |
| V48 User Switch | MIXED 3/4 | MIXED 3/4 | T2: secondary user turn missing "ma'am" |
| V52 Recipe Scaling | MIXED 3/4 | MIXED 3/4 | T1: wrong tripling result |
| V57 Reminder Correction | MIXED 3/4 | MIXED 3/4 | T3: "change call to text" uses recall_memory |
| V60 Research to Document | MIXED 3/4 | PASS | R043 email check too narrow, R044 honest response |
| V61 Personal Context Chain | MIXED 3/4 | MIXED 3/4 | T3: "what do I have going on" routes to weather |

### Categorized Functional Failures (~13 turn failures)

1. **Routing bugs (3 turns):** V02:T2 anaphoric→reminders, V40:T2-T3 site search→LLM instead of web_navigation, V61:T3 "this week"→weather keyword match overrides context
2. **Wrong tool selection (2 turns):** V44:T4 codebase search→web_search, V57:T3 reminder edit→recall_memory
3. **secondary user honorific persona (5 turns):** V46:T3-T4, V47:T1-T3, V48:T2 — "ma'am"/"Ms. Guest" missing from responses (functional checks, not auto-honorific)
4. **Math error (1 turn):** V52:T1 wrong tripling of 3/4 cup
5. **Context loss (1 turn):** V61:T3 stored appointment not surfaced when asked "what do I have going on"

## Research Sources Used

- **Original V1 plan:** `research/conversation_testing_20260307/plan_conversation_test_suite.md` (session 194, Mar 7)
- **V2 introduction commit:** `88e76b4` (Mar 10) — commit message documents 62 conversations, 26 categories, secondary user honorific validation
- **R023 golden reference:** `tests/iterative_results/run_023_analysis.md` — first-ever V2 run, manually graded per-turn with human annotations. Used as the authoritative source for what each conversation's "correct" behavior looks like.
- **R033 analysis:** `tests/iterative_results/run_033_analysis.md` — second full V2 run comparison
- **R044 analysis:** `memory/run_044_analysis.md` — identified the grading system bugs that prompted this overhaul
- **Owner direction (session 276):** Confirmed V46 farewell must use formal "Ms. Guest", clarified the formal/informal honorific flow for secondary user interactions

## Files Modified

- `scripts/test_conversations.py` — all changes in this file:
  - `grade_turn()`: 3-tuple verdicts, honorific category separation
  - `_any_of()`, `_skip_mum()`: new check helpers
  - `print_conversation_result()`: handles 3-tuple display, honorific warnings
  - `print_analysis()`: separate honorific compliance metric, split failure reasons
  - `save_results()` / `load_results()`: 3-tuple serialization with 2-tuple backward compat
  - `regrade_results()`: functional-only grade computation
  - V2 conversation definitions: 26 conversations gained new explicit checks, 3 checks fixed
