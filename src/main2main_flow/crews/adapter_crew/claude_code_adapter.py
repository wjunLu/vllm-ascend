"""Claude Code CLI adapter replacing CrewAI / OpenCode for the adapter crew.

Spawns `claude -p` as a subprocess. The orchestrator uses Claude Code's
built-in Agent tool to run subagents in sequence with iterative feedback:
  patch_analyzer ↔ analyzer_qa  (up to 3 rounds)
  code_adapter   ↔ code_reviewer (up to 3 rounds)

Output streams in real time; subagent boundaries are highlighted.
Each subagent's output is archived to the step directory.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ── subagent system prompts (loaded from .opencode/agents/*.md) ───────────────

_AGENTS_DIR = Path(__file__).parent / ".opencode" / "agents"


def _load_agent_prompt(name: str) -> str:
    """Return the body of an agent .md file (strips YAML frontmatter)."""
    path = _AGENTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    # strip --- frontmatter ---
    if text.startswith("---"):
        end = text.index("---", 3)
        text = text[end + 3:].lstrip()
    return text


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_claude_code_adapter(inputs: dict[str, Any]) -> AdaptResult:
    prompt = _build_prompt(inputs)

    proc = subprocess.Popen(
        [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
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

    # assistant text / tool use
    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                print(block.get("text", ""), end="", flush=True)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name == "Agent":
                    agent_type = inp.get("subagent_type", "?")
                    print(f"\n{'━'*60}", flush=True)
                    print(f"▶ subagent [{agent_type}] starting", flush=True)
                    print(f"{'━'*60}", flush=True)
                else:
                    brief = json.dumps(inp, ensure_ascii=False)[:200]
                    print(f"\n[{name}] ← {brief}", flush=True)

    # tool results (includes subagent output)
    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            # find the matching tool_use name from prior assistant turn
            # Claude Code embeds it in tool_use_id; we show content directly
            content = block.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "text":
                        text = c.get("text", "")
                        if text:
                            print(f"\n{'─'*60}", flush=True)
                            print(f"◀ tool result:", flush=True)
                            print(text, flush=True)
                            print(f"{'─'*60}\n", flush=True)
            elif isinstance(content, str) and content:
                print(f"\n{'─'*60}", flush=True)
                print(f"◀ tool result:", flush=True)
                print(content, flush=True)
                print(f"{'─'*60}\n", flush=True)

    elif t == "result":
        if ev.get("is_error"):
            print(f"\n[error] {ev.get('result', '')}", flush=True)


# ── prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    mode = inputs.get("mode", "adapt")
    step_id = inputs.get("step_id", "")
    step_dir = inputs.get("step_dir", "")
    patch_path = inputs.get("patch_path", "")
    changed_files_path = inputs.get("changed_files_path", "")
    ascend_path = inputs.get("ascend_path", "")
    vllm_path = inputs.get("vllm_path", "")
    release_tag = inputs.get("release_tag", "")
    reference_dir = inputs.get("reference_dir", "")
    error_logs: list[str] = json.loads(inputs.get("error_logs", "[]"))

    analyzer_prompt = _load_agent_prompt("patch_analyzer")
    qa_prompt = _load_agent_prompt("analyzer_qa")
    adapter_prompt = _load_agent_prompt("code_adapter")
    reviewer_prompt = _load_agent_prompt("code_reviewer")

    if mode == "fix":
        error_section = "\n".join(f"  - {p}" for p in error_logs)
        task_context = f"""\
MODE: fix
Error logs:
{error_section}
Read {reference_dir}/diagnosis-guide.md and {reference_dir}/error-pattern-examples.md.
"""
    else:
        task_context = f"""\
MODE: adapt
Upstream patch:      {patch_path}
Changed files list:  {changed_files_path}
Release tag:         {release_tag}
Read {reference_dir}/adapt-guide.md first. Follow instructions exactly.
"""

    return f"""\
You are the orchestrator for adapting vllm-ascend to upstream vLLM changes (step {step_id}).

REPOSITORIES:
  vllm:         {vllm_path}
  vllm-ascend:  {ascend_path}
  reference:    {reference_dir}

ARCHIVE DIRECTORY: {step_dir}

TASK CONTEXT:
{task_context}

YOUR WORKFLOW — use the Agent tool to spawn subagents in sequence:

━━━ PHASE 1: Analysis + QA (up to 3 rounds) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For each round (max 3):
  a) Spawn Agent with subagent_type="claude", prompt=
     ---
     {analyzer_prompt}

     INPUTS: {task_context}
     ascend_path: {ascend_path}
     Prior QA feedback (if any): <insert rejection from previous round>
     ---
     Save output to {step_dir}/analysis.md (append with "## Round N" header).

  b) Spawn Agent with subagent_type="claude", prompt=
     ---
     {qa_prompt}

     INPUTS:
     patch_analyzer output: <insert output from step a>
     patch_path: {patch_path}
     changed_files_path: {changed_files_path}
     reference_dir: {reference_dir}
     ---
     Save output to {step_dir}/analysis_qa.md (append with "## Round N" header).

  c) If REJECTED → loop back to a) with the rejection feedback.
     If APPROVED or max rounds reached → proceed to Phase 2.

━━━ PHASE 2: Code Adaptation + Review (up to 3 rounds) ━━━━━━━━━━━━━━━━━━━━━

For each round (max 3):
  a) Spawn Agent with subagent_type="claude", prompt=
     ---
     {adapter_prompt}

     INPUTS:
     Analysis: <insert approved patch_analyzer output>
     ascend_path: {ascend_path}
     patch_path: {patch_path}
     release_tag: {release_tag}
     reference_dir: {reference_dir}
     Prior reviewer feedback (if any): <insert from previous round>
     ---
     Save output to {step_dir}/adaptation_log.md (append with "## Round N" header).

  b) Spawn Agent with subagent_type="claude", prompt=
     ---
     {reviewer_prompt}

     INPUTS:
     Analysis plan: <insert patch_analyzer output>
     Code changes: <insert code_adapter output>
     ascend_path: {ascend_path}
     release_tag: {release_tag}
     reference_dir: {reference_dir}
     ---
     Save output to {step_dir}/review.md (append with "## Round N" header).

  c) If reviewer finds issues → loop back to a) with feedback.
     If APPROVED or max rounds reached → output final JSON.

━━━ FINAL OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return the code_reviewer's JSON block verbatim:
```json
{{
  "modified_files": ["vllm_ascend/foo.py"],
  "is_noop": false,
  "step_summary": "..."
}}
```
"""


# ── result parser ─────────────────────────────────────────────────────────────

def _parse_result(jsonl: str) -> AdaptResult:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        elif t == "result":
            text_parts.append(ev.get("result", ""))

    full_text = "\n".join(text_parts)
    matches = re.findall(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if matches:
        try:
            return AdaptResult(**json.loads(matches[-1]))
        except (json.JSONDecodeError, TypeError):
            pass

    return AdaptResult(step_summary=full_text[-4000:] if full_text else "")
