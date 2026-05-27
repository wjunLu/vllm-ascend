import os
import subprocess
import tempfile

UpgradeCompleted = "UpgradeCompleted"
StepCompleted = "StepCompleted"
UpgradeFailed = "UpgradeFailed"
StepRetryNeeded = "StepRetryNeeded"


def is_git_url(path: str) -> bool:
    return path.startswith(("https://", "http://", "git@"))


def clone_repo(url: str, target: str) -> None:
    print(f"[init] Cloning {url} → {target}")
    subprocess.run(["git", "clone", url, target], check=True)


def resolve_path(raw: str, name: str, temp_dirs: list) -> str:
    if is_git_url(raw):
        tmp = tempfile.TemporaryDirectory()
        temp_dirs.append(tmp)
        target = os.path.join(tmp.name, name)
        clone_repo(raw, target)
        return target
    return raw


def cleanup_temp_dirs(temp_dirs: list) -> None:
    for tmp in temp_dirs:
        tmp.cleanup()
    temp_dirs.clear()