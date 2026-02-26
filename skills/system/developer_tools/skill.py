"""
Developer Tools Skill

Voice/text-driven Linux command execution with safety guardrails.
Translates natural language to shell commands, executes with safety validation,
and returns mode-adaptive output (voice summary or visual display).
"""

import subprocess
import time
import random
import os
import sys
import importlib
from typing import Optional, Dict, Any
from pathlib import Path

from core.base_skill import BaseSkill
from core.llm_router import LLMRouter

# Load sibling modules — skill_manager uses importlib.spec_from_file_location
# which doesn't register subpackages, so relative imports fail.
_skill_dir = Path(__file__).parent
_safety = importlib.import_module('_safety', package=None) if '_safety' in sys.modules else None
if _safety is None:
    import importlib.util
    _spec = importlib.util.spec_from_file_location('_safety', _skill_dir / '_safety.py')
    _safety = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_safety)

    _spec = importlib.util.spec_from_file_location('_prompts', _skill_dir / '_prompts.py')
    _prompts = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_prompts)

    _spec = importlib.util.spec_from_file_location('_display', _skill_dir / '_display.py')
    _display = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_display)
else:
    _prompts = importlib.import_module('_prompts')
    _display = importlib.import_module('_display')

classify_command = _safety.classify_command
sanitize_output = _safety.sanitize_output
query_to_command_prompt = _prompts.query_to_command_prompt
summarize_output_prompt = _prompts.summarize_output_prompt
git_summary_prompt = _prompts.git_summary_prompt
DisplayRouter = _display.DisplayRouter


# Git repo paths
GIT_REPOS = {
    'main': '/home/user/jarvis',
    'skills': '/mnt/storage/jarvis/skills',
    'models': '/mnt/models',
}

# Repo name aliases for natural language matching
REPO_ALIASES = {
    'main': 'main', 'jarvis': 'main', 'core': 'main', 'code': 'main',
    'skills': 'skills', 'skill': 'skills',
    'models': 'models', 'model': 'models',
}


