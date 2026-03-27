import pytest
from httpx import AsyncClient, ASGITransport
from app.main import create_app
from app.core.config import get_settings
from app.db.database import Database
from app.auth.api_key import hash_api_key
from app.auth.jwt_handler import create_token

TEST_API_KEY = "test-api-key-for-testing-purposes-1234567890abcdef"
TEST_JWT_SECRET = "test-secret-key-for-jwt-signing"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear lru_cache on get_settings so env var changes take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEFAULT_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    # Prevent YAML config from being loaded (nested keys break flat Settings)
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
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
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()
