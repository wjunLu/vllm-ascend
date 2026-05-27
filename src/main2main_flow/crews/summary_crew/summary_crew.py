from pathlib import Path
from typing import Type

from pydantic import BaseModel, Field

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task
from crewai.tools import BaseTool


class _ListFilesInput(BaseModel):
    directory: str = Field(description="Directory path to list files from")
    pattern: str = Field(default="*", description="Glob pattern to filter files, e.g. '*.log' or 'summary_*.md'")


class ListFilesTool(BaseTool):
    name: str = "list_files"
    description: str = (
        "List files in a directory. Optionally filter by glob pattern such as "
        "'*.log', 'summary_*.md', or '*.patch'. Returns sorted file paths, one per line."
    )
    args_schema: Type[BaseModel] = _ListFilesInput

    def _run(self, directory: str, pattern: str = "*") -> str:
        path = Path(directory)
        if not path.exists():
            return f"Directory not found: {directory}"
        matches = sorted(path.glob(pattern))
        if not matches:
            return f"No files matching '{pattern}' in {directory}"
        return "\n".join(str(f) for f in matches)


class _ReadFileInput(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")


class ReadFileTool(BaseTool):
    name: str = "read_file"
    description: str = "Read and return the full text content of a file."
    args_schema: Type[BaseModel] = _ReadFileInput

    def _run(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading {file_path}: {exc}"


@CrewBase
class SummaryCrew:
    """Summary Crew — generates the final main2main run summary report."""

    agents: list[BaseAgent]
    tasks: list[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def summary_reporter(self) -> Agent:
        return Agent(
            config=self.agents_config["summary_reporter"],  # type: ignore[index]
            tools=[ListFilesTool(), ReadFileTool()],
        )

    @task
    def generate_summary_task(self) -> Task:
        return Task(
            config=self.tasks_config["generate_summary_task"],  # type: ignore[index]
        )

    @crew
    def crew(self) -> Crew:
        """Creates the Summary Crew"""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
