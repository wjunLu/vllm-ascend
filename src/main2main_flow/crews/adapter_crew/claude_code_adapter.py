"""Claude Code CLI adapter for the adapter crew.

Configuration lives in config/:
  agents.yaml      — system prompt for each subagent
  orchestrator.md  — orchestrator workflow template

Spawns `claude -p --output-format stream-json --verbose` as a subprocess.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_CONFIG_DIR = Path(__file__).parent / "config"


# ── config loaders ────────────────────────────────────────────────────────────

def _load_agents() -> dict:
    return yaml.safe_load((_CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))


def _orchestrator_prompt(inputs: dict[str, Any]) -> str:
    agents = _load_agents()
    template = (_CONFIG_DIR / "orchestrator.md").read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}
    ctx.update({
        "analyzer_prompt": agents["patch_analyzer"]["system_prompt"].format_map(ctx),
        "qa_prompt":       agents["analyzer_qa"]["system_prompt"].format_map(ctx),
        "adapter_prompt":  agents["code_adapter"]["system_prompt"].format_map(ctx),
        "reviewer_prompt": agents["code_reviewer"]["system_prompt"].format_map(ctx),
    })
    return template.format_map(ctx)


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_claude_code_adapter(inputs: dict[str, Any]) -> AdaptResult:
    proc = subprocess.Popen(
        [
            "claude",
            "-p", _orchestrator_prompt(inputs),
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        _print_event(line)
    proc.wait()

    return _parse_result("".join(lines))


# ── event printer ─────────────────────────────────────────────────────────────

def _print_event(line: str) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    t = ev.get("type")

    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                print(block.get("text", ""), end="", flush=True)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp  = block.get("input", {})
                if name == "Agent":
                    print(f"\n{'━'*60}", flush=True)
                    print(f"▶ subagent [{inp.get('subagent_type', '?')}] starting", flush=True)
                    print(f"{'━'*60}", flush=True)
                else:
                    brief = json.dumps(inp, ensure_ascii=False)[:200]
                    print(f"\n[{name}] ← {brief}", flush=True)

    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            texts = (
                [c["text"] for c in content if c.get("type") == "text" and c.get("text")]
                if isinstance(content, list)
                else ([content] if isinstance(content, str) and content else [])
            )
            for text in texts:
                print(f"\n{'─'*60}\n◀ tool result:\n{text}\n{'─'*60}\n", flush=True)

    elif t == "result" and ev.get("is_error"):
        print(f"\n[error] {ev.get('result', '')}", flush=True)


# ── result parser ─────────────────────────────────────────────────────────────

def _parse_result(jsonl: str) -> AdaptResult:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        elif ev.get("type") == "result":
            text_parts.append(ev.get("result", ""))

    full_text = "\n".join(text_parts)
    matches = re.findall(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if matches:
        try:
            return AdaptResult(**json.loads(matches[-1]))
        except (json.JSONDecodeError, TypeError):
            pass
    return AdaptResult(step_summary=full_text[-4000:] if full_text else "")
