import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.api_key import hash_api_key
from app.auth.jwt_handler import create_token
from app.core.config import get_settings
from app.db.database import Database
from app.main import create_app

TEST_API_KEY = "test-api-key-for-testing-purposes-1234567890abcdef"
TEST_JWT_SECRET = "test-secret-key-for-jwt-signing"


@pytest.fixture
def anyio_backend():
    """Pin anyio-marked tests to asyncio. aiosqlite/Database is asyncio-only,
    so the trio parametrization fails with 'no current event loop'."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear lru_cache on get_settings so env var changes take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Reset module-level rate limiter buckets between tests.

    Limiters in app.middleware.dependencies are module-level singletons;
    without reset, /token tests sharing IP exhaust 5/min and 6th hits 429.
    """
    from app.middleware import dependencies as deps

    for name in ("_read_limiter", "_write_limiter", "_exec_limiter", "_global_limiter", "_auth_limiter"):
        limiter = getattr(deps, name, None)
        if limiter is not None:
            limiter._buckets.clear()
    return


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEFAULT_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    # rag.py METRICS_DB path: prod /opt/linux-ai-server/data/rag_metrics.db
    # CI runner'da yok; tmp_path uzerinde test-only metrics dosyasi kullan.
    rag_metrics = tmp_path / "rag_metrics.db"
    monkeypatch.setenv("RAG_METRICS_DB", str(rag_metrics))
    # rag modulu zaten yuklenmis olabilir; module-level constant da yamala.
    monkeypatch.setattr("app.api.rag.METRICS_DB", str(rag_metrics))
    # Prevent YAML config from being loaded (nested keys break flat Settings)
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})

    # Redirect AgentRegistry away from /var/AI-stump (no perms in CI).
    # Import here so the module-level _registry already exists.
    from app.api import agents as agents_module

    monkeypatch.setattr(agents_module._registry, "_agents_dir", str(tmp_path / "agents"))

    return create_app()


@pytest.fixture
def admin_token() -> str:
    """Generate a valid admin JWT token for tests."""
    return create_token(subject="test-admin", permissions="admin", secret=TEST_JWT_SECRET)


@pytest.fixture
def read_token() -> str:
    """Generate a valid read-only JWT token for tests."""
    return create_token(subject="test-reader", permissions="read", secret=TEST_JWT_SECRET)


@pytest.fixture
def auth_headers(admin_token) -> dict:
    """HTTP headers with admin Bearer token."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def read_headers(read_token) -> dict:
    """HTTP headers with read-only Bearer token."""
    return {"Authorization": f"Bearer {read_token}"}


@pytest.fixture
async def client(app, tmp_path, monkeypatch):
    # Initialize DB and seed test key (lifespan doesn't run with ASGITransport)
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.initialize()

    # Seed the default admin key
    existing = await db.fetch_all("SELECT id FROM api_keys LIMIT 1")
    if not existing:
        await db.execute(
            "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
            (hash_api_key(TEST_API_KEY), "admin", "admin"),
        )

    app.state.db = db

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await db.close()


@pytest.fixture
async def ci_db(tmp_path):
    """Fresh aiosqlite Database for CI lesson tests, schema applied."""
    db = Database(str(tmp_path / "ci.db"))
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()
