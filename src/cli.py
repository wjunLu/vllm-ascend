"""CLI entry-point for ``kickoff`` console script and ``python main.py``."""
import argparse
import json
import sys
from pathlib import Path

from flow import Main2MainFlow


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
    output_dir = Path(__file__).resolve().parent.parent / "output"
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
