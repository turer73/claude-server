"""Agent management API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.agent_system import AgentRegistry, AgentDefinition as AgentDef
from app.models.schemas import AgentDefinition, AgentRunRequest

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_registry = AgentRegistry()


@router.get("/list")
async def list_agents():
    return {"agents": _registry.list_agents()}


@router.post("/create")
async def create_agent(body: AgentDefinition):
    agent = AgentDef(
        name=body.name,
        description=body.description,
        trigger=body.trigger,
        schedule=body.schedule,
        tools=body.tools,
        system_prompt=body.system_prompt,
        steps=body.steps,
    )
    _registry.register(agent)
    _registry.save_agent(agent.name)
    return {"created": True, "name": agent.name}


@router.get("/{name}")
async def get_agent(name: str):
    agent = _registry.get(name)
    return {
        "name": agent.name,
        "description": agent.description,
        "trigger": agent.trigger,
        "tools": agent.tools,
        "status": agent.status,
    }


@router.delete("/{name}")
async def delete_agent(name: str):
    _registry.unregister(name)
    return {"deleted": True}
