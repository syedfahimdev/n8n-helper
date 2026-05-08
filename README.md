# n8n-helper

A small, self-hosted **MCP server** that exposes Python tools to [n8n](https://n8n.io/) (and any other MCP client) over **HTTP streamable transport** with **bearer-token authentication**.

> **What problem does this solve?** n8n's HTTP node can fetch URLs and call REST APIs, but it can't run a headless browser, score a resume against a JD, scrape multiple job boards, or compose Python-only libraries. This server is a sidecar that gives n8n (and any LLM agent) a place to put that logic — once, in one Python project — and then call it from any workflow.

---

## Table of contents

1. [What this server is](#1-what-this-server-is)
2. [Architecture](#2-architecture)
3. [Quick start](#3-quick-start)
4. [Authentication](#4-authentication)
5. [Connecting from n8n](#5-connecting-from-n8n)
6. [Adding a tool](#6-adding-a-tool)
7. [Built-in tools](#7-built-in-tools)
8. [Deployment](#8-deployment)
9. [Roadmap](#9-roadmap)

---

## 1. What this server is

`n8n-helper` is a [FastMCP](https://github.com/prefecthq/fastmcp) server that:

- Speaks the **Model Context Protocol (MCP)** over HTTP streamable transport (the modern replacement for SSE), so any MCP-aware client can connect.
- Authenticates clients with a **bearer token** in the `Authorization` header — set in `.env`, never committed.
- Exposes **tools**: Python functions decorated with `@mcp.tool` in `tools/*.py`. Each tool has a clear name and description so it can be discovered by keyword or use-case via the built-in `find_tool` capability.

The same server is intended to be used by:

- **n8n workflows** — via the *MCP Client Tool* node, calling `https://<host>/mcp/`.
- **AI agents / Claude Code / Cursor** — same endpoint, same bearer token.
- **Direct HTTP callers** — `curl`, scripts, etc., for debugging or one-off automation.

A markdown-defined "skills" layer (capabilities you author/iterate on without touching server code) is on the roadmap but **deliberately deferred** until the tools layer is mature.

## 2. Architecture

```
                ┌──────────────────────────────────────────────────┐
                │                  n8n-helper                      │
                │                                                  │
   n8n ─────────┤  FastMCP server                                  │
   Claude ──────┤    transport=http  (streamable)                  │
   curl  ───────┤    auth=Bearer <N8N_HELPER_TOKEN>                │
                │    stateless_http=True (serverless-safe)         │
                │                                                  │
                │    register_tools()                              │
                │      ├── tools/discovery.py  →  find_tool        │
                │      └── tools/health.py     →  health           │
                │                                                  │
                └──────────────────────────────────────────────────┘
```

The whole point is to keep the surface small and explicit. Every capability the server exposes is registered by name in one place (`server.py`'s `register_tools()`) — read it cold, you see everything available.

## 3. Quick start

```bash
git clone https://github.com/syedfahimdev/n8n-helper.git
cd n8n-helper

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Generate a strong token and paste it as N8N_HELPER_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(32))"

python server.py
# → server listening on 0.0.0.0:8000, MCP endpoint at /mcp/
```

Verify health:

```bash
TOKEN="$(grep N8N_HELPER_TOKEN .env | head -1 | cut -d= -f2)"
curl -sS -X POST http://localhost:8000/mcp/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

You should see a JSON response listing `find_tool` and `health`.

## 4. Authentication

Auth is a **bearer token in the `Authorization` header**. The server uses FastMCP's `StaticTokenVerifier`, which compares the incoming token against an in-process map loaded from environment variables.

| Variable | Purpose |
|---|---|
| `N8N_HELPER_TOKEN` | Primary token. Required. Has full `tools:read,tools:write` scopes. |
| `N8N_HELPER_TOKEN_RO` | Optional second token with only `tools:read`. Use for clients you don't fully trust. |

Token rotation: change the value in `.env` (or in your platform's secret store), restart the server, update the consumer. There is no token revocation list — by design, the source of truth is the environment.

For OAuth2 (full Authorization Server flows for human users), see the [Roadmap](#9-roadmap). v1 ships bearer-only because n8n + Claude Desktop + Cursor all support bearer headers natively and OAuth2 adds significant operational complexity that personal usage doesn't need yet.

## 5. Connecting from n8n

In your n8n workflow:

1. Add an **AI Agent** node (or any node that supports MCP tools).
2. Add an **MCP Client Tool** sub-node.
3. Configure:
   - **Endpoint**: `https://your-domain.example/mcp/` (or the local URL while testing)
   - **Transport**: HTTP Streamable
   - **Authentication**: Header → `Authorization: Bearer <N8N_HELPER_TOKEN>`
4. Save. The n8n agent will auto-discover every tool registered on the server (`find_tool`, `health`, plus anything you add).

When the agent decides to use a tool, it sends a JSON-RPC `tools/call` request; the server runs the function and streams the result back. n8n surfaces the result as the node output.

## 6. Adding a tool

Tools are Python functions registered with `@mcp.tool`. They run in-process — no subprocess overhead, full access to the project's deps.

```python
# tools/example.py
from fastmcp import FastMCP

def register(mcp: FastMCP) -> None:
    @mcp.tool
    def greet(name: str) -> str:
        """Say hello. Used as a smoke test for new clients."""
        return f"hi, {name}"
```

Then in `server.py`'s `register_tools()`:

```python
from tools import discovery, health, example
example.register(mcp)
```

Restart the server. The tool is live.

> **Why explicit registration instead of auto-discovery?** It keeps the tool surface visible in one place (`server.py`). When you read this server cold, every capability is one grep away. Auto-discovery hides that information.

### 6.1 Tool argument convention

Tools must use **flat scalar arguments** — strings, ints, floats, bools — never nested objects. Two reasons:

1. n8n's MCP Client Tool node renders each argument as one form field. Flat scalars become a clean form; nested dicts render as raw JSON the user has to write by hand.
2. URL/form-encoded transports (and many tool-calling LLMs) expect flat key=value parameters.

If your tool needs structured input, accept a JSON-encoded string parameter and parse it inside the tool — don't expose nested types in the schema.

### 6.2 Designing a new tool

Every tool is **discoverable by keywords or use-case**. That only works if the name and description carry enough signal. Before writing any code, fill in this short design sketch:

```
Tool name:        <verb>_<domain>          # e.g. fetch_url, score_resume_match
One-line desc:    <what it does in plain language, 1 sentence>
Inputs:           <flat scalar args; declare each with a clear description>
Output:           <shape of return value; flat dict preferred>
Upstream APIs:    <free / freemium / paid services this tool composes, if any>
```

If you can't fill the one-line description in a single sentence, the tool is doing too much — split it. If the inputs need nested objects, flatten them or accept a JSON-encoded string parameter.

### 6.3 Composing multiple external APIs in one tool

A single MCP tool can stitch together multiple upstream services to deliver one clean capability. The client doesn't see vendor diversity; the tool absorbs it.

Example sketch:

```python
@mcp.tool
async def score_job_url(url: str, resume_path: str = "/data/resume.md") -> dict:
    """Fetch a job posting URL and score the user's resume against it.

    Internally composes:
      - Jina Reader (free, https://r.jina.ai)        → URL → clean markdown
      - Local resume.md                              → reference text
      - In-process keyword overlap                   → coverage percentage
    """
    jd_text = await _fetch_via_jina(url)
    resume = Path(resume_path).read_text()
    return _compute_overlap(jd_text, resume)
```

Guidelines for composition:

- **Prefer free + OSS first.** Reach for paid tiers only when free options can't meet the latency / quality / volume bar.
- **Keep API keys in env vars**, never in the tool's code or arguments. The tool reads `os.getenv(...)` lazily so the server boots even if a key is missing.
- **Fail loud.** If an upstream service returns 4xx/5xx, raise — don't silently fall back unless the description explicitly promises a fallback.
- **Cache idempotent reads.** If a tool fetches the same URL many times, an in-process LRU keeps cost down without changing the API.
- **Document upstream dependencies in the docstring.** Future maintainers (and `find_tool`'s users) need to know what the tool actually depends on.

## 7. Built-in tools

Out of the box, after `pip install -r requirements.txt && python server.py`, the server exposes:

| Tool | Description |
|---|---|
| `find_tool(query, limit)` | **The discovery tool.** Search the registry by free-text query. Returns top matches with name, description, and inputSchema — enough for an agent to call the matched tool directly. Use this first whenever you don't already know the right tool. |
| `health()` | Returns server status, uptime, Python version, and a UTC timestamp. Smoke-test for connectivity. |

The server intentionally ships with a small, sharp default set. Capability tools (URL fetching, text scoring, job search, etc.) are added one at a time, each with its own design pass — see [§6.2](#62-designing-a-new-tool).

### 7.1 The `find_tool` workflow

When the server hosts dozens (eventually hundreds) of tools, listing them all in an agent's context wastes tokens and listing them in a dropdown frustrates humans. `find_tool` solves this by being *the* entry point for tool discovery:

```jsonc
// Agent calls find_tool first
{
  "name": "find_tool",
  "arguments": { "query": "fetch a webpage as markdown", "limit": 3 }
}

// Server returns ranked matches with the schema needed to call them
{
  "total": 12,
  "matches": [
    {
      "name": "fetch_url",
      "description": "Fetch a URL and return its content as clean markdown.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "url": { "type": "string", "description": "Absolute URL to fetch" },
          "max_chars": { "type": "integer", "default": 20000 }
        },
        "required": ["url"]
      },
      "_score": 11
    }
  ]
}
```

The agent then issues a normal MCP `tools/call` against the matched tool. In n8n's classic (non-agent) workflow mode, you don't need `find_tool` — n8n already shows every registered tool in its dropdown — but the same naming/description discipline pays off when the catalog grows.

## 8. Deployment

The same `build_app()` factory in `server.py` is reused for every deployment target — local development, Vercel, VPS, Docker — so the wiring is identical and only the runtime envelope changes.

### 8.1 Vercel (recommended for this project)

The repo ships with `api/index.py` (ASGI entrypoint — uses one of Vercel's recognized Python entrypoint filenames) and `vercel.json` (Fluid Compute on). Vercel has [native MCP server support](https://vercel.com/docs/mcp/deploy-mcp-servers-to-vercel) — Fluid Compute keeps instances warm so MCP session state survives bursty traffic.

Deploy:

```bash
# from inside the project root
vercel link              # connect this folder to a Vercel project (or create one)
vercel env add N8N_HELPER_TOKEN production
# paste the token you generated for .env, hit enter
vercel --prod            # first production deploy
```

After the first deploy, the endpoint is:

```
POST https://<project-name>.vercel.app/mcp
Header: Authorization: Bearer <N8N_HELPER_TOKEN>
```

Connect the GitHub repo via the Vercel dashboard once and every `git push` to `main` will auto-deploy. If you don't see your latest changes live after a push, run `vercel --prod` from the repo root — that explicitly promotes the latest deployment to the production alias.

**Caveats specific to Vercel + MCP:**

- Sessions are stateless (`stateless_http=True` in `api/index.py`). Each MCP call is self-contained, so cold starts don't surface as "Session not found" errors. This is the right default for tool-calling workloads.
- `.env` is in `.gitignore` and `.vercelignore`; never put secrets in `vercel.json`. Use `vercel env add` (or the dashboard).

### 8.2 Other targets

| Option | Setup | When to use |
|---|---|---|
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8000` | Fastest path for local-only deploys. Free. |
| **Reverse proxy on a VPS** | nginx → uvicorn → `server:build_app().http_app()`, Caddy gives auto-TLS | Best when you already pay for the box. |
| **Fly.io / Railway / Render** | `Dockerfile` → push → done | Lowest ops if you don't already have a VPS. |

For all of these, set `N8N_HELPER_TOKEN` in the host's secret store, never in a committed file. Confirm TLS, and that the bearer header is being forwarded by your proxy.

## 9. Roadmap

The current scope is intentionally small (HTTP + bearer + tools + `find_tool` discovery). Planned next steps, in priority order:

1. **Real capability tools** — each with a name + description pass first. Likely first additions: `fetch_url` (Jina Reader), `score_resume_against_jd` (composes URL fetch + keyword overlap + master resume).
2. **OAuth2 authorization server** — for human users, multi-tenant, or selling tool access. Layers over the bearer model.
3. **Webhooks IN** — `/webhooks/<integration>/<event>` endpoints that trigger tool chains (Stripe, GitHub, Telegram).
4. **Skills layer (markdown-defined capabilities)** — deferred until tools are mature. Author/iterate without touching server code; markdown frontmatter declares inputs and execution category (in-process Python, subprocess script, or LLM prompt).
5. **Cron** — APScheduler runs tools/skills on a cron expression.
6. **Events OUT** — pub/sub bus that fires `tool.<name>.completed` events for downstream subscribers.
7. **Lifecycle middleware** — per-call audit log, rate limit, billing meter.

Items 1–3 are the most likely to ship next. Items 4–7 unlock multi-tenant / paid usage and are deliberately deferred.

---

## License

MIT.
