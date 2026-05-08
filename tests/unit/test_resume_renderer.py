from lib.resume_renderer import render

SAMPLE_MD = """# JANE DOE
City • email@example.com

## SUMMARY
Customer Success Manager.

## EXPERIENCE

### CSM — Acme Corp
**Remote | 2020 – Present**

- Did stuff
- Did more stuff
"""


def test_render_returns_html_with_structure():
    html = render(SAMPLE_MD, doc_title="Jane Doe Resume")
    assert "<title>Jane Doe Resume</title>" in html
    assert "JANE DOE" in html
    assert '<h2 class="section">SUMMARY</h2>' in html
    assert '<h3 class="entry">CSM' in html
    assert "<li>Did stuff</li>" in html


def test_render_inlines_css():
    html = render(SAMPLE_MD, doc_title="x")
    assert "<style>" in html
    assert "Calibri" in html
