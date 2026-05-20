"""
conftest.py — Shared fixtures for the Job Scout test suite.

Uses an in-memory SQLite database so tests never touch the real jobscout.db.
companies.json reads/writes are patched to a temp file per test.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Point the app at a fresh in-memory DB before importing anything that
# touches the real engine.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

from database import Base, get_db
from main import app

TEST_ENGINE = create_engine(
    "sqlite://",  # in-memory
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(bind=TEST_ENGINE)


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)


@pytest.fixture
def db():
    conn = TEST_ENGINE.connect()
    transaction = conn.begin()
    session = TestingSessionLocal(bind=conn)
    yield session
    session.close()
    transaction.rollback()
    conn.close()


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sample company data
# ---------------------------------------------------------------------------

SAMPLE_COMPANIES = [
    {"name": "Acme Corp",   "ats": "greenhouse", "id": "acmecorp",  "url": "https://acmecorp.com/careers", "enabled": True},
    {"name": "Lever Co",    "ats": "lever",      "id": "leverco",   "url": "https://jobs.lever.co/leverco", "enabled": True},
    {"name": "Ashby Inc",   "ats": "ashby",      "id": "ashbyinc",  "url": "https://jobs.ashbyhq.com/ashbyinc", "enabled": True},
    {"name": "Generic Ltd", "ats": "generic",    "id": "",          "url": "https://generic.io/careers", "enabled": False},
]


@pytest.fixture
def companies_file():
    """Write sample companies to a temp file and yield its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(SAMPLE_COMPANIES, f)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def patch_companies(companies_file):
    """Patch the COMPANIES_PATH used by routers and scraper_service."""
    with patch("routers.scrape.COMPANIES_PATH", companies_file), \
         patch("services.scraper_service.COMPANIES_PATH", companies_file):
        yield companies_file
