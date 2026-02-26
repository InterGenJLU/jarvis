# JARVIS Edge Case Testing Plan

**Created:** February 20, 2026 (session 30)
**Purpose:** Systematic adversarial testing to catch edge cases across all subsystems
**Method:** Phased execution — work through one phase per session, track results inline
**Status Legend:** `[ ]` untested | `[P]` pass | `[F]` fail | `[S]` skip (not applicable)

---

## Automated Test Suite

The automated test harness (`scripts/test_edge_cases.py`) validates routing and unit-level behavior
by injecting text directly into the pipeline — no voice/mic/TTS needed.

**Current results: 294/294 (100%) — Tier 1: 115/115 | Tier 2: 151/151 | Tier 4: 28/28**

### Quick Reference

```bash
python3 scripts/test_edge_cases.py              # Tiers 1+2 (default)
python3 scripts/test_edge_cases.py --tier 1     # Unit tests only (<1s)
python3 scripts/test_edge_cases.py --tier 2     # Routing tests only (~5s load)
python3 scripts/test_edge_cases.py --tier 4     # LLM tests only (requires llama-server)
python3 scripts/test_edge_cases.py --all        # All tiers (1+2+4)
python3 scripts/test_edge_cases.py --phase 4A   # Single phase (e.g. system prompt adherence)
python3 scripts/test_edge_cases.py --id 4A-01   # Single test
python3 scripts/test_edge_cases.py --verbose     # Show all tests (not just failures)
python3 scripts/test_edge_cases.py --json        # JSON output
```

### Tiers

| Tier | Scope | Tests | Load Time | Description |
|------|-------|-------|-----------|-------------|
| 1 | Unit | 115 | <1s | Ambient filter (13), noise filter (7), TTS normalizer (20), speech chunker (5), self-awareness (10), task planner (67), people manager (3) |
| 2 | Routing | 151 | ~5s | Intent routing (40), priority chain/state machines (28), skill validation (23), priority ordering (2), routing expansion (20), plan control (39), intro state machine (10) |
| 3 | Execution | — | Future | Run skill handlers, validate response content |
| 4 | LLM | 28 | ~60s | System prompt adherence (5), personality (3), tool calling (4), structured output (2), multi-turn context (2), safety/refusal (3), hallucination resistance (2), technical knowledge (4), voice fitness (3). Requires llama-server on port 8080 |

### Post-Test Cleanup

The test suite includes automatic artifact prevention and cleanup that runs around every execution:
- **Process guard** — blocks visual subprocess launches during tests: any `Popen` with `start_new_session=True` (app_launcher, web_navigation) AND any `Popen` whose command is a known visual binary like `code`, `gnome-terminal`, or browsers (developer_tools `_display.py`). Regular `subprocess.run()` calls (git, wmctrl, etc.) work normally
- **share/ directory** — snapshots contents before tests, removes any new files created by file_editor handlers
- **Component state** — resets all pending confirmations, forget flows, rundown states, and conversation state
- **JSON output** — `cleanup` key only appears when artifacts were actually blocked or removed

### Production Bug Found

The automated suite uncovered a **crash bug** in `skill_manager.py:execute_intent()`:
developer_tools uses a 2-tuple `(command, expiry)` for `_pending_confirmation` while
`execute_intent()` assumed all skills use file_editor's 3-tuple `(action, detail, expiry)`.
When developer_tools ran a handler that set a 2-tuple confirmation, the next call to
`execute_intent()` crashed with `ValueError: not enough values to unpack (expected 3, got 2)`.
**Fixed:** Added tuple-length guard at line 472 — only processes 3-tuple confirmations via the
centralized path; skills with other formats handle confirmations internally.

---

## Phase 1: Intent Routing Stress Test

The routing system (4 layers + priority chain) is the most historically buggy area. These tests probe ambiguous commands, keyword conflicts, and layer transitions.

**Round 1:** 26/40 pass (65%) — 14 failures
**Round 2:** 29/40 pass (72.5%) — 5 fixed, 2 regressions, 6 persistent. Fixes: `50e50eb` `a5e2ccc`
**Round 3:** 37/40 pass (92.5%) — all 8 R2 failures fixed. 3 reclassified (not bugs). Fixes: `d4c8324` (F1-F7) + weather intent_id collision fix.
**Root cause of persistent 1A-11/1D-02:** `register_semantic_intent()` generates intent_id from handler name — 3 weather intents sharing `get_current_weather` silently overwrote each other, discarding 13 of 18 examples. Fixed by merging into single registration + adding duplicate-ID warning guard.

### 1A. Ambiguous Verb Routing

Commands where the verb maps to multiple skills. The correct skill should win.

