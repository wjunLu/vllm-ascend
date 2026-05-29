"""OpenCode team adapter — spawns team lead via `opencode run` subprocess.

The team lead uses TeamCreate + Agent tools to build a team of 4 specialists.
All JSON events are printed to console and logged to step_dir.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_PROMPT_PATH = Path(__file__).parent / "prompt.md"

_TIMEOUT_MINUTES = 30
_STALE_SECONDS = 300


# ── prompt loader ─────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}
    return template.format_map(ctx)


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_opencode_adapter(inputs: dict[str, Any]) -> AdaptResult:
    prompt = _build_prompt(inputs)
    step_dir = inputs.get("step_dir", "")
    log_path = Path(step_dir) / "opencode.log" if step_dir else None
    raw_path = Path(step_dir) / "opencode_raw.jsonl" if step_dir else None
    stderr_path = Path(step_dir) / "opencode_stderr.log" if step_dir else None

    print(f"\n{'═'*60}")
    print("TEAM LEAD PROMPT:")
    print(f"{'═'*60}")
    print(prompt)
    print(f"{'═'*60}\n")

    if log_path:
        log_path.write_text(
            f"{'═'*60}\nTEAM LEAD PROMPT:\n{'═'*60}\n{prompt}\n{'═'*60}\n\n",
            encoding="utf-8",
        )
    if raw_path:
        raw_path.write_text("")

    proc = subprocess.Popen(
        [
            "opencode", "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    killed = False

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    print(f"\n[opencode] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process", flush=True)
                    proc.kill()
                    killed = True
                    break
                if now - last_output_time > _STALE_SECONDS:
                    print(f"\n[opencode] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process", flush=True)
                    proc.kill()
                    killed = True
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

    proc.wait(timeout=10)

    if killed:
        if stderr_path and stderr_path.exists():
            stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            print(f"\n[opencode] stderr tail:\n{stderr_content}", flush=True)
        return AdaptResult(step_summary="opencode process killed due to timeout")

    return _parse_result("".join(state.lines))


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


# ── result parser ─────────────────────────────────────────────────────────────

def _parse_result(jsonl: str) -> AdaptResult:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            text_parts.append(ev.get("part", {}).get("text", ""))

    full_text = "\n".join(text_parts)
    matches = re.findall(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if matches:
        try:
            return AdaptResult(**json.loads(matches[-1]))
        except (json.JSONDecodeError, TypeError):
            pass
    return AdaptResult(step_summary=full_text[-4000:] if full_text else "")
