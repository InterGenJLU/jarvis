"""App Launcher & Desktop Control skill — launch apps, manage windows, and control volume."""

import os
import re
import shlex
import subprocess
from typing import Optional, Dict, Any

from core.base_skill import BaseSkill
from core.desktop_manager import get_desktop_manager


class AppLauncherSkill(BaseSkill):
    """Launch apps, close them, and manage window state (fullscreen/minimize/maximize)."""

    def initialize(self) -> bool:
        """Register semantic intents for app launching and window management."""

        # Load app aliases from config
        self.apps = self.config.get("app_launcher.apps", {})

        # Get desktop manager for window operations
        self._desktop = get_desktop_manager()

        self.register_semantic_intent(
            examples=[
                "open chrome",
                "launch brave",
                "start firefox",
                "run vs code",
                "launch the terminal",
                "open the calculator",
                "pull up the file manager",
                "start nautilus",
                "open settings",
            ],
            handler=self.launch_app,
            threshold=0.48,
        )

        self.register_semantic_intent(
            examples=[
                "close chrome",
                "close the browser",
                "close the terminal",
                "close the calculator",
                "close settings",
                "shut down firefox",
                "exit vs code",
                "close that application",
                "kill the app",
            ],
            handler=self.close_app,
            threshold=0.48,
        )

        self.register_semantic_intent(
            examples=[
                "fullscreen",
                "make it fullscreen",
                "go fullscreen",
                "fullscreen please",
                "fullscreen chrome",
                "make the window fullscreen",
            ],
            handler=self.fullscreen_app,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "minimize that",
                "minimize the window",
                "minimize chrome",
                "hide the browser",
                "minimize it",
            ],
            handler=self.minimize_app,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "maximize that",
                "maximize the window",
                "maximize chrome",
                "make it bigger",
                "maximize it",
            ],
            handler=self.maximize_app,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "what apps can you launch",
                "show me available apps",
                "what programs do you know",
                "list your applications",
                "what can you open",
            ],
            handler=self.list_apps,
            threshold=0.55,
        )

        # ── Volume control intents ────────────────────────────────────

        self.register_semantic_intent(
            examples=[
                "turn the volume up",
                "louder",
                "increase the volume",
                "raise the volume",
                "volume up",
                "turn it up",
            ],
            handler=self.volume_up,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "turn the volume down",
                "quieter",
                "decrease the volume",
                "lower the volume",
                "volume down",
                "turn it down",
            ],
            handler=self.volume_down,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "mute",
                "unmute",
                "toggle mute",
                "mute the sound",
                "silence the audio",
            ],
            handler=self.toggle_mute,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "what's the volume",
                "volume level",
                "how loud is it",
                "check the volume",
            ],
            handler=self.get_volume,
            threshold=0.50,
        )

        # ── Workspace + window focus intents ─────────────────────────

        self.register_semantic_intent(
            examples=[
                "switch to workspace 2",
                "go to workspace 3",
                "next workspace",
                "previous workspace",
                "workspace 1",
            ],
            handler=self.switch_workspace,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "move this to workspace 2",
                "send chrome to workspace 3",
                "move window to workspace 1",
                "put this on workspace 2",
            ],
            handler=self.move_to_workspace,
            threshold=0.52,
        )

        self.register_semantic_intent(
            examples=[
                "switch to chrome",
                "focus the terminal",
                "bring up vs code",
                "go to brave",
                "show me firefox",
                "switch to the browser",
            ],
            handler=self.focus_app,
            threshold=0.48,
        )

        self.register_semantic_intent(
            examples=[
                "what windows are open",
                "show running apps",
                "list open windows",
                "what's running",
            ],
            handler=self.list_windows,
            threshold=0.50,
        )

        # ── Clipboard intents ────────────────────────────────────────

        self.register_semantic_intent(
            examples=[
                "what's on my clipboard",
                "read the clipboard",
                "paste from clipboard",
                "what did I copy",
            ],
            handler=self.read_clipboard,
            threshold=0.52,
        )

        self.register_semantic_intent(
            examples=[
                "copy that to clipboard",
                "save to clipboard",
                "put that on the clipboard",
                "clipboard copy",
            ],
            handler=self.write_clipboard,
            threshold=0.52,
        )

        self.logger.info(f"App Launcher initialized with {len(self.apps)} configured apps")
        return True

    def handle_intent(self, intent: str, entities: Dict[str, Any]) -> str:
        """Route pattern-based intents. Semantic intents bypass this."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]['handler']
            return handler(entities=entities)
        self.logger.error(f"Unknown intent: {intent}")
        return f"I'm not sure how to handle that, {self.honorific}."

    # ── App name extraction ────────────────────────────────────────────

    def _extract_app_name(self, text: str) -> Optional[str]:
        """Match query text against known app aliases and display names."""
        text_lower = text.lower()
        for alias, app_config in self.apps.items():
            name = app_config.get("name", alias).lower()
            # Check alias first ("chrome"), then display name ("google chrome")
            if alias in text_lower or name in text_lower:
                return alias
        return None

    # ── Launch / Close ─────────────────────────────────────────────────

    def launch_app(self, entities: dict = None) -> str:
        """Launch an application by name."""
        text = (entities or {}).get('original_text', '')
        alias = self._extract_app_name(text)
        if not alias:
            return (
                f"I don't recognize that application, {self.honorific}. "
                f"Say 'what apps can you launch' to see what's available."
            )

        app = self.apps[alias]
        display_name = app.get("name", alias)
        exec_cmd = app.get("exec", alias)

        try:
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")

            cmd_parts = shlex.split(exec_cmd)
            subprocess.Popen(
                cmd_parts,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.logger.info(f"Launched {display_name}: {exec_cmd}")
            return f"Launching {display_name}, {self.honorific}."
        except FileNotFoundError:
            self.logger.error(f"Executable not found for {alias}: {exec_cmd}")
            return f"I couldn't find the executable for {display_name}, {self.honorific}."
        except Exception as e:
            self.logger.error(f"Failed to launch {alias}: {e}")
            return f"Something went wrong launching {display_name}, {self.honorific}."

    def close_app(self, entities: dict = None) -> str:
        """Close an application window gracefully via desktop manager."""
        text = (entities or {}).get('original_text', '')
        alias = self._extract_app_name(text)
        if not alias:
            return f"Which application should I close, {self.honorific}?"

        app = self.apps[alias]
        display_name = app.get("name", alias)

        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."

        win = self._find_app_window(alias)
        if not win:
            return f"I don't see {display_name} running, {self.honorific}."

        if self._desktop.close_window(window_id=win["id"]):
            self.logger.info(f"Closed {display_name} (window {win['id']})")
            return f"Closing {display_name}, {self.honorific}."
        return f"I couldn't close {display_name}, {self.honorific}."

    # ── Window management ──────────────────────────────────────────────

    def fullscreen_app(self, entities: dict = None) -> str:
        """Make a window fullscreen."""
        text = (entities or {}).get('original_text', '')
        win = self._resolve_window(text)
        if not win:
            return self._no_window_response("fullscreen")

        if self._desktop.fullscreen_window(window_id=win["id"]):
            self.logger.info(f"Fullscreened window {win['id']}")
            return f"Done, {self.honorific}."
        return f"I couldn't fullscreen that window, {self.honorific}."

    def minimize_app(self, entities: dict = None) -> str:
        """Minimize a window."""
        text = (entities or {}).get('original_text', '')
        win = self._resolve_window(text)
        if not win:
            return self._no_window_response("minimize")

        if self._desktop.minimize_window(window_id=win["id"]):
            self.logger.info(f"Minimized window {win['id']}")
            return f"Minimized, {self.honorific}."
        return f"I couldn't minimize that window, {self.honorific}."

    def maximize_app(self, entities: dict = None) -> str:
        """Maximize a window."""
        text = (entities or {}).get('original_text', '')
        win = self._resolve_window(text)
        if not win:
            return self._no_window_response("maximize")

        if self._desktop.maximize_window(window_id=win["id"]):
            self.logger.info(f"Maximized window {win['id']}")
            return f"Maximized, {self.honorific}."
        return f"I couldn't maximize that window, {self.honorific}."

    def list_apps(self, entities: dict = None) -> str:
        """List all configured applications."""
        if not self.apps:
            return f"I don't have any applications configured, {self.honorific}."

        names = [app.get("name", alias) for alias, app in self.apps.items()]
        if len(names) == 1:
            app_list = names[0]
        elif len(names) == 2:
            app_list = f"{names[0]} and {names[1]}"
        else:
            app_list = ", ".join(names[:-1]) + f", and {names[-1]}"

        return f"I can launch {app_list}, {self.honorific}."

    # ── Volume control ──────────────────────────────────────────────────

    def _parse_volume_amount(self, text: str) -> int:
        """Extract a volume change amount from text, default 10%."""
        match = re.search(r'(\d+)\s*%?', text)
        if match:
            return int(match.group(1))
        return 10  # default step

    def volume_up(self, entities: dict = None) -> str:
        """Increase the system volume."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        text = (entities or {}).get('original_text', '')
        step = self._parse_volume_amount(text)
        current = self._desktop.get_volume()
        if current is None:
            return f"I couldn't read the current volume, {self.honorific}."
        new_vol = min(150, current + step)
        if self._desktop.set_volume(new_vol):
            return f"Volume up to {new_vol}%, {self.honorific}."
        return f"I couldn't change the volume, {self.honorific}."

    def volume_down(self, entities: dict = None) -> str:
        """Decrease the system volume."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        text = (entities or {}).get('original_text', '')
        step = self._parse_volume_amount(text)
        current = self._desktop.get_volume()
        if current is None:
            return f"I couldn't read the current volume, {self.honorific}."
        new_vol = max(0, current - step)
        if self._desktop.set_volume(new_vol):
            return f"Volume down to {new_vol}%, {self.honorific}."
        return f"I couldn't change the volume, {self.honorific}."

    def toggle_mute(self, entities: dict = None) -> str:
        """Toggle mute on/off."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        if self._desktop.toggle_mute():
            muted = self._desktop.is_muted()
            state = "muted" if muted else "unmuted"
            return f"Audio {state}, {self.honorific}."
        return f"I couldn't toggle mute, {self.honorific}."

    def get_volume(self, entities: dict = None) -> str:
        """Report the current volume level."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        vol = self._desktop.get_volume()
        muted = self._desktop.is_muted()
        if vol is not None:
            mute_note = " (muted)" if muted else ""
            return f"Volume is at {vol}%{mute_note}, {self.honorific}."
        return f"I couldn't check the volume, {self.honorific}."

    # ── Workspace + window focus ─────────────────────────────────────

    _WORD_TO_NUM = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    }

    def _parse_workspace_index(self, text: str) -> Optional[int]:
        """Extract a workspace number from text (0-indexed internally)."""
        text_lower = text.lower()
        if "next" in text_lower:
            return "next"
        if "previous" in text_lower or "prev" in text_lower:
            return "previous"
        # Digit match
        match = re.search(r'\b(\d+)\b', text_lower)
        if match:
            return int(match.group(1)) - 1  # user says 1-based, API is 0-based
        # Word match
        for word, num in self._WORD_TO_NUM.items():
            if word in text_lower:
                return num - 1
        return None

    def switch_workspace(self, entities: dict = None) -> str:
        """Switch to a workspace by number or next/previous."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."

        text = (entities or {}).get('original_text', '')
        target = self._parse_workspace_index(text)

        if target is None:
            return f"Which workspace, {self.honorific}?"

        workspaces = self._desktop.list_workspaces()
        if not workspaces:
            return f"I can't access workspace information, {self.honorific}. The desktop extension may not be active."

        active_idx = next((ws["index"] for ws in workspaces if ws.get("active")), 0)
        n_ws = len(workspaces)

        if target == "next":
            target = min(active_idx + 1, n_ws - 1)
        elif target == "previous":
            target = max(active_idx - 1, 0)

        if not isinstance(target, int) or target < 0 or target >= n_ws:
            return f"Workspace {target + 1} doesn't exist, {self.honorific}. There are {n_ws} workspaces."

        if target == active_idx:
            return f"Already on workspace {target + 1}, {self.honorific}."

        if self._desktop.switch_workspace(target):
            return f"Switched to workspace {target + 1}, {self.honorific}."
        return f"I couldn't switch workspaces, {self.honorific}."

    def move_to_workspace(self, entities: dict = None) -> str:
        """Move a window to a different workspace."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."

        text = (entities or {}).get('original_text', '')
        target = self._parse_workspace_index(text)

        if target is None or target in ("next", "previous"):
            return f"Which workspace should I move it to, {self.honorific}?"

        win = self._resolve_window(text)
        if not win:
            return f"I couldn't find a window to move, {self.honorific}."

        workspaces = self._desktop.list_workspaces()
        n_ws = len(workspaces) if workspaces else 0

        if not isinstance(target, int) or target < 0 or target >= n_ws:
            return f"Workspace {target + 1} doesn't exist, {self.honorific}."

        if self._desktop.move_window_to_workspace(win["id"], target):
            title = win.get("title", "window")[:30]
            return f"Moved {title} to workspace {target + 1}, {self.honorific}."
        return f"I couldn't move the window, {self.honorific}."

    def focus_app(self, entities: dict = None) -> str:
        """Switch focus to a running application."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."

        text = (entities or {}).get('original_text', '')
        alias = self._extract_app_name(text)
        if alias:
            win = self._find_app_window(alias)
            if win:
                display_name = self.apps[alias].get("name", alias)
                if self._desktop.focus_window(window_id=win["id"]):
                    return f"Switching to {display_name}, {self.honorific}."
                return f"I couldn't focus {display_name}, {self.honorific}."
            display_name = self.apps[alias].get("name", alias)
            return f"I don't see {display_name} running, {self.honorific}."

        # Try fuzzy match against all windows
        search = text.lower()
        # Strip common prefixes
        for prefix in ("switch to", "focus", "bring up", "go to", "show me"):
            if search.startswith(prefix):
                search = search[len(prefix):].strip()
                break

        if search:
            win = self._desktop.find_window(app_name=search)
            if win:
                if self._desktop.focus_window(window_id=win["id"]):
                    title = win.get("title", "window")[:30]
                    return f"Switching to {title}, {self.honorific}."

        return f"I couldn't find that application, {self.honorific}."

    def list_windows(self, entities: dict = None) -> str:
        """List currently open windows."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."

        windows = self._desktop.list_windows()
        if not windows:
            return f"I don't see any windows open, {self.honorific}."

        # Group by wm_class for a cleaner summary
        seen = {}
        for win in windows:
            key = win.get("wm_class", "") or win.get("title", "Unknown")
            if key not in seen:
                seen[key] = win.get("title", key)

        if len(seen) <= 5:
            names = list(seen.values())
        else:
            names = list(seen.values())[:5]
            names.append(f"and {len(seen) - 5} more")

        app_list = ", ".join(names)
        return f"You have {len(seen)} applications running: {app_list}, {self.honorific}."

    # ── Clipboard ─────────────────────────────────────────────────────

    def read_clipboard(self, entities: dict = None) -> str:
        """Read the contents of the clipboard."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        content = self._desktop.get_clipboard()
        if content is None:
            return f"I couldn't read the clipboard, {self.honorific}. You may need to install wl-clipboard."
        if not content.strip():
            return f"The clipboard is empty, {self.honorific}."
        # Truncate for speech
        if len(content) > 200:
            content = content[:200] + "... and more"
        return f"On your clipboard: {content}"

    def write_clipboard(self, entities: dict = None) -> str:
        """Copy the last JARVIS response to the clipboard."""
        if not self._desktop:
            return f"Desktop manager is not available, {self.honorific}."
        # Get last assistant response from conversation history
        last_response = None
        if hasattr(self, 'conversation') and self.conversation:
            history = getattr(self.conversation, 'session_history', [])
            for msg in reversed(history):
                if msg.get('role') == 'assistant':
                    last_response = msg.get('content', '')
                    break
        if not last_response:
            return f"I don't have a recent response to copy, {self.honorific}."
        if self._desktop.set_clipboard(last_response):
            return f"Copied to clipboard, {self.honorific}."
        return f"I couldn't copy to the clipboard, {self.honorific}. You may need to install wl-clipboard."

    # ── Helpers ────────────────────────────────────────────────────────

    def _find_app_window(self, alias: str) -> Optional[dict]:
        """Find a window matching the app alias using desktop manager."""
        if not self._desktop:
            return None

        app = self.apps.get(alias, {})
        display_name = app.get("name", alias).lower()
        exec_cmd = app.get("exec", "").lower()

        # Build search terms from alias, display name, and executable
        search_terms = [alias.lower(), display_name]
        if exec_cmd:
            base_exec = exec_cmd.split()[0].split("/")[-1]
            search_terms.append(base_exec)

        # Search through all windows
        for term in search_terms:
            win = self._desktop.find_window(app_name=term)
            if win:
                return win
        return None

    def _resolve_window(self, text: str) -> Optional[dict]:
        """Resolve a window from text, or fall back to the active window."""
        if not self._desktop:
            return None

        # Try to find a named app in the query
        alias = self._extract_app_name(text)
        if alias:
            win = self._find_app_window(alias)
            if win:
                return win

        # Fall back to the active window
        return self._desktop.get_active_window()

    def _no_window_response(self, action: str) -> str:
        """Response when no window can be found for the requested action."""
        return f"I couldn't find a window to {action}, {self.honorific}."
