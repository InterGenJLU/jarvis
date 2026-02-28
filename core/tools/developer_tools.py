"""Tool definition: developer_tools — git, codebase search, system admin."""

import logging
import subprocess
import time as _time

TOOL_NAME = "developer_tools"
SKILL_NAME = "developer_tools"

DEPENDENCIES = {"config": "_config"}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "developer_tools",
        "description": (
            "Developer and system administration operations: git status/log/diff/branch "
            "across repos, codebase search, process info, service status, network info, "
            "package info, system health check, service logs, or run a shell command. "
            "Only use when the user explicitly asks about git, code, processes, services, "
            "network, packages, logs, or shell commands. NOT for casual conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "git_status", "git_log", "git_diff", "git_branch",
                        "codebase_search", "process_info", "service_status",
                        "network_info", "package_info", "system_health",
                        "check_logs", "run_command", "confirm_pending",
                    ],
                    "description": (
                        "git_status: show git status across repos. "
                        "git_log: show recent commits. "
                        "git_diff: show uncommitted changes. "
                        "git_branch: list branches. "
                        "codebase_search: grep for a pattern in core/ and skills/. "
                        "process_info: top processes by CPU or memory. "
                        "service_status: check a systemd service or list JARVIS services. "
                        "network_info: IP addresses, open ports, ping, or interfaces. "
                        "package_info: check installed package version. "
                        "system_health: run full 5-layer health diagnostic. "
                        "check_logs: view recent JARVIS service logs. "
                        "run_command: execute a shell command (safety-classified). "
                        "confirm_pending: execute a previously suggested command that "
                        "required confirmation. Use when the user says 'yes', 'go ahead', "
                        "'proceed' after being asked to confirm."
                    )
                },
                "repo": {
                    "type": "string",
                    "enum": ["main", "skills", "models", "all"],
                    "description": "Which git repo (for git_* actions). Default: all."
                },
                "count": {
                    "type": "integer",
                    "description": "Number of log entries (for git_log). Default: 10, max 50."
                },
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (for codebase_search)."
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["cpu", "memory"],
                    "description": "Sort order for process_info. Default: cpu."
                },
                "service_name": {
                    "type": "string",
                    "description": "Service name (for service_status). Omit to list JARVIS services."
                },
                "info_type": {
                    "type": "string",
                    "enum": ["addresses", "ports", "ping", "interfaces"],
                    "description": "Type of network info. Default: addresses."
                },
                "target": {
                    "type": "string",
                    "description": "Hostname or IP to ping (for network_info with info_type=ping)."
                },
                "package_name": {
                    "type": "string",
                    "description": "Package name to look up (for package_info)."
                },
                "filter": {
                    "type": "string",
                    "enum": ["recent", "errors", "warnings"],
                    "description": "Log filter (for check_logs). Default: recent."
                },
                "minutes": {
                    "type": "integer",
                    "description": "How many minutes of logs to show (for check_logs). Default: 15."
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (for run_command)."
                }
            },
            "required": ["action"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For developer operations (git, codebase search, processes, "
    "services, network info, packages, logs, or shell commands), call "
    "developer_tools. For codebase_search, extract the pattern. For "
    "run_command, provide the exact shell command."
)

logger = logging.getLogger("jarvis.tools.developer_tools")


# ---------------------------------------------------------------------------
# Runtime dependency — injected via tool_registry.inject_dependencies()
# ---------------------------------------------------------------------------

_config = None


# ---------------------------------------------------------------------------
# Lazy-loaded safety module
# ---------------------------------------------------------------------------

_safety_module = None


def _get_safety():
    """Lazy-load _safety.py from the developer_tools skill directory."""
    global _safety_module
    if _safety_module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_safety',
            '/mnt/storage/jarvis/skills/system/developer_tools/_safety.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _safety_module = mod
    return _safety_module


# ---------------------------------------------------------------------------
# Git repo paths
# ---------------------------------------------------------------------------

_GIT_REPOS = {
    'main': '/home/user/jarvis',
    'skills': '/mnt/storage/jarvis/skills',
    'models': '/mnt/models',
}


def _resolve_repos(repo: str) -> dict:
    """Return dict of repo_name->path for the requested repo(s)."""
    if repo == 'all' or not repo:
        return dict(_GIT_REPOS)
    if repo in _GIT_REPOS:
        return {repo: _GIT_REPOS[repo]}
    return dict(_GIT_REPOS)


def _run_cmd(cmd: str, cwd: str = None, timeout: int = 15) -> str:
    """Run a shell command and return stdout (or error message)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            output += f"\n{result.stderr.strip()}" if output else result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Pending confirmation state for run_command -> confirm_pending flow
# ---------------------------------------------------------------------------

_pending_command = None  # (command_str, expiry_time) or None


# ---------------------------------------------------------------------------
# Sub-handler registry
# ---------------------------------------------------------------------------

_DEVTOOLS_HANDLERS = {}


def _register_devtool(action: str):
    """Decorator to register a developer_tools action handler."""
    def decorator(fn):
        _DEVTOOLS_HANDLERS[action] = fn
        return fn
    return decorator


def handler(args: dict) -> str:
    """Route to the appropriate developer_tools action handler."""
    action = args.get("action", "")
    sub_handler = _DEVTOOLS_HANDLERS.get(action)
    if not sub_handler:
        available = ", ".join(sorted(_DEVTOOLS_HANDLERS.keys()))
        return f"Unknown developer_tools action '{action}'. Available: {available}"
    return sub_handler(args)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

@_register_devtool("git_status")
def _devtools_git_status(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git status --short", cwd=path)
        if output == "(no output)":
            output = "clean"
        lines.append(f"[{name}] ({path}):\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_log")
def _devtools_git_log(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    count = max(1, min(50, int(args.get("count", 10))))
    lines = []
    for name, path in repos.items():
        output = _run_cmd(f"git log --oneline --decorate -{count}", cwd=path)
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_diff")
def _devtools_git_diff(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git diff", cwd=path)
        if output == "(no output)":
            output = "no changes"
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("git_branch")
def _devtools_git_branch(args: dict) -> str:
    repos = _resolve_repos(args.get("repo", "all"))
    lines = []
    for name, path in repos.items():
        output = _run_cmd("git branch -a", cwd=path)
        lines.append(f"[{name}]:\n{output}")
    return "\n\n".join(lines)


@_register_devtool("codebase_search")
def _devtools_codebase_search(args: dict) -> str:
    pattern = args.get("pattern", "").strip()
    if not pattern:
        return "Error: 'pattern' is required for codebase search."
    search_dirs = [
        '/home/user/jarvis/core',
        '/mnt/storage/jarvis/skills',
    ]
    all_matches = []
    for d in search_dirs:
        output = _run_cmd(
            f"grep -rn --include='*.py' "
            f"--exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=venv "
            f"-- {subprocess.list2cmdline([pattern])} {d}",
            timeout=10,
        )
        if output and output != "(no output)" and not output.startswith("Error"):
            all_matches.extend(output.split('\n'))
    if not all_matches:
        return f"No matches found for '{pattern}'."
    if len(all_matches) > 30:
        truncated = all_matches[:30]
        truncated.append(f"... ({len(all_matches) - 30} more matches)")
        return "\n".join(truncated)
    return "\n".join(all_matches)


@_register_devtool("process_info")
def _devtools_process_info(args: dict) -> str:
    sort_by = args.get("sort_by", "cpu")
    sort_key = "-%mem" if sort_by == "memory" else "-%cpu"
    return _run_cmd(f"ps aux --sort={sort_key} | head -15")


@_register_devtool("service_status")
def _devtools_service_status(args: dict) -> str:
    service_name = args.get("service_name", "").strip()
    if service_name:
        # Try user service first, then system
        output = _run_cmd(f"systemctl --user status {subprocess.list2cmdline([service_name])}")
        if "could not be found" in output.lower() or "not loaded" in output.lower():
            output = _run_cmd(f"systemctl status {subprocess.list2cmdline([service_name])}")
        return output
    # List JARVIS-related services
    lines = ["User services:"]
    for svc in ["jarvis", "jarvis-web", "llama-server"]:
        status = _run_cmd(f"systemctl --user is-active {svc} 2>/dev/null")
        lines.append(f"  {svc}: {status}")
    lines.append("\nSystem services:")
    status = _run_cmd("systemctl is-active llama-server 2>/dev/null")
    lines.append(f"  llama-server: {status}")
    return "\n".join(lines)


@_register_devtool("network_info")
def _devtools_network_info(args: dict) -> str:
    info_type = args.get("info_type", "addresses")
    target = args.get("target", "")
    if info_type == "ports":
        return _run_cmd("ss -tlnp")
    elif info_type == "ping" and target:
        return _run_cmd(f"ping -c 4 {subprocess.list2cmdline([target])}", timeout=10)
    elif info_type == "interfaces":
        return _run_cmd("ip link show")
    # Default: addresses
    return _run_cmd("ip -brief addr show")


@_register_devtool("package_info")
def _devtools_package_info(args: dict) -> str:
    package_name = args.get("package_name", "").strip()
    if package_name:
        lines = []
        which = _run_cmd(f"which {subprocess.list2cmdline([package_name])}")
        if which and not which.startswith("Error") and which != "(no output)":
            lines.append(f"Location: {which}")
        version = _run_cmd(f"{subprocess.list2cmdline([package_name])} --version 2>&1 | head -1")
        if version and not version.startswith("Error") and version != "(no output)":
            lines.append(f"Version: {version}")
        pip_info = _run_cmd(f"pip show {subprocess.list2cmdline([package_name])} 2>/dev/null")
        if pip_info and not pip_info.startswith("Error") and pip_info != "(no output)":
            lines.append(f"Pip info:\n{pip_info}")
        return "\n".join(lines) if lines else f"Package '{package_name}' not found."
    # General info
    return _run_cmd("python3 --version && pip --version")


@_register_devtool("system_health")
def _devtools_system_health(args: dict) -> str:
    if not _config:
        return "Error: config not initialized. Cannot run health check."
    try:
        from core.health_check import get_full_health
        results = get_full_health(_config)
        lines = []
        for layer_name, checks in results.items():
            lines.append(f"=== {layer_name.upper()} ===")
            for check in checks:
                status = check.get('status', 'unknown')
                name = check.get('name', 'unknown')
                detail = check.get('detail', '')
                icon = {'green': 'OK', 'yellow': 'WARN', 'red': 'FAIL'}.get(status, '??')
                lines.append(f"  [{icon}] {name}: {detail}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error running health check: {e}"


@_register_devtool("check_logs")
def _devtools_check_logs(args: dict) -> str:
    log_filter = args.get("filter", "recent")
    minutes = max(1, min(1440, int(args.get("minutes", 15))))
    cmd = f'journalctl --user -u jarvis --since "{minutes} min ago" --no-pager'
    if log_filter == "errors":
        cmd += " | grep -i error"
    elif log_filter == "warnings":
        cmd += " | grep -iE '(warn|error)'"
    output = _run_cmd(cmd, timeout=10)
    safety = _get_safety()
    return safety.sanitize_output(output)


@_register_devtool("run_command")
def _devtools_run_command(args: dict) -> str:
    global _pending_command
    command = args.get("command", "").strip()
    if not command:
        return "Error: 'command' is required."
    safety = _get_safety()
    tier, reason = safety.classify_command(command)
    if tier == 'blocked':
        return f"BLOCKED: {reason}. This command is not allowed."
    if tier == 'confirmation':
        _pending_command = (command, _time.time() + 30)
        return f"CONFIRMATION REQUIRED: `{command}` — {reason}. Shall I proceed?"
    # Tier 1 (allowed) or Tier 2 (safe_write) — execute
    output = _run_cmd(command, cwd='/home/user/jarvis', timeout=30)
    return safety.sanitize_output(output)


@_register_devtool("confirm_pending")
def _devtools_confirm_pending(args: dict) -> str:
    global _pending_command
    if _pending_command is None:
        return "No pending command to confirm."
    command, expiry = _pending_command
    if _time.time() > expiry:
        _pending_command = None
        return "That confirmation has expired. Please issue the command again."
    _pending_command = None
    safety = _get_safety()
    output = _run_cmd(command, cwd='/home/user/jarvis', timeout=30)
    return safety.sanitize_output(output)
