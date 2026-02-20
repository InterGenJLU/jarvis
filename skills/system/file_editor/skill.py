"""
File Editor Skill

Voice-driven file creation and editing, sandboxed to ~/jarvis/share/.
Supports write, edit, read, list, and delete operations with LLM-powered
content generation and safety guardrails.
"""

import os
import re
import time
import random
from pathlib import Path
from typing import Optional

from core.base_skill import BaseSkill
from core.llm_router import LLMRouter


# Sandboxed directory — all file operations restricted here
SHARE_DIR = Path(os.path.expanduser("~/jarvis/share"))

# Safety limits
MAX_WRITE_BYTES = 50 * 1024       # 50KB max file write
MAX_EDIT_BYTES = 15 * 1024        # 15KB max file for editing
MAX_EDIT_LINES = 500              # 500 lines max for editing
MAX_FILES_IN_SHARE = 50           # Cap on total files


class FileEditorSkill(BaseSkill):
    """Voice-driven file creation and editing in the share/ directory."""

    def initialize(self) -> bool:
        """Register semantic intents and initialize resources."""
        self.logger.info("File editor skill initializing...")

        self._llm = LLMRouter(self.config)
        self._pending_confirmation = None  # (action, detail, expiry_time)

        # Ensure share directory exists
        SHARE_DIR.mkdir(parents=True, exist_ok=True)

        # --- Semantic Intents ---

        self.register_semantic_intent(
            examples=[
                "write me a python script that prints hello world",
                "create a bash script to back up my documents",
                "generate a config file for nginx",
                "write a file called notes.txt with my meeting agenda",
                "compose a python script to parse JSON files",
                "draft me a shell script to monitor disk usage",
                "make me a script that checks if a service is running",
                "save a new file to the share folder",
            ],
            handler=self.write_file,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "edit jarvis_test.py and change the print message",
                "modify the script in the share to add error handling",
                "update notes.txt to include the new meeting time",
                "change the output message in my python script",
                "rewrite the main function in test.py",
                "fix the bug in my script in the share folder",
                "add a new function to the script in the share",
            ],
            handler=self.edit_file,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "what files are in the share folder",
                "show me what's in the share",
                "list the files in the share directory",
                "what do I have in the share",
                "show my shared files",
                "what's in my share folder",
            ],
            handler=self.list_share_contents,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "read jarvis_test.py from the share",
                "show me the contents of hello.py in the share",
                "what does the script in the share say",
                "read the file I saved in the share folder",
                "display my notes from the share",
                "let me see what's in test.py",
                "what's in the config.txt file",
                "show me what's in jarvis_test.sh",
            ],
            handler=self.read_file,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "delete jarvis_test.py from the share",
                "remove the test script from the share folder",
                "get rid of that file in the share",
                "clean up the share directory",
                "remove notes.txt from the share",
                "delete jarvis_test.sh",
                "remove test.py",
                "delete the file I created",
            ],
            handler=self.delete_file,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "yes", "go ahead", "proceed", "do it", "confirmed",
                "no", "cancel", "abort", "never mind",
            ],
            handler=self.confirm_action,
            threshold=0.80,
        )

        self.logger.info("File editor skill initialized (5 intents + confirmation)")
        return True

    def handle_intent(self, intent: str, entities: dict) -> str:
        """Route pattern-based intents. Semantic intents bypass this."""
        if intent in self.semantic_intents:
            handler = self.semantic_intents[intent]['handler']
            return handler(entities)
        self.logger.error(f"Unknown intent: {intent}")
        return f"I'm sorry, I don't understand that command, {self.honorific}."

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _safe_path(self, filename: str) -> Optional[Path]:
        """Sanitize filename and resolve to share/ directory. Returns None if invalid."""
        if not filename:
            return None
        # Strip directory components — ../../etc/passwd → passwd
        safe_name = Path(filename).name
        if not safe_name or safe_name in ('.', '..'):
            return None
        resolved = (SHARE_DIR / safe_name).resolve()
        # Verify it's still inside share/
        if not str(resolved).startswith(str(SHARE_DIR.resolve())):
            return None
        return resolved

    def _extract_filename(self, text: str) -> Optional[str]:
        """Extract a filename from user text. Tries regex first, then fuzzy match against share/."""
        # Regex: word chars, dots, hyphens — e.g. test.py, my-script.sh, notes.txt
        match = re.search(r'(\w[\w.-]*\.\w+)', text)
        if match:
            return match.group(1)

        # Fuzzy match against existing files in share/
        existing = [f.name for f in SHARE_DIR.iterdir() if f.is_file()] if SHARE_DIR.exists() else []
        text_lower = text.lower()
        for name in existing:
            if name.lower() in text_lower:
                return name

        return None

    def _strip_markdown_fences(self, content: str) -> str:
        """Remove markdown code fences that LLMs sometimes wrap output in."""
        content = content.strip()
        # Remove opening fence: ```python, ```bash, ```, etc.
        content = re.sub(r'^```\w*\s*\n?', '', content)
        # Remove closing fence
        content = re.sub(r'\n?```\s*$', '', content)
        return content

    # ------------------------------------------------------------------
    # Intent: write_file
    # ------------------------------------------------------------------

    def write_file(self, entities: dict) -> str:
        """Create a new file in the share/ directory using LLM content generation."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] write_file request: {user_text[:80]}")

        # Check file count limit
        existing_count = len(list(SHARE_DIR.iterdir())) if SHARE_DIR.exists() else 0
        if existing_count >= MAX_FILES_IN_SHARE:
            return (f"The share folder already has {existing_count} files, {self.honorific}. "
                    "Please delete some before creating new ones.")

        # Step 1: Parse request — extract filename, filetype, description
        parse_prompt = (
            "Extract the filename, file type, and description from this request.\n"
            "If no filename is given, invent a sensible one based on the description.\n"
            "If no file extension is given, infer it from the description.\n\n"
            f"Request: {user_text}\n\n"
            "Respond in EXACTLY this format (3 lines, nothing else):\n"
            "FILENAME: <filename with extension>\n"
            "FILETYPE: <python/bash/text/yaml/json/html/etc>\n"
            "DESCRIPTION: <what the file should contain>"
        )

        parse_result = self._llm.generate(parse_prompt, max_tokens=128)
        filename, filetype, description = self._parse_file_request(parse_result, user_text)

        if not filename:
            return f"I couldn't determine a filename from your request, {self.honorific}. Could you specify one?"

        # Check if file exists — ask for overwrite confirmation
        target = self._safe_path(filename)
        if not target:
            return f"That filename isn't valid, {self.honorific}. Please use a simple name like 'script.py'."

        if target.exists():
            self._pending_confirmation = ('overwrite', {
                'filename': filename,
                'description': description,
                'filetype': filetype,
                'user_text': user_text,
            }, time.time() + 30)
            self.conversation.request_follow_up = 30.0
            return f"{filename} already exists in the share, {self.honorific}. Shall I overwrite it?"

        # Step 2: Generate content
        return self._generate_and_save(filename, filetype, description, user_text)

    def _parse_file_request(self, llm_output: str, original_text: str) -> tuple:
        """Parse structured LLM output into (filename, filetype, description)."""
        filename = None
        filetype = None
        description = original_text  # fallback

        for line in llm_output.strip().splitlines():
            line = line.strip()
            if line.upper().startswith('FILENAME:'):
                filename = line.split(':', 1)[1].strip()
            elif line.upper().startswith('FILETYPE:'):
                filetype = line.split(':', 1)[1].strip()
            elif line.upper().startswith('DESCRIPTION:'):
                description = line.split(':', 1)[1].strip()

        # Sanitize filename
        if filename:
            filename = Path(filename).name
            # Ensure it has an extension
            if '.' not in filename and filetype:
                ext_map = {
                    'python': '.py', 'bash': '.sh', 'shell': '.sh',
                    'text': '.txt', 'yaml': '.yaml', 'json': '.json',
                    'html': '.html', 'css': '.css', 'javascript': '.js',
                    'markdown': '.md', 'csv': '.csv', 'xml': '.xml',
                }
                ext = ext_map.get(filetype.lower(), '.txt')
                filename += ext

        return filename, filetype, description

    def _generate_and_save(self, filename: str, filetype: str, description: str, user_text: str) -> str:
        """Generate file content via LLM and save to share/."""
        gen_prompt = (
            f"Generate the content for a {filetype or 'text'} file.\n\n"
            f"Description: {description}\n"
            f"Original request: {user_text}\n\n"
            "RULES:\n"
            "1. Output ONLY the file content — no markdown fences, no explanations, no preamble.\n"
            "2. If it's code, make it complete and runnable.\n"
            "3. Include appropriate comments in the code.\n"
            "4. Do NOT wrap output in ```.\n"
        )

        content = self._llm.generate(gen_prompt, max_tokens=2048)
        content = self._strip_markdown_fences(content)

        # Check size limit
        if len(content.encode('utf-8')) > MAX_WRITE_BYTES:
            return (f"The generated content exceeds the 50KB limit, {self.honorific}. "
                    "Try a simpler request.")

        # Save
        target = self._safe_path(filename)
        if not target:
            return f"Invalid filename, {self.honorific}."

        target.write_text(content, encoding='utf-8')

        # Make scripts executable
        if filename.endswith(('.sh', '.py', '.bash')):
            target.chmod(target.stat().st_mode | 0o755)

        size = target.stat().st_size
        lines = content.count('\n') + 1
        self.logger.info(f"[file_editor] write_file → share/{filename} ({size} bytes, {lines} lines)")
        return (f"Done, {self.honorific}. I've created {filename} in the share folder — "
                f"{lines} lines, {self._human_size(size)}.")

    # ------------------------------------------------------------------
    # Intent: edit_file
    # ------------------------------------------------------------------

    def edit_file(self, entities: dict) -> str:
        """Edit an existing file in the share/ directory using LLM rewrite."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] edit_file request: {user_text[:80]}")

        filename = self._extract_filename(user_text)
        if not filename:
            # List available files as hint
            files = [f.name for f in SHARE_DIR.iterdir() if f.is_file()] if SHARE_DIR.exists() else []
            if files:
                file_list = ', '.join(files[:10])
                return (f"Which file would you like me to edit, {self.honorific}? "
                        f"I have: {file_list}")
            return f"There are no files in the share folder to edit, {self.honorific}."

        target = self._safe_path(filename)
        if not target or not target.exists():
            return f"I can't find {filename} in the share folder, {self.honorific}."

        # Check size limits
        stat = target.stat()
        if stat.st_size > MAX_EDIT_BYTES:
            return (f"{filename} is too large to edit by voice ({self._human_size(stat.st_size)}), "
                    f"{self.honorific}. The limit is 15KB.")

        content = target.read_text(encoding='utf-8', errors='replace')
        line_count = content.count('\n') + 1
        if line_count > MAX_EDIT_LINES:
            return (f"{filename} has {line_count} lines, which exceeds the editing limit of "
                    f"{MAX_EDIT_LINES}, {self.honorific}.")

        # LLM rewrite
        edit_prompt = (
            f"Here is the current content of {filename}:\n\n"
            f"{content}\n\n"
            f"Edit instruction: {user_text}\n\n"
            "RULES:\n"
            "1. Make ONLY the changes requested. Preserve everything else exactly.\n"
            "2. Output the COMPLETE file content after editing — not a diff, the full file.\n"
            "3. Do NOT wrap output in markdown fences.\n"
            "4. Do NOT add explanations before or after the content.\n"
        )

        new_content = self._llm.generate(edit_prompt, max_tokens=2048)
        new_content = self._strip_markdown_fences(new_content)

        # Save
        target.write_text(new_content, encoding='utf-8')

        new_lines = new_content.count('\n') + 1
        new_size = target.stat().st_size
        self.logger.info(f"[file_editor] edit_file → {filename} ({new_size} bytes, {new_lines} lines)")
        return (f"Done, {self.honorific}. I've updated {filename} — "
                f"{new_lines} lines, {self._human_size(new_size)}.")

    # ------------------------------------------------------------------
    # Intent: list_share
    # ------------------------------------------------------------------

    def list_share_contents(self, entities: dict) -> str:
        """List files in the share/ directory."""
        self.logger.info("[file_editor] list_share_contents")
        if not SHARE_DIR.exists():
            return f"The share folder is empty, {self.honorific}."

        files = sorted(SHARE_DIR.iterdir())
        files = [f for f in files if f.is_file()]

        if not files:
            return f"The share folder is empty, {self.honorific}."

        entries = []
        for f in files:
            size = self._human_size(f.stat().st_size)
            entries.append(f"  {f.name} ({size})")

        listing = '\n'.join(entries)
        count = len(files)
        summary = f"There {'is' if count == 1 else 'are'} {count} file{'s' if count != 1 else ''} in the share folder"

        # Voice mode: just the summary
        # Console: summary + listing
        return f"{summary}, {self.honorific}.\n{listing}"

    # ------------------------------------------------------------------
    # Intent: read_file
    # ------------------------------------------------------------------

    def read_file(self, entities: dict) -> str:
        """Read and display a file from the share/ directory."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] read_file request: {user_text[:80]}")

        filename = self._extract_filename(user_text)
        if not filename:
            files = [f.name for f in SHARE_DIR.iterdir() if f.is_file()] if SHARE_DIR.exists() else []
            if files:
                file_list = ', '.join(files[:10])
                return (f"Which file would you like me to read, {self.honorific}? "
                        f"I have: {file_list}")
            return f"There are no files in the share folder, {self.honorific}."

        target = self._safe_path(filename)
        if not target or not target.exists():
            return f"I can't find {filename} in the share folder, {self.honorific}."

        content = target.read_text(encoding='utf-8', errors='replace')
        lines = content.count('\n') + 1
        size = self._human_size(target.stat().st_size)

        # For voice mode: LLM summary. For console: show full content.
        # We return full content — the pipeline/console handles display.
        # Prefix with a spoken summary, then the raw content.
        header = f"Here's {filename}, {self.honorific} — {lines} lines, {size}:\n\n"
        return header + content

    # ------------------------------------------------------------------
    # Intent: delete_file
    # ------------------------------------------------------------------

    def delete_file(self, entities: dict) -> str:
        """Delete a file from share/ with confirmation."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] delete_file request: {user_text[:80]}")

        filename = self._extract_filename(user_text)
        if not filename:
            files = [f.name for f in SHARE_DIR.iterdir() if f.is_file()] if SHARE_DIR.exists() else []
            if files:
                file_list = ', '.join(files[:10])
                return (f"Which file should I delete, {self.honorific}? "
                        f"I have: {file_list}")
            return f"The share folder is empty, {self.honorific}. Nothing to delete."

        target = self._safe_path(filename)
        if not target or not target.exists():
            return f"I can't find {filename} in the share folder, {self.honorific}."

        # Always require confirmation for delete
        self._pending_confirmation = ('delete', {'filename': filename}, time.time() + 30)
        self.conversation.request_follow_up = 30.0
        return f"Delete {filename} from the share, {self.honorific}? This cannot be undone."

    # ------------------------------------------------------------------
    # Confirmation handler
    # ------------------------------------------------------------------

    def confirm_action(self, entities: dict) -> str:
        """Handle yes/no confirmation for overwrite and delete operations."""
        if not self._pending_confirmation:
            return None  # Nothing pending — fall through to LLM

        action, detail, expiry = self._pending_confirmation

        if time.time() > expiry:
            self._pending_confirmation = None
            return f"That confirmation has expired, {self.honorific}. Please issue the command again."

        text = entities.get('original_text', '').lower()
        affirmatives = {'yes', 'yeah', 'yep', 'go ahead', 'proceed', 'do it', 'confirmed', 'affirmative', 'sure'}
        negatives = {'no', 'nope', 'cancel', 'abort', 'never mind', 'stop', "don't"}

        if any(word in text for word in affirmatives):
            self._pending_confirmation = None

            if action == 'delete':
                target = self._safe_path(detail['filename'])
                if target and target.exists():
                    target.unlink()
                    self.logger.info(f"[file_editor] deleted share/{detail['filename']}")
                    return random.choice([
                        f"Done, {self.honorific}. {detail['filename']} has been deleted.",
                        f"{detail['filename']} removed, {self.honorific}.",
                        f"Deleted, {self.honorific}.",
                    ])
                return f"The file no longer exists, {self.honorific}."

            elif action == 'overwrite':
                self.logger.info(f"[file_editor] overwriting share/{detail['filename']}")
                return self._generate_and_save(
                    detail['filename'], detail['filetype'],
                    detail['description'], detail['user_text']
                )

            return f"Action completed, {self.honorific}."

        if any(word in text for word in negatives):
            self._pending_confirmation = None
            return random.choice([
                f"Cancelled, {self.honorific}.",
                f"Very well, {self.honorific}. Operation cancelled.",
                f"Understood, {self.honorific}. Standing down.",
            ])

        return f"I didn't catch that, {self.honorific}. Should I proceed, or cancel?"

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convert bytes to human-readable size."""
        if size_bytes < 1024:
            return f"{size_bytes} bytes"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
