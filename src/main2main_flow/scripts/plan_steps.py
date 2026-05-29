#!/usr/bin/env python3
"""Deterministic step planner for the main2main upgrade pipeline.

Splits a range of upstream vLLM commits into ordered steps based on changed
lines in vllm/ source files. Commits that do not touch vllm/ are skipped.

Algorithm:
  1. git rev-list --reverse base..target → ordered commit list
  2. For each commit, git diff-tree --numstat → changed files + lines
  3. Keep only files under vllm/; skip commits with no vllm/ changes
  4. Commits accumulate into a step until vllm_changed_lines > LINE_BUDGET
     or the step reaches the sublinear commit-count budget
  5. A single commit with vllm_changed_lines > LINE_BUDGET becomes its own step

Output:
  - <workspace>/steps.json  — machine-readable plan
  - stdout (from main()): JSON summary
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from main2main_flow.utils import WORKSPACE_DIR

LINE_BUDGET = 1000
BASE_LINE_BUDGET = 1000
BASE_COMMIT_COUNT_BUDGET = 10

REQUIREMENTS_FILES = {
    "pyproject.toml",
    "setup.py",
    "requirements/common.txt",
}
REQUIREMENTS_PREFIXES = ("requirements/build/",)


def _commit_count_budget(line_budget: int = LINE_BUDGET) -> int:
    return max(1, round(BASE_COMMIT_COUNT_BUDGET * math.sqrt(line_budget / BASE_LINE_BUDGET)))


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _list_commits(repo: Path, base: str, target: str) -> list[dict[str, str]]:
    """Return commits in chronological order (oldest first)."""
    log_output = _run_git(
        repo, "log", "--reverse", "--format=%H%x1f%s", f"{base}..{target}"
    )
    commits: list[dict[str, str]] = []
    for line in log_output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 1)
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
        })
    return commits


def _numstat(repo: Path, sha: str) -> list[dict[str, Any]]:
    """Return per-file change stats for a single commit."""
    output = _run_git(repo, "diff-tree", "--no-commit-id", "-r", "--numstat", sha)
    files: list[dict[str, Any]] = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = int(parts[0]) if parts[0] != "-" else 0
        deleted = int(parts[1]) if parts[1] != "-" else 0
        files.append({
            "path": parts[2],
            "added": added,
            "deleted": deleted,
            "lines": added + deleted,
        })
    return files


def _classify_file(filepath: str) -> str:
    if filepath in REQUIREMENTS_FILES or filepath.startswith(REQUIREMENTS_PREFIXES):
        return "requirements"
    if filepath.startswith("vllm/"):
        return "vllm"
    return "ignored"


def _commit_stats(files: list[dict[str, Any]]) -> dict[str, Any]:
    vllm_lines = 0
    vllm_files: list[str] = []

    for f in files:
        if f["path"].startswith("vllm/"):
            vllm_lines += f["lines"]
            vllm_files.append(f["path"])

    return {
        "vllm_changed_lines": vllm_lines,
        "files": vllm_files,
    }


def _plan_steps(
    commits: list[dict[str, str]],
    stats_per_commit: dict[str, dict[str, Any]],
    base_commit: str,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    current_commits: list[dict[str, str]] = []
    current_vllm_lines = 0
    current_files: list[str] = []

    def _flush(start: str) -> None:
        nonlocal current_commits, current_vllm_lines, current_files
        if not current_commits:
            return
        steps.append({
            "index": len(steps) + 1,
            "id": f"step-{len(steps) + 1}",
            "commits": list(current_commits),
            "commit_count": len(current_commits),
            "start_commit": start,
            "end_commit": current_commits[-1]["sha"],
            "vllm_changed_lines": current_vllm_lines,
            "line_budget": LINE_BUDGET,
            "commit_count_budget": _commit_count_budget(),
            "files_changed": sorted(set(current_files)),
        })
        current_commits = []
        current_vllm_lines = 0
        current_files = []

    prev_end = base_commit
    for commit in commits:
        st = stats_per_commit.get(commit["sha"], {})
        vllm_lines = st.get("vllm_changed_lines", 0)
        files = st.get("files", [])

        if vllm_lines == 0:
            continue

        if vllm_lines > LINE_BUDGET:
            _flush(prev_end)
            prev_end = steps[-1]["end_commit"] if steps else base_commit
            current_commits = [commit]
            current_vllm_lines = vllm_lines
            current_files = list(files)
            _flush(prev_end)
            prev_end = steps[-1]["end_commit"] if steps else base_commit
            continue

        if (
            current_vllm_lines + vllm_lines > LINE_BUDGET
            or len(current_commits) >= _commit_count_budget()
        ):
            _flush(prev_end)
            prev_end = steps[-1]["end_commit"] if steps else base_commit

        current_commits.append(commit)
        current_vllm_lines += vllm_lines
        current_files.extend(files)

    _flush(prev_end)
    return steps


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# main2main Step Plan",
        "",
        f"**Base:** `{plan['base_commit']}`",
        f"**Target:** `{plan['target_commit']}`",
        f"**Steps:** {len(plan['steps'])}  |  **Total vllm commits:** {sum(s['commit_count'] for s in plan['steps'])}",
        "",
    ]
    for step in plan["steps"]:
        lines.append(
            f"## {step['id']} (commits: {step['commit_count']}, "
            f"vllm: {step['vllm_changed_lines']} lines)"
        )
        lines.append("")
        lines.append(f"- Range: `{step['start_commit'][:8]}..{step['end_commit'][:8]}`")
        lines.append("")
        for c in step["commits"]:
            lines.append(f"  - `{c['sha'][:8]}` {c['subject']}")
        lines.append("")
    return "\n".join(lines)


def run_plan(vllm_path: Path, base_commit: str, target_commit: str) -> dict[str, Any]:
    """Build the step plan and write output files to <workspace>/.

    Returns the plan dict.
    """
    commits = _list_commits(vllm_path, base_commit, target_commit)
    stats_per_commit: dict[str, dict[str, Any]] = {}
    for c in commits:
        files = _numstat(vllm_path, c["sha"])
        stats_per_commit[c["sha"]] = _commit_stats(files)

    steps = _plan_steps(commits, stats_per_commit, base_commit)
    plan = {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "total_commits": sum(s["commit_count"] for s in steps),
        "steps": steps,
    }

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    (WORKSPACE_DIR / "steps.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )

    # Clean up step dirs from previous runs
    steps_root = WORKSPACE_DIR / "steps"
    if steps_root.exists():
        shutil.rmtree(steps_root)
    steps_root.mkdir(parents=True)

    return plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan upgrade steps for main2main pipeline."
    )
    parser.add_argument("--vllm-path", type=Path, required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--target-commit", required=True)
    args = parser.parse_args()

    plan = run_plan(args.vllm_path, args.base_commit, args.target_commit)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
