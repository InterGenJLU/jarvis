"""
File Editor Skill

Voice-driven file creation and editing, sandboxed to ~/jarvis/share/.
Supports write, edit, read, list, delete, and document generation
(PPTX/DOCX/PDF) with LLM-powered content generation, web research,
and image sourcing via Pexels API.
"""

import importlib.util
import json
import os
import re
import shutil
import tempfile
import time
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.base_skill import BaseSkill
from core.llm_router import LLMRouter
from core.web_research import WebResearcher


def _import_sibling(name: str):
    """Import a module from the same directory as this file."""
    path = Path(__file__).parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_doc_gen_mod = _import_sibling("document_generator")
_img_search_mod = _import_sibling("image_search")
DocumentGenerator = _doc_gen_mod.DocumentGenerator
ImageSearch = _img_search_mod.ImageSearch


# Sandboxed directory — all file operations restricted here
SHARE_DIR = Path(os.path.expanduser("~/jarvis/share"))

# Allowed read-only prefixes beyond share/ (absolute paths)
_READ_ALLOWED_PREFIXES = [
    Path(os.path.expanduser("~/jarvis")).resolve(),
    Path(os.path.expanduser("~/Documents")).resolve(),
]
MAX_READ_BYTES = 50 * 1024        # 50KB max for read outside share/

# Safety limits
MAX_WRITE_BYTES = 50 * 1024       # 50KB max file write
MAX_EDIT_BYTES = 15 * 1024        # 15KB max file for editing
MAX_EDIT_LINES = 500              # 500 lines max for editing
MAX_FILES_IN_SHARE = 50           # Cap on total files

# Two-phase generation temperatures
LAYOUT_TEMPERATURE = 1.2    # High creativity for slide type selection
CONTENT_TEMPERATURE = 0.4   # Low temperature for accurate content synthesis

# Valid slide types for the expanded engine
VALID_SLIDE_TYPES = [
    "title_hero", "section_divider", "agenda", "card_grid_3", "card_grid_4",
    "timeline", "data_table", "full_bleed_image", "image_text_reversed",
    "closing", "stat", "comparison", "bullets",
]

_PIPELINE_CACHE_TTL = 600  # 10 minutes


@dataclass
class _PipelineCache:
    """Cached pipeline state for post-generation editing."""
    structure: dict
    research_context: str
    params: dict
    filename: str
    output_path: str
    images: dict
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > _PIPELINE_CACHE_TTL


