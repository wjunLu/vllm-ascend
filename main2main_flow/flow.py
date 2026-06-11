
import json
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router

from main2main_flow.agent.opencode_adapter import AdaptResult, run_opencode_adapter
from main2main_flow.scripts.detect_commits import detect
from main2main_flow.scripts.plan_steps import run_plan
from main2main_flow.scripts.pre_ci_check import run_check
from main2main_flow.scripts.push_to_github import push_and_create_pr
from main2main_flow.scripts.run_tests import run_tests
from main2main_flow.scripts.update_commit_reference import run_update
from main2main_flow.utils import (
    UpgradeCompleted, UpgradeFailed,
    HasCommit, HasNoCommit, resolve_path, WORKSPACE_DIR, DETECT_FILE, STEPS_FILE, FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    FINAL_CODE_STRUCTURE_GUIDE_FILE, run_git
)

_REFERENCE_DIR = str(Path(__file__).parent / "reference")


def _parse_test_cases_env() -> list[str] | None:
    val = os.getenv("MAIN2MAIN_TEST_CASES", "").strip()
    if not val:
        return None
    return [t.strip() for t in val.replace("\n", " ").split() if t.strip()]


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

    original_vllm_ref: str = ""
    original_ascend_ref: str = ""

    test_errors: list = []
    retry_count: int = 0

    final_status: str = ""

    # Tracked from detect step for PR title / push
    base_commit: str = ""

    # Changed files from current adaptation step (for precise test selection)
    changed_files: list[str] = []


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

        vllm_branch = run_git(self.state.vllm_path, "branch", "--show-current").strip()
        self.state.original_vllm_ref = vllm_branch or run_git(self.state.vllm_path, "rev-parse", "HEAD").strip()
        ascend_branch = run_git(self.state.vllm_ascend_path, "branch", "--show-current").strip()
        self.state.original_ascend_ref = ascend_branch or run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()

    @router(initialize)
    def analyze_commit_and_plan_step(self) -> Literal["HasCommit", "HasNoCommit"]:
        vllm_path = Path(self.state.vllm_path)
        vllm_ascend_path = Path(self.state.vllm_ascend_path)

        # generate detect.json in workspace
        result, has_commit = detect(vllm_path, vllm_ascend_path,
                                    self.state.target_commit or None)
        self.state.release_tag = result.get("compat_tag") or ""
        self.state.base_commit = result.get("base_commit", "")

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
            test_pass = self._run_e2e_test()
            if test_pass:
                self.state.current_step += 1
                self.state.retry_count = 0
                continue
            else:
                self.state.retry_count += 1
                if self.state.retry_count >= 3:
                    self.state.final_status = UpgradeFailed
                    return
                continue
        self.state.final_status = UpgradeCompleted

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
        previous_step = self.state.steps[self.state.current_step - 1] if self.state.current_step > 0 else None
        previous_step_id = previous_step["id"] if previous_step else ""
        previous_step_summary_path = (
            WORKSPACE_DIR / STEPS_DIR / previous_step_id / EACH_STEP_SUMMARY_FILE
            if previous_step_id else ""
        )
        is_last_step = self.state.current_step == self.state.total_steps - 1

        vllm_path = self.state.vllm_path
        ascend_path = self.state.vllm_ascend_path

        if self.state.retry_count == 0:
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
            print(f"[ai_analysis] {step_id}: retry count {self.state.retry_count}, \
 skipping to fix mode")

        error_logs: list[str] = list(self.state.test_errors)
        patch_path = step_dir / VLLM_GIT_PATCH_FILE
        changed_files_path = step_dir / VLLM_GIT_CHANGED_FILES
        adapt_result: AdaptResult | None = None

        for attempt in range(1, 4):
            mode = "fix" if error_logs else "adapt"
            print(f"[ai_analysis] {step_id}: opencode attempt {attempt}, mode={mode}")
            adapt_result = run_opencode_adapter({
                "step_id": step_id,
                "previous_step_id": previous_step_id,
                "previous_step_summary_path": str(previous_step_summary_path),
                "is_last_step": is_last_step,
                "step_dir": str(step_dir),
                "patch_path": str(patch_path),
                "changed_files_path": str(changed_files_path),
                "ascend_path": ascend_path,
                "release_tag": self.state.release_tag,
                "vllm_path": vllm_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": mode,
                "error_logs": json.dumps(error_logs, ensure_ascii=False),
                "code_structure_guide_file": EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
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

        summary_path = step_dir / EACH_STEP_SUMMARY_FILE
        if adapt_result and adapt_result.step_summary and not summary_path.exists():
            summary_path.write_text(adapt_result.step_summary, encoding="utf-8")

        adaptation_patch_path = step_dir / EACH_STEP_TARGET_PATCH_FILE
        adaptation_patch = run_git(ascend_path, "diff", "HEAD")
        adaptation_patch_path.write_text(adaptation_patch, encoding="utf-8")

        changed_files = run_git(ascend_path, "diff", "--name-only", "HEAD").strip().splitlines()
        changed_files = [f for f in changed_files if f]  # filter empty lines

        ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        self.state.cur_vllm_commit = step["end_commit"]
        self.state.cur_ascend_commit = ascend_head
        self.state.cur_patch_path = str(adaptation_patch_path)
        self.state.changed_files = changed_files

        print(f"[ai_analysis] {step_id}: done, "
              f"is_noop={getattr(adapt_result, 'is_noop', False)}, "
              f"modified={getattr(adapt_result, 'modified_files', [])}, "
              f"vllm={step['end_commit'][:8]}, ascend={ascend_head[:8]}")

    def _run_e2e_test(self):
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        print(f"run_e2e_test: step-{step_id} round={self.state.retry_count}")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            print(f"[run_e2e_test] SKIP_E2E_TEST=true, treating as passed")
            return True

        print(f"The adaptation patch is at: {self.state.cur_patch_path}")
        result = run_tests(
            vllm_path=self.state.vllm_path,
            vllm_commit=self.state.cur_vllm_commit,
            ascend_path=self.state.vllm_ascend_path,
            ascend_commit=self.state.cur_ascend_commit,
            patch_path=self.state.cur_patch_path or None,
            step_id=step_id,
            select_by_files=self.state.changed_files or None,
            test_cases=_parse_test_cases_env(),
            remote=os.getenv("MAIN2MAIN_RUN_TESTS_REMOTE") or None,
            round_number=self.state.retry_count,
            log_dir=str(WORKSPACE_DIR / STEPS_DIR),
        )

        test_passed = result.get("can_commit", False)
        summary_log = str(WORKSPACE_DIR / STEPS_DIR / str(step_id) / "tests" / f"round-{self.state.retry_count}-summary.json")
        print(f"test_passed={test_passed}, ci_result={result.get('ci_result')}")

        if not test_passed:
            self.state.test_errors = [summary_log]

        return test_passed

    @listen(process_steps)
    def generate_final_post(self):
        # The last successful step's patch is cumulative: git diff HEAD after all
        # successful adaptations. Prefer its cumulative summary, and fall back to
        # concatenating available step summaries if the last one is missing.
        if self.state.current_step == 0:
            print(f"[generate_final_post] fail to upgrade, no step success")
            return

        last_step = self.state.steps[self.state.current_step - 1]
        step_dir = WORKSPACE_DIR / STEPS_DIR / last_step["id"]
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        last_summary_path = step_dir / EACH_STEP_SUMMARY_FILE

        if last_summary_path.exists():
            shutil.copy2(last_summary_path, final_summary_path)
        else:
            summaries = []
            for index in range(self.state.current_step):
                current_step = self.state.steps[index]
                summary_path = WORKSPACE_DIR / STEPS_DIR / current_step["id"] / EACH_STEP_SUMMARY_FILE
                if summary_path.exists():
                    summaries.append(summary_path.read_text(encoding="utf-8"))
            final_summary_path.write_text("\n\n".join(summaries), encoding="utf-8")

        shutil.copy2(step_dir / EACH_STEP_TARGET_PATCH_FILE, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)

        status = "completed" if self.state.final_status == UpgradeCompleted else "failed"
        status_json = {
            "status": status,
            "steps_completed": self.state.current_step,
            "steps_total": self.state.total_steps,
            "reached_commit": self.state.cur_vllm_commit,
            "old_commit": self.state.base_commit,
            "new_commit": self.state.target_commit or self.state.cur_vllm_commit,
        }
        (WORKSPACE_DIR / "final_status.json").write_text(
            json.dumps(status_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        last_guide_path = step_dir / EACH_STEP_CODE_STRUCTURE_GUIDE_FILE
        if last_guide_path.exists():
            shutil.copy2(last_guide_path, WORKSPACE_DIR / FINAL_CODE_STRUCTURE_GUIDE_FILE)
            print(f"[generate_final_post] Copied code-structure-guide to workspace.")

        if os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() != "true":
            vllm_path = self.state.vllm_path
            ascend_path = self.state.vllm_ascend_path
            run_git(vllm_path, "checkout", self.state.original_vllm_ref)
            print(f"[generate_final_post] Restored vllm to '{self.state.original_vllm_ref}'.")
            run_git(ascend_path, "checkout", "-f", self.state.original_ascend_ref)
            print(f"[generate_final_post] Restored vllm-ascend to '{self.state.original_ascend_ref}'.")

    @listen(generate_final_post)
    def push_to_github(self):
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            print("[push] PUSH_TO_GITHUB is not true, skipping.")
            return "SKIP_PUSH"

        github_repo = os.getenv("GITHUB_REPO", "")
        if not github_repo:
            print("[push] GITHUB_REPO is empty, cannot create PR.")
            return "SKIP_PUSH"

        head_fork = os.getenv("HEAD_FORK", "")
        draft = os.getenv("PR_DRAFT", "true").lower() == "true"
        labels_str = os.getenv("PR_LABELS", "ready,ready-for-test")
        labels = [lbl.strip() for lbl in labels_str.split(",") if lbl.strip()]
        branch_name = os.getenv("PR_BRANCH_NAME", "")

        return push_and_create_pr(
            ascend_path=Path(self.state.vllm_ascend_path),
            github_repo=github_repo,
            patch_path=WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE,
            summary_path=WORKSPACE_DIR / FINAL_SUMMARY_FILE,
            old_commit=self.state.base_commit,
            new_commit=self.state.target_commit or self.state.cur_vllm_commit,
            head_fork=head_fork,
            draft=draft,
            labels=labels,
            branch_name=branch_name,
        )

