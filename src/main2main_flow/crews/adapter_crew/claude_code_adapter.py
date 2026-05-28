"""Claude Code CLI adapter for the adapter crew.

架构说明
--------
这里使用的是 subagent 模式，不是 CrewAI 的 team/crew 模式。

  subagent 模式：
    由一个 orchestrator（协调者）统一调度所有 agent。
    每个 subagent 是独立调用，运行完毕后返回结果，彼此之间不直接通信。
    orchestrator 持有全局上下文，负责在 agent 之间传递信息。

  team/crew 模式（未使用）：
    多个 agent 并行存在，可以直接互相发消息。

协调者与子 agent 的交互逻辑
----------------------------
1. 本模块用 subprocess 启动 `claude -p <orchestrator_prompt>`，
   这个 claude 进程就是协调者（orchestrator）。

2. 协调者的 prompt 来自 config/orchestrator.md，里面描述了完整的工作流：
   Phase 1 — 分析循环（最多 3 轮）：
     a) 协调者通过 Agent tool 启动 patch_analyzer subagent，传入上游 patch。
     b) 协调者通过 Agent tool 启动 analyzer_qa subagent，传入 patch_analyzer 的输出。
     c) 若 analyzer_qa 返回 REJECTED，协调者把 QA 反馈逐字注入下一轮
        patch_analyzer 的 prompt，重复直到 APPROVED 或达到 3 轮上限。

   Phase 2 — 适配循环（最多 3 轮）：
     a) 协调者通过 Agent tool 启动 code_adapter subagent，传入已审批的分析结果。
     b) 协调者通过 Agent tool 启动 code_reviewer subagent，传入 code_adapter 的输出。
     c) 若 reviewer 返回 "approved": false，协调者把 issues 列表注入下一轮
        code_adapter 的 prompt，重复直到 approved 或达到 3 轮上限。

3. 所有 subagent 的输出由协调者归档到 step_dir 下的 md 文件中。
4. 流程结束后，由协调者综合全程信息写 step_summary，输出最终 JSON。

5. Claude Code 以 stream-json 格式输出 JSONL 事件流，本模块解析并实时打印：
   - assistant 事件：协调者的思考文本和工具调用（含 Agent tool 启动 subagent）
   - user 事件：工具返回结果（含 subagent 执行完毕后的输出）
   - result 事件：整个 claude 进程的最终输出

Configuration lives in config/:
  agents.yaml      — 每个 subagent 的 system prompt（角色 + 任务 + 输出格式）
  orchestrator.md  — 协调者的工作流模板（Phase 1 / Phase 2 / Final Output）
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
    prompt = _orchestrator_prompt(inputs)
    step_dir = inputs.get("step_dir", "")
    log_path = Path(step_dir) / "claude_code.log" if step_dir else None

    # print prompt for debugging
    print(f"\n{'═'*60}")
    print("ORCHESTRATOR PROMPT:")
    print(f"{'═'*60}")
    print(prompt)
    print(f"{'═'*60}\n")

    if log_path:
        log_path.write_text(
            f"{'═'*60}\nORCHESTRATOR PROMPT:\n{'═'*60}\n{prompt}\n{'═'*60}\n\n",
            encoding="utf-8",
        )

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

    lines: list[str] = []
    assert proc.stdout is not None
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    try:
        for line in proc.stdout:
            lines.append(line)
            _print_event(line)
            if log_fh:
                _log_event(line, log_fh)
    finally:
        if log_fh:
            log_fh.close()
    proc.wait()

    return _parse_result("".join(lines))


# ── event printer + logger ───────────────────────────────────────────────────

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


# ── event logger ─────────────────────────────────────────────────────────────

def _log_event(line: str, fh: Any) -> None:
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
                name = block.get("name", "")
                inp  = block.get("input", {})
                if name == "Agent":
                    fh.write(f"\n{'━'*60}\n▶ subagent [{inp.get('subagent_type','?')}] starting\n{'━'*60}\n")
                else:
                    fh.write(f"\n[{name}] ← {json.dumps(inp, ensure_ascii=False)[:500]}\n")

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
                fh.write(f"\n{'─'*60}\n◀ tool result:\n{text}\n{'─'*60}\n")

    elif t == "result":
        fh.write(f"\n{'═'*60}\nFINAL RESULT:\n{ev.get('result','')}\n{'═'*60}\n")

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
