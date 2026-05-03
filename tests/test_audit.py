import pytest

from app.db.database import Database
from app.middleware.audit_log import AuditLogger


@pytest.fixture
async def audit_db(tmp_path):
    db = Database(str(tmp_path / "audit.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.anyio
async def test_log_action(audit_db):
    logger = AuditLogger(audit_db)
    await logger.log(
        request_id="req-123",
        user="admin",
        action="set_governor",
        resource="/kernel/governor",
        status="success",
        details='{"mode": "performance"}',
        ip_address="192.168.1.1",
    )
    rows = await audit_db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["action"] == "set_governor"
    assert rows[0]["ip_address"] == "192.168.1.1"


@pytest.mark.anyio
async def test_log_without_optional_fields(audit_db):
    logger = AuditLogger(audit_db)
    await logger.log(
        request_id="req-456",
        user="admin",
        action="read_file",
        resource="/files/read",
        status="success",
    )
    rows = await audit_db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["details"] is None
    assert rows[0]["ip_address"] is None


@pytest.mark.anyio
async def test_log_multiple(audit_db):
    logger = AuditLogger(audit_db)
    for i in range(5):
        await logger.log(
            request_id=f"req-{i}",
            user="admin",
            action=f"action-{i}",
            resource="/test",
            status="success",
        )
    rows = await audit_db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 5


@pytest.mark.anyio
async def test_log_has_timestamp(audit_db):
    logger = AuditLogger(audit_db)
    await logger.log(
        request_id="req-ts",
        user="admin",
        action="test",
        resource="/test",
        status="success",
    )
    row = await audit_db.fetch_one("SELECT * FROM audit_log")
    assert row is not None
    assert row["timestamp"] is not None


@pytest.mark.anyio
async def test_log_error_status(audit_db):
    logger = AuditLogger(audit_db)
    await logger.log(
        request_id="req-err",
        user="admin",
        action="delete_file",
        resource="/files/delete",
        status="error",
        details='{"error": "Permission denied"}',
    )
    row = await audit_db.fetch_one("SELECT * FROM audit_log WHERE status = ?", ("error",))
    assert row is not None
    assert row["status"] == "error"
