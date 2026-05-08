"""n8n-helper — FastMCP server exposing tools and skills over HTTP streamable transport.

Run:
    python server.py

The server reads config from `.env` (see `.env.example`). Clients (n8n, Claude
Desktop, Cursor, anything that speaks MCP) connect to:

    POST  http://<host>:<port>/mcp/
    Header: Authorization: Bearer <N8N_HELPER_TOKEN>

Adding a new tool: write a `@mcp.tool` function in `tools/` and register it
inside `register_tools(mcp)` below. Adding a new markdown skill: drop a folder
under `skills/<name>/` with a `SKILL.md`. The `run_skill` tool will discover and
execute it on demand.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _build_verifier() -> StaticTokenVerifier:
    """Build a bearer-token verifier from environment variables.

    The primary token (`N8N_HELPER_TOKEN`) is required. A second optional
    read-only token (`N8N_HELPER_TOKEN_RO`) can be set for a less-privileged
    client. Tokens never leave this process — only their hashes are compared.
    """
    primary = os.getenv("N8N_HELPER_TOKEN")
    if not primary or primary == "replace-me-with-a-long-random-string":
        raise RuntimeError(
            "N8N_HELPER_TOKEN is not set. Copy .env.example to .env and set a "
            "strong random token before starting the server."
        )

    tokens: dict[str, dict] = {
        primary: {
            "client_id": "n8n-primary",
            "scopes": ["tools:read", "tools:write"],
        }
    }
    readonly = os.getenv("N8N_HELPER_TOKEN_RO")
    if readonly:
        tokens[readonly] = {
            "client_id": "readonly",
            "scopes": ["tools:read"],
        }

    return StaticTokenVerifier(tokens=tokens, required_scopes=["tools:read"])


def register_tools(mcp: FastMCP) -> None:
    """Wire every tool module's functions into the FastMCP instance.

    Each module under `tools/` exposes a `register(mcp)` function. Keeping
    registration explicit (rather than auto-importing) makes the tool surface
    obvious from this file alone — every capability is one grep away.

    Convention for new tools:
      • Inputs are flat scalars (str, int, float, bool). Avoid nested dicts
        as arguments — they make n8n's UI render badly and don't add value.
      • Each tool has a clear docstring; `find_tool` searches both name and
        description, so plain-language descriptions matter.
      • Tools that depend on external services (anthropic, openai, paid
        APIs) should import their SDK lazily so the server boots without
        the dep installed.
    """
    from tools import discovery, health, skills_runner

    discovery.register(mcp)
    health.register(mcp)
    skills_runner.register(mcp)


def build_app() -> FastMCP:
    mcp = FastMCP(
        name="n8n-helper",
        instructions=(
            "Tools and skills exposed for n8n workflows. Use `health` to check "
            "the server is up. Use `list_skills` to see available markdown skills. "
            "Use `run_skill` to execute a skill by name."
        ),
        auth=_build_verifier(),
    )
    register_tools(mcp)
    return mcp


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()

    mcp = build_app()
    # `transport="http"` is FastMCP's HTTP streamable transport (the modern
    # MCP spec replacement for SSE). Endpoint will be POST /mcp/.
    mcp.run(transport="http", host=host, port=port, log_level=log_level)
