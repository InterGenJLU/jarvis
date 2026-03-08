# Session 202 Handoff — March 8, 2026

## What Was Done
Ran conversation test suite run 005 (40/40, 174 turns, zero errors), performed comprehensive content quality analysis across all responses, fixed C16 keyword routing, committed everything, published.

---

## Run 005 Results Summary

- **40/40 conversations**, 174 turns, 35 minutes total
- **100% Qwen3.5-35B-A3B-Q3_K_M** — zero Claude fallback
- **Avg latency:** 10.4s/turn | **Avg response:** 55 words
- **Routing:** 6 skill-handled, 167 LLM-handled, 1 other
- **Results at:** `tests/iterative_results/run_005_results.json` (full untruncated responses)

### Fix Verification (from session 201)

| Fix | Target | Result |
|-----|--------|--------|
| Fix 1: WS disconnect cleanup | C06 no APT28 bleed | **FIXED** |
| Fix 1: WS disconnect cleanup | C02 no cross-session history | **FIXED** (new tool selection issue found) |
| Fix 2: Show-me guard | C14/C15 pass through to LLM | **FIXED** |
| Fix 3: Keyword negatives | C32 "Google Home" | **FIXED** |
| Fix 3: Keyword negatives | C16 "find ." in bash context | **NOT FIXED** (root cause: "script" keyword, not "find") |
| Fix 4: OOM guards | C07/C08 no crashes | **FIXED** |

**5/7 fixes confirmed. C16 root cause identified and fixed this session (see below).**

---

## C16 Fix Landed This Session

**Root cause:** "script" is a filesystem skill keyword in `metadata.yaml`. "I wrote a bash **script**..." matched it, bypassing the "find" negative context entirely.

**Fix:** Added "script" negative context to `_keyword_negative_contexts` in `core/skill_manager.py`:
- Suppressed when preceded by wrote/write/writing/have/had/got/made/built/created
- Suppressed when prefixed by bash/python/shell
- Suppressed when followed by that/which/to (descriptive context, not filesystem intent)

**Committed:** `75a2805` — NOT yet tested in a run. Verify in run 006.

---

## Comprehensive Content Quality Analysis

### Response Quality — Strengths
- **Associative reasoning strong:** Multi-turn context carried correctly (C05 weather callback, C06 system info chain, C17-C18 math chains, C20 towing comparison synthesis)
- **Math/conversions accurate:** C23 recipe scaling, C24 painting, C27 deck building — all correct arithmetic
- **Cybersecurity knowledge genuinely good:** C10 lateral movement, C11 zero-days with real CVEs, C15 SQL injection code — reads like a practitioner, not regurgitation
- **Code examples clean:** C14 dictionary lookup, C15 parameterized queries — correct and well-commented
- **Tone natural:** "Sir" integrated smoothly, conversational without being robotic

### Content Issues Found — 15 Total, 3 Root Cause Categories

#### Category 1: System Prompt Too Broad (5 issues — fixable via prompt refinement)

**P2-CRITICAL: "Would you like me to read through it all for you?" overuse (~35 instances)**
- **Root cause:** Rule 14 in `core/persona.py:500-502` says: "If the user asks for a recipe, instructions, how-to, or steps, YOU MUST describe what you found AND THEN ASK 'Would you like me to read through it all for you?'"
- Qwen over-generalizes to ALL web search results — product comparisons, security checklists, factual summaries, even content it already fully presented
- **Fix:** Narrow Rule 14 to ONLY literal sequential instructions (recipes, how-tos, setup guides). Add explicit exception: "If you already provided the substance of the answer, do NOT ask if the user wants you to read through it."

**P3: Visible search iteration (C19 T1, C36 T5)**
- "The search results didn't yield... let me try a more specific query..." — exposes internal process
- No rule currently tells Jarvis to hide retry logic
- **Fix:** Add rule: "If a search doesn't return what you need, search again silently. DO NOT tell the user you're retrying or that results were insufficient."

**P3: Hedging / meta-narration (C19 T1)**
- "That's for transport, not driving. Let me get you the actual driving distance..." — should just re-search and answer
- Same fix as above

**P3: Shoehorned memory (C34 T1)**
- Recalled "multi-monitor setup" from memory but forced it into smart home advice where it wasn't relevant
- **Fix:** Could add guidance: "Only cite recalled facts when they directly inform the answer."

