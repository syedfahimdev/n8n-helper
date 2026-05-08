# Telegram Job Scorer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Telegram URL → ATS-score → tailor-on-approval → polished PDF flow described in the 2026-05-08 design spec.

**Architecture:** Telegram → n8n (deterministic Switch routing) → n8n-helper MCP on Vercel (tools layer) → Supabase (Storage + Postgres) for state and artifacts; Gotenberg Docker container alongside n8n for HTML→PDF.

**Tech Stack:** Python 3.12, FastMCP 3.2, httpx, PyMuPDF, Supabase Python client, anthropic, n8n self-hosted (https://n8n.srv1445761.hstgr.cloud), Gotenberg Docker image, Vercel Functions (Fluid Compute), pytest.

**Spec:** `docs/superpowers/specs/2026-05-08-tg-job-scorer-design.md`

---

## File Structure

New files in `n8n-helper/`:

| Path | Responsibility |
|---|---|
| `tools/resume.py` | `upload_master_resume`, `get_master_resume_status` MCP tools (single owner of master text) |
| `tools/scoring.py` | `score_job_url` MCP tool (Jina fetch + deterministic ATS score) |
| `tools/tailor.py` | `tailor_resume_for_jd`, `ai_recruiter_take` MCP tools (LLM calls) |
| `tools/storage.py` | `list_tailored_resumes` MCP tool + Supabase Storage client used by other tools |
| `lib/supabase_client.py` | Singleton Supabase client (Storage + Postgres). Imported by tool modules. |
| `lib/scoring_algo.py` | Pure-function ATS scorer. Adapted from `score_resume.py`. No I/O. Easy to unit-test. |
| `lib/jina_fetcher.py` | Single async function `fetch_jd(url)`. In-process 5-min LRU cache. |
| `lib/resume_renderer.py` | Markdown → HTML using existing template (Calibri, navy headers). No PDF — n8n calls Gotenberg. |
| `lib/anthropic_client.py` | Lazy-initialized Anthropic client + the tailoring/recruiter prompts. |
| `db/migrations/001_initial.sql` | Postgres schema: `master_resume`, `pending_tailor_offers`, indexes, pg_cron expiry job. |
| `tests/unit/test_scoring_algo.py` | Pure scorer tests with hand-crafted JD/resume pairs. |
| `tests/unit/test_resume_renderer.py` | MD → HTML structural tests. |
| `tests/unit/test_jina_fetcher.py` | Cache behavior + URL validation tests. |
| `tests/integration/test_resume_tools.py` | Mocks Supabase + httpx, tests upload + status tools. |
| `tests/integration/test_scoring_tools.py` | Mocks Jina + Postgres, tests score_job_url envelope. |
| `tests/integration/test_tailor_tools.py` | Mocks Anthropic + Jina, tests HTML structure + predicted_score. |
| `n8n/workflow.json` | Exported deterministic workflow JSON (committed for review/diff). |
| `docs/smoke-tests.md` | Manual post-deploy checklist. |

Modified files:
- `server.py` — register the new tool modules in `register_tools()`.
- `requirements.txt` — add `supabase>=2.5`, `anthropic>=0.40`, `pymupdf>=1.27`, `markdown>=3.6`.
- `.env.example` — add `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `GOTENBERG_URL`.
- `vercel.json` — add `maxDuration: 60` for the index function (LLM tailoring can take ~30s).
- `README.md` — add "Built-in tools" entries and a Supabase + Gotenberg setup section.

---

## Task 1: Supabase project + database schema

**Files:**
- Create: `db/migrations/001_initial.sql`
- Modify: `.env.example`

- [ ] **Step 1: Create a free Supabase project (manual)**

Go to https://supabase.com → New project → name "n8n-helper" → save the project ref.
Save these values to `/root/.local/n8n-helper/n8n.env` (NOT the repo):

```
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_KEY=<service_role key from project settings>
```

- [ ] **Step 2: Add the schema migration file**

Create `db/migrations/001_initial.sql`:

```sql
-- Single-row table holding the master resume's extracted text.
CREATE TABLE IF NOT EXISTS master_resume (
  id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  text        text NOT NULL,
  pdf_path    text NOT NULL DEFAULT 'resumes/master.pdf',
  uploaded_at timestamptz NOT NULL DEFAULT now()
);

-- Pending tailor offers — pre-approved jobs awaiting the user's button tap.
CREATE TABLE IF NOT EXISTS pending_tailor_offers (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id        text NOT NULL,
  url            text NOT NULL,
  score          real NOT NULL,
  missing_terms  text[] NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT (now() + interval '1 hour'),
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','expired'))
);

CREATE INDEX IF NOT EXISTS idx_pending_offers_chat_recent
  ON pending_tailor_offers (chat_id, created_at DESC);

-- pg_cron daily cleanup of long-expired rows.
SELECT cron.schedule(
  'tailor-offers-cleanup',
  '0 4 * * *',
  $$DELETE FROM pending_tailor_offers WHERE expires_at < now() - interval '1 day'$$
);
```

- [ ] **Step 3: Apply the migration via Supabase SQL editor**

Open the project's SQL editor → paste `001_initial.sql` → Run. Verify with:

```sql
SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
-- expect: master_resume, pending_tailor_offers
```

- [ ] **Step 4: Create Storage bucket "resumes" (private)**

Storage → New bucket → name `resumes` → public **off** → save.

- [ ] **Step 5: Update .env.example**

Append to `.env.example`:

```
# --- Supabase ---
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=replace-with-service-role-key

# --- LLM ---
ANTHROPIC_API_KEY=
ANTHROPIC_TAILOR_MODEL=claude-haiku-4-5-20251001

# --- PDF rendering ---
GOTENBERG_URL=http://gotenberg:3000
```

- [ ] **Step 6: Commit**

```bash
git add db/migrations/001_initial.sql .env.example
git commit -m "feat: supabase schema for master resume + pending offers"
```

---

## Task 2: Singleton Supabase client

**Files:**
- Create: `lib/__init__.py`
- Create: `lib/supabase_client.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add supabase to requirements.txt**

Append:

```
supabase>=2.5
```

Then `pip install supabase==2.5.0` inside `.venv`.

- [ ] **Step 2: Write the client module**

Create `lib/__init__.py` (empty file — makes `lib` a package).

Create `lib/supabase_client.py`:

```python
"""Singleton Supabase client. Imported by every tool that needs Storage or Postgres.

The client is lazy: it's not built until first use, so unit tests that don't
need real Supabase can monkeypatch the factory without paying connection cost.
"""
from __future__ import annotations

import os
from functools import lru_cache

from supabase import Client, create_client


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)
```

- [ ] **Step 3: Manual smoke check**

Run interactively:

```bash
source .venv/bin/activate
source /root/.local/n8n-helper/n8n.env
python -c "
from lib.supabase_client import get_client
c = get_client()
res = c.table('master_resume').select('id').execute()
print('rows:', res.data)  # expect [] (table empty)
"
```

- [ ] **Step 4: Commit**

```bash
git add lib/ requirements.txt
git commit -m "feat: lazy Supabase client (Storage + Postgres)"
```

---

## Task 3: Pure ATS scoring algorithm

**Files:**
- Create: `lib/scoring_algo.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_scoring_algo.py`

- [ ] **Step 1: Write failing test for `score(jd_text, resume_text)` envelope**

Create `tests/__init__.py` and `tests/unit/__init__.py` (empty).

Create `tests/unit/test_scoring_algo.py`:

```python
from lib.scoring_algo import score


def test_perfect_match_returns_high_score():
    jd = "Customer Success Manager with QBR, NRR, and SaaS experience."
    resume = "Customer Success Manager with QBR, NRR, and SaaS experience."
    result = score(jd_text=jd, resume_text=resume)
    assert result["score"] >= 95
    assert result["verdict"] == "apply"
    assert result["missing_terms"] == []


def test_zero_overlap_returns_low_score():
    jd = "Senior Backend Engineer in Rust working on GPU kernels."
    resume = "Pastry chef with 10 years of experience in French cuisine."
    result = score(jd_text=jd, resume_text=resume)
    assert result["score"] < 30
    assert result["verdict"] == "stretch"


def test_borderline_match_yields_tailor_verdict():
    jd = "Customer Success Manager. SaaS. Renewals. NRR. APIs. Python. FastAPI."
    resume = "Customer Success Manager experienced in renewals and APIs."
    result = score(jd_text=jd, resume_text=resume)
    assert 65 <= result["score"] < 90
    assert result["verdict"] == "tailor"
    assert "saas" in [t.lower() for t in result["missing_terms"]]


def test_envelope_shape():
    result = score(jd_text="Python SaaS", resume_text="Python")
    assert set(result.keys()) >= {"score", "verdict", "missing_terms"}
    assert isinstance(result["missing_terms"], list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_scoring_algo.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'lib.scoring_algo'"

- [ ] **Step 3: Implement the scorer**

Create `lib/scoring_algo.py`:

```python
"""Pure-function ATS scorer.

Computes a Jobscan-style coverage percentage of the JD's vocabulary in the
resume. No I/O, no async, no external services — everything in this module
should be unit-testable with hand-crafted strings.

The scorer collapses whitespace before matching so multi-word phrases broken
across lines (common after PDF text extraction) still match.
"""
from __future__ import annotations

import re
from collections import Counter

# Stopwords + JD scaffolding that shouldn't count toward the match.
_STOPWORDS = frozenset(
    "a an and the of for with in to from on at by as is are was were be been being "
    "have has had do does did this that these those we you your our their his her "
    "they them about will can may must should would could into across over under "
    "experience experienced work working ability able strong proven required preferred "
    "responsibilities qualifications role team teams company".split()
)

_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z\-\.]*[a-zA-Z]|[a-zA-Z]")
_PHRASE_TARGETS = {
    "customer success", "account management", "voice of the customer",
    "quarterly business review", "net revenue retention", "time-to-value",
    "fastapi", "next.js", "google cloud", "high agency",
}

