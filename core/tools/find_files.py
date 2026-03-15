"""Tool definition: find_files — filesystem search, listing, and system queries."""

import grp
import os
import pwd
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

TOOL_NAME = "find_files"
SKILL_NAME = "filesystem"

# ---------------------------------------------------------------------------
# Shared directory map — single source of truth for named directories
# ---------------------------------------------------------------------------

_DIR_MAP = {
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
    "tools": Path.home() / "jarvis" / "core" / "tools",
    "skills": Path("/mnt/storage/jarvis/skills"),
    "models": Path("/mnt/models"),
    "storage": Path("/mnt/storage"),
    "tmp": Path("/tmp"),
}


def _resolve_dir(directory: str) -> Path | None:
    """Resolve a directory name or path to a Path object."""
    target = _DIR_MAP.get(directory.lower().strip())
    if target:
        return target
    # Try as literal path
    p = Path(directory).expanduser()
    if p.exists() and p.is_dir():
        return p
    return None


def _format_size(size_bytes: int) -> str:
    """Format byte count to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _run(cmd: list | str, timeout: int = 10, cwd: str = None) -> str:
    """Run a command and return stdout, or error string."""
    try:
        result = subprocess.run(
            cmd, shell=isinstance(cmd, str),
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Filesystem operations on this computer: search files, list directories, "
            "directory sizes, disk usage, file metadata, recent files, directory tree, "
            "find large files, package info, and count code lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search", "count_files", "count_code", "list_files",
                        "dir_sizes", "disk_usage", "file_info", "recent_files",
                        "tree", "find_large", "find_largest", "package_info",
                    ],
                    "description": (
                        "search: find files matching a name pattern. "
                        "count_files: count files in a directory. "
                        "count_code: count lines of code in the codebase. "
                        "list_files: list files and folders in a directory with sizes. "
                        "dir_sizes: show recursive sizes of subdirectories. "
                        "disk_usage: show disk space usage across mount points. "
                        "file_info: detailed metadata for a specific file or directory. "
                        "recent_files: find files modified within N days. "
                        "tree: show recursive directory structure. "
                        "find_large: find files above a size threshold. "
                        "find_largest: show the biggest files (top 20 by size, no threshold). "
                        "package_info: check if a package is installed and its version."
                    )
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Filename or glob pattern (for 'search'), "
                        "or file/directory path (for 'file_info')."
                    )
                },
                "directory": {
                    "type": "string",
                    "description": (
                        "Directory name (e.g. 'documents', 'downloads', 'home', "
                        "'jarvis') or absolute path."
                    )
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days back for 'recent_files' (default 1)."
                },
                "depth": {
                    "type": "integer",
                    "description": "Max depth for 'tree' action (default 2)."
                },
                "min_size": {
                    "type": "string",
                    "description": (
                        "Size threshold for 'find_large' — e.g. '100M', '1G' "
                        "(default '100M')."
                    )
                },
                "package_name": {
                    "type": "string",
                    "description": "Package or command name for 'package_info'."
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max number of results to return for 'list_files' "
                        "(default 100, max 100)."
                    )
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["name", "modified", "size"],
                    "description": (
                        "Sort order for 'list_files': name (alphabetical, default), "
                        "modified (most recent first), or size (largest first)."
                    )
                },
            },
            "required": ["action"]
        }
    }
}

SYSTEM_PROMPT_RULE = (
    "ALWAYS use this when the user asks about their local files, folders, "
    "directory tree, directory structure, or file counts. "
    "For questions about files on THIS COMPUTER (find files, count files, "
    "list files, directory sizes, disk usage, file details, recent files, "
    "directory tree, large files, installed packages, count code lines), "
    "call find_files. "
    "Examples: 'find my resume' → search, 'how many files in downloads?' → count_files, "
    "'list the files in documents' → list_files, "
    "'how big is my jarvis folder' → dir_sizes, "
    "'how much disk space do I have' → disk_usage, "
    "'what files did I modify today' → recent_files, "
    "'show me the directory structure' → tree, "
    "'find large files' → find_large, "
    "'what are the biggest files' → find_largest (shows top 20 by size), "
    "'find files over 1 gig' → find_large with min_size='1G', "
    "'what are the permissions on this file' → file_info, "
    "'is ffmpeg installed' → package_info, "
    "'what version of python do I have' → package_info. "
    "Use list_files when the user asks to see/list/show the files in a directory. "
    "'show me the 5 most recent files in downloads' → list_files with directory=downloads, sort_by=modified, limit=5. "
    "'what are the newest files on my desktop' → list_files with directory=desktop, sort_by=modified. "
    "NOT for: reading file contents, editing files, web downloads."
)


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

def handler(args: dict) -> str:
    """Route to the appropriate find_files action."""
    action = args.get("action", "search")
    dispatch = {
        "search": lambda: _find_search(args.get("pattern", "")),
        "count_files": lambda: _find_count_files(args.get("directory", "home")),
        "count_code": lambda: _find_count_code(),
        "list_files": lambda: _find_list_files(
            args.get("directory", "home"),
            limit=args.get("limit"),
            sort_by=args.get("sort_by"),
        ),
        "dir_sizes": lambda: _find_dir_sizes(args.get("directory", "home")),
        "disk_usage": lambda: _find_disk_usage(),
        "file_info": lambda: _find_file_info(args.get("pattern", "")),
        "recent_files": lambda: _find_recent_files(
            args.get("directory", "home"), int(args.get("days", 1))),
        "tree": lambda: _find_tree(
            args.get("directory", "home"), int(args.get("depth", 2))),
        "find_large": lambda: _find_large(
            args.get("directory", "home"), args.get("min_size", "100M")),
        "find_largest": lambda: _find_largest(
            args.get("directory", "home"), int(args.get("limit", 20))),
        "package_info": lambda: _find_package_info(args.get("package_name", "")),
    }
    fn = dispatch.get(action)
    if fn:
        return fn()
    return f"Unknown find_files action: {action}"


# ---------------------------------------------------------------------------
# Original actions (preserved)
# ---------------------------------------------------------------------------

def _find_search(pattern: str) -> str:
    """Search for files matching a name pattern."""
    if not pattern:
        return "Error: 'pattern' is required for file search."
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
    target = _resolve_dir(directory)
    if not target:
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


def _find_list_files(directory: str, limit: int = None,
                     sort_by: str = None) -> str:
    """List files in a named directory with sizes (including directory sizes)."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."
    try:
        entries = list(target.iterdir())
    except PermissionError:
        return f"Permission denied accessing '{directory}'."

    if not entries:
        return f"'{directory}' is empty."

    # Filter hidden files
    visible = [e for e in entries if not e.name.startswith('.')]
    if not visible:
        return f"'{directory}' contains only hidden files."

    # Sort entries
    sort_by = sort_by or "name"
    if sort_by == "modified":
        visible.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    elif sort_by == "size":
        visible.sort(key=lambda p: (p.stat().st_size if p.is_file() else 0),
                     reverse=True)
    else:
        visible.sort(key=lambda p: p.name.lower())

    # Apply limit
    cap = min(limit or 100, 100)
    total = len(visible)
    display = visible[:cap]

    # Get directory sizes via du -sh for all subdirs in one call
    visible_dirs = [e for e in display if e.is_dir()]
    dir_sizes = {}
    if visible_dirs:
        du_args = ["du", "-sh", "--"] + [str(d) for d in visible_dirs[:50]]
        du_output = _run(du_args, timeout=5)
        if du_output and not du_output.startswith("Error"):
            for line in du_output.split("\n"):
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    size_str, path_str = parts
                    dirname = Path(path_str).name
                    dir_sizes[dirname] = size_str

    lines = []
    for entry in display:
        if entry.is_dir():
            sz = dir_sizes.get(entry.name, "")
            if sz:
                lines.append(f"  [DIR] {entry.name}/  ({sz})")
            else:
                lines.append(f"  [DIR] {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
                lines.append(f"  {entry.name}  ({_format_size(size)})")
            except OSError:
                lines.append(f"  {entry.name}")

    sort_label = {"name": "alphabetical", "modified": "most recent", "size": "largest"}.get(sort_by, "")
    header = f"'{directory}' — {total} items (sorted by {sort_label}):"
    if total > cap:
        lines.append(f"  ... and {total - cap} more")
    return header + "\n" + "\n".join(lines)


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


# ---------------------------------------------------------------------------
# New actions
# ---------------------------------------------------------------------------

def _find_dir_sizes(directory: str) -> str:
    """Show recursive sizes of items in a directory, sorted largest first."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."

    # du -sh on each child item (files + dirs)
    try:
        visible = [e for e in target.iterdir() if not e.name.startswith('.')]
    except PermissionError:
        return f"Permission denied accessing '{directory}'."

    if not visible:
        return f"'{directory}' is empty."

    du_args = ["du", "-sh", "--"] + [str(e) for e in visible[:50]]
    output = _run(du_args, timeout=10)
    if not output or output.startswith("Error"):
        return f"Could not determine sizes in '{directory}'."

    # Parse and sort by raw bytes
    items = []
    for line in output.split("\n"):
        parts = line.split("\t", 1)
        if len(parts) == 2:
            size_str, path_str = parts
            name = Path(path_str).name
            is_dir = Path(path_str).is_dir()
            # Get raw bytes for sorting
            raw = _run(["du", "-sb", "--", path_str], timeout=5)
            raw_bytes = 0
            if raw and not raw.startswith("Error"):
                try:
                    raw_bytes = int(raw.split("\t")[0])
                except (ValueError, IndexError):
                    pass
            suffix = "/" if is_dir else ""
            items.append((raw_bytes, f"  {name}{suffix}  ({size_str})"))

    items.sort(key=lambda x: x[0], reverse=True)

    # Total size of the directory itself
    total_output = _run(["du", "-sh", "--", str(target)], timeout=5)
    total_size = ""
    if total_output and not total_output.startswith("Error"):
        total_size = total_output.split("\t")[0]

    header = f"'{directory}' — {len(items)} items"
    if total_size:
        header += f", total {total_size}"
    header += ":"

    return header + "\n" + "\n".join(line for _, line in items)


def _find_disk_usage() -> str:
    """Show disk space usage across mount points."""
    output = _run(["df", "-h"], timeout=5)
    if not output or output.startswith("Error"):
        return "Could not retrieve disk usage."

    lines = output.split("\n")
    if not lines:
        return "No disk usage data."

    # Filter to real filesystems (exclude tmpfs, snap, etc.)
    header = lines[0]
    real = []
    for line in lines[1:]:
        fs = line.split()[0] if line.split() else ""
        if fs.startswith("/dev/") and "/snap/" not in line:
            real.append(line)

    if not real:
        return "No real filesystems found."

    return header + "\n" + "\n".join(real)


def _find_file_info(path_str: str) -> str:
    """Get detailed metadata for a specific file or directory."""
    if not path_str:
        return "Error: 'pattern' (file path) is required for file_info."

    # Try named dir map first, then literal path
    target = _DIR_MAP.get(path_str.lower().strip())
    if not target:
        target = Path(path_str).expanduser()
    if not target.exists():
        return f"'{path_str}' does not exist."

    try:
        st = target.stat()
    except PermissionError:
        return f"Permission denied accessing '{path_str}'."
    except OSError as e:
        return f"Error accessing '{path_str}': {e}"

    # Type
    if target.is_symlink():
        ftype = f"symlink → {os.readlink(target)}"
    elif target.is_dir():
        ftype = "directory"
    elif target.is_file():
        ftype = "file"
    else:
        ftype = "other"

    # Permissions
    mode = st.st_mode
    perms_octal = oct(mode)[-3:]
    perms_rwx = ""
    for who in [(mode >> 6) & 7, (mode >> 3) & 7, mode & 7]:
        perms_rwx += ("r" if who & 4 else "-")
        perms_rwx += ("w" if who & 2 else "-")
        perms_rwx += ("x" if who & 1 else "-")

    # Owner/group
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)

    # Size
    if target.is_dir():
        du_out = _run(["du", "-sh", "--", str(target)], timeout=5)
        if du_out and not du_out.startswith("Error"):
            size_str = du_out.split("\t")[0]
        else:
            size_str = "(unknown)"
    else:
        size_str = _format_size(st.st_size)

    # Timestamps
    modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    accessed = datetime.fromtimestamp(st.st_atime).strftime("%Y-%m-%d %H:%M:%S")
    created = datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"Path: {target}",
        f"Type: {ftype}",
        f"Size: {size_str}",
        f"Permissions: {perms_rwx} ({perms_octal})",
        f"Owner: {owner}:{group}",
        f"Modified: {modified}",
        f"Accessed: {accessed}",
        f"Created: {created}",
    ]
    return "\n".join(lines)


def _find_recent_files(directory: str, days: int = 1) -> str:
    """Find files modified within the last N days."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."

    days = max(1, min(365, days))

    output = _run([
        "find", str(target), "-mtime", f"-{days}", "-type", "f",
        "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
        "-not", "-path", "*/venv/*", "-not", "-path", "*/.cache/*",
        "-not", "-path", "*/.local/*",
        "-printf", "%T@ %s %p\n",
    ], timeout=10)

    if not output or output.startswith("Error"):
        return f"No files modified in the last {days} day(s) in '{directory}'."

    # Parse, sort by mtime descending
    items = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        if len(parts) == 3:
            try:
                mtime = float(parts[0])
                size = int(parts[1])
                path = parts[2]
                items.append((mtime, size, path))
            except ValueError:
                continue

    if not items:
        return f"No files modified in the last {days} day(s) in '{directory}'."

    items.sort(key=lambda x: x[0], reverse=True)
    display = items[:30]

    period = f"{days} day" + ("s" if days > 1 else "")
    header = f"{len(items)} files modified in the last {period} in '{directory}'"
    if len(items) > 30:
        header += f" (showing 30 of {len(items)})"
    header += ":"

    lines = [header]
    for mtime, size, path in display:
        ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        rel = path.replace(str(target) + "/", "")
        lines.append(f"  {ts}  {_format_size(size):>8s}  {rel}")

    return "\n".join(lines)


def _find_tree(directory: str, depth: int = 2) -> str:
    """Show recursive directory structure."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."

    depth = max(1, min(5, depth))

    # Try the tree command first
    output = _run(["tree", "-L", str(depth), "--noreport", "--charset=ascii",
                    "-I", ".git|__pycache__|venv|.cache|node_modules",
                    str(target)], timeout=10)

    if output and not output.startswith("Error"):
        lines = output.split("\n")
        if len(lines) > 200:
            lines = lines[:200]
            lines.append(f"  ... ({len(output.split(chr(10))) - 200} more entries)")
        return "\n".join(lines)

    # Fallback: Python os.walk with depth limit
    lines = [str(target)]
    base_depth = str(target).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(target):
        current_depth = dirpath.count(os.sep) - base_depth
        if current_depth >= depth:
            dirnames.clear()
            continue
        # Skip hidden/excluded dirs
        dirnames[:] = sorted([
            d for d in dirnames
            if not d.startswith('.') and d not in (
                '__pycache__', 'venv', '.cache', 'node_modules', '.git')
        ])
        indent = "  " * (current_depth + 1)
        for d in dirnames:
            lines.append(f"{indent}{d}/")
        for f in sorted(filenames):
            if not f.startswith('.'):
                lines.append(f"{indent}{f}")
        if len(lines) > 200:
            lines.append(f"  ... (output truncated at 200 lines)")
            break

    return "\n".join(lines)


def _find_large(directory: str, min_size: str = "100M") -> str:
    """Find files above a size threshold."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."

    # Validate min_size format
    if not min_size:
        min_size = "100M"
    size_clean = min_size.strip().upper()
    if not any(size_clean.endswith(s) for s in ("K", "M", "G", "T", "C")):
        # Assume megabytes if no suffix
        size_clean += "M"

    output = _run([
        "find", str(target), "-type", "f", "-size", f"+{size_clean}",
        "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
        "-not", "-path", "*/venv/*", "-not", "-path", "*/.cache/*",
        "-printf", "%s %p\n",
    ], timeout=10)

    if not output or output.startswith("Error"):
        return f"No files larger than {min_size} found in '{directory}'."

    # Parse and sort by size descending
    items = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            try:
                size = int(parts[0])
                path = parts[1]
                items.append((size, path))
            except ValueError:
                continue

    if not items:
        return f"No files larger than {min_size} found in '{directory}'."

    items.sort(key=lambda x: x[0], reverse=True)
    display = items[:20]

    header = f"{len(items)} files larger than {min_size} in '{directory}'"
    if len(items) > 20:
        header += f" (showing top 20)"
    header += ":"

    lines = [header]
    for size, path in display:
        rel = path.replace(str(target) + "/", "")
        lines.append(f"  {_format_size(size):>10s}  {rel}")

    return "\n".join(lines)


def _find_largest(directory: str, limit: int = 20) -> str:
    """Find the largest files in a directory (no size threshold)."""
    target = _resolve_dir(directory)
    if not target:
        return f"Directory '{directory}' does not exist."

    # Find all files, sorted by size
    output = _run([
        "find", str(target), "-type", "f",
        "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
        "-not", "-path", "*/venv/*", "-not", "-path", "*/.cache/*",
        "-printf", "%s %p\n",
    ], timeout=10)

    if not output or output.startswith("Error"):
        return f"No files found in '{directory}'."

    items = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            try:
                items.append((int(parts[0]), parts[1]))
            except ValueError:
                continue

    if not items:
        return f"No files found in '{directory}'."

    items.sort(key=lambda x: x[0], reverse=True)
    cap = min(limit, 20)
    display = items[:cap]

    header = f"Top {len(display)} largest files in '{directory}':"
    lines = [header]
    for size, path in display:
        rel = path.replace(str(target) + "/", "")
        lines.append(f"  {_format_size(size):>10s}  {rel}")

    return "\n".join(lines)


def _find_package_info(package_name: str) -> str:
    """Check if a package is installed and retrieve version info."""
    if not package_name:
        return "Error: 'package_name' is required for package_info."

    name = package_name.strip()
    sections = []

    # 1. dpkg — apt/deb package
    dpkg_out = _run(["dpkg", "-s", name], timeout=5)
    if dpkg_out and "Status:" in dpkg_out and "installed" in dpkg_out.lower():
        # Extract key fields
        status = version = description = ""
        for line in dpkg_out.split("\n"):
            if line.startswith("Status:"):
                status = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
            elif line.startswith("Description:"):
                description = line.split(":", 1)[1].strip()
        parts = [f"dpkg: installed"]
        if version:
            parts.append(f"version {version}")
        if description:
            parts.append(f"({description})")
        sections.append(" — ".join(parts))
    else:
        # Check if available but not installed
        apt_out = _run(f"apt list {shlex.quote(name)} 2>/dev/null", timeout=5)
        if apt_out and name in apt_out and "installed" not in apt_out.lower():
            sections.append(f"dpkg: not installed (available via apt)")

    # 2. which — binary location
    which_out = _run(["which", name], timeout=3)
    if which_out and not which_out.startswith("Error") and which_out.strip():
        sections.append(f"Binary: {which_out.strip()}")

    # 3. --version
    version_out = _run(f"{shlex.quote(name)} --version 2>&1 | head -1", timeout=5)
    if (version_out and not version_out.startswith("Error")
            and version_out.strip() and "not found" not in version_out.lower()):
        sections.append(f"Version: {version_out.strip()}")

    # 4. pip — Python package
    pip_out = _run(["pip", "show", name], timeout=5)
    if pip_out and not pip_out.startswith("Error") and "Name:" in pip_out:
        pip_version = ""
        pip_location = ""
        for line in pip_out.split("\n"):
            if line.startswith("Version:"):
                pip_version = line.split(":", 1)[1].strip()
            elif line.startswith("Location:"):
                pip_location = line.split(":", 1)[1].strip()
        parts = ["pip: installed"]
        if pip_version:
            parts.append(f"version {pip_version}")
        if pip_location:
            parts.append(f"at {pip_location}")
        sections.append(" — ".join(parts))

    if not sections:
        return f"Package '{name}' is not installed (not found via dpkg, PATH, or pip)."

    return f"Package '{name}':\n" + "\n".join(f"  {s}" for s in sections)
