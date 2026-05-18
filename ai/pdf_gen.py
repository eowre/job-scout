import io
import pdfplumber

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

ACCENT = colors.HexColor("#2E75B6")
DARK   = colors.HexColor("#1e293b")
MUTED  = colors.HexColor("#64748b")
BLACK  = colors.HexColor("#0f172a")

# ── Base style values (at scale=1.0) ──────────────────────────────────────────
_BASE = {
    "name_size":       16,
    "contact_size":    8,
    "summary_size":    8.5,
    "section_size":    8.5,
    "job_title_size":  8.5,
    "job_meta_size":   8,
    "bullet_size":     8,
    "edu_size":        8.5,
    "edu_meta_size":   8,
    "skill_size":      8,
    "extra_size":      8,
    # Spacing
    "name_after":      2,
    "contact_after":   6,
    "summary_after":   5,
    "section_before":  5,
    "section_after":   2,
    "job_meta_after":  2,
    "bullet_leading":  10,
    "bullet_after":    1,
    "spacer":          3,
    "rule_after":      3,
    # Margins (in inches)
    "margin":          0.5,
}


def _styles(s):
    """Build style dict scaled by factor s."""
    def sz(key):  return _BASE[key] * s
    def sp(key):  return _BASE[key] * s

    return {
        "name": ParagraphStyle(
            "name", fontName="Helvetica-Bold", fontSize=sz("name_size"),
            textColor=DARK, alignment=TA_CENTER, spaceAfter=sp("name_after")
        ),
        "contact": ParagraphStyle(
            "contact", fontName="Helvetica", fontSize=sz("contact_size"),
            textColor=MUTED, alignment=TA_CENTER, spaceAfter=sp("contact_after")
        ),
        "summary": ParagraphStyle(
            "summary", fontName="Helvetica", fontSize=sz("summary_size"),
            textColor=BLACK, leading=sz("summary_size") * 1.3,
            spaceAfter=sp("summary_after")
        ),
        "section": ParagraphStyle(
            "section", fontName="Helvetica-Bold", fontSize=sz("section_size"),
            textColor=ACCENT, spaceBefore=sp("section_before"),
            spaceAfter=sp("section_after"),
            textTransform="uppercase", letterSpacing=0.5
        ),
        "job_title": ParagraphStyle(
            "job_title", fontName="Helvetica-Bold", fontSize=sz("job_title_size"),
            textColor=DARK, spaceAfter=1
        ),
        "job_meta": ParagraphStyle(
            "job_meta", fontName="Helvetica-Oblique", fontSize=sz("job_meta_size"),
            textColor=MUTED, spaceAfter=sp("job_meta_after")
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName="Helvetica", fontSize=sz("bullet_size"),
            textColor=BLACK, leading=sp("bullet_leading"),
            leftIndent=10 * s, bulletIndent=0,
            spaceAfter=sp("bullet_after"), bulletText="\u2022"
        ),
        "edu_degree": ParagraphStyle(
            "edu_degree", fontName="Helvetica-Bold", fontSize=sz("edu_size"),
            textColor=DARK, spaceAfter=1
        ),
        "edu_school": ParagraphStyle(
            "edu_school", fontName="Helvetica-Oblique", fontSize=sz("edu_meta_size"),
            textColor=MUTED, spaceAfter=sp("section_after")
        ),
        "skill": ParagraphStyle(
            "skill", fontName="Helvetica", fontSize=sz("skill_size"),
            textColor=BLACK, leading=sp("bullet_leading"),
            leftIndent=10 * s, bulletIndent=0,
            spaceAfter=sp("bullet_after"), bulletText="\u2022"
        ),
        "extra": ParagraphStyle(
            "extra", fontName="Helvetica", fontSize=sz("extra_size"),
            textColor=BLACK, leading=sp("bullet_leading"),
            leftIndent=10 * s, bulletIndent=0,
            spaceAfter=sp("bullet_after"), bulletText="\u2022"
        ),
    }