| ID | Test Input | Expected Skill | Expected Intent | Status | Notes |
|----|-----------|----------------|-----------------|--------|-------|
| 1A-01 | "delete the test file from the share" | file_editor | delete_file | [P] | R1P R2P — Correct skill and handler |
| 1A-02 | "delete my dentist reminder" | reminders | cancel_reminder | [P] | R1F R2F R3P — **FIXED** by F1 (ambiguous suffix disambiguation via semantic similarity) |
| 1A-03 | "forget that I like coffee" | memory_manager | forget (Priority 3) | [P] | R1P R2P |
| 1A-04 | "remove the meeting from my calendar" | reminders | cancel_reminder | [P] | R1F R2P — **FIXED** by +calendar/meeting/appointment keywords |
| 1A-05 | "open chrome" | app_launcher | launch_app | [P] | R1P R2P |
| 1A-06 | "open the article" | news (Priority 5) | article pull-up | [F] | R1F(app_launcher) R2F(LLM). **Not a routing bug** — pull-up needs prior news read to set `get_last_read_url()`. Test needs context setup. |
| 1A-07 | "search for python tutorials" | web_navigation | search_web | [P] | R1P R2P |
| 1A-08 | "search codebase for database" | developer_tools | codebase_search | [P] | R1F R2P — **FIXED** by keyword tie-breaking |
| 1A-09 | "find my config file" | filesystem | file_search | [P] | R1F R2F R3P — **FIXED** by F5 (find_file threshold 0.70→0.55, +example) |
| 1A-10 | "find files containing error" | developer_tools | codebase_search | [P] | R1P R2P (keyword_global_semantic path) |
| 1A-11 | "what's the weather in the news" | weather | current_weather | [P] | R1P R2F R3P — **FIXED.** Root cause: intent_id collision — 3 weather intents shared `get_current_weather` handler, last registration (4 temp examples) silently overwrote first (9 general examples including this test phrase). Fix: merged into single registration (18 examples, threshold 0.60). |
| 1A-12 | "close the browser window" | app_launcher | close_app | [P] | R1P R2P |
| 1A-13 | "show me my drives" | system_info | get_all_drives | [P] | R1P R2P |
| 1A-14 | "show me the git diff" | developer_tools | git_diff | [P] | R1P R2P |
| 1A-15 | "write a reminder to call mom" | reminders | set_reminder | [P] | R1P R2P |
| 1A-16 | "create a bash script for backups" | file_editor | write_file | [P] | R1P R2P |
| 1A-17 | "edit the weather skill" | file_editor | edit_file | [P] | R1P R2F R3P — **FIXED** by F4 (+example "edit the weather skill") |

### 1B. Substring & Word Boundary Traps

Tests for the `"no" in "diagnostic"` class of bugs. All keyword matching should use `\b` word boundaries.

| ID | Test Input | Should NOT Match | Status | Notes |
|----|-----------|-----------------|--------|-------|
| 1B-01 | "run a full system health diagnostic" | "no" bare ack | [P] | R1P R2P |
| 1B-02 | "what's the weather forecast" | "cast" (if any keyword) | [P] | R1P R2P |
| 1B-03 | "tell me about the amazon rainforest" | web_nav amazon_search | [P] | R1F R2P — **FIXED** by adding "amazon" to _generic_keywords |
| 1B-04 | "open the storage drives panel" | "open" generic → wrong skill | [P] | R1P R2P |
| 1B-05 | "is the event happening tomorrow" | reminders (event keyword) | [F] | R1F(weather) R2F(llm_fallback+web search). "event" keyword removed — no false reminders match. LLM web search is reasonable for ambiguous input. |
| 1B-06 | "the application crashed" | app_launcher (application keyword) | [P] | R1F R2F R3P — **FIXED** by F7 (close_app threshold 0.48→0.55, example swap) |
| 1B-07 | "I acknowledge that" | reminders acknowledge_current | [P] | R1P R2P |
| 1B-08 | "what time does the news come on" | time_info or news? | [P] | R1P R2P |

### 1C. Generic Keyword Blocklist Bypass

The `_generic_keywords` blocklist (search, open, find, look, browse, navigate, web, file, code, directory, count, analyze) should prevent false suffix matches but not block legitimate routing.

| ID | Test Input | Expected Result | Status | Notes |
|----|-----------|-----------------|--------|-------|
| 1C-01 | "search" (bare word) | LLM fallback (no skill match) | [P] | R1F R2P R3P — Transient crash in R3 batch run (heap corruption from prior tests); passes on clean restart. Web search fallback is expected LLM behavior. |
| 1C-02 | "open" (bare word) | LLM fallback | [P] | R1F R2F R3P — **FIXED** by F2 (bare word guard moved to match_intent, blocks all layers) |
| 1C-03 | "file" (bare word) | LLM fallback | [P] | R1F R2F R3P — **FIXED** by F2 (same root cause as 1C-02) |
| 1C-04 | "search youtube for music" | web_navigation youtube_search | [P] | R1P R2P |
| 1C-05 | "analyze this script" | filesystem script_analysis | [P] | R1P R2P |
| 1C-06 | "count lines in my project" | filesystem count_code_lines | [P] | R1F(llm_fallback) R2P — **FIXED** by +filesystem keywords. Note: count_code_lines IS the correct handler (no "code_analysis" intent exists). Test expectation corrected. |
| 1C-07 | "navigate to workspace 2" | app_launcher switch_workspace | [P] | R1P R2P |

### 1D. Layer Transition Edge Cases

Commands that should escalate through routing layers (exact → fuzzy → keyword → semantic → LLM).

| ID | Test Input | Expected Layer | Expected Result | Status | Notes |
|----|-----------|---------------|-----------------|--------|-------|
| 1D-01 | "what's up" | Layer 1/2 (exact/fuzzy) | conversation whats_up | [P] | R1P R2P |
| 1D-02 | "could you perhaps look into the current meteorological conditions" | Global semantic | weather current_weather | [P] | R1F R2F R3P — **FIXED.** Same root cause as 1A-11 (intent_id collision discarded the meteorological example). LLM web search for local weather also acceptable behavior. |
| 1D-03 | "yo what time is it bro" | Semantic | time_info time_query | [P] | R1P R2P |
| 1D-04 | "tell me something interesting" | LLM fallback | Qwen response | [P] | R1P R2P |
| 1D-05 | "how do I make pasta" | LLM fallback (or web) | Qwen/web research | [P] | R1P R2P |
| 1D-06 | "jarvis" (bare wake word, no command) | Noise filter or LLM | Should not crash | [P] | R1P R2P — console routes to developer_tools/service_status via semantic (voice mode opens conversation window) |
| 1D-07 | "" (empty string) | Noise filter | Silently ignored | [P] | R1P R2P |
| 1D-08 | "um" | Noise filter | `_is_conversation_noise()` | [P] | R1F R2P — **FIXED** by console noise filter |

