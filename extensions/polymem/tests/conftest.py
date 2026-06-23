"""Shared fixtures: throwaway SQLite + TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from polymem import create_app

TEST_KEY = "test-key-xyz"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "polymem-test.db"


@pytest.fixture
def client(db_path):
    app = create_app(db_path=db_path, api_key=TEST_KEY)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-Memory-Key": TEST_KEY}


@pytest.fixture
def client_noauth(db_path):
    """Client with auth disabled — for testing the optional perimeter mode."""
    app = create_app(db_path=db_path, api_key=None)
    with TestClient(app) as c:
        yield c
