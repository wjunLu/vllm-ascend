#!/usr/bin/env python3
"""Push the main2main patch as a new branch and open a GitHub pull request.

Steps:
  1. Find the highest-numbered *.patch file in STEPS_DIR.
  2. Create a new local branch "update/main2main-<timestamp>" in ascend_path.
  3. Apply the patch with ``git apply``.
  4. Commit the applied changes.
  5. Push the branch to origin.
  6. Open a PR via ``gh pr create``, using final_summary.md as the description.

Environment variables (all overridable by CLI flags):
  PUSH_TO_GITHUB  — must be "true" to do anything
  GITHUB_REPO     — target repo "owner/name" (optional; gh infers from remote)
  STEPS_DIR       — directory that contains N.patch files
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_STEPS_DIR = Path("/tmp/main2main/steps")
DEFAULT_SUMMARY_PATH = Path("output/final_summary.md")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str,
    steps_dir: Path = DEFAULT_STEPS_DIR,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
) -> str:
    """Create a branch, apply the last patch, push, and open a GitHub PR.

    Returns the PR URL, or "" when no patch file is found.
    Raises subprocess.CalledProcessError on git/gh failure.
    """
    # 1. Find the last patch file by numeric stem (1.patch < 2.patch < N.patch)
    patch_files = sorted(
        steps_dir.glob("*.patch"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else -1,
    )
    if not patch_files:
        print("[push] No patch files found in STEPS_DIR, skipping PR.", file=sys.stderr)
        return ""

    last_patch = patch_files[-1].resolve()
    print(f"[push] Applying patch: {last_patch}")

    # 2. Create a new branch
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"update/main2main-{timestamp}"
    subprocess.run(
        ["git", "-C", str(ascend_path), "checkout", "-b", branch],
        check=True,
    )
    print(f"[push] Created branch '{branch}'.")

    # 3. Apply the patch
    subprocess.run(
        ["git", "-C", str(ascend_path), "apply", str(last_patch)],
        check=True,
    )

    # 4. Stage all changes introduced by the patch and commit
    subprocess.run(
        ["git", "-C", str(ascend_path), "add", "-A"],
        check=True,
    )
    commit_msg = f"main2main: sync vllm upstream ({timestamp})"
    subprocess.run(
        ["git", "-C", str(ascend_path), "commit", "-s", "-m", commit_msg],
        check=True,
    )
    print(f"[push] Committed patch as '{commit_msg}'.")

    # 5. Push branch to origin
    subprocess.run(
        ["git", "-C", str(ascend_path), "push", "origin", branch],
        check=True,
    )
    print(f"[push] Pushed branch '{branch}' to origin.")

    # 6. Create the PR; use final_summary.md (written by the summary crew) as description
    pr_description = summary_path.read_text(encoding="utf-8")

    gh_cmd = [
        "gh", "pr", "create",
        "--title", commit_msg,
        "--body", pr_description,
        "--head", branch,
        "--repo", github_repo,
    ]

    result = subprocess.run(
        gh_cmd, check=True, capture_output=True, text=True, cwd=str(ascend_path)
    )
    pr_url = result.stdout.strip()
    print(f"[push] PR created: {pr_url}")
    return pr_url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the last main2main patch to a new branch and open a GitHub PR."
    )
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Local vllm-ascend repository path.")
    parser.add_argument("--steps-dir", type=Path,
                        default=Path(os.getenv("STEPS_DIR", "/tmp/main2main/steps")),
                        help="Directory containing N.patch files (default: $STEPS_DIR).")
    parser.add_argument("--github-repo", default=os.getenv("GITHUB_REPO"),
                        required=not os.getenv("GITHUB_REPO"),
                        help="Target repo in owner/name form, e.g. vllm-project/vllm-ascend (or set $GITHUB_REPO).")
    parser.add_argument("--summary-path", type=Path, default=None,
                        help="Markdown file used as PR description.")
    parser.add_argument("--push", action="store_true",
                        default=os.getenv("PUSH_TO_GITHUB", "false").lower() == "true",
                        help="Actually push and create PR (default: $PUSH_TO_GITHUB).")
    args = parser.parse_args()

    if not args.push:
        print("[push] PUSH_TO_GITHUB is not true, skipping.", file=sys.stderr)
        sys.exit(0)

    push_and_create_pr(
        ascend_path=args.ascend_path,
        steps_dir=args.steps_dir,
        github_repo=args.github_repo,
        summary_path=args.summary_path,
    )


if __name__ == "__main__":
    main()
