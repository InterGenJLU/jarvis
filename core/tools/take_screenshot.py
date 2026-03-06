"""Tool definition: take_screenshot — capture screen or survey monitor layout."""

import base64
import os
import re
import subprocess
import time

TOOL_NAME = "take_screenshot"
ALWAYS_INCLUDED = True

# Intent examples for semantic pruner scoring — without these, the pruner
# can't tell that "take a screenshot" should route through tool calling
# and instead defers to non-migrated skills (e.g. file_editor).
INTENT_EXAMPLES = [
    "take a screenshot",
    "capture my screen",
    "what's on my screen",
    "describe my screen",
    "screenshot",
    "show me what's on my display",
    "what am I looking at",
    "what do you see on screen",
]

SCHEMA = {
    "type": "function",
    "function": {
        "name": "take_screenshot",
        "description": (
            "Take a screenshot of the desktop or survey monitor layout. "
            "System has 3 monitors. Use action=survey first if the user's "
            "target monitor is ambiguous."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["capture", "survey"],
                    "description": (
                        "capture=take screenshot (default), "
                        "survey=list monitors and their windows without capturing"
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["monitor", "window", "all"],
                    "description": (
                        "monitor=focused monitor (default), "
                        "window=active window only, "
                        "all=all monitors combined"
                    ),
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Specific monitor output name (e.g. DP-1, HDMI-1). "
                        "Only needed after a survey to target a non-focused monitor."
                    ),
                },
            },
            "required": [],
        },
    },
}

SYSTEM_PROMPT_RULE = (
    "RULE: Screenshot requests. "
    "If the user specifies a target ('this window', 'the left monitor', a monitor name), "
    "call take_screenshot with action=capture and the appropriate target/output. "
    "If the request is ambiguous ('what's on my screen?', 'take a screenshot', "
    "'describe my screen'), call take_screenshot with action=survey FIRST to see "
    "the monitor layout and open windows, then make a SECOND call with action=capture "
    "targeting the most relevant monitor. "
    "If the user asks about monitor layout or which monitor shows what, "
    "use action=survey and report the results without capturing. "
    "After capturing, describe what you see in the image in detail."
)

# Runtime dependency — injected by tool_registry.inject_dependencies()
DEPENDENCIES = {"desktop_manager": "_desktop_manager"}
_desktop_manager = None


def handler(args: dict) -> dict | str:
    """Handle take_screenshot tool calls.

    Returns:
        For survey: plain text string describing monitor/window layout.
        For capture: dict with {"text": str, "image_data": str} where
                     image_data is base64-encoded PNG for multimodal LLM input.
    """
    action = args.get("action", "capture")

    if action == "survey":
        return _survey_monitors()
    else:
        return _capture_screenshot(args)


# ---------------------------------------------------------------------------
# Monitor geometry
# ---------------------------------------------------------------------------

def _parse_monitors() -> list[dict]:
    """Parse xrandr --listmonitors for output names, positions, and sizes.

    Returns list of dicts: {name, x, y, width, height, primary}.
    Coordinates are in logical (xrandr) pixels.
    """
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    monitors = []
    for line in result.stdout.strip().split("\n")[1:]:  # skip "Monitors: N"
        # " 0: +*DP-2 2560/610x1440/350+1920+0  DP-2"
        m = re.match(
            r'\s*(\d+):\s+\+(\*?)(\S+)\s+'
            r'(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)',
            line,
        )
        if m:
            monitors.append({
                "name": m.group(3),
                "width": int(m.group(4)),
                "height": int(m.group(5)),
                "x": int(m.group(6)),
                "y": int(m.group(7)),
                "primary": m.group(2) == "*",
            })
    return monitors


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------

def _survey_monitors() -> str:
    """List monitors and their windows."""
    lines = []
    monitors = _parse_monitors()

    if monitors:
        for i, mon in enumerate(monitors):
            label = (
                f"Monitor {i} ({mon['name']}, "
                f"{mon['width']}x{mon['height']})"
            )
            if mon["primary"]:
                label += " [PRIMARY]"
            lines.append(label)
    else:
        lines.append("Could not query monitor layout.")

    # Get window list from desktop manager
    if _desktop_manager:
        windows = _desktop_manager.list_windows()
        if windows:
            lines.append("")
            lines.append("Open windows:")
            for win in windows:
                title = win.get("title", "Unknown")
                wm_class = win.get("wm_class", "")
                monitor = win.get("monitor", "?")
                if title:
                    label = f"  - {title}"
                    if wm_class:
                        label += f" ({wm_class})"
                    label += f" [monitor {monitor}]"
                    lines.append(label)
        else:
            lines.append("No windows found.")
    else:
        lines.append("Desktop manager not available — cannot list windows.")

    return "\n".join(lines) if lines else "No monitor information available."


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _capture_screenshot(args: dict) -> dict | str:
    """Capture a screenshot and return as base64 image data."""
    target = args.get("target", "monitor")
    output_name = args.get("output")

    if not _desktop_manager:
        return "Error: Desktop manager not available — cannot capture screenshot."

    # Specific monitor by output name: capture full desktop, crop with PIL
    if output_name:
        return _capture_specific_monitor(output_name)

    # Focused monitor: detect which monitor has focus, crop to it
    if target == "monitor":
        return _capture_focused_monitor()

    # All monitors: full desktop, downscale for LLM
    if target == "all":
        screenshot_path = _desktop_manager.take_screenshot(target="all")
        if not screenshot_path:
            return "Error: Screenshot capture failed."
        return _encode_and_cleanup(screenshot_path, "all monitors", max_width=1920)

    # Active window
    screenshot_path = _desktop_manager.take_screenshot(target=target)
    if not screenshot_path:
        return "Error: Screenshot capture failed."
    return _encode_and_cleanup(screenshot_path, target)


