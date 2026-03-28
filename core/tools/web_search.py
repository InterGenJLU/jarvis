"""Tool definition: web_search — always-included, frontend-dispatched."""

TOOL_NAME = "web_search"
SKILL_NAME = None  # Not skill-gated
ALWAYS_INCLUDED = True

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for CURRENT or REAL-TIME information only. "
            "Use for: breaking news, live scores, stock prices, current events, "
            "recent product releases, event dates/tickets, travel times, "
            "local business info, recent statistics, things that change frequently. "
            "Do NOT use for: general knowledge, definitions, explanations, "
            "how things work, history, science facts, math, coding help, "
            "or follow-ups to questions already answered in this conversation."
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
    "Call web_search for CURRENT or SPECIFIC real-world data: "
    "breaking news, live scores, stock prices, product reviews, event dates, "
    "weather alerts, people in the news, recent releases, local businesses, "
    "what a person or company has recently done, invested in, or announced. "
    "Do NOT search for timeless general knowledge (science facts, "
    "history, definitions, how things work). "
    "When building the query, strip conversational filler ('can you find me', "
    "'I need', 'please'). Example: 'Can you find me a good pizza recipe?' "
    "→ query: 'best homemade pizza recipe'. "
    "NOT for: opinions, creative writing, math, coding help, local system info. "
    "Do NOT use for questions about the user's LOCAL files, folders, directories, "
    "codebase, project structure, or git repos — use find_files or developer_tools instead. "
    "Example: 'search the codebase for X' → developer_tools codebase_search, NOT web_search."
)

handler = None  # Dispatched by frontends (WebResearcher.search())
