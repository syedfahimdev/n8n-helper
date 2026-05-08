"""Tool discovery — `find_tool` lets clients (and AI agents) search the
server's tool registry by name or description when there are too many to
enumerate by hand.

When the server hosts hundreds of tools, listing them all in an agent's
context (or scrolling them in n8n's dropdown) is hostile UX. `find_tool`
takes a free-text query, scores every registered tool by relevance, and
returns the top matches with their input schemas — enough information for
an agent to call the matched tool directly via MCP `tools/call`, or for a
human to copy the tool name into n8n's dropdown.

Scoring is intentionally simple (substring + token match in name and
description) — fast enough to run on every call, transparent enough to
debug, and good enough until we hit the scale where a vector embedding
matters more than a few false positives.
"""
from __future__ import annotations

import re
from fastmcp import FastMCP


_TOKEN = re.compile(r"[a-zA-Z0-9]+")


def _score(query_tokens: list[str], name: str, description: str) -> int:
    """Score a tool by how well it matches the query. Higher = better.

    Heavily weight name matches over description matches. A name token
    match is worth 5 points; a description token match 1; a full phrase
    match in the name adds 10. Returns 0 for tools that don't match at all.
    """
    name_l = name.lower()
    desc_l = description.lower()
    score = 0

    full_phrase = " ".join(query_tokens)
    if full_phrase and full_phrase in name_l:
        score += 10
    if full_phrase and full_phrase in desc_l:
        score += 3

    for tok in query_tokens:
        if tok in name_l:
            score += 5
        if tok in desc_l:
            score += 1
    return score


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def find_tool(query: str, limit: int = 5) -> dict:
        """Search the server's tool registry by free-text query.

        Use this as the first call when you don't know which tool to use.
        Returns the top matches ranked by relevance, each with its name,
        description, and inputSchema. The schema is the JSON Schema for
        the tool's arguments — exactly what you'd pass to `tools/call`.

        Args:
            query: Free-text query describing what you want to do, e.g.
                "fetch a webpage", "score a job posting", "search jobs".
            limit: Maximum number of matches to return. Defaults to 5.

        Returns:
            A dict with two keys:
              - matches: list of {name, description, inputSchema} for the
                top-ranked tools (excluding `find_tool` itself).
              - total: total number of tools registered on the server.

        Tips:
            - The query is matched against tool names AND descriptions —
              describing tools in plain language pays off here.
            - Names contribute more weight than descriptions; if your
              search is too noisy, name your tools more specifically.
        """
        all_tools = await mcp._list_tools()
        q_tokens = [t.lower() for t in _TOKEN.findall(query)]

        ranked: list[tuple[int, object]] = []
        for tool in all_tools:
            if tool.name == "find_tool":
                continue
            s = _score(q_tokens, tool.name, tool.description or "")
            if s > 0:
                ranked.append((s, tool))
        ranked.sort(key=lambda pair: -pair[0])

        return {
            "total": len(all_tools),
            "matches": [
                {
                    "name": tool.name,
                    "description": (tool.description or "").splitlines()[0],
                    "inputSchema": tool.parameters,
                    "_score": score,
                }
                for score, tool in ranked[:limit]
            ],
        }
