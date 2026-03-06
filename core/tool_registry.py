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

import base64
import importlib
import logging
import os
import time
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

TOOL_HANDLERS = {}        # tool_name -> handler function
SKILL_TOOLS = {}          # tool_name -> schema (skill-gated, for semantic pruning)
ALWAYS_INCLUDED_TOOLS = {}  # tool_name -> schema (always in every tool call)
ALL_TOOLS = {}            # tool_name -> schema (all tools)
TOOL_SKILL_MAP = {}       # skill_name -> list[str] of tool_names (for conversation_router pruner)
_external_prompt_rules = {}  # tool_name -> system prompt rule string (MCP tools)

for _mod in _tool_modules:
    _name = _mod.TOOL_NAME
    _handler = getattr(_mod, 'handler', None)
    _always = getattr(_mod, 'ALWAYS_INCLUDED', False)
    _skill = getattr(_mod, 'SKILL_NAME', None)

    ALL_TOOLS[_name] = _mod.SCHEMA

    if _handler is not None:
        TOOL_HANDLERS[_name] = _handler

    if _always:
        ALWAYS_INCLUDED_TOOLS[_name] = _mod.SCHEMA

    if _skill is not None:
        SKILL_TOOLS[_name] = _mod.SCHEMA
        TOOL_SKILL_MAP[_skill] = [_name]

# Log what we found
logger.info(
    f"Tool registry: {len(_tool_modules)} tools discovered, "
    f"{len(TOOL_HANDLERS)} with handlers, "
    f"{len(SKILL_TOOLS)} skill-gated, "
    f"{len(ALWAYS_INCLUDED_TOOLS)} always-included"
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
RECALL_MEMORY_TOOL = _get_schema("recall_memory")
TAKE_SCREENSHOT_TOOL = _get_schema("take_screenshot")
CAPTURE_WEBCAM_TOOL = _get_schema("capture_webcam")


# ---------------------------------------------------------------------------
# External tool registration (MCP servers)
# ---------------------------------------------------------------------------

def register_external_tool(name, schema, handler, system_prompt_rule, skill_name=None):
    """Register an externally-provided tool (e.g., from an MCP server).

    Args:
        name: Tool function name (must be unique across all tools).
        schema: OpenAI function-calling format schema dict.
        handler: Callable(args: dict) -> str.
        system_prompt_rule: LLM system prompt rule for this tool.
        skill_name: Optional virtual skill name (e.g. "mcp_email") for
                    semantic pruning.  One skill can map to multiple tools.
    """
    ALL_TOOLS[name] = schema
    TOOL_HANDLERS[name] = handler
    _external_prompt_rules[name] = system_prompt_rule

    if skill_name:
        SKILL_TOOLS[name] = schema
        existing = TOOL_SKILL_MAP.get(skill_name)
        if existing is None:
            TOOL_SKILL_MAP[skill_name] = [name]
        else:
            existing.append(name)

    logger.info(f"Registered external tool: {name}" +
                (f" (skill: {skill_name})" if skill_name else ""))


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

    # External (MCP) tool rules — deduplicate by rule text
    seen_rules = {r for r in rules}
    for tool_name, rule in _external_prompt_rules.items():
        if tool_name in active_tool_names and rule not in seen_rules:
            rules.append(rule)
            seen_rules.add(rule)

    rules.extend(_GLOBAL_RULES_SUFFIX)

    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    return (
        "You have access to tools that can retrieve local data. "
        "RULES — follow these EXACTLY:\n" + numbered
    )


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

_REGISTRY_READY = False  # Set True after inject_dependencies()


def parse_tool_result(result) -> tuple:
    """Extract text and optional image data from a tool handler result.

    Tool handlers may return a plain string or a dict with
    {"text": str, "image_data": str} for multimodal results (e.g. screenshots).

    Returns:
        (text: str, image_data: str | None)
    """
    if isinstance(result, dict):
        text = result.get("text", str(result))
        img = result.get("image_data")
        logger.debug("parse_tool_result: dict — text_len=%d has_image=%s%s",
                      len(text) if text else 0, bool(img),
                      f" img_len={len(img)}" if img else "")
        return text, img
    return result, None


_IMAGES_DIR = "/mnt/storage/jarvis/data/images"  # configurable via storage.images_path


def get_images_dir() -> str:
    """Return the configured images directory path."""
    return _IMAGES_DIR


def save_tool_image(image_data: str, tool_name: str) -> str:
    """Save base64-encoded image to disk.

    Args:
        image_data: Base64-encoded PNG data (no data: prefix).
        tool_name: Tool that produced the image (used in filename).

    Returns:
        Absolute path to the saved file.
    """
    logger.debug("save_tool_image: base64_input=%d bytes, tool=%s", len(image_data), tool_name)
    os.makedirs(_IMAGES_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{tool_name}.png"
    filepath = os.path.join(_IMAGES_DIR, filename)
    raw = base64.b64decode(image_data)
    with open(filepath, "wb") as f:
        f.write(raw)
    logger.info("Saved tool image: %s (%d bytes on disk)", filepath, len(raw))
    return filepath


def execute_tool(tool_name: str, arguments: dict) -> str | dict:
    """Dispatch a tool call to the appropriate handler.

    Args:
        tool_name: The tool function name from the LLM's tool_call.
        arguments: The parsed arguments dict.

    Returns:
        Plain-text result string, or dict with {"text", "image_data"}
        for multimodal tools. On error, returns an error description string.
    """
    if not _REGISTRY_READY:
        logger.warning("execute_tool called before inject_dependencies(): %s",
                        tool_name)
        return f"Error: tool registry not initialized yet"
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        logger.warning(f"Unknown tool: {tool_name}")
        return f"Error: unknown tool '{tool_name}'"
    _trunc_args = {k: (str(v)[:80] + "..." if len(str(v)) > 80 else v) for k, v in arguments.items()}
    logger.debug("execute_tool: %s(%s)", tool_name, _trunc_args)
    try:
        _t0 = time.time()
        result = handler(arguments)
        _elapsed = (time.time() - _t0) * 1000
        _rtype = type(result).__name__
        _rsize = len(str(result)) if result else 0
        logger.debug("execute_tool: %s returned %s (%d chars) in %.0fms",
                      tool_name, _rtype, _rsize, _elapsed)
        return result
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
    global _REGISTRY_READY, _IMAGES_DIR
    # Configure images directory from config if available
    config = deps.get("config")
    if config and hasattr(config, "get"):
        _IMAGES_DIR = config.get("storage.images_path", _IMAGES_DIR)
    for mod in _tool_modules:
        declared = getattr(mod, 'DEPENDENCIES', {})
        for dep_name, var_name in declared.items():
            if dep_name in deps and var_name:
                setattr(mod, var_name, deps[dep_name])
                logger.debug(f"Injected {dep_name} into {mod.TOOL_NAME}")
    _REGISTRY_READY = True
    logger.info("Tool registry ready — dependencies injected")
