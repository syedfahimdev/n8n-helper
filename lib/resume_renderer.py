"""Convert a tailored resume's markdown to a styled HTML document.

The HTML is the *complete* artifact passed to Gotenberg — full <html>, inline
<style>, the same template (Calibri, navy section headers, 2-page layout)
used in the manually-built resumes.
"""
from __future__ import annotations

import html
import re

CSS = r"""
@page { size: Letter; margin: 0.4in 0.5in; }
* { box-sizing: border-box; }
html, body {
  font-family: "Calibri", "Helvetica Neue", "Helvetica", "Arial", sans-serif;
  font-size: 10pt; line-height: 1.32; color: #1c1c1c; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.header { text-align: center; margin-bottom: 4px; }
.header .name {
  font-size: 22pt; font-weight: 700; margin: 0; color: #102a43; text-transform: uppercase;
}
.header .contact { font-size: 9.2pt; color: #334155; margin: 2px 0 0; }
h2.section {
  font-size: 10.5pt; font-weight: 700; text-transform: uppercase; color: #102a43;
  border-bottom: 0.75px solid #b8c2cc; padding-bottom: 1px; margin: 8px 0 3px;
}
h3.entry { font-size: 10.5pt; font-weight: 700; color: #1a202c; margin: 5px 0 0; }
.entry-meta { font-size: 9.2pt; font-style: italic; color: #4a5568; margin: 0 0 2px; }
p { margin: 2px 0; }
ul { margin: 2px 0 4px 0; padding-left: 15px; }
li { margin: 1px 0; }
strong { color: #1a202c; }
"""


def _md_inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def _render_body(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_ul = False
    name_done = False
    i = 0

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            close_ul()
            i += 1
            continue

        if line.startswith("# ") and not name_done:
            close_ul()
            name = line[2:].strip()
            out.append('<div class="header">')
            out.append(f'<div class="name">{_md_inline(name)}</div>')
            i += 1
            contact_lines = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("#"):
                contact_lines.append(lines[i].strip())
                i += 1
            if contact_lines:
                contact = " &nbsp;|&nbsp; ".join(_md_inline(c) for c in contact_lines)
                out.append(f'<div class="contact">{contact}</div>')
            out.append("</div>")
            name_done = True
            continue

        if line.startswith("## "):
            close_ul()
            out.append(f'<h2 class="section">{_md_inline(line[3:].strip())}</h2>')
            i += 1
            continue

        if line.startswith("### "):
            close_ul()
            out.append(f'<h3 class="entry">{_md_inline(line[4:].strip())}</h3>')
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].lstrip().startswith("**"):
                out.append(f'<p class="entry-meta">{_md_inline(lines[j].strip())}</p>')
                i = j + 1
                continue
            i += 1
            continue

        if line.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_md_inline(line[2:].strip())}</li>")
            i += 1
            continue

        m = re.match(r"\*\*([^*]+):\*\*\s*(.+)$", line)
        if m:
            close_ul()
            out.append(
                f'<p><strong>{html.escape(m.group(1))}:</strong> {_md_inline(m.group(2))}</p>'
            )
            i += 1
            continue

        close_ul()
        out.append(f"<p>{_md_inline(line)}</p>")
        i += 1

    close_ul()
    return "\n".join(out)


def render(md: str, doc_title: str) -> str:
    body = _render_body(md)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(doc_title)}</title>"
        f"<style>{CSS}</style></head><body>\n{body}\n</body></html>"
    )
