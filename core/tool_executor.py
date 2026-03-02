"""Tool Executor — backward-compatibility shim.

All tool definitions now live in core/tools/*.py.
The registry (core/tool_registry.py) auto-discovers and assembles them.
This module preserves the existing import API for frontends:
    - execute_tool(tool_name, arguments) -> str
    - set_reminder_manager(mgr)
    - set_config(config)
    - set_current_user_fn(fn)
"""

from core.tool_registry import execute_tool, inject_dependencies  # noqa: F401


def set_reminder_manager(mgr):
    """Wire the reminder manager singleton for tool dispatch.

    Called during init in pipeline.py, jarvis_console.py, jarvis_web.py.
    """
    inject_dependencies({"reminder_manager": mgr})


def set_config(config):
    """Wire the config object for tool dispatch (health check, etc.).

    Called during init in pipeline.py, jarvis_console.py, jarvis_web.py.
    """
    inject_dependencies({"config": config})


def set_current_user_fn(fn):
    """Wire a callable that returns the active user_id (e.g. 'christopher', 'erica').

    Used by manage_reminders tool to stamp created_by on new reminders.
    Called during init in pipeline.py, jarvis_console.py, jarvis_web.py.
    """
    inject_dependencies({"current_user_fn": fn})
