#!/usr/bin/env python
import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router, or_, and_

from main2main_flow.crews.adapter_crew.opencode_adapter import AdaptResult, run_opencode_adapter
from main2main_flow.crews.summary_crew.summary_crew import SummaryCrew
from main2main_flow.scripts.detect_commits import detect
from main2main_flow.scripts.plan_steps import run_plan
from main2main_flow.scripts.pre_ci_check import run_check
from main2main_flow.scripts.push_to_github import push_and_create_pr
from main2main_flow.scripts.run_tests import run_tests
from main2main_flow.scripts.update_commit_reference import run_update
from main2main_flow.utils import (
    UpgradeCompleted, StepCompleted, UpgradeFailed, StepRetryNeeded,
    HasCommit, HasNoCommit, resolve_path, WORKSPACE_DIR, DETECT_FILE, STEPS_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, run_git
)

_REFERENCE_DIR = str(Path(__file__).parent / "reference")


class Main2MainState(BaseModel):
    vllm_path: str = ""
    vllm_ascend_path: str = ""
    target_commit: str = ""
    test_log_dir: str = ""
    
    steps: list = []
    release_tag: str = ""

    total_steps: int = 0
    current_step: int = 0

    cur_vllm_commit: str = ""
    cur_ascend_commit: str = ""
    cur_patch_path: str = ""

    test_errors: list = []
    retry_count: int = 0


