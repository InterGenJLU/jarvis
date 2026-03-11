"""
Document Generator Module

Generates PPTX presentations, DOCX documents, and PDF files
from structured outline data. Used by the file_editor skill
for voice-driven document creation.

Supports 14 slide types (bullets, stat, comparison, image+text, title/hero,
section divider, agenda, card grids, timeline, data table, full-bleed image,
reversed image+text, closing), bold text markup, speaker notes, slide numbers,
and theme selection.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor

from docx import Document
from docx.shared import Pt as DocxPt, Inches as DocxInches, RGBColor as DocxRGB
from docx.enum.text import WD_ALIGN_PARAGRAPH

from core.logger import get_logger
from core.debug_logger import get_debug_logger


SHARE_DIR = Path(os.path.expanduser("~/jarvis/share"))

# Font standardization — universal system-safe fonts
HEADING_FONT = "Calibri"
BODY_FONT = "Calibri"

# Color themes — all light-background
THEMES = {
    "professional": {
        "heading": RGBColor(0x1A, 0x1A, 0x2E),    # Navy
        "subtitle": RGBColor(0x55, 0x55, 0x77),    # Purple-gray
        "body": RGBColor(0x33, 0x33, 0x33),         # Dark gray
        "accent": RGBColor(0x2B, 0x57, 0x9A),       # Blue
    },
    "modern": {
        "heading": RGBColor(0x2D, 0x2D, 0x2D),     # Charcoal
        "subtitle": RGBColor(0x66, 0x66, 0x66),     # Medium gray
        "body": RGBColor(0x3A, 0x3A, 0x3A),         # Dark gray
        "accent": RGBColor(0x00, 0x96, 0x88),        # Teal
    },
    "bold": {
        "heading": RGBColor(0x1A, 0x23, 0x3B),     # Dark navy
        "subtitle": RGBColor(0x55, 0x55, 0x66),     # Muted gray
        "body": RGBColor(0x33, 0x33, 0x33),         # Dark gray
        "accent": RGBColor(0xE8, 0x6C, 0x00),       # Warm orange
    },
}

_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


def _parse_bold_text(text: str) -> list:
    """Parse **bold** markdown into [(text, is_bold), ...] segments.

    Example: "**Key fact:** the detail" -> [("Key fact:", True), (" the detail", False)]
    Returns [(text, False)] if no ** markers found.
    """
    segments = []
    last_end = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > last_end:
            segments.append((text[last_end:match.start()], False))
        segments.append((match.group(1), True))
        last_end = match.end()
    if last_end < len(text):
        segments.append((text[last_end:], False))
    if not segments:
        segments.append((text, False))
    return segments


def _add_formatted_runs(paragraph, text, font_size, font_color,
                        accent_color=None, heading=False):
    """Add text with bold/normal runs parsed from **markdown** to a paragraph.

    Bold segments render in accent_color (if provided) for visual emphasis.
    Set heading=True to use HEADING_FONT instead of BODY_FONT.
    """
    segments = _parse_bold_text(text)
    for seg_text, is_bold in segments:
        run = paragraph.add_run()
        run.text = seg_text
        run.font.name = HEADING_FONT if heading else BODY_FONT
        run.font.size = font_size
        run.font.bold = is_bold
        if is_bold and accent_color:
            run.font.color.rgb = accent_color
        else:
            run.font.color.rgb = font_color


class DocumentGenerator:
    """Generates PPTX, DOCX, and PDF documents from structured outlines."""

    # Dispatch table for new slide types (uniform signature)
    _SLIDE_DISPATCHERS = {
        "title_hero": "_add_title_hero_slide",
        "section_divider": "_add_section_divider_slide",
        "agenda": "_add_agenda_slide",
        "card_grid_3": "_add_card_grid_slide",
        "card_grid_4": "_add_card_grid_slide",
        "timeline": "_add_timeline_slide",
        "data_table": "_add_data_table_slide",
        "full_bleed_image": "_add_full_bleed_image_slide",
        "image_text_reversed": "_add_image_text_reversed_slide",
        "closing": "_add_closing_slide",
    }

    def __init__(self, config=None):
        self.logger = get_logger(__name__, config)
        SHARE_DIR.mkdir(parents=True, exist_ok=True)

    def _add_accent_bar(self, slide, prs, theme, y_pos=None):
        """Add a thin accent-colored bar across the slide under the title area."""
        bar_height = Inches(0.06)
        y = y_pos or Inches(1.55)
        shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0.5), y,
            Emu(int(prs.slide_width - Inches(1))), bar_height)
        shape.fill.solid()
        shape.fill.fore_color.rgb = theme["accent"]
        shape.line.fill.background()  # No border

    # ------------------------------------------------------------------
    # PPTX Generation
    # ------------------------------------------------------------------

    def create_presentation(self, structure: dict, filename: str = "presentation.pptx",
                            images: dict = None,
                            theme_name: str = "professional") -> Optional[Path]:
        """Generate a PPTX presentation from a structured outline.

        Args:
            structure: Dict with keys: title, subtitle, slides[]
                       Each slide has: title, bullets[], slide_type, notes, image_query
            filename: Output filename (saved to share/)
            images: Optional {slide_index: image_path} mapping for embedded images
            theme_name: Theme preset name (professional, modern, bold)

        Returns:
            Path to saved .pptx file, or None on failure
        """
        try:
            _dbg = get_debug_logger()
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)

            theme = THEMES.get(theme_name, THEMES["professional"])
            slides_data = structure.get("slides", [])
            total_slides = len(slides_data)

            _dbg.log_skill_event("doc_gen", "create_presentation_entry", {
                "filename": filename,
                "theme_name": theme_name,
                "total_slides": total_slides,
                "slide_types": [s.get("slide_type", "bullets") for s in slides_data],
                "image_indices": list(images.keys()) if images else [],
                "structure_keys": list(structure.keys()),
            })

            for i, slide_data in enumerate(slides_data):
                slide_title = slide_data.get("title", f"Slide {i + 1}")
                bullets = slide_data.get("bullets", [])
                slide_type = slide_data.get("slide_type", "bullets")
                image_path = images.get(i) if images else None
                notes = slide_data.get("notes", "")

                # First slide: title/hero or legacy title
                if i == 0 and slide_type in ("title_hero", "bullets"):
                    dispatch_method = "_add_title_slide"
                    dispatch_reason = f"first_slide_override (type={slide_type} matched title_hero|bullets)"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "has_image": bool(image_path),
                        "bullet_count": len(bullets),
                        "data_keys": list(slide_data.keys()),
                    })
                    slide = self._add_title_slide(
                        prs, structure.get("title", slide_title),
                        structure.get("subtitle", ""), theme)
                # New slide types with uniform signature
                elif slide_type in self._SLIDE_DISPATCHERS:
                    dispatch_method = self._SLIDE_DISPATCHERS[slide_type]
                    dispatch_reason = "dispatch_table"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "has_image": bool(image_path),
                        "bullet_count": len(bullets),
                        "data_keys": list(slide_data.keys()),
                    })
                    method = getattr(self, dispatch_method)
                    slide = method(prs, slide_title, slide_data, theme,
                                   image_path=image_path)
                # Legacy: stat with validation
                elif slide_type == "stat" and slide_data.get("stat_value"):
                    dispatch_method = "_add_stat_slide"
                    dispatch_reason = "legacy_stat (has stat_value)"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "has_image": bool(image_path),
                        "stat_value": slide_data.get("stat_value"),
                        "data_keys": list(slide_data.keys()),
                    })
                    slide = self._add_stat_slide(prs, slide_title, slide_data, theme)
                # Legacy: comparison with validation
                elif slide_type == "comparison" and slide_data.get("left_heading"):
                    dispatch_method = "_add_comparison_slide"
                    dispatch_reason = "legacy_comparison (has left_heading)"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "has_image": bool(image_path),
                        "data_keys": list(slide_data.keys()),
                    })
                    slide = self._add_comparison_slide(prs, slide_title, slide_data, theme)
                # Image slide (any type with available image)
                elif image_path and Path(image_path).exists():
                    dispatch_method = "_add_image_slide"
                    dispatch_reason = f"image_fallback (type={slide_type} not in dispatchers, image exists)"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "image_path": str(image_path),
                        "bullet_count": len(bullets),
                        "data_keys": list(slide_data.keys()),
                    })
                    slide = self._add_image_slide(
                        prs, slide_title, bullets, image_path, theme)
                # Default: bullet content
                else:
                    dispatch_method = "_add_content_slide"
                    dispatch_reason = f"default_fallback (type={slide_type} unmatched, no image)"
                    _dbg.log_skill_event("doc_gen", "slide_dispatch", {
                        "slide_index": i,
                        "slide_type": slide_type,
                        "slide_title": slide_title,
                        "dispatch_method": dispatch_method,
                        "dispatch_reason": dispatch_reason,
                        "has_image": bool(image_path),
                        "bullet_count": len(bullets),
                        "data_keys": list(slide_data.keys()),
                    })
                    slide = self._add_content_slide(prs, slide_title, bullets, theme)

                # Speaker notes
                if notes:
                    notes_slide = slide.notes_slide
                    notes_slide.notes_text_frame.text = notes

                # Slide number (skip title slide)
                if i > 0:
                    self._add_slide_number(slide, i + 1, total_slides, prs)

            # Save
            output_path = SHARE_DIR / filename
            prs.save(str(output_path))
            _dbg.log_skill_event("doc_gen", "presentation_saved", {
                "filename": filename,
                "output_path": str(output_path),
                "total_slides": total_slides,
            })
            self.logger.info(f"[doc_gen] Created PPTX: {output_path} ({total_slides} slides)")
            return output_path

        except Exception as e:
            self.logger.error(f"[doc_gen] PPTX creation failed: {e}")
            return None

    def _add_title_slide(self, prs, title, subtitle, theme):
        """Add a title slide with accent strip and visual hierarchy."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_title_slide", {
            "method": "_add_title_slide (LEGACY)",
            "title": title,
            "subtitle": subtitle,
            "title_font_name": "NONE (missing font.name)",
            "subtitle_font_name": "NONE (missing font.name)",
            "title_font_size": 40,
            "subtitle_font_size": 20,
        })
        slide_layout = prs.slide_layouts[5]  # Title Only — full manual control
        slide = prs.slides.add_slide(slide_layout)

        # Clear the layout placeholder so it doesn't show default text
        # ("Click to add title") behind our custom textboxes
        if slide.placeholders:
            for ph in list(slide.placeholders):
                sp = ph._element
                sp.getparent().remove(sp)

        # Accent strip — bold bar across the bottom ~25%
        strip_height = Inches(2.0)
        strip_y = Emu(int(prs.slide_height - strip_height))
        strip = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), strip_y,
            prs.slide_width, strip_height)
        strip.fill.solid()
        strip.fill.fore_color.rgb = theme["accent"]
        strip.line.fill.background()

        # Title text — large, centered in upper area
        title_box = slide.shapes.add_textbox(
            Inches(1), Inches(1.8),
            Emu(int(prs.slide_width - Inches(2))), Inches(2.5))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = title
        run.font.size = Pt(40)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        # Subtitle — inside the accent strip, white text
        if subtitle:
            sub_box = slide.shapes.add_textbox(
                Inches(1), Emu(int(strip_y + Inches(0.4))),
                Emu(int(prs.slide_width - Inches(2))), Inches(1.2))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            sp.alignment = PP_ALIGN.CENTER
            run_s = sp.add_run()
            run_s.text = subtitle
            run_s.font.size = Pt(20)
            run_s.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

        return slide

    def _add_content_slide(self, prs, title, bullets, theme):
        """Add a content slide with title and bullet points (bold markup supported)."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_content_slide", {
            "method": "_add_content_slide",
            "title": title,
            "bullet_count": len(bullets),
            "layout_index": 1,
            "title_font_name": "NONE (uses placeholder default)",
            "body_font": "via _add_formatted_runs (BODY_FONT)",
        })
        slide_layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(slide_layout)

        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(28)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        if len(slide.placeholders) > 1:
            body_shape = slide.placeholders[1]
            tf = body_shape.text_frame
            tf.clear()

            for j, bullet in enumerate(bullets):
                p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                _add_formatted_runs(p, bullet, Pt(18), theme["body"], theme["accent"])
                p.space_after = Pt(8)
                p.level = 0

        return slide

    def _add_image_slide(self, prs, title, bullets, image_path, theme):
        """Add a content slide with text on left and image on right."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_image_slide", {
            "method": "_add_image_slide",
            "title": title,
            "bullet_count": len(bullets),
            "image_path": str(image_path),
            "image_exists": Path(image_path).exists() if image_path else False,
            "title_font_name": "NONE (uses placeholder default)",
            "body_font": "via _add_formatted_runs (BODY_FONT)",
        })
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(28)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        slide_width = prs.slide_width

        # Text box — left 55%
        txBox = slide.shapes.add_textbox(
            Inches(0.5), Inches(1.8),
            Emu(int(slide_width * 0.52)), Inches(4.5))
        tf = txBox.text_frame
        tf.word_wrap = True

        for j, bullet in enumerate(bullets):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            _add_formatted_runs(p, f"\u2022 {bullet}", Pt(16), theme["body"], theme["accent"])
            p.space_after = Pt(6)

        # Image — right 40%
        img_left = Emu(int(slide_width * 0.57))
        img_width = Emu(int(slide_width * 0.38))
        img_max_height = Inches(4.5)

        try:
            pic = slide.shapes.add_picture(
                str(image_path), img_left, Inches(1.8), width=img_width)
            if pic.height > img_max_height:
                ratio = img_max_height / pic.height
                pic.height = img_max_height
                pic.width = int(pic.width * ratio)
        except Exception as e:
            self.logger.warning(f"[doc_gen] Failed to add image to slide: {e}")

        return slide

    def _add_stat_slide(self, prs, title, slide_data, theme):
        """Add a stat callout slide — large centered number with label and context."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_stat_slide", {
            "method": "_add_stat_slide (LEGACY)",
            "title": title,
            "stat_value": slide_data.get("stat_value"),
            "stat_label": slide_data.get("stat_label"),
            "bullet_count": len(slide_data.get("bullets", [])),
            "title_font_name": "NONE (uses placeholder default)",
            "stat_font_name": "NONE (missing font.name)",
            "label_font_name": "NONE (missing font.name)",
        })
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        # Title
        title_shape = slide.placeholders[0]
        title_shape.text = title
        for p in title_shape.text_frame.paragraphs:
            p.font.size = Pt(28)
            p.font.bold = True
            p.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        slide_width = prs.slide_width
        content_width = Emu(int(slide_width - Inches(2)))

        # Big stat number — centered, accent color
        stat_value = slide_data.get("stat_value", "")
        stat_box = slide.shapes.add_textbox(
            Inches(1), Inches(2.0), content_width, Inches(1.5))
        tf = stat_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = stat_value
        run.font.size = Pt(54)
        run.font.bold = True
        run.font.color.rgb = theme["accent"]

        # Label below the stat
        stat_label = slide_data.get("stat_label", "")
        if stat_label:
            label_box = slide.shapes.add_textbox(
                Inches(1), Inches(3.6), content_width, Inches(0.8))
            tf2 = label_box.text_frame
            tf2.word_wrap = True
            p2 = tf2.paragraphs[0]
            p2.alignment = PP_ALIGN.CENTER
            run2 = p2.add_run()
            run2.text = stat_label
            run2.font.size = Pt(20)
            run2.font.color.rgb = theme["subtitle"]

        # Supporting context bullets
        bullets = slide_data.get("bullets", [])
        if bullets:
            bullet_box = slide.shapes.add_textbox(
                Inches(1.5), Inches(4.6),
                Emu(int(slide_width - Inches(3))), Inches(2.5))
            tf3 = bullet_box.text_frame
            tf3.word_wrap = True
            for j, bullet in enumerate(bullets):
                p3 = tf3.paragraphs[0] if j == 0 else tf3.add_paragraph()
                _add_formatted_runs(p3, f"\u2022 {bullet}", Pt(16), theme["body"], theme["accent"])
                p3.space_after = Pt(4)

        return slide

    def _add_comparison_slide(self, prs, title, slide_data, theme):
        """Add a two-column comparison slide."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_comparison_slide", {
            "method": "_add_comparison_slide (LEGACY)",
            "title": title,
            "left_heading": slide_data.get("left_heading"),
            "right_heading": slide_data.get("right_heading"),
            "left_points": len(slide_data.get("left_points", [])),
            "right_points": len(slide_data.get("right_points", [])),
            "title_font_name": "NONE (uses placeholder default)",
            "heading_font_name": "NONE (missing font.name)",
        })
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        # Title
        title_shape = slide.placeholders[0]
        title_shape.text = title
        for p in title_shape.text_frame.paragraphs:
            p.font.size = Pt(28)
            p.font.bold = True
            p.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        slide_width = prs.slide_width
        col_width = Emu(int((slide_width - Inches(2)) * 0.47))
        top_y = Inches(1.8)
        col_height = Inches(4.8)

        # Left column
        left_box = slide.shapes.add_textbox(
            Inches(0.5), top_y, col_width, col_height)
        ltf = left_box.text_frame
        ltf.word_wrap = True

        left_heading = slide_data.get("left_heading", "Option A")
        p_lh = ltf.paragraphs[0]
        run_lh = p_lh.add_run()
        run_lh.text = left_heading
        run_lh.font.size = Pt(22)
        run_lh.font.bold = True
        run_lh.font.color.rgb = theme["accent"]
        p_lh.space_after = Pt(12)

        for point in slide_data.get("left_points", []):
            p_l = ltf.add_paragraph()
            _add_formatted_runs(p_l, f"\u2022 {point}", Pt(16), theme["body"], theme["accent"])
            p_l.space_after = Pt(6)

        # Right column
        right_x = Emu(int(slide_width * 0.52))
        right_box = slide.shapes.add_textbox(
            right_x, top_y, col_width, col_height)
        rtf = right_box.text_frame
        rtf.word_wrap = True

        right_heading = slide_data.get("right_heading", "Option B")
        p_rh = rtf.paragraphs[0]
        run_rh = p_rh.add_run()
        run_rh.text = right_heading
        run_rh.font.size = Pt(22)
        run_rh.font.bold = True
        run_rh.font.color.rgb = theme["accent"]
        p_rh.space_after = Pt(12)

        for point in slide_data.get("right_points", []):
            p_r = rtf.add_paragraph()
            _add_formatted_runs(p_r, f"\u2022 {point}", Pt(16), theme["body"], theme["accent"])
            p_r.space_after = Pt(6)

        return slide

    # ------------------------------------------------------------------
    # New slide types (uniform signature for dispatch table)
    # ------------------------------------------------------------------

    def _add_title_hero_slide(self, prs, title, slide_data, theme,
                              image_path=None):
        """Title/hero slide with large display text and optional hero image."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_title_hero_slide", {
            "method": "_add_title_hero_slide",
            "title": title,
            "subtitle": slide_data.get("subtitle", ""),
            "has_image": bool(image_path),
            "image_path": str(image_path) if image_path else None,
            "image_exists": Path(image_path).exists() if image_path else False,
            "title_font": HEADING_FONT,
            "title_size": 44,
            "subtitle_font": BODY_FONT,
            "subtitle_size": 22,
        })
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        # Clear default placeholders
        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Optional hero image as background
        if image_path and Path(image_path).exists():
            try:
                slide.shapes.add_picture(
                    str(image_path), Inches(0), Inches(0),
                    prs.slide_width, prs.slide_height)
                # Semi-transparent overlay for text readability
                overlay = slide.shapes.add_shape(
                    MSO_SHAPE.RECTANGLE, Inches(0), Inches(0),
                    prs.slide_width, prs.slide_height)
                overlay.fill.solid()
                overlay.fill.fore_color.rgb = RGBColor(0x00, 0x00, 0x00)
                overlay.fill.fore_color.brightness = 0.0
                from pptx.oxml.ns import qn
                overlay.fill._fill.attrib[qn('a:opacity')] = '40000'
                overlay.line.fill.background()
            except Exception as e:
                self.logger.warning(f"[doc_gen] Hero image failed: {e}")

        # Accent strip at bottom
        strip_height = Inches(2.0)
        strip_y = Emu(int(prs.slide_height - strip_height))
        strip = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0), strip_y,
            prs.slide_width, strip_height)
        strip.fill.solid()
        strip.fill.fore_color.rgb = theme["accent"]
        strip.line.fill.background()

        # Title — large centered
        title_box = slide.shapes.add_textbox(
            Inches(1), Inches(1.5),
            Emu(int(prs.slide_width - Inches(2))), Inches(2.5))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(44)
        run.font.bold = True
        title_color = RGBColor(0xFF, 0xFF, 0xFF) if image_path else theme["heading"]
        run.font.color.rgb = title_color

        # Subtitle inside accent strip
        subtitle = slide_data.get("subtitle", "")
        if subtitle:
            sub_box = slide.shapes.add_textbox(
                Inches(1), Emu(int(strip_y + Inches(0.4))),
                Emu(int(prs.slide_width - Inches(2))), Inches(1.2))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            sp.alignment = PP_ALIGN.CENTER
            run_s = sp.add_run()
            run_s.text = subtitle
            run_s.font.name = BODY_FONT
            run_s.font.size = Pt(22)
            run_s.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

        return slide

    def _add_section_divider_slide(self, prs, title, slide_data, theme,
                                    image_path=None):
        """Numbered section divider for narrative pacing."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_section_divider_slide", {
            "method": "_add_section_divider_slide",
            "title": title,
            "section_number": slide_data.get("section_number", ""),
            "subtitle": slide_data.get("subtitle", ""),
            "fonts": {"number": HEADING_FONT, "title": HEADING_FONT, "subtitle": BODY_FONT},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        section_num = slide_data.get("section_number", "")

        # Large section number — left side, accent color
        if section_num:
            num_box = slide.shapes.add_textbox(
                Inches(1.0), Inches(2.0), Inches(2.5), Inches(3.0))
            tf = num_box.text_frame
            p = tf.paragraphs[0]
            run = p.add_run()
            run.text = str(section_num).zfill(2)
            run.font.name = HEADING_FONT
            run.font.size = Pt(72)
            run.font.bold = True
            run.font.color.rgb = theme["accent"]

        # Section title — to the right of the number
        title_left = Inches(4.0) if section_num else Inches(1.0)
        title_box = slide.shapes.add_textbox(
            title_left, Inches(2.2),
            Emu(int(prs.slide_width - title_left - Inches(1))), Inches(1.5))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(36)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        # Optional subtitle
        subtitle = slide_data.get("subtitle", "")
        if subtitle:
            sub_box = slide.shapes.add_textbox(
                title_left, Inches(3.8),
                Emu(int(prs.slide_width - title_left - Inches(1))), Inches(1.0))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            run_s = sp.add_run()
            run_s.text = subtitle
            run_s.font.name = BODY_FONT
            run_s.font.size = Pt(20)
            run_s.font.color.rgb = theme["subtitle"]

        # Bottom accent bar
        self._add_accent_bar(slide, prs, theme, y_pos=Inches(6.5))

        return slide

    def _add_agenda_slide(self, prs, title, slide_data, theme,
                          image_path=None):
        """Numbered topic list agenda slide."""
        _dbg = get_debug_logger()
        items = slide_data.get("agenda_items", slide_data.get("bullets", []))
        _dbg.log_skill_event("doc_gen", "render_agenda_slide", {
            "method": "_add_agenda_slide",
            "title": title,
            "item_count": len(items),
            "item_source": "agenda_items" if slide_data.get("agenda_items") else "bullets",
            "fonts": {"title": HEADING_FONT, "numbers": HEADING_FONT, "items": "via _add_formatted_runs"},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Title
        title_box = slide.shapes.add_textbox(
            Inches(0.5), Inches(0.4),
            Emu(int(prs.slide_width - Inches(1))), Inches(1.0))
        tf = title_box.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        # Agenda items — numbered list
        items = slide_data.get("agenda_items", slide_data.get("bullets", []))
        start_y = Inches(2.0)
        item_height = Inches(0.7)

        for idx, item in enumerate(items[:8]):  # Max 8 items
            y = start_y + idx * item_height

            # Number
            num_box = slide.shapes.add_textbox(
                Inches(1.0), y, Inches(1.0), item_height)
            ntf = num_box.text_frame
            np_ = ntf.paragraphs[0]
            np_.alignment = PP_ALIGN.RIGHT
            nr = np_.add_run()
            nr.text = f"{idx + 1:02d}"
            nr.font.name = HEADING_FONT
            nr.font.size = Pt(28)
            nr.font.bold = True
            nr.font.color.rgb = theme["accent"]

            # Topic text
            txt_box = slide.shapes.add_textbox(
                Inches(2.5), y, Inches(9.0), item_height)
            ttf = txt_box.text_frame
            ttf.word_wrap = True
            tp = ttf.paragraphs[0]
            _add_formatted_runs(tp, item, Pt(22), theme["body"], theme["accent"])

        return slide

    def _add_card_grid_slide(self, prs, title, slide_data, theme,
                             image_path=None):
        """Card grid (SmartArt substitute) — 3 or 4 columns."""
        _dbg = get_debug_logger()
        cards = slide_data.get("cards", [])
        is_4col = slide_data.get("slide_type") == "card_grid_4"
        _dbg.log_skill_event("doc_gen", "render_card_grid_slide", {
            "method": "_add_card_grid_slide",
            "title": title,
            "card_count": len(cards),
            "column_count": 4 if is_4col else 3,
            "slide_type": slide_data.get("slide_type"),
            "card_headings": [c.get("heading", "") for c in cards[:4]],
            "has_cards_key": "cards" in slide_data,
            "fonts": {"title": HEADING_FONT, "card_heading": HEADING_FONT, "card_body": "via _add_formatted_runs"},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Title
        title_box = slide.shapes.add_textbox(
            Inches(0.5), Inches(0.4),
            Emu(int(prs.slide_width - Inches(1))), Inches(1.0))
        tf = title_box.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        # Determine column count from slide_type
        cards = slide_data.get("cards", [])
        is_4col = slide_data.get("slide_type") == "card_grid_4"
        col_count = 4 if is_4col else 3
        cards = cards[:col_count]  # Clamp to column count

        if not cards:
            return slide

        # Layout calculations
        margin = Inches(0.5)
        gap = Inches(0.25) if is_4col else Inches(0.3)
        total_gap = gap * (col_count - 1)
        usable_width = int(prs.slide_width - 2 * margin - total_gap)
        card_width = usable_width // col_count
        card_top = Inches(1.9)
        card_height = Inches(4.5)

        for idx, card in enumerate(cards):
            x = int(margin + idx * (card_width + gap))

            # Card background — rounded rectangle
            bg = slide.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE,
                x, card_top, card_width, card_height)
            bg.fill.solid()
            bg.fill.fore_color.rgb = RGBColor(0xF5, 0xF5, 0xF5)
            bg.line.color.rgb = RGBColor(0xE0, 0xE0, 0xE0)
            bg.line.width = Pt(1)

            # Card heading
            heading_box = slide.shapes.add_textbox(
                x + Inches(0.2), card_top + Inches(0.3),
                card_width - Inches(0.4), Inches(0.8))
            htf = heading_box.text_frame
            htf.word_wrap = True
            hp = htf.paragraphs[0]
            hp.alignment = PP_ALIGN.CENTER
            hr = hp.add_run()
            hr.text = card.get("heading", "")
            hr.font.name = HEADING_FONT
            hr.font.size = Pt(20)
            hr.font.bold = True
            hr.font.color.rgb = theme["accent"]

            # Card description
            desc_box = slide.shapes.add_textbox(
                x + Inches(0.2), card_top + Inches(1.3),
                card_width - Inches(0.4), card_height - Inches(1.6))
            dtf = desc_box.text_frame
            dtf.word_wrap = True
            dp = dtf.paragraphs[0]
            _add_formatted_runs(
                dp, card.get("description", ""),
                Pt(14), theme["body"], theme["accent"])

        return slide

    def _add_timeline_slide(self, prs, title, slide_data, theme,
                            image_path=None):
        """Horizontal timeline with markers, labels, and connectors."""
        _dbg = get_debug_logger()
        points = slide_data.get("timeline_points", [])
        _dbg.log_skill_event("doc_gen", "render_timeline_slide", {
            "method": "_add_timeline_slide",
            "title": title,
            "point_count": len(points),
            "has_timeline_points_key": "timeline_points" in slide_data,
            "point_labels": [p.get("label", "") for p in points[:6]],
            "fonts": {"title": HEADING_FONT, "labels": HEADING_FONT, "descriptions": BODY_FONT},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Title
        title_box = slide.shapes.add_textbox(
            Inches(0.5), Inches(0.4),
            Emu(int(prs.slide_width - Inches(1))), Inches(1.0))
        tf = title_box.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        points = slide_data.get("timeline_points", [])[:6]  # Max 6 points
        if not points:
            return slide

        # Horizontal line
        line_y = Inches(3.8)
        line_left = Inches(1.5)
        line_width = Emu(int(prs.slide_width - Inches(3)))
        line_shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            line_left, line_y, line_width, Inches(0.04))
        line_shape.fill.solid()
        line_shape.fill.fore_color.rgb = theme["accent"]
        line_shape.line.fill.background()

        # Markers + labels at evenly spaced positions
        num_points = len(points)
        spacing = int(line_width) // max(num_points - 1, 1) if num_points > 1 else 0
        marker_size = Inches(0.3)

        for idx, point in enumerate(points):
            if num_points == 1:
                cx = int(line_left + line_width // 2)
            else:
                cx = int(line_left) + idx * spacing
            marker_x = cx - int(marker_size) // 2

            # Circular marker
            marker = slide.shapes.add_shape(
                MSO_SHAPE.OVAL,
                marker_x, Emu(int(line_y - marker_size // 2)),
                marker_size, marker_size)
            marker.fill.solid()
            marker.fill.fore_color.rgb = theme["accent"]
            marker.line.fill.background()

            # Alternate labels above/below
            label_text = point.get("label", "")
            desc_text = point.get("description", "")
            label_width = Inches(2.0)
            label_x = cx - int(label_width) // 2

            if idx % 2 == 0:  # Above
                # Label
                lbox = slide.shapes.add_textbox(
                    label_x, Inches(2.2), label_width, Inches(0.5))
                ltf = lbox.text_frame
                ltf.word_wrap = True
                lp = ltf.paragraphs[0]
                lp.alignment = PP_ALIGN.CENTER
                lr = lp.add_run()
                lr.text = label_text
                lr.font.name = HEADING_FONT
                lr.font.size = Pt(14)
                lr.font.bold = True
                lr.font.color.rgb = theme["heading"]
                # Description
                dbox = slide.shapes.add_textbox(
                    label_x, Inches(2.7), label_width, Inches(0.8))
                dtf = dbox.text_frame
                dtf.word_wrap = True
                dp = dtf.paragraphs[0]
                dp.alignment = PP_ALIGN.CENTER
                dr = dp.add_run()
                dr.text = desc_text
                dr.font.name = BODY_FONT
                dr.font.size = Pt(12)
                dr.font.color.rgb = theme["body"]
            else:  # Below
                lbox = slide.shapes.add_textbox(
                    label_x, Inches(4.3), label_width, Inches(0.5))
                ltf = lbox.text_frame
                ltf.word_wrap = True
                lp = ltf.paragraphs[0]
                lp.alignment = PP_ALIGN.CENTER
                lr = lp.add_run()
                lr.text = label_text
                lr.font.name = HEADING_FONT
                lr.font.size = Pt(14)
                lr.font.bold = True
                lr.font.color.rgb = theme["heading"]

                dbox = slide.shapes.add_textbox(
                    label_x, Inches(4.8), label_width, Inches(0.8))
                dtf = dbox.text_frame
                dtf.word_wrap = True
                dp = dtf.paragraphs[0]
                dp.alignment = PP_ALIGN.CENTER
                dr = dp.add_run()
                dr.text = desc_text
                dr.font.name = BODY_FONT
                dr.font.size = Pt(12)
                dr.font.color.rgb = theme["body"]

        return slide

    def _add_data_table_slide(self, prs, title, slide_data, theme,
                              image_path=None):
        """Styled data table with header row and alternating shading."""
        _dbg = get_debug_logger()
        headers = slide_data.get("table_headers", [])
        rows = slide_data.get("table_rows", [])
        _dbg.log_skill_event("doc_gen", "render_data_table_slide", {
            "method": "_add_data_table_slide",
            "title": title,
            "has_table_headers": bool(headers),
            "header_count": len(headers),
            "row_count": len(rows),
            "headers": headers[:6],
            "fallback_to_bullets": not bool(headers),
            "fonts": {"title": HEADING_FONT, "header": HEADING_FONT, "cells": BODY_FONT},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Title
        title_box = slide.shapes.add_textbox(
            Inches(0.5), Inches(0.4),
            Emu(int(prs.slide_width - Inches(1))), Inches(1.0))
        tf = title_box.text_frame
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        self._add_accent_bar(slide, prs, theme)

        headers = slide_data.get("table_headers", [])
        rows = slide_data.get("table_rows", [])

        if not headers:
            # Fallback to bullet slide if no table data
            bullets = slide_data.get("bullets", [])
            for j, bullet in enumerate(bullets):
                bp = tf.add_paragraph() if j > 0 else tf.paragraphs[0]
                _add_formatted_runs(bp, bullet, Pt(16), theme["body"],
                                    theme["accent"])
            return slide

        # Clamp dimensions
        num_cols = min(len(headers), 6)
        num_rows = min(len(rows), 8)
        headers = headers[:num_cols]
        rows = [r[:num_cols] for r in rows[:num_rows]]

        # Add table
        table_top = Inches(1.9)
        table_left = Inches(0.8)
        table_width = Emu(int(prs.slide_width - Inches(1.6)))
        row_height = Inches(0.55)
        total_height = row_height * (num_rows + 1)

        table_shape = slide.shapes.add_table(
            num_rows + 1, num_cols,
            table_left, table_top, table_width, total_height)
        table = table_shape.table

        # Style header row
        for c, header_text in enumerate(headers):
            cell = table.cell(0, c)
            cell.text = ""
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            run = p.add_run()
            run.text = str(header_text)
            run.font.name = HEADING_FONT
            run.font.size = Pt(14)
            run.font.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            # Header cell fill
            cell_fill = cell.fill
            cell_fill.solid()
            cell_fill.fore_color.rgb = theme["accent"]

        # Data rows with alternating shading
        alt_fill = RGBColor(0xF0, 0xF0, 0xF0)
        for r, row_data in enumerate(rows):
            for c in range(num_cols):
                cell = table.cell(r + 1, c)
                cell_text = str(row_data[c]) if c < len(row_data) else ""
                cell.text = ""
                p = cell.text_frame.paragraphs[0]
                run = p.add_run()
                run.text = cell_text
                run.font.name = BODY_FONT
                run.font.size = Pt(13)
                run.font.color.rgb = theme["body"]
                # Alternating row fill
                if r % 2 == 1:
                    cell_fill = cell.fill
                    cell_fill.solid()
                    cell_fill.fore_color.rgb = alt_fill

        return slide

    def _add_full_bleed_image_slide(self, prs, title, slide_data, theme,
                                     image_path=None):
        """Full-bleed image with title overlay."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_full_bleed_image_slide", {
            "method": "_add_full_bleed_image_slide",
            "title": title,
            "overlay_text": slide_data.get("overlay_text", ""),
            "has_image": bool(image_path),
            "image_path": str(image_path) if image_path else None,
            "image_exists": Path(image_path).exists() if image_path else False,
            "fonts": {"title": HEADING_FONT, "overlay": BODY_FONT},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        has_image = image_path and Path(image_path).exists()

        if has_image:
            # Full-bleed image
            try:
                slide.shapes.add_picture(
                    str(image_path), Inches(0), Inches(0),
                    prs.slide_width, prs.slide_height)
            except Exception as e:
                self.logger.warning(f"[doc_gen] Full-bleed image failed: {e}")
                has_image = False

        if has_image:
            # Dark overlay for text readability
            overlay = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, Inches(0), Inches(0),
                prs.slide_width, prs.slide_height)
            overlay.fill.solid()
            overlay.fill.fore_color.rgb = RGBColor(0x00, 0x00, 0x00)
            overlay.line.fill.background()
            # Set 40% opacity via XML
            from pptx.oxml.ns import qn
            solid = overlay.fill._fill.find(qn('a:solidFill'))
            if solid is not None:
                clr = solid[0]
                alpha = clr.makeelement(qn('a:alpha'), {'val': '40000'})
                clr.append(alpha)
            title_color = RGBColor(0xFF, 0xFF, 0xFF)
        else:
            # Fallback: accent-colored background
            bg = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, Inches(0), Inches(0),
                prs.slide_width, prs.slide_height)
            bg.fill.solid()
            bg.fill.fore_color.rgb = theme["accent"]
            bg.line.fill.background()
            title_color = RGBColor(0xFF, 0xFF, 0xFF)

        # Title — centered
        title_box = slide.shapes.add_textbox(
            Inches(1), Inches(2.5),
            Emu(int(prs.slide_width - Inches(2))), Inches(2.0))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(36)
        run.font.bold = True
        run.font.color.rgb = title_color

        # Overlay text
        overlay_text = slide_data.get("overlay_text", "")
        if overlay_text:
            sub_box = slide.shapes.add_textbox(
                Inches(1), Inches(4.5),
                Emu(int(prs.slide_width - Inches(2))), Inches(1.0))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            sp.alignment = PP_ALIGN.CENTER
            sr = sp.add_run()
            sr.text = overlay_text
            sr.font.name = BODY_FONT
            sr.font.size = Pt(20)
            sr.font.color.rgb = title_color

        return slide

    def _add_image_text_reversed_slide(self, prs, title, slide_data, theme,
                                        image_path=None):
        """Image on left, text on right (mirror of standard image+text)."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_image_text_reversed_slide", {
            "method": "_add_image_text_reversed_slide",
            "title": title,
            "bullet_count": len(slide_data.get("bullets", [])),
            "has_image": bool(image_path),
            "image_path": str(image_path) if image_path else None,
            "image_exists": Path(image_path).exists() if image_path else False,
            "fonts": {"title": HEADING_FONT, "body": "via _add_formatted_runs"},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        # Title via placeholder
        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(28)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]
            paragraph.font.name = HEADING_FONT

        self._add_accent_bar(slide, prs, theme)

        slide_width = prs.slide_width
        bullets = slide_data.get("bullets", [])

        # Image — left 40%
        if image_path and Path(image_path).exists():
            img_width = Emu(int(slide_width * 0.38))
            img_max_height = Inches(4.5)
            try:
                pic = slide.shapes.add_picture(
                    str(image_path), Inches(0.5), Inches(1.8),
                    width=img_width)
                if pic.height > img_max_height:
                    ratio = img_max_height / pic.height
                    pic.height = img_max_height
                    pic.width = int(pic.width * ratio)
            except Exception as e:
                self.logger.warning(f"[doc_gen] Reversed image failed: {e}")

        # Text — right 55%
        txt_left = Emu(int(slide_width * 0.43))
        txt_width = Emu(int(slide_width * 0.52))
        txBox = slide.shapes.add_textbox(
            txt_left, Inches(1.8), txt_width, Inches(4.5))
        tf = txBox.text_frame
        tf.word_wrap = True

        for j, bullet in enumerate(bullets):
            bp = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            _add_formatted_runs(
                bp, f"\u2022 {bullet}", Pt(16), theme["body"], theme["accent"])
            bp.space_after = Pt(6)

        return slide

    def _add_closing_slide(self, prs, title, slide_data, theme,
                           image_path=None):
        """Closing/thank-you slide with optional contact info and takeaways."""
        _dbg = get_debug_logger()
        _dbg.log_skill_event("doc_gen", "render_closing_slide", {
            "method": "_add_closing_slide",
            "title": title,
            "closing_text": slide_data.get("closing_text", ""),
            "bullet_count": len(slide_data.get("bullets", [])),
            "fonts": {"title": HEADING_FONT, "closing": BODY_FONT, "bullets": "via _add_formatted_runs"},
        })
        slide_layout = prs.slide_layouts[5]
        slide = prs.slides.add_slide(slide_layout)

        for ph in list(slide.placeholders):
            sp = ph._element
            sp.getparent().remove(sp)

        # Accent strip at bottom
        strip_height = Inches(1.5)
        strip_y = Emu(int(prs.slide_height - strip_height))
        strip = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0), strip_y,
            prs.slide_width, strip_height)
        strip.fill.solid()
        strip.fill.fore_color.rgb = theme["accent"]
        strip.line.fill.background()

        # Title — large centered
        title_box = slide.shapes.add_textbox(
            Inches(1), Inches(1.5),
            Emu(int(prs.slide_width - Inches(2))), Inches(2.0))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = title
        run.font.name = HEADING_FONT
        run.font.size = Pt(36)
        run.font.bold = True
        run.font.color.rgb = theme["heading"]

        # Closing text
        closing_text = slide_data.get("closing_text", "")
        if closing_text:
            close_box = slide.shapes.add_textbox(
                Inches(2), Inches(3.5),
                Emu(int(prs.slide_width - Inches(4))), Inches(1.0))
            ctf = close_box.text_frame
            ctf.word_wrap = True
            cp = ctf.paragraphs[0]
            cp.alignment = PP_ALIGN.CENTER
            cr = cp.add_run()
            cr.text = closing_text
            cr.font.name = BODY_FONT
            cr.font.size = Pt(18)
            cr.font.color.rgb = theme["subtitle"]

        # Key takeaway bullets
        bullets = slide_data.get("bullets", [])
        if bullets:
            bullet_box = slide.shapes.add_textbox(
                Inches(2), Inches(4.5),
                Emu(int(prs.slide_width - Inches(4))), Inches(1.8))
            btf = bullet_box.text_frame
            btf.word_wrap = True
            for j, bullet in enumerate(bullets):
                bp = btf.paragraphs[0] if j == 0 else btf.add_paragraph()
                bp.alignment = PP_ALIGN.CENTER
                _add_formatted_runs(
                    bp, bullet, Pt(16), theme["body"], theme["accent"])
                bp.space_after = Pt(4)

        return slide

    def _add_slide_number(self, slide, number, total, prs):
        """Add slide number in the bottom-right corner."""
        num_box = slide.shapes.add_textbox(
            Emu(int(prs.slide_width - Inches(1))),
            Emu(int(prs.slide_height - Inches(0.5))),
            Inches(0.8), Inches(0.3))
        tf = num_box.text_frame
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        run = p.add_run()
        run.text = f"{number} / {total}"
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    # ------------------------------------------------------------------
    # DOCX Generation
    # ------------------------------------------------------------------

    def create_document(self, structure: dict, filename: str = "document.docx",
                        images: dict = None) -> Optional[Path]:
        """Generate a DOCX document from a structured outline.

        Args:
            structure: Dict with keys: title, subtitle, slides[] (sections)
            filename: Output filename (saved to share/)
            images: Optional {section_index: image_path} mapping

        Returns:
            Path to saved .docx file, or None on failure
        """
        try:
            doc = Document()

            # Document title
            title_para = doc.add_heading(structure.get("title", "Document"), level=0)
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            # Subtitle
            subtitle = structure.get("subtitle", "")
            if subtitle:
                sub_para = doc.add_paragraph()
                sub_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = sub_para.add_run(subtitle)
                run.italic = True
                run.font.size = DocxPt(14)
                run.font.color.rgb = DocxRGB(0x55, 0x55, 0x77)

            doc.add_paragraph()  # Spacer

            sections = structure.get("slides", [])

            for i, section in enumerate(sections):
                if i == 0 and not section.get("bullets"):
                    continue

                section_title = section.get("title", f"Section {i}")
                bullets = section.get("bullets", [])
                slide_type = section.get("slide_type", "bullets")

                # Section heading
                doc.add_heading(section_title, level=1)

                # Stat section — prominent value + label
                if slide_type == "stat":
                    stat_val = section.get("stat_value", "")
                    stat_label = section.get("stat_label", "")
                    if stat_val:
                        stat_para = doc.add_paragraph()
                        stat_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        run = stat_para.add_run(stat_val)
                        run.bold = True
                        run.font.size = DocxPt(24)
                        run.font.color.rgb = DocxRGB(0x2B, 0x57, 0x9A)
                    if stat_label:
                        label_para = doc.add_paragraph()
                        label_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        run = label_para.add_run(stat_label)
                        run.font.size = DocxPt(12)
                        run.font.color.rgb = DocxRGB(0x66, 0x66, 0x66)

                # Image (if available)
                image_path = images.get(i) if images else None
                if image_path and Path(image_path).exists():
                    try:
                        doc.add_picture(str(image_path), width=DocxInches(5.5))
                        last_paragraph = doc.paragraphs[-1]
                        last_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    except Exception as e:
                        self.logger.warning(f"[doc_gen] Failed to add image to doc section {i}: {e}")

                # Bullet points with bold markup (accent-colored leads)
                for bullet in bullets:
                    p = doc.add_paragraph(style="List Bullet")
                    segments = _parse_bold_text(bullet)
                    for seg_text, is_bold in segments:
                        run = p.add_run(seg_text)
                        run.bold = is_bold
                        if is_bold:
                            run.font.color.rgb = DocxRGB(0x2B, 0x57, 0x9A)

                # Speaker notes as callout
                notes = section.get("notes", "")
                if notes:
                    notes_para = doc.add_paragraph()
                    run = notes_para.add_run(f"\u25B6 {notes}")
                    run.italic = True
                    run.font.size = DocxPt(10)
                    run.font.color.rgb = DocxRGB(0x66, 0x66, 0x99)

            # Save
            output_path = SHARE_DIR / filename
            doc.save(str(output_path))
            self.logger.info(f"[doc_gen] Created DOCX: {output_path} ({len(sections)} sections)")
            return output_path

        except Exception as e:
            self.logger.error(f"[doc_gen] DOCX creation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # PDF Conversion
    # ------------------------------------------------------------------

    def convert_to_pdf(self, source_path: Path) -> Optional[Path]:
        """Convert a PPTX or DOCX file to PDF via LibreOffice CLI.

        Tries native `libreoffice` command first, then flatpak as fallback.

        Args:
            source_path: Path to the .pptx or .docx file

        Returns:
            Path to the PDF file, or None on failure
        """
        commands = [
            ["libreoffice", "--headless", "--convert-to", "pdf",
             "--outdir", str(SHARE_DIR), str(source_path)],
            ["flatpak", "run", "org.libreoffice.LibreOffice",
             "--headless", "--convert-to", "pdf",
             "--outdir", str(SHARE_DIR), str(source_path)],
        ]

        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    pdf_name = source_path.stem + ".pdf"
                    pdf_path = SHARE_DIR / pdf_name
                    if pdf_path.exists():
                        self.logger.info(f"[doc_gen] Converted to PDF: {pdf_path}")
                        return pdf_path
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                self.logger.error("[doc_gen] LibreOffice conversion timed out (60s)")
                return None
            except Exception as e:
                self.logger.error(f"[doc_gen] PDF conversion failed: {e}")
                continue

        self.logger.error("[doc_gen] LibreOffice not available — cannot convert to PDF")
        return None