**P2: C15 T4 — asked to "read through" content it just presented**
- Generated a full checklist, then asked "Would you like me to read through it all for you?" — nonsensical
- Same Rule 14 over-generalization

#### Category 2: Routing / Tool Selection Bugs (5 issues — code fixes)

| Conv | Turn | Issue | Severity |
|------|------|-------|----------|
| C02 | T2 | "list them" → `manage_reminders` 3x (should be `find_files`) | P1 |
| C03 | T3 | "now open it" → `app_launcher` lists apps (should open specific file) | P3 |
| C05 | T3 | "tell me more about the first one" → `web_navigation` intercept | P1 |
| C07 | T4 | "what's in there now" → web search for local folder | P3 |
| C16 | T1 | "bash script that does find" → `filesystem` via "script" keyword | **FIXED** |

#### Category 3: LLM Knowledge Limitations (5 issues — model constraints)

| Conv | Turn | Issue | Severity |
|------|------|-------|----------|
| C29 | T4 | Fabricated franchise info (Red Notice, Gray Man as "active franchises") | P2 |
| C30 | T2 | **Hallucination:** Taylor Sheridan directed "The Power of the Dog" (actually Jane Campion) | P2 |
| C28 | T5 | Context collapse — repeated Pattinson films instead of new releases | P2 |
| C33 | T5 | Oversimplified — conflated PoE camera wiring with electrical work | P3 |
| C23 | T2 | Math technically correct but could be more precise (6.75 → 7 scoops) | P3 |

---

## Planned Work (Next Session)

### Priority 1: System Prompt Refinement
- Review all 14 rules in `core/persona.py` against run 005 corpus
- Tighten Rule 14 (readback offer) — scope to literal sequential instructions only
- Add rules for: internal process hiding, search-before-claiming for entertainment/trivia
- Run 006 to measure improvement

### Priority 2: LLM Model Evaluation
- **Owner requested:** Review latest Qwen 3.5 "small" model releases
- RX 7600 (8GB) now available as secondary GPU — more VRAM headroom
- Currently running: `Qwen3.5-35B-A3B-Q3_K_M` (MoE, 3B active params)
- Evaluate: Can we fit a higher quantization (Q4/Q5) or a larger model with dual-GPU?
- Key question: Do newer Qwen releases reduce hallucination rate on trivia/entertainment?

### Priority 3: Routing Bug Fixes
- C02 T2: manage_reminders tool selection (investigate why anaphoric "them" → reminders)
- C05 T3: web_navigation intercepting "tell me more about the first one"
- C03 T3: app_launcher vs file open
- C07 T4: web search for local folder query

### Priority 4: Test Suite Enhancements (post-prompt-refinement)
- Profile-aware skill routing (#12) — skills respect `current_user`
- Dual-user test harness — `user_id` field, `set_user` WS message
- Image test harness — `image_data` field, fixture loader
- Obtain test images for C41-C43
- Add C41-C43 with dual-user framing
- System prompt guidance for reasoning tasks (from run 001 C17/C19)
- Location data for mobile interactions

---

## Pending Items (Owner-Noted, Not Yet Started)
- Vision 7c live test — enroll faces, enable presence
- IMAP email via MCP
- CalDAV calendar (secondary user) — BLOCKED on app-specific password

---

## Files Modified This Session
| File | Changes |
|------|---------|
| `core/skill_manager.py` | Added "script" negative context to `_keyword_negative_contexts` |
| `scripts/test_conversations.py` | Removed 300-char response truncation from verbose output |
| `scripts/publish/publish.sh` | Added `tests/iterative_results/` to rsync exclude |

## Commits This Session
| Hash | Description |
|------|-------------|
| `25f5b1e` | Fix 4 run-004 bugs + memory snapshot/restore infrastructure |
| `edd59a5` | Add run 001-004 test results and analysis artifacts |
| `1db63d2` | Exclude tests/iterative_results/ from public publish |
| `75a2805` | Fix C16 'script' keyword + remove response truncation |
| `6836e95` | Add run 005 test results |

## State
- **Memory DB:** 26 active facts (clean — snapshot `pre_run_005` auto-restored after run)
- **Test suite:** 40 conversations, 174 turns. Run 005 is content quality baseline.
- **All files compile clean, working tree clean**
- **Published to public repo**
- **jarvis-web.service running with all fixes EXCEPT C16** (restart needed to pick up C16 fix)