_APPLY = 90
_TAILOR = 65


def _tokens(text: str) -> set[str]:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return {t for t in _TOKEN.findall(text) if len(t) >= 3 and t not in _STOPWORDS}


def _phrase_hits(text: str, targets: set[str]) -> set[str]:
    text_l = re.sub(r"\s+", " ", text.lower())
    return {p for p in targets if p in text_l}


def score(jd_text: str, resume_text: str) -> dict:
    jd_tokens = _tokens(jd_text)
    resume_tokens = _tokens(resume_text)

    # Single-word coverage.
    matched_words = jd_tokens & resume_tokens
    word_coverage = len(matched_words) / max(1, len(jd_tokens))

    # Multi-word phrase boost — phrases that appear in JD AND resume add weight.
    jd_phrases = _phrase_hits(jd_text, _PHRASE_TARGETS)
    matched_phrases = jd_phrases & _phrase_hits(resume_text, jd_phrases)
    phrase_coverage = (
        len(matched_phrases) / max(1, len(jd_phrases)) if jd_phrases else 1.0
    )

    score_pct = round(100 * (0.7 * word_coverage + 0.3 * phrase_coverage), 1)

    if score_pct >= _APPLY:
        verdict = "apply"
    elif score_pct >= _TAILOR:
        verdict = "tailor"
    else:
        verdict = "stretch"

    missing = sorted(jd_tokens - resume_tokens)[:5]

    return {
        "score": score_pct,
        "verdict": verdict,
        "missing_terms": missing,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_scoring_algo.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/scoring_algo.py tests/
git commit -m "feat: pure ATS scoring algorithm with tests"
```

---

## Task 4: Jina Reader fetcher with cache

**Files:**
- Create: `lib/jina_fetcher.py`
- Create: `tests/unit/test_jina_fetcher.py`

- [ ] **Step 1: Write failing test for URL validation**

Create `tests/unit/test_jina_fetcher.py`:

```python
import pytest

from lib.jina_fetcher import fetch_jd, _build_jina_url


def test_build_jina_url_prefixes_correctly():
    assert (
        _build_jina_url("https://example.com/job/123")
        == "https://r.jina.ai/https://example.com/job/123"
    )


def test_rejects_non_http_url():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(fetch_jd("ftp://example.com"))


def test_rejects_missing_scheme():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(fetch_jd("example.com"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_jina_fetcher.py -v`
Expected: FAIL with "ModuleNotFoundError".

- [ ] **Step 3: Implement the fetcher**

Create `lib/jina_fetcher.py`:

```python
"""Fetch JD text via Jina Reader.

Jina Reader (https://r.jina.ai/<url>) renders JS-heavy pages server-side and
returns clean markdown — works on Ashby/Greenhouse/Workday where a plain HTTP
GET only sees a `<div id="root"></div>` shell.

Includes a 5-minute in-process cache so repeat calls (e.g. score → tailor →
re-score) on the same URL within a single warm Vercel instance don't refetch.
"""
from __future__ import annotations

import time
from typing import Tuple

import httpx

_TTL_SECONDS = 300
_cache: dict[str, Tuple[float, str]] = {}


def _build_jina_url(url: str) -> str:
    return f"https://r.jina.ai/{url}"


async def fetch_jd(url: str, timeout: float = 25.0) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Only http(s) URLs are accepted; got {url!r}")

    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(_build_jina_url(url))
        r.raise_for_status()
        text = r.text

    _cache[url] = (now, text)
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_jina_fetcher.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/jina_fetcher.py tests/unit/test_jina_fetcher.py
git commit -m "feat: Jina Reader fetcher with 5-min in-process cache"
```

---

## Task 5: Resume renderer (MD → HTML)

**Files:**
- Create: `lib/resume_renderer.py`
- Create: `tests/unit/test_resume_renderer.py`
- Modify: `requirements.txt` (add `markdown`)

- [ ] **Step 1: Add markdown to requirements.txt**

Append `markdown>=3.6`. Then `pip install markdown==3.6.0` in `.venv`.

- [ ] **Step 2: Write failing test for HTML structure**

Create `tests/unit/test_resume_renderer.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_resume_renderer.py -v`
Expected: FAIL with module-not-found.

- [ ] **Step 4: Implement the renderer**

Create `lib/resume_renderer.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_resume_renderer.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add lib/resume_renderer.py tests/unit/test_resume_renderer.py requirements.txt
git commit -m "feat: markdown → HTML resume renderer (Gotenberg input)"
```

---

## Task 6: `upload_master_resume` and `get_master_resume_status` tools

**Files:**
- Create: `tools/resume.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_resume_tools.py`
- Modify: `requirements.txt` (add `pymupdf`)
- Modify: `server.py`

- [ ] **Step 1: Add pymupdf to requirements.txt**

Append `pymupdf>=1.27`. Then `pip install pymupdf==1.27.2` in `.venv`.

- [ ] **Step 2: Write failing integration test**

Create `tests/integration/__init__.py` (empty).

Create `tests/integration/test_resume_tools.py`:

```python
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_supabase(monkeypatch):
    client = MagicMock()
    table = client.table.return_value
    storage = client.storage.from_.return_value
    monkeypatch.setattr("lib.supabase_client.get_client", lambda: client)
    return client, table, storage


def test_status_returns_not_exists_when_table_empty(mock_supabase):
    client, table, _ = mock_supabase
    table.select.return_value.execute.return_value.data = []
    from tools.resume import _status_impl

    assert _status_impl()["exists"] is False


def test_upload_extracts_text_and_writes_storage_and_table(mock_supabase, tmp_path):
    client, table, storage = mock_supabase
    # Pre-write a tiny PDF locally to avoid needing a real network call.
    import fitz
    pdf_path = tmp_path / "r.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), "Customer Success Manager\nQBR NRR SaaS")
    doc.save(pdf_path)
    doc.close()

    table.upsert.return_value.execute.return_value.data = [{"id": 1}]
    storage.upload.return_value = MagicMock()

    from tools.resume import _upload_impl

    with patch("tools.resume._download_pdf", return_value=pdf_path.read_bytes()):
        result = _upload_impl(pdf_url="https://example/file.pdf")

    assert result["ok"] is True
    assert result["length"] > 0
    storage.upload.assert_called_once()
    table.upsert.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_resume_tools.py -v`
Expected: FAIL with `tools.resume` import error.

- [ ] **Step 4: Implement the tools**

Create `tools/resume.py`:

```python
"""Master-resume management tools.

`upload_master_resume` is the only writer of the master text — it pulls a PDF
from a (Telegram-hosted) URL, extracts the text, stores both the PDF blob in
Supabase Storage and the extracted text in Postgres so other tools (`score_*`,
`tailor_*`) can read the text without re-parsing the PDF every call.
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF
import httpx
from fastmcp import FastMCP

from lib.supabase_client import get_client


def _download_pdf(url: str) -> bytes:
    with httpx.Client(timeout=20.0, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def _extract_text(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc).strip()
    finally:
        doc.close()


def _upload_impl(pdf_url: str) -> dict:
    pdf_bytes = _download_pdf(pdf_url)
    text = _extract_text(pdf_bytes)
    if not text:
        raise ValueError(
            "PDF has no extractable text (image-only PDF). "
            "Upload a text-based PDF."
        )

    sb = get_client()
    storage = sb.storage.from_("resumes")
    storage.upload(
        path="master.pdf",
        file=pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )

    sb.table("master_resume").upsert(
        {"id": 1, "text": text, "pdf_path": "resumes/master.pdf"}
    ).execute()

    return {"ok": True, "length": len(text), "replaced_previous": True}


def _status_impl() -> dict:
    sb = get_client()
    res = sb.table("master_resume").select("uploaded_at,text").limit(1).execute()
    if not res.data:
        return {"exists": False}
    row = res.data[0]
    return {
        "exists": True,
        "uploaded_at": row.get("uploaded_at"),
        "length": len(row.get("text", "")),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def upload_master_resume(pdf_url: str) -> dict:
        """Save an uploaded PDF as the master resume, extracting its text.

        Use this once at setup time and again whenever you want to refresh
        the master copy. Subsequent score / tailor tools read the cached
        text from Postgres, not the PDF.

        Args:
            pdf_url: Absolute http(s) URL to the PDF — typically a
                Telegram-hosted file URL retrieved via Telegram's `getFile`
                API.

        Returns:
            A dict shaped { ok: bool, length: int, replaced_previous: bool }.
            Raises ValueError if the PDF contains no extractable text
            (image-only PDFs are rejected).
        """
        return _upload_impl(pdf_url)

    @mcp.tool
    def get_master_resume_status() -> dict:
        """Cheap status check — returns whether a master resume is on file.

        Other flows call this before assuming a master exists. Used by
        n8n to short-circuit the score flow if the user hasn't uploaded
        anything yet.

        Returns:
            { exists: bool, uploaded_at?: str, length?: int }.
        """
        return _status_impl()
```

- [ ] **Step 5: Register the tools in server.py**

Edit `server.py`'s `register_tools()`:

```python
def register_tools(mcp: FastMCP) -> None:
    from tools import discovery, health, resume

    discovery.register(mcp)
    health.register(mcp)
    resume.register(mcp)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/integration/test_resume_tools.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add tools/resume.py tests/integration/ server.py requirements.txt
git commit -m "feat: upload_master_resume and get_master_resume_status tools"
```

---

## Task 7: `score_job_url` tool

**Files:**
- Create: `tools/scoring.py`
- Create: `tests/integration/test_scoring_tools.py`
- Modify: `server.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_scoring_tools.py`:

```python
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_supabase(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("lib.supabase_client.get_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_score_job_url_returns_envelope(mock_supabase):
    client = mock_supabase
    client.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
        {"text": "Customer Success Manager. SaaS. QBR. NRR. APIs. Python."}
    ]

    fake_jd = "Customer Success Manager. SaaS. QBR. NRR. APIs. Python."

    from tools.scoring import _score_url_impl

    with patch("tools.scoring.fetch_jd", return_value=fake_jd):
        result = await _score_url_impl("https://example.com/job")

    assert "score" in result
    assert "verdict" in result
    assert "missing_terms" in result
    assert "fetched_url" in result
    assert result["fetched_url"] == "https://example.com/job"


@pytest.mark.asyncio
async def test_score_job_url_no_master_raises(mock_supabase):
    client = mock_supabase
    client.table.return_value.select.return_value.limit.return_value.execute.return_value.data = []

    from tools.scoring import _score_url_impl

    with patch("tools.scoring.fetch_jd", return_value="some jd"):
        with pytest.raises(ValueError, match="master resume"):
            await _score_url_impl("https://example.com/job")
```

Add `pytest-asyncio>=0.23` to requirements.txt and `pip install` it. Add `tool.pytest.ini_options` with `asyncio_mode = "auto"` in a new `pyproject.toml` at repo root if not present (one-line).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_scoring_tools.py -v`
Expected: FAIL with module import error.

- [ ] **Step 3: Implement the tool**

Create `tools/scoring.py`:

```python
"""Score a job URL against the stored master resume.

The deterministic side of the system. No LLM. Fetches via Jina, loads master
text from Postgres, runs the pure scoring algorithm, returns the envelope
n8n routes on.
"""
from __future__ import annotations

import re

from fastmcp import FastMCP

from lib.jina_fetcher import fetch_jd
from lib.scoring_algo import score
from lib.supabase_client import get_client


def _extract_title_and_company(jd_text: str) -> tuple[str, str]:
    """Best-effort title/company extraction from Jina-rendered markdown.

    Jina puts the page title on the first non-blank line. If that line matches
    the pattern "Job Title @ Company" or "Job Title — Company", we split.
    Otherwise we return the whole title and an empty company.
    """
    for line in jd_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if " @ " in line:
            t, c = line.split(" @ ", 1)
            return t.strip(), c.strip()
        if " — " in line:
            t, c = line.split(" — ", 1)
            return t.strip(), c.strip()
        return line, ""
    return "", ""


async def _score_url_impl(url: str) -> dict:
    sb = get_client()
    res = sb.table("master_resume").select("text").limit(1).execute()
    if not res.data:
        raise ValueError(
            "No master resume on file. Upload one first via "
            "upload_master_resume."
        )
    resume_text = res.data[0]["text"]

    jd_text = await fetch_jd(url)
    s = score(jd_text=jd_text, resume_text=resume_text)
    title, company = _extract_title_and_company(jd_text)

    return {
        **s,
        "jd_title": title,
        "company": company,
        "fetched_url": url,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def score_job_url(url: str) -> dict:
        """Fetch a job posting URL and score the master resume against it.

        Returns a deterministic ATS-style match score, the verdict band
        n8n routes on, and the top missing keywords. No LLM call.

        Args:
            url: Absolute http(s) URL of the job posting. Works for any
                page Jina Reader can render — Ashby, Greenhouse, Workday,
                most company career pages.

        Returns:
            { score: float, verdict: "apply"|"tailor"|"stretch",
              missing_terms: list[str], jd_title: str, company: str,
              fetched_url: str }. Verdict bands: ≥90 apply, 65-89 tailor,
            <65 stretch.
        """
        return await _score_url_impl(url)
```

- [ ] **Step 4: Register the tool in server.py**

Add `scoring.register(mcp)` in `register_tools()`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_scoring_tools.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add tools/scoring.py tests/integration/test_scoring_tools.py server.py requirements.txt
git commit -m "feat: score_job_url tool with deterministic ATS scoring"
```

---

## Task 8: Anthropic client + tailoring prompt

**Files:**
- Create: `lib/anthropic_client.py`
- Modify: `requirements.txt` (add `anthropic`)

- [ ] **Step 1: Add anthropic to requirements.txt**

Append `anthropic>=0.40`. Then `pip install anthropic==0.40.0` in `.venv`.

- [ ] **Step 2: Implement the client + prompt builders**

Create `lib/anthropic_client.py`:

```python
"""Lazy-initialized Anthropic client and the prompt templates we send.

The client is a function call (`client()`) rather than a module-level object
so importing this module doesn't blow up when ANTHROPIC_API_KEY is unset
(e.g. during scoring-only tests).
"""
from __future__ import annotations

import os
from functools import lru_cache

import anthropic


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    key = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(api_key=key)


TAILOR_SYSTEM = """You tailor a Customer Success / TAM / SE-style resume to a specific job description.

Rules — non-negotiable:
1. Do not invent, fabricate, or imply experience the master does not contain.
   You may rephrase, reorder, and emphasize. You may NOT add new employers,
   credentials, dates, or numbers.
2. Output only the markdown of the tailored resume — no commentary, no
   preamble, no closing remarks.
3. Preserve the structure: header, summary, core competencies / skills,
   experience, projects, education, certifications.
4. Mirror the JD's exact phrasing for technical terms (FastAPI not Fast API,
   Next.js not NextJS, QBR not Quarterly Business Reviews unless both appear).
5. Keep length to two pages of dense text.
6. Update the headline (line 3 after the contact block) to mirror the JD's
   role title.
"""


def build_tailor_user_msg(master_md: str, jd_text: str) -> str:
    return (
        "## Master resume (markdown — do NOT add experience to this):\n\n"
        f"{master_md}\n\n"
        "## Job description:\n\n"
        f"{jd_text}\n\n"
        "Return only the tailored resume markdown."
    )


RECRUITER_SYSTEM = """You are a senior tech recruiter judging fit between a candidate and a job.

Rules:
- One-line verdict from {strong, borderline, stretch}.
- One-sentence reason. No more.
- Do not summarize the resume or the JD. Just the verdict and the reason.

Output format (single line, no markdown):
verdict: <strong|borderline|stretch> - <one-sentence reason>
"""


def build_recruiter_user_msg(master_md: str, jd_text: str) -> str:
    return (
        "## Resume:\n\n"
        f"{master_md}\n\n"
        "## Job description:\n\n"
        f"{jd_text}"
    )


def call(model: str, system: str, user_msg: str, max_tokens: int = 4096) -> str:
    resp = client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if hasattr(b, "text"))
```

- [ ] **Step 3: Commit**

```bash
git add lib/anthropic_client.py requirements.txt
git commit -m "feat: lazy Anthropic client + tailor and recruiter prompts"
```

---

## Task 9: `tailor_resume_for_jd` tool

**Files:**
- Create: `tools/tailor.py`
- Create: `tests/integration/test_tailor_tools.py`
- Modify: `server.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_tailor_tools.py`:

```python
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_supabase(monkeypatch):
    client = MagicMock()
    client.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
        {"text": "# JANE\nCSM with Python.\n\n## EXPERIENCE\n\n### CSM — Acme\n**2020 – Present**\n\n- Did stuff"}
    ]
    monkeypatch.setattr("lib.supabase_client.get_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_tailor_returns_html_and_predicted_score(mock_supabase, monkeypatch):
    fake_jd = "Senior Customer Success Manager — SaaS. Python. APIs. NRR."
    fake_md = (
        "# JANE\nCSM with Python and SaaS.\n\n## SUMMARY\nCustomer Success Manager.\n\n"
        "## EXPERIENCE\n\n### CSM — Acme\n**2020 – Present**\n\n- SaaS, NRR, APIs, Python."
    )

    monkeypatch.setattr("tools.tailor.fetch_jd", lambda url: _async(fake_jd))
    monkeypatch.setattr("tools.tailor.call_llm", lambda *a, **kw: fake_md)

    from tools.tailor import _tailor_impl

    result = await _tailor_impl("https://example.com/job/csm")

    assert "html" in result
    assert "<title>" in result["html"]
    assert "JANE" in result["html"]
    assert "predicted_score" in result
    assert "jd_slug" in result
    assert result["jd_slug"]


async def _async(v):
    return v
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_tailor_tools.py -v`
Expected: FAIL with module import error.

- [ ] **Step 3: Implement the tool**

Create `tools/tailor.py`:

```python
"""LLM-tailor the master resume to a specific JD and prepare it for PDF
rendering by Gotenberg (called from n8n, not from this tool).

Returns HTML + predicted score + a deterministic slug (used by the caller to
build the storage filename). The tailor tool does NOT write to Supabase
Storage — that's n8n's job after Gotenberg has produced the PDF.
"""
from __future__ import annotations

import os
import re

from fastmcp import FastMCP

from lib.anthropic_client import (
    TAILOR_SYSTEM,
    build_tailor_user_msg,
    call as call_llm,
)
from lib.jina_fetcher import fetch_jd
from lib.resume_renderer import render
from lib.scoring_algo import score
from lib.supabase_client import get_client


_MODEL_DEFAULT = "claude-haiku-4-5-20251001"


def _slugify(s: str, max_len: int = 32) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "tailored"


def _derive_slug(jd_text: str) -> str:
    first = next((ln.strip() for ln in jd_text.splitlines() if ln.strip()), "")
    if " @ " in first:
        t, c = first.split(" @ ", 1)
        return _slugify(f"{c}-{t}")
    return _slugify(first)


async def _tailor_impl(url: str) -> dict:
    sb = get_client()
    res = sb.table("master_resume").select("text").limit(1).execute()
    if not res.data:
        raise ValueError("No master resume on file.")
    master_md = res.data[0]["text"]

    jd_text = await fetch_jd(url)
    model = os.getenv("ANTHROPIC_TAILOR_MODEL", _MODEL_DEFAULT)
    tailored_md = call_llm(
        model=model,
        system=TAILOR_SYSTEM,
        user_msg=build_tailor_user_msg(master_md, jd_text),
        max_tokens=4096,
    ).strip()

    if not tailored_md.startswith("#"):
        # Defensive: model occasionally returns code-fenced output despite
        # instructions. Strip ```markdown ... ``` envelopes if present.
        tailored_md = re.sub(r"^```(?:markdown)?\s*", "", tailored_md)
        tailored_md = re.sub(r"\s*```$", "", tailored_md)

    predicted = score(jd_text=jd_text, resume_text=tailored_md)
    slug = _derive_slug(jd_text)
    title = next((ln.strip("# ").strip() for ln in tailored_md.splitlines() if ln.startswith("#")), "Resume")
    html = render(tailored_md, doc_title=title)

    return {
        "html": html,
        "jd_slug": slug,
        "predicted_score": predicted["score"],
        "missing_after": predicted["missing_terms"],
        "tailored_md": tailored_md,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def tailor_resume_for_jd(url: str) -> dict:
        """LLM-tailor the master resume to a specific JD and return HTML + predicted score.

        The caller (n8n) is expected to:
        1. POST the returned `html` to Gotenberg to render a PDF.
        2. Upload the PDF to Supabase Storage using the returned `jd_slug`.

        Args:
            url: Absolute http(s) URL of the job posting.

        Returns:
            { html: str, jd_slug: str, predicted_score: float,
              missing_after: list[str], tailored_md: str }. The HTML is the
            full rendered document (Calibri-style template, navy headers).
        """
        return await _tailor_impl(url)
```

- [ ] **Step 4: Register the tool in server.py**

Add `tailor.register(mcp)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_tailor_tools.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add tools/tailor.py tests/integration/test_tailor_tools.py server.py
git commit -m "feat: tailor_resume_for_jd tool (LLM tailor → HTML + predicted score)"
```

---

## Task 10: `ai_recruiter_take` and `list_tailored_resumes` tools

**Files:**
- Modify: `tools/tailor.py` (add second tool)
- Create: `tools/storage.py`
- Modify: `server.py`

- [ ] **Step 1: Add ai_recruiter_take to tools/tailor.py**

Append to `tools/tailor.py` (inside `register`):

```python
    @mcp.tool
    async def ai_recruiter_take(url: str) -> dict:
        """Holistic LLM judgment of fit, separate from ATS scoring.

        Use this when the deterministic score is borderline and you want a
        recruiter-style read. Returns one verdict + one-sentence reason.

        Args:
            url: Absolute http(s) URL of the job posting.

        Returns:
            { verdict: "strong"|"borderline"|"stretch", why: str }.
        """
        from lib.anthropic_client import (
            RECRUITER_SYSTEM,
            build_recruiter_user_msg,
            call as call_llm,
        )

        sb = get_client()
        res = sb.table("master_resume").select("text").limit(1).execute()
        if not res.data:
            raise ValueError("No master resume on file.")
        master_md = res.data[0]["text"]

        jd_text = await fetch_jd(url)
        model = os.getenv("ANTHROPIC_RECRUITER_MODEL", _MODEL_DEFAULT)
        text = call_llm(
            model=model,
            system=RECRUITER_SYSTEM,
            user_msg=build_recruiter_user_msg(master_md, jd_text),
            max_tokens=200,
        ).strip()

        # Output contract: "verdict: <kind> - <reason>"
        m = re.match(
            r"verdict:\s*(strong|borderline|stretch)\s*[-—]\s*(.+)$",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return {"verdict": "borderline", "why": text}
        return {"verdict": m.group(1).lower(), "why": m.group(2).strip()}
```

- [ ] **Step 2: Create tools/storage.py**

Create `tools/storage.py`:

```python
"""Read-side Supabase Storage helpers exposed as MCP tools.

Write-side operations (uploading the master, uploading a tailored copy) live
in their respective tools — `tools.resume` writes the master, n8n writes the
tailored copy directly via the Supabase REST API after Gotenberg renders the
PDF. This module is read-only.
"""
from __future__ import annotations

from fastmcp import FastMCP

from lib.supabase_client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def list_tailored_resumes(limit: int = 10) -> list[dict]:
        """List recent tailored resume copies in Supabase Storage.

        Args:
            limit: Max number of files to return. Defaults to 10.

        Returns:
            list of { filename, size, created_at, signed_download_url }
            sorted newest first. The signed URL is valid for 1 hour.
        """
        sb = get_client()
        bucket = sb.storage.from_("resumes")
        files = bucket.list(
            "tailored",
            {"limit": limit, "sortBy": {"column": "created_at", "order": "desc"}},
        )
        out = []
        for f in files:
            path = f"tailored/{f['name']}"
            signed = bucket.create_signed_url(path, 3600)
            out.append(
                {
                    "filename": f["name"],
                    "size": f.get("metadata", {}).get("size"),
                    "created_at": f.get("created_at"),
                    "signed_download_url": signed.get("signedURL")
                    or signed.get("signed_url"),
                }
            )
        return out
```

- [ ] **Step 3: Register both in server.py**

Add `storage.register(mcp)`.

- [ ] **Step 4: Manual smoke check (no auto-test for storage; needs real bucket)**

Run with a tailored file uploaded manually via Supabase dashboard:

```bash
source .venv/bin/activate && source /root/.local/n8n-helper/n8n.env
python -c "
from tools.storage import register
from fastmcp import FastMCP
mcp = FastMCP('t')
register(mcp)
"
```

(Placeholder until n8n end-to-end test in Task 13.)

- [ ] **Step 5: Commit**

```bash
git add tools/tailor.py tools/storage.py server.py
git commit -m "feat: ai_recruiter_take + list_tailored_resumes tools"
```

---

## Task 11: Vercel max-duration bump + redeploy

**Files:**
- Modify: `vercel.json`

- [ ] **Step 1: Update vercel.json**

Replace contents:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "fluid": true,
  "functions": {
    "api/index.py": {
      "maxDuration": 60
    }
  }
}
```

(60s headroom for the LLM tailor call, which is the slowest path.)

- [ ] **Step 2: Add Supabase + Anthropic env vars to Vercel**

```bash
source /root/.local/n8n-helper/n8n.env
echo -n "$SUPABASE_URL" | vercel env add SUPABASE_URL production
echo -n "$SUPABASE_SERVICE_KEY" | vercel env add SUPABASE_SERVICE_KEY production
echo -n "$ANTHROPIC_API_KEY" | vercel env add ANTHROPIC_API_KEY production
```

- [ ] **Step 3: Commit + push (triggers auto-deploy)**

```bash
git add vercel.json
git commit -m "chore: vercel maxDuration 60s for LLM tailor calls"
git push origin main
```

- [ ] **Step 4: Force production promotion + verify**

```bash
vercel deploy --prod --yes
sleep 25
# Confirm new tools are live
python - <<'EOF'
import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
TOKEN = os.environ["N8N_HELPER_TOKEN"]  # use the prod token
async def main():
    t = StreamableHttpTransport(
        url="https://n8n-helper.vercel.app/mcp",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    async with Client(t) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        print("live:", names)
        expected = {
            "find_tool", "health", "upload_master_resume",
            "get_master_resume_status", "score_job_url",
            "tailor_resume_for_jd", "ai_recruiter_take",
            "list_tailored_resumes",
        }
        assert expected.issubset(set(names)), f"missing: {expected - set(names)}"
        print("✓ all 8 tools live")
asyncio.run(main())
EOF
```

Expected output: `live: [...]` with all 8 tools, then `✓ all 8 tools live`.

---

## Task 12: Gotenberg Docker container alongside n8n

**Files:** none in repo — instructional only.

- [ ] **Step 1: SSH to the n8n host and run Gotenberg**

On the user's Hostinger box (`srv1445761`):

```bash
docker run -d \
  --name gotenberg \
  --restart unless-stopped \
  -p 3001:3000 \
  gotenberg/gotenberg:8
```

- [ ] **Step 2: Smoke test from the same host**

```bash
echo '<!doctype html><html><body><h1>Hi</h1></body></html>' > /tmp/in.html
curl -sS -X POST http://localhost:3001/forms/chromium/convert/html \
  -F files=@/tmp/in.html \
  -o /tmp/out.pdf
file /tmp/out.pdf
# expect: /tmp/out.pdf: PDF document, version ...
```

- [ ] **Step 3: Confirm reachable from the n8n container**

In n8n's docker-compose network, the URL is `http://gotenberg:3000` if both
containers share a network, or `http://<host-ip>:3001` otherwise. Note
which works — n8n will use that URL in the workflow's HTTP Request node.

(No git commit — this is host config.)

---

## Task 13: Build the deterministic n8n workflow (in place at `IApjHgCVF7zQ3Oob`)

**Files:**
- Create: `n8n/workflow.json` (committed for diff/review)

- [ ] **Step 1: Dispatch via n8n-mcp**

Use `mcp__n8n-mcp__n8n_update_full_workflow` to replace the workflow at
`IApjHgCVF7zQ3Oob` with a deterministic structure. The full node JSON is
maintained in `n8n/workflow.json`. Build it node by node using the SDK
patterns from `mcp__n8n-mcp__get_sdk_reference`, then validate with
`mcp__n8n-mcp__n8n_validate_workflow` before update.

Workflow shape (high-level — exact node configs in `n8n/workflow.json`):

```
Telegram Trigger (Message)
  ↓
Switch:
  ├─ has_url     → MCP.score_job_url → Switch verdict
  │                 ├─ apply   → Telegram reply (no offer)
  │                 ├─ tailor  → Postgres INSERT pending_offer → Telegram reply with inline buttons
  │                 └─ stretch → Telegram reply (warn)
  ├─ has_pdf     → Telegram getFile → MCP.upload_master_resume → Telegram reply
  └─ /recruiter  → MCP.ai_recruiter_take → Telegram reply

Telegram Trigger (Callback Query)
  ↓
Switch on callback_data:
  └─ tailor:<uuid> → Postgres SELECT pending_offer → MCP.tailor_resume_for_jd
                     → HTTP POST to Gotenberg → Supabase Storage upload
                     → Postgres UPDATE status='approved'
                     → Telegram reply + PDF attachment
```

- [ ] **Step 2: Commit the workflow JSON**

```bash
git add n8n/workflow.json
git commit -m "feat: deterministic n8n workflow JSON (committed for diff)"
git push origin main
```

- [ ] **Step 3: Activate the workflow in n8n**

Toggle "Active" in the n8n UI for workflow `IApjHgCVF7zQ3Oob`. Verify the
Telegram trigger is registered:

```bash
source /root/.local/n8n-helper/n8n.env
curl -sS -H "X-N8N-API-KEY: $N8N_API_KEY" \
  "$N8N_BASE_URL/api/v1/workflows/IApjHgCVF7zQ3Oob" | python -c "
import json, sys
w = json.load(sys.stdin)
print('active:', w.get('active'))
print('nodes:', len(w.get('nodes', [])))
"
```

Expected: `active: True`, `nodes: ≥10`.

---

## Task 14: Smoke tests + docs

**Files:**
- Create: `docs/smoke-tests.md`
- Modify: `README.md` (add the 6 new tools to the §7 table; add Setup section pointing at db migration + Gotenberg + Supabase)

- [ ] **Step 1: Write smoke-tests.md**

Create `docs/smoke-tests.md`:

```markdown
# Smoke tests — Telegram job scorer

Run after every production deploy that touches the workflow or any of these
MCP tools: upload_master_resume, get_master_resume_status, score_job_url,
tailor_resume_for_jd, ai_recruiter_take, list_tailored_resumes.

1. **Upload master**
   - Open Telegram bot, attach `Syed_Fahim_Resume.pdf`
   - Expect: "✅ Master saved." within 8s

2. **Score — apply band**
   - Paste a known-good URL (e.g. https://www.junction.com/app/careers/...)
   - Expect: "✅ <≥90>% — strong ATS match. <Title>. Top missing: [...]"
   - No tailor offer

3. **Score — tailor band**
   - Paste a known-borderline URL (e.g. NikoHealth)
   - Expect: "⚠ <65–89>% — borderline." with [Tailor it] [Skip] buttons

4. **Score — stretch band**
   - Paste a deliberate mismatch URL
   - Expect: "🚫 <<65>% — stretch role." with [See gaps] [Skip]

5. **Tailor flow**
   - Tap [Tailor it] on a tailor-band offer
   - Within 60s: PDF attachment + "🎯 Tailored. New ATS score: <X>% (was <Y>%)."

6. **Expired offer**
   - Wait 70 minutes, tap a stale [Tailor it]
   - Expect: "Offer expired. Resend the URL."

7. **AI recruiter**
   - Send "/recruiter <url>"
   - Expect: "<strong|borderline|stretch> — <one-sentence reason>"

8. **No master fallback**
   - In a fresh DB (or after manually deleting master_resume row), paste any URL
   - Expect: "Upload your resume first."
```

- [ ] **Step 2: Update README.md §7 (Built-in tools)**

Add the 6 new rows:

```markdown
| `upload_master_resume(pdf_url)` | Save an uploaded PDF as the master resume; extract text to Postgres. |
| `get_master_resume_status()` | Cheap status check — returns whether a master is on file. |
| `score_job_url(url)` | Fetch a JD via Jina Reader and score the master resume against it. Returns score, verdict (apply/tailor/stretch), missing terms. |
| `tailor_resume_for_jd(url)` | LLM-tailor the master to a JD and return rendered HTML + predicted score. Caller renders PDF via Gotenberg. |
| `ai_recruiter_take(url)` | Holistic LLM judgment of fit; one verdict + one-sentence reason. |
| `list_tailored_resumes(limit)` | Recent tailored copies with signed download URLs. |
```

- [ ] **Step 3: Add a Setup → Supabase + Gotenberg section to the README**

Add right after §3 Quick Start:

```markdown
### 3.1 Supabase setup

1. Create a free Supabase project.
2. Apply `db/migrations/001_initial.sql` via the SQL editor.
3. Create a private Storage bucket named `resumes`.
4. Copy `SUPABASE_URL` + the service-role key into `.env`.

### 3.2 Gotenberg setup (PDF rendering)

Run alongside your n8n:

\`\`\`bash
docker run -d --name gotenberg --restart unless-stopped \\
  -p 3001:3000 gotenberg/gotenberg:8
\`\`\`

In the n8n workflow, the HTTP Request node POSTs HTML to
`http://gotenberg:3000/forms/chromium/convert/html`.
```

- [ ] **Step 4: Commit + push**

```bash
git add docs/smoke-tests.md README.md
git commit -m "docs: smoke tests + README setup instructions for v2 stack"
git push origin main
```

- [ ] **Step 5: Run all 8 smoke tests manually**

Tick each one off in `docs/smoke-tests.md` (locally — don't commit a
checked file). If any fails, file an issue with the failing step,
expected vs actual.

---

## Task 15: CI workflow

**Files:**
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Add the GitHub Actions workflow**

Create `.github/workflows/test.yml`:

```yaml
name: tests
on:
  push:
    branches: [main]
  pull_request:

jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - name: install
        run: pip install -r requirements.txt && pip install pytest pytest-asyncio
      - name: unit + integration tests
        env:
          SUPABASE_URL: http://example.com
          SUPABASE_SERVICE_KEY: dummy
          ANTHROPIC_API_KEY: dummy
          N8N_HELPER_TOKEN: dummy-token-for-tests
        run: pytest tests/ -v
```

- [ ] **Step 2: Commit + push**

```bash
git add .github/workflows/test.yml
git commit -m "ci: pytest on every push and PR"
git push origin main
```

- [ ] **Step 3: Verify on GitHub**

Open `https://github.com/syedfahimdev/n8n-helper/actions` — confirm the
run for the latest commit is green.

---

## Self-review

**Spec coverage:**
- §1 Goal — covered Tasks 6–10 (tools), Task 13 (workflow).
- §2 Locked decisions — implemented per locks (Supabase: T1+T2+T6, ATS scoring: T3+T7, thresholds: T3 verdict bands, routing T13, Gotenberg T12, inline buttons T13, PDF artifact T5+T9+T13).
- §3 Architecture — three layers built across T6–T13.
- §4 Tool catalog — all 6 new tools shipped (T6 ×2, T7 ×1, T9 ×1, T10 ×2).
- §5 Data model — T1.
- §6 Message flows — T13.
- §7 Error handling — partially covered in tool implementations (raise ValueError for missing master, code-fence stripping in tailor); n8n branches cover the user-facing error replies (T13).
- §8 Testing — unit (T3, T4, T5), integration (T6, T7, T9), smoke (T14), CI (T15).
- §9 Out of scope — not implemented (correct).
- §10 Open questions — model choice via env var (T1, T8); slug collision via timestamp suffix in n8n (T13).
- §11 Implementation target — T13 uses `n8n_update_full_workflow` against `IApjHgCVF7zQ3Oob`.
- §12 Acceptance criteria — covered by smoke tests in T14.

**Placeholder scan:** no TBD/TODO/FIXME present. Every step has the actual code or command needed.

**Type consistency:** `score(jd_text, resume_text)` keyword args used consistently across T3, T7, T9. `_score_url_impl` and `_tailor_impl` both async, both expected by their tests. `predicted_score` returned by tailor matches the field n8n reads in T13.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-08-tg-job-scorer-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
