"""Pure-function ATS scorer.

Computes a Jobscan-style coverage percentage of the JD's vocabulary in the
resume. No I/O, no async, no external services — everything in this module
should be unit-testable with hand-crafted strings.

The scorer collapses whitespace before matching so multi-word phrases broken
across lines (common after PDF text extraction) still match.
"""
from __future__ import annotations

import re

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

    # Multi-word phrase boost — phrases in JD also present in resume add credit
    # on top of the single-word match (each matched phrase counts as one bonus
    # token of coverage).
    jd_phrases = _phrase_hits(jd_text, _PHRASE_TARGETS)
    matched_phrases = jd_phrases & _phrase_hits(resume_text, jd_phrases)

    denom = max(1, len(jd_tokens))
    coverage = min(1.0, (len(matched_words) + len(matched_phrases)) / denom)
    score_pct = round(100 * coverage, 1)

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
