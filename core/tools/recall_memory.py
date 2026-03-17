"""Tool definition: recall_memory — search personal memory for previously learned facts."""

TOOL_NAME = "recall_memory"
SKILL_NAME = None  # Not skill-gated
ALWAYS_INCLUDED = True

DEPENDENCIES = {
    "memory_manager": "_memory_manager",
    "current_user_fn": "_current_user_fn",
}

# Injected at runtime via inject_dependencies()
_memory_manager = None
_current_user_fn = None

SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": (
            "Search personal memory for facts previously learned about the user. "
            "Use when answering a question that might relate to something the user "
            "told you before (preferences, relationships, habits, plans, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in memory (e.g. 'favorite color', 'birthday', 'work')"
                }
            },
            "required": ["query"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "Call recall_memory when answering a question that might relate to something "
    "the user told you before. DO NOT call it for every message — only when past "
    "knowledge would genuinely help your answer. DO NOT call it for greetings or "
    "small talk. Do NOT use recall_memory to update, modify, cancel, or delete "
    "reminders — use manage_reminders instead."
)


def handler(args: dict) -> str:
    """Search memory for facts matching the query."""
    if _memory_manager is None:
        return "Error: memory system not initialized"

    query = args.get("query", "").strip()
    if not query:
        return "Error: query parameter is required"

    user_id = _current_user_fn() if _current_user_fn else "primary_user"

    # Combine text and semantic search, deduplicate by fact_id
    text_results = _memory_manager.search_facts_text(query, user_id)
    semantic_results = _memory_manager._search_facts_semantic(query, user_id, top_k=5)

    seen_ids = set()
    combined = []
    for fact in text_results + semantic_results:
        fid = fact.get("fact_id")
        if fid and fid not in seen_ids:
            seen_ids.add(fid)
            combined.append(fact)
        if len(combined) >= 5:
            break

    # Accumulate recalled fact IDs for broad forget scoping ("forget all of that").
    # Threshold filters out low-relevance results; accumulation preserves IDs from
    # earlier recalls in the same conversation window so nothing discussed is lost.
    _FORGET_SCORE_THRESHOLD = 0.45
    _new_ids = [
        f["fact_id"] for f in combined
        if "fact_id" in f
        and f.get("score", 1.0) >= _FORGET_SCORE_THRESHOLD
    ]
    _existing = set(_memory_manager._last_recalled_fact_ids)
    _memory_manager._last_recalled_fact_ids.extend(
        fid for fid in _new_ids if fid not in _existing
    )

    if not combined:
        return "No memories found matching that query."

    lines = []
    for f in combined:
        # Convert to natural second-person phrasing; fall back to raw content
        # with the user's name stripped to avoid "the user prefers X" leaking
        phrase = _memory_manager._fact_to_phrase(f)
        if not phrase:
            phrase = f.get("content", "")
            # Strip subject name prefix to avoid "the user owns X" in output
            subject = f.get("subject", "")
            if subject:
                phrase = phrase.replace(subject, "").strip(" ,.")
        confidence = f.get("confidence", 0)
        lines.append(f"{phrase} (confidence: {confidence:.0%})")

    return "\n".join(lines)
