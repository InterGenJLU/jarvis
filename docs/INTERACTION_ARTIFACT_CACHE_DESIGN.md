# Interaction Artifact Cache — Design Document

> **Date:** March 3, 2026 (Session 131)
> **Status:** Active design discussion — data model phase
> **Research:** See `INTERACTION_ARTIFACT_CACHE_RESEARCH.md`
> **Prior work this replaces:** Readback flow plan (`memory/plan_readback_flow.md`)

---

## Origin Story

The readback flow (sessions 128-130) was the catalyst. Web search → LLM picks a recipe → user says "read it to me" → system needs to retrieve the cached tool result and re-prompt the LLM. This required ad-hoc caching in `conv_state.last_tool_result_text`, which gets cleared after single use and can't be re-referenced.

The architectural insight: **this isn't a readback problem — it's a fundamental interaction model problem.** Any query that produces structured data should cache that data as discrete, addressable items. The LLM should be able to manipulate every aspect during interaction at the user's request. This applies to everything JARVIS does, not just web research.

## The Vision

```
User Query
    |
    v
Router -> Skill/Tool/LLM
    |
    v
Response + Artifacts
    |
    v
+----------------------------------+
|   Interaction Artifact Cache      |
|                                   |
|  Turn 1: [item_1, item_2, ...]   |
|  Turn 2: [synthesis_1]           |
|  Turn 3: [modified_item_1]       |
|                                   |
|  Each item: typed, addressable,   |
|  with provenance + metadata       |
+----------------------------------+
    | (on window close / session end)
    v
    Summarize -> Long-term Memory
```

### How Every Interaction Type Benefits

| Scenario | Today | With Artifact Cache |
|----------|-------|---------------------|
| "What's the weather this week?" | Single response, gone | 7 day-items, "what about Thursday?" without re-fetch |
| "Search for Apache Struts vulns" | 5 results as flat list | 5 discrete items with content, referenceable across turns |
| "Find large files on my system" | Tool output as one string | Individual file entries, "delete the third one" |
| "Set 3 reminders" | 3 separate skill calls | 3 confirmation items, "cancel the second one" |
| "Read me that recipe" | Re-prompt with cached string | Structured sections, "skip to step 4", "just the ingredients" |
| "What did we look up yesterday?" | 500-char summary | Full artifacts from prior session, re-loadable |

## Key Design Questions (Under Discussion)

### 1. Granularity — What counts as an "item"?
- A search result is obvious
- An LLM conversational response — keep atomic? Or decompose into sections?
- Likely answer: keep atomic by default, decompose on demand (when user asks to navigate within)

### 2. Schema — Do items need types?
- Options: typed (search_result, tool_output, synthesis, sub_item) vs flat with metadata tags
- CogCanvas uses 5 fixed types; Google ADK uses MIME types; A-Mem lets structure emerge
- JARVIS likely needs types because voice reference resolution benefits from them ("that recipe" vs "that search")

### 3. Lifetime — How long do items live?
- Tiered (MemGPT model): hot (in-context) → warm (session, SQLite) → cold (summarized)
- Window-scoped items cleared on window close (current behavior)
- Session-scoped items persist across windows within a process lifetime
- Promoted items survive into long-term memory

### 4. Decomposition — Who breaks responses into sub-items?
- LLM itself (structured output on every response) — adds latency
- Post-processor (regex/heuristic) — fragile
- On-demand only ("read me the ingredients" triggers decomposition) — probably right
- CogCanvas does it on every turn; Manus only caches what's needed

### 5. Reference Language — How do users point to cached items?
- Ordinal: "the second one" (already handled for research results)
- Type-based: "that recipe", "the weather forecast"
- Entity-based: "the Apache one", "the one from Tastes Better"
- Recency: "the last search", "what you just said"
- Google Assistant solves this with ML-based contextual rephrasing
- JARVIS can solve with LLM pass or pattern-based resolution

### 6. Memory Promotion — What gets summarized at session end?
- Everything? Only items the user interacted with? LLM-selected?
- CogCanvas: crystallize everything (no summarization)
- OpenAI: filter ephemeral items, consolidate the rest
- Manus: only persist what can't be cheaply reconstructed

