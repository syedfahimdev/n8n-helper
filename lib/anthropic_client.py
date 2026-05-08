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
