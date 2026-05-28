import os

from crewai import Agent, Crew, LLM, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task
from crewai_tools import FileReadTool, FileWriterTool
from pydantic import BaseModel, Field

from main2main_flow.tools.shell_tool import ShellCommandTool


class AdaptResult(BaseModel):
    """Structured output from the adapter crew for one main2main step."""

    modified_files: list[str] = Field(
        default_factory=list,
        description="vllm-ascend files modified during this step (empty if no-op).",
    )
    is_noop: bool = Field(
        default=False,
        description="True when the upstream patch requires no vllm-ascend code changes.",
    )
    step_summary: str = Field(
        default="",
        description=(
            "Human-readable step summary: upstream changes absorbed, "
            "vllm-ascend files modified, version guards added."
        ),
    )


@CrewBase
class AdapterCrew:
    """Adapter Crew — analyzes upstream vLLM patches and adapts vllm-ascend.

    Runs three sequential agents for each main2main step:
      1. patch_analyzer — analyzes the patch or errors, produces a plan
      2. code_adapter   — applies code changes based on the plan
      3. code_reviewer  — verifies changes are correct and complete

    Pre-CI verification is handled deterministically in the flow (ai_analysis).
    Failures are fed back as errors for the next crew attempt.
    """

    agents: list[BaseAgent]
    tasks: list[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def patch_analyzer(self) -> Agent:
        return Agent(
            config=self.agents_config["patch_analyzer"],  # type: ignore[index]
            tools=[FileReadTool()],
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def analyzer_QA(self) -> Agent:
        return Agent(
            config=self.agents_config["analyzer_QA"],  # type: ignore[index]
            tools=[FileReadTool()],
            allow_delegation=True,
            verbose=True,
        )

    @agent
    def code_adapter(self) -> Agent:
        return Agent(
            config=self.agents_config["code_adapter"],  # type: ignore[index]
            tools=[FileReadTool(), FileWriterTool(), ShellCommandTool()],
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def code_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["code_reviewer"],  # type: ignore[index]
            tools=[FileReadTool(), ShellCommandTool()],
            allow_delegation=True,
            verbose=True,
        )

    @task
    def analyze_patch_task(self) -> Task:
        return Task(
            config=self.tasks_config["analyze_patch_task"],  # type: ignore[index]
        )

    @task
    def analyze_qa_task(self) -> Task:
        return Task(
            config=self.tasks_config["analyze_qa_task"],  # type: ignore[index]
        )

    @task
    def adapt_code_task(self) -> Task:
        return Task(
            config=self.tasks_config["adapt_code_task"],  # type: ignore[index]
        )

    @task
    def review_code_task(self) -> Task:
        return Task(
            config=self.tasks_config["review_code_task"],  # type: ignore[index]
            output_pydantic=AdaptResult,
        )

    @crew
    def crew(self) -> Crew:
        """Creates the Adapter Crew for one main2main step."""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.hierarchical,
            manager_llm="claude-sonnet-4-6",
            verbose=True,
            output_log_file="logs.txt",
        )
