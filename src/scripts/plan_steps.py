#!/usr/bin/env python3
"""Deterministic step planner for the main2main upgrade pipeline.

Splits a range of upstream vLLM commits into ordered steps based on changed
lines in vllm/ source files. Commits that do not touch vllm/ are skipped.

Algorithm:
  1. git log --reverse base..target → ordered commit list
  2. For each commit, git diff-tree --numstat → vllm/ changed lines
  3. Keep only commits that touch vllm/; skip others
  4. Commits accumulate into a step until vllm_changed_lines > LINE_BUDGET
     or the step reaches the commit-count budget
  5. A single commit with vllm_changed_lines > LINE_BUDGET becomes its own step

Output:
  - <workspace>/steps.json  — machine-readable plan
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from utils import run_git

LINE_BUDGET = 1000
BASE_LINE_BUDGET = 1000
BASE_COMMIT_COUNT_BUDGET = 10


def _commit_count_budget(line_budget: int = LINE_BUDGET) -> int:
    return max(1, round(BASE_COMMIT_COUNT_BUDGET * math.sqrt(line_budget / BASE_LINE_BUDGET)))


def _list_commits(repo: Path, base: str, target: str) -> list[dict[str, str]]:
    log_output = run_git(
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


def _vllm_lines_for_commit(repo: Path, sha: str) -> int:
    output = run_git(repo, "diff-tree", "--no-commit-id", "-r", "--numstat", sha, "--", ":(top)vllm/")
    total = 0
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
            total += added + deleted
    return total


def _make_step(index: int, commits: list[dict[str, str]], start: str, lines: int) -> dict[str, Any]:
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": list(commits),
        "commit_count": len(commits),
        "start_commit": start,
        "end_commit": commits[-1]["sha"],
        "vllm_changed_lines": lines,
        "line_budget": LINE_BUDGET,
        "commit_count_budget": _commit_count_budget(),
    }


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    base_commit: str,
) -> list[dict[str, Any]]:
    eligible = [c for c in commits if lines_per_commit.get(c["sha"], 0) > 0]

    steps: list[dict[str, Any]] = []
    step_commits: list[dict[str, str]] = []
    step_lines = 0
    start = base_commit

    for commit in eligible:
        lines = lines_per_commit[commit["sha"]]

        if lines > LINE_BUDGET:
            if step_commits:
                steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))
                start = steps[-1]["end_commit"]
                step_commits = []
                step_lines = 0
            steps.append(_make_step(len(steps) + 1, [commit], start, lines))
            start = steps[-1]["end_commit"]
            continue

        if step_lines + lines > LINE_BUDGET or len(step_commits) >= _commit_count_budget():
            steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))
            start = steps[-1]["end_commit"]
            step_commits = []
            step_lines = 0

        step_commits.append(commit)
        step_lines += lines

    if step_commits:
        steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))

    return steps


def _enrich_steps_with_diff(vllm_path: Path, steps: list[dict[str, Any]]) -> None:
    for step in steps:
        step["upstream_patch"] = run_git(
            vllm_path, "diff",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", ":(top)vllm/",
        )
        changed_files = run_git(
            vllm_path, "diff", "--name-only",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", ":(top)vllm/",
        )
        step["changed_files"] = changed_files
        step["files_changed"] = sorted(f for f in changed_files.strip().splitlines() if f)


def run_plan(vllm_path: Path, base_commit: str, target_commit: str) -> dict[str, Any]:
    commits = _list_commits(vllm_path, base_commit, target_commit)
    lines_per_commit = {c["sha"]: _vllm_lines_for_commit(vllm_path, c["sha"]) for c in commits}

    steps = _plan_steps(commits, lines_per_commit, base_commit)
    _enrich_steps_with_diff(vllm_path, steps)
    return {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "total_commits": sum(s["commit_count"] for s in steps),
        "steps": steps,
    }