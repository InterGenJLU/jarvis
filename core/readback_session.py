"""
Structured readback session — section-based interactive content delivery.

Replaces fire-and-forget 4096-token streaming with a state machine that
parses content into typed sections (ingredients, steps, notes), delivers
them with natural pauses, and supports mid-readback queries like
"what was step 3?" or "how much flour?".

One LLM call parses raw content into structured JSON.  Everything after
that is cache lookups — no more LLM calls for delivery, recall, or repeat.
"""

import json
import logging
import re

logger = logging.getLogger("jarvis.readback")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class ReadbackChunk:
    """One deliverable unit of readback content."""
    __slots__ = ("section_type", "title", "content", "items", "pause_after")

    def __init__(self, section_type: str, title: str, content: str,
                 items: list[str] | None = None, pause_after: bool = False):
        self.section_type = section_type    # "preamble", "ingredients", "equipment", "instructions", "notes"
        self.title = title                  # "Ingredients", "Steps 1 through 4", etc.
        self.content = content              # TTS-ready text for this chunk
        self.items = items or []            # Individual items (for ingredient/step lookup)
        self.pause_after = pause_after      # Whether to pause after delivering this chunk


class ReadbackSession:
    """Manages interactive readback of structured content.

    State machine: "parsing" → "delivering" → "paused" ⇄ "delivering" → "complete"
    """

    # Instruction batch size — split large step lists into groups
    STEP_BATCH_SIZE = 5
    # Small content threshold: no pauses at all
    SMALL_THRESHOLD_STEPS = 5
    SMALL_THRESHOLD_SECTIONS = 2

    def __init__(self):
        self.state: str = "parsing"         # "parsing" | "delivering" | "paused" | "complete"
        self.source_title: str = ""         # "Tastes Better From Scratch"
        self.source_artifact_id: str = ""   # backing artifact (for importance scoring)
        self.chunks: list[ReadbackChunk] = []
        self.current_idx: int = 0           # Which chunk to deliver next
        self.last_delivered_idx: int = -1   # For "repeat that"
        self.all_ingredients: list[str] = []  # Flat list for search
        self.all_steps: list[dict] = []     # [{step: 1, text: "..."}, ...]

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse_content(self, raw_tool_result: str, prior_pick: str, llm) -> bool:
        """Call LLM to parse raw content into structured JSON.

        Returns True on success, False on parse failure (caller should
        fall back to unstructured _stream_readback).
        """
        prompt = self._build_parse_prompt(raw_tool_result, prior_pick)
        try:
            response = llm.chat(prompt, max_tokens=4096)
        except Exception as e:
            logger.error("Readback parse LLM call failed: %s", e)
            return False

        if not response:
            logger.warning("Readback parse: empty LLM response")
            return False

        # Strip markdown fencing if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # Remove opening ```json or ``` and closing ```
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("Readback parse: JSON decode failed: %s (first 200 chars: %s)",
                           e, cleaned[:200])
            return False

        return self._build_from_json(data)

    def _build_parse_prompt(self, raw_tool_result: str, prior_pick: str) -> str:
        """Build the LLM prompt for structured JSON extraction."""
        pick_clause = ""
        if prior_pick:
            pick_clause = f'Extract content from the source matching "{prior_pick}".\n'

        return (
            f"Here are search results:\n\n{raw_tool_result}\n\n"
            f"{pick_clause}"
            "Extract the content into this JSON format:\n"
            "{\n"
            '  "title": "Recipe Title",\n'
            '  "source": "Website Name",\n'
            '  "preamble": "Brief intro (1-2 sentences, or null)",\n'
            '  "sections": [\n'
            '    {"type": "ingredients", "title": "Ingredients", "items": ["2 1/4 tsp active dry yeast", ...]},\n'
            '    {"type": "equipment", "title": "Equipment Needed", "items": [...]},\n'
            '    {"type": "instructions", "title": "Instructions", "steps": [\n'
            '      {"step": 1, "text": "Dissolve yeast in warm water..."},\n'
            '      {"step": 2, "text": "Add flour and salt..."}\n'
            "    ]},\n"
            '    {"type": "notes", "title": "Tips", "text": "For best results..."}\n'
            "  ]\n"
            "}\n"
            "RULES:\n"
            "1. Extract from SAME source as the prior pick ONLY.\n"
            "2. Include ALL items and steps — DO NOT skip or summarize.\n"
            "3. For list sections (ingredients, equipment): use \"items\" array.\n"
            "4. For step sections (instructions): use \"steps\" array with step numbers.\n"
            "5. For narrative (preamble, notes): use \"text\" string.\n"
            "6. Return ONLY valid JSON — no markdown fencing, no explanation."
        )

    def _build_from_json(self, data: dict) -> bool:
        """Build chunks from parsed JSON data.  Returns False on inadequate data."""
        self.source_title = data.get("source") or data.get("title") or "the source"

        sections = data.get("sections")
        if not sections or not isinstance(sections, list):
            logger.warning("Readback parse: no sections in JSON")
            return False

        # Build preamble chunk
        preamble = data.get("preamble")
        if preamble:
            self.chunks.append(ReadbackChunk(
                section_type="preamble",
                title="Introduction",
                content=preamble,
            ))

        # Build section chunks
        for section in sections:
            s_type = section.get("type", "")
            s_title = section.get("title", s_type.title())

            if s_type in ("ingredients", "equipment"):
                items = section.get("items", [])
                if not items:
                    continue
                content = f"{s_title}:\n" + "\n".join(f"- {item}" for item in items)
                self.chunks.append(ReadbackChunk(
                    section_type=s_type,
                    title=s_title,
                    content=content,
                    items=items,
                ))
                if s_type == "ingredients":
                    self.all_ingredients.extend(items)

            elif s_type == "instructions":
                steps = section.get("steps", [])
                if not steps:
                    continue
                # Collect all steps for lookup
                for s in steps:
                    self.all_steps.append({
                        "step": s.get("step", len(self.all_steps) + 1),
                        "text": s.get("text", ""),
                    })
                # Batch large instruction sets
                self._add_instruction_chunks(steps)

            elif s_type == "notes":
                text = section.get("text", "")
                items = section.get("items", [])
                if items:
                    text = f"{s_title}:\n" + "\n".join(f"- {item}" for item in items)
                elif text:
                    text = f"{s_title}: {text}"
                if text:
                    self.chunks.append(ReadbackChunk(
                        section_type="notes",
                        title=s_title,
                        content=text,
                        items=items,
                    ))

            else:
                # Generic section — treat as narrative
                text = section.get("text", "")
                items = section.get("items", [])
                if items:
                    text = f"{s_title}:\n" + "\n".join(f"- {item}" for item in items)
                elif text:
                    text = f"{s_title}: {text}"
                if text:
                    self.chunks.append(ReadbackChunk(
                        section_type=s_type or "other",
                        title=s_title,
                        content=text,
                        items=items,
                    ))

        # Need at least 1 section with items or steps to justify structured readback
        has_substance = any(
            c.section_type in ("ingredients", "equipment", "instructions")
            for c in self.chunks
        )
        if not has_substance and len(self.chunks) <= 1:
            logger.warning("Readback parse: insufficient structure (1 section, no items/steps)")
            return False

        self._calculate_pauses()
        self.state = "delivering"
        logger.info("Readback session created: %d chunks, %d ingredients, %d steps from '%s'",
                     len(self.chunks), len(self.all_ingredients),
                     len(self.all_steps), self.source_title)
        return True

    def _add_instruction_chunks(self, steps: list[dict]):
        """Split instruction steps into batches and add as chunks."""
        if len(steps) <= self.STEP_BATCH_SIZE:
            # Single batch
            content = "\n".join(
                f"Step {s.get('step', i+1)}: {s.get('text', '')}"
                for i, s in enumerate(steps)
            )
            step_nums = [s.get("step", i+1) for i, s in enumerate(steps)]
            title = f"Steps {step_nums[0]} through {step_nums[-1]}" if len(steps) > 1 else f"Step {step_nums[0]}"
            self.chunks.append(ReadbackChunk(
                section_type="instructions",
                title=title,
                content=content,
                items=[s.get("text", "") for s in steps],
            ))
        else:
            # Multiple batches
            for batch_start in range(0, len(steps), self.STEP_BATCH_SIZE):
                batch = steps[batch_start:batch_start + self.STEP_BATCH_SIZE]
                content = "\n".join(
                    f"Step {s.get('step', batch_start+i+1)}: {s.get('text', '')}"
                    for i, s in enumerate(batch)
                )
                step_nums = [s.get("step", batch_start+i+1) for i, s in enumerate(batch)]
                title = f"Steps {step_nums[0]} through {step_nums[-1]}" if len(batch) > 1 else f"Step {step_nums[0]}"
                self.chunks.append(ReadbackChunk(
                    section_type="instructions",
                    title=title,
                    content=content,
                    items=[s.get("text", "") for s in batch],
                ))

    def _calculate_pauses(self):
        """Set pause_after on each chunk based on content size and structure."""
        total_steps = len(self.all_steps)
        # Count non-preamble/non-notes sections
        content_sections = [c for c in self.chunks
                            if c.section_type not in ("preamble", "notes")]

        # Small content: no pauses at all
        if (total_steps <= self.SMALL_THRESHOLD_STEPS
                and len(content_sections) <= self.SMALL_THRESHOLD_SECTIONS):
            for chunk in self.chunks:
                chunk.pause_after = False
            return

        # Medium/large: calculated pauses
        for i, chunk in enumerate(self.chunks):
            if chunk.section_type == "preamble":
                chunk.pause_after = False  # flows into next

            elif chunk.section_type in ("ingredients", "equipment"):
                # Pause if instructions follow
                has_instructions_after = any(
                    c.section_type == "instructions" for c in self.chunks[i+1:]
                )
                chunk.pause_after = has_instructions_after

            elif chunk.section_type == "instructions":
                # Pause between instruction batches (but not the last one)
                is_last_chunk = (i >= len(self.chunks) - 1)
                next_is_instructions = (
                    not is_last_chunk
                    and self.chunks[i+1].section_type == "instructions"
                )
                chunk.pause_after = next_is_instructions

            elif chunk.section_type == "notes":
                chunk.pause_after = False  # end of content

            else:
                chunk.pause_after = False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def next_chunk(self) -> ReadbackChunk | None:
        """Return next chunk and advance. None when complete."""
        if self.current_idx >= len(self.chunks):
            self.state = "complete"
            return None

        chunk = self.chunks[self.current_idx]
        self.last_delivered_idx = self.current_idx
        self.current_idx += 1

        if chunk.pause_after:
            self.state = "paused"
        elif self.current_idx >= len(self.chunks):
            self.state = "complete"
        else:
            self.state = "delivering"

        return chunk

    def get_step(self, n: int) -> str | None:
        """Look up a specific step by number."""
        for step in self.all_steps:
            if step["step"] == n:
                return f"Step {n}: {step['text']}"
        return None

    def search_ingredients(self, query: str) -> str | None:
        """Case-insensitive substring search across all ingredients."""
        query_lower = query.lower()
        for item in self.all_ingredients:
            if query_lower in item.lower():
                return f"The recipe calls for {item}."
        return None

    def get_section(self, name: str) -> ReadbackChunk | None:
        """Find the first chunk matching a section type."""
        for chunk in self.chunks:
            if chunk.section_type == name:
                return chunk
        return None

    def get_last_delivered(self) -> ReadbackChunk | None:
        """Return the most recently delivered chunk (for 'repeat that')."""
        if 0 <= self.last_delivered_idx < len(self.chunks):
            return self.chunks[self.last_delivered_idx]
        return None

    def get_summary(self) -> str:
        """End-of-readback summary."""
        parts = []
        if self.all_ingredients:
            parts.append(f"{len(self.all_ingredients)} ingredients")
        if self.all_steps:
            parts.append(f"{len(self.all_steps)} steps")
        detail = ", ".join(parts) if parts else "the content"
        return f"That's everything from {self.source_title} — {detail}."

    def get_size(self) -> str:
        """Classify content size: 'small', 'medium', or 'large'."""
        total_steps = len(self.all_steps)
        content_sections = [c for c in self.chunks
                            if c.section_type not in ("preamble", "notes")]
        if (total_steps <= self.SMALL_THRESHOLD_STEPS
                and len(content_sections) <= self.SMALL_THRESHOLD_SECTIONS):
            return "small"
        if total_steps <= 10:
            return "medium"
        return "large"

    def is_active(self) -> bool:
        """True if session is in delivering or paused state."""
        return self.state in ("delivering", "paused")

    def end(self):
        """End the session."""
        self.state = "complete"
