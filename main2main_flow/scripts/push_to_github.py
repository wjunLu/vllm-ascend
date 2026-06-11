#!/usr/bin/env python3
"""Push the main2main patch as a new branch and open a GitHub pull request.

In CI mode (default when PUSH_TO_GITHUB=true):
  1. Ensure gh CLI is authenticated (use GH_TOKEN in CI, or existing gh auth).
  2. Configure git credential helper so git push uses the same token.
  3. If changes are already on a working branch (no --patch-path), use it directly;
     otherwise create a branch from the current commit and apply the final patch.
  4. Push the branch to the fork repo.
  5. Open a draft PR via ``gh pr create`` with proper commit-range title.
  6. Add labels to the PR.
  7. Write the PR URL to a file for downstream workflow steps.

In local mode (PUSH_TO_GITHUB not set):
  1-4: same as above.
  5. Open a regular (non-draft) PR.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to do anything
  GITHUB_REPO     — target repo "owner/name" (required, e.g. vllm-project/vllm-ascend)
  HEAD_FORK       — fork to push to (optional, e.g. vllm-ascend-ci/vllm-ascend)
  GH_TOKEN        — GitHub Personal Access Token (required in CI;
                    also used by git push via credential helper)
  PR_LABELS       — comma-separated labels to add (default: "ready,ready-for-test")
  PR_DRAFT        — "true" (default) or "false"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from main2main_flow.utils import run_git

DEFAULT_WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
_PR_URL_FILE = "/tmp/main2main/pr_url.txt"


def _gh_token() -> str:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def _detect_default_branch(repo: Path | str, remote: str = "origin") -> str:
    try:
        ref = run_git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD").strip()
        return ref.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _ensure_gh_auth(ascend_path: Path | str) -> None:
    run_git(ascend_path, "config", "credential.helper", "!gh auth git-credential")
    print("[push] Git credential helper set to 'gh auth git-credential'.")


def _add_labels(github_repo: str, pr_number: str, labels: list[str]) -> None:
    if not labels:
        return
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         f"/repos/{github_repo}/issues/{pr_number}/labels"],
        input=json.dumps({"labels": labels}),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[push] Warning: Failed to add labels {labels}: {result.stderr.strip()}")
    else:
        print(f"[push] Labels added: {labels}")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str,
    patch_path: Path | None = None,
    summary_path: Path | None = None,
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    old_commit: str = "",
    new_commit: str = "",
    head_fork: str = "",
    draft: bool = True,
    labels: list[str] | None = None,
    branch_name: str = "",
) -> str:
    """Create a branch (or reuse current), push to fork, and open a GitHub PR.

    Returns the PR URL, or "" when preconditions are not met.
    Raises subprocess.CalledProcessError on git/gh failure.
    """
    if not github_repo:
        print("[push] GITHUB_REPO is empty, cannot create PR.", file=sys.stderr)
        return ""

    summary_file = summary_path or workspace_dir / "final_summary.md"
    if not summary_file.exists():
        print(f"[push] Summary file not found: {summary_file}, using empty description.", file=sys.stderr)
        pr_description = ""
    else:
        pr_description = summary_file.read_text(encoding="utf-8")

    _ensure_gh_auth(ascend_path)

    # ---- branch ----
    current_branch = run_git(ascend_path, "branch", "--show-current").strip()
    is_detached = not current_branch

    patch_file = patch_path.resolve() if patch_path else None
    has_patch = patch_file and patch_file.exists()

    if is_detached and not has_patch:
        print("[push] Detached HEAD and no patch to apply, cannot push.", file=sys.stderr)
        return ""

    try:
        if has_patch and not (os.getenv("MAIN2MAIN_KEEP_BRANCH") == "true" and not is_detached):
            # Apply-patch mode: create fresh branch from current commit
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = branch_name or f"update/main2main-{ts}"
            run_git(ascend_path, "checkout", "-b", branch)
            print(f"[push] Created branch '{branch}', applying patch: {patch_file}")
            run_git(ascend_path, "apply", str(patch_file))
            run_git(ascend_path, "add", "-A")
            commit_msg = _build_commit_msg(old_commit, new_commit, ts)
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print(f"[push] Committed as '{commit_msg}'.")
        elif is_detached:
            branch = branch_name or f"update/main2main-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            run_git(ascend_path, "checkout", "-b", branch)
            print(f"[push] Created branch '{branch}' from detached HEAD.")
        else:
            # Reuse current branch (already has all step commits from the flow)
            branch = current_branch
            print(f"[push] Reusing current branch '{branch}' (already has step commits).")

        # ---- push ----
        if head_fork:
            fork_url = f"https://github.com/{head_fork}.git"
            token = os.getenv("GH_TOKEN", "") or _gh_token()
            fork_remote = f"https://{token}@github.com/{head_fork}.git" if token else fork_url
            print(f"[push] Pushing to fork: {fork_url}")
            run_git(ascend_path, "-c", "credential.helper=", "-c", "http.extraheader=", "push", "--force-with-lease", fork_remote, branch)
            head_ref = f"{head_fork.split('/')[0]}:{branch}"
        else:
            run_git(ascend_path, "push", "origin", branch)
            head_ref = branch
        print(f"[push] Pushed branch '{branch}'.")

        # ---- PR ----
        base_branch = _detect_default_branch(ascend_path)
        pr_title = _build_pr_title(old_commit, new_commit)

        gh_cmd = [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_description,
            "--head", head_ref,
            "--base", base_branch,
            "--repo", github_repo,
        ]
        if draft:
            gh_cmd.append("--draft")

        result = subprocess.run(
            gh_cmd, capture_output=True, text=True, cwd=str(ascend_path),
            env={**os.environ, "GH_TOKEN": os.getenv("GH_TOKEN", "")},
        )
        if result.returncode != 0:
            print(f"[push] PR create FAILED: {result.stderr.strip()}", flush=True)
            result.check_returncode()
        pr_url = result.stdout.strip()
        print(f"[push] PR created: {pr_url}")

        # ---- labels ----
        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
        if pr_number.isdigit():
            if labels is None:
                labels = ["ready", "ready-for-test"]
            _add_labels(github_repo, pr_number, labels)

        # ---- persist PR URL ----
        Path("/tmp/main2main").mkdir(parents=True, exist_ok=True)
        Path(_PR_URL_FILE).write_text(pr_url + "\n")
        print(f"[push] PR URL written to {_PR_URL_FILE}")

    finally:
        # Only restore if we created a new branch from a different starting point
        if has_patch:
            run_git(ascend_path, "checkout", current_branch if not is_detached else "HEAD")
            print(f"[push] Restored original ref.")

    return pr_url


def _build_commit_msg(old_commit: str, new_commit: str, ts: str) -> str:
    if old_commit and new_commit:
        short_old = old_commit[:8]
        short_new = new_commit[:8]
        return f"main2main: sync vllm upstream ({short_old}...{short_new}) [{ts}]"
    return f"main2main: sync vllm upstream ({ts})"


def _build_pr_title(old_commit: str, new_commit: str) -> str:
    if old_commit and new_commit:
        short_old = old_commit[:8]
        short_new = new_commit[:8]
        return f"[Misc]feat: adapt to vLLM main ({short_old}...{short_new})"
    return "main2main: sync vllm upstream"


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
    parser.add_argument("--old-commit", default="",
                        help="Old vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--new-commit", default="",
                        help="New vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--head-fork", default=os.getenv("HEAD_FORK", ""),
                        help="Fork repo to push to, e.g. vllm-ascend-ci/vllm-ascend.")
    parser.add_argument("--draft", action="store_true",
                        default=os.getenv("PR_DRAFT", "true").lower() == "true",
                        help="Create as draft PR (default: true).")
    parser.add_argument("--labels", default=os.getenv("PR_LABELS", "ready,ready-for-test"),
                        help="Comma-separated labels to add to the PR.")
    parser.add_argument("--branch-name", default="",
                        help="Branch name (auto-generated if empty).")
    parser.add_argument("--push", action="store_true",
                        default=os.getenv("PUSH_TO_GITHUB", "false").lower() == "true",
                        help="Actually push and create PR (default: $PUSH_TO_GITHUB).")
    args = parser.parse_args()

    if not args.push:
        print("[push] PUSH_TO_GITHUB is not true, skipping.", file=sys.stderr)
        sys.exit(0)

    label_list = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()] if args.labels else []

    push_and_create_pr(
        ascend_path=args.ascend_path,
        patch_path=args.patch_path,
        summary_path=args.summary_path,
        workspace_dir=args.workspace_dir,
        github_repo=args.github_repo,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
        head_fork=args.head_fork,
        draft=args.draft,
        labels=label_list,
        branch_name=args.branch_name,
    )


if __name__ == "__main__":
    main()