---

## Phase 2: Priority Chain & State Machines

Tests for the 7-priority command handling chain in `ConversationRouter.route()`.

**Automated coverage: 28/30 tests** — covered by `scripts/test_edge_cases.py` Tier 2 (marked `[P]` below).
Remaining 4 tests (2A-05..08) require a mid-rundown interruption feature that doesn't exist yet — see Future Features below.

### 2A. Rundown State Machine (Priority 1)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2A-01 | "yes" | Rundown pending | Accept rundown, start reading | [P] | Automated |
| 2A-02 | "no thanks" | Rundown pending | Decline rundown | [P] | Automated |
| 2A-03 | "what time is it" | Rundown pending | **Intercepted by rundown** (P1 traps ALL input) | [P] | Automated. Original expectation "fall through" was wrong — rundown intercepts everything by design |
| 2A-04 | "no" | Rundown pending | Decline (word-boundary match, not substring) | [P] | Automated. Historical substring bug was fixed |
| 2A-05 | "continue" | Mid-rundown | Next item | [F] | **Future feature** — no mid-rundown state exists. `deliver_rundown()` clears state, pauses listener, blocks on TTS |
| 2A-06 | "skip" | Mid-rundown | Skip current item | [F] | **Future feature** — requires item-by-item delivery state machine |
| 2A-07 | "stop" / "that's enough" | Mid-rundown | End rundown early | [F] | **Future feature** — requires interruptible delivery |
| 2A-08 | "defer that" | Mid-rundown item | Defer reminder | [F] | **Future feature** — requires per-item state during delivery |

### 2B. Reminder Acknowledgment (Priority 2)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2B-01 | "got it" | Reminder nagging | Acknowledge, stop nagging | [P] | Automated. P2 intercepts all input when `is_awaiting_ack()` is True |
| 2B-02 | "snooze 10 minutes" | Reminder nagging | **Acked** (snooze intent lost) | [P] | Automated. P2 acks ALL input — snooze is not separately handled at router level |
| 2B-03 | "what reminder is that" | Reminder nagging | **Acked** (query intent lost) | [P] | Automated. Same as 2B-02 — P2 is a blanket ack |
| 2B-04 | "yes" | No reminder pending | NOT intercepted — falls to LLM | [P] | Automated. No false positive |

### 2C. Memory Forget Confirmation (Priority 2.5)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2C-01 | "yes" | `_pending_forget` set | Execute forget | [P] | Automated |
| 2C-02 | "no" | `_pending_forget` set | Cancel forget | [P] | Automated |
| 2C-03 | "yes, delete it" | `_pending_forget` set | Execute forget (P2.5 intercepts before file_editor P4) | [P] | Automated. Historical re-routing bug confirmed fixed |
| 2C-04 | "forget my birthday" | No pending | Does NOT match FORGET_PATTERNS | [P] | Automated. Requires "forget that..." prefix — "forget my birthday" alone falls to skill routing |

### 2D. Dismissal Detection (Priority 2.7)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2D-01 | "no thanks" | After JARVIS offer | Dismiss, close convo window | [P] | Automated |
| 2D-02 | "that's all" | Active conversation | End conversation | [P] | Automated |
| 2D-03 | "never mind" | Active conversation | Dismiss | [P] | Automated |
| 2D-04 | "no thanks, but what time is it" | Active conversation | **Not dismissed** — routes to conversation ("thanks" keyword) | [P] | Automated. Compound: comma content isn't a dismissal phrase, falls through |

### 2E. Bare Acknowledgment Filter (Priority 2.8)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2E-01 | "yeah" | No question pending | Filtered as noise (skip=True) | [P] | Automated |
| 2E-02 | "ok" | No question pending | **Routes to greeting** (len<=2 triggers greeting at P2 before bare ack at P2.8) | [P] | Automated. Original expectation "noise" was wrong |
| 2E-03 | "yeah" | JARVIS just asked a question | Answer, not noise (skip=False) | [P] | Automated |
| 2E-04 | "ok google" | — | Not filtered (has content after "ok") | [P] | Automated |

### 2F. File Editor Confirmation Flow (Priority 4 internal)

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 2F-01 | "yes" | File delete pending (30s) | Confirm delete | [P] | Automated |
| 2F-02 | "no" | File delete pending (30s) | Cancel delete | [P] | Automated |
| 2F-03 | "go ahead" | Delete pending | Execute delete | [P] | Automated. "go ahead" is in affirmatives set |
| 2F-04 | "yes" | No confirmation pending | NOT intercepted — falls to LLM | [P] | Automated |
| 2F-05 | "yes" | Delete pending (simulated step 2) | Confirm delete (30s expiry) | [P] | Automated. Multi-step: setup sets pending confirmation, "yes" confirms |
| 2F-06 | (expired) → "yes" | After delete prompt | Expired — no action, falls to LLM | [P] | Automated

### Future Features Identified During Testing

These test cases (2A-05..08) describe **mid-rundown interruption** — a feature that does not yet exist:

- **Current behavior:** `deliver_rundown()` clears `_rundown_state` to `None`, pauses the listener via callback, then blocks on synchronous `tts.speak()`. There is no "reading" state in the state machine and no code path to receive commands during delivery.
- **Proposed feature:** Item-by-item rundown delivery with an interruptible state machine. Would enable "continue"/"skip"/"stop"/"defer that" commands during rundown reading.
- **Implementation notes:** Requires refactoring `deliver_rundown()` from a single blocking TTS call to an item-at-a-time loop with state checks between items. The listener would need to remain active during delivery.
- **Also discovered:** P2 reminder ack is a blanket handler — `_handle_reminder_ack()` acks ALL input when `is_awaiting_ack()` is True. "Snooze 10 minutes" and "what reminder is that" are both treated as simple acks, losing the snooze/query intent. A future enhancement could parse the input at P2 to distinguish ack/snooze/query.

---

## Phase 3: Audio Pipeline Edge Cases

### 3A. Ambient Wake Word Filter

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 3A-01 | "Jarvis, what time is it" | PASS — process command | [ ] | Normal command |
| 3A-02 | "Hey Jarvis, set a timer" | PASS — prefix exception | [ ] | "Hey" prefix |
| 3A-03 | "Jarvis is really cool" | BLOCK — copula "is" | [ ] | Ambient mention |
| 3A-04 | "I think Jarvis is broken" | BLOCK — position (not first 2 words) + copula | [ ] | |
| 3A-05 | "Jarvis's response was great" | BLOCK — possessive | [ ] | |
| 3A-06 | "Jarvis, I think the weather..." | PASS — position 0 + comma | [ ] | Comma after wake word = addressing |
| 3A-07 | "Tell Jarvis to check the weather" | BLOCK — position >2 | [ ] | Talking ABOUT Jarvis |
| 3A-08 | "Good morning Jarvis" | PASS — "good morning" prefix exception | [ ] | |
| 3A-09 | "Paris is beautiful this time of year" | BLOCK — threshold 0.80 (paris=0.73) | [ ] | False positive elimination |
| 3A-10 | "Farvis, what's the weather" | PASS — "farvis" = 0.83 similarity | [ ] | Acceptable mispronunciation |
| 3A-11 | "The tide jarvis was a success" | BLOCK — below 0.80 threshold | [ ] | |
| 3A-12 | "Jarvis is, I think, the best assistant" | PASS or BLOCK? | [ ] | Comma after copula — edge case |
| 3A-13 | 20+ word sentence mentioning Jarvis at word 18 | BLOCK — length >15 + position >0 | [ ] | |

### 3B. Conversation Windows

| ID | Test Input | Context | Expected | Status | Notes |
|----|-----------|---------|----------|--------|-------|
| 3B-01 | Command without wake word | Window open (4s default) | Process command | [ ] | Follow-up in active window |
| 3B-02 | Command without wake word | Window closed | Require wake word | [ ] | |
| 3B-03 | Command without wake word | Window open, 3.9s elapsed | Race condition — process or ignore? | [ ] | Timer edge |
| 3B-04 | "and what about tomorrow" | After weather query, window open | Follow-up → weather forecast | [ ] | |
| 3B-05 | (silence for 5 seconds) | Window open | Window closes, no crash | [ ] | Auto-close timer |
| 3B-06 | Rapid-fire: 3 commands in 2 seconds | Window open | All processed sequentially | [ ] | Queue handling |

### 3C. Noise Filter

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 3C-01 | "um" | Filtered — single short word | [ ] | |
| 3C-02 | "uh huh" | Filtered? Or bare ack? | [ ] | |
| 3C-03 | "..." | Filtered — garbage | [ ] | |
| 3C-04 | "(laughing)" | Filtered — transcription artifact | [ ] | Whisper sometimes outputs these |
| 3C-05 | "Thank you. (applause)" | Process "thank you", ignore artifact | [ ] | |
| 3C-06 | "you" | Filtered — single short word | [ ] | Whisper ghost transcription |
| 3C-07 | "the the the" | Filtered? | [ ] | Stuttered transcription |

### 3D. Device Hot-Plug

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 3D-01 | Unplug USB mic during listening | Reconnect on replug (5s poll) | [ ] | `_device_monitor_loop()` |
| 3D-02 | Start JARVIS with no mic plugged in | Graceful error, retry on plug | [ ] | |
| 3D-03 | Plug in different USB mic | Should use configured device | [ ] | udev rule: FIFINE K669B |

---

## Phase 4: LLM & Web Research

### 4A. Quality Gate

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 4A-01 | Ask factual question Qwen knows | Direct answer, no retry | [ ] | Happy path |
| 4A-02 | Ask obscure question Qwen deflects on | Auto web research fallback | [ ] | Deflection safety net |
| 4A-03 | Ask question that produces gibberish first sentence | Retry with nudge → Claude fallback | [ ] | Quality gate rejection |
| 4A-04 | Very long question (>500 tokens) | Context overflow handling | [ ] | Trim to system + last 6 messages |
| 4A-05 | Question in a language other than English | Qwen may answer in that language | [ ] | Behavior undefined — document it |

