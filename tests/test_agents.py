import pytest
import yaml

from app.core.agent_system import AgentDefinition as AgentDef
from app.core.agent_system import AgentRegistry


@pytest.fixture
def registry(tmp_path):
    return AgentRegistry(agents_dir=str(tmp_path))


@pytest.fixture
def sample_agent_yaml(tmp_path):
    agent_def = {
        "name": "test-agent",
        "description": "A test agent",
        "trigger": "manual",
        "tools": ["shell_exec", "file_read"],
        "system_prompt": "You are a test agent.",
        "steps": [
            {"tool": "shell_exec", "params": {"command": "echo hello"}},
        ],
    }
    path = tmp_path / "test-agent.yml"
    path.write_text(yaml.dump(agent_def))
    return path


def test_registry_empty(registry):
    assert registry.list_agents() == []


def test_registry_load_yaml(registry, sample_agent_yaml):
    registry.load_from_directory()
    agents = registry.list_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "test-agent"


def test_registry_get_agent(registry, sample_agent_yaml):
    registry.load_from_directory()
    agent = registry.get("test-agent")
    assert agent is not None
    assert agent.name == "test-agent"
    assert "shell_exec" in agent.tools


def test_registry_get_not_found(registry):
    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        registry.get("nonexistent")


def test_registry_register_agent(registry):
    agent = AgentDef(
        name="custom-agent",
        description="Custom agent",
        trigger="manual",
        tools=["file_read"],
    )
    registry.register(agent)
    assert registry.get("custom-agent").name == "custom-agent"


def test_registry_unregister(registry):
    agent = AgentDef(name="temp", description="temp", trigger="manual", tools=[])
    registry.register(agent)
    registry.unregister("temp")
    assert len(registry.list_agents()) == 0


def test_registry_save_yaml(registry, tmp_path):
    agent = AgentDef(
        name="saved-agent",
        description="Will be saved",
        trigger="cron",
        schedule="*/5 * * * *",
        tools=["shell_exec"],
    )
    registry.register(agent)
    registry.save_agent("saved-agent")
    saved_path = tmp_path / "saved-agent.yml"
    assert saved_path.exists()
    data = yaml.safe_load(saved_path.read_text())
    assert data["name"] == "saved-agent"
    assert data["schedule"] == "*/5 * * * *"


def test_agent_definition_dataclass():
    agent = AgentDef(
        name="test",
        description="test agent",
        trigger="manual",
        tools=["shell_exec"],
        system_prompt="You are helpful.",
        steps=[{"tool": "shell_exec", "params": {"command": "ls"}}],
    )
    assert agent.name == "test"
    assert len(agent.steps) == 1


def test_agent_definition_to_dict():
    agent = AgentDef(name="test", description="test", trigger="manual", tools=[])
    d = agent.to_dict()
    assert d["name"] == "test"
    assert isinstance(d, dict)


def test_registry_load_invalid_yaml(registry, tmp_path):
    """Invalid YAML should be skipped, not crash."""
    (tmp_path / "bad.yml").write_text("not: valid: yaml: [[[")
    registry.load_from_directory()  # Should not raise
    assert len(registry.list_agents()) == 0


def test_registry_load_missing_name(registry, tmp_path):
    """YAML without name field should be skipped."""
    (tmp_path / "noname.yml").write_text(yaml.dump({"description": "no name"}))
    registry.load_from_directory()
    assert len(registry.list_agents()) == 0
