#!/usr/bin/env python3
"""Run main2main tests with resource-aware parallel scheduling.

Given the total NPU cards available on the target machine, this script:
1. Maps each suite to its card requirement
2. Schedules suites into rounds (greedy bin-packing); suites in the same round
   run in parallel, rounds run sequentially
3. Aggregates per-suite results into a single result JSON

Execution targets:
  - Local:       run directly (no --remote)
  - Remote host: --remote user@host (container taken from env)
  - Remote env:  --remote env (reads env vars below)

  Remote execution always runs inside a Docker container on the target machine.
  The container must already exist — it is NOT auto-created.
  ascend_path is used directly as the remote workdir (typically backed by a
  shared filesystem).

Remote environment variables:
  MAIN2MAIN_REMOTE_HOST       SSH target for remote execution, e.g. root@1.2.3.4
  MAIN2MAIN_REMOTE_CONTAINER  container name for "docker exec"

Usage:
  # Local execution (multi-card parallel)
  python3 run_tests.py \\
    --vllm-path /vllm-workspace/vllm \\
    --vllm-commit abc1234 \\
    --ascend-path /vllm-workspace/vllm-ascend \\
    --ascend-commit def5678 \\
    --patch /tmp/main2main/steps/step-0/fix.patch \\
    --step-id 0 --total-cards 8 \\
    --suite e2e-singlecard-light \\
    --suite e2e-2card-light

  # Remote execution with container
  python3 run_tests.py \\
    --vllm-path /vllm-workspace/vllm \\
    --vllm-commit abc1234 \\
    --ascend-path /vllm-workspace/vllm-ascend \\
    --ascend-commit def5678 \\
    --step-id 1 --total-cards 8 \\
    --remote container-e2e \\
    --suite e2e-singlecard-light \\
    --suite e2e-2card-light

  # Dry-run: print schedule only, no execution
  python3 run_tests.py ... --dry-run
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

PASS_RESULTS = {"passed", "env_flake_pass"}
DEFAULT_SUITES = ["e2e-singlecard-light"]

# ---------------------------------------------------------------------------
# Environment setup (was set_env.py)
# ---------------------------------------------------------------------------

DEFAULT_VLLM_REPO = "https://github.com/vllm-project/vllm.git"
DEFAULT_ASCEND_REPO = "https://github.com/vllm-project/vllm-ascend.git"


def _git_run(cmd: list[str], cwd: Path, msg: str) -> None:
    print(f"  {msg} ... ", end="", flush=True)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED")
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)
    print("OK")


def _ensure_repo(path: Path, remote: str) -> bool:
    """Clone the repo if the directory doesn't exist or is not a git repo.

    Returns True if the repo was freshly cloned, False if it already existed.
    """
    if path.exists():
        if not (path / ".git").exists():
            print(f"  {path} exists but is not a git repo, removing ... ", end="", flush=True)
            shutil.rmtree(path)
            print("OK")
        else:
            print(f"  {path} already exists, fetching ... ", end="", flush=True)
            _git_run(["git", "fetch", "--tags"], path, "fetch")
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Cloning {remote} -> {path} ... ", end="", flush=True)
    result = subprocess.run(
        ["git", "clone", remote, str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("FAILED")
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)
    print("OK")
    return True


def _checkout(path: Path, commit: str) -> None:
    """Checkout a specific commit (detached HEAD)."""
    _git_run(["git", "checkout", commit], path, f"checkout {commit[:8]}")


def _apply_patch(path: Path, patch_path: Path) -> None:
    """Apply a patch file. Supports .patch (git am) and .diff (git apply)."""
    if not patch_path.exists():
        print(f"Error: patch not found: {patch_path}", file=sys.stderr)
        sys.exit(1)

    suffix = patch_path.suffix
    if suffix == ".patch":
        _git_run(["git", "am", str(patch_path)], path, f"git am {patch_path.name}")
    else:
        _git_run(["git", "apply", str(patch_path)], path, f"git apply {patch_path.name}")


def _build_exec_cmd(
    inner_cmd: str,
    remote_host: str | None = None,
    remote_container: str | None = None,
) -> list[str]:
    """Wrap *inner_cmd* for execution on the target (local / remote container)."""
    if remote_host:
        return ["ssh", "-o", "StrictHostKeyChecking=no", remote_host,
                f"docker exec {remote_container} sh -c {shlex.quote(inner_cmd)}"]
    else:
        return ["sh", "-c", inner_cmd]


def _pip_install(
    repo_path: Path,
    remote_host: str | None = None,
    remote_container: str | None = None,
    extra_env: dict[str, str] | None = None,
    requirements: str | None = None,
    verbose: bool = False,
    skip_editable: bool = False,
) -> None:
    """Run pip install in *repo_path* on the target (local/remote/container)."""
    merged_env = dict(_PIP_INSTALL_BASE_ENV)
    if extra_env:
        merged_env.update(extra_env)
    env_prefix = " ".join(f"{k}={v}" for k, v in merged_env.items()) + " "

    verbose_flag = "-v " if verbose else ""

    cmds = []
    if requirements:
        cmds.append(f"pip install -r {shlex.quote(requirements)}")
    if not skip_editable:
        cmds.append(f"{env_prefix}pip install {verbose_flag}-e .")

    for i, cmd in enumerate(cmds):
        full_cmd = f"cd {shlex.quote(str(repo_path))} && {cmd}"
        label = f"pip install ({repo_path.name}) [{i+1}/{len(cmds)}]"
        print(f"  {label} ... ", end="", flush=True)
        exec_cmd = _build_exec_cmd(full_cmd, remote_host, remote_container)
        result = subprocess.run(exec_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("FAILED")
            print(result.stderr.strip(), file=sys.stderr)
            sys.exit(result.returncode)
        print("OK")


def _setup_mirrors(
    remote_host: str | None = None,
    remote_container: str | None = None,
) -> None:
    """Configure git proxy and pip mirrors on the target."""
    cmds = [
        "git config --global url.https://ghfast.top/https://github.com/.insteadOf https://github.com/",
        "pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
    ]
    for cmd in cmds:
        exec_cmd = _build_exec_cmd(cmd, remote_host, remote_container)
        subprocess.run(exec_cmd, capture_output=True, text=True)


_PIP_INSTALL_BASE_ENV = {
    "PIP_EXTRA_INDEX_URL": "https://mirrors.huaweicloud.com/ascend/repos/pypi",
}


def setup_env(
    vllm_path: Path,
    vllm_commit: str,
    ascend_path: Path,
    ascend_commit: str,
    patch_path: Path | None = None,
    vllm_remote: str = DEFAULT_VLLM_REPO,
    ascend_remote: str = DEFAULT_ASCEND_REPO,
    remote_host: str | None = None,
    remote_container: str | None = None,
) -> None:
    """Prepare the environment: clone, checkout, install, apply patch, install."""
    _setup_mirrors(remote_host, remote_container)

    print("=== Setup vLLM ===")
    _ensure_repo(vllm_path, vllm_remote)
    _checkout(vllm_path, vllm_commit)
    print("=== Install vLLM ===")
    _pip_install(vllm_path, remote_host, remote_container,
                 extra_env={"VLLM_TARGET_DEVICE": "empty"})

    print("=== Setup vllm-ascend ===")
    ascend_fresh = _ensure_repo(ascend_path, ascend_remote)
    _checkout(ascend_path, ascend_commit)

    if patch_path:
        print("=== Apply patch ===")
        _apply_patch(ascend_path, patch_path)

    print("=== Install vllm-ascend ===")
    _pip_install(ascend_path, remote_host, remote_container,
                 requirements="requirements-dev.txt", verbose=True,
                 skip_editable=not ascend_fresh)

    print("\nSetup complete.")
    print(f"  vLLM:        {vllm_path} @ {vllm_commit[:8]}")
    print(f"  vllm-ascend: {ascend_path} @ {ascend_commit[:8]}"
          + (f" + {patch_path.name}" if patch_path else ""))

# ---------------------------------------------------------------------------
# Suite -> card requirement
# ---------------------------------------------------------------------------

_SUITE_CARDS: dict[str, int] = {
    "e2e-singlecard-light": 1,
    "e2e-2card-light": 2,
    "e2e-4card-light": 4,
    "e2e-singlecard": 1,
    "e2e-multicard-2-cards": 2,
    "e2e-multicard-4-cards": 4,
    "e2e-upstream_singlecard": 1,
}

_SUITE_CARDS_FALLBACK: list[tuple[str, int]] = [
    (r"singlecard|single.card|1card|1.cards?|1-card", 1),
    (r"2card|2.cards?|2-card|two.card", 2),
    (r"4card|4.cards?|4-card|four.card", 4),
    (r"8card|8.cards?|8-card|eight.card", 8),
]


def _suite_cards(suite_name: str) -> int:
    if suite_name in _SUITE_CARDS:
        return _SUITE_CARDS[suite_name]
    lower = suite_name.lower()
    for pattern, cards in _SUITE_CARDS_FALLBACK:
        if re.search(pattern, lower):
            return cards
    return 1


# ---------------------------------------------------------------------------
# Remote config
# ---------------------------------------------------------------------------

def _resolve_remote(remote: str) -> tuple[str, str]:
    """Resolve --remote to (host, container).

    If *remote* contains '@', treat as literal SSH target, container is still
    taken from environment variable.
    Otherwise read from environment variables:
      MAIN2MAIN_REMOTE_HOST      (required)
      MAIN2MAIN_REMOTE_CONTAINER (required)
    """
    host = remote if "@" in remote else os.getenv("MAIN2MAIN_REMOTE_HOST", "")
    if not host:
        print("Error: --remote used but MAIN2MAIN_REMOTE_HOST is not set",
              file=sys.stderr)
        sys.exit(1)

    container = os.getenv("MAIN2MAIN_REMOTE_CONTAINER", "")
    if not container:
        print("Error: --remote used but MAIN2MAIN_REMOTE_CONTAINER is not set",
              file=sys.stderr)
        sys.exit(1)

    print(f"  Remote target: {host}  container: {container}")
    return host, container


# ---------------------------------------------------------------------------
# Scheduler: greedy first-fit decreasing bin-packing
# ---------------------------------------------------------------------------

def _schedule_rounds(suite_names: list[str], total_cards: int) -> list[list[str]]:
    ordered = sorted(suite_names, key=lambda s: (-_suite_cards(s), s))
    rounds: list[list[str]] = []
    usage: list[int] = []

    for name in ordered:
        need = _suite_cards(name)
        if need > total_cards:
            raise ValueError(
                f"Suite '{name}' requires {need} cards but only {total_cards} available"
            )
        placed = False
        for i in range(len(rounds)):
            if usage[i] + need <= total_cards:
                rounds[i].append(name)
                usage[i] += need
                placed = True
                break
        if not placed:
            rounds.append([name])
            usage.append(need)

    return rounds


def _assign_devices(rounds: list[list[str]]) -> list[list[tuple[str, str]]]:
    """Assign non-overlapping device IDs to suites within each round.

    Within a round, suites get disjoint device ranges so they don't contend.
    Across rounds, devices reset — previous round's processes have finished.
    Returns the same structure with each suite name paired with its device list
    string, e.g. "0,1,2,3" for ASCEND_RT_VISIBLE_DEVICES.
    """
    result: list[list[tuple[str, str]]] = []
    for rnd in rounds:
        assigned: list[tuple[str, str]] = []
        next_dev = 0
        for suite_name in rnd:
            need = _suite_cards(suite_name)
            devices = ",".join(str(i) for i in range(next_dev, next_dev + need))
            next_dev += need
            assigned.append((suite_name, devices))
        result.append(assigned)
    return result



# ---------------------------------------------------------------------------
# Unchanged from original run_main2main_ci.py
# ---------------------------------------------------------------------------

def _run_to_log(command: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command, cwd=cwd, env=env,
            stdout=log_file, stderr=subprocess.STDOUT,
        )
        return process.wait()


def _run_summary(
    ci_log_summary: Path,
    log_path: Path,
    summary_path: Path,
    step_id: int,
    round_number: int,
) -> dict:
    if not ci_log_summary.exists():
        return {
            "summary_exit_code": 1,
            "summary_error": f"ci_log_summary.py not found: {ci_log_summary}",
            "summary": None,
        }
    command = [
        sys.executable, str(ci_log_summary),
        "--log-file", str(log_path),
        "--format", "llm-json",
        "--output", str(summary_path),
        "--step-name", f"main2main {step_id} round {round_number}",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return {
            "summary_exit_code": result.returncode,
            "summary_error": result.stderr.strip() or result.stdout.strip(),
            "summary": None,
        }
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        return {
            "summary_exit_code": result.returncode,
            "summary_error": f"summary output was not written: {summary_path}",
            "summary": None,
        }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "summary_exit_code": result.returncode,
            "summary_error": f"invalid summary JSON: {exc}",
            "summary": None,
        }
    return {
        "summary_exit_code": result.returncode,
        "summary_error": None,
        "summary": summary,
    }


def _count(summary: dict | None, field: str) -> int:
    if not summary:
        return 0
    count_field = f"{field}_count"
    if count_field in summary:
        return int(summary[count_field])
    value = summary.get(field, [])
    return len(value) if isinstance(value, list) else 0


def _classify_result(run_suite_exit_code: int, summary: dict | None, summary_error: str | None) -> str:
    if run_suite_exit_code == 0:
        return "passed"
    if summary_error or summary is None:
        return "summary_error"
    code_bugs_count = len(summary.get("code_bugs", []))
    env_flakes_count = len(summary.get("env_flakes", []))
    if code_bugs_count == 0 and env_flakes_count > 0:
        return "env_flake_pass"
    return "failed"


# ---------------------------------------------------------------------------
# Programmatic entry point (called by main.py)
# ---------------------------------------------------------------------------

# Estimated durations per suite (sum of test estimated_time from config.yaml)
_MOCK_DURATIONS: dict[str, int] = {
    "e2e-singlecard-light": 1111,
    "e2e-2card-light": 477,
    "e2e-4card-light": 365,
    "e2e-singlecard": 3200,
    "e2e-multicard-2-cards": 2400,
    "e2e-multicard-4-cards": 3000,
}


def run_tests(
    vllm_path: str | Path,
    vllm_commit: str,
    ascend_path: str | Path,
    ascend_commit: str,
    patch_path: str | Path | None = None,
    step_id: int = 0,
    suites: list[str] | None = None,
    total_cards: int = 1,
    remote: str | None = None,
    log_dir: str | Path = "/vllm-workspace/logs",
    remote_log_dir: str | Path | None = None,
    round_number: int = 1,
    dry_run: bool = False,
    sequential: bool = False,
    mock: bool = False,
    mock_scale: float = 0.1,
) -> dict:
    """Run end-to-end tests for a main2main step.

    1. Resolves remote target and ensures container is running
    2. Clones/checkouts repos, applies patch, pip-installs on target
    3. Schedules and executes CI suites
    4. Writes result JSON to <log_dir>/steps/<id>/tests/round-<round>-result.json
    5. Returns the result dict (includes ``can_commit`` bool and ``ci_result`` str)

    Returns:
        dict with keys: step_id, round, suite, suites, ci_result, passed,
        can_commit, requires_fix, code_bugs_count, env_flakes_count,
        failed_test_files_count, failed_test_cases_count, suite_results, ...
    """
    vllm_path = Path(vllm_path)
    ascend_path = Path(ascend_path)
    if patch_path:
        patch_path = Path(patch_path)
    log_dir = Path(log_dir)
    if remote_log_dir is None:
        remote_log_dir = log_dir
    else:
        remote_log_dir = Path(remote_log_dir)

    # ---- Step 1: Resolve remote ----
    remote_host: str | None = None
    remote_container: str | None = None

    if remote:
        remote_host, remote_container = _resolve_remote(remote)

    # ---- Step 2: Setup environment (git + pip install) ----
    setup_env(
        vllm_path=vllm_path,
        vllm_commit=vllm_commit,
        ascend_path=ascend_path,
        ascend_commit=ascend_commit,
        patch_path=patch_path,
        remote_host=remote_host,
        remote_container=remote_container,
    )

    # ---- Step 3: Verify run_suite.py exists ----
    run_suite = ascend_path / ".github" / "workflows" / "scripts" / "run_suite.py"
    ci_log_summary = ascend_path / ".github" / "workflows" / "scripts" / "ci_log_summary.py"

    if not remote_host and not run_suite.exists():
        print(f"Error: run_suite.py not found: {run_suite}", file=sys.stderr)
        sys.exit(1)

    # ---- Step 4: Setup env vars ----
    env = os.environ.copy()
    env.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
    env.setdefault("VLLM_USE_MODELSCOPE", "true")

    # ---- Step 5: Determine output dirs ----
    ci_dir = log_dir / "steps" / str(step_id) / "tests"
    result_path = ci_dir / f"round-{round_number}-result.json"

    suite_names = suites or DEFAULT_SUITES

    # ---- Step 6: Schedule rounds ----
    if sequential:
        rounds = [[s] for s in suite_names]
    else:
        rounds = _schedule_rounds(suite_names, total_cards)

    device_rounds = _assign_devices(rounds)

    # Print schedule
    parallel_count = sum(1 for r in rounds if len(r) > 1)
    print(f"Schedule ({len(rounds)} round(s), {parallel_count} parallel, "
          f"total cards: {total_cards}):")
    for i, rnd in enumerate(device_rounds):
        usage = sum(_suite_cards(s) for s, _ in rnd)
        mode = "parallel" if len(rnd) > 1 else "serial"
        parts = [f"{s}({_suite_cards(s)}c, devs={d})" for s, d in rnd]
        print(f"  Round {i + 1} ({mode}, using {usage}/{total_cards} cards): "
              f"{', '.join(parts)}")
    print(flush=True)

    if dry_run:
        print("[dry-run] Skipping execution.", flush=True)
        return {}

    # ---- Step 7: Execute rounds ----
    t0 = time.monotonic()
    all_suite_results: list[dict] = []
    rounds_info: list[dict] = []

    for round_idx, rnd in enumerate(device_rounds, start=1):
        round_t0 = time.monotonic()
        print(f"\n== Round {round_idx}/{len(device_rounds)}: {len(rnd)} suite(s) ==",
              flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(rnd)) as executor:
            future_to_suite = {}

            for suite_name, devices in rnd:
                log_path = ci_dir / f"round-{round_number}-{suite_name}.log"
                summary_path = ci_dir / f"round-{round_number}-{suite_name}-summary.json"

                if mock:
                    duration = int(_MOCK_DURATIONS.get(suite_name, 60) * mock_scale)
                    if remote_host:
                        inner_cmd = f"docker exec {remote_container} sleep {duration}"
                        command = ["ssh", "-o", "StrictHostKeyChecking=no", remote_host, inner_cmd]
                    else:
                        command = ["sleep", str(duration)]
                    print(f"  [{suite_name}] mock: sleep {duration}s (scale={mock_scale})", flush=True)
                elif remote_host:
                    remote_run_suite = (
                        ascend_path / ".github" / "workflows" / "scripts" / "run_suite.py"
                    )
                    remote_env = [
                        f"ASCEND_RT_VISIBLE_DEVICES={devices}",
                    ]
                    for k in sorted(env):
                        if k.startswith("VLLM_"):
                            remote_env.append(f"{k}={shlex.quote(env[k])}")
                    inner_cmd = (
                        f"docker exec {remote_container} "
                        f"env {' '.join(remote_env)} "
                        f"python3 {shlex.quote(str(remote_run_suite))} "
                        f"--suite {shlex.quote(suite_name)} --continue-on-error"
                    )
                    command = ["ssh", "-o", "StrictHostKeyChecking=no", remote_host, inner_cmd]
                else:
                    command = [
                        sys.executable, str(run_suite),
                        "--suite", suite_name, "--continue-on-error",
                    ]

                def _run_and_summarize(cmd, lp, sp, sn, dvs, asc, cls, sid, rn, env_copy):
                    if not remote_host:
                        env_copy["ASCEND_RT_VISIBLE_DEVICES"] = dvs
                    exit_code = _run_to_log(cmd, asc, lp, env_copy)
                    if mock:
                        return {
                            "suite": sn,
                            "cards_required": _suite_cards(sn),
                            "run_suite_exit_code": exit_code,
                            "ci_result": "passed" if exit_code == 0 else "failed",
                            "summary_error": None,
                            "code_bugs_count": 0,
                            "env_flakes_count": 0,
                            "failed_test_files_count": 0,
                            "failed_test_cases_count": 0,
                            "log_path": str(lp),
                            "summary_path": str(sp),
                        }
                    summary_result = _run_summary(cls, lp, sp, sid, rn)
                    s = summary_result["summary"]
                    se = summary_result["summary_error"]
                    cr = _classify_result(exit_code, s, se)
                    return {
                        "suite": sn,
                        "cards_required": _suite_cards(sn),
                        "run_suite_exit_code": exit_code,
                        "ci_result": cr,
                        "summary_error": se,
                        "code_bugs_count": len((s or {}).get("code_bugs", [])),
                        "env_flakes_count": len((s or {}).get("env_flakes", [])),
                        "failed_test_files_count": _count(s, "failed_test_files"),
                        "failed_test_cases_count": _count(s, "failed_test_cases"),
                        "log_path": str(lp),
                        "summary_path": str(sp),
                    }

                fut = executor.submit(
                    _run_and_summarize,
                    command, log_path, summary_path, suite_name, devices,
                    ascend_path, ci_log_summary,
                    step_id, round_number, env.copy(),
                )
                future_to_suite[fut] = suite_name
                print(f"  [{suite_name}] started ({_suite_cards(suite_name)} card(s))", flush=True)

            round_suite_results = []
            for fut in concurrent.futures.as_completed(future_to_suite):
                suite_name = future_to_suite[fut]
                r = fut.result()
                round_suite_results.append(r)
                print(
                    f"  [{suite_name}] done: exit={r['run_suite_exit_code']}, "
                    f"result={r['ci_result']}, "
                    f"bugs={r['code_bugs_count']}, flakes={r['env_flakes_count']}",
                    flush=True,
                )

        round_elapsed = time.monotonic() - round_t0
        all_suite_results.extend(round_suite_results)
        rounds_info.append({
            "round": round_idx,
            "suites": [r["suite"] for r in round_suite_results],
            "cards_used": sum(_suite_cards(s) for s, _ in rnd),
            "total_cards": total_cards,
            "elapsed_s": round(round_elapsed, 1),
        })
        print(f"  Round {round_idx} elapsed: {round_elapsed:.1f}s", flush=True)

        # Pull remote logs back to local log_dir
        if remote_host:
            remote_ci_dir = f"{remote_log_dir}/steps/{step_id}/tests/"
            print(f"  Pulling remote logs: {remote_host}:{remote_ci_dir} -> {ci_dir}", flush=True)
            scp_result = subprocess.run(
                ["scp", "-r", "-o", "StrictHostKeyChecking=no",
                 f"{remote_host}:{remote_ci_dir}*", str(ci_dir) + "/"],
                capture_output=True, text=True,
            )
            if scp_result.returncode != 0:
                print(f"  [warn] scp pull failed (non-fatal): {scp_result.stderr.strip()}", flush=True)
            else:
                print(f"  Remote logs pulled successfully.", flush=True)

    total_elapsed = time.monotonic() - t0

    # ---- Step 8: Aggregate results ----
    outcomes = {r["ci_result"] for r in all_suite_results}
    if "failed" in outcomes:
        overall = "failed"
    elif "summary_error" in outcomes:
        overall = "summary_error"
    elif outcomes == {"passed"}:
        overall = "passed"
    else:
        overall = "env_flake_pass"

    suite_names = [r["suite"] for r in all_suite_results]
    suite_label = suite_names[0] if len(suite_names) == 1 else "+".join(suite_names)
    can_commit = overall in PASS_RESULTS

    result = {
        "step_id": step_id,
        "round": round_number,
        "suite": suite_label,
        "suites": suite_names,
        "ci_result": overall,
        "passed": overall == "passed",
        "can_commit": can_commit,
        "requires_fix": overall == "failed",
        "log_path": str(ci_dir),
        "summary_path": str(ci_dir),
        "total_cards": total_cards,
        "sequential": sequential,
        "remote": remote,
        "elapsed_s": round(total_elapsed, 1),
        "rounds": rounds_info,
        "suite_results": {r["suite"]: r for r in all_suite_results},
        "code_bugs_count": sum(r["code_bugs_count"] for r in all_suite_results),
        "env_flakes_count": sum(r["env_flakes_count"] for r in all_suite_results),
        "failed_test_files_count": sum(r["failed_test_files_count"] for r in all_suite_results),
        "failed_test_cases_count": sum(r["failed_test_cases_count"] for r in all_suite_results),
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"\nmain2main CI aggregated: {overall}  (can_commit={can_commit})", flush=True)
    print(f"Total elapsed: {total_elapsed:.1f}s  ({total_elapsed/60:.1f} min)", flush=True)
    print(f"Result written to {result_path}", flush=True)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run main2main CI with resource-aware parallel scheduling.",
    )
    # --- Setup env args ---
    parser.add_argument("--vllm-path", type=Path, required=True,
                        help="Path to the vLLM repository")
    parser.add_argument("--vllm-commit", required=True,
                        help="vLLM commit hash to checkout")
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Path to the vllm-ascend repository (local)")
    parser.add_argument("--ascend-commit", required=True,
                        help="vllm-ascend commit hash to checkout before applying patch")
    parser.add_argument("--patch", type=Path,
                        help="Patch file to apply to vllm-ascend after checkout")
    # --- CI args ---
    parser.add_argument("--step-id", type=int, required=True,
                        help="Step identifier, for example step-1")
    parser.add_argument("--round", type=int, default=1,
                        help="CI round number for this step")
    parser.add_argument("--suite", action="append",
                        help="run_suite.py suite name. Can be specified multiple times. "
                             "Defaults to e2e-singlecard-light.")
    parser.add_argument("--log-dir", type=Path, default=Path("/vllm-workspace/logs"),
                        help="Directory for test logs")
    parser.add_argument("--total-cards", type=int, default=1,
                        help="Total NPU cards available on the target machine "
                             "(default: 1)")
    parser.add_argument("--sequential", action="store_true",
                        help="Force serial execution (one suite at a time)")
    parser.add_argument("--remote",
                        help="Enable remote execution. Use 'user@host' for direct SSH, "
                             "or any value to read from env vars "
                             "(MAIN2MAIN_REMOTE_HOST / _CONTAINER / _IMAGE).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print schedule only, do not execute")
    parser.add_argument("--mock", action="store_true",
                        help="Use sleep-based simulation instead of real run_suite.py")
    parser.add_argument("--mock-scale", type=float, default=0.1,
                        help="Time scale factor for --mock (default: 0.1 = 10x faster)")
    args = parser.parse_args()

    result = run_tests(
        vllm_path=args.vllm_path,
        vllm_commit=args.vllm_commit,
        ascend_path=args.ascend_path,
        ascend_commit=args.ascend_commit,
        patch_path=args.patch,
        step_id=args.step_id,
        suites=args.suite,
        total_cards=args.total_cards,
        remote=args.remote,
        log_dir=args.log_dir,
        round_number=args.round,
        dry_run=args.dry_run,
        sequential=args.sequential,
        mock=args.mock,
        mock_scale=args.mock_scale,
    )

    can_commit = result.get("can_commit", False)
    sys.exit(0 if can_commit else 1)


if __name__ == "__main__":
    main()