def _capture_focused_monitor() -> dict | str:
    """Capture the monitor that has the focused window.

    Uses GNOME D-Bus get_active_window() to find the monitor index,
    maps to xrandr output name, then delegates to _capture_specific_monitor().
    """
    monitors = _parse_monitors()
    if not monitors:
        return "Error: Could not detect monitors."

    # Try to get focused window's monitor index from GNOME D-Bus
    target_name = None
    active_win = _desktop_manager.get_active_window()
    if active_win and "monitor" in active_win:
        mon_idx = active_win["monitor"]
        if 0 <= mon_idx < len(monitors):
            target_name = monitors[mon_idx]["name"]

    # Fallback: use the primary monitor
    if not target_name:
        for mon in monitors:
            if mon["primary"]:
                target_name = mon["name"]
                break
        # Last resort: first monitor
        if not target_name:
            target_name = monitors[0]["name"]

    return _capture_specific_monitor(target_name)


def _capture_specific_monitor(output_name: str) -> dict | str:
    """Capture a specific monitor by output name using full-desktop + PIL crop."""
    monitors = _parse_monitors()
    target_mon = None
    for mon in monitors:
        if mon["name"].lower() == output_name.lower():
            target_mon = mon
            break
    if not target_mon:
        available = ", ".join(m["name"] for m in monitors)
        return f"Error: Monitor '{output_name}' not found. Available: {available}"

    # Capture full desktop
    full_path = f"/tmp/jarvis_screenshot_full_{int(time.time())}.png"
    screenshot_path = _desktop_manager.take_screenshot(
        target="all", output_path=full_path,
    )
    if not screenshot_path:
        return "Error: Full desktop screenshot failed."

    try:
        from PIL import Image
        img = Image.open(screenshot_path)

        # Compute scale: screenshot pixels vs xrandr logical bounding box
        bbox_w = max(m["x"] + m["width"] for m in monitors)
        scale = img.width / bbox_w

        # Crop to target monitor
        x = int(target_mon["x"] * scale)
        y = int(target_mon["y"] * scale)
        w = int(target_mon["width"] * scale)
        h = int(target_mon["height"] * scale)
        cropped = img.crop((x, y, x + w, y + h))

        # Save cropped image
        crop_path = f"/tmp/jarvis_screenshot_{int(time.time())}.png"
        cropped.save(crop_path)
        img.close()

        # Clean up full desktop capture
        try:
            os.unlink(screenshot_path)
        except OSError:
            pass

        return _encode_and_cleanup(crop_path, f"monitor {output_name}",
                                   max_width=None)
    except ImportError:
        _try_unlink(screenshot_path)
        return "Error: PIL not available — cannot crop to specific monitor."
    except Exception as e:
        _try_unlink(screenshot_path)
        return f"Error cropping screenshot: {e}"


def _encode_and_cleanup(screenshot_path: str, target: str,
                        max_width: int | None = None) -> dict | str:
    """Read screenshot file, optionally downscale, base64 encode, return result dict.

    Args:
        max_width: Maximum pixel width before downscaling. None = keep native resolution.
                   Targeted captures (single monitor/window) should pass None;
                   full-desktop captures should pass 1920.
    """
    try:
        from PIL import Image
        import io

        img = Image.open(screenshot_path)
        orig_w, orig_h = img.size

        # Downscale if max_width is set and image exceeds it
        if max_width and orig_w > max_width:
            ratio = max_width / orig_w
            new_w = max_width
            new_h = int(orig_h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        image_bytes = buf.getvalue()
        img.close()

        image_data = base64.b64encode(image_bytes).decode("utf-8")
        size_kb = len(image_bytes) / 1024

        _try_unlink(screenshot_path)

        desc = f"Screenshot captured ({target}, {orig_w}x{orig_h}"
        if max_width and orig_w > max_width:
            desc += f" → resized to {new_w}x{new_h}"
        desc += f", {size_kb:.0f} KB)."

        return {
            "text": desc + " The image is attached — describe what you see.",
            "image_data": image_data,
        }
    except Exception as e:
        _try_unlink(screenshot_path)
        return f"Error reading screenshot: {e}"


def _try_unlink(path: str):
    """Remove a file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass
