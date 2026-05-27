#!/usr/bin/env python3
"""Detect base and target vLLM commits for the main2main upgrade pipeline.

Data sources:
  - base_commit:   extracted from vllm-ascend/docs/source/conf.py
                   (the "main_vllm_commit" field in myst_substitutions)
  - compat_tag:    extracted from the same file ("main_vllm_tag")
  - target_commit: HEAD of the local vLLM repository

Output JSON schema:
  {
    "base_commit":    "<40-char sha>",
    "target_commit":  "<40-char sha>",
    "compat_tag":     "<tag or null>",
    "has_drift":      <bool>
  }

Side-effects:
  - Creates /tmp/main2main/ and /tmp/main2main/steps/ directories.
  - Writes /tmp/main2main/detect.json.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def extract_from_conf_py(ascend_path: Path) -> dict[str, str | None]:
    """Parse conf.py for the pinned vLLM commit and compatibility tag."""
    conf_path = ascend_path / "docs" / "source" / "conf.py"
    if not conf_path.exists():
        print(f"Error: {conf_path} not found", file=sys.stderr)
        sys.exit(1)

    conf_text = conf_path.read_text(encoding="utf-8")

    commit_match = re.search(r'"main_vllm_commit":\s*"([0-9a-f]{40})"', conf_text)
    tag_match = re.search(r'"main_vllm_tag":\s*"([^"]+)"', conf_text)

    if not commit_match:
        print("Error: could not find main_vllm_commit in conf.py", file=sys.stderr)
        sys.exit(1)

    return {
        "base_commit": commit_match.group(1),
        "compat_tag": tag_match.group(1) if tag_match else None,
    }


def get_repo_head(repo_path: Path) -> str:
    """Return the HEAD commit SHA of a local git repository."""
    if not repo_path.exists():
        print(f"Error: path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def detect(vllm_path: Path, ascend_path: Path, target_commit: str | None = None) -> dict:
    """Run drift detection and write /tmp/main2main/detect.json.

    Returns the detect result dict.
    """
    conf = extract_from_conf_py(ascend_path)
    target = target_commit if target_commit else get_repo_head(vllm_path)

    result = {
        "base_commit": conf["base_commit"],
        "target_commit": target,
        "compat_tag": conf["compat_tag"],
        "has_drift": conf["base_commit"] != target,
    }

    workspace = Path("/tmp/main2main")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "steps").mkdir(exist_ok=True)
    (workspace / "detect.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect base/target commits for main2main pipeline."
    )
    parser.add_argument("--vllm-path", type=Path, required=True)
    parser.add_argument("--ascend-path", type=Path, required=True)
    parser.add_argument("--target-commit", default=None, help="Target commit SHA (default: vllm HEAD)")
    args = parser.parse_args()

    result = detect(args.vllm_path, args.ascend_path, target_commit=args.target_commit)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
