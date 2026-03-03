# Interaction Artifact Cache — Research Report

> **Date:** March 3, 2026 (Session 131)
> **Context:** Architectural exploration for universal interaction caching in JARVIS
> **Status:** Research complete, plan in progress

---

## The Problem

JARVIS currently has 9 separate ad-hoc caching mechanisms, each storing a different slice of interaction data in different formats, with different lifetimes, and none of them talk to each other:

| Cache | Location | Storage | Lifetime | Addressable? |
|-------|----------|---------|----------|-------------|
| Search results | `conv_state.research_results` | In-memory list | Window close | By index only |
| Tool output | `conv_state.last_tool_result_text` | String | Single use, then cleared | No |
| LLM response | `conv_state.last_response_text` | String | Next turn overwrites | No |
| Research exchange | `conv_state.research_exchange` | Dict | Window close | No |
| Interaction log | SQLite `interaction_log` | Database | 30 days | Semantic search, 500 char summary |
| Session history | JSONL + in-memory | List | Process lifetime | No |
| Context window | `context_window.segments` | TopicSegments | Process lifetime | No |
| Streaming buffer | `full_response` | String | Single request | No |
| Conversation window | `conversation_window_active` | Boolean | Timeout/close | No |

The readback flow (sessions 128-130) exposed this: it's the first feature that needed to reach back into a prior turn's structured output and manipulate it. The current workaround is fragile — string caching in conv_state, cleared after single use.

## The Vision

Every interaction produces **artifacts** — discrete, typed, addressable items:
- A web search returns 5 results → 5 cached items (title, URL, snippet, full page if fetched)
- The LLM picks one and synthesizes → 1 cached synthesis (with provenance to source)
- That synthesis has sub-structure (ingredients, steps) → navigable on demand
- User says "read me the ingredients" → retrieves sub-item
- User says "actually use the second recipe" → re-synthesizes from cached item #2
- Session ends → useful artifacts summarized into long-term memory

This is **working memory with structure**, not a flat string buffer. It applies to every interaction type: weather, file searches, reminders, news, recipes, research — everything.

---

## Prior Art Survey

### Tier 1: Directly Applicable Patterns

#### CogCanvas (arXiv 2601.00821, January 2026)
**Best conceptual match for within-conversation artifact decomposition.**

Extracts typed "cognitive artifacts" from every conversation turn:
- 5 types: Decision, Todo, KeyFact, Reminder, Insight
- Each artifact: `(type, content, quote, source, embedding, turn_index, confidence)`
- `quote` field = verbatim excerpt from source message (anti-hallucination)
- Graph-linked with 3 edge types:
  - Reference edges (semantic similarity > 0.5)
  - Causal edges (specific type pairs, similarity > 0.45, temporal constraints)
  - Temporal-heuristic edges (recent KeyFacts → subsequent Decisions)
- Retrieval: hybrid scoring (semantic + lexical), then 1-hop graph expansion

**Core thesis: Crystallization > Compression**
- 93% exact match on constraint preservation vs 19% for summarization
- "Recursive information decay" — summarizing "use type hints everywhere" → "prefers type hints" loses the critical quantifier
- 34.7% accuracy on LoCoMo benchmark (vs 25.6% RAG, 13.7% GraphRAG)
- 97.5% recall on information retention (+78.5pp vs summarization)
- Training-free, plug-and-play with any LLM backend

**Limitations:** Session-scoped only. 5 fixed artifact types. No consolidation/forgetting mechanism. No cross-session persistence.

#### MemGPT / Letta (UC Berkeley → commercial, arXiv 2310.08560)
**Best tiered lifetime model.**

OS-inspired memory architecture:
- **Core Memory (RAM):** Labeled blocks pinned to context window. Each block: label, description, value, character limit. Always visible to LLM.
- **Recall Memory (page file):** Complete interaction history, searchable but not in context.
- **Archival Memory (disk):** Vector DB for long-term knowledge. Explicitly formulated.

Agent self-manages via 6 tool calls: `core_memory_append`, `core_memory_replace`, `archival_memory_insert`, `archival_memory_search`, `conversation_search`, `conversation_search_date`.

Eviction: FIFO for conversation history. ~70% of messages evicted when buffer fills, with recursive summarization. The agent evaluates future value of information — important facts get high retention; transient content gets summarized.

Multi-agent: multiple agents can share the same `block_id`.

**Limitations:** All memory management through LLM = latency + token cost. Agent can make poor decisions. Single-agent focused.

#### OpenAI Agents SDK Memory Notes
**Best consolidation/promotion pattern.**

Simple structured notes:
```python
@dataclass
class MemoryNote:
    text: str                    # 1-2 sentence preference/constraint
    last_update_date: str        # ISO YYYY-MM-DD
    keywords: List[str]          # 1-3 topic tags (lowercase)
```

Precedence rules:
1. Current user input overrides everything
2. Session memory supersedes global on conflicts
3. Within same scope, most recent date wins

Session-end consolidation: async job merges session → global, deduplicates semantically, resolves conflicts by timestamp, filters ephemeral phrases ("this time", "right now").

**Limitations:** Text-only. Flat list. No graph structure.

### Tier 2: Architectural Patterns

#### Blackboard Architecture (1970s-80s, revived 2025)
**Maps to JARVIS's existing structure.**

Three components:
1. **Blackboard** — shared structured workspace, hierarchical levels of abstraction
2. **Knowledge Sources** — independent specialists that read/write the blackboard
3. **Control Component** — determines focus of attention (which KS fires next)

JARVIS mapping:
- Router = control component
- Skills/tools = knowledge sources
- Missing piece = the structured blackboard itself

