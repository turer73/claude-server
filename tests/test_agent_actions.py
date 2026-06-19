"""Action/Provider deseni testleri (ElizaOS uyarlaması — app/core/agents)."""

import sqlite3

from app.core.agents import ActionRegistry, RecentChangesProvider, compose
from app.core.agents.code_actions import build_code_review_registry


class _OkAction:
    name = "ok"
    description = "test"

    async def run(self, **kw):
        return {"ran": True, "kw": kw}


class _BoomAction:
    name = "boom"
    description = "hata"

    async def run(self, **kw):
        raise RuntimeError("patladı")


class _StaticProvider:
    def __init__(self, name, text):
        self.name = name
        self._text = text

    async def provide(self, **kw):
        return self._text


async def test_registry_register_run():
    reg = ActionRegistry()
    reg.register(_OkAction())
    assert reg.names() == ["ok"]
    assert await reg.run("ok", x=1) == {"ran": True, "kw": {"x": 1}}


async def test_registry_unknown_action_returns_none():
    assert await ActionRegistry().run("yok") is None


async def test_registry_failing_action_fail_silent():
    """Action hata atarsa registry None döner (ajan döngüsü bozulmaz)."""
    reg = ActionRegistry()
    reg.register(_BoomAction())
    assert await reg.run("boom") is None


async def test_compose_combines_providers():
    out = await compose([_StaticProvider("a", "AAA"), _StaticProvider("b", "BBB")])
    assert "## a\nAAA" in out
    assert "## b\nBBB" in out


async def test_compose_skips_failing_provider():
    class _Bad:
        name = "bad"

        async def provide(self, **kw):
            raise ValueError("x")

    out = await compose([_Bad(), _StaticProvider("good", "G")])
    assert "## good\nG" in out
    assert "bad" not in out


async def test_recent_changes_provider(tmp_path):
    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, created_at TEXT);
        CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, project TEXT, task TEXT, created_at TEXT);
        INSERT INTO discoveries (project,type,title,created_at) VALUES ('p','fix','yakin fix', datetime('now','-1 day'));
        INSERT INTO discoveries (project,type,title,created_at) VALUES ('p','fix','eski', datetime('now','-30 days'));
        INSERT INTO tasks_log (project,task,created_at) VALUES ('k','deploy', datetime('now','-2 days'));
        """
    )
    conn.commit()
    conn.close()
    ctx = await RecentChangesProvider(str(db), days=7).provide()
    assert "yakin fix" in ctx
    assert "deploy" in ctx
    assert "eski" not in ctx  # 7g dışı


async def test_recent_changes_missing_db_safe():
    assert isinstance(await RecentChangesProvider("/nonexistent/x.db").provide(), str)


def test_code_review_registry_has_modes():
    assert set(build_code_review_registry().names()) == {"review", "learn", "research"}


async def test_code_review_learn_action(monkeypatch):
    import app.core.code_reviewer as cr

    monkeypatch.setattr(cr, "synthesize_lesson", lambda: True)
    assert await build_code_review_registry().run("learn") == {"created": True}
