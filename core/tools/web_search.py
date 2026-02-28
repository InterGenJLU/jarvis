"""Tool definition: web_search — always-included, frontend-dispatched."""

TOOL_NAME = "web_search"
SKILL_NAME = None  # Not skill-gated
ALWAYS_INCLUDED = True

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use this for ANY factual question "
            "about the real world: distances, people, events, news, scores, prices, "
            "statistics, locations, travel times, or anything requiring accurate data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up"
                }
            },
            "required": ["query"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For factual questions about the OUTSIDE WORLD (people, "
    "events, specific news topics, scores, prices, etc.), call web_search."
)

handler = None  # Dispatched by frontends (WebResearcher.search())
