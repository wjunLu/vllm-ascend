import shutil
import subprocess
from pathlib import Path

UpgradeCompleted = "UpgradeCompleted"
StepCompleted = "StepCompleted"
UpgradeFailed = "UpgradeFailed"
StepRetryNeeded = "StepRetryNeeded"
HasCommit = "HasCommit"
HasNoCommit = "HasNoCommit"

# Project-relative workspace: <project_root>/workspace/
WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
STEPS_DIR = "steps"
VLLM_GIT_PATCH_FILE = "upstream.patch"
VLLM_GIT_CHANGED_FILES = "changed_files.txt"

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
