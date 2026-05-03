import pytest

from app.db.database import Database


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.anyio
async def test_database_creates_tables(db):
    tables = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_names = [row["name"] for row in tables]
    assert "api_keys" in table_names
    assert "audit_log" in table_names
    assert "metrics_history" in table_names
    assert "alerts" in table_names
    assert "jobs" in table_names


@pytest.mark.anyio
async def test_audit_log_insert(db):
    await db.execute(
        "INSERT INTO audit_log (request_id, user, action, resource, status) VALUES (?, ?, ?, ?, ?)",
        ("req-1", "admin", "set_governor", "/kernel/governor", "success"),
    )
    rows = await db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["user"] == "admin"
    assert rows[0]["action"] == "set_governor"


@pytest.mark.anyio
async def test_api_key_insert(db):
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        ("sha256-hash-here", "test-key", "admin"),
    )
    rows = await db.fetch_all("SELECT * FROM api_keys WHERE name = ?", ("test-key",))
    assert len(rows) == 1
    assert rows[0]["permissions"] == "admin"


@pytest.mark.anyio
async def test_fetch_one(db):
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        ("hash1", "key1", "read"),
    )
    row = await db.fetch_one("SELECT * FROM api_keys WHERE name = ?", ("key1",))
    assert row is not None
    assert row["name"] == "key1"


@pytest.mark.anyio
async def test_fetch_one_not_found(db):
    row = await db.fetch_one("SELECT * FROM api_keys WHERE name = ?", ("nonexistent",))
    assert row is None


@pytest.mark.anyio
async def test_metrics_history_insert(db):
    await db.execute(
        "INSERT INTO metrics_history (cpu_usage, memory_usage, disk_usage, temperature) VALUES (?, ?, ?, ?)",
        (45.2, 62.1, 38.5, 55.0),
    )
    rows = await db.fetch_all("SELECT * FROM metrics_history")
    assert len(rows) == 1
    assert rows[0]["cpu_usage"] == 45.2


@pytest.mark.anyio
async def test_alerts_insert(db):
    await db.execute(
        "INSERT INTO alerts (severity, source, message) VALUES (?, ?, ?)",
        ("critical", "cpu", "CPU usage above 85%"),
    )
    rows = await db.fetch_all("SELECT * FROM alerts WHERE severity = ?", ("critical",))
    assert len(rows) == 1
    assert rows[0]["resolved"] == 0


@pytest.mark.anyio
async def test_jobs_insert(db):
    await db.execute(
        "INSERT INTO jobs (type, payload) VALUES (?, ?)",
        ("backup", '{"target": "/var/AI-stump/"}'),
    )
    row = await db.fetch_one("SELECT * FROM jobs WHERE type = ?", ("backup",))
    assert row is not None
    assert row["status"] == "pending"


@pytest.mark.anyio
async def test_database_not_initialized():
    database = Database("/tmp/not-init.db")
    with pytest.raises(RuntimeError, match="not initialized"):
        await database.fetch_all("SELECT 1")


@pytest.mark.anyio
async def test_indexes_created(db):
    indexes = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
    index_names = [row["name"] for row in indexes]
    assert "idx_audit_timestamp" in index_names
    assert "idx_metrics_timestamp" in index_names
    assert "idx_jobs_status" in index_names
