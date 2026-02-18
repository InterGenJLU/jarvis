"""
Developer Tools — Safety Module

Three-tier command allowlist with validation and output sanitization.
"""

import re
import shlex
from typing import Tuple

# Tier 1: Always allowed (read-only)
TIER1_ALLOWED = {
    # File inspection
    'cat', 'head', 'tail', 'less', 'wc', 'file', 'stat',
    # Search
    'grep', 'rg', 'find', 'locate', 'which', 'whereis',
    # Listing
    'ls', 'tree', 'du', 'df',
    # Git read-only
    'git status', 'git log', 'git diff', 'git branch', 'git show',
    'git remote', 'git tag', 'git stash list',
    # System info
    'ps', 'top', 'htop', 'free', 'uptime', 'uname', 'lsb_release',
    'lscpu', 'lsblk', 'lsusb', 'lspci', 'hostname', 'whoami', 'id',
    'nproc', 'arch',
    # Network info
    'ip', 'ifconfig', 'ping', 'dig', 'nslookup', 'host', 'ss', 'netstat',
    'curl', 'wget',
    # Service status
    'systemctl status', 'systemctl is-active', 'systemctl list-units',
    'journalctl', 'service --status-all',
    # Package info
    'pip list', 'pip show', 'pip freeze',
    'dpkg -l', 'dpkg -s', 'apt list',
    'python3 --version', 'python --version', 'node --version',
    'npm list', 'ffmpeg -version',
    # Docker read-only
    'docker ps', 'docker images', 'docker logs',
    # Misc
    'date', 'cal', 'env', 'printenv',
}

# Tier 2: Safe writes (non-destructive)
TIER2_SAFE_WRITES = {
    'cp', 'mkdir', 'touch',
    'git add', 'git commit', 'git stash', 'git stash pop',
    'tee', 'chmod', 'chown',
}

# Tier 3: Confirmation required (destructive)
TIER3_CONFIRMATION = {
    'rm', 'rmdir', 'mv', 'kill', 'killall', 'pkill',
    'systemctl stop', 'systemctl restart', 'systemctl start',
    'git reset', 'git clean', 'git checkout', 'git revert',
    'pip install', 'pip uninstall',
    'apt install', 'apt remove', 'apt purge',
    'truncate',
}

# Blocked — never allowed
BLOCKED_COMMANDS = {
    'sudo', 'su', 'dd', 'mkfs', 'fdisk', 'parted',
    'shutdown', 'reboot', 'poweroff', 'halt', 'init',
    'eval', 'exec',
    'passwd', 'useradd', 'userdel', 'usermod',
    'iptables', 'ufw',
    'mount', 'umount',
    'crontab',
}

# Blocked patterns (regex)
BLOCKED_PATTERNS = [
    r'\|\s*bash',          # pipe to bash
    r'\|\s*sh\b',          # pipe to sh
    r'\$\(',              # command substitution
    r'`[^`]+`',           # backtick execution
    r'>\s*/dev/sd',        # write to block devices
    r'>\s*/etc/',          # write to system config
    r'rm\s+-rf?\s+/',      # rm -rf /
    r':\(\)\{',            # fork bomb
    r'>\s*/proc/',         # write to proc
    r'>\s*/sys/',          # write to sys
]


def classify_command(command: str) -> Tuple[str, str]:
    """
    Classify a command into a safety tier.

    Returns:
        Tuple of (tier, reason) where tier is one of:
        'allowed', 'safe_write', 'confirmation', 'blocked'
    """
    stripped = command.strip()

    # Check blocked patterns first
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, stripped):
            return ('blocked', f'Blocked pattern detected: {pattern}')

    # Extract the base command (first word or first two words for compound commands)
    parts = stripped.split()
    if not parts:
        return ('blocked', 'Empty command')

    base_cmd = parts[0]
    two_word = f"{parts[0]} {parts[1]}" if len(parts) > 1 else ""

    # Check blocked commands
    if base_cmd in BLOCKED_COMMANDS:
        return ('blocked', f'Command "{base_cmd}" is never allowed')

    # Check tier 1 (always allowed) — check two-word first for compound commands
    if two_word in TIER1_ALLOWED:
        return ('allowed', f'Read-only command: {two_word}')
    if base_cmd in TIER1_ALLOWED:
        return ('allowed', f'Read-only command: {base_cmd}')

    # Check tier 2 (safe writes)
    if two_word in TIER2_SAFE_WRITES:
        return ('safe_write', f'Safe write operation: {two_word}')
    if base_cmd in TIER2_SAFE_WRITES:
        return ('safe_write', f'Safe write operation: {base_cmd}')

    # Check tier 3 (confirmation required)
    if two_word in TIER3_CONFIRMATION:
        return ('confirmation', f'Destructive operation requires confirmation: {two_word}')
    if base_cmd in TIER3_CONFIRMATION:
        return ('confirmation', f'Destructive operation requires confirmation: {base_cmd}')

    # Unknown command — treat as confirmation required for safety
    return ('confirmation', f'Unknown command "{base_cmd}" — requires confirmation')


def sanitize_output(output: str, max_lines: int = 200, max_chars: int = 8000) -> str:
    """
    Sanitize command output for display/LLM processing.
    Truncates long output and strips ANSI escape codes.
    """
    # Strip ANSI escape codes
    ansi_pattern = re.compile(r'\x1b\[[0-9;]*m')
    clean = ansi_pattern.sub('', output)

    lines = clean.split('\n')
    if len(lines) > max_lines:
        truncated = lines[:max_lines]
        truncated.append(f"\n... ({len(lines) - max_lines} more lines truncated)")
        clean = '\n'.join(truncated)

    if len(clean) > max_chars:
        clean = clean[:max_chars] + f"\n... (truncated at {max_chars} characters)"

    return clean
