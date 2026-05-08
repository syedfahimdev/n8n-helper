"""Text tools — small, reusable analysis primitives.

These exist so n8n workflows (and AI agents) can do common text work
inside a tool call, instead of pasting it into a Code node and writing
the same regex by hand each time.

Inputs are deliberately flat scalars — single strings and ints — so n8n's
MCP Client Tool node renders them as a clean form with one input per
field. Avoid nested objects in tool arguments; either flatten them or
serialize to a JSON string parameter.
"""
from __future__ import annotations

import re
from collections import Counter

from fastmcp import FastMCP


# Common English stopwords. Kept inline (not a dependency) so this module
# stays drop-in.
_STOPWORDS = {
    "a","an","the","and","or","but","if","then","else","of","at","by","for","with",
    "about","against","between","into","through","during","before","after","above",
    "below","to","from","up","down","in","out","on","off","over","under","again",
    "further","once","here","there","when","where","why","how","all","any","both",
    "each","few","more","most","other","some","such","no","nor","not","only","own",
    "same","so","than","too","very","s","t","can","will","just","don","should","now",
    "is","are","was","were","be","been","being","have","has","had","do","does","did",
    "this","that","these","those","i","me","my","we","our","you","your","he","him",
    "his","she","her","it","its","they","them","their","what","which","who","whom",
    "as",
}

_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z\-\.]*[a-zA-Z]|[a-zA-Z]")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def extract_keywords(text: str, top_n: int = 20, min_length: int = 3) -> dict:
        """Extract the top-N most frequent meaningful terms from text.

        Lowercases everything, drops English stopwords, drops tokens
        shorter than `min_length`, and returns the top-`top_n` by
        frequency. Useful as a quick "what's this document about" probe
        or as a pre-filter before semantic processing.

        Args:
            text: The text to analyze. Any length.
            top_n: How many keywords to return. Defaults to 20.
            min_length: Minimum token length. Defaults to 3 (drops "ok",
                "is", short numbers, etc).

        Returns:
            A dict with `total_tokens`, `unique_tokens`, and
            `keywords` (list of {term, count}, ordered by count desc).
        """
        toks = [t for t in _tokenize(text) if len(t) >= min_length and t not in _STOPWORDS]
        counts = Counter(toks)
        top = counts.most_common(top_n)
        return {
            "total_tokens": len(toks),
            "unique_tokens": len(counts),
            "keywords": [{"term": term, "count": count} for term, count in top],
        }

    @mcp.tool
    def score_text_overlap(text_a: str, text_b: str, min_length: int = 3) -> dict:
        """Score how much of `text_a`'s vocabulary appears in `text_b`.

        Computes a Jobscan-style keyword-coverage percentage: the
        fraction of unique meaningful terms in `text_a` that also occur
        in `text_b`. Useful for resume-vs-JD coverage checks, document
        deduplication, or quick "are these talking about the same
        thing" probes.

        Args:
            text_a: The reference text. Its vocabulary is what gets
                scored. For a resume-vs-JD match, pass the JD here so
                the score reflects how much of the JD your resume
                covers (which is what ATS engines actually compute).
            text_b: The candidate text whose coverage is being measured.
            min_length: Drop tokens shorter than this. Defaults to 3.

        Returns:
            A dict with `coverage_pct` (0-100), `total_terms`,
            `matched_terms`, and `missing_terms` (up to 50 examples of
            terms in A that didn't appear in B).
        """
        a_tokens = {t for t in _tokenize(text_a) if len(t) >= min_length and t not in _STOPWORDS}
        b_text = " ".join(_tokenize(text_b))
        if not a_tokens:
            return {"coverage_pct": 0.0, "total_terms": 0, "matched_terms": 0, "missing_terms": []}
        matched = {t for t in a_tokens if t in b_text}
        missing = sorted(a_tokens - matched)
        return {
            "coverage_pct": round(100.0 * len(matched) / len(a_tokens), 1),
            "total_terms": len(a_tokens),
            "matched_terms": len(matched),
            "missing_terms": missing[:50],
        }