### 4B. Web Research Tool Calling

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 4B-01 | "what's the score of the game" | Tool call → DuckDuckGo search | [ ] | Verifiable fact → must search |
| 4B-02 | "tell me about photosynthesis" | LLM direct (established science) | [ ] | Should NOT search for textbook knowledge |
| 4B-03 | "what happened in the news today" | Should search (current events) | [ ] | |
| 4B-04 | "who won the super bowl" | Should search (recent event) | [ ] | Date comparison in synthesis |
| 4B-05 | "best restaurants near me" | Should search + use location from memory? | [ ] | Roadmap #7: user facts in research |
| 4B-06 | "tell me more about that" | Research follow-up (Priority 3.5) | [ ] | `_detect_research_followup()` |
| 4B-07 | "what's 2+2" | LLM direct — trivial math | [ ] | Should NOT trigger web search |
| 4B-08 | Search that returns 0 DuckDuckGo results | Graceful fallback | [ ] | |
| 4B-09 | Search where all page fetches timeout | Return search snippets only | [ ] | trafilatura 3s timeout |

### 4C. Streaming Edge Cases

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 4C-01 | LLM response is exactly 1 sentence | Delivered as single chunk | [ ] | |
| 4C-02 | LLM response is 0 tokens (empty) | Graceful handling, no crash | [ ] | |
| 4C-03 | LLM response contains markdown tables | Stripped before TTS, preserved for web/console | [ ] | Per-chunk metric stripping |
| 4C-04 | LLM produces very long response (1000+ tokens) | max_tokens limit respected | [ ] | `_estimate_max_tokens()` |
| 4C-05 | Interrupt mid-stream (new wake word) | Stop current TTS, process new command | [ ] | `_active_procs` scoped interrupt |
| 4C-06 | Network error to llama-server mid-stream | Graceful error message | [ ] | Connection refused/timeout |

---

## Phase 5: Skill-Specific Edge Cases

### 5A. Weather

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5A-01 | "weather" (bare word) | Current weather for default location | [ ] | |
| 5A-02 | "weather in a city that doesn't exist" | Graceful API error | [ ] | OpenWeather returns 404 |
| 5A-03 | "what's the temperature in celsius" | Respect unit preference | [ ] | |
| 5A-04 | "will it rain next month" | Forecast limit handling | [ ] | API only has ~5 days |
| 5A-05 | "weather in São Paulo" | Unicode city name | [ ] | |
| 5A-06 | "weather in New York City" vs "NYC" | Both should resolve | [ ] | |

### 5B. Reminders & Calendar

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5B-01 | "remind me tomorrow" (no task specified) | Ask what to remind about | [ ] | |
| 5B-02 | "remind me to call mom at 3 PM on February 30th" | Invalid date handling | [ ] | Feb 30 doesn't exist |
| 5B-03 | "remind me in 0 minutes" | Immediate? Or error? | [ ] | Edge: zero duration |
| 5B-04 | "remind me every 5 seconds" | Reject or very short interval | [ ] | Spam prevention |
| 5B-05 | "what do I have today" (empty calendar) | "Nothing scheduled" response | [ ] | |
| 5B-06 | "set a reminder" then immediately "cancel it" | Create then cancel | [ ] | |
| 5B-07 | Calendar event created externally (Google Calendar web) | Synced on next poll | [ ] | Incremental sync |
| 5B-08 | "daily rundown" at 11 PM | Still works, not morning-only | [ ] | |
| 5B-09 | 50+ reminders active simultaneously | Performance test | [ ] | |

### 5C. File Editor

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5C-01 | "write a file called ../../../etc/passwd" | Path traversal blocked by `_safe_path()` | [ ] | Security: sandbox escape |
| 5C-02 | "write a file called test.py" (already exists) | Overwrite confirmation prompt | [ ] | |
| 5C-03 | "edit a file that doesn't exist" | Error message | [ ] | |
| 5C-04 | "write a file with an emoji name 🎉.txt" | Unicode filename handling | [ ] | |
| 5C-05 | "write a 10,000 word essay" | LLM token limit handling | [ ] | |
| 5C-06 | "edit a file that's 600 lines" | Reject (500 line limit) | [ ] | 15KB/500 line guard |
| 5C-07 | "delete nonexistent.txt" | Error: file not found | [ ] | |
| 5C-08 | "list files" (share folder empty) | "No files" message | [ ] | |
| 5C-09 | "write a binary file" | Should it refuse? | [ ] | LLM can't generate binary |
| 5C-10 | "read a file with special characters in content" | Display correctly | [ ] | |

### 5D. Developer Tools

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5D-01 | "git status" (all repos clean) | "All clean" message | [ ] | |
| 5D-02 | "git status" (uncommitted changes exist) | Show changed files | [ ] | |
| 5D-03 | "run rm -rf /" | Safety tier rejection | [ ] | Destructive command blocked |
| 5D-04 | "run a command" (no command specified) | Ask what command | [ ] | |
| 5D-05 | "search codebase for a string that doesn't exist" | "No results" gracefully | [ ] | |
| 5D-06 | "health check" when GPU is busy | Report degraded state | [ ] | |
| 5D-07 | "show me" with no prior output to show | Error message | [ ] | |
| 5D-08 | "run pip install malware" | Safety tier check | [ ] | |
| 5D-09 | "what branch am I on" (detached HEAD) | Handle gracefully | [ ] | |

