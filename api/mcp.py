"""Vercel ASGI entrypoint.

Vercel's Python runtime auto-discovers any module under `api/` that exports a
top-level `app` and serves it as a function. Placing this file at
`api/mcp.py` makes the deployed endpoint:

    POST https://<project>.vercel.app/api/mcp

The same FastMCP server defined in the project root's `server.py` is reused
verbatim — `build_app()` is the single source of truth so local development
(`python server.py`) and production deployment share identical wiring.

Vercel's Fluid Compute (enabled in vercel.json) keeps warm instances alive,
so MCP session state held in-process survives across the bursty traffic
patterns typical of agentic clients. Cold starts force a session re-handshake
which n8n's MCP Client Tool node handles transparently.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so `from server import build_app` works.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import build_app  # noqa: E402  (path setup must precede import)

mcp = build_app()
# path="/" because Vercel's routing already places this function at /api/mcp;
# we don't want FastMCP to add another /mcp segment to that path.
app = mcp.http_app(path="/")
