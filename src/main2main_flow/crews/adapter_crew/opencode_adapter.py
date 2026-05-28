"""OpenCode-based replacement for AdapterCrew.

Spawns `opencode run` as a subprocess with a multi-agent orchestrator prompt.
The main agent delegates to patch_analyzer → code_adapter → code_reviewer subagents,
then returns a JSON-serializable AdaptResult.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


def run_opencode_adapter(inputs: dict[str, Any]) -> AdaptResult:
    prompt = _build_prompt(inputs)
    project_root = Path(__file__).parent.parent.parent.parent.parent

    result = subprocess.run(
        [
            "opencode", "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            prompt,
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )

    if result.returncode != 0:
        print(f"[opencode] stderr: {result.stderr[-2000:]}")

    return _parse_result(result.stdout)


def _build_prompt(inputs: dict[str, Any]) -> str:
    mode = inputs.get("mode", "adapt")
    step_id = inputs.get("step_id", "")
    patch_path = inputs.get("patch_path", "")
    changed_files_path = inputs.get("changed_files_path", "")
    ascend_path = inputs.get("ascend_path", "")
    vllm_path = inputs.get("vllm_path", "")
    release_tag = inputs.get("release_tag", "")
    reference_dir = inputs.get("reference_dir", "")
    error_logs: list[str] = json.loads(inputs.get("error_logs", "[]"))

    if mode == "fix":
        error_section = "\n".join(f"  - {p}" for p in error_logs)
        task_description = f"""\
── FIX MODE ──────────────────────────────────────────────────────────────────
Error logs to diagnose:
{error_section}

Read each log file above using read_file tool to get full error details.
Read {reference_dir}/diagnosis-guide.md for error type → fix pattern mapping.
Read {reference_dir}/error-pattern-examples.md for concrete fix examples.
"""
    else:
        task_description = f"""\
── ADAPT MODE ────────────────────────────────────────────────────────────────
Upstream patch:      {patch_path}
Changed files list:  {changed_files_path}
Release tag:         {release_tag}

Read {reference_dir}/adapt-guide.md first — it contains the Key Areas table,
File Mapping table, and step-by-step instructions. Follow them exactly.
"""

    return f"""\
You are the orchestrator for adapting vllm-ascend to upstream vLLM changes (step {step_id}).

REPOSITORIES:
  vllm:         {vllm_path}
  vllm-ascend:  {ascend_path}
  reference:    {reference_dir}

{task_description}

YOUR WORKFLOW — use the Task tool to delegate in order:

1. Spawn subagent "patch_analyzer" with:
   - The task description above
   - Instruction to read the patch and reference guides
   - Ask for: subsystems touched, vllm-ascend files affected, change plan, version guard assessment

2. Spawn subagent "analyzer_qa" with:
   - The patch_analyzer's full output as context
   - The patch at {patch_path} and changed files at {changed_files_path} for cross-checking
   - Reference dir: {reference_dir}
   - If it returns REJECTED, go back to step 1 with the rejection feedback and retry once.

3. Spawn subagent "code_adapter" with:
   - The approved patch_analyzer output as context
   - The same inputs (ascend_path, patch_path, release_tag, reference_dir)
   - Instruction to apply all required changes and run: git -C {ascend_path} diff HEAD

4. Spawn subagent "code_reviewer" with:
   - The patch_analyzer's plan and code_adapter's diff as context
   - ascend_path: {ascend_path}, release_tag: {release_tag}
   - Instruction to verify all changes are correct and complete

5. Collect the reviewer's JSON output and return it as your final answer verbatim.

The reviewer's output must be a JSON block in this format:
```json
{{
  "modified_files": ["vllm_ascend/foo.py"],
  "is_noop": false,
  "step_summary": "..."
}}
```
"""


def _parse_result(jsonl_output: str) -> AdaptResult:
    text_parts: list[str] = []
    for line in jsonl_output.strip().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "text":
                text_parts.append(event.get("text", ""))
        except json.JSONDecodeError:
            continue

    full_text = "".join(text_parts)

    # Extract the last JSON block from the output
    matches = re.findall(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if matches:
        try:
            data = json.loads(matches[-1])
            return AdaptResult(**data)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: return summary with raw text
    return AdaptResult(step_summary=full_text[-4000:] if full_text else "")
