# Semantic Intent Matching System

**Created:** February 2026
**Updated:** February 27, 2026
**Status:** Production — used for non-migrated skill routing (3 skills). Most queries now route via LLM tool calling (P4-LLM).

---

## Overview

JARVIS uses sentence-transformer embeddings (all-MiniLM-L6-v2, 23MB) for semantic similarity matching. This replaced brittle exact-pattern matching — 3-9 example phrases per intent instead of 100+ regex patterns.

With the LLM-centric migration (Phases 1-2, Feb 26-27), semantic matching now serves two roles:
1. **Skill routing** — matching queries to the 3 non-migrated skills (app_launcher, file_editor, social_introductions)
2. **Tool pruning** — the semantic pruner in `conversation_router.py` selects which LLM tools are relevant for each query

For the 8 migrated tools (time, system_info, filesystem, weather, reminders, developer_tools, news, web_search), Qwen3.5 handles tool selection natively via `stream_with_tools()`.

---

## Architecture

### Model

**Model:** `all-MiniLM-L6-v2`
- Size: 23MB on disk, ~100MB in RAM
- Speed: ~100ms inference on CPU
- Offline-capable, no API calls
- Cache: `/mnt/models/sentence-transformers/`

### Core Class: SemanticMatcher (`core/semantic_matcher.py`, 76 lines)

```python
class SemanticMatcher:
    def __init__(self, model_name="all-MiniLM-L6-v2", cache_dir=None)
    def register_intent(self, intent_id, examples, threshold=0.85)
    def match(self, query, default_threshold=0.85) -> (intent_id, score)
```

- Encodes all example phrases at registration time (pre-computed, cached)
- On match: encodes query, computes cosine similarity against all registered intent embeddings
- Returns best matching intent if above threshold, plus raw score (always returned for diagnostics)
- Per-intent thresholds supported (stored in `intent_thresholds` dict)

### Skill Registration (`core/base_skill.py`)

```python
self.register_semantic_intent(
    examples=["what cpu do i have", "show me my processor", "tell me about this computer's cpu"],
    handler=self.get_cpu_info,
    threshold=0.85,
    priority=5
)
```

- Intent ID format: `{ClassName}_{handler_name}` (e.g., `SystemInfoSkill_get_cpu_info`)
- Duplicate intent IDs log a warning — same handler registered twice silently overwrites
- Default threshold: 0.85 (85% cosine similarity)

### Default Dispatch (`base_skill.py:handle_intent()`)

The base class provides a concrete `handle_intent()` that dispatches via the `semantic_intents` dict:

```python
def handle_intent(self, intent, entities):
    if intent in self.semantic_intents:
        handler_fn = self.semantic_intents[intent]['handler']
        # Detect whether handler accepts entities param via inspect.signature
        if 'entities' in sig.parameters:
            return handler_fn(entities=entities or {})
        return handler_fn()
```

Skills with custom dispatch (file_editor, app_launcher, social_introductions) override this method.

---

## Skill Manager Matching Layers (`core/skill_manager.py`)

The `match_intent()` method uses a 4-layer hybrid approach. Each layer is tried in order; first match wins.

| Layer | Method | What | Speed |
|-------|--------|------|-------|
| 1 | Exact regex `.match()` | Pattern `^...$` against normalized text | <1ms |
| 2 | Fuzzy regex `.search()` | Pattern found anywhere in text | <1ms |
| 3 | `_match_by_keywords()` | Whole-word `\b` keyword count + alias bonus + suffix match + sub-semantic | ~1-5ms |
| 4 | `_match_semantic_intents()` | Cross-skill embedding similarity (all-MiniLM-L6-v2) | ~100ms |

### Layer 3 Details (Keyword Matching)

Layer 3 is the most complex, with multiple sub-steps:

1. **Keyword count** — word-boundary matching (`re.findall(r'\b\w+\b')`) against each skill's keywords
2. **Alias bonus** — "google", "codebase" get extra weight via `_keyword_aliases`
3. **Suffix match (4a)** — handler name suffix matches keyword (e.g., handler `_amazon` matches keyword "amazon")
4. **Keyword→semantic fallback (4b)** — within the matched skill, cosine similarity at relaxed threshold (0.7x)
5. **Generic keyword blocklist** — `_generic_keywords` set prevents common words from stealing queries: search, open, find, look, browse, navigate, web, file, code, directory, count, analyze

### Layer 4 Details (Global Semantic)

- Compares query embedding against ALL registered semantic intents across ALL skills
- Uses pre-computed embedding cache (`_semantic_embedding_cache`) — built at skill load time, no per-query re-encoding
- Threshold per intent (default 0.85)
- Returns best match above threshold

### Bare Generic Word Guard

Before any layer, a guard blocks queries that are ONLY generic keywords (e.g., bare "search" or "find"). These fall through to the LLM instead of matching a skill.

---

## Semantic Pruner (Tool Selection)

The semantic pruner in `conversation_router.py` (`_handle_tool_calling()`) determines which LLM tools are relevant for each query:

1. Encode query with the same sentence-transformer model
2. Compare against all skill semantic intents (both migrated and non-migrated)
3. **Threshold: 0.40** — sweep-tested across 56 queries at 7 thresholds (0.30-0.60). 0.40 = zero cliff risk + zero false negatives
4. **Hard cap: 4 domain tools** — safety net (never fires at 0.40 but guards future tool additions)
5. **Skill guard** — if a stateful skill (app_launcher, file_editor, social_introductions) scores higher than any tool, defer to skill routing
6. **web_search always included** — marked `ALWAYS_INCLUDED=True` in tool definition

The pruner reduces token cost by only including relevant tool schemas in the LLM prompt, rather than sending all 8 tools for every query.

---

## Embedding Cache

Pre-computed at skill load time in `skill_manager.load_skill()`:

```python
_semantic_embedding_cache[f"{skill_name}_{intent_id}"] = model.encode(examples)
```

Both Layer 4 (global semantic) and the semantic pruner use cached embeddings — no per-query re-encoding of intent examples. Only the user query is encoded per request (~100ms).

---

## Performance

| Metric | Value |
|--------|-------|
| Model load time | ~2s (first load, cached after) |
| Per-query encoding | ~100ms (CPU) |
| Embedding comparison (all intents) | <1ms |
| Total semantic match | ~100ms |
| Memory footprint | ~100MB |
| Registered intents | ~109 across all skills |

---

## Configuration

```yaml
# In config.yaml
semantic_matching:
  enabled: true
  model: "all-MiniLM-L6-v2"
  cache_dir: "/mnt/models/sentence-transformers"
  default_threshold: 0.85
  fallback_to_llm: true
```

---

## Current Role in Architecture

With Phase 2 complete, the semantic matching system's role has narrowed:

| Use Case | Status |
|----------|--------|
| **Tool pruning** (select which tools to send to LLM) | Active — core function |
| **Non-migrated skill routing** (app_launcher, file_editor, social_intros) | Active — 3 skills |
| **Migrated skill routing** (time, weather, system, etc.) | Replaced by LLM tool calling |

Phase 4 of the LLM-centric migration will evaluate whether the semantic matcher can be removed entirely once all skills are migrated or simplified. For now, it remains essential for tool pruning and the 3 non-migrated skills.

---

*Originally a proposal document (Feb 2026). Rewritten as reference documentation Feb 27, 2026 after system reached production stability.*
