#!/usr/bin/env python3
"""Push the main2main patch as a new branch and open a GitHub pull request.

Steps:
  1. Ensure gh CLI is authenticated (use GH_TOKEN in CI, or existing gh auth).
  2. Configure git credential helper so git push uses the same token.
  3. Create a new local branch "update/main2main-<timestamp>" from the
     current branch (which should be at the original base commit).
  4. Apply the final_target.patch file with ``git apply``.
  5. Commit the applied changes.
  6. Push the branch to origin.
  7. Open a PR via ``gh pr create``, using final_summary.md as the description.
  8. Restore the original branch.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to do anything
  GITHUB_REPO     — target repo "owner/name" (required)
  GH_TOKEN        — GitHub Personal Access Token (required in CI;
                    also used by git push via credential helper)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from main2main_flow.utils import run_git

DEFAULT_WORKSPACE_DIR = Path(__file__).parent.parent.parent.parent / "workspace"


def _detect_default_branch(repo: Path | str, remote: str = "origin") -> str:
    try:
        ref = run_git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD").strip()
        return ref.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _ensure_gh_auth(ascend_path: Path | str) -> None:
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True, capture_output=True, text=True,
        )
        print("[push] gh CLI already authenticated.")
    except subprocess.CalledProcessError:
        gh_token = os.getenv("GH_TOKEN", "")
        if not gh_token:
            print("[push] gh not authenticated and GH_TOKEN not set. "
                  "Run 'gh auth login' locally or set GH_TOKEN in CI.",
                  file=sys.stderr)
            sys.exit(1)
        print("[push] Authenticating gh CLI with GH_TOKEN...")
        subprocess.run(
            ["gh", "auth", "login",
             "--with-token"],
            input=gh_token, check=True, capture_output=True, text=True,
        )
        print("[push] gh CLI authenticated via GH_TOKEN.")

    run_git(ascend_path, "config", "credential.helper", "!gh auth git-credential")
    print("[push] Git credential helper set to 'gh auth git-credential'.")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str,
    patch_path: Path | None = None,
    summary_path: Path | None = None,
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
) -> str:
    """Create a branch, apply the final patch, push, and open a GitHub PR.

    Returns the PR URL, or "" when the patch file is missing.
    Raises subprocess.CalledProcessError on git/gh failure.
    """
    if not github_repo:
        print("[push] GITHUB_REPO is empty, cannot create PR.", file=sys.stderr)
        return ""

    patch_file = patch_path or workspace_dir / "final_target.patch"
    if not patch_file.exists():
        print(f"[push] Patch file not found: {patch_file}, skipping PR.", file=sys.stderr)
        return ""

    summary_file = summary_path or workspace_dir / "final_summary.md"
    if not summary_file.exists():
        print(f"[push] Summary file not found: {summary_file}, using empty description.", file=sys.stderr)
        pr_description = ""
    else:
        pr_description = summary_file.read_text(encoding="utf-8")

    patch_file = patch_file.resolve()
    print(f"[push] Applying patch: {patch_file}")

    _ensure_gh_auth(ascend_path)

    original_branch = run_git(ascend_path, "branch", "--show-current").strip()
    was_detached = not original_branch
    if was_detached:
        original_branch = run_git(ascend_path, "rev-parse", "HEAD").strip()

    try:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"update/main2main-{timestamp}"
        run_git(ascend_path, "checkout", "-b", branch)
        print(f"[push] Created branch '{branch}' from '{original_branch}'.")

        run_git(ascend_path, "apply", str(patch_file))

        run_git(ascend_path, "add", "-A")
        commit_msg = f"main2main: sync vllm upstream ({timestamp})"
        run_git(ascend_path, "commit", "-s", "-m", commit_msg)
        print(f"[push] Committed patch as '{commit_msg}'.")

        run_git(ascend_path, "push", "origin", branch)
        print(f"[push] Pushed branch '{branch}' to origin.")

        base_branch = _detect_default_branch(ascend_path)

        gh_cmd = [
            "gh", "pr", "create",
            "--title", commit_msg,
            "--body", pr_description,
            "--head", branch,
            "--base", base_branch,
            "--repo", github_repo,
        ]

        result = subprocess.run(
            gh_cmd, check=True, capture_output=True, text=True, cwd=str(ascend_path)
        )
        pr_url = result.stdout.strip()
        print(f"[push] PR created: {pr_url}")
    finally:
        run_git(ascend_path, "checkout", original_branch)
        print(f"[push] Restored original branch '{original_branch}'.")

    return pr_url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the main2main final patch to a new branch and open a GitHub PR."
    )
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Local vllm-ascend repository path.")
    parser.add_argument("--patch-path", type=Path, default=None,
                        help="Path to final_target.patch (default: workspace/final_target.patch).")
    parser.add_argument("--summary-path", type=Path, default=None,
                        help="Markdown file used as PR description (default: workspace/final_summary.md).")
    parser.add_argument("--workspace-dir", type=Path, default=DEFAULT_WORKSPACE_DIR,
                        help="Workspace directory containing final_target.patch and final_summary.md.")
    parser.add_argument("--github-repo", default=os.getenv("GITHUB_REPO"),
                        required=not os.getenv("GITHUB_REPO"),
                        help="Target repo in owner/name form, e.g. vllm-project/vllm-ascend (or set $GITHUB_REPO).")
    parser.add_argument("--push", action="store_true",
                        default=os.getenv("PUSH_TO_GITHUB", "false").lower() == "true",
                        help="Actually push and create PR (default: $PUSH_TO_GITHUB).")
    args = parser.parse_args()

    if not args.push:
        print("[push] PUSH_TO_GITHUB is not true, skipping.", file=sys.stderr)
        sys.exit(0)

    push_and_create_pr(
        ascend_path=args.ascend_path,
        patch_path=args.patch_path,
        summary_path=args.summary_path,
        workspace_dir=args.workspace_dir,
        github_repo=args.github_repo,
    )


if __name__ == "__main__":
    main()