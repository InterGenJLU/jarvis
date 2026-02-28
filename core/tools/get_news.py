"""Tool definition: get_news — RSS news headline retrieval."""

TOOL_NAME = "get_news"
SKILL_NAME = "news"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": (
            "Read RSS news headlines already collected by the local feed monitor. "
            "Use for requests about news headlines, news updates, or headline counts. "
            "NOT for searching the web for specific news topics — use web_search for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "count"],
                    "description": (
                        "read: read top unread headlines (optionally filtered). "
                        "count: count unread headlines by category."
                    )
                },
                "category": {
                    "type": "string",
                    "enum": ["tech", "cyber", "politics", "general", "local"],
                    "description": "Filter to a specific news category. Omit for all categories."
                },
                "max_priority": {
                    "type": "integer",
                    "enum": [1, 2, 3],
                    "description": (
                        "Filter by urgency: 1=critical only, 2=critical+high, "
                        "3=critical+high+normal. Omit for all priorities."
                    )
                }
            },
            "required": ["action"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For news headline requests (read headlines, news updates, "
    "headline counts), call get_news. This retrieves LOCAL RSS feed "
    "headlines — NOT web search results. If the user asks about a "
    "SPECIFIC news topic (e.g. 'latest news about SpaceX'), use "
    "web_search instead."
)


def handler(args: dict) -> str:
    """Read or count RSS news headlines from the local feed monitor."""
    from core.news_manager import get_news_manager
    mgr = get_news_manager()
    if not mgr:
        return "News system is not available."
    action = args.get("action", "read")
    category = args.get("category")
    max_priority = args.get("max_priority")
    if action == "count":
        return mgr.get_headline_count_response()
    return mgr.read_headlines(category=category, limit=5, max_priority=max_priority)
