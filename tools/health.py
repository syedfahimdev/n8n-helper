"""Health check tool — used by clients (and by the operator) to confirm the
server is alive and reachable behind whatever tunnel/proxy is in front of it.
"""
from __future__ import annotations

import platform
import time

from fastmcp import FastMCP

_BOOT_TIME = time.time()


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def health() -> dict:
        """Return server status, uptime, Python version, and a wall-clock timestamp.

        Useful for verifying the server is reachable through your reverse proxy
        or tunnel. Should round-trip in <50ms even under load.
        """
        return {
            "status": "ok",
            "server": "n8n-helper",
            "uptime_seconds": round(time.time() - _BOOT_TIME, 2),
            "python": platform.python_version(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
