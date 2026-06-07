"""OpenCode team adapter — spawns team lead via `opencode run` subprocess.

The team lead uses TeamCreate + Agent tools to build a team of 4 specialists.
All JSON events are printed to console and logged to step_dir.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_PROMPT_PATH = Path(__file__).parent / "prompt.md"
_REFERENCE_DIR = Path(__file__).parent.parent / "reference"

_TIMEOUT_MINUTES = 30
_STALE_SECONDS = 300
_MAX_STALE_RETRIES = 3

# ── prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}

    mode = inputs.get("mode", "adapt")
    code_structure = _load_ref("code-structure-guide.md")
    if mode == "fix":
        ref_content = _load_ref("diagnosis-guide.md") + "\n\n" + _load_ref("error-pattern-examples.md") + "\n\n" + code_structure
    else:
        ref_content = _load_ref("adapt-guide.md") + "\n\n" + code_structure

    ctx["reference_content"] = ref_content
    return template.format_map(ctx)


def _load_ref(filename: str) -> str:
    path = _REFERENCE_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _build_continue_prompt(base_prompt: str, inputs: dict[str, Any], retry: int) -> str:
    step_dir = inputs.get("step_dir", "")
    return f"""Continue the adaptation task for step {inputs.get('step_id', '')}.

The previous opencode run produced no output for {_STALE_SECONDS} seconds and
was terminated. This is continuation retry {retry}/{_MAX_STALE_RETRIES}.

Do not start from scratch. The current vllm-ascend working tree may already
contain partial code changes from the previous attempt. These files may also
contain partial results:

  - {step_dir}/analysis.md
  - {step_dir}/review.md
  - {step_dir}/step_summary.md
  - {step_dir}/opencode.log
  - {step_dir}/opencode_raw.jsonl

First inspect the existing changes and generated files. Reuse prior work, then
continue any unfinished adaptation, static review, and step_summary.md updates.
Continue to follow the original task requirements below.

