"""Tool definitions for LLM-centric tool calling.

Each .py file in this package defines one tool with standardized attributes:
    TOOL_NAME: str           -- OpenAI function name
    SKILL_NAME: str | None   -- Semantic matcher skill name (for pruning)
    SCHEMA: dict             -- OpenAI-compatible tool schema
    SYSTEM_PROMPT_RULE: str  -- Per-tool rule for LLM system prompt
    handler(args) -> str     -- Tool execution function (None for frontend-dispatched)

Optional:
    ALWAYS_INCLUDED: bool    -- True if tool is always in tool list (default False)
    DEPENDENCIES: dict       -- {dep_name: module_var_name} for runtime injection
"""
