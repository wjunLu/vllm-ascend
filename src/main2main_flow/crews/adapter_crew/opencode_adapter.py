"""OpenCode-based replacement for AdapterCrew.

Spawns `opencode run` as a subprocess with a multi-agent orchestrator prompt.
The orchestrator delegates to subagents in sequence, with iterative feedback loops:
  patch_analyzer ↔ analyzer_qa  (up to 3 rounds)
  code_adapter   ↔ code_reviewer (up to 3 rounds)

Each subagent's output is archived to the step directory.
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
    adapter_crew_dir = Path(__file__).parent

    proc = subprocess.Popen(
        [
            "opencode", "run",
            "--dangerously-skip-permissions",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=None,   # stderr goes directly to terminal
        text=True,
        bufsize=1,
        cwd=str(adapter_crew_dir),
    )

    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        chunks.append(line)
    proc.wait()

    return _parse_result("".join(chunks))


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

    if mode == "fix":
        error_section = "\n".join(f"  - {p}" for p in error_logs)
        task_description = f"""\
── FIX MODE ──────────────────────────────────────────────────────────────────
Error logs to diagnose:
{error_section}

Read each log file above to get full error details.
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

ARCHIVE DIRECTORY (write every subagent output here):
  {step_dir}

{task_description}

YOUR WORKFLOW:

━━━ PHASE 1: Analysis + QA (up to 3 rounds) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Round loop (max 3):
  a) Spawn subagent "patch_analyzer" with the task description above.
     - Pass all inputs: mode, patch_path, changed_files_path, ascend_path,
       release_tag, reference_dir, and any QA rejection feedback from prior round.
     - After it responds, write its full output to: {step_dir}/analysis.md
       (append if file exists, prepend "## Round N\\n")

  b) Spawn subagent "analyzer_qa" with:
     - The patch_analyzer's full output
     - patch_path: {patch_path}, changed_files_path: {changed_files_path}
     - reference_dir: {reference_dir}
     - After it responds, write its full output to: {step_dir}/analysis_qa.md
       (append if file exists, prepend "## Round N\\n")

  c) If analyzer_qa returns REJECTED: feed the rejection back to patch_analyzer
     and repeat the round. If APPROVED or max rounds reached: proceed to Phase 2.

━━━ PHASE 2: Code Adaptation + Review (up to 3 rounds) ━━━━━━━━━━━━━━━━━━━━━

Round loop (max 3):
  a) Spawn subagent "code_adapter" with:
     - The approved patch_analyzer output as context
     - mode, ascend_path, patch_path, release_tag, reference_dir
     - Any reviewer feedback from prior round
     - After it responds, write its full output to: {step_dir}/adaptation_log.md
       (append if file exists, prepend "## Round N\\n")

  b) Spawn subagent "code_reviewer" with:
     - The patch_analyzer's plan and code_adapter's latest output
     - ascend_path: {ascend_path}, release_tag: {release_tag}
     - reference_dir: {reference_dir}
     - After it responds, write its full output to: {step_dir}/review.md
       (append if file exists, prepend "## Round N\\n")

  c) If code_reviewer finds issues: feed them back to code_adapter and repeat.
     If APPROVED or max rounds reached: proceed to final output.

━━━ FINAL OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return the code_reviewer's final JSON block verbatim as your answer:
```json
{{
  "modified_files": ["vllm_ascend/foo.py"],
  "is_noop": false,
  "step_summary": "..."
}}
```
"""


def _parse_result(output: str) -> AdaptResult:
    # Extract the last JSON block from the output
    matches = re.findall(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    if matches:
        try:
            data = json.loads(matches[-1])
            return AdaptResult(**data)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: return summary with raw text tail
    return AdaptResult(step_summary=output[-4000:] if output else "")