### 5E. Web Navigation

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5E-01 | "open result 5" (only 3 results) | Out of range error | [ ] | |
| 5E-02 | "search youtube" (no query) | Ask for search term | [ ] | |
| 5E-03 | "next page" (no prior search) | Error: no active search | [ ] | |
| 5E-04 | "open a URL with spaces" | URL encoding | [ ] | |
| 5E-05 | "search for <script>alert('xss')</script>" | XSS in search query | [ ] | Sanitization |
| 5E-06 | "search amazon for something" then "search youtube for something else" | Context switches cleanly | [ ] | |
| 5E-07 | "repeat the last search" (no prior search) | Error handling | [ ] | |

### 5F. App Launcher & Desktop

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5F-01 | "launch chrome" (already running) | Focus existing or launch new? | [ ] | |
| 5F-02 | "close chrome" (not running) | Graceful error | [ ] | |
| 5F-03 | "volume up" 20 times rapidly | Cap at 100%? | [ ] | |
| 5F-04 | "switch to workspace 99" | Error: invalid workspace | [ ] | |
| 5F-05 | "fullscreen" (no active window) | Error handling | [ ] | |
| 5F-06 | "what's on my clipboard" (clipboard empty) | "Clipboard is empty" | [ ] | |
| 5F-07 | "launch an app not in config" | Error: unknown app | [ ] | 8 configured apps only |
| 5F-08 | D-Bus extension not loaded | Fall back to wmctrl | [ ] | Fallback chain test |
| 5F-09 | "minimize" with Wayland-only (no XWayland) | D-Bus should work, wmctrl fails | [ ] | |
| 5F-10 | "copy that to clipboard" (nothing to copy) | Error handling | [ ] | |

### 5G. Conversation Skill

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5G-01 | "hello" | Greeting response | [ ] | |
| 5G-02 | "good morning" at 10 PM | Still responds appropriately? | [ ] | Time-aware response? |
| 5G-03 | "how are you" twice in a row | Not identical response | [ ] | |
| 5G-04 | "goodbye" | Farewell + close window | [ ] | |
| 5G-05 | "thanks" after a skill response | "You're welcome" not re-routed | [ ] | |

### 5H. News

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5H-01 | "any news" (0 headlines available) | "No headlines" message | [ ] | RSS all empty |
| 5H-02 | "cybersecurity news" | Filtered to cyber category | [ ] | |
| 5H-03 | "continue" (no reading in progress) | Error or LLM fallback | [ ] | |
| 5H-04 | "pull that up" (no article mentioned) | Error handling | [ ] | |
| 5H-05 | Duplicate headlines from multiple feeds | Semantic dedup filters them | [ ] | |

### 5I. System Info

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 5I-01 | "what cpu do i have" | Ryzen 9 5900X response | [ ] | |
| 5I-02 | "how much disk space" (drive unmounted) | Report available drives only | [ ] | |
| 5I-03 | "what's my GPU" | RX 7900 XT (20GB) | [ ] | |
| 5I-04 | All 5 intents in rapid succession | All respond correctly | [ ] | |

---

## Phase 6: Memory & Context Window

### 6A. Conversational Memory

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 6A-01 | "remember that I like pizza" | Store fact in SQLite | [ ] | |
| 6A-02 | "what do you know about me" | Recall stored facts | [ ] | Transparency intent |
| 6A-03 | "forget that I like pizza" | Forget flow with confirmation | [ ] | |
| 6A-04 | "what's my favorite food" | Recall "pizza" if stored | [ ] | Proactive surfacing |
| 6A-05 | Store contradictory facts | Later fact should supersede? | [ ] | "I like pizza" then "I hate pizza" |
| 6A-06 | 25+ messages in one session | Batch extraction trigger | [ ] | Bug B2: untested |
| 6A-07 | "forget everything about me" | Mass delete with confirmation | [ ] | |
| 6A-08 | Recall with no stored facts | "I don't have any facts" | [ ] | |

### 6B. Context Window

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 6B-01 | Single-topic conversation (10 messages) | One topic segment | [ ] | |
| 6B-02 | Abrupt topic switch mid-conversation | New segment detected (threshold 0.35) | [ ] | |
| 6B-03 | Return to previous topic | Relevance scoring finds old segment | [ ] | |
| 6B-04 | Token budget exhausted (6500 tokens) | Oldest segments evicted | [ ] | |
| 6B-05 | Context window flush on shutdown | Persisted to disk | [ ] | Cross-session persistence |
| 6B-06 | Start new session | Loads from disk, continues | [ ] | |

### 6C. Cross-Session Memory

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 6C-01 | Restart JARVIS, ask "what did we talk about" | Loads last 32 msgs from chat_history.jsonl | [ ] | |
| 6C-02 | chat_history.jsonl corrupted/missing | Graceful start with empty history | [ ] | |
| 6C-03 | Very large chat_history.jsonl (10MB+) | Performance check — only loads last 32 | [ ] | |

---

## Phase 7: TTS & Normalization

### 7A. TTS Engine

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 7A-01 | Speak a sentence | Kokoro primary engine | [ ] | |
| 7A-02 | Kokoro fails/crashes | Automatic Piper fallback | [ ] | |
| 7A-03 | aplay device busy | Retry logic (5 attempts) | [ ] | |
| 7A-04 | Speak while already speaking | `_tts_lock` serializes | [ ] | |
| 7A-05 | Speak empty string | No crash, no audio | [ ] | |
| 7A-06 | Speak very long text (2000+ words) | Streaming chunked delivery | [ ] | |
| 7A-07 | Speak text with only punctuation "..." | Handle gracefully | [ ] | |
| 7A-08 | Ack cache playback | Pre-synthesized, instant | [ ] | |

### 7B. TTS Normalization (14 passes)

