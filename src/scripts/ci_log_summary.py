#!/usr/bin/env python3
"""Lightweight CI log summarizer for main2main_flow.

Parses the structured output of run_suite.py (which wraps pytest) and produces
JSON summaries suitable for automated classification of test failures.

Replaces the now-deleted vllm-ascend/.github/workflows/scripts/ci_log_summary.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---- patterns ----

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_GHA_GROUP_RE = re.compile(r"^::(?:group|endgroup)::.*$")

_RUN_SUITE_START_RE = re.compile(r"\[\d+/\d+\]\s+START\s+(tests/\S+)")
_RUN_SUITE_END_RE = re.compile(r"\[\d+/\d+\]\s+(?:PASSED|FAILED\s+\(exit\s+code\s+\d+\))\s+(tests/\S+)")
_RUN_SUITE_FAILED_RE = re.compile(r"\[\d+/\d+\]\s+FAILED\s+\(exit\s+code\s+\d+\)\s+(tests/\S+)")

_PYTEST_FAILURE_HEADER_RE = re.compile(r"^_+\s+test_\S+.*_+$")
_PYTEST_FAILURES_BANNER_RE = re.compile(r"^=+\s+FAILURES\s+=+$")
_PYTEST_SUMMARY_BANNER_RE = re.compile(r"^=+\s+short test summary info\s+=+$", re.IGNORECASE)
_PYTEST_SUMMARY_FAILED_RE = re.compile(r"^FAILED\s+(tests/\S+\.py::\S+)")
_PYTEST_SUMMARY_FAILED_PAYLOAD_RE = re.compile(r"^FAILED\s+(tests/\S+\.py::\S+)\s+-\s+(.+)")

_ERROR_RE = re.compile(r"((?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*(?:Error|Exception|Warning)):\s*(.+)")
_TRACEBACK_START_RE = re.compile(
    r"^(Traceback\s+\(most\s+recent\s+call\s+last\):"
    r"|ImportError\s+while\s+loading\s+conftest"
    r"|ERROR\s+collecting)"
)

_ENV_FLAKE_PATTERNS: list[str] = [
    r"OSError:.*Stale file handle",
    r"ConnectionResetError",
    r"filelock.*Lock",
    r"ConnectionRefusedError",
    r"TimeoutError",
    r"torch\.cuda\.OutOfMemoryError",
    r"OSError:.*No space left on device",
    r"RuntimeError:.*CUDA error",
    r"RuntimeError:.*NCCL",
]


def clean_line(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    line = _GHA_GROUP_RE.sub("", line)
    return line


def _is_env_flake(error_type: str, error_msg: str) -> bool:
    full = f"{error_type}: {error_msg}"
    return any(re.search(p, full) for p in _ENV_FLAKE_PATTERNS)


def _extract_test_name_from_header(line: str) -> str | None:
    cleaned = clean_line(line).strip("_ ").strip()
    return cleaned if cleaned else None


def _extract_failed_test_cases(log_text: str) -> list[str]:
    failed: set[str] = set()
    lines = log_text.splitlines()
    in_summary = False
    for raw_line in lines:
        line = clean_line(raw_line)
        if _PYTEST_SUMMARY_BANNER_RE.match(line):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("="):
                in_summary = False
                continue
            match = _PYTEST_SUMMARY_FAILED_RE.match(line)
            if match:
                failed.add(match.group(1))
    for raw_line in lines:
        line = clean_line(raw_line)
        match = _RUN_SUITE_FAILED_RE.search(line)
        if match and "::" in match.group(1):
            failed.add(match.group(1))
    return sorted(failed)


def _extract_failed_test_files(log_text: str, test_cases: list[str]) -> list[str]:
    files: set[str] = {tc.split("::")[0] for tc in test_cases}
    for raw_line in log_text.splitlines():
        line = clean_line(raw_line)
        match = _RUN_SUITE_FAILED_RE.search(line)
        if not match:
            continue
        target = match.group(1)
        files.add(target.split("::")[0] if "::" in target else target)
    return sorted(files)


def _extract_errors(log_text: str, failed_test_cases: list[str]) -> list[dict]:
    """Extract one representative error per unique (error_type, error_message) pair."""
    errors: list[dict] = []
    lines = log_text.splitlines()
    seen: set[tuple[str, str]] = set()

    # 1. Try summary payload first (most reliable): "FAILED tests/...::test_name - ErrorType: msg"
    for raw_line in lines:
        line = clean_line(raw_line)
        match = _PYTEST_SUMMARY_FAILED_PAYLOAD_RE.match(line)
        if not match:
            continue
        test_case = match.group(1)
        payload = match.group(2).strip()
        err_match = _ERROR_RE.search(payload)
        if not err_match:
            continue
        error_type, error_msg = err_match.group(1), err_match.group(2).strip()
        # Strip trailing quotes, newlines
        error_msg = re.sub(r"(?:\\n|\n).*$", "", error_msg).strip()
        error_msg = re.sub(r"(?:\\[nr]|['\"])+$", "", error_msg).strip()

        key = (error_type, error_msg)
        if key in seen:
            continue
        seen.add(key)
        test_file = test_case.split("::")[0]
        is_flake = _is_env_flake(error_type, error_msg)
        errors.append({
            "error_type": error_type,
            "error_message": error_msg,
            "category": "Environment Flake" if is_flake else "Code Bug",
            "context": [line],
            "failed_test_files": [test_file],
            "failed_test_cases": [test_case],
        })

    # 2. Scan failure blocks for errors not caught by summary
    in_failures = False
    current_test_name: str | None = None
    for raw_line in lines:
        line = clean_line(raw_line)
        if _PYTEST_FAILURES_BANNER_RE.match(line):
            in_failures = True
            current_test_name = None
            continue
        if not in_failures:
            continue
        if _PYTEST_SUMMARY_BANNER_RE.match(line):
            in_failures = False
            break

        header_match = _extract_test_name_from_header(line)
        if header_match:
            current_test_name = header_match
            continue

        err_match = _ERROR_RE.search(line)
        if not err_match:
            continue
        error_type, error_msg = err_match.group(1), err_match.group(2).strip()
        error_msg = re.sub(r"(?:\\n|\n).*$", "", error_msg).strip()

        key = (error_type, error_msg)
        if key in seen:
            continue
        seen.add(key)

        # Try to find matching test case
        matching_cases = [tc for tc in failed_test_cases if current_test_name and
                         (current_test_name in tc or tc.endswith("::" + current_test_name))]
        test_file = matching_cases[0].split("::")[0] if matching_cases else ""
        is_flake = _is_env_flake(error_type, error_msg)
        errors.append({
            "error_type": error_type,
            "error_message": error_msg,
            "category": "Environment Flake" if is_flake else "Code Bug",
            "context": [line],
            "failed_test_files": [test_file] if test_file else [],
            "failed_test_cases": matching_cases[:1],
        })

    return errors


def process_local_log(log_text: str) -> dict:
    """Parse a raw test log into a structured summary dict."""
    failed_test_cases = _extract_failed_test_cases(log_text)
    failed_test_files = _extract_failed_test_files(log_text, failed_test_cases)
    all_errors = _extract_errors(log_text, failed_test_cases)

    code_bugs = [e for e in all_errors if e["category"] == "Code Bug"]
    env_flakes = [e for e in all_errors if e["category"] == "Environment Flake"]

    return {
        "failed_test_files": failed_test_files,
        "failed_test_cases": failed_test_cases,
        "failed_test_files_count": len(failed_test_files),
        "failed_test_cases_count": len(failed_test_cases),
        "code_bugs": code_bugs,
        "env_flakes": env_flakes,
        "code_bugs_count": len(code_bugs),
        "env_flakes_count": len(env_flakes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize CI test logs into structured JSON.")
    parser.add_argument("--log-file", type=Path, required=True, help="Path to raw test log file.")
    parser.add_argument("--format", choices=("llm-json", "json"), default="llm-json",
                        help="Output format (default: llm-json).")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write output to file instead of stdout.")
    parser.add_argument("--step-name", default="Run test", help="Ignored; kept for CLI compat.")
    args = parser.parse_args()

    if not args.log_file.exists() or args.log_file.stat().st_size == 0:
        empty = {"failed_test_files": [], "failed_test_cases": [],
                 "failed_test_files_count": 0, "failed_test_cases_count": 0,
                 "code_bugs": [], "env_flakes": [], "code_bugs_count": 0, "env_flakes_count": 0}
        output = json.dumps(empty, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return

    log_text = args.log_file.read_text(encoding="utf-8", errors="replace")
    result = process_local_log(log_text)

    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
