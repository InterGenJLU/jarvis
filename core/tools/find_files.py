"""Tool definition: find_files — filesystem search and counting."""

import subprocess
from pathlib import Path

TOOL_NAME = "find_files"
SKILL_NAME = "filesystem"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Search for files on the local filesystem by name or pattern. "
            "Also counts files in a directory, or counts lines of code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "count_files", "count_code"],
                    "description": (
                        "search: find files matching a name pattern. "
                        "count_files: count files in a directory. "
                        "count_code: count lines of code in the codebase."
                    )
                },
                "pattern": {
                    "type": "string",
                    "description": "Filename or glob pattern to search for (for 'search' action)"
                },
                "directory": {
                    "type": "string",
                    "description": "Directory name to search in (e.g. 'documents', 'downloads', 'home')"
                }
            },
            "required": ["action"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "For questions about files on THIS COMPUTER (find files, count files, "
    "count code lines), call find_files. "
    "Examples: 'find my resume' → search, 'how many Python files?' → count_files, "
    "'lines of code in the project?' → count_lines. "
    "NOT for: reading file contents, editing files, web downloads."
)


def handler(args: dict) -> str:
    """Search for files, count files in a directory, or count code lines."""
    action = args.get("action", "search")
    if action == "count_code":
        return _find_count_code()
    elif action == "count_files":
        return _find_count_files(args.get("directory", "home"))
    elif action == "search":
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: 'pattern' is required for file search."
        return _find_search(pattern)
    return f"Unknown find_files action: {action}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_search(pattern: str) -> str:
    """Search for files matching a name pattern."""
    search_paths = [
        str(Path.home()),
        str(Path.home() / "Documents"),
        str(Path.home() / "Downloads"),
        str(Path.home() / "Desktop"),
    ]
    all_matches = []
    for search_path in search_paths:
        if not Path(search_path).exists():
            continue
        try:
            result = subprocess.run(
                ["find", search_path, "-name", f"*{pattern}*", "-type", "f",
                 "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*/venv/*", "-not", "-path", "*/.cache/*"],
                capture_output=True, text=True, timeout=10,
            )
            matches = [f for f in result.stdout.strip().split("\n") if f]
            all_matches.extend(matches)
        except subprocess.TimeoutExpired:
            continue
        if all_matches:
            break  # Stop after first path with results

    if not all_matches:
        return f"No files found matching '{pattern}'."
    # Deduplicate and limit
    seen = set()
    unique = []
    for m in all_matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    if len(unique) == 1:
        return f"Found: {unique[0]}"
    display = unique[:10]
    result_text = f"Found {len(unique)} files matching '{pattern}':\n"
    result_text += "\n".join(f"  - {f}" for f in display)
    if len(unique) > 10:
        result_text += f"\n  ... and {len(unique) - 10} more"
    return result_text


def _find_count_files(directory: str) -> str:
    """Count files in a named directory."""
    dir_map = {
        "documents": Path.home() / "Documents",
        "downloads": Path.home() / "Downloads",
        "desktop": Path.home() / "Desktop",
        "home": Path.home(),
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
        "music": Path.home() / "Music",
        "scripts": Path.home() / "scripts",
        "jarvis": Path.home() / "jarvis",
        "core": Path.home() / "jarvis" / "core",
        "skills": Path("/mnt/storage/jarvis/skills"),
        "models": Path("/mnt/models"),
    }
    target = dir_map.get(directory.lower())
    if not target:
        # Try as literal path
        target = Path(directory).expanduser()
    if not target.exists():
        return f"Directory '{directory}' does not exist."
    try:
        file_count = sum(1 for item in target.iterdir() if item.is_file())
        dir_count = sum(1 for item in target.iterdir() if item.is_dir())
    except PermissionError:
        return f"Permission denied accessing '{directory}'."
    if file_count == 0 and dir_count == 0:
        return f"'{directory}' is empty."
    parts = []
    if file_count:
        parts.append(f"{file_count:,} files")
    if dir_count:
        parts.append(f"{dir_count:,} folders")
    return f"'{directory}' contains {' and '.join(parts)}."


def _find_count_code() -> str:
    """Count lines of Python code in the JARVIS codebase."""
    jarvis_path = Path.home() / "jarvis"
    if not jarvis_path.exists():
        return "JARVIS codebase not found."
    result = subprocess.run(
        ["find", str(jarvis_path), "-name", "*.py", "-type", "f",
         "-not", "-path", "*/venv*", "-not", "-path", "*/__pycache__/*"],
        capture_output=True, text=True,
    )
    py_files = [f for f in result.stdout.strip().split("\n") if f]
    if not py_files:
        return "No Python files found."
    total_lines = 0
    for py_file in py_files:
        try:
            with open(py_file, "r") as f:
                total_lines += sum(1 for _ in f)
        except Exception:
            continue
    return f"Codebase: {total_lines:,} lines of Python across {len(py_files)} files."
