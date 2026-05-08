"""Skill runner — turns markdown files in `skills/<name>/SKILL.md` into MCP tools.

A "skill" is a self-contained capability described in a folder. The folder
must contain a `SKILL.md` whose YAML frontmatter declares how the skill runs:

    ---
    name: job-scorer
    description: Score a job posting URL against the master resume
    category: script           # python | script | prompt
    inputs:
      url: { type: string, required: true }
    runs:
      script: scripts/score.py
      entry: score_url
    ---
    [markdown body — used as the prompt for `category: prompt` skills]

Three execution categories:

* **python** — import `runs.module` and call `runs.entry(**inputs)` in-process.
* **script** — run `python <runs.script> --json '<inputs>'` as a subprocess.
* **prompt** — send the markdown body + inputs to an LLM and return the reply.

The runner exposes two MCP tools to clients:

* `list_skills()` — enumerate every skill folder with its frontmatter summary.
* `run_skill(name, inputs)` — execute the named skill and return its output.

Adding a skill = creating a folder. No server restart required for a fresh
discovery — the registry is rescanned on every `list_skills` / `run_skill` call.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"


def _parse_skill(skill_dir: Path) -> dict | None:
    """Load and split a SKILL.md into frontmatter + body. Returns None if missing."""
    md_path = skill_dir / "SKILL.md"
    if not md_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {"name": skill_dir.name, "description": "", "body": text, "meta": {}}
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return {
        "name": meta.get("name", skill_dir.name),
        "description": meta.get("description", ""),
        "category": meta.get("category", "prompt"),
        "inputs": meta.get("inputs", {}),
        "runs": meta.get("runs", {}),
        "body": body.strip(),
        "meta": meta,
        "path": skill_dir,
    }


def _discover_skills() -> dict[str, dict]:
    """Return a map of skill_name -> parsed skill dict."""
    if not SKILLS_DIR.exists():
        return {}
    skills: dict[str, dict] = {}
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        parsed = _parse_skill(child)
        if parsed:
            skills[parsed["name"]] = parsed
    return skills


def _run_python_skill(skill: dict, inputs: dict[str, Any]) -> Any:
    """Import the declared module and call its entry function.

    The module is resolved relative to the skill folder, so `runs.module:
    handlers` looks for `skills/<skill>/handlers.py` and calls
    `handlers.<entry>(**inputs)`.
    """
    spec = skill["runs"]
    module_name = spec.get("module")
    entry = spec.get("entry", "run")
    if not module_name:
        raise ValueError(f"Skill {skill['name']!r} has category=python but no runs.module")

    skill_dir = skill["path"]
    sys.path.insert(0, str(skill_dir))
    try:
        mod = importlib.import_module(module_name)
        importlib.reload(mod)  # pick up edits during dev without restart
        fn = getattr(mod, entry)
        return fn(**inputs)
    finally:
        sys.path.remove(str(skill_dir))


def _run_script_skill(skill: dict, inputs: dict[str, Any]) -> Any:
    """Execute `python <script> --json '<inputs>'` as a subprocess.

    Output contract: the script prints a single JSON document on stdout. The
    runner parses it and returns it to the caller. stderr is captured and
    surfaced on failure.
    """
    spec = skill["runs"]
    script = spec.get("script")
    if not script:
        raise ValueError(f"Skill {skill['name']!r} has category=script but no runs.script")

    script_path = skill["path"] / script
    if not script_path.exists():
        raise FileNotFoundError(f"Skill script not found: {script_path}")

    payload = json.dumps(inputs)
    proc = subprocess.run(
        [sys.executable, str(script_path), "--json", payload],
        capture_output=True,
        text=True,
        cwd=skill["path"],
        timeout=int(os.getenv("SKILL_TIMEOUT", "120")),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Skill {skill['name']!r} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    out = proc.stdout.strip()
    if not out:
        return {"ok": True}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def _run_prompt_skill(skill: dict, inputs: dict[str, Any]) -> Any:
    """Send the skill's markdown body + inputs to an LLM and return the reply.

    Uses the Anthropic API (Haiku by default — cheap and fast — overridable
    via `runs.model`). The skill body becomes the system prompt; inputs are
    rendered as the user message.
    """
    try:
        import anthropic  # imported lazily so this module loads without the SDK
    except ImportError as e:
        raise RuntimeError(
            "Prompt skills require the `anthropic` package. "
            "Install with `pip install anthropic`."
        ) from e

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot run prompt skills.")

    model = skill["runs"].get("model", "claude-haiku-4-5-20251001")
    client = anthropic.Anthropic(api_key=api_key)
    user_msg = json.dumps(inputs, indent=2) if inputs else "(no inputs provided)"
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=skill["body"],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    return {"text": text, "model": model}


_DISPATCH = {
    "python": _run_python_skill,
    "script": _run_script_skill,
    "prompt": _run_prompt_skill,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def list_skills() -> list[dict]:
        """Return every skill discovered under /skills/ with its frontmatter summary.

        Each entry includes name, description, category, declared inputs, and
        the runs spec — enough for an agent to decide which skill to call and
        how to shape its input payload.
        """
        return [
            {
                "name": s["name"],
                "description": s["description"],
                "category": s["category"],
                "inputs": s["inputs"],
                "runs": s["runs"],
            }
            for s in _discover_skills().values()
        ]

    @mcp.tool
    def run_skill(name: str, inputs: dict | None = None) -> dict:
        """Execute the named skill and return its output.

        Args:
            name: The skill name (matches the `name` field in SKILL.md frontmatter,
                or the folder name if no frontmatter is present).
            inputs: Keyword arguments to pass to the skill. Required keys are
                declared by the skill's frontmatter under `inputs`.

        The runner dispatches to one of three handlers based on `category`:
        python (in-process function call), script (subprocess), or prompt
        (LLM call with the markdown body as system prompt).
        """
        skills = _discover_skills()
        if name not in skills:
            available = ", ".join(skills) or "(none)"
            raise ValueError(f"Unknown skill: {name!r}. Available: {available}")
        skill = skills[name]
        handler = _DISPATCH.get(skill["category"])
        if handler is None:
            raise ValueError(
                f"Skill {name!r} declares unknown category {skill['category']!r}; "
                f"expected one of: {', '.join(_DISPATCH)}"
            )
        result = handler(skill, inputs or {})
        return {"skill": name, "result": result}