━━━ ORIGINAL TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{base_prompt}
"""


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_opencode_adapter(inputs: dict[str, Any]) -> AdaptResult:
    base_prompt = _build_prompt(inputs)
    prompt = base_prompt
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path = step_path / "opencode.log" if step_path else None
    raw_path = step_path / "opencode_raw.jsonl" if step_path else None
    stderr_path = step_path / "opencode_stderr.log" if step_path else None

    if log_path:
        log_path.write_text("")
    if raw_path:
        raw_path.write_text("")
    if stderr_path:
        stderr_path.write_text("")

    all_lines: list[str] = []
    last_reason: _StopReason | None = None

    for attempt in range(_MAX_STALE_RETRIES + 1):
        _print_prompt(prompt, attempt)
        if log_path:
            _log_prompt(prompt, attempt, log_path)

        lines, reason = _run_once(prompt, log_path, raw_path, stderr_path)
        all_lines.extend(lines)
        last_reason = reason

        if reason is None:
            break

        if reason == "stale_timeout" and attempt < _MAX_STALE_RETRIES:
            retry = attempt + 1
            print(f"\n[opencode] retrying after stale timeout ({retry}/{_MAX_STALE_RETRIES})", flush=True)
            prompt = _build_continue_prompt(base_prompt, inputs, retry)
            continue

        if stderr_path and stderr_path.exists():
            stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            if stderr_content:
                print(f"\n[opencode] stderr tail:\n{stderr_content}", flush=True)
        break

    result = _build_result(step_path, inputs.get("ascend_path", ""), "".join(all_lines))
    if last_reason and not result.step_summary:
        result.step_summary = f"opencode process stopped due to {last_reason}"
    return result


_StopReason = Literal["stale_timeout", "total_timeout"]


def _print_prompt(prompt: str, attempt: int) -> None:
    title = "TEAM LEAD PROMPT" if attempt == 0 else f"TEAM LEAD CONTINUE PROMPT #{attempt}"
    print(f"\n{'═'*60}")
    print(title)
    print(f"{'═'*60}")
    print(prompt)
    print(f"{'═'*60}\n")


def _log_prompt(prompt: str, attempt: int, log_path: Path) -> None:
    title = "TEAM LEAD PROMPT" if attempt == 0 else f"TEAM LEAD CONTINUE PROMPT #{attempt}"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{'═'*60}\n{title}:\n{'═'*60}\n{prompt}\n{'═'*60}\n\n")


def _run_once(
    prompt: str,
    log_path: Path | None,
    raw_path: Path | None,
    stderr_path: Path | None,
) -> tuple[list[str], _StopReason | None]:
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    proc = subprocess.Popen(
        [
            "opencode", "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    lines_queue: queue.Queue[str | None] = queue.Queue()

    def _stdout_reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines_queue.put(line)
        lines_queue.put(None)

    reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
    reader_thread.start()

    state = _EventState()
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    raw_fh = raw_path.open("a", encoding="utf-8") if raw_path else None

    deadline = time.monotonic() + _TIMEOUT_MINUTES * 60
    last_output_time = time.monotonic()
    stop_reason: _StopReason | None = None

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    print(f"\n[opencode] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process", flush=True)
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > _STALE_SECONDS:
                    print(f"\n[opencode] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process", flush=True)
                    proc.kill()
                    stop_reason = "stale_timeout"
                    break
                continue

            if line is None:
                break

            last_output_time = time.monotonic()
            state.lines.append(line)
            if raw_fh:
                raw_fh.write(line)
            _print_event(line, state)
            if log_fh:
                _log_event(line, state, log_fh)
    finally:
        if log_fh:
            log_fh.close()
        if raw_fh:
            raw_fh.close()
        if stderr_fh:
            stderr_fh.close()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stop_reason = stop_reason or "total_timeout"
        proc.wait(timeout=10)

    return state.lines, stop_reason


# ── event state ───────────────────────────────────────────────────────────────

class _EventState:
    """Tracks callID → tool name for attributing output."""
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._tool_by_call: dict[str, str] = {}


# ── event printer ─────────────────────────────────────────────────────────────

def _print_event(line: str, state: _EventState) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            print(text, end="", flush=True)

    elif t == "tool_use":
        tool = part.get("tool", "")
        call_id = part.get("callID", "")
        st = part.get("state", {})
        status = st.get("status", "")
        inp = st.get("input", {})

        if status == "pending":
            state._tool_by_call[call_id] = tool
            if tool == "Agent":
                agent_name = inp.get("name", "") or inp.get("subagent_type", "?")
                print(f"\n{'━'*60}", flush=True)
                print(f"▶ [Team: {agent_name}] spawning ({tool})", flush=True)
                print(f"{'━'*60}", flush=True)
            elif tool == "TeamCreate":
                team_name = inp.get("team_name", "?")
                print(f"\n▶ [TeamLead] creating team '{team_name}'", flush=True)
            elif tool == "SendMessage":
                to = inp.get("to", "?")
                summary = inp.get("summary", "")
                print(f"\n▶ [TeamLead] → {to}: {summary}", flush=True)
            else:
                brief = json.dumps(inp, ensure_ascii=False)[:200]
                print(f"\n[TeamLead: {tool}] ← {brief}", flush=True)

        elif status == "completed":
            output = st.get("output", "")
            if output:
                agent = state._tool_by_call.get(call_id, "")
                label = f"Team: {agent}" if agent else "TeamLead"
                # Truncate very long outputs for display
                display = output if len(output) <= 3000 else output[:3000] + "\n... [truncated]"
                print(f"\n{'─'*60}\n[{label}] output:\n{display}\n{'─'*60}", flush=True)


# ── event logger ─────────────────────────────────────────────────────────────

def _log_event(line: str, state: _EventState, fh: Any) -> None:  # noqa: ARG001
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        fh.write(line)
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            fh.write(text)

    elif t == "tool_use":
        tool = part.get("tool", "")
        st = part.get("state", {})
        inp = json.dumps(st.get("input", {}), ensure_ascii=False)
        fh.write(f"\n[TeamLead: {tool}] ← {inp[:500]}\n")
        output = st.get("output", "")
        if output:
            fh.write(f"{'─'*60}\n[output]\n{output[:4000]}\n{'─'*60}\n")

    fh.flush()


# ── result builder ─────────────────────────────────────────────────────────────

def _build_result(step_dir: Path | None, ascend_path: str, jsonl: str) -> AdaptResult:
    summary = ""
    if step_dir:
        summary_path = step_dir / "step_summary.md"
        if summary_path.exists():
            summary = summary_path.read_text(encoding="utf-8")

    if not summary:
        summary = _text_from_jsonl(jsonl)[-4000:]

    modified_files = _modified_files(ascend_path)
    return AdaptResult(
        modified_files=modified_files,
        is_noop=not modified_files,
        step_summary=summary,
    )


def _text_from_jsonl(jsonl: str) -> str:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            text_parts.append(ev.get("part", {}).get("text", ""))
    return "\n".join(text_parts)


def _modified_files(ascend_path: str) -> list[str]:
    if not ascend_path:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=ascend_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [line for line in result.stdout.splitlines() if line]
