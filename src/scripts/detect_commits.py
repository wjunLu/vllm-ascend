#!/usr/bin/env python3
"""Detect base and target vLLM commits for the main2main upgrade pipeline.

Data sources:
  - base_commit:   extracted from vllm-ascend/docs/source/conf.py
                   (the "main_vllm_commit" field in myst_substitutions)
  - compat_tag:    extracted from the same file ("main_vllm_tag")
  - target_commit: HEAD of the local vLLM repository

Side-effects:
  - Creates <workspace>/ and <workspace>/steps/ directories.
  - Writes <workspace>/detect.json.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from utils import run_git


def _extract_from_conf_py(ascend_path: Path) -> dict[str, str | None]:
    """Parse the pinned vLLM commit and compatibility tag.

    Tries the verified-commit file first (new format), then falls back to
    the hardcoded SHA in conf.py (old format).
    """
    verified_path = ascend_path / ".github" / "vllm-main-verified.commit"
    if verified_path.exists():
        base_commit = verified_path.read_text(encoding="utf-8").strip()
    else:
        conf_path = ascend_path / "docs" / "source" / "conf.py"
        if not conf_path.exists():
            print(f"Error: {conf_path} not found", file=sys.stderr)
            sys.exit(1)
        conf_text = conf_path.read_text(encoding="utf-8")
        commit_match = re.search(r'"main_vllm_commit":\s*"([0-9a-f]{40})"', conf_text)
        if not commit_match:
            print("Error: could not find main_vllm_commit in conf.py", file=sys.stderr)
            sys.exit(1)
        base_commit = commit_match.group(1)

    conf_path = ascend_path / "docs" / "source" / "conf.py"
    tag_match = re.search(r'"main_vllm_tag":\s*"([^"]+)"', conf_path.read_text(encoding="utf-8"))
    return {
        "base_commit": base_commit,
        "compat_tag": tag_match.group(1) if tag_match else None,
    }


def _get_repo_head(repo_path: Path) -> str:
    """Return the HEAD commit SHA of a local git repository."""
    if not repo_path.exists():
        print(f"Error: path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    return run_git(repo_path, "rev-parse", "HEAD").strip()


def detect(
    vllm_path: Path,
    ascend_path: Path,
    target_commit: str | None = None,
) -> dict:
    """Run drift detection and write <workspace>/detect.json.

    Returns the detect result dict.
    """
    conf = _extract_from_conf_py(ascend_path)
    target = target_commit if target_commit else _get_repo_head(vllm_path)

    result = {
        "base_commit": conf["base_commit"],
        "target_commit": target,
        "compat_tag": conf["compat_tag"],
    }

    return result, conf["base_commit"] != target
