# n8n-helper

A small, self-hosted **MCP server** that exposes Python tools and markdown-defined "skills" to [n8n](https://n8n.io/) (and any other MCP client) over **HTTP streamable transport** with **bearer-token authentication**.

> **What problem does this solve?** n8n's HTTP node can fetch URLs and call REST APIs, but it can't run a headless browser, score a resume against a JD, scrape multiple job boards, or compose Python-only libraries. This server is a sidecar that gives n8n (and any LLM agent) a place to put that logic — once, in one Python project — and then call it from any workflow.

---

## Table of contents

1. [What this server is](#1-what-this-server-is)
2. [Architecture](#2-architecture)
3. [Quick start](#3-quick-start)
4. [Authentication](#4-authentication)
5. [Connecting from n8n](#5-connecting-from-n8n)
6. [Adding a tool (in-process Python)](#6-adding-a-tool-in-process-python)
7. [Adding a skill (markdown + script / prompt)](#7-adding-a-skill-markdown--script--prompt)
8. [Built-in tools](#8-built-in-tools)
9. [Deployment](#9-deployment)
10. [Roadmap](#10-roadmap)

---

## 1. What this server is

`n8n-helper` is a [FastMCP](https://github.com/prefecthq/fastmcp) server that:

- Speaks the **Model Context Protocol (MCP)** over HTTP streamable transport (the modern replacement for SSE), so any MCP-aware client can connect.
- Authenticates clients with a **bearer token** in the `Authorization` header — set in `.env`, never committed.
- Exposes **two layers of capability**:
  - **Tools** — Python functions decorated with `@mcp.tool` in `tools/*.py`. Best for fast, in-process logic.
  - **Skills** — markdown-defined units in `skills/<name>/SKILL.md`. Best for capabilities that have prompt logic, ship as a script, or you want to author/edit without restarting the server.

The same server is intended to be used by:
- **n8n workflows** — via the *MCP Client Tool* node, calling `http://<host>:8000/mcp/`.
- **AI agents / Claude Code / Cursor** — same endpoint, same bearer token.
- **Direct HTTP callers** — `curl`, scripts, etc., for debugging or one-off automation.

## 2. Architecture

```
                ┌──────────────────────────────────────────────────┐
                │                  n8n-helper                      │
                │                                                  │
   n8n ─────────┤  FastMCP server                                  │
   Claude ──────┤    transport=http  (streamable)                  │
   curl  ───────┤    auth=Bearer <N8N_HELPER_TOKEN>                │
                │                                                  │
                │    register_tools()                              │
                │      └── tools/health.py        @mcp.tool        │
                │      └── tools/skills_runner.py @mcp.tool        │
                │           └── list_skills, run_skill             │
                │                                                  │
                │    skills/<name>/SKILL.md                        │
                │      ├── category: python  (in-process import)   │
                │      ├── category: script  (subprocess)          │
                │      └── category: prompt  (LLM call)            │
                │                                                  │
                └──────────────────────────────────────────────────┘
```

Two layers exist for a reason:
- **Tools** are for capabilities you want available *the instant the server starts*. They live in version control alongside the server, get type-checked imports, and run in-process.
- **Skills** are for capabilities you want to author, iterate, or hot-swap. A skill is a folder. Drop a folder in, the next `list_skills` call sees it. Edit the skill's frontmatter or script; the next `run_skill` reloads it.

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

You should see a JSON response listing `health`, `list_skills`, and `run_skill`.

## 4. Authentication

Auth is a **bearer token in the `Authorization` header**. The server uses FastMCP's `StaticTokenVerifier`, which compares the incoming token against an in-process map loaded from environment variables.

| Variable | Purpose |
|---|---|
| `N8N_HELPER_TOKEN` | Primary token. Required. Has full `tools:read,tools:write` scopes. |
| `N8N_HELPER_TOKEN_RO` | Optional second token with only `tools:read`. Use for clients you don't fully trust. |

Token rotation: change the value in `.env`, restart the server, update the consumer. There is no token revocation list — by design, the source of truth is the environment.

For OAuth2 (full Authorization Server flows for human users), see the [Roadmap](#10-roadmap). v1 ships bearer-only because n8n + Claude Desktop + Cursor all support bearer headers natively and OAuth2 adds significant operational complexity that personal usage doesn't need yet.

## 5. Connecting from n8n

In your n8n workflow:

1. Add an **AI Agent** node (or any node that supports MCP tools).
2. Add an **MCP Client Tool** sub-node.
3. Configure:
   - **Endpoint**: `https://your-domain.example/mcp/` (or the local URL while testing)
   - **Transport**: HTTP Streamable
   - **Authentication**: Header → `Authorization: Bearer <N8N_HELPER_TOKEN>`
4. Save. The n8n agent will auto-discover every tool registered on the server (`health`, `list_skills`, `run_skill`, plus anything you add).

When the agent decides to use a tool, it sends a JSON-RPC `tools/call` request; the server runs the function and streams the result back. n8n surfaces the result as the node output.

## 6. Adding a tool (in-process Python)

For most capabilities, this is the right path. Tools are pure Python functions that run inside the server process — no subprocess overhead, full access to the project's deps.

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
from tools import health, skills_runner, example
example.register(mcp)
```

Restart the server. The tool is live.

> **Why explicit registration instead of auto-discovery?** It keeps the tool surface visible in one place (`server.py`). When you read this server cold, every capability is one grep away. Auto-discovery hides that information.

## 7. Adding a skill (markdown + script / prompt)

Skills are for capabilities you want to **author, edit, or hot-swap** without touching the server code. Each skill is a folder containing a `SKILL.md`.

### 7.1 Skill file structure

```
skills/
└── job-scorer/
    ├── SKILL.md          # frontmatter + instructions
    ├── scripts/
    │   └── score.py      # for category=script
    └── data/
        └── ...           # optional supporting files
```

### 7.2 SKILL.md frontmatter

```yaml
---
name: job-scorer                       # MCP-callable name
description: Score a job URL against the master resume
category: script                       # python | script | prompt
inputs:
  url: { type: string, required: true }
  resume_path: { type: string, default: /root/n8n-helper/data/resume.md }
runs:
  script: scripts/score.py             # path within this skill folder
  entry: score_url                     # informational; subprocess uses --json arg
---

# Job Scorer

Description of what the skill does, when to use it, and what its outputs mean.
This body is shown to AI agents via `list_skills`. For `category: prompt`
skills, this body is also passed as the LLM system prompt.
```

### 7.3 The three categories

| Category | What it does | When to use |
|---|---|---|
| `python` | Imports a module from the skill folder and calls `entry(**inputs)` in-process. Fastest, no subprocess. | Pure-Python logic that wants the server's deps. |
| `script` | Runs `python <script> --json '<inputs>'` as a subprocess. Captures stdout (must be a single JSON object). | Heavier scripts, isolation, or when you want to write the script in any language with a Python wrapper. |
| `prompt` | Sends the markdown body + inputs to an LLM (Anthropic Haiku by default). Returns the model's text. | Skills whose value *is* the prompt — research, classification, drafting. |

### 7.4 Script-skill convention

A script skill MUST:

- Accept a `--json '<payload>'` CLI argument.
- Print **exactly one JSON document** to stdout on success.
- Exit non-zero on failure (the runner surfaces stderr to the caller).

A minimal example:

```python
# skills/job-scorer/scripts/score.py
import argparse, json, sys

def score_url(url: str, resume_path: str = "/root/n8n-helper/data/resume.md") -> dict:
    # ... real scoring logic here ...
    return {"score": 87.5, "missing": ["wearable"]}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    args = ap.parse_args()
    inputs = json.loads(args.json)
    print(json.dumps(score_url(**inputs)))
```

Calling from n8n / any MCP client:

```json
{
  "name": "run_skill",
  "arguments": {
    "name": "job-scorer",
    "inputs": { "url": "https://jobs.example.com/123" }
  }
}
```

## 8. Built-in tools

Out of the box, after `pip install -r requirements.txt && python server.py`, the server exposes:

| Tool | Description |
|---|---|
| `find_tool(query, limit)` | Search the registry by free-text query. Returns top matches with name, description, and inputSchema — enough for an agent to call the matched tool directly. |
| `health()` | Returns server status, uptime, Python version, and a UTC timestamp. Smoke-test for connectivity. |
| `fetch_url(url, max_chars)` | Fetch a URL via Jina Reader and return its content as clean markdown. Handles JS-rendered SPAs (Ashby, Greenhouse, Workday) that fail with a plain HTTP fetch. |
| `extract_keywords(text, top_n, min_length)` | Top-N most frequent meaningful terms in a text (drops stopwords). |
| `score_text_overlap(text_a, text_b, min_length)` | Jobscan-style coverage percentage: how much of `text_a`'s vocabulary appears in `text_b`. Useful for resume-vs-JD checks. |
| `list_skills()` | Lists every skill folder under `skills/` with its frontmatter summary. |
| `run_skill(name, inputs)` | Executes a named skill and returns its output. Dispatches to python/script/prompt handler based on the skill's category. |

Skills under `skills/` are discovered on every call, so adding a new skill folder requires no restart.

### 8.1 The `find_tool` workflow

When the server hosts dozens (eventually hundreds) of tools, listing them all in an agent's context wastes tokens and listing them in a dropdown frustrates humans. `find_tool` solves this by being *the* entry point for tool discovery:

```jsonc
// Agent calls find_tool first
{
  "name": "find_tool",
  "arguments": { "query": "score a job posting", "limit": 3 }
}

// Server returns ranked matches with the schema needed to call them
{
  "total": 12,
  "matches": [
    {
      "name": "score_text_overlap",
      "description": "Jobscan-style coverage percentage...",
      "inputSchema": {
        "properties": {
          "text_a": { "type": "string", "description": "..." },
          "text_b": { "type": "string", "description": "..." }
        },
        "required": ["text_a", "text_b"]
      }
    }
  ]
}
```

The agent then issues a normal MCP `tools/call` against the matched tool. In n8n's classic (non-agent) workflow mode, you don't need `find_tool` — n8n already shows every registered tool in its dropdown — but the same convention pays off when the catalog grows.

### 8.2 Tool argument convention

Tools must use **flat scalar arguments** — strings, ints, floats, bools — never nested objects. Two reasons:

1. n8n's MCP Client Tool node renders each argument as one form field. Flat scalars become a clean form; nested dicts render as raw JSON the user has to write by hand.
2. URL/form-encoded transports (and many tool-calling LLMs) expect flat key=value parameters.

If your tool needs structured input, accept a JSON-encoded string parameter and parse it inside the tool — don't expose nested types in the schema.

## 9. Deployment

The same `build_app()` factory in `server.py` is reused for every deployment target — local development, Vercel, VPS, Docker — so the wiring is identical and only the runtime envelope changes.

### 9.1 Vercel (recommended for this project)

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

Connect the GitHub repo via the Vercel dashboard once and every `git push` to `main` will auto-deploy.

**Caveats specific to Vercel + MCP:**

- Sessions live in-process. Fluid Compute keeps a warm instance alive across calls so this usually doesn't matter, but on cold start the MCP client must re-handshake. n8n's MCP Client Tool node does this transparently.
- For high-fanout multi-instance workloads you'll want session state in Vercel KV (Redis). Out of scope for v1.
- `.env` is in `.gitignore` and `.vercelignore`; never put secrets in `vercel.json`. Use `vercel env add` (or the dashboard).

### 9.2 Other targets

| Option | Setup | When to use |
|---|---|---|
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8000` | Fastest path for local-only deploys. Free. |
| **Reverse proxy on a VPS** | nginx → uvicorn → `server:build_app().http_app()`, Caddy gives auto-TLS | Best when you already pay for the box. |
| **Fly.io / Railway / Render** | `Dockerfile` → push → done | Lowest ops if you don't already have a VPS. |

For all of these, set `N8N_HELPER_TOKEN` in the host's secret store, never in a committed file. Confirm TLS, and that the bearer header is being forwarded by your proxy.

## 10. Roadmap

The current scope is intentionally small (HTTP + bearer + tools + skills runner). Planned next steps, in priority order:

1. **OAuth2 authorization server** — for human users, multi-tenant, or selling skill access. Replaces (or layers over) the bearer model.
2. **Webhooks IN** — `/webhooks/<integration>/<event>` endpoints that trigger skill chains (Stripe, GitHub, Telegram).
3. **Cron** — APScheduler runs skills on a cron expression declared in their frontmatter.
4. **Events OUT** — pub/sub bus that fires `skill.<name>.completed` events for downstream subscribers.
5. **Lifecycle middleware** — per-call audit log, rate limit, billing meter.
6. **Skill registry index** — DB-backed listing for fast `list_skills` over hundreds of skills.

Items 1–3 are the most likely to ship in v2. Items 4–6 unlock multi-tenant / paid usage and are deliberately deferred.

---

## License

MIT.