2025 revival papers:
- Han & Zhang (arXiv 2507.01701): public/private blackboard spaces, LLM as control unit
- Data science blackboard (arXiv 2510.01285): 13-57% improvement over pipeline architectures

#### Google ADK Artifacts
**Closest production framework.**

Artifacts = binary/textual data with:
- Filename (identifier)
- MIME type
- Automatic versioning (each save = new version)
- Session-scoped or user-scoped (`"user:"` prefix)
- Storage backends: InMemory (dev) or GCS (production)

4 state scopes: session, user, app, invocation. Typed Event records for every interaction.

**Limitations:** File-oriented, not conversation-output-oriented. No semantic decomposition. No search (list + load by exact name only).

#### Dialogue State Tracking (DST)
**Most mature research on structured conversation state.**

Maintains belief state as `<domain, slot, value>` triples. Benchmark: MultiWOZ (7+ domains). Slots are categorical or open-text.

**Critical gap:** DST tracks *user goals*, not *system outputs*. Knows the user wants an Italian restaurant, but doesn't cache the 3 restaurants found as referenceable items.

#### Zep/Graphiti (arXiv 2501.13956, January 2025)
**Strongest temporal tracking.**

Three-tier graph: Episodes → Entities → Communities.

Bi-temporal model — every edge tracks 4 timestamps:
- `t'_created` / `t'_expired` (system transaction times)
- `t_valid` / `t_invalid` (real-world validity periods)

Enables: "what was true about X at time Y?" Automatically invalidates contradicted facts.

18.5% accuracy improvement, 90% latency reduction. Requires Neo4j.

#### A-Mem / Zettelkasten (NeurIPS 2025, arXiv 2502.12110)
**Best emergent organization pattern.**

Each note: content, timestamp, keywords, tags, contextual_description, embedding, links. Atomic (one idea per note), richly linked. New memories trigger retroactive updates to existing notes — the network continuously evolves.

No predefined schema. Structure emerges from content. Open source on GitHub.

### Tier 3: Design Constraints to Steal

#### Manus AI — Restorable Compression
If you have the URL → drop page content. If you have tool args → can re-execute. Only cache what can't be cheaply reconstructed. Error traces always retained.

#### CMA Specification (arXiv 2601.09913, January 2026)
Formal requirements checklist:
1. Persistence — fragments from days prior remain addressable
2. Selective Retention — memories compete (recency, usage, salience)
3. Retrieval-Driven Mutation — every lookup alters future accessibility
4. Associative Routing — structural connections enable multi-hop discovery
5. Temporal Continuity — explicit temporal edges
6. Consolidation & Abstraction — background conversion of episodes to semantic knowledge

Won 82/92 trials vs RAG. 2.4x latency overhead.

#### CrewAI Memory — Composite Scoring
Retrieval: `semantic_weight * similarity + recency_weight * decay + importance_weight * importance`. Consolidation threshold 0.85 prevents duplicates.

#### JetBrains Research — Observation Masking
Simple observation masking (replace older observations with placeholders) halves cost while matching LLM summarization solve rates. LLM summarization causes 13-15% longer trajectories. Hybrid approach (mask first, summarize near capacity) best.

#### RAISE Framework — Scratchpad for Voice
4-component working memory: conversation history, scratchpad (transient facts), examples, task trajectory. Tested on phone-based real estate dialogue — one of few voice-specific implementations.

### Commercial Voice Assistants — What They Do (and Don't Do)

**Google Assistant:** No result cache. Contextual rephrasing — rewrites "the second one" into a standalone query via ML model. Operates at string level, no typed registry underneath.

**Amazon Alexa:** Raw key-value state bag (JSON maps). Zero built-in reference resolution. Skill developers implement everything themselves.

**Key takeaway:** No commercial voice assistant caches tool outputs as discrete typed items.

---

## The Gap

**Nobody does exactly what we're describing.** The closest patterns:
1. DST slot-value tracking — structured/typed, but tracks user goals, not system outputs
2. Google ADK events + artifacts — typed events, artifacts by name/version
3. MemGPT core memory blocks — labeled, bounded, self-managed
4. CogCanvas cognitive artifacts — decomposed turns with graph linking
5. CrewAI unified memory — scored records with composite retrieval

The missing piece in ALL prior art: **a system that caches tool/skill outputs as first-class typed objects that can be referenced by ordinal ("the second one"), by type ("that recipe"), or by entity ("the Apache one") within a voice conversation.**

Voice-specific gaps in research:
- Sequential access patterns ("read me the next step", "go back")
- Sub-item navigation within a cached artifact ("skip to ingredients")
- TTS normalization on cached content
- Users can't scroll/skim — system must navigate for them

---

## Patterns to Emulate for JARVIS

### Data Model (from CogCanvas + A-Mem + Google ADK)
Each artifact: id, type, content, source_skill, timestamp, metadata, embedding, provenance links

### Tiered Lifetime (from MemGPT + CMA)
- Hot: in-context during active window
- Warm: session-scoped, retrievable (SQLite)
- Cold: summarized into long-term memory

### Reference Resolution (from DST + Google Assistant)
- Ordinal: "the second one" → items[1]
- Type-based: "that recipe" → most recent where type=recipe
- Recency: "the last search" → most recent where source=web_search
- Entity: "the Apache one" → entity match against content

### Summarization (from CogCanvas + OpenAI)
- Crystallize > compress
- Ephemeral filtering at promotion time
- Importance scoring for retention priority

### Restorable Compression (from Manus)
- URL available → can drop page content
- Tool args available → can re-execute
- Only persist what can't be cheaply reconstructed
