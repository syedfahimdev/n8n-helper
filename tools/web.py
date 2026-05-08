"""Web tools — fetch URLs as clean text via Jina Reader.

`fetch_url` solves the n8n + JS-rendered-page problem: n8n's HTTP node
gets the raw HTML before JavaScript runs, so single-page apps (most
modern sites — Ashby, Greenhouse, Workday, dashboards, blogs built on
React/Vue/Svelte) return an empty `<div id="root">`. Jina Reader
(`r.jina.ai/<url>`) renders the page server-side and returns clean
markdown — fast, no headless browser to manage, free for personal use.

Use this anywhere a workflow needs the *content* of a URL (job posting
text, article body, doc page) rather than the HTML wire format.
"""
from __future__ import annotations

import httpx
from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def fetch_url(url: str, max_chars: int = 20000) -> dict:
        """Fetch a URL and return its content as clean markdown.

        Internally calls Jina Reader (https://r.jina.ai/<url>), which
        renders the target page server-side and strips boilerplate. This
        works for JavaScript-rendered SPAs that fail with a plain HTTP
        fetch — Ashby, Greenhouse, Workday, most modern dashboards.

        Args:
            url: Absolute URL to fetch, e.g. "https://example.com/post".
                Must be http or https. Schemes other than http(s) are
                rejected to prevent SSRF and accidental file:// reads.
            max_chars: Truncate the returned text at this many characters.
                Defaults to 20000 — enough for almost any article or
                job posting. Pass a larger number for long-form pages.

        Returns:
            A dict with `url`, `content` (markdown), and `truncated` (bool).
        """
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Only http(s) URLs are allowed (got {url!r})")

        endpoint = f"https://r.jina.ai/{url}"
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(endpoint)
            r.raise_for_status()
            text = r.text

        truncated = len(text) > max_chars
        return {
            "url": url,
            "content": text[:max_chars],
            "truncated": truncated,
            "length": len(text),
        }
