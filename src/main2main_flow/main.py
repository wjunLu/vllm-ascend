#!/usr/bin/env python
from pathlib import Path

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router, or_

from main2main_flow.crews.content_crew.content_crew import ContentCrew


class ContentState(BaseModel):
    topic: str = ""
    outline: str = ""
    draft: str = ""
    final_post: str = ""


class ContentFlow(Flow[ContentState]):

    @start()
    def initialize(self):
        # 定义各种state、中间info信息，用于不同step之间的通信
        self.end_to_adapt = False
        self.max_step = 0
        self.step=0

    @listen(initialize)
    def analyze_commit_and_plan_step(self):
        print("analyze_commit_and_plan_step")
        self.max_step = 10
        return "Analysize"

    @router(or_(analyze_commit_and_plan_step, "NEXT_STEP"))
    def commit_adapt(self):
        if self.end_to_adapt:
            return "END"
        else:
            # Call Agent
            print("commit_adapt")
            return "ADAPT_OK"

    @router("ADAPT_OK")
    def run_e2e_test(self):
        # run e2e test
        print("run_e2e_test")
        res = True
        if res:
            self.step+=1
            return "PASS"
        else:
            return "PASS_FAIL"

    @listen("PASS")
    def pass_step(self):
        return "NEXT_STEP"

    @listen("PASS_FAIL")
    def push(self):
        return "NEXT_STEP"


def kickoff():
    content_flow = ContentFlow()
    content_flow.kickoff()


def plot():
    content_flow = ContentFlow()
    content_flow.plot()


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
    content_flow = ContentFlow()

    try:
        result = content_flow.kickoff({"crewai_trigger_payload": trigger_payload})
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the flow with trigger: {e}")


if __name__ == "__main__":
    kickoff()
