"""Fixed, trusted renderers (ART-01): Markdown preview HTML, CSV export, Markdown to PDF.

No renderer executes content: Markdown is parsed with raw HTML disabled and the output is
sanitized again with an allowlist; the PDF is built from parsed tokens with all text escaped.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from typing import Any
from xml.sax.saxutils import escape

import nh3
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})

PREVIEW_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code",
    "em", "strong", "a", "table", "thead", "tbody", "tr", "th", "td",
}


def markdown_to_safe_html(text: str) -> str:
    """Raw HTML in the source is shown as text, then the output passes an allowlist sanitizer.
    Images are dropped (no remote fetches from a preview); links allow only http(s) and mailto."""
    rendered = _md.render(text)
    return nh3.clean(
        rendered,
        tags=PREVIEW_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


# ------------------------------------------------------------------ CSV

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
CSV_DISCLOSURE = ("{n} cell(s) began with =, +, -, @, tab, or carriage return and were prefixed with a single quote (') "
                  "so spreadsheet apps show them as text instead of running them as formulas.")


def escape_cell(value: Any) -> tuple[str, bool]:
    s = "" if value is None else str(value)
    if s.startswith(FORMULA_PREFIXES) and not _PLAIN_NUMBER.match(s):
        return "'" + s, True
    return s, False


def rows_to_csv(rows: Iterable[Iterable[Any]]) -> tuple[bytes, int]:
    """Returns (UTF-8 CSV bytes, number of escaped cells). Plain signed numbers such as -5 or +1.5
    are left alone: spreadsheet apps read them as numbers, never as formulas."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    escaped = 0
    for row in rows:
        out = []
        for cell in row:
            v, changed = escape_cell(cell)
            escaped += changed
            out.append(v)
        w.writerow(out)
    return buf.getvalue().encode("utf-8"), escaped


def csv_preview(text: str, max_rows: int = 200) -> list[list[str]]:
    return [row for _, row in zip(range(max_rows), csv.reader(io.StringIO(text)))]


# ------------------------------------------------------------------ PDF (reportlab; see tech-design section 19)


def _inline(tokens: list[Any]) -> str:
    """Inline markdown tokens to reportlab paragraph markup, escaping every text run."""
    out: list[str] = []
    link_stack: list[str | None] = []
    for t in tokens:
        if t.type == "text":
            out.append(escape(t.content))
        elif t.type == "code_inline":
            out.append(f'<font face="Courier">{escape(t.content)}</font>')
        elif t.type in ("softbreak",):
            out.append(" ")
        elif t.type == "hardbreak":
            out.append("<br/>")
        elif t.type == "strong_open":
            out.append("<b>")
        elif t.type == "strong_close":
            out.append("</b>")
        elif t.type == "em_open":
            out.append("<i>")
        elif t.type == "em_close":
            out.append("</i>")
        elif t.type == "link_open":
            link_stack.append(t.attrGet("href"))
        elif t.type == "link_close":
            href = link_stack.pop() if link_stack else None
            if href:
                out.append(f" ({escape(str(href))})")
        elif t.type == "image":
            out.append(escape(f"[image: {t.content}]"))
        elif t.content:
            out.append(escape(t.content))
    return "".join(out)


def markdown_to_pdf(text: str, title: str) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, Preformatted, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    heading = {1: styles["Heading1"], 2: styles["Heading2"], 3: styles["Heading3"], 4: styles["Heading4"], 5: styles["Heading5"], 6: styles["Heading6"]}
    story: list[Any] = []
    tokens = _md.parse(text)
    list_depth = 0
    ordered: list[int | None] = []
    pending_heading: int | None = None
    quote = 0
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            pending_heading = int(t.tag[1])
        elif t.type == "bullet_list_open":
            list_depth += 1
            ordered.append(None)
        elif t.type == "ordered_list_open":
            list_depth += 1
            ordered.append(int(t.attrGet("start") or 1))
        elif t.type in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1
            ordered.pop()
        elif t.type == "blockquote_open":
            quote += 1
        elif t.type == "blockquote_close":
            quote -= 1
        elif t.type == "inline":
            markup = _inline(t.children or [])
            if pending_heading:
                story.append(Paragraph(markup, heading[pending_heading]))
                pending_heading = None
            elif list_depth and i >= 2 and tokens[i - 2].type == "list_item_open":
                n = ordered[-1]
                bullet = f"{n}." if n is not None else "•"
                if n is not None:
                    ordered[-1] = n + 1
                style = styles["Normal"].clone("li", leftIndent=18 * list_depth, bulletIndent=18 * list_depth - 12, spaceAfter=2)
                story.append(Paragraph(markup, style, bulletText=bullet))
            else:
                style = styles["BodyText"] if not quote else styles["BodyText"].clone("quote", leftIndent=18, textColor="#444444")
                story.append(Paragraph(markup, style))
        elif t.type in ("fence", "code_block"):
            story.append(Preformatted(t.content.rstrip("\n"), styles["Code"]))
        elif t.type == "hr":
            story.append(HRFlowable(width="100%"))
        elif t.type == "paragraph_close" and not list_depth:
            story.append(Spacer(1, 2))
    if not story:
        story.append(Paragraph("", styles["BodyText"]))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, title=title, author="Open Personal Agent", creator="Open Personal Agent",
                            leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch, invariant=1)
    doc.build(story)
    return buf.getvalue()
