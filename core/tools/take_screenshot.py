"""Tool definition: take_screenshot — capture screen or survey monitor layout."""

import base64
import os
import re
import subprocess
import time

TOOL_NAME = "take_screenshot"
ALWAYS_INCLUDED = True

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
                    "enum": ["monitor", "window"],
                    "description": (
                        "monitor=focused monitor (default), "
                        "window=active window only"
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
    "For 'what's on my screen?', 'describe my screen', 'take a screenshot', "
    "or any request about what the user is looking at, call take_screenshot "
    "with action=capture. The result includes the screenshot image — "
    "describe what you see in detail. "
    "If the user asks about monitor layout or which monitor shows what, "
    "use action=survey first."
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

    # Focused monitor or active window: use desktop_manager
    screenshot_path = _desktop_manager.take_screenshot(target=target)
    if not screenshot_path:
        return "Error: Screenshot capture failed."

    return _encode_and_cleanup(screenshot_path, target)


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

        return _encode_and_cleanup(crop_path, f"monitor {output_name}")
    except ImportError:
        _try_unlink(screenshot_path)
        return "Error: PIL not available — cannot crop to specific monitor."
    except Exception as e:
        _try_unlink(screenshot_path)
        return f"Error cropping screenshot: {e}"


def _encode_and_cleanup(screenshot_path: str, target: str) -> dict | str:
    """Read screenshot file, base64 encode, clean up, return result dict."""
    try:
        with open(screenshot_path, "rb") as f:
            image_bytes = f.read()

        image_data = base64.b64encode(image_bytes).decode("utf-8")
        size_kb = len(image_bytes) / 1024

        _try_unlink(screenshot_path)

        return {
            "text": (
                f"Screenshot captured ({target}, {size_kb:.0f} KB). "
                "The image is attached — describe what you see."
            ),
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
