"""Tool Registry — auto-discovers tool definitions and builds all registries.

Each tool is a Python module in core/tools/ with standardized attributes.
This module scans that package at import time and assembles:
    - TOOL_HANDLERS: dict mapping tool_name -> handler function
    - SKILL_TOOLS:   dict mapping tool_name -> schema (skill-gated only)
    - ALL_TOOLS:     dict mapping tool_name -> schema (all tools)
    - TOOL_SKILL_MAP: dict mapping skill_name -> tool_name (for semantic pruner)
    - Individual schema constants (WEB_SEARCH_TOOL, GET_TIME_TOOL, etc.)
    - build_tool_prompt_rules(): assembles numbered rules for LLM system prompt
    - execute_tool(): dispatches tool calls to handlers
    - inject_dependencies(): wires runtime objects into tool modules
"""

import importlib
import logging
from pathlib import Path

logger = logging.getLogger("jarvis.tool_registry")


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

_tool_modules = []  # List of imported tool modules

_tools_dir = Path(__file__).parent / "tools"
for _path in sorted(_tools_dir.glob("*.py")):
    if _path.name.startswith("_"):
        continue
    _mod_name = f"core.tools.{_path.stem}"
    try:
        _mod = importlib.import_module(_mod_name)
        # Validate required attributes
        for _attr in ("TOOL_NAME", "SCHEMA", "SYSTEM_PROMPT_RULE"):
            if not hasattr(_mod, _attr):
                logger.error(f"Tool module {_mod_name} missing required attribute: {_attr}")
                continue
        _tool_modules.append(_mod)
    except Exception as _e:
        logger.error(f"Failed to load tool module {_mod_name}: {_e}")


# ---------------------------------------------------------------------------
# Build registries from discovered modules
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {}   # tool_name -> handler function
SKILL_TOOLS = {}     # tool_name -> schema (skill-gated, for semantic pruning)
ALL_TOOLS = {}       # tool_name -> schema (all tools)
TOOL_SKILL_MAP = {}  # skill_name -> tool_name (for conversation_router pruner)

for _mod in _tool_modules:
    _name = _mod.TOOL_NAME
    _handler = getattr(_mod, 'handler', None)
    _always = getattr(_mod, 'ALWAYS_INCLUDED', False)
    _skill = getattr(_mod, 'SKILL_NAME', None)

    ALL_TOOLS[_name] = _mod.SCHEMA

    if _handler is not None:
        TOOL_HANDLERS[_name] = _handler

    if _skill is not None:
        SKILL_TOOLS[_name] = _mod.SCHEMA
        TOOL_SKILL_MAP[_skill] = _name

# Log what we found
logger.info(
    f"Tool registry: {len(_tool_modules)} tools discovered, "
    f"{len(TOOL_HANDLERS)} with handlers, "
    f"{len(SKILL_TOOLS)} skill-gated"
)


# ---------------------------------------------------------------------------
# Individual schema constants (backward compatibility for imports)
# ---------------------------------------------------------------------------

def _get_schema(tool_name: str) -> dict:
    """Look up a schema by tool name, returning empty dict if missing."""
    return ALL_TOOLS.get(tool_name, {})

WEB_SEARCH_TOOL = _get_schema("web_search")
GET_SYSTEM_INFO_TOOL = _get_schema("get_system_info")
FIND_FILES_TOOL = _get_schema("find_files")
GET_WEATHER_TOOL = _get_schema("get_weather")
MANAGE_REMINDERS_TOOL = _get_schema("manage_reminders")
DEVELOPER_TOOLS_TOOL = _get_schema("developer_tools")
GET_NEWS_TOOL = _get_schema("get_news")


# ---------------------------------------------------------------------------
# System prompt rules assembly
# ---------------------------------------------------------------------------

_GLOBAL_RULES_PREFIX = [
    "If a tool matches the user's request, ALWAYS call it — "
    "even if you think you already know the answer. Tools return "
    "live data; your knowledge may be stale.",
]

_GLOBAL_RULES_SUFFIX = [
    "If the user asks for MULTIPLE things (e.g. 'time and weather'), "
    "call ALL relevant tools — one at a time.",
    "For greetings, small talk, casual questions (e.g. 'everything ok', "
    "'what's going on', 'what's happening', 'how are you'), creative "
    "requests, opinions, and follow-up elaborations, answer directly "
    "without any tool.",
    "NEVER fabricate system info, file paths, or hardware specs. "
    "If unsure, call the tool.",
]


def build_tool_prompt_rules(active_tool_names: set) -> str:
    """Assemble numbered system prompt rules for the active tool set.

    Args:
        active_tool_names: Set of tool names in the current tool list
                          (e.g. {"web_search", "get_time", "get_weather"})

    Returns:
        Complete rules block including preamble, per-tool rules, and suffix.
    """
    rules = list(_GLOBAL_RULES_PREFIX)

    # Per-tool rules — only for tools that are active
    for mod in _tool_modules:
        if mod.TOOL_NAME in active_tool_names and mod.SYSTEM_PROMPT_RULE:
            rules.append(mod.SYSTEM_PROMPT_RULE)

    rules.extend(_GLOBAL_RULES_SUFFIX)

    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    return (
        "You have access to tools that can retrieve local data. "
        "RULES — follow these EXACTLY:\n" + numbered
    )


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, arguments: dict) -> str:
    """Dispatch a tool call to the appropriate handler.

    Args:
        tool_name: The tool function name from the LLM's tool_call.
        arguments: The parsed arguments dict.

    Returns:
        Plain-text result string for the LLM to synthesize.
        On error, returns an error description (not an exception).
    """
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        logger.warning(f"Unknown tool: {tool_name}")
        return f"Error: unknown tool '{tool_name}'"
    try:
        return handler(arguments)
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return f"Error executing {tool_name}: {e}"


# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

def inject_dependencies(deps: dict):
    """Inject runtime dependencies into tool modules that declare them.

    Tool modules declare dependencies via a DEPENDENCIES dict mapping
    dependency names to module-level variable names:

        DEPENDENCIES = {"reminder_manager": "_reminder_manager"}
        _reminder_manager = None  # Set at runtime

    Args:
        deps: Mapping of dependency name -> runtime object.
              e.g. {"reminder_manager": <ReminderManager>, "config": <Config>}
    """
    for mod in _tool_modules:
        declared = getattr(mod, 'DEPENDENCIES', {})
        for dep_name, var_name in declared.items():
            if dep_name in deps and var_name:
                setattr(mod, var_name, deps[dep_name])
                logger.debug(f"Injected {dep_name} into {mod.TOOL_NAME}")
