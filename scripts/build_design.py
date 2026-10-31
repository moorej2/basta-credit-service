"""Regenerate the two-page PDF from docs/design.md (requires reportlab)."""

import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

ROOT = Path(__file__).resolve().parents[1]


def build():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="DesignTitle",
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            textColor=colors.black,
            spaceAfter=14,
        )
    )
    styles.add(
        ParagraphStyle(
            name="DesignSection",
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=colors.black,
            spaceBefore=0,
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(
            name="DesignSubsection",
            fontName="Helvetica-Bold",
            fontSize=11.5,
            leading=15,
            textColor=colors.black,
            spaceBefore=12,
            spaceAfter=7,
            keepWithNext=True,
        )
    )
    styles.add(
        ParagraphStyle(
            name="DesignBody",
            fontName="Helvetica",
            fontSize=10.5,
            leading=15.5,
            textColor=colors.black,
            alignment=TA_LEFT,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="DesignBullet",
            parent=styles["DesignBody"],
            leftIndent=12,
            bulletIndent=0,
            spaceAfter=9,
        )
    )

    def inline(value):
        value = escape(value)
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
        return re.sub(r"`(.+?)`", r'<font name="Courier" size="9.3">\1</font>', value)

    story = []
    text = (ROOT / "docs/design.md").read_text()
    for block in re.split(r"\n\s*\n", text.strip()):
        if block == "<!-- PAGE BREAK -->":
            story.append(PageBreak())
        elif block.startswith("### "):
            story.append(Paragraph(escape(block[4:]), styles["DesignSubsection"]))
        elif block.startswith("## "):
            story.append(Paragraph(escape(block[3:]), styles["DesignSection"]))
        elif block.startswith("# "):
            story.append(Paragraph(escape(block[2:]), styles["DesignTitle"]))
        elif re.match(r"\d+\. ", block):
            for line in block.splitlines():
                number, content = line.split(". ", 1)
                story.append(
                    Paragraph(inline(content), styles["DesignBullet"], bulletText=number + ".")
                )
        elif block.startswith("- "):
            for line in block.splitlines():
                story.append(
                    Paragraph(inline(line[2:]), styles["DesignBullet"], bulletText="\u2022")
                )
        else:
            story.append(Paragraph(inline(block.replace("\n", " ")), styles["DesignBody"]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(0.7 * inch, 0.38 * inch, "Basta tech lead exercise | Design document")
        canvas.drawRightString(7.8 * inch, 0.38 * inch, f"{doc.page} / 2")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(ROOT / "docs/design.pdf"),
        pagesize=(8.5 * inch, 11 * inch),
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.65 * inch,
        title="Async credit processing service design",
        author="",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    build()