def _rule(s):
    return HRFlowable(
        width="100%", thickness=1,
        color=ACCENT,
        spaceAfter=_BASE["rule_after"] * s,
        spaceBefore=1
    )


def _two_col(left_text, right_text, scale):
    from reportlab.platypus import Table, TableStyle
    lsz = _BASE["job_title_size"] * scale
    rsz = _BASE["job_meta_size"] * scale
    left_style = ParagraphStyle(
        "left", fontName="Helvetica-Bold", fontSize=lsz, textColor=DARK
    )
    right_style = ParagraphStyle(
        "right", fontName="Helvetica", fontSize=rsz,
        textColor=MUTED, alignment=2
    )
    tbl = Table(
        [[Paragraph(left_text, left_style), Paragraph(right_text, right_style)]],
        colWidths=["70%", "30%"]
    )
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
    ]))
    return tbl


def _build_story(structured: dict, scale: float) -> list:
    """Build the reportlab story flowables at the given scale."""
    st = _styles(scale)
    s = scale
    story = []

    # Header
    story.append(Paragraph(structured.get("name", "Your Name"), st["name"]))
    if structured.get("contact"):
        story.append(Paragraph(structured["contact"], st["contact"]))

    # Summary
    if structured.get("summary"):
        block = [
            Paragraph("Professional Summary", st["section"]),
            _rule(s),
            Paragraph(structured["summary"], st["summary"]),
        ]
        story.append(KeepTogether(block))

    # Experience
    if structured.get("experience"):
        story.append(Paragraph("Experience", st["section"]))
        story.append(_rule(s))
        for job in structured["experience"]:
            job_block = [_two_col(job.get("title", ""), job.get("dates", ""), s)]
            if job.get("company"):
                job_block.append(Paragraph(job["company"], st["job_meta"]))
            for b in job.get("bullets", []):
                job_block.append(Paragraph(b, st["bullet"]))
            job_block.append(Spacer(1, _BASE["spacer"] * s))
            story.append(KeepTogether(job_block))

    # Education
    if structured.get("education"):
        story.append(Paragraph("Education", st["section"]))
        story.append(_rule(s))
        for edu in structured["education"]:
            edu_block = [_two_col(edu.get("degree", ""), edu.get("dates", ""), s)]
            if edu.get("school"):
                edu_block.append(Paragraph(edu["school"], st["edu_school"]))
            if edu.get("notes"):
                edu_block.append(Paragraph(edu["notes"], st["edu_school"]))
            story.append(KeepTogether(edu_block))

    # Skills
    if structured.get("skills"):
        block = [Paragraph("Skills", st["section"]), _rule(s)]
        for skill in structured["skills"]:
            block.append(Paragraph(skill, st["skill"]))
        story.append(KeepTogether(block))

    # Extras
    if structured.get("extras"):
        block = [Paragraph("Additional", st["section"]), _rule(s)]
        for extra in structured["extras"]:
            block.append(Paragraph(extra, st["extra"]))
        story.append(KeepTogether(block))

    return story


def _render_to_bytes(structured: dict, scale: float) -> bytes:
    """Render the resume to a bytes buffer at the given scale."""
    margin = _BASE["margin"] * scale * inch
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )
    doc.build(_build_story(structured, scale))
    return buf.getvalue()


def _page_count(pdf_bytes: bytes) -> int:
    """Count pages in a PDF byte string using pdfplumber."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)


def generate_pdf(structured: dict, output_path: str) -> str:
    """
    Build a professional single-page resume PDF. Iteratively reduces scale
    until the output fits on one page (up to 6 attempts, ~12% reduction each).
    """
    scale = 1.0
    pdf_bytes = None

    for attempt in range(7):
        pdf_bytes = _render_to_bytes(structured, scale)
        pages = _page_count(pdf_bytes)
        if pages <= 1:
            break
        # Reduce scale by ~11% each round; stops at ~scale=0.47 after 6 rounds
        scale *= 0.89

    with open(output_path, "wb") as f:
        f.write(pdf_bytes)
    return output_path