### 7. Replaces vs. Wraps — Integration with existing systems?
- ConversationState → artifact cache becomes the source of truth for turn data
- context_window → could use artifact metadata for topic segmentation
- memory_manager → receives promoted artifacts at session end
- conversation.py → session_history continues as-is for raw message logging

## Feasibility Assessment

### Straightforward
- Data model: SQLite, already have the patterns
- Caching tool outputs: already almost doing it (conv_state ad-hoc caching)
- Tiered lifetime: existing architecture has the tiers, just not unified
- Session-end summarization: proven patterns (OpenAI, CogCanvas)
- Performance: ~20-50 artifacts per session, SQLite handles trivially

### Real Engineering Challenge
- Reference resolution: ordinals are easy (already done), type/entity-based harder
- Decomposition granularity: when to break items into sub-items
- Qwen structured output reliability: needs numbered-RULES prompt tuning

### Not a Concern
- Storage: text artifacts are tiny
- Complexity: unified system would REDUCE complexity vs current 9 mechanisms

## Implementation Approach (Incremental)

**Phase 1 — The Cache Layer**
- Define the artifact data model
- Build `InteractionCache` class (in-memory + SQLite warm tier)
- Wire into conversation_router: every skill/tool/LLM response writes artifacts
- Prove it works by making readback use the cache instead of ad-hoc conv_state

**Phase 2 — Reference Resolution**
- Ordinal references (extend existing `_handle_research_followup`)
- Type-based references ("that recipe", "the weather")
- Build into router as a new priority level

**Phase 3 — Sub-Item Navigation**
- On-demand decomposition (user asks for part of an artifact)
- Voice navigation ("next step", "go back", "skip to ingredients")
- TTS integration for sequential readback

**Phase 4 — Memory Promotion**
- Session-end summarization hook
- Crystallize important artifacts into memory_manager
- Ephemeral filtering (don't promote transient items)

**Phase 5 — Cross-Session Retrieval**
- "What did we look up yesterday?"
- Warm tier becomes queryable across sessions
- Integrate with existing interaction_log in memory_manager

---

## Appendix: Current Ad-Hoc Caching Mechanisms (to be unified)

### 1. conv_state.research_results (in-memory list)
- SET: jarvis_web.py:724, pipeline.py:1135
- READ: conversation_router.py:486 (research follow-up)
- LIFE: until window close

### 2. conv_state.last_tool_result_text (string)
- SET: jarvis_web.py:723
- READ: jarvis_web.py:497 (readback trigger)
- LIFE: single use, then cleared

### 3. conv_state.last_response_text (string)
- SET: conversation_state.py:63 via update()
- READ: jarvis_web.py:292 (readback prior_pick injection)
- LIFE: next turn overwrites

### 4. conv_state.research_exchange (dict)
- SET: pipeline.py:1262 via set_research_context()
- READ: conversation_router.py:1201 (LLM context fallback)
- LIFE: until window close. Answer truncated to 400 chars.

### 5. Session history (JSONL + in-memory)
- SET: conversation.py:151 add_message()
- READ: conversation.py:203 get_recent_history()
- LIFE: 16 turns, 12K chars in memory. JSONL on disk.

### 6. Context window segments (TopicSegments)
- SET: context_window.py:148 on_message()
- READ: context_window.py:185 assemble_context()
- LIFE: process lifetime, 20 segments max. Currently disabled.

### 7. Interaction log (SQLite)
- SET: pipeline.py:1277 persist_interaction()
- READ: memory_manager.py:1221 recall_interactions()
- LIFE: 30 days. Answer truncated to 500 chars.

### 8. Streaming buffer (local vars)
- SET: jarvis_web.py:661 during token stream
- READ: jarvis_web.py:388 after stream complete
- LIFE: single request

### 9. Conversation window state (boolean)
- SET: continuous_listener.py:766
- READ: pipeline.py:720
- LIFE: timeout/close
