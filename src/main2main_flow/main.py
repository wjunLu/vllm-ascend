#!/usr/bin/env python
import argparse
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router, or_, and_

from main2main_flow.scripts.detect_commits import detect, get_repo_head
from main2main_flow.scripts.plan_steps import run_plan
from main2main_flow.scripts.push_to_github import push_and_create_pr
from main2main_flow.utils import UpgradeCompleted, StepCompleted, UpgradeFailed, StepRetryNeeded, resolve_path, cleanup_temp_dirs
from main2main_flow.crews.summary_crew.summary_crew import SummaryCrew


class Main2MainState(BaseModel):
    # 输入：两个仓库路径，从环境变量读取
    vllm_path: str = ""
    vllm_ascend_path: str = ""
    target_commit: str = ""   # 可选：指定升级目标 commit，默认使用 vllm HEAD

    # Step 1 输出：后续 step 需要读取
    has_drift: bool = False        # vllm 上游是否有未同步的 commit
    steps: list = []              # 规划好的 adaptation 步骤列表

    # 流程计数，原来放在 self.xxx 实例属性上
    current_step: int = 0
    retry_count: int = 0


class Main2MainFlow(Flow[Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._temp_dirs: list = []

    @start()
    def initialize(self):
        raw_vllm = (
            self.state.vllm_path or os.getenv("VLLM_PATH")
        )
        raw_ascend = (
            self.state.vllm_ascend_path or os.getenv("VLLM_ASCEND_PATH")
        )
        self.state.vllm_path = resolve_path(raw_vllm, "vllm", self._temp_dirs)
        self.state.vllm_ascend_path = resolve_path(raw_ascend, "vllm-ascend", self._temp_dirs)
        self.state.target_commit = (
            self.state.target_commit or os.getenv("VLLM_TARGET_COMMIT", "")
        )

    @listen(initialize)
    def analyze_commit_and_plan_step(self):
        vllm_path = Path(self.state.vllm_path)
        vllm_ascend_path = Path(self.state.vllm_ascend_path)

        # 1. 检测漂移：ascend 固定的 base_commit vs 目标 commit（默认 vllm HEAD）
        result = detect(vllm_path, vllm_ascend_path,
                        target_commit=self.state.target_commit or None)
        self.state.has_drift = result["has_drift"]
        print(f"[analyze] base={result['base_commit'][:8]}  "
              f"target={result['target_commit'][:8]}  "
              f"has_drift={self.state.has_drift}")

        if not self.state.has_drift:
            print("[analyze] 已同步，无需适配。")
            return

        # 2. 规划 adaptation 步骤
        plan = run_plan(vllm_path, result["base_commit"], result["target_commit"])
        self.state.steps = plan["steps"]
        print(f"[analyze] 规划了 {len(self.state.steps)} 个步骤，"
              f"共 {plan['total_commits']} 个 commit。")
        print("===========================================")
        import json
        print(json.dumps(plan["steps"], indent=2, ensure_ascii=False))

    @router(analyze_commit_and_plan_step)
    def after_analyze(self) -> Literal["NO_DRIFT", "HAS_DRIFT"]:
        if not self.state.has_drift:
            return "NO_DRIFT"
        return "HAS_DRIFT"

    @listen("NO_DRIFT")
    def done_no_drift(self):
        print("[done] 仓库已同步，无需适配，流程结束。")

    @listen(or_("HAS_DRIFT", StepCompleted, StepRetryNeeded))
    def ai_analysis(self):
        # 逢春
        # Call Agent
        print("commit_adapt")
        return "ADAPT_OK"

    @router(ai_analysis)
    def run_e2e_test(self) -> Literal["StepCompleted", "UpgradeCompleted", "UpgradeFailed", "StepRetryNeeded"]:
        # run e2e test 卫军
        print("run_e2e_test")
        test_reslut = True
        if test_reslut:
            self.state.current_step += 1
            if self.state.current_step >= len(self.state.steps):
                return UpgradeCompleted
            else:
                return StepCompleted
        else:
            self.state.retry_count += 1
            if self.state.retry_count >= 3:
                self.state.retry_count = 0
                return UpgradeFailed
            else:
                return StepRetryNeeded

    @listen(or_(UpgradeCompleted, UpgradeFailed))
    def generate_final_post(self):
        ci_results_dir = os.getenv("CI_RESULTS_DIR", "/tmp/main2main/ci_results")
        steps_dir = os.getenv("STEPS_DIR", "/tmp/main2main/steps")
        result = (
            SummaryCrew()
            .crew()
            .kickoff(
                inputs={
                    "ci_results_dir": ci_results_dir,
                    "steps_dir": steps_dir,
                }
            )
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
    parser.add_argument(
        "--vllm-path", default=None,
        help="Local path or GitHub URL for the vllm repo",
    )
    parser.add_argument(
        "--vllm-ascend-path", default=None,
        help="Local path or GitHub URL for the vllm-ascend repo",
    )
    parser.add_argument(
        "--target-commit", default=None,
        help="Target vllm commit SHA to upgrade to (default: vllm HEAD)",
    )
    args = parser.parse_args()

    inputs = {}
    if args.vllm_path:
        inputs["vllm_path"] = args.vllm_path
    if args.vllm_ascend_path:
        inputs["vllm_ascend_path"] = args.vllm_ascend_path
    if args.target_commit:
        inputs["target_commit"] = args.target_commit

    flow = Main2MainFlow()
    try:
        flow.kickoff(inputs=inputs if inputs else None)
    finally:
        cleanup_temp_dirs(flow._temp_dirs)


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
    """
    Run the flow with trigger payload.
    """
    import json
    import sys

    # Get trigger payload from command line argument
    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided. Please provide JSON payload as argument.")

    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Invalid JSON payload provided as argument")

    # Create flow and kickoff with trigger payload
    # The @start() methods will automatically receive crewai_trigger_payload parameter
    flow = Main2MainFlow()

    try:
        result = flow.kickoff({"crewai_trigger_payload": trigger_payload})
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the flow with trigger: {e}")


if __name__ == "__main__":
    kickoff()
