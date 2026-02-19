"""
JARVIS Desktop Manager — unified interface for GNOME desktop control.

Provides window management (via GNOME Shell extension D-Bus bridge with
wmctrl fallback), volume control (pactl), notifications (notify-send),
and clipboard (wl-clipboard).

Singleton pattern — use get_desktop_manager(config) to obtain the instance.
"""

import json
import os
import subprocess
from typing import Optional

from core.logger import get_logger

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance = None


def get_desktop_manager(config=None):
    """Return the singleton DesktopManager instance."""
    global _instance
    if _instance is None and config is not None:
        _instance = DesktopManager(config)
    return _instance


# ---------------------------------------------------------------------------
# D-Bus constants
# ---------------------------------------------------------------------------

_DBUS_NAME = "org.jarvis.Desktop"
_DBUS_PATH = "/org/jarvis/Desktop"
_DBUS_IFACE = "org.jarvis.Desktop"


class DesktopManager:
    """Unified desktop control for JARVIS.

    Window ops: D-Bus extension -> wmctrl fallback
    Audio ops: pactl (always available)
    Notifications: notify-send (always available)
    Clipboard: wl-copy/wl-paste (needs wl-clipboard installed)
    """

    def __init__(self, config):
        self.config = config
        self.logger = get_logger("desktop_manager", config)
        self._extension_uuid = config.get("desktop.extension_uuid", "jarvis-desktop@jarvis")
        self._fallback_wmctrl = config.get("desktop.fallback_wmctrl", True)

        # Lazy D-Bus proxy — don't block startup
        self._proxy = None
        self._dbus_available = None  # None = not checked yet

        self.logger.info("Desktop manager initialized (lazy D-Bus connection)")

    # ── D-Bus connection (lazy) ──────────────────────────────────────

    def _get_proxy(self):
        """Lazily connect to the GNOME Shell extension D-Bus service."""
        if self._proxy is not None:
            return self._proxy

        if self._dbus_available is False:
            return None

        try:
            import dbus
            bus = dbus.SessionBus()
            self._proxy = bus.get_object(_DBUS_NAME, _DBUS_PATH)
            # Quick health check
            iface = dbus.Interface(self._proxy, _DBUS_IFACE)
            version = iface.Ping()
            self._dbus_available = True
            self.logger.info(f"Connected to JARVIS Desktop Bridge v{version}")
            return self._proxy
        except Exception as e:
            self._dbus_available = False
            self._proxy = None
            self.logger.warning(f"GNOME extension not available: {e}")
            return None

    def _call(self, method, *args):
        """Call a D-Bus method on the extension. Returns None on failure."""
        proxy = self._get_proxy()
        if proxy is None:
            return None
        try:
            import dbus
            iface = dbus.Interface(proxy, _DBUS_IFACE)
            return getattr(iface, method)(*args)
        except Exception as e:
            self.logger.warning(f"D-Bus call {method} failed: {e}")
            # Mark as unavailable so we retry next time
            self._dbus_available = None
            self._proxy = None
            return None

    def reconnect(self):
        """Force a reconnect attempt (e.g. after extension reload)."""
        self._proxy = None
        self._dbus_available = None
        return self._get_proxy() is not None

    @property
    def available(self) -> bool:
        """True if the GNOME extension D-Bus service is reachable."""
        if self._dbus_available is None:
            self._get_proxy()
        return self._dbus_available is True

    # ── Window operations ────────────────────────────────────────────

    def list_windows(self) -> list:
        """List all normal windows. Returns list of dicts."""
        result = self._call("ListWindows")
        if result is not None:
            try:
                return json.loads(str(result))
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: wmctrl
        if self._fallback_wmctrl:
            return self._wmctrl_list_windows()
        return []

    def get_active_window(self) -> Optional[dict]:
        """Get the currently focused window."""
        result = self._call("GetActiveWindow")
        if result is not None:
            try:
                data = json.loads(str(result))
                return data if data else None
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: xprop
        if self._fallback_wmctrl:
            return self._xprop_active_window()
        return None

    def find_window(self, app_name: str = None, title: str = None,
                    wm_class: str = None) -> Optional[dict]:
        """Find the first window matching any of the criteria."""
        windows = self.list_windows()
        search = (app_name or "").lower()
        title_search = (title or "").lower()
        class_search = (wm_class or "").lower()

        for win in windows:
            win_title = win.get("title", "").lower()
            win_class = win.get("wm_class", "").lower()
            if search and (search in win_title or search in win_class):
                return win
            if title_search and title_search in win_title:
                return win
            if class_search and class_search in win_class:
                return win
        return None

    def focus_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Activate/raise a window."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("FocusWindow", wid)
        if result is not None:
            return bool(result)
        if self._fallback_wmctrl:
            return self._wmctrl_action(wid, "focus")
        return False

    def close_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Gracefully close a window."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("CloseWindow", wid)
        if result is not None:
            return bool(result)
        if self._fallback_wmctrl:
            return self._wmctrl_action(wid, "close")
        return False

    def minimize_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Minimize a window."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("MinimizeWindow", wid)
        if result is not None:
            return bool(result)
        if self._fallback_wmctrl:
            return self._wmctrl_action(wid, "minimize")
        return False

    def maximize_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Maximize a window."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("MaximizeWindow", wid)
        if result is not None:
            return bool(result)
        if self._fallback_wmctrl:
            return self._wmctrl_action(wid, "maximize")
        return False

    def unmaximize_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Restore a window from maximized state."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("UnmaximizeWindow", wid)
        return bool(result) if result is not None else False

    def fullscreen_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Make a window fullscreen."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("FullscreenWindow", wid)
        if result is not None:
            return bool(result)
        if self._fallback_wmctrl:
            return self._wmctrl_action(wid, "fullscreen")
        return False

    def unfullscreen_window(self, window_id: int = None, app_name: str = None) -> bool:
        """Restore a window from fullscreen."""
        wid = self._resolve_id(window_id, app_name)
        if wid is None:
            return False
        result = self._call("UnfullscreenWindow", wid)
        return bool(result) if result is not None else False

    def move_resize_window(self, window_id: int, x: int, y: int, w: int, h: int) -> bool:
        """Move and resize a window."""
        result = self._call("MoveResizeWindow", window_id, x, y, w, h)
        return bool(result) if result is not None else False

    # ── Workspace operations (extension only) ────────────────────────

    def list_workspaces(self) -> list:
        """List workspaces. Returns list of dicts."""
        result = self._call("ListWorkspaces")
        if result is not None:
            try:
                return json.loads(str(result))
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def switch_workspace(self, index: int) -> bool:
        """Switch to workspace by index."""
        result = self._call("SwitchWorkspace", index)
        return bool(result) if result is not None else False

    def move_window_to_workspace(self, window_id: int, ws_index: int) -> bool:
        """Move a window to a different workspace."""
        result = self._call("MoveWindowToWorkspace", window_id, ws_index)
        return bool(result) if result is not None else False

    # ── Volume control (pactl) ───────────────────────────────────────

    def get_volume(self) -> Optional[int]:
        """Get current sink volume as percentage."""
        try:
            result = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                # Parse "Volume: front-left: 65536 / 100% / ..."
                for part in result.stdout.split("/"):
                    part = part.strip()
                    if part.endswith("%"):
                        return int(part[:-1])
        except Exception as e:
            self.logger.warning(f"get_volume failed: {e}")
        return None

    def set_volume(self, percent: int) -> bool:
        """Set sink volume (0-150)."""
        percent = max(0, min(150, percent))
        try:
            result = subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except Exception as e:
            self.logger.warning(f"set_volume failed: {e}")
            return False

    def toggle_mute(self) -> bool:
        """Toggle mute on the default sink."""
        try:
            result = subprocess.run(
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except Exception as e:
            self.logger.warning(f"toggle_mute failed: {e}")
            return False

    def is_muted(self) -> Optional[bool]:
        """Check if default sink is muted."""
        try:
            result = subprocess.run(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return "yes" in result.stdout.lower()
        except Exception as e:
            self.logger.warning(f"is_muted failed: {e}")
        return None

    # ── Notifications (notify-send) ──────────────────────────────────

    def send_notification(self, title: str, body: str = "",
                          urgency: str = "normal") -> bool:
        """Send a desktop notification."""
        urgency = urgency if urgency in ("low", "normal", "critical") else "normal"
        try:
            cmd = ["notify-send", f"--urgency={urgency}", title]
            if body:
                cmd.append(body)
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception as e:
            self.logger.warning(f"send_notification failed: {e}")
            return False

    # ── Clipboard (wl-clipboard) ─────────────────────────────────────

    def get_clipboard(self) -> Optional[str]:
        """Get clipboard contents. Requires wl-clipboard."""
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            self.logger.debug("wl-paste not found — install wl-clipboard")
        except Exception as e:
            self.logger.warning(f"get_clipboard failed: {e}")
        return None

    def set_clipboard(self, text: str) -> bool:
        """Set clipboard contents. Requires wl-clipboard."""
        try:
            result = subprocess.run(
                ["wl-copy"],
                input=text, text=True, capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except FileNotFoundError:
            self.logger.debug("wl-copy not found — install wl-clipboard")
            return False
        except Exception as e:
            self.logger.warning(f"set_clipboard failed: {e}")
            return False

    # ── Health ───────────────────────────────────────────────────────

    def get_health(self) -> dict:
        """Return health status for health_check.py integration."""
        return {
            "extension_available": self.available,
            "extension_uuid": self._extension_uuid,
            "fallback_wmctrl": self._fallback_wmctrl,
        }

    # ── Private helpers ──────────────────────────────────────────────

    def _resolve_id(self, window_id: Optional[int], app_name: Optional[str]) -> Optional[int]:
        """Resolve a window ID from either an explicit ID or an app name search."""
        if window_id is not None:
            return window_id
        if app_name:
            win = self.find_window(app_name=app_name)
            if win:
                return win.get("id")
        return None

    # ── wmctrl fallback helpers ──────────────────────────────────────

    def _wmctrl_list_windows(self) -> list:
        """List windows using wmctrl -l -p (XWayland only)."""
        try:
            result = subprocess.run(
                ["wmctrl", "-l", "-p"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []

            windows = []
            for line in result.stdout.strip().splitlines():
                # Format: "0x01234567  0 12345 hostname Window Title"
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                windows.append({
                    "id": int(parts[0], 16),
                    "title": parts[4],
                    "wm_class": "",
                    "pid": int(parts[2]) if parts[2].isdigit() else 0,
                    "workspace": int(parts[1]) if parts[1].isdigit() else 0,
                    "monitor": 0,
                    "minimized": False,
                    "maximized": False,
                    "fullscreen": False,
                    "x": 0, "y": 0, "width": 0, "height": 0,
                    "_wmctrl_id": parts[0],  # hex string for wmctrl commands
                })
            return windows
        except FileNotFoundError:
            self.logger.debug("wmctrl not found")
            return []
        except Exception as e:
            self.logger.warning(f"wmctrl list failed: {e}")
            return []

    def _xprop_active_window(self) -> Optional[dict]:
        """Get active window via xprop (XWayland only)."""
        try:
            result = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "window id #" in result.stdout.lower():
                parts = result.stdout.strip().split()
                if parts:
                    hex_id = parts[-1]
                    if hex_id.startswith("0x"):
                        return {
                            "id": int(hex_id, 16),
                            "title": "",
                            "wm_class": "",
                            "_wmctrl_id": hex_id,
                        }
        except Exception as e:
            self.logger.debug(f"xprop active window failed: {e}")
        return None

    def _wmctrl_action(self, window_id: int, action: str) -> bool:
        """Perform a wmctrl action on a window."""
        # We need a hex string for wmctrl
        hex_id = f"0x{window_id:08x}"

        try:
            if action == "close":
                subprocess.run(["wmctrl", "-i", "-c", hex_id],
                               timeout=5, capture_output=True)
            elif action == "focus":
                subprocess.run(["wmctrl", "-i", "-a", hex_id],
                               timeout=5, capture_output=True)
            elif action == "minimize":
                subprocess.run(["wmctrl", "-i", "-r", hex_id, "-b", "add,hidden"],
                               timeout=5, capture_output=True)
            elif action == "maximize":
                subprocess.run(["wmctrl", "-i", "-r", hex_id, "-b", "remove,fullscreen"],
                               timeout=5, capture_output=True)
                subprocess.run(["wmctrl", "-i", "-r", hex_id, "-b", "add,maximized_vert,maximized_horz"],
                               timeout=5, capture_output=True)
            elif action == "fullscreen":
                subprocess.run(["wmctrl", "-i", "-r", hex_id, "-b", "remove,maximized_vert,maximized_horz"],
                               timeout=5, capture_output=True)
                subprocess.run(["wmctrl", "-i", "-r", hex_id, "-b", "add,fullscreen"],
                               timeout=5, capture_output=True)
            else:
                return False
            return True
        except Exception as e:
            self.logger.warning(f"wmctrl {action} failed: {e}")
            return False
