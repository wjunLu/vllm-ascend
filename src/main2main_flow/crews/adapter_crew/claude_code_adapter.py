"""Claude Code team adapter — spawns team lead via `claude -p` subprocess.

The team lead uses TeamCreate + Agent tools to build a team of 4 specialists.
All stream-json events are printed to console and logged to step_dir.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


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

def run_claude_code_adapter(inputs: dict[str, Any]) -> AdaptResult:
    prompt = _build_prompt(inputs)
    step_dir = inputs.get("step_dir", "")
    log_path = Path(step_dir) / "claude_code.log" if step_dir else None
    raw_path = Path(step_dir) / "claude_code_raw.jsonl" if step_dir else None

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
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )

    state = _EventState()
    assert proc.stdout is not None
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    raw_fh = raw_path.open("a", encoding="utf-8") if raw_path else None
    try:
        for line in proc.stdout:
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
    proc.wait()

    return _parse_result("".join(state.lines))


# ── event state ───────────────────────────────────────────────────────────────

class _EventState:
    """Tracks tool_use id → agent name so tool_results can be attributed."""
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.tool_to_agent: dict[str, str] = {}
        # Map teammate names to their Agent tool_use id
        self.agent_ids: set[str] = set()

    def tag_tool(self, block: dict) -> str:
        name = block.get("name", "")
        if name == "Agent":
            inp = block.get("input", {})
            agent_name = inp.get("name", "") or inp.get("subagent_type", "?")
            self.tool_to_agent[block.get("id", "")] = agent_name
            self.agent_ids.add(agent_name)
            return f"[Team: {agent_name}]"
        return f"[TeamLead: {name}]"

    def tag_result(self, block: dict) -> str:
        agent = self.tool_to_agent.get(block.get("tool_use_id", ""), "")
        if agent:
            return f"[Team: {agent}]"
        return "[TeamLead]"

    def resolve_agent(self, block: dict) -> str:
        """Best-effort agent name for a tool_result."""
        return self.tool_to_agent.get(block.get("tool_use_id", ""), "TeamLead")


# ── content extractor ─────────────────────────────────────────────────────────

def _extract_texts(content: Any) -> list[str]:
    """Extract readable text from tool_result content, which can be a string
    or a list of content blocks."""
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        return [c["text"] for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
    return []


# ── event printer ─────────────────────────────────────────────────────────────

def _print_event(line: str, state: _EventState) -> None:
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
                tag = state.tag_tool(block)
                name = block.get("name", "")
                if name == "Agent":
                    inp = block.get("input", {})
                    agent_name = inp.get("name", "") or inp.get("subagent_type", "?")
                    print(f"\n{'━'*60}", flush=True)
                    print(f"▶ {tag} starting ({agent_name})", flush=True)
                    print(f"{'━'*60}", flush=True)
                else:
                    brief = json.dumps(block.get("input", {}), ensure_ascii=False)[:200]
                    print(f"\n{tag} ← {brief}", flush=True)

    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "tool_result":
                agent = state.resolve_agent(block)
                for text in _extract_texts(block.get("content", "")):
                    print(f"\n{'─'*60}\n[Team: {agent}] output:\n{text}\n{'─'*60}\n", flush=True)
            elif btype == "text":
                # Messages from teammates arrive as text blocks in user events
                sender = ev.get("sender", "") or block.get("sender", "") or "teammate"
                text = block.get("text", "")
                if text:
                    print(f"\n{'─'*60}\n[Team: {sender}] message:\n{text}\n{'─'*60}\n", flush=True)

    elif t == "result" and ev.get("is_error"):
        print(f"\n[error] {ev.get('result', '')}", flush=True)


# ── event logger ─────────────────────────────────────────────────────────────

def _log_event(line: str, state: _EventState, fh: Any) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        fh.write(line)
        return

    t = ev.get("type")

    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                fh.write(block.get("text", ""))
            elif btype == "tool_use":
                tag = state.tag_tool(block)
                inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                fh.write(f"\n{tag} ← {inp[:500]}\n")

    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "tool_result":
                agent = state.resolve_agent(block)
                for text in _extract_texts(block.get("content", "")):
                    fh.write(f"\n{'─'*60}\n[Team: {agent}]\n{text}\n{'─'*60}\n")
            elif btype == "text":
                sender = ev.get("sender", "") or block.get("sender", "") or "teammate"
                text = block.get("text", "")
                if text:
                    fh.write(f"\n{'─'*60}\n[Team: {sender}] message:\n{text}\n{'─'*60}\n")

    elif t == "result":
        result_text = ev.get("result", "")
        is_error = ev.get("is_error", False)
        prefix = "[TeamLead] FINAL RESULT (ERROR):" if is_error else "[TeamLead] FINAL RESULT:"
        fh.write(f"\n{'═'*60}\n{prefix}\n{result_text}\n{'═'*60}\n")

    fh.flush()


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
