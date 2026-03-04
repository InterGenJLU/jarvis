"""JARVIS MCP Server — exposes native tools to Claude Code via stdio.

Standalone FastMCP server process. Wraps each native tool handler as an
MCP tool function. Runs on stdio transport (default for Claude Code).

Usage:
    python mcp_server.py          # stdio (for Claude Code / Claude Desktop)

Excluded tools:
    - web_search: Claude Code has its own; handler is None
    - developer_tools actions: run_command, confirm_pending (security)
"""

import logging
import os
import sys

# Ensure project root is on sys.path for core.* imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Redirect all JARVIS logging to file — stdout must stay clean for JSON-RPC
os.environ["JARVIS_LOG_FILE_ONLY"] = "1"
os.environ["JARVIS_LOG_TARGET"] = "mcp_server"

from mcp.server.fastmcp import FastMCP

from core.config import load_config
from core.tool_registry import TOOL_HANDLERS, inject_dependencies

logger = logging.getLogger("jarvis.mcp_server")

# ---------------------------------------------------------------------------
# Lightweight init — only what tools need, no TTS/STT/VAD
# ---------------------------------------------------------------------------

config = load_config()

# Inject config for developer_tools
inject_dependencies({"config": config})

# Reminder manager — shares SQLite DB with running JARVIS (WAL concurrency)
try:
    from core.reminder_manager import get_reminder_manager
    reminder_mgr = get_reminder_manager(config, tts=None, conversation=None)
    if reminder_mgr:
        inject_dependencies({"reminder_manager": reminder_mgr})
        logger.info("Reminder manager initialized for MCP")
except Exception as e:
    logger.warning(f"Reminder manager unavailable: {e}")

# Memory manager — read-only semantic search against shared SQLite + FAISS
try:
    from core.memory_manager import get_memory_manager
    memory_mgr = get_memory_manager(config, conversation=None, embedding_model=None)
    if memory_mgr:
        inject_dependencies({"memory_manager": memory_mgr})
        logger.info("Memory manager initialized for MCP")
except Exception as e:
    logger.warning(f"Memory manager unavailable: {e}")

# Current user — MCP context is always the primary user
inject_dependencies({"current_user_fn": lambda: "primary_user"})


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "jarvis",
    instructions=(
        "JARVIS personal assistant tools. Access reminders, weather, "
        "system info, files, developer tools, news, and personal memory."
    ),
)


# ---------------------------------------------------------------------------
# Tool wrappers — delegate to native handlers
# ---------------------------------------------------------------------------

def _safe_call(tool_name: str, args: dict) -> str:
    """Wrap a native tool handler with error protection for MCP transport."""
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return f"Error: tool '{tool_name}' not available"
    try:
        return handler(args)
    except Exception as e:
        logger.error("MCP tool '%s' raised: %s", tool_name, e)
        return f"Error in {tool_name}: {type(e).__name__}: {e}"


@mcp.tool()
def get_weather(query_type: str, location: str = "") -> str:
    """Get current weather, forecast, or rain check for a location.

    Args:
        query_type: One of: current, forecast, tomorrow, rain_check
        location: City or location name. Omit for the default location.
    """
    return _safe_call("get_weather",
                       {"query_type": query_type, "location": location})


@mcp.tool()
def get_system_info(category: str) -> str:
    """Get information about this computer's hardware or OS.

    Args:
        category: One of: cpu, memory, disk, gpu, uptime, hostname, username, all_drives
    """
    return _safe_call("get_system_info", {"category": category})


@mcp.tool()
def find_files(action: str, pattern: str = "", directory: str = "") -> str:
    """Search for files on the local filesystem by name or pattern.

    Args:
        action: One of: search, count_files, count_code
        pattern: Filename or glob pattern (for search action)
        directory: Directory name to search in (e.g. 'documents', 'downloads')
    """
    args = {"action": action}
    if pattern:
        args["pattern"] = pattern
    if directory:
        args["directory"] = directory
    return _safe_call("find_files", args)