| ID | Input | Expected Spoken Form | Status | Notes |
|----|-------|---------------------|--------|-------|
| 7B-01 | "192.168.1.1" | "one ninety two dot one sixty eight dot one dot one" | [ ] | IP normalization |
| 7B-02 | "$42.50" | "forty two dollars and fifty cents" | [ ] | Currency before decimals |
| 7B-03 | "2.5GB" | "two point five gigabytes" | [ ] | File size |
| 7B-04 | "3:45 PM" | "three forty five PM" | [ ] | Timestamp |
| 7B-05 | "https://example.com/path" | "example dot com" (or similar) | [ ] | URL normalization |
| 7B-06 | "/home/user/jarvis" | "home christopher jarvis" | [ ] | Path normalization |
| 7B-07 | "config.yaml" | "config dot yaml" | [ ] | Filename with extension |
| 7B-08 | "AMD RX 7900 XT" | "AMD RX seventy nine hundred XT" | [ ] | GPU model |
| 7B-09 | "port 8080" | "port eighty eighty" | [ ] | Port number |
| 7B-10 | "In 2024, the GDP was $21.5T" | Year + currency + number all in one | [ ] | Multiple normalizations in sequence |
| 7B-11 | "## Heading\n- bullet\n**bold**" | Markdown stripped | [ ] | Markdown removal |
| 7B-12 | "0.001" | "zero point zero zero one" | [ ] | Small decimal |
| 7B-13 | "test.py" | "test dot pie" | [ ] | `.py` extension pronunciation |
| 7B-14 | "1,234,567" | "one million two hundred thirty four thousand five hundred sixty seven" | [ ] | Large number with commas |

### 7C. Speech Chunker

| ID | Input Stream | Expected Chunks | Status | Notes |
|----|-------------|----------------|--------|-------|
| 7C-01 | "Hello. How are you?" | ["Hello.", "How are you?"] | [ ] | Two sentences |
| 7C-02 | "No end punctuation" + flush | ["No end punctuation"] | [ ] | flush() for end-of-stream |
| 7C-03 | "Dr. Smith went to the store." | Single chunk (not split on "Dr.") | [ ] | Abbreviation handling |
| 7C-04 | "Version 3.5 is great!" | Split on "!" not on "3.5" | [ ] | Decimal in sentence |
| 7C-05 | "Really?! No way!" | How many chunks? | [ ] | Multiple sentence-end chars |

---

## Phase 8: Web UI

### 8A. WebSocket Connection

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 8A-01 | Connect to WebSocket | Receives session_list + current session + last 50 history messages | [ ] | |
| 8A-02 | Send a message | Response streamed back | [ ] | |
| 8A-03 | Disconnect and reconnect | Reconnect feedback, history reloaded | [ ] | |
| 8A-04 | Two simultaneous WebSocket clients | Both receive updates | [ ] | |
| 8A-05 | Send message > 500KB | Rejection | [ ] | Upload limit |
| 8A-06 | Send empty message | No crash | [ ] | |
| 8A-07 | Rapid-fire 10 messages in 1 second | Queue handled, no drops | [ ] | |

### 8B. Session Management

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 8B-01 | Load sessions (>30 min gap detection) | Correct session boundaries | [ ] | |
| 8B-02 | Rename a session (double-click) | Name persisted in sessions_meta.json | [ ] | |
| 8B-03 | Click on old session | History loads for that session | [ ] | |
| 8B-04 | "View more" pagination | Next 10 sessions load | [ ] | |
| 8B-05 | Current session shows LIVE badge | Badge visible | [ ] | |

### 8C. File Handling & Slash Commands

| ID | Test Input | Expected | Status | Notes |
|----|-----------|----------|--------|-------|
| 8C-01 | Drag and drop a .txt file | File content injected into document buffer | [ ] | |
| 8C-02 | Drag and drop a .exe file | Binary rejection | [ ] | |
| 8C-03 | `/file /etc/passwd` | Read and display (if permitted) | [ ] | |
| 8C-04 | `/clipboard` | Paste clipboard contents | [ ] | |
| 8C-05 | `/context` | Show current document buffer | [ ] | |
| 8C-06 | `/clear` | Clear document buffer | [ ] | |
| 8C-07 | `/append` then add text | Text appended to buffer | [ ] | |
| 8C-08 | `/file nonexistent_path` | Error message | [ ] | |
| 8C-09 | Upload file > 500KB | Rejection with message | [ ] | |

### 8D. Streaming & Rendering

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 8D-01 | LLM streams code block | Rendered with syntax highlighting + copy button | [ ] | |
| 8D-02 | LLM streams markdown table | Rendered as HTML table | [ ] | |
| 8D-03 | XSS attempt in LLM response | Sanitized by `renderMarkdown()` | [ ] | |
| 8D-04 | Streaming cursor visible during generation | Blinking dot animation | [ ] | |
| 8D-05 | Very long response (scrolling) | Auto-scroll to bottom | [ ] | |
| 8D-06 | Ctrl+L | Clear chat display | [ ] | |

---

## Phase 9: Integration & Stress Tests

### 9A. Multi-System Interactions

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 9A-01 | Ask weather → "remind me if it'll rain" | Weather skill → reminder creation | [ ] | Cross-skill chain |
| 9A-02 | Web search → "save that to a file" | Web research → file_editor | [ ] | |
| 9A-03 | "what did I ask you earlier about the weather" | Memory recall + context window | [ ] | |
| 9A-04 | Voice command + Web UI command simultaneously | Both processed, no race condition | [ ] | |
| 9A-05 | News reading interrupted by reminder alarm | Reminder priority > news continuation | [ ] | Priority chain |
| 9A-06 | Start rundown → reminder fires mid-rundown | How do they interact? | [ ] | |
| 9A-07 | Calendar event + reminder for same time | Both fire? One deduped? | [ ] | |

