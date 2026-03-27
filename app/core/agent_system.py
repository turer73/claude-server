"""Agent system — YAML-defined agents with tool execution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

from app.exceptions import NotFoundError


@dataclass
class AgentDefinition:
    """Agent definition — can be loaded from YAML or created programmatically."""
    name: str
    description: str
    trigger: str = "manual"  # manual, cron, event
    schedule: str | None = None
    tools: list[str] = field(default_factory=list)
    system_prompt: str | None = None
    steps: list[dict] | None = None
    status: str = "idle"
    last_run: str | None = None
    last_result: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Remove runtime fields
        d.pop("status", None)
        d.pop("last_run", None)
        d.pop("last_result", None)
        return d


class AgentRegistry:
    """Registry of agent definitions — loads from YAML files, supports hot-reload."""

    def __init__(self, agents_dir: str = "/var/AI-stump/agents") -> None:
        self._agents_dir = agents_dir
        self._agents: dict[str, AgentDefinition] = {}

    def load_from_directory(self) -> int:
        """Load all YAML agent definitions from directory. Returns count loaded."""
        loaded = 0
        if not os.path.isdir(self._agents_dir):
            return 0
        for filename in os.listdir(self._agents_dir):
            if not filename.endswith((".yml", ".yaml")):
                continue
            filepath = os.path.join(self._agents_dir, filename)
            try:
                with open(filepath) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict) or "name" not in data:
                    continue
                agent = AgentDefinition(
                    name=data["name"],
                    description=data.get("description", ""),
                    trigger=data.get("trigger", "manual"),
                    schedule=data.get("schedule"),
                    tools=data.get("tools", []),
                    system_prompt=data.get("system_prompt"),
                    steps=data.get("steps"),
                )
                self._agents[agent.name] = agent
                loaded += 1
            except (yaml.YAMLError, KeyError, TypeError):
                continue
        return loaded

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.name] = agent

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)

    def get(self, name: str) -> AgentDefinition:
        agent = self._agents.get(name)
        if not agent:
            raise NotFoundError(f"Agent '{name}' not found")
        return agent

    def list_agents(self) -> list[dict]:
        return [
            {
                "name": a.name,
                "description": a.description,
                "trigger": a.trigger,
                "tools": a.tools,
                "status": a.status,
            }
            for a in self._agents.values()
        ]

    def save_agent(self, name: str) -> str:
        agent = self.get(name)
        os.makedirs(self._agents_dir, exist_ok=True)
        filepath = os.path.join(self._agents_dir, f"{name}.yml")
        with open(filepath, "w") as f:
            yaml.dump(agent.to_dict(), f, default_flow_style=False)
        return filepath


class AgentRunner:
    """Execute agent steps using registered tools."""

    def __init__(self, tool_registry: dict | None = None) -> None:
        self._tools = tool_registry or {}

    def register_tool(self, name: str, func: callable) -> None:
        self._tools[name] = func

    async def run(self, agent: AgentDefinition, params: dict | None = None) -> dict:
        results = []
        agent.status = "running"
        try:
            if agent.steps:
                for step in agent.steps:
                    tool_name = step.get("tool")
                    tool_params = step.get("params", {})
                    if params:
                        tool_params.update(params)
                    if tool_name in self._tools:
                        result = self._tools[tool_name](**tool_params)
                        results.append({"tool": tool_name, "result": str(result)})
                    else:
                        results.append({"tool": tool_name, "result": f"Tool '{tool_name}' not found"})
            agent.status = "idle"
            agent.last_result = str(results)
            return {"agent": agent.name, "results": results, "status": "completed"}
        except Exception as e:
            agent.status = "error"
            agent.last_result = str(e)
            return {"agent": agent.name, "error": str(e), "status": "error"}
