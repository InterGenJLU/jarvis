"""
Document Generator Module

Generates PPTX presentations, DOCX documents, and PDF files
from structured outline data. Used by the file_editor skill
for voice-driven document creation.

Supports multiple slide types (bullets, stat callout, comparison),
bold text markup, speaker notes, slide numbers, and theme selection.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from pptx.dml.color import RGBColor

from docx import Document
from docx.shared import Pt as DocxPt, Inches as DocxInches, RGBColor as DocxRGB
from docx.enum.text import WD_ALIGN_PARAGRAPH

from core.logger import get_logger


SHARE_DIR = Path(os.path.expanduser("~/jarvis/share"))

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


def _add_formatted_runs(paragraph, text, font_size, font_color):
    """Add text with bold/normal runs parsed from **markdown** to a paragraph."""
    segments = _parse_bold_text(text)
    for seg_text, is_bold in segments:
        run = paragraph.add_run()
        run.text = seg_text
        run.font.size = font_size
        run.font.color.rgb = font_color
        run.font.bold = is_bold


class DocumentGenerator:
    """Generates PPTX, DOCX, and PDF documents from structured outlines."""

    def __init__(self, config=None):
        self.logger = get_logger(__name__, config)
        SHARE_DIR.mkdir(parents=True, exist_ok=True)

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
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)

            theme = THEMES.get(theme_name, THEMES["professional"])
            slides_data = structure.get("slides", [])
            total_slides = len(slides_data)

            for i, slide_data in enumerate(slides_data):
                slide_title = slide_data.get("title", f"Slide {i + 1}")
                bullets = slide_data.get("bullets", [])
                slide_type = slide_data.get("slide_type", "bullets")
                image_path = images.get(i) if images else None
                notes = slide_data.get("notes", "")

                if i == 0:
                    slide = self._add_title_slide(
                        prs, structure.get("title", slide_title),
                        structure.get("subtitle", ""), theme)
                elif slide_type == "stat" and slide_data.get("stat_value"):
                    slide = self._add_stat_slide(prs, slide_title, slide_data, theme)
                elif slide_type == "comparison" and slide_data.get("left_heading"):
                    slide = self._add_comparison_slide(prs, slide_title, slide_data, theme)
                elif image_path and Path(image_path).exists():
                    slide = self._add_image_slide(
                        prs, slide_title, bullets, image_path, theme)
                else:
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
            self.logger.info(f"[doc_gen] Created PPTX: {output_path} ({total_slides} slides)")
            return output_path

        except Exception as e:
            self.logger.error(f"[doc_gen] PPTX creation failed: {e}")
            return None

    def _add_title_slide(self, prs, title, subtitle, theme):
        """Add a title slide (first slide of the deck)."""
        slide_layout = prs.slide_layouts[0]
        slide = prs.slides.add_slide(slide_layout)

        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(36)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]

        if len(slide.placeholders) > 1:
            subtitle_shape = slide.placeholders[1]
            subtitle_shape.text = subtitle
            for paragraph in subtitle_shape.text_frame.paragraphs:
                paragraph.font.size = Pt(20)
                paragraph.font.color.rgb = theme["subtitle"]

        return slide

    def _add_content_slide(self, prs, title, bullets, theme):
        """Add a content slide with title and bullet points (bold markup supported)."""
        slide_layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(slide_layout)

        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(28)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]

        if len(slide.placeholders) > 1:
            body_shape = slide.placeholders[1]
            tf = body_shape.text_frame
            tf.clear()

            for j, bullet in enumerate(bullets):
                p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                _add_formatted_runs(p, bullet, Pt(18), theme["body"])
                p.space_after = Pt(8)
                p.level = 0

        return slide

    def _add_image_slide(self, prs, title, bullets, image_path, theme):
        """Add a content slide with text on left and image on right."""
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        title_shape = slide.placeholders[0]
        title_shape.text = title
        for paragraph in title_shape.text_frame.paragraphs:
            paragraph.font.size = Pt(28)
            paragraph.font.bold = True
            paragraph.font.color.rgb = theme["heading"]

        slide_width = prs.slide_width

        # Text box — left 55%
        txBox = slide.shapes.add_textbox(
            Inches(0.5), Inches(1.8),
            Emu(int(slide_width * 0.52)), Inches(4.5))
        tf = txBox.text_frame
        tf.word_wrap = True

        for j, bullet in enumerate(bullets):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            _add_formatted_runs(p, f"\u2022 {bullet}", Pt(16), theme["body"])
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
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        # Title
        title_shape = slide.placeholders[0]
        title_shape.text = title
        for p in title_shape.text_frame.paragraphs:
            p.font.size = Pt(28)
            p.font.bold = True
            p.font.color.rgb = theme["heading"]

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
                _add_formatted_runs(p3, f"\u2022 {bullet}", Pt(16), theme["body"])
                p3.space_after = Pt(4)

        return slide

    def _add_comparison_slide(self, prs, title, slide_data, theme):
        """Add a two-column comparison slide."""
        slide_layout = prs.slide_layouts[5]  # Title Only
        slide = prs.slides.add_slide(slide_layout)

        # Title
        title_shape = slide.placeholders[0]
        title_shape.text = title
        for p in title_shape.text_frame.paragraphs:
            p.font.size = Pt(28)
            p.font.bold = True
            p.font.color.rgb = theme["heading"]

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
            _add_formatted_runs(p_l, f"\u2022 {point}", Pt(16), theme["body"])
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
            _add_formatted_runs(p_r, f"\u2022 {point}", Pt(16), theme["body"])
            p_r.space_after = Pt(6)

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

                # Bullet points with bold markup
                for bullet in bullets:
                    p = doc.add_paragraph(style="List Bullet")
                    segments = _parse_bold_text(bullet)
                    for seg_text, is_bold in segments:
                        run = p.add_run(seg_text)
                        run.bold = is_bold

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
