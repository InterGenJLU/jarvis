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
    "small talk."
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

    if not combined:
        return "No memories found matching that query."

    lines = []
    for f in combined:
        cat = f.get("category", "general")
        content = f.get("content", "")
        confidence = f.get("confidence", 0)
        lines.append(f"[{cat}] {content} (confidence: {confidence:.0%})")

    return "\n".join(lines)