class Main2MainFlow(Flow[Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @start()
    def initialize(self):
        """Initialize state; all paths default to workspace/ under the project root."""
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
        WORKSPACE_DIR.mkdir(parents=True)

        raw_vllm = (self.state.vllm_path
                    or os.getenv("VLLM_PATH")
                    or str(WORKSPACE_DIR / "repos" / "vllm"))
        raw_ascend = (self.state.vllm_ascend_path
                      or os.getenv("VLLM_ASCEND_PATH")
                      or str(WORKSPACE_DIR / "repos" / "vllm-ascend"))

        self.state.vllm_path = resolve_path(raw_vllm, "vllm")
        self.state.vllm_ascend_path = resolve_path(raw_ascend, "vllm-ascend")
        self.state.target_commit = (
            self.state.target_commit or os.getenv("VLLM_TARGET_COMMIT", "")
        )
        if not self.state.test_log_dir:
            self.state.test_log_dir = str(WORKSPACE_DIR / "test-logs")

    @router(initialize)
    def analyze_commit_and_plan_step(self) -> Literal["HasCommit", "HasNoCommit"]:
        vllm_path = Path(self.state.vllm_path)
        vllm_ascend_path = Path(self.state.vllm_ascend_path)

        # generate detect.json in workspace
        result, has_commit = detect(vllm_path, vllm_ascend_path,
                                    self.state.target_commit or None)
        self.state.release_tag = result.get("compat_tag") or ""

        (WORKSPACE_DIR / DETECT_FILE).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        print(f"[analyze] base={result['base_commit'][:8]}  "
              f"target={result['target_commit'][:8]}")

        if not has_commit:
            return HasNoCommit

        # generate steps.json in workspace
        plan = run_plan(vllm_path, result["base_commit"], result["target_commit"])
        self.state.steps = plan["steps"]
        self.state.total_steps = len(plan["steps"])

        (WORKSPACE_DIR / STEPS_FILE).write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        if self.state.total_steps == 0:
            return HasNoCommit

        print(f"[analyze] 规划了 {self.state.total_steps} 个步骤，"
              f"共 {plan['total_commits']} 个 commit。")
        print("===========================================")
        print(json.dumps(plan["steps"], indent=2, ensure_ascii=False))

        # generate every step folder in workspace
        for index in range(self.state.total_steps):
            step = self.state.steps[index]
            step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
            step_dir.mkdir(parents=True, exist_ok=True)
            (step_dir / VLLM_GIT_PATCH_FILE).write_text(step["upstream_patch"], encoding="utf-8")
            (step_dir / VLLM_GIT_CHANGED_FILES).write_text(step["changed_files"], encoding="utf-8")

        return HasCommit

    @listen(HasNoCommit)
    def has_no_commit(self):
        print(f"[done] 仓库已同步，无需适配，流程结束。")

    @listen(HasCommit)
    def process_steps(self):
        while self.state.current_step < self.state.total_steps:
            self._ai_analysis()
            result = self._run_e2e_test()
            if result == UpgradeCompleted or result == UpgradeFailed:
                return result
        return UpgradeCompleted

    def _ai_analysis(self):
        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
            step = self.state.steps[self.state.current_step]
            step_id = step["id"]
            step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
            print(f"[ai_analysis] SKIP_AI_ANALYSIS=true, skipping for step {step_id}")
            ascend_head = run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()
            self.state.cur_vllm_commit = step["end_commit"]
            self.state.cur_ascend_commit = ascend_head
            self.state.cur_patch_path = str(step_dir / EACH_STEP_TARGET_PATCH_FILE)
            return

        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id

        vllm_path = self.state.vllm_path
        ascend_path = self.state.vllm_ascend_path
        has_test_results = (step_dir / "tests").exists()

        if not has_test_results:
            run_git(vllm_path, "checkout", step["end_commit"])
            print(f"[ai_analysis] {step_id}: vllm checked out to {step['end_commit'][:8]}")

            try:
                ref_result = run_update(
                    ascend_path=Path(ascend_path),
                    old_commit=step["start_commit"],
                    new_commit=step["end_commit"],
                )
                print(f"[ai_analysis] {step_id}: updated commit ref in "
                      f"{len(ref_result['files_updated'])} file(s): "
                      f"{ref_result['files_updated']}")
            except ValueError:
                print(f"[ai_analysis] {step_id}: commit ref already updated, skipping")
        else:
            print(f"[ai_analysis] {step_id}: tests/ exists, skipping to fix mode")

        error_logs: list[str] = list(self.state.test_errors)
        patch_path = step_dir / VLLM_GIT_PATCH_FILE
        changed_files_path = step_dir / VLLM_GIT_CHANGED_FILES
        adapt_result: AdaptResult | None = None

        for attempt in range(1, 4):
            mode = "fix" if error_logs else "adapt"
            print(f"[ai_analysis] {step_id}: opencode attempt {attempt}, mode={mode}")
            adapt_result = run_opencode_adapter({
                "step_id": step_id,
                "step_dir": str(step_dir),
                "patch_path": str(patch_path),
                "changed_files_path": str(changed_files_path),
                "ascend_path": ascend_path,
                "release_tag": self.state.release_tag,
                "vllm_path": vllm_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": mode,
                "error_logs": json.dumps(error_logs, ensure_ascii=False),
            })

            check_result = run_check(ascend_path, self.state.release_tag)
            if check_result["all_passed"]:
                print(f"[ai_analysis] {step_id}: pre_ci passed on attempt {attempt}")
                break
            log_path = step_dir / PRE_CI_CHECK_FILE
            log_path.write_text(json.dumps(check_result, indent=2, ensure_ascii=False))

            error_logs = [str(log_path)]
            print(f"[ai_analysis] {step_id}: pre_ci failed → {log_path}")

        self.state.test_errors = []

        summary = adapt_result.step_summary if adapt_result else ""
        (step_dir / EACH_STEP_SUMMARY_FILE).write_text(summary, encoding="utf-8")

        adaptation_patch_path = step_dir / EACH_STEP_TARGET_PATCH_FILE
        adaptation_patch = run_git(ascend_path, "diff", "HEAD")
        adaptation_patch_path.write_text(adaptation_patch, encoding="utf-8")

        ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        self.state.cur_vllm_commit = step["end_commit"]
        self.state.cur_ascend_commit = ascend_head
        self.state.cur_patch_path = str(adaptation_patch_path)

        print(f"[ai_analysis] {step_id}: done, "
              f"is_noop={getattr(adapt_result, 'is_noop', False)}, "
              f"modified={getattr(adapt_result, 'modified_files', [])}, "
              f"vllm={step['end_commit'][:8]}, ascend={ascend_head[:8]}")

    def _run_e2e_test(self):
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        round_n = self.state.retry_count + 1
        print(f"run_e2e_test: step-{step_id} round={round_n}")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            print(f"[run_e2e_test] SKIP_E2E_TEST=true, treating as passed")
            self.state.retry_count = 0
            self.state.current_step += 1
            if self.state.current_step >= self.state.total_steps:
                return UpgradeCompleted
            return StepCompleted

        print(f"The adaptation patch is at: {self.state.cur_patch_path}")
        result = run_tests(
            vllm_path=self.state.vllm_path,
            vllm_commit=self.state.cur_vllm_commit,
            ascend_path=self.state.vllm_ascend_path,
            ascend_commit=self.state.cur_ascend_commit,
            patch_path=self.state.cur_patch_path or None,
            step_id=step_id,
            total_cards=8,
            suites=["e2e-2card-light"],
            remote="env",
            round_number=round_n,
            log_dir=str(WORKSPACE_DIR / STEPS_DIR),
        )

        test_passed = result.get("can_commit", False)
        summary_log = str(WORKSPACE_DIR / STEPS_DIR / str(step_id) / "tests" / f"round-{round_n}-summary.json")
        print(f"test_passed={test_passed}, ci_result={result.get('ci_result')}")

        if test_passed:
            self.state.retry_count = 0
            self.state.current_step += 1
            if self.state.current_step >= self.state.total_steps:
                return UpgradeCompleted
            else:
                return StepCompleted
        else:
            self.state.retry_count += 1
            self.state.test_errors = [summary_log]
            if self.state.retry_count >= 3:
                self.state.retry_count = 0
                self.state.test_errors = []
                return UpgradeFailed
            else:
                return StepRetryNeeded

    @listen(process_steps)
    def generate_final_post(self):
        ci_results_dir = os.getenv("CI_RESULTS_DIR", str(WORKSPACE_DIR / "ci_results"))
        steps_dir = os.getenv("STEPS_DIR", str(WORKSPACE_DIR / STEPS_DIR))
        result = (
            SummaryCrew()
            .crew()
            .kickoff(inputs={
                "ci_results_dir": ci_results_dir,
                "steps_dir": steps_dir,
            })
        )
        return result

    @listen(and_(UpgradeCompleted, generate_final_post))
    def push_to_github(self):
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            print("[push] PUSH_TO_GITHUB is not true, skipping.")
            return "SKIP_PUSH"

        return push_and_create_pr(
            ascend_path=Path(self.state.vllm_ascend_path),
            github_repo=os.getenv("GITHUB_REPO", ""),
        )


def kickoff():
    parser = argparse.ArgumentParser(description="Run Main2Main Flow")
    parser.add_argument("--vllm-path", default=None,
                        help="Local path or GitHub URL for the vllm repo")
    parser.add_argument("--vllm-ascend-path", default=None,
                        help="Local path or GitHub URL for the vllm-ascend repo")
    parser.add_argument("--target-commit", default=None,
                        help="Target vllm commit SHA to upgrade to (default: vllm HEAD)")
    args = parser.parse_args()

    inputs = {}
    if args.vllm_path:
        inputs["vllm_path"] = args.vllm_path
    if args.vllm_ascend_path:
        inputs["vllm_ascend_path"] = args.vllm_ascend_path
    if args.target_commit:
        inputs["target_commit"] = args.target_commit

    flow = Main2MainFlow()
    flow.kickoff(inputs=inputs if inputs else None)


def plot():
    import shutil
    output_dir = Path(__file__).parent.parent.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    flow = Main2MainFlow()
    tmp_html = Path(flow.plot(filename="flow.html", show=False))
    for f in tmp_html.parent.iterdir():
        shutil.copy2(f, output_dir / f.name)
    print(f"Flow plot saved to: {output_dir / tmp_html.name}")


def run_with_trigger():
    """Run the flow with a JSON trigger payload passed as a CLI argument."""
    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided. Please provide JSON payload as argument.")
    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Invalid JSON payload provided as argument")

    flow = Main2MainFlow()
    try:
        result = flow.kickoff({"crewai_trigger_payload": trigger_payload})
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the flow with trigger: {e}")


if __name__ == "__main__":
    kickoff()