@mcp.tool()
def manage_reminders(
    action: str,
    title: str = "",
    time_text: str = "",
    priority: str = "normal",
    snooze_minutes: int = 15,
    cancel_fragment: str = "",
) -> str:
    """Manage reminders: set new ones, list existing, cancel, acknowledge, or snooze.

    Args:
        action: One of: add, list, cancel, acknowledge, snooze
        title: What to be reminded about (required for 'add')
        time_text: When to remind, in natural language (required for 'add')
        priority: Importance level: urgent, high, or normal
        snooze_minutes: Minutes to snooze (default 15)
        cancel_fragment: Part of the reminder title to match for cancellation
    """
    args = {"action": action}
    if title:
        args["title"] = title
    if time_text:
        args["time_text"] = time_text
    if priority != "normal":
        args["priority"] = priority
    if snooze_minutes != 15:
        args["snooze_minutes"] = snooze_minutes
    if cancel_fragment:
        args["cancel_fragment"] = cancel_fragment
    return _safe_call("manage_reminders", args)


# developer_tools — block run_command and confirm_pending
_BLOCKED_DEV_ACTIONS = {"run_command", "confirm_pending"}


@mcp.tool()
def developer_tools(
    action: str,
    repo: str = "",
    count: int = 0,
    pattern: str = "",
    sort_by: str = "",
    service_name: str = "",
    info_type: str = "",
    target: str = "",
    package_name: str = "",
    filter: str = "",
    minutes: int = 0,
) -> str:
    """Developer and system administration operations.

    Supports: git_status, git_log, git_diff, git_branch, codebase_search,
    process_info, service_status, network_info, package_info, system_health,
    check_logs. (run_command and confirm_pending are blocked via MCP.)

    Args:
        action: The operation to perform (see above)
        repo: Which git repo: main, skills, models, or all
        count: Number of log entries for git_log
        pattern: Search pattern for codebase_search
        sort_by: Sort order for process_info: cpu or memory
        service_name: Service name for service_status
        info_type: Type of network info: addresses, ports, ping, interfaces
        target: Hostname or IP to ping
        package_name: Package name for package_info
        filter: Log filter: recent, errors, or warnings
        minutes: Minutes of logs to show for check_logs
    """
    if action in _BLOCKED_DEV_ACTIONS:
        return f"Action '{action}' is not available via MCP."

    args = {"action": action}
    if repo:
        args["repo"] = repo
    if count:
        args["count"] = count
    if pattern:
        args["pattern"] = pattern
    if sort_by:
        args["sort_by"] = sort_by
    if service_name:
        args["service_name"] = service_name
    if info_type:
        args["info_type"] = info_type
    if target:
        args["target"] = target
    if package_name:
        args["package_name"] = package_name
    if filter:
        args["filter"] = filter
    if minutes:
        args["minutes"] = minutes
    return _safe_call("developer_tools", args)


@mcp.tool()
def get_news(action: str, category: str = "", max_priority: int = 0) -> str:
    """Read RSS news headlines already collected by the local feed monitor.

    Args:
        action: One of: read, count
        category: Filter by: tech, cyber, politics, general, local. Omit for all.
        max_priority: Filter by urgency: 1=critical, 2=critical+high, 3=all. Omit for all.
    """
    args = {"action": action}
    if category:
        args["category"] = category
    if max_priority:
        args["max_priority"] = max_priority
    return _safe_call("get_news", args)


@mcp.tool()
def recall_memory(query: str) -> str:
    """Search personal memory for facts previously learned about the user.

    Use when answering a question that might relate to something the user
    mentioned before (preferences, relationships, habits, plans, etc.).

    Args:
        query: What to search for in memory (e.g. 'favorite color', 'birthday')
    """
    return _safe_call("recall_memory", {"query": query})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