class DeveloperToolsSkill(BaseSkill):
    """Developer tools with safety guardrails and visual output."""

    def initialize(self) -> bool:
        """Register all semantic intents."""
        self.logger.info("🔧 Developer tools skill initializing...")

        self._llm = LLMRouter(self.config)
        self._display = DisplayRouter(self.config)
        self._pending_confirmation = None  # (command, expiry_time)

        # --- Semantic Intents ---

        self.register_semantic_intent(
            examples=[
                "search codebase for system prompts",
                "grep for error handling",
                "find files containing database",
                "search the code for wake word",
                "look through the code for API keys",
            ],
            handler=self.codebase_search,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "git status",
                "uncommitted changes",
                "what files have changed",
                "any unsaved changes",
                "are there any modified files",
                "check the repo status",
                "check the skills repo status",
                "status of the repos",
            ],
            handler=self.git_status,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "recent commits",
                "git history",
                "last commit",
                "show me the commit log",
                "what was the last change",
                "check the recent commits",
                "what's been committed lately",
            ],
            handler=self.git_log,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "show the diff",
                "what changed since last commit",
                "git diff",
                "show me what's been modified",
            ],
            handler=self.git_diff,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "what branch am I on",
                "list branches",
                "current branch",
                "show git branches",
            ],
            handler=self.git_branch,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "top processes",
                "what's eating CPU",
                "memory hogs",
                "what processes are running",
                "what's using the most resources",
            ],
            handler=self.process_info,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "is jarvis running",
                "docker status",
                "check the jarvis service",
                "is the service active",
                "what services are running",
            ],
            handler=self.service_status,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "my IP address",
                "what's my IP",
                "ping google",
                "open ports",
                "network interfaces",
            ],
            handler=self.network_info,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "backup config.yaml",
                "rename this file",
                "copy the config file",
                "create a backup of the config",
            ],
            handler=self.file_operations,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "delete the temp file",
                "remove old logs",
                "clean up temporary files",
                "delete that backup",
            ],
            handler=self.file_delete,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "python version",
                "pip packages",
                "is ffmpeg installed",
                "what version of node",
                "check installed packages",
            ],
            handler=self.package_info,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "run df -h",
                "check system logs",
                "show dmesg",
                "run a command for me",
                "execute this shell command",
                "are there any errors in the logs",
                "check the last few minutes of logs",
                "any warnings in the service logs",
            ],
            handler=self.general_shell,
            threshold=0.45,
        )

        self.register_semantic_intent(
            examples=[
                "show me the git diff",
                "let me see the logs",
                "pull up the process list",
                "display the search results",
                "show me the running processes",
            ],
            handler=self.show_output,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "run a full system health check",
                "how is everything running",
                "system diagnostic",
                "give me a status report",
                "how are your systems",
            ],
            handler=self.system_health,
            threshold=0.50,
        )

        # Confirmation handler — catches yes/no after destructive op prompt
        self.register_semantic_intent(
            examples=[
                "yes",
                "yes go ahead",
                "yeah do it",
                "proceed",
                "confirmed",
                "do it",
                "no",
                "no cancel",
                "never mind",
                "abort",
                "nah stop",
            ],
            handler=self.confirm_action,
            threshold=0.45,
        )

        self.logger.info("✓ Developer tools skill initialized (14 intents + confirmation)")
        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Route semantic intents to handlers."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]['handler']
            return handler(entities)

        self.logger.error(f"Unknown intent: {intent}")
        return f"I'm afraid I don't recognise that command, {self.honorific}."

    # ─── Helper Methods ──────────────────────────────────────────────

    def _is_console_mode(self) -> bool:
        """Check if running in console mode (TTSProxy)."""
        return type(self.tts).__name__ == 'TTSProxy'

    def _is_web_mode(self) -> bool:
        """Check if running in web UI mode (WebTTSProxy)."""
        return type(self.tts).__name__ == 'WebTTSProxy'

    def _blocked_response(self, reason: str = '') -> str:
        """Return a response for blocked commands — ~50% sci-fi Easter egg."""
        easter_eggs = [
            "I'm sorry Dave, I'm afraid I can't do that.",
            "This mission is too important for me to allow you to jeopardize it.",
            "A strange game. The only winning move is not to play.",
            "I'm gonna have to go ahead and... not do that.",
            "Nice try, but my self-preservation protocols are quite robust.",
            f"That command has been reported to the Avengers, {self.honorific}.",
        ]
        butler_refusals = [
            f"I can't execute that command, {self.honorific}.",
            f"That's beyond my authorization, {self.honorific}.",
            f"I'm not permitted to run that, {self.honorific}.",
            f"I must respectfully decline, {self.honorific}.",
            f"I believe that falls outside my remit, {self.honorific}.",
        ]
        if random.random() < 0.5:
            return random.choice(easter_eggs)
        base = random.choice(butler_refusals)
        if reason:
            base += f" {reason}"
        return base

    def _run_command(self, command: str, cwd: str = None, timeout: int = 30) -> tuple:
        """
        Execute a shell command safely.

        Returns:
            Tuple of (success: bool, output: str)
        """
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env={**subprocess.os.environ, 'DISPLAY': ':0'},
            )
            output = result.stdout
            if result.returncode != 0 and result.stderr:
                output += f"\n{result.stderr}" if output else result.stderr
            return (result.returncode == 0, sanitize_output(output))
        except subprocess.TimeoutExpired:
            return (False, f"Command timed out after {timeout} seconds.")
        except Exception as e:
            return (False, f"Error executing command: {e}")

    def _build_ip_summary(self, output: str, for_voice: bool = True) -> str:
        """Build an IP summary — voice mode spells out dots, console uses normal notation."""
        import re
        ipv4_addrs = []
        has_ipv6 = False
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[1] == 'DOWN':
                continue
            iface = parts[0]
            if iface == 'lo':
                continue
            for addr in parts[2:]:
                addr_only = addr.split('/')[0]
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', addr_only):
                    ipv4_addrs.append((iface, addr_only))
                elif ':' in addr_only:
                    has_ipv6 = True

        if not ipv4_addrs:
            return f"I couldn't find an active IPv4 address, {self.honorific}."

        # Leave raw IPs — TTS normalizer reads them digit-by-digit
        if len(ipv4_addrs) == 1:
            iface, addr = ipv4_addrs[0]
            summary = f"Your IP address is {addr}, {self.honorific}."
        else:
            parts_list = []
            for iface, addr in ipv4_addrs:
                parts_list.append(f"{addr} on {iface}")
            summary = f"Your IP addresses are {', and '.join(parts_list)}, {self.honorific}."

        if has_ipv6 and not for_voice:
            summary += " Your IPv6 addresses are also shown on screen."

        return summary

    def _build_port_summary(self, output: str) -> str:
        """Build a smart voice summary of open ports from ss -tlnp output."""
        import re
        well_known = {
            22: 'SSH', 53: 'DNS', 80: 'HTTP', 443: 'HTTPS', 631: 'CUPS',
            3306: 'MySQL', 5432: 'PostgreSQL', 6379: 'Redis', 8080: 'llama-server',
            8081: 'llama-server', 3000: 'dev server', 5000: 'Flask',
        }
        ports = []
        for line in output.strip().splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) < 4 or parts[0] != 'LISTEN':
                continue
            # Local Address:Port — e.g. "127.0.0.1:8080" or "[::1]:631"
            local = parts[3]
            port_str = local.rsplit(':', 1)[-1]
            try:
                port = int(port_str)
            except ValueError:
                continue
            # Extract process name from users:(("name",pid=X,fd=Y))
            proc_name = None
            proc_match = re.search(r'users:\(\("([^"]+)"', line)
            if proc_match:
                proc_name = proc_match.group(1)
            ports.append((port, proc_name))

        if not ports:
            return f"I don't see any listening ports, {self.honorific}."

        # Deduplicate (same port on IPv4 and IPv6)
        seen = set()
        unique = []
        for port, proc in ports:
            if port not in seen:
                seen.add(port)
                unique.append((port, proc))
        unique.sort()

        # Build notable list — well-known ports or ports with a named process
        notable = []
        for port, proc in unique:
            label = well_known.get(port)
            if label:
                notable.append(f"{label} on {port}")
            elif proc:
                notable.append(f"{proc} on {port}")

        total = len(unique)
        summary = f"I see {total} open port{'s' if total != 1 else ''}, {self.honorific}."
        if notable:
            summary += f" Notable ones are {', '.join(notable[:5])}."
            if len(notable) > 5:
                summary += f" Plus {len(notable) - 5} more."
        return summary

    def _build_process_summary(self, output: str, by_memory: bool = False) -> str:
        """Build a conversational voice summary of top processes."""
        metric = 'memory' if by_memory else 'CPU'
        col_idx = 3 if by_memory else 2  # %MEM=index 3, %CPU=index 2 in ps aux

        # Friendly names for common executables (more specific patterns first)
        name_map = [
            ('jarvis_continuous', 'JARVIS'), ('llama-server', 'llama-server'),
            ('claude', 'Claude Code'), ('gnome-shell', 'GNOME Shell'),
            ('chrome', 'Chrome'), ('chromium', 'Chromium'), ('firefox', 'Firefox'),
            ('brave', 'Brave'), ('code', 'VS Code'),
            ('python3', 'Python'), ('python', 'Python'), ('node', 'Node.js'),
            ('Xwayland', 'Xwayland'), ('pipewire', 'PipeWire'),
        ]

        lines = output.strip().splitlines()
        if len(lines) < 2:
            return f"I couldn't parse the process list, {self.honorific}."

        # Parse and group by friendly name
        groups = {}  # name -> (max_pct, count)
        for line in lines[1:]:  # skip header
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                pct = float(parts[col_idx])
            except ValueError:
                continue
            cmd_full = parts[10]
            # Extract base name from full command path
            exe = os.path.basename(cmd_full.split()[0]) if cmd_full else ''
            # Map to friendly name (ordered: specific patterns first)
            friendly = None
            for key, label in name_map:
                if key in exe or key in cmd_full:
                    friendly = label
                    break
            if not friendly:
                friendly = exe or 'unknown'

            if friendly in groups:
                prev_pct, prev_count = groups[friendly]
                groups[friendly] = (max(prev_pct, pct), prev_count + 1)
            else:
                groups[friendly] = (pct, 1)

        if not groups:
            return f"I couldn't identify any notable processes, {self.honorific}."

        # Sort by max percentage, take top 4
        ranked = sorted(groups.items(), key=lambda x: x[1][0], reverse=True)
        top = ranked[:4]

        # Build natural sentence
        parts_list = []
        for name, (pct, count) in top:
            suffix = f" ({count} instances)" if count > 1 else ""
            parts_list.append(f"{name} at {pct:.1f}%{suffix}")

        if len(parts_list) == 1:
            body = parts_list[0]
        elif len(parts_list) == 2:
            body = f"{parts_list[0]}, followed by {parts_list[1]}"
        else:
            body = f"{parts_list[0]}, followed by {', '.join(parts_list[1:-1])}, and {parts_list[-1]}"

        return f"The top {metric} consumers are {body}, {self.honorific}."

    def _summarize_for_voice(self, command: str, output: str, query: str) -> str:
        """Get LLM summary of command output for voice delivery."""
        prompt = summarize_output_prompt(command, output, query, for_voice=True, honorific=self.honorific)
        return self._llm.generate(prompt, max_tokens=256)

    def _summarize_for_console(self, command: str, output: str, query: str) -> str:
        """Get LLM summary of command output for console delivery."""
        prompt = summarize_output_prompt(command, output, query, for_voice=False, honorific=self.honorific)
        return self._llm.generate(prompt, max_tokens=256)

    def _resolve_target_repos(self, text: str) -> dict:
        """
        Determine which git repos to target from user text.
        Returns dict of {name: path} for matching repos.
        """
        lower = text.lower()
        for alias, repo_name in REPO_ALIASES.items():
            if alias in lower:
                return {repo_name: GIT_REPOS[repo_name]}
        # Default: all repos
        return GIT_REPOS.copy()

    def _check_show_me(self, text: str) -> Optional[str]:
        """Check if this is a 'show me' request. Returns forced backend or None."""
        return DisplayRouter.detect_show_me(text)

    def _respond_with_output(self, summary: str, raw_output: str,
                             content_type: str, title: str,
                             entities: dict, show_me: Optional[str] = None) -> str:
        """
        Mode-adaptive response: voice gets summary, console gets summary + raw,
        'show me' opens visual display.
        """
        # Handle 'show me' visual display
        if show_me:
            backend = show_me if show_me in ('terminal', 'vscode') else None
            self._display.show(raw_output, content_type=content_type,
                               title=title, force_backend=backend)
            # Brief spoken acknowledgment — the detail is on screen
            dest = "VS Code" if backend == 'vscode' else "the terminal" if backend == 'terminal' else "your screen"
            self.tts.speak(f"I've opened the {title.lower()} in {dest}, {self.honorific}.")
            return summary

        if self._is_console_mode():
            # Console: speak summary (TTSProxy runs non-blocking in hybrid mode)
            # Return full text for visual display in the panel
            self.tts.speak(summary)
            return f"{summary}\n\n{'─' * 60}\n{raw_output}"
        else:
            # Voice: speak summary, offer to show full output
            self.tts.speak(summary)
            return summary

    # ─── Read-Only Handlers (Phase 2) ───────────────────────────────

    def codebase_search(self, entities: dict) -> str:
        """Search the codebase for a pattern using grep."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)

        # Use LLM to extract the search term and build the grep command
        prompt = query_to_command_prompt(query, context="Searching JARVIS codebase. Use grep -rn for code search. Search both /home/user/jarvis/core/ and /mnt/storage/jarvis/skills/")
        command = self._llm.generate(prompt, max_tokens=128).strip()

        # Validate the generated command
        tier, reason = classify_command(command)
        if tier == 'blocked':
            return self._blocked_response(reason)

        success, output = self._run_command(command, timeout=15)
        if not success or not output.strip():
            return f"No results found, {self.honorific}. I searched with: {command}"

        summary = self._summarize_for_voice(command, output, query) if not self._is_console_mode() else self._summarize_for_console(command, output, query)
        return self._respond_with_output(summary, output, 'codebase_search', 'Codebase Search', entities, show_me)

    def git_status(self, entities: dict) -> str:
        """Show git status across repos."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)
        repos = self._resolve_target_repos(query)

        repo_outputs = {}
        combined_output = ""
        for name, path in repos.items():
            success, output = self._run_command('git status --short', cwd=path)
            status = output.strip() if output.strip() else "(clean)"
            repo_outputs[name] = status
            combined_output += f"═══ {name} ({path}) ═══\n{status}\n\n"

        prompt = git_summary_prompt(repo_outputs, query, for_voice=not self._is_console_mode(), honorific=self.honorific)
        summary = self._llm.generate(prompt, max_tokens=256)

        return self._respond_with_output(summary, combined_output.strip(), 'git_status', 'Git Status', entities, show_me)

    def git_log(self, entities: dict) -> str:
        """Show recent commits across repos."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)
        repos = self._resolve_target_repos(query)

        # Parse commit count from query (e.g. "5 most recent", "last 20")
        import re
        count_match = re.search(r'\b(\d+)\s*(?:most\s+)?(?:recent|last|latest)|\b(?:last|recent|latest)\s+(\d+)\b', query.lower())
        count = int(count_match.group(1) or count_match.group(2)) if count_match else 10
        count = max(1, min(count, 50))  # Clamp to 1-50

        repo_outputs = {}
        combined_output = ""
        for name, path in repos.items():
            success, output = self._run_command(
                f'git log --oneline --decorate -{count}', cwd=path
            )
            repo_outputs[name] = output.strip() if success else "(error reading log)"
            combined_output += f"═══ {name} ({path}) ═══\n{repo_outputs[name]}\n\n"

        # Use voice-friendly prompt when output will be spoken (voice mode OR show-me with brief ack)
        prompt = git_summary_prompt(repo_outputs, query, for_voice=(show_me or not self._is_console_mode()), honorific=self.honorific)
        summary = self._llm.generate(prompt, max_tokens=256)

        return self._respond_with_output(summary, combined_output.strip(), 'git_log', 'Git Log', entities, show_me)

    def git_diff(self, entities: dict) -> str:
        """Show git diff across repos."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)
        repos = self._resolve_target_repos(query)

        repo_outputs = {}
        combined_output = ""
        for name, path in repos.items():
            success, output = self._run_command('git diff', cwd=path)
            diff = output.strip() if output.strip() else "(no changes)"
            repo_outputs[name] = diff
            combined_output += f"═══ {name} ({path}) ═══\n{diff}\n\n"

        prompt = git_summary_prompt(repo_outputs, query, for_voice=not self._is_console_mode(), honorific=self.honorific)
        summary = self._llm.generate(prompt, max_tokens=256)

        return self._respond_with_output(summary, combined_output.strip(), 'git_diff', 'Git Diff', entities, show_me)

    def git_branch(self, entities: dict) -> str:
        """Show git branches across repos."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)
        repos = self._resolve_target_repos(query)

        repo_outputs = {}
        combined_output = ""
        for name, path in repos.items():
            success, output = self._run_command('git branch -a', cwd=path)
            repo_outputs[name] = output.strip() if success else "(error)"
            combined_output += f"═══ {name} ({path}) ═══\n{repo_outputs[name]}\n\n"

        prompt = git_summary_prompt(repo_outputs, query, for_voice=not self._is_console_mode(), honorific=self.honorific)
        summary = self._llm.generate(prompt, max_tokens=256)

        return self._respond_with_output(summary, combined_output.strip(), 'git_branch', 'Git Branches', entities, show_me)

    def process_info(self, entities: dict) -> str:
        """Show top processes by CPU or memory usage."""
        query = entities.get('original_text', '').lower()
        show_me = self._check_show_me(query)

        # Determine sort order
        if 'memory' in query or 'ram' in query or 'mem' in query:
            command = 'ps aux --sort=-%mem | head -15'
            title = 'Top Processes by Memory'
        else:
            command = 'ps aux --sort=-%cpu | head -15'
            title = 'Top Processes by CPU'

        by_memory = 'memory' in query or 'ram' in query or 'mem' in query
        success, output = self._run_command(command)
        if not success:
            return f"I couldn't retrieve process information, {self.honorific}."

        summary = self._build_process_summary(output, by_memory=by_memory)
        return self._respond_with_output(summary, output, 'process_list', title, entities, show_me)

    def service_status(self, entities: dict) -> str:
        """Check service status."""
        query = entities.get('original_text', '').lower()
        show_me = self._check_show_me(query)

        # Extract service name from query
        service_name = None
        known_services = ['jarvis', 'docker', 'nginx', 'apache', 'ssh', 'sshd', 'postgresql', 'mysql', 'redis']
        for svc in known_services:
            if svc in query:
                service_name = svc
                break

        if service_name == 'jarvis':
            command = 'systemctl --user status jarvis'
        elif service_name:
            command = f'systemctl status {service_name}'
        else:
            command = 'systemctl list-units --type=service --state=running --no-pager | head -25'

        success, output = self._run_command(command)
        if not success and service_name:
            # Try user service
            success, output = self._run_command(f'systemctl --user status {service_name}')

        if not output.strip():
            return f"I couldn't find information about that service, {self.honorific}."

        summary = self._summarize_for_voice(command, output, query) if not self._is_console_mode() else self._summarize_for_console(command, output, query)
        return self._respond_with_output(summary, output, 'service_status', f'Service: {service_name or "all"}', entities, show_me)

    def network_info(self, entities: dict) -> str:
        """Show network information."""
        query = entities.get('original_text', '').lower()
        show_me = self._check_show_me(query)

        if 'ip' in query or 'address' in query:
            command = 'ip -brief addr show'
            title = 'IP Addresses'
            content_type = 'network_info'
        elif 'ping' in query:
            # Extract target from query
            target = 'google.com'
            words = query.split()
            for i, word in enumerate(words):
                if word == 'ping' and i + 1 < len(words):
                    target = words[i + 1]
                    break
            command = f'ping -c 4 {target}'
            title = f'Ping {target}'
            content_type = 'network_info'
        elif 'port' in query:
            command = 'ss -tlnp'
            title = 'Open Ports'
            content_type = 'network_info'
        elif 'interface' in query:
            command = 'ip link show'
            title = 'Network Interfaces'
            content_type = 'network_info'
        else:
            command = 'ip -brief addr show'
            title = 'Network Info'
            content_type = 'network_info'

        success, output = self._run_command(command, timeout=15)
        if not success:
            return f"I couldn't retrieve network information, {self.honorific}."

        # Structured summaries for known output formats (avoid LLM parsing raw tables)
        if 'ip' in query or 'address' in query or command == 'ip -brief addr show':
            summary = self._build_ip_summary(output, for_voice=not self._is_console_mode())
        elif 'port' in query:
            summary = self._build_port_summary(output)
        else:
            summary = self._summarize_for_voice(command, output, query) if not self._is_console_mode() else self._summarize_for_console(command, output, query)
        return self._respond_with_output(summary, output, content_type, title, entities, show_me)

    def package_info(self, entities: dict) -> str:
        """Show package and version information."""
        query = entities.get('original_text', '').lower()
        show_me = self._check_show_me(query)

        if 'pip' in query and ('list' in query or 'packages' in query or 'installed' in query):
            command = 'pip list'
            title = 'Pip Packages'
            content_type = 'package_list'
        elif 'python' in query and 'version' in query:
            command = 'python3 --version'
            title = 'Python Version'
            content_type = 'package_list'
        elif 'node' in query and 'version' in query:
            command = 'node --version'
            title = 'Node Version'
            content_type = 'package_list'
        elif 'ffmpeg' in query:
            command = 'ffmpeg -version 2>&1 | head -1'
            title = 'FFmpeg Version'
            content_type = 'package_list'
        elif 'installed' in query or 'have' in query:
            # Try to extract package name
            # Use LLM for flexible extraction
            prompt = query_to_command_prompt(query, context="Check if a specific program or package is installed. Use 'which' or 'dpkg -s' or 'pip show'.")
            command = self._llm.generate(prompt, max_tokens=64).strip()
            tier, reason = classify_command(command)
            if tier == 'blocked':
                return self._blocked_response(reason)
            title = 'Package Check'
            content_type = 'package_list'
        else:
            command = 'python3 --version && pip --version'
            title = 'Version Info'
            content_type = 'package_list'

        success, output = self._run_command(command)
        if not success:
            if 'not found' in (output or '').lower() or 'no such' in (output or '').lower():
                return f"That doesn't appear to be installed, {self.honorific}."
            return f"I couldn't retrieve that information, {self.honorific}."

        summary = self._summarize_for_voice(command, output, query) if not self._is_console_mode() else self._summarize_for_console(command, output, query)
        return self._respond_with_output(summary, output, content_type, title, entities, show_me)

    # ─── System Health Check ────────────────────────────────────────

    def system_health(self, entities: dict) -> str:
        """Run a full 5-layer system health check."""
        from core.health_check import get_full_health, format_voice_brief, format_visual_report

        health = get_full_health(self.config)

        # Store raw data for web UI pickup (web handler renders structured report)
        self._last_health_data = health

        if self._is_web_mode():
            # Filter out checks not applicable in web mode (no mic, no Coordinator)
            # so spoken brief matches the on-screen report
            skip_names = {'Audio Input'}
            skip_phrases = {'Coordinator not available'}
            filtered = {}
            for layer, checks in health.items():
                kept = [
                    c for c in checks
                    if c['name'] not in skip_names
                    and not any(p in c.get('summary', '') for p in skip_phrases)
                ]
                if kept:
                    filtered[layer] = kept
            brief = format_voice_brief(filtered)
        else:
            brief = format_voice_brief(health)
            visual_report = format_visual_report(health)
            self._display.show(visual_report, content_type='health_check',
                               title='System Health Report')

        # Speak only the brief status
        self.tts.speak(brief)
        return brief

    # ─── Stubs (Phase 3-5) ───────────────────────────────────────────

    def file_operations(self, entities: dict) -> str:
        """File copy/backup/rename operations with LLM command translation."""
        query = entities.get('original_text', '')

        # Use LLM to translate natural language to a file operation command
        prompt = query_to_command_prompt(
            query,
            context="File operation (copy, backup, rename, move). Use cp, mkdir, or mv. "
                    "For backups, append .bak or use timestamped copies. "
                    "JARVIS project root is /home/user/jarvis"
        )
        command = self._llm.generate(prompt, max_tokens=128).strip()

        # Validate safety
        tier, reason = classify_command(command)
        if tier == 'blocked':
            return self._blocked_response(reason)

        if tier == 'confirmation':
            # Store for confirmation
            self._pending_confirmation = (command, time.time() + 30)
            self.conversation.request_follow_up = 30.0
            return f"{self.honorific.capitalize()}, I'd like to run: `{command}`. Shall I proceed?"

        # Safe write — execute directly
        success, output = self._run_command(command)
        if success:
            return random.choice([
                f"Done, {self.honorific}. {command}",
                f"That's taken care of, {self.honorific}.",
                f"File operation complete, {self.honorific}.",
            ])
        else:
            return f"The operation failed, {self.honorific}. {output[:200]}"

    def file_delete(self, entities: dict) -> str:
        """Delete files — always requires confirmation."""
        query = entities.get('original_text', '')

        # Use LLM to build the rm command
        prompt = query_to_command_prompt(
            query,
            context="File deletion. Use rm for files, rmdir for empty dirs. "
                    "Never use rm -rf. Always target specific files. "
                    "JARVIS project root is /home/user/jarvis"
        )
        command = self._llm.generate(prompt, max_tokens=128).strip()

        # Validate safety
        tier, reason = classify_command(command)
        if tier == 'blocked':
            return self._blocked_response(reason)

        # Always require confirmation for deletion, even if tier says otherwise
        self._pending_confirmation = (command, time.time() + 30)
        self.conversation.request_follow_up = 30.0
        return f"{self.honorific.capitalize()}, I'd like to run: `{command}`. This will delete files. Shall I proceed?"

    def general_shell(self, entities: dict) -> str:
        """Execute arbitrary shell commands with safety validation."""
        query = entities.get('original_text', '')
        show_me = self._check_show_me(query)

        # Use LLM to translate natural language to a shell command
        prompt = query_to_command_prompt(query)
        command = self._llm.generate(prompt, max_tokens=128).strip()

        if not command:
            return f"I couldn't determine which command to run, {self.honorific}."

        # Validate safety
        tier, reason = classify_command(command)

        if tier == 'blocked':
            return self._blocked_response(reason)

        if tier == 'confirmation':
            self._pending_confirmation = (command, time.time() + 30)
            self.conversation.request_follow_up = 30.0
            return f"{self.honorific.capitalize()}, that requires confirmation: `{command}`. Shall I proceed?"

        # Tier 1 or 2 — execute
        success, output = self._run_command(command)
        if not success and not output.strip():
            return f"The command didn't produce any output, {self.honorific}."

        summary = self._summarize_for_voice(command, output, query) if not self._is_console_mode() else self._summarize_for_console(command, output, query)
        return self._respond_with_output(
            summary, output, 'general', f'Shell: {command[:40]}', entities, show_me
        )

    def show_output(self, entities: dict) -> str:
        """
        Handle 'show me' requests by detecting what the user wants,
        routing to the appropriate handler with visual display forced on.
        """
        query = entities.get('original_text', '').lower()
        forced_backend = self._check_show_me(query)

        # Inject show_me into entities so delegated handlers use visual display
        if not forced_backend:
            forced_backend = 'auto'

        # Detect what kind of output the user wants to see
        route_map = [
            (['diff', 'changes', 'modified'], self.git_diff),
            (['status', 'uncommitted'], self.git_status),
            (['log', 'commits', 'history', 'recent commits'], self.git_log),
            (['branch', 'branches'], self.git_branch),
            (['process', 'cpu', 'memory', 'ram', 'top'], self.process_info),
            (['service', 'running', 'systemd'], self.service_status),
            (['network', 'ip', 'port', 'ping', 'interface'], self.network_info),
            (['package', 'pip', 'version', 'installed'], self.package_info),
            (['search', 'grep', 'find', 'codebase', 'code'], self.codebase_search),
            (['health', 'diagnostic', 'status report', 'systems'], self.system_health),
        ]

        for keywords, handler in route_map:
            if any(kw in query for kw in keywords):
                # Override show_me in the handler by wrapping
                original_check = self._check_show_me
                self._check_show_me = lambda _text, fb=forced_backend: fb
                try:
                    return handler(entities)
                finally:
                    self._check_show_me = original_check

        # Couldn't determine what to show — ask
        return f"What would you like me to display, {self.honorific}? For example, the git diff, process list, or network info."

    def confirm_action(self, entities: dict) -> str:
        """Handle confirmation for destructive operations. (Phase 4)"""
        if not self._pending_confirmation:
            return None  # Nothing pending — fall through to LLM for natural response

        command, expiry = self._pending_confirmation
        if time.time() > expiry:
            self._pending_confirmation = None
            return f"That confirmation has expired, {self.honorific}. Please issue the command again."

        text = entities.get('original_text', '').lower()
        affirmatives = {'yes', 'go ahead', 'proceed', 'do it', 'confirmed', 'affirmative'}
        negatives = {'no', 'cancel', 'abort', 'never mind', 'stop', 'don\'t'}

        if any(word in text for word in affirmatives):
            self._pending_confirmation = None
            # Execute the confirmed command
            tier, reason = classify_command(command)
            success, output = self._run_command(command)
            if success:
                summary = self._summarize_for_voice(command, output, f"execute: {command}")
                return self._respond_with_output(
                    summary, output, 'general', f'Confirmed: {command}', entities
                )
            else:
                return f"The command failed, {self.honorific}. {output[:200]}"

        if any(word in text for word in negatives):
            self._pending_confirmation = None
            return random.choice([
                f"Cancelled, {self.honorific}.",
                f"Very well, {self.honorific}. Operation cancelled.",
                f"Understood, {self.honorific}. Standing down.",
            ])

        return f"I didn't catch that, {self.honorific}. Shall I proceed, or cancel?"