class FileEditorSkill(BaseSkill):
    """Voice-driven file creation and editing in the share/ directory."""

    def initialize(self) -> bool:
        """Register semantic intents and initialize resources."""
        self.logger.info("File editor skill initializing...")

        self._llm = LLMRouter(self.config)
        self._web_researcher = WebResearcher(self.config)
        self._doc_generator = DocumentGenerator(self.config)
        self._image_search = ImageSearch(config=self.config)
        self._pending_confirmation = None  # (action, detail, expiry_time)
        self._last_generated_file = None   # Path to last doc gen output (for "open it")
        self._pipeline_cache = None        # _PipelineCache for edit/refine follow-ups

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
                "edit the weather skill",
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
                "create a presentation about renewable energy",
                "make a PowerPoint comparing AWS and Azure",
                "prepare a 7 slide presentation on cybersecurity trends",
                "build me a slide deck about machine learning",
                "put together a presentation on the top 5 programming languages",
                "look up cloud providers and make a PowerPoint about them",
                "generate a pptx about network security best practices",
                "create slides comparing Docker and Kubernetes",
                "research the best grooming habits and prepare a presentation",
                "find the top 5 tips and make a slide deck about them",
            ],
            handler=self.create_presentation,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "make slide 2 more technical",
                "change the title to something catchier",
                "remove the fourth slide",
                "swap slides 3 and 5",
                "add a slide about password management",
                "make the conclusion stronger",
                "add another slide after slide 3",
                "insert a new slide as number 3",
                "edit the presentation to add more detail",
                "update slide 4 with better statistics",
                "delete the agenda slide",
                "move the timeline slide to the end",
                "rewrite the bullet points on slide 2",
                "append a slide about cloud security",
                "make it more professional",
                "make the introduction slide more engaging",
                "can you add a table slide comparing the options",
            ],
            handler=self.edit_presentation,
            threshold=0.48,
        )

        self.register_semantic_intent(
            examples=[
                "write a report about climate change impacts",
                "create a document summarizing the project status",
                "prepare a Word document about network architecture",
                "generate a docx outlining our security policies",
                "write up a comparison of database technologies",
                "create a PDF report on quarterly performance",
                "put together a document about Python best practices",
                "draft a report comparing React and Vue",
                "research database options and write a report about them",
                "look into security frameworks and create a document",
            ],
            handler=self.create_document,
            threshold=0.55,
        )

        self.register_semantic_intent(
            examples=[
                "open it",
                "show it onscreen",
                "put it on screen",
                "display the presentation",
                "open the document",
                "show me that onscreen",
                "open the file you just created",
            ],
            handler=self.open_document,
            threshold=0.50,
        )

        self.register_semantic_intent(
            examples=[
                "print it",
                "print the document",
                "print the presentation",
                "send it to the printer",
                "print that for me",
                "make a hard copy",
                "print the file you just created",
            ],
            handler=self.print_document,
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

        self.logger.info("File editor skill initialized (9 intents + confirmation)")
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

    def _resolve_read_path(self, user_text: str) -> Optional[Path]:
        """Resolve a file path for reading.

        Tries in order:
        1. Absolute path from user text (if within allowed prefixes)
        2. Filename extracted → _safe_path (share/ sandbox)

        Returns resolved Path or None.
        """
        # Check for absolute paths in the user text
        abs_match = re.search(r'(/[\w./-]+)', user_text)
        if abs_match:
            candidate = Path(abs_match.group(1))
            if candidate.is_file():
                resolved = candidate.resolve()
                # Block path traversal
                if '..' in str(candidate):
                    return None
                # Check against allowed prefixes
                for prefix in _READ_ALLOWED_PREFIXES:
                    if str(resolved).startswith(str(prefix)):
                        if resolved.stat().st_size <= MAX_READ_BYTES:
                            return resolved
                        self.logger.warning(
                            "[file_editor] read_file: %s exceeds %dKB limit",
                            resolved, MAX_READ_BYTES // 1024,
                        )
                        return None

        # Fall back to share/ sandbox
        filename = self._extract_filename(user_text)
        if filename:
            return self._safe_path(filename)
        return None

    def read_file(self, entities: dict) -> str:
        """Read and display a file from the share/ directory or allowed paths."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] read_file request: {user_text[:80]}")

        target = self._resolve_read_path(user_text)
        if not target:
            # No path resolved — offer share/ listing
            files = [f.name for f in SHARE_DIR.iterdir() if f.is_file()] if SHARE_DIR.exists() else []
            if files:
                file_list = ', '.join(files[:10])
                return (f"Which file would you like me to read, {self.honorific}? "
                        f"I have: {file_list}")
            return f"There are no files in the share folder, {self.honorific}."

        if not target.exists():
            return f"I can't find that file, {self.honorific}."

        content = target.read_text(encoding='utf-8', errors='replace')
        lines = content.count('\n') + 1
        size = self._human_size(target.stat().st_size)

        # For voice mode: LLM summary. For console: show full content.
        # We return full content — the pipeline/console handles display.
        # Prefix with a spoken summary, then the raw content.
        header = f"Here's {target.name}, {self.honorific} — {lines} lines, {size}:\n\n"
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
    # Intent: create_presentation
    # ------------------------------------------------------------------

    # Patterns that indicate modification of an existing presentation
    _EDIT_SIGNAL_RE = re.compile(
        r'\b(add\s+a\s+slide|add\s+another\s+slide|append\s+a\s+slide|'
        r'insert\s+a\s+slide|add\s+slide|remove\s+slide|delete\s+slide|'
        r'swap\s+slide|move\s+slide|reorder\s+slide|edit\s+slide|'
        r'change\s+slide|update\s+slide|modify\s+slide|'
        r'make\s+(?:the\s+)?(?:\w+\s+)?slide|make\s+slide|make\s+it\b|'
        r'rewrite\s+slide|add\s+a\s+new\s+slide)\b', re.IGNORECASE)

    def create_presentation(self, entities: dict) -> str:
        """Create a PPTX presentation via multi-step pipeline:
        parse request → optional web research → LLM synthesis → slide generation."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] create_presentation request: {user_text[:80]}")
        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_skill_event("file_editor", "create_presentation_entry",
                             {"user_text": user_text[:200]})

        # Cache-aware redirect: if a live pipeline cache exists and the
        # request looks like a modification, route to edit_presentation
        # instead of creating a brand-new presentation (V36 fix).
        if (self._pipeline_cache and not self._pipeline_cache.is_expired()
                and self._EDIT_SIGNAL_RE.search(user_text)):
            self.logger.info("[file_editor] Redirecting to edit_presentation "
                             "(active cache + edit signal detected)")
            _dbg.log_skill_event("file_editor", "create_to_edit_redirect", {
                "user_text": user_text[:200],
                "cache_filename": self._pipeline_cache.filename,
                "cache_age_s": round(time.time() - self._pipeline_cache.created_at, 1),
            })
            return self.edit_presentation(entities)

        # Step 1: Parse the request
        params = self._parse_document_request(user_text)
        if not params:
            return f"I couldn't understand that document request, {self.honorific}. Could you rephrase it?"

        # Override to presentation type
        params["doc_type"] = "presentation"
        if not params.get("filename"):
            params["filename"] = "presentation.pptx"
        if not params["filename"].endswith(".pptx"):
            params["filename"] = Path(params["filename"]).stem + ".pptx"

        return self._generate_document(params)

    # ------------------------------------------------------------------
    # Intent: edit_presentation
    # ------------------------------------------------------------------

    def edit_presentation(self, entities: dict) -> str:
        """Edit/refine/append to the most recently generated presentation."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] edit_presentation request: {user_text[:80]}")
        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()

        # Check for valid pipeline cache
        has_cache = bool(self._pipeline_cache)
        expired = self._pipeline_cache.is_expired() if has_cache else False
        _dbg.log_skill_event("file_editor", "edit_presentation_cache_check", {
            "user_text": user_text[:200],
            "has_cache": has_cache,
            "cache_expired": expired,
            "cache_filename": self._pipeline_cache.filename if has_cache else None,
            "cache_age_s": round(time.time() - self._pipeline_cache.created_at, 1) if has_cache else None,
        })
        if not has_cache or expired:
            self.logger.info("[file_editor] No active pipeline cache, "
                             "routing to create_presentation")
            _dbg.log_skill_event("file_editor", "edit_fallthrough_to_create",
                                 {"reason": "expired" if expired else "no_cache"})
            return self.create_presentation(entities)

        cache = self._pipeline_cache

        # Send cached structure + edit request to LLM for interpretation
        modified_structure = self._interpret_edit(
            cache.structure, user_text, cache.research_context)
        if not modified_structure:
            return (f"I had trouble understanding that edit, {self.honorific}. "
                    "Could you rephrase it?")

        # Re-render from modified structure
        theme_name = cache.params.get('theme', 'professional')
        filename = cache.filename

        # Carry forward existing images + fetch for any new slides
        images = dict(cache.images)
        temp_dir = None
        new_slides = modified_structure.get('slides', [])
        if self._image_search.available:
            temp_dir = tempfile.mkdtemp(prefix="jarvis_doc_")
            for i, slide in enumerate(new_slides):
                if i not in images and slide.get('image_query'):
                    img = self._image_search.search_and_download(
                        slide['image_query'], Path(temp_dir))
                    if img:
                        images[i] = str(img)

        try:
            output_path = self._doc_generator.create_presentation(
                modified_structure, filename=filename, images=images,
                theme_name=theme_name,
            )
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        if not output_path:
            return (f"I encountered an error re-rendering the presentation, "
                    f"{self.honorific}. The previous version is still in "
                    "the share folder.")

        # Update cache with modified structure + reset TTL
        self._pipeline_cache = _PipelineCache(
            structure=modified_structure,
            research_context=cache.research_context,
            params=cache.params,
            filename=filename,
            output_path=str(output_path),
            images=images,
        )
        self._last_generated_file = output_path

        slide_count = len(modified_structure.get('slides', []))
        return (f"Done, {self.honorific}. I've updated {filename} — "
                f"{slide_count} slides. Want any other changes?")

    def _interpret_edit(self, current_structure: dict, edit_request: str,
                        research_context: str = "") -> Optional[dict]:
        """Use LLM to interpret an edit request and return modified structure."""
        compact = json.dumps(current_structure, indent=2)
        if len(compact) > 6000:
            compact = compact[:6000] + "\n... [truncated]"

        research_block = ""
        if research_context:
            truncated = (research_context[:3000]
                         if len(research_context) > 3000 else research_context)
            research_block = (
                f"\nRESEARCH DATA (use for new content):\n{truncated}\n")

        slide_types_str = ", ".join(VALID_SLIDE_TYPES)

        edit_prompt = (
            "You are editing an existing presentation structure.\n\n"
            f"CURRENT STRUCTURE:\n{compact}\n\n"
            f"{research_block}"
            f'EDIT REQUEST: "{edit_request}"\n\n'
            "Apply the requested edit and output the COMPLETE modified "
            "structure as valid JSON.\n\n"
            "EDIT OPERATIONS YOU CAN PERFORM:\n"
            "- MODIFY content: Change title, bullets, stats, descriptions\n"
            "- ADD slide: Insert a new slide at a specific position or end\n"
            "- REMOVE slide: Delete a slide by number or description\n"
            "- REORDER slides: Swap, move, or rearrange positions\n"
            "- CHANGE slide_type: Convert a slide to a different type\n\n"
            "RULES:\n"
            "1. Output the COMPLETE structure (all slides, not just changed)\n"
            "2. Preserve all unchanged slides exactly as they are\n"
            "3. For new slides, provide full content matching the slide_type\n"
            f"4. slide_type MUST be one of: {slide_types_str}\n"
            "5. New content bullets must be informative (10-20 words) with "
            "specific facts, using **bold lead phrases**\n"
            "6. Maintain image_query fields for slides that need images\n"
            "7. Output ONLY valid JSON. No markdown fences, no explanations.\n"
        )

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()

        raw = self._llm.generate(edit_prompt, max_tokens=3072,
                                 temperature=CONTENT_TEMPERATURE)
        raw = self._strip_markdown_fences(raw)

        _dbg.log_skill_event("file_editor", "interpret_edit_llm_response", {
            "edit_request": edit_request[:200],
            "raw_len": len(raw),
            "raw_preview": raw[:1000],
        })

        modified = self._parse_json_response(raw)
        if not modified or 'slides' not in modified:
            self.logger.error(
                f"[file_editor] Edit interpretation failed: {raw[:200]}")
            return None

        _dbg.log_skill_event("file_editor", "interpret_edit_complete", {
            "original_slide_count": len(current_structure.get('slides', [])),
            "modified_slide_count": len(modified.get('slides', [])),
            "modified_types": [s.get('slide_type') for s in modified.get('slides', [])],
        })

        return modified

    # ------------------------------------------------------------------
    # Intent: create_document
    # ------------------------------------------------------------------

    def create_document(self, entities: dict) -> str:
        """Create a DOCX document or PDF via multi-step pipeline."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] create_document request: {user_text[:80]}")

        # Step 1: Parse the request
        params = self._parse_document_request(user_text)
        if not params:
            return f"I couldn't understand that document request, {self.honorific}. Could you rephrase it?"

        # Determine format
        text_lower = user_text.lower()
        if params.get("doc_type") == "presentation":
            # User said "presentation" but routed here — redirect
            params["doc_type"] = "presentation"
            if not params.get("filename"):
                params["filename"] = "presentation.pptx"
            if not params["filename"].endswith(".pptx"):
                params["filename"] = Path(params["filename"]).stem + ".pptx"
        elif "pdf" in text_lower or (params.get("filename", "").endswith(".pdf")):
            params["doc_type"] = "pdf"
            if not params.get("filename"):
                params["filename"] = "document.pdf"
        else:
            params["doc_type"] = "document"
            if not params.get("filename"):
                params["filename"] = "document.docx"
            if not params["filename"].endswith(".docx"):
                params["filename"] = Path(params["filename"]).stem + ".docx"

        return self._generate_document(params)

    # ------------------------------------------------------------------
    # Document generation pipeline (shared by both intents)
    # ------------------------------------------------------------------

    def _parse_document_request(self, user_text: str) -> Optional[dict]:
        """Parse a document creation request into structured parameters via LLM."""
        parse_prompt = (
            'Analyze this document creation request.\n\n'
            f'REQUEST: "{user_text}"\n\n'
            'Output EXACTLY this format (one field per line, nothing else):\n'
            'DOC_TYPE: presentation|document\n'
            'TOPIC: <main topic — what to research/write about>\n'
            'RESEARCH_NEEDED: yes|no\n'
            'SLIDE_COUNT: <number, default 7 for presentations, 5 for documents>\n'
            'FILENAME: <filename with extension, invent if not specified>\n'
            'ANALYSIS_TYPE: overview|comparison|deep-dive|tutorial|summary\n'
            'KEY_POINTS: <comma-separated areas to cover, or "auto" to let AI decide>\n'
            'THEME: professional|modern|bold|minimal|elegant|earth|forest|ocean|jarvis|banfield '
            '(default professional. modern=clean/minimalist, bold=impactful, minimal=crisp, '
            'elegant=luxury/gold, earth=warm/natural, forest=nature/green, ocean=education/blue, '
            'jarvis=tech/cyan, banfield=warm/orange)'
        )

        result = self._llm.generate(parse_prompt, max_tokens=384)
        self.logger.debug(f"[file_editor] parse result: {result}")

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_skill_event("file_editor", "parse_document_request", {
            "raw_parse_result": result[:500],
            "user_text_preview": user_text[:200],
        })

        params = {}
        for line in result.strip().splitlines():
            line = line.strip()
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower().replace(' ', '_')
            value = value.strip()

            if key == 'doc_type':
                params['doc_type'] = value.lower()
            elif key == 'topic':
                params['topic'] = value
            elif key == 'research_needed':
                params['research_needed'] = value.lower() in ('yes', 'true', '1')
            elif key == 'slide_count':
                try:
                    params['slide_count'] = max(3, min(15, int(value)))
                except ValueError:
                    params['slide_count'] = 7
            elif key == 'filename':
                params['filename'] = Path(value).name if value else None
            elif key == 'analysis_type':
                params['analysis_type'] = value.lower()
            elif key == 'key_points':
                params['key_points'] = value
            elif key == 'theme':
                valid_themes = ('professional', 'modern', 'bold', 'minimal',
                                'elegant', 'earth', 'forest', 'ocean',
                                'jarvis', 'banfield')
                theme_val = value.lower().strip('.,;:!? ')
                if theme_val in valid_themes:
                    params['theme'] = theme_val

        # Require at least a topic
        if not params.get('topic'):
            return None

        # Defaults
        params.setdefault('doc_type', 'presentation')
        params.setdefault('research_needed', True)
        params.setdefault('slide_count', 7)
        params.setdefault('analysis_type', 'overview')
        params.setdefault('key_points', 'auto')
        params.setdefault('theme', 'professional')

        _dbg.log_skill_event("file_editor", "parsed_document_params", {
            "theme": params.get('theme'),
            "slide_count": params.get('slide_count'),
            "doc_type": params.get('doc_type'),
            "topic_preview": (params.get('topic') or '')[:100],
            "filename": params.get('filename'),
        })

        return params

    def _generate_document(self, params: dict) -> str:
        """Execute the full document generation pipeline.

        Steps: research → structure → images → assemble → save
        """
        topic = params['topic']
        doc_type = params.get('doc_type', 'presentation')
        slide_count = params.get('slide_count', 7)
        filename = params.get('filename', 'output.pptx')
        analysis_type = params.get('analysis_type', 'overview')
        key_points = params.get('key_points', 'auto')
        theme_name = params.get('theme', 'professional')

        self.logger.info(f"[file_editor] generating {doc_type}: topic={topic!r}, "
                         f"slides={slide_count}, file={filename}")

        # Step 2: Web research (if needed)
        research_context = ""
        if params.get('research_needed', False):
            research_context = self._do_research(topic, key_points)

        # Step 3: Generate structure via LLM
        structure = self._generate_structure(
            topic, slide_count, analysis_type, key_points, research_context,
            doc_type=doc_type,
        )
        if not structure:
            return (f"I had trouble generating the document structure, {self.honorific}. "
                    "Could you try rephrasing your request?")

        # Step 4: Search for images (Pexels)
        images = {}
        temp_dir = None
        if self._image_search.available:
            temp_dir = tempfile.mkdtemp(prefix="jarvis_doc_")
            images = self._fetch_images(structure, temp_dir)

        # Step 5: Assemble document
        try:
            if doc_type == 'presentation':
                output_path = self._doc_generator.create_presentation(
                    structure, filename=filename, images=images,
                    theme_name=theme_name,
                )
            elif doc_type == 'pdf':
                # Generate DOCX first, then convert
                docx_name = Path(filename).stem + ".docx"
                docx_path = self._doc_generator.create_document(
                    structure, filename=docx_name, images=images
                )
                if docx_path:
                    output_path = self._doc_generator.convert_to_pdf(docx_path)
                    if output_path:
                        # Clean up intermediate DOCX
                        docx_path.unlink(missing_ok=True)
                    else:
                        # PDF conversion failed — keep the DOCX
                        output_path = docx_path
                        self.logger.warning("[file_editor] PDF conversion failed, keeping DOCX")
                else:
                    output_path = None
            else:
                output_path = self._doc_generator.create_document(
                    structure, filename=filename, images=images
                )
        finally:
            # Clean up temp images
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        if not output_path:
            return (f"I encountered an error creating the document, {self.honorific}. "
                    "Please try again.")

        # Remember for follow-up "open it" commands
        self._last_generated_file = output_path

        # Cache pipeline state for editing follow-ups
        if doc_type == 'presentation':
            self._pipeline_cache = _PipelineCache(
                structure=structure,
                research_context=research_context,
                params=params,
                filename=filename,
                output_path=str(output_path),
                images=images,
            )
            self.logger.info(
                f"[file_editor] Pipeline cache stored: {filename} "
                f"({len(structure.get('slides', []))} slides)")
            from core.debug_logger import get_debug_logger
            get_debug_logger().log_skill_event("file_editor", "pipeline_cache_stored", {
                "filename": filename,
                "slide_count": len(structure.get('slides', [])),
                "slide_types": [s.get('slide_type') for s in structure.get('slides', [])],
                "has_research": bool(research_context),
                "image_count": len(images),
            })

        slide_count_actual = len(structure.get('slides', []))
        img_count = len(images)
        img_note = f" with {img_count} images" if img_count > 0 else ""

        if doc_type == 'presentation':
            return (f"Done, {self.honorific}. I've created {output_path.name} — "
                    f"{slide_count_actual} slides{img_note} in the share folder.")
        elif doc_type == 'pdf':
            if output_path.suffix == '.pdf':
                return (f"Done, {self.honorific}. I've created {output_path.name} — "
                        f"{slide_count_actual} sections{img_note} in the share folder.")
            else:
                return (f"I couldn't convert to PDF, {self.honorific}, but I've saved it as "
                        f"{output_path.name} — {slide_count_actual} sections{img_note} "
                        "in the share folder.")
        else:
            return (f"Done, {self.honorific}. I've created {output_path.name} — "
                    f"{slide_count_actual} sections{img_note} in the share folder.")

    def _do_research(self, topic: str, key_points: str = "auto") -> str:
        """Perform web research on the topic and return formatted context."""
        self.logger.info(f"[file_editor] researching: {topic}")

        # Build search query
        search_query = topic
        if key_points and key_points != "auto":
            search_query += f" {key_points.split(',')[0].strip()}"

        results = self._web_researcher.search(search_query, max_results=5)
        if not results:
            self.logger.warning(f"[file_editor] no search results for: {search_query}")
            return ""

        pages = self._web_researcher.fetch_pages_parallel(
            results, max_results=3, max_chars=3000, timeout=5.0
        )

        if not pages:
            # Use snippets as fallback
            snippets = []
            for r in results[:5]:
                title = r.get('title', '')
                snippet = r.get('snippet', '')
                if snippet:
                    snippets.append(f"[{title}]: {snippet}")
            return "\n\n".join(snippets) if snippets else ""

        return "\n\n".join(pages)

    def _generate_structure(self, topic: str, slide_count: int,
                            analysis_type: str, key_points: str,
                            research_context: str,
                            doc_type: str = "presentation") -> Optional[dict]:
        """Generate document structure via two-phase LLM pipeline.

        Phase 1: Layout selection at HIGH temperature (creative variety)
        Phase 2: Content synthesis at LOW temperature (accuracy)

        For non-presentation doc_type, falls back to single-phase generation.
        """
        if doc_type != "presentation":
            return self._generate_structure_single(
                topic, slide_count, analysis_type, key_points,
                research_context, doc_type)

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()
        _dbg.log_skill_event("file_editor", "two_phase_start", {
            "topic": topic, "slide_count": slide_count,
            "analysis_type": analysis_type,
            "has_research": bool(research_context),
        })

        # Phase 1: Creative layout selection
        layout = self._generate_layout(topic, slide_count, analysis_type,
                                       key_points)
        if not layout:
            self.logger.warning("[file_editor] Phase 1 failed, retrying at lower temp")
            _dbg.log_skill_event("file_editor", "phase1_retry", {"reason": "parse_fail"})
            layout = self._generate_layout(topic, slide_count, analysis_type,
                                           key_points, temperature=1.0)
        if not layout:
            self.logger.error("[file_editor] Phase 1 layout generation failed")
            _dbg.log_skill_event("file_editor", "phase1_failed")
            return None

        type_names = [s.get('slide_type', '?') for s in layout]
        self.logger.info(f"[file_editor] Phase 1 layout: {type_names}")
        _dbg.log_skill_event("file_editor", "phase1_complete", {
            "layout": layout, "type_names": type_names,
        })

        # Phase 2: Content synthesis with locked layout
        structure = self._generate_content(
            topic, layout, analysis_type, key_points,
            research_context)
        if not structure or 'slides' not in structure:
            self.logger.error("[file_editor] Phase 2 content generation failed")
            _dbg.log_skill_event("file_editor", "phase2_failed")
            return None

        _dbg.log_skill_event("file_editor", "phase2_complete", {
            "slide_count": len(structure.get('slides', [])),
            "slide_types": [s.get('slide_type') for s in structure.get('slides', [])],
            "structure_keys": list(structure.keys()),
        })

        return structure

    def _generate_layout(self, topic: str, slide_count: int,
                         analysis_type: str, key_points: str,
                         temperature: float = LAYOUT_TEMPERATURE) -> Optional[list]:
        """Phase 1: Generate slide layout plan at HIGH temperature.

        Returns a list of {slide_type, topic_hint} dicts — small schema,
        robust at high temperature. No research data injected.
        """
        key_points_block = ""
        if key_points and key_points != "auto":
            key_points_block = f"\nKEY AREAS TO COVER: {key_points}\n"

        slide_types_str = ", ".join(VALID_SLIDE_TYPES)

        layout_prompt = (
            f'Create a slide layout plan for a {slide_count}-slide '
            f'{analysis_type} about "{topic}".\n'
            f'{key_points_block}\n'
            'Output valid JSON only — a JSON array of objects, one per slide:\n'
            '[\n'
            '  {"slide_type": "title_hero", "topic_hint": "Main title and intro"},\n'
            '  {"slide_type": "bullets", "topic_hint": "Overview of key trends"},\n'
            '  {"slide_type": "card_grid_3", "topic_hint": "Three main categories"},\n'
            '  {"slide_type": "timeline", "topic_hint": "Evolution over time"},\n'
            '  {"slide_type": "stat", "topic_hint": "Key statistic"},\n'
            '  {"slide_type": "closing", "topic_hint": "Summary and takeaways"}\n'
            ']\n\n'
            'RULES:\n'
            f'1. slide_type MUST be one of: {slide_types_str}\n'
            '2. First slide MUST be "title_hero". Last slide MUST be "closing".\n'
            '3. MANDATORY VARIETY: Use at least 3 different slide_type values.\n'
            '4. MUST include at least one of: card_grid_3, card_grid_4, timeline, data_table.\n'
            '5. Maximum 2 consecutive "bullets" slides — break them up with visual types.\n'
            '6. topic_hint is a 5-10 word description of what this slide covers.\n'
            f'7. Generate exactly {slide_count} entries.\n'
            '8. Output ONLY valid JSON array. No markdown fences, no explanations.\n'
        )

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()

        raw = self._llm.generate(layout_prompt, max_tokens=512,
                                 temperature=temperature)
        raw = self._strip_markdown_fences(raw)

        _dbg.log_skill_event("file_editor", "phase1_llm_response", {
            "temperature": temperature,
            "raw_len": len(raw),
            "raw_preview": raw[:500],
        })

        try:
            layout = json.loads(raw.strip())
        except json.JSONDecodeError:
            # Try to extract array from surrounding text
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                try:
                    layout = json.loads(match.group())
                except json.JSONDecodeError:
                    self.logger.error(f"[file_editor] Layout JSON parse failed: {raw[:200]}")
                    return None
            else:
                self.logger.error(f"[file_editor] No JSON array in layout response: {raw[:200]}")
                return None

        if not isinstance(layout, list) or len(layout) == 0:
            return None

        # Validate and sanitize slide types
        for entry in layout:
            if entry.get('slide_type') not in VALID_SLIDE_TYPES:
                entry['slide_type'] = 'bullets'

        return layout

    def _generate_content(self, topic: str, layout: list,
                          analysis_type: str, key_points: str,
                          research_context: str) -> Optional[dict]:
        """Phase 2: Fill locked layout with content at LOW temperature.

        Receives the layout plan from Phase 1 and research data.
        Returns the full structure dict with all content fields populated.
        """
        research_block = ""
        if research_context:
            if len(research_context) > 8000:
                research_context = research_context[:8000] + "\n[...truncated]"
            research_block = (
                f"\nRESEARCH DATA (use this as your primary source):\n"
                f"{research_context}\n"
            )

        key_points_instruction = ""
        if key_points and key_points != "auto":
            key_points_instruction = f"\nKEY AREAS TO COVER: {key_points}\n"

        layout_json = json.dumps(layout, indent=2)

        content_prompt = (
            f'Generate full presentation content for a {analysis_type} about "{topic}".\n'
            f'Today\'s date: March 2026.\n'
            f'{research_block}'
            f'{key_points_instruction}\n'
            f'LOCKED LAYOUT (do NOT change slide_type or order):\n{layout_json}\n\n'
            'Output valid JSON with this structure:\n'
            '{\n'
            '  "title": "Presentation Title",\n'
            '  "subtitle": "Brief subtitle with year/scope",\n'
            '  "slides": [\n'
            '    // For EACH slide in the layout, provide the required fields:\n'
            '    // ALL types need: "title", "slide_type", "notes", "image_query"\n'
            '    // bullets: + "bullets" (array of 4-6 items)\n'
            '    // stat: + "stat_value" (short like "$4.45M"), "stat_label", "bullets"\n'
            '    // comparison: + "left_heading", "left_points", "right_heading", "right_points", "bullets"\n'
            '    // card_grid_3/card_grid_4: + "cards" (array of {heading, description})\n'
            '    // timeline: + "timeline_points" (array of {label, description})\n'
            '    // data_table: + "table_headers" (array), "table_rows" (array of arrays)\n'
            '    // section_divider: + "section_number" ("01"), "subtitle"\n'
            '    // agenda: + "agenda_items" (array of topic strings)\n'
            '    // full_bleed_image: + "overlay_text", "image_query"\n'
            '    // image_text_reversed: + "bullets", "image_query"\n'
            '    // title_hero: + "subtitle"\n'
            '    // closing: + "closing_text", "bullets"\n'
            '  ]\n'
            '}\n\n'
            'RULES:\n'
            '1. First slide is the title/hero. Last slide is the closing.\n'
            '2. Each content slide MUST have 4-6 bullet points (where applicable).\n'
            '3. Each bullet MUST be an informative phrase (10-20 words) '
            'containing at least one specific fact: a number, statistic, '
            'dollar figure, percentage, named entity, or concrete example.\n'
            '   GOOD: "**Ransomware cost:** enterprises lost $20B globally in 2025, up 40% from 2023"\n'
            '   BAD: "Ransomware is increasing"\n'
            '4. Use **bold lead phrases** at the start of bullets: **Key term:** followed by the detail.\n'
            '5. "stat" slides: stat_value is a SHORT string like "$107B" or "47%". '
            'stat_label is one sentence. Add 2-3 supporting bullets.\n'
            '6. "comparison" slides: left/right headings and 2-3 points each. Add summary bullet.\n'
            '7. "card_grid" slides: 3 cards (or 4 for card_grid_4), each with heading + 1-2 sentence description.\n'
            '8. "timeline" slides: 3-6 timeline points, each with label + brief description.\n'
            '9. "data_table" slides: headers array + rows (max 6 cols, 8 rows).\n'
            '10. MUST extract and use specific facts from the RESEARCH DATA. '
            'Do NOT ignore research and write generic content.\n'
            '11. image_query should be a simple 2-4 word search for a relevant stock photo.\n'
            '12. NEVER write vague filler bullets like "X is important" or "Y is growing".\n'
            '13. notes field is optional but encouraged — speaker talking points.\n'
            '14. Output ONLY valid JSON. No markdown fences, no explanations.\n'
        )

        from core.debug_logger import get_debug_logger
        _dbg = get_debug_logger()

        phase2_timeout = max(30, len(layout) * 5)
        scaled_max_tokens = max(4096, len(layout) * 768)
        raw = self._llm.generate(content_prompt, max_tokens=scaled_max_tokens,
                                 temperature=CONTENT_TEMPERATURE,
                                 timeout=phase2_timeout)
        raw = self._strip_markdown_fences(raw)

        _dbg.log_skill_event("file_editor", "phase2_llm_response", {
            "temperature": CONTENT_TEMPERATURE,
            "max_tokens": scaled_max_tokens,
            "raw_len": len(raw),
            "raw_preview": raw[:1000],
        })

        structure = self._parse_json_response(raw)

        if not structure or 'slides' not in structure:
            # Attempt JSON repair for truncated LLM output
            repaired = self._repair_truncated_slides_json(raw)
            if repaired and 'slides' in repaired:
                salvaged = len(repaired['slides'])
                requested = len(layout)
                self.logger.warning(
                    f"[file_editor] JSON truncated — salvaged {salvaged}/{requested} slides"
                )
                _dbg.log_skill_event("file_editor", "phase2_json_repair", {
                    "salvaged": salvaged,
                    "requested": requested,
                    "raw_len": len(raw),
                })
                return repaired
            self.logger.error(f"[file_editor] Content gen failed: {raw[:200]}")
            return None

        return structure

    def _generate_structure_single(self, topic: str, slide_count: int,
                                   analysis_type: str, key_points: str,
                                   research_context: str,
                                   doc_type: str = "document") -> Optional[dict]:
        """Single-phase structure generation for non-presentation doc types."""
        research_block = ""
        if research_context:
            if len(research_context) > 8000:
                research_context = research_context[:8000] + "\n[...truncated]"
            research_block = (
                f"\nRESEARCH DATA (use this as your primary source):\n"
                f"{research_context}\n"
            )

        key_points_instruction = ""
        if key_points and key_points != "auto":
            key_points_instruction = f"\nKEY AREAS TO COVER: {key_points}\n"

        bullet_guidance = (
            '2. Each section MUST have 4-6 bullet points.\n'
            '3. Each bullet MUST be a complete, informative sentence (15-30 words) '
            'containing specific facts: numbers, statistics, dollar figures, '
            'named examples, dates, or concrete evidence.\n'
            '   GOOD: "**Data breach costs:** The average reached $4.45M in 2024, '
            'with healthcare breaches averaging $10.93M according to IBM."\n'
            '   BAD: "Data breaches are becoming more expensive."\n'
            '4. Use **bold lead phrases** at the start of bullets using **double asterisks**.\n'
        )

        structure_prompt = (
            f'Create a structured outline for a {slide_count}-section '
            f'{analysis_type} about "{topic}".\n'
            f'Today\'s date: March 2026.\n'
            f'{research_block}'
            f'{key_points_instruction}\n'
            'Output valid JSON only — no other text, no markdown fences:\n'
            '{\n'
            '  "title": "Document Title",\n'
            '  "subtitle": "Brief subtitle with year/scope",\n'
            '  "slides": [\n'
            '    {\n'
            '      "title": "Section Title",\n'
            '      "slide_type": "bullets",\n'
            '      "bullets": ["**Key fact:** informative point with data"],\n'
            '      "notes": "Additional context"\n'
            '    }\n'
            '  ]\n'
            '}\n\n'
            'RULES:\n'
            '1. First section is title/intro. Last section is conclusion/summary.\n'
            f'{bullet_guidance}'
            '5. slide_type MUST be one of: "bullets", "stat".\n'
            '6. MUST include at least 1 "stat" section with a striking number.\n'
            '7. "stat" sections: stat_value is a SHORT string, stat_label is one sentence.\n'
            '8. MUST extract and use specific facts from the RESEARCH DATA.\n'
            f'9. Generate exactly {slide_count} sections total.\n'
            '10. NEVER write vague filler. Every bullet must teach something specific.\n'
            '11. Output ONLY valid JSON. No markdown fences, no explanations.\n'
        )

        raw = self._llm.generate(structure_prompt, max_tokens=3072)
        raw = self._strip_markdown_fences(raw)

        structure = self._parse_json_response(raw)

        if not structure or 'slides' not in structure:
            self.logger.error(f"[file_editor] failed to parse structure JSON: {raw[:200]}")
            return None

        return structure

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """Extract and parse JSON from LLM response, handling common issues."""
        text = text.strip()

        # Remove markdown fences if present
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the text
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None

    def _repair_truncated_slides_json(self, raw: str) -> Optional[dict]:
        """Attempt to recover slides from truncated JSON output.

        Finds the last complete slide object and closes the JSON structure.
        Returns parsed dict with however many slides survived, or None.
        """
        # Find "slides" array start
        slides_match = re.search(r'"slides"\s*:\s*\[', raw)
        if not slides_match:
            return None

        # Walk backwards from end to find last complete slide object.
        # Each slide ends with '}' possibly followed by ',' before the next.
        # Find all top-level slide objects by matching balanced braces.
        arr_start = slides_match.end()
        depth = 0
        last_complete_end = -1
        i = arr_start

        while i < len(raw):
            ch = raw[i]
            if ch == '"':
                # Skip string contents (handle escaped quotes)
                i += 1
                while i < len(raw) and raw[i] != '"':
                    if raw[i] == '\\':
                        i += 1  # skip escaped char
                    i += 1
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_complete_end = i + 1
            i += 1

        if last_complete_end <= arr_start:
            return None

        # Build repaired JSON: everything up to last complete slide, then close
        repaired = raw[:last_complete_end] + ']}'
        try:
            # Wrap if we started mid-object — find the root '{'
            root_start = raw.find('{')
            if root_start >= 0 and root_start < slides_match.start():
                repaired = raw[root_start:last_complete_end] + ']}'
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None

    def _fetch_images(self, structure: dict, temp_dir: str) -> dict:
        """Fetch images for each slide that has an image_query.

        Returns: {slide_index: image_path} mapping
        """
        images = {}
        slides = structure.get('slides', [])

        for i, slide in enumerate(slides):
            # Skip title slide (first) and conclusion (last)
            if i == 0 or i == len(slides) - 1:
                continue

            query = slide.get('image_query', '')
            if not query:
                continue

            image_path = self._image_search.search_and_download(query, Path(temp_dir))
            if image_path:
                images[i] = str(image_path)
                self.logger.debug(f"[file_editor] image for slide {i}: {image_path.name}")

        return images

    # ------------------------------------------------------------------
    # Intent: open_document (follow-up after doc gen)
    # ------------------------------------------------------------------

    def open_document(self, entities: dict) -> str:
        """Open the last generated document in its default application."""
        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] open_document request: {user_text[:80]}")

        # Check for explicit filename in the request
        filename = self._extract_filename(user_text)
        if filename:
            target = self._safe_path(filename)
            if target and target.exists():
                return self._open_file_with_app(target)

        # Fall back to last generated file
        if self._last_generated_file and self._last_generated_file.exists():
            return self._open_file_with_app(self._last_generated_file)

        return (f"I don't have a recent document to open, {self.honorific}. "
                "Could you specify which file?")

    def _open_file_with_app(self, file_path: Path) -> str:
        """Open a file using xdg-open via the desktop manager."""
        from core.desktop_manager import get_desktop_manager
        desktop = get_desktop_manager()
        if desktop and desktop.open_file(str(file_path)):
            self.logger.info(f"[file_editor] opened {file_path.name}")
            return f"Opening {file_path.name}, {self.honorific}."
        # Fallback: try subprocess directly
        try:
            import subprocess
            subprocess.Popen(
                ["xdg-open", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"Opening {file_path.name}, {self.honorific}."
        except Exception as e:
            self.logger.error(f"[file_editor] failed to open {file_path}: {e}")
            return (f"I couldn't open {file_path.name}, {self.honorific}. "
                    "You can find it in the share folder.")

    # ------------------------------------------------------------------
    # Intent: print_document
    # ------------------------------------------------------------------

    def print_document(self, entities: dict) -> str:
        """Print a document from the share folder."""
        import subprocess

        user_text = entities.get('original_text', '')
        self.logger.info(f"[file_editor] print_document request: {user_text[:80]}")

        # Find the target file
        target = None

        # Check for explicit filename in request
        filename = self._extract_filename(user_text)
        if filename:
            path = self._safe_path(filename)
            if path and path.exists():
                target = path

        # Fall back to last generated file
        if not target and self._last_generated_file and self._last_generated_file.exists():
            target = self._last_generated_file

        if not target:
            return (f"I don't have a document to print, {self.honorific}. "
                    "Could you specify which file?")

        # Detect available printer
        try:
            result = subprocess.run(
                ["lpstat", "-p", "-d"],
                capture_output=True, text=True, timeout=5,
            )
            printer = None
            for line in result.stdout.splitlines():
                if line.startswith("printer ") and "idle" in line:
                    printer = line.split()[1]
                    break
            if not printer:
                # Take the first printer found
                for line in result.stdout.splitlines():
                    if line.startswith("printer "):
                        printer = line.split()[1]
                        break
        except Exception as e:
            self.logger.error(f"[file_editor] printer detection failed: {e}")
            return (f"I couldn't detect a printer, {self.honorific}. "
                    "Please check that your printer is connected.")

        if not printer:
            return (f"No printers found on the system, {self.honorific}. "
                    "Please check your printer connection.")

        # Send to printer
        try:
            result = subprocess.run(
                ["lp", "-d", printer, str(target)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                self.logger.info(f"[file_editor] sent {target.name} to {printer}")
                return (f"Sent {target.name} to the printer, {self.honorific}.")
            else:
                self.logger.error(f"[file_editor] lp failed: {result.stderr}")
                return (f"The print command failed, {self.honorific}. "
                        f"{result.stderr.strip()}")
        except Exception as e:
            self.logger.error(f"[file_editor] print failed: {e}")
            return (f"I couldn't send the file to the printer, {self.honorific}. "
                    f"Error: {e}")

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
