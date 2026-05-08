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
