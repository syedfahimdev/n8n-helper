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
        """Search this server's tool registry to find the right tool for a task.

        Call this FIRST whenever you (an AI agent) need to do something but
        don't already know which specific tool to invoke. It is cheaper than
        listing the entire catalog (one round-trip, ~few hundred tokens)
        and returns enough information that your next call can be the
        actual `tools/call` against the matched tool — no second discovery
        round-trip needed.

        ## How to write a good `query`

        Use a short natural-language phrase describing the *action* you
        want to perform, in 2–8 words. Include the verb and the noun.
        Avoid filler words ("please", "I want to", "can you").

        Good queries (return clean matches):
          • "fetch a webpage as markdown"
          • "score a resume against a job description"
          • "search jobs on indeed and linkedin"
          • "extract email addresses from text"
          • "convert markdown to pdf"

        Bad queries (too vague — return many low-confidence matches):
          • "help"
          • "the thing for jobs"
          • "do stuff with text"

        If your first query returns no matches with a good `_score` (>= 5),
        rephrase with more specific verbs/nouns and try again.

        ## What you get back

        A dict with:
          • `total` — total number of tools on the server (lets you know
            how big the catalog is).
          • `matches` — up to `limit` items, each with:
              - `name` — exact tool name to use in `tools/call`
              - `description` — first line of the tool's docstring
              - `inputSchema` — JSON Schema for the tool's arguments,
                with `properties`, `required`, and per-field
                `description`. Use this verbatim to construct the
                arguments object for `tools/call`.
              - `_score` — relevance score (higher is better; >=10 means
                the query phrase appeared in the tool's name).

        ## What to do next

        Read `inputSchema.properties` to learn each argument's name and
        type, fill in values, then issue:

            tools/call { "name": <matched.name>, "arguments": { ... } }

        ## Important

        `find_tool` does not call the matched tool for you. It is search
        only. The agent is responsible for the follow-up `tools/call`.

        Args:
            query: 2–8 word natural-language phrase describing the action
                you want to perform. Match against tool names and
                descriptions; names weighted higher.
            limit: Max number of matches to return. Defaults to 5. Lower
                this when you're confident; raise it when exploring.

        Returns:
            A dict shaped `{ total: int, matches: [{ name, description,
            inputSchema, _score }, ...] }`. `matches` is empty if nothing
            scored above zero — try a different query.
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