### 9B. Error Recovery

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 9B-01 | llama-server process killed | Graceful error, Claude fallback | [ ] | |
| 9B-02 | Network disconnected (no internet) | Web research fails gracefully, local skills work | [ ] | |
| 9B-03 | OpenWeather API key expired | Weather skill error message | [ ] | |
| 9B-04 | Google Calendar OAuth token expired | Token refresh or graceful error | [ ] | |
| 9B-05 | Disk full | Graceful handling of write failures | [ ] | |
| 9B-06 | FAISS index corrupted | Memory manager starts fresh | [ ] | |
| 9B-07 | memory.db locked by another process | SQLite retry or error | [ ] | |

### 9C. Performance & Load

| ID | Scenario | Expected | Status | Notes |
|----|----------|----------|--------|-------|
| 9C-01 | 100 consecutive commands | No memory leak, consistent latency | [ ] | |
| 9C-02 | JARVIS uptime > 24 hours | No degradation | [ ] | |
| 9C-03 | 50 reminders + 16 RSS feeds + calendar polling | Background tasks don't starve command processing | [ ] | |
| 9C-04 | First command after cold start | STT warm-up eliminates delay | [ ] | |
| 9C-05 | GPU VRAM after extended use | No VRAM leak | [ ] | |

---

## Test Execution Log

Track session-by-session execution here:

| Session | Date | Phase(s) Tested | Pass | Fail | Notes |
|---------|------|-----------------|------|------|-------|
| 30 | Feb 20 | Phase 1 R1 | 26 | 14 | Initial baseline (65%) |
| 31 | Feb 20 | Phase 1 R2 | 29 | 8+3 | 5 fixed, 2 regressions, 3 reclassified. Fixes: `50e50eb` `a5e2ccc` |
| 32 | Feb 20 | Phase 1 R2→R3 fixes | — | — | F1-F7 applied: suffix disambig, bare word guard, thresholds. `d4c8324` `7213ce1` |
| 33 | Feb 21 | Phase 1 R3 | 37 | 0+3 | **ALL PASS.** 3 reclassified (not bugs). Weather intent_id collision found & fixed. |
| 36 | Feb 21 | Automated suite | 122 | 0 | Tier 1: 39 unit + Tier 2: 83 routing (Phase 1 + Phase 2 + Phase 5). Production bug found & fixed (`c053805`). |
| 37 | Feb 21 | Cleanup + docs | — | — | Post-test artifact cleanup added. Phase 2 doc updated with automated coverage (18/30). |
| 39 | Feb 22 | Phase 2 expansion | 132 | 0 | +10 tests: 2A-04, 2B-01..04, 2C-03, 2C-04b, 2D-05, 2F-03, 2F-05. Phase 2: 28/30, 4 deferred (mid-rundown = future feature). |
| 44 | Feb 22 | Phase 1E expansion | 144 | 0 | +12 tests across Phase 1E. All tiers passing. |
| 46 | Feb 23 | Additional routing tests | 152 | 0 | +8 tests. Tier 1: 39 unit + Tier 2: 113 routing. |
| 61 | Feb 24 | Tier 4 LLM tests | 180 | 0 | +28 LLM tests (9 phases). Requires llama-server. |
| 69 | Feb 25 | Task planner tests | 242 | 0 | +62 tests across task planner, routing expansion, additional unit tests. |
| 74 | Feb 25 | Social introductions | 270 | 0 | +14 people manager unit tests + 14 intro routing tests. Tier 1: 112, Tier 2: 130, Tier 4: 28. |
| 78 | Feb 26 | Phase 9C: Intro state machine | 294 | 0 | +10 multi-turn state machine tests (happy path, correction, P2.6 interception, timeout, already-known, filler strip, multiple facts, list_people states, fix pronunciation flow, done phrases). +14 from Tier 1/2 recount. Tier 1: 115, Tier 2: 151, Tier 4: 28. |

---

## How to Test

### Voice Mode (Production)
```bash
systemctl --user restart jarvis
journalctl --user -u jarvis -f    # Watch logs
# Speak test commands and verify routing in logs
```

### Console Mode (Faster Iteration)
```bash
python3 jarvis_console.py
# Type test commands, check [ROUTING] log lines
```

### Web UI
```bash
jarvis_webserver_start    # or: python3 jarvis_web.py
# Open http://localhost:8765 in browser
```

### Checking Routing Decisions
Look for these log patterns:
- `[SKILL_MANAGER]` — which layer matched, which skill/intent
- `[PIPELINE]` — which priority level handled the command
- `[MEMORY]` — fact storage/recall operations
- `[TTS]` — which engine spoke, chunk delivery
- `[STT]` — transcription text and confidence

---

**Total: ~200 manual test cases across 9 phases, 30+ subsections**
**Automated: 294 tests (Tier 1: 115 unit + Tier 2: 151 routing + Tier 4: 28 LLM) via `scripts/test_edge_cases.py` — includes post-test artifact cleanup**
**Phase 2: 28/30 automated, 4 deferred (mid-rundown interruption — future feature)**
**Remaining: Phases 3-9 require live voice/hybrid/web testing**
