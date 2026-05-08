"""Vercel ASGI entrypoint.

Vercel's Python backend framework requires the entrypoint to live at one of a
fixed set of names (app.py, index.py, server.py, main.py, wsgi.py, asgi.py) in
the project root, src/, app/, or api/. We use api/index.py and let the FastMCP
server own all routing via path="/mcp" — so the deployed endpoint is:

    POST https://<project>.vercel.app/mcp

The same `build_app()` factory used by `python server.py` for local
development is reused here verbatim, so dev and prod share identical wiring.

Fluid Compute (enabled in vercel.json) keeps warm instances alive, so the
in-process MCP session state survives across the bursty traffic typical of
agentic clients. Cold starts force a session re-handshake which n8n's MCP
Client Tool node handles transparently.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so `from server import build_app` works.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import build_app  # noqa: E402  (path setup must precede import)

mcp = build_app()
app = mcp.http_app(path="/mcp")
