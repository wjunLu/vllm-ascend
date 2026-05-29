import shutil
import subprocess
from pathlib import Path

UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"
HasCommit = "HasCommit"
HasNoCommit = "HasNoCommit"

WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
STEPS_DIR = "steps"
VLLM_GIT_PATCH_FILE = "upstream.patch"
VLLM_GIT_CHANGED_FILES = "changed_files.txt"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
EACH_STEP_SUMMARY_FILE = "step_summary.json"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
FINAL_SUMMARY_FILE = "final_summary.json"
FINAL_TARGET_PATCH_FILE = "final_target.patch"

def run_git(repo: Path | str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def is_git_url(path: str) -> bool:
    return path.startswith(("https://", "http://", "git@"))


def clone_repo(url: str, target: str) -> None:
    print(f"[init] Cloning {url} → {target}")
    subprocess.run(["git", "clone", url, target], check=True)


def resolve_path(raw: str, name: str) -> str:
    if is_git_url(raw):
        target = WORKSPACE_DIR / "repos" / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        clone_repo(raw, str(target))
        return str(target)
    return raw
