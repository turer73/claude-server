"""Agent management API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.agent_system import AgentDefinition as AgentDef
from app.core.agent_system import AgentRegistry
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import AgentDefinition

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_registry = AgentRegistry()


@router.get("/list", dependencies=[Depends(require_auth)])
async def list_agents():
    return {"agents": _registry.list_agents()}


@router.post("/create", dependencies=[Depends(require_write)])
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


@router.get("/{name}", dependencies=[Depends(require_auth)])
async def get_agent(name: str):
    agent = _registry.get(name)
    return {
        "name": agent.name,
        "description": agent.description,
        "trigger": agent.trigger,
        "tools": agent.tools,
        "status": agent.status,
    }


@router.delete("/{name}", dependencies=[Depends(require_write)])
async def delete_agent(name: str):
    _registry.unregister(name)
    return {"deleted": True}
