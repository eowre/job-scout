"""
test_routes_keywords.py — Endpoint tests for GET/PUT /config/keywords.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# A minimal config.py body for testing writes
_CONFIG_TEMPLATE = '''
JOB_KEYWORDS = [
    "software engineer",
    "devops engineer",
]

EXCLUDE_KEYWORDS = [
    "intern",
]
'''


@pytest.fixture
def config_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(_CONFIG_TEMPLATE)
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def patch_config(config_file):
    import config as cfg_module
    with patch("routers.keywords.CONFIG_PATH", config_file), \
         patch("routers.keywords.config.JOB_KEYWORDS",
               ["software engineer", "devops engineer"],
               create=True), \
         patch("routers.keywords.config.EXCLUDE_KEYWORDS",
               ["intern"],
               create=True), \
         patch("routers.keywords._reload"):  # skip live reload in tests
        yield config_file


# ---------------------------------------------------------------------------
# GET /config/keywords
# ---------------------------------------------------------------------------

class TestGetKeywords:
    def test_returns_include_and_exclude(self, client, patch_config):
        r = client.get("/config/keywords")
        assert r.status_code == 200
        data = r.json()
        assert "include" in data
        assert "exclude" in data
        assert isinstance(data["include"], list)
        assert isinstance(data["exclude"], list)

    def test_include_has_values(self, client, patch_config):
        r = client.get("/config/keywords")
        assert len(r.json()["include"]) > 0


# ---------------------------------------------------------------------------
# PUT /config/keywords
# ---------------------------------------------------------------------------

class TestUpdateKeywords:
    def test_update_include(self, client, patch_config, config_file):
        new_include = ["solutions engineer", "platform engineer"]
        r = client.put("/config/keywords",
                       json={"include": new_include, "exclude": []})
        assert r.status_code == 200
        assert set(r.json()["include"]) == set(new_include)

        # Verify written to file
        content = config_file.read_text()
        assert "solutions engineer" in content
        assert "platform engineer" in content

    def test_update_exclude(self, client, patch_config, config_file):
        r = client.put("/config/keywords",
                       json={"include": ["engineer"], "exclude": ["intern", "director"]})
        assert r.status_code == 200
        assert "director" in r.json()["exclude"]
        assert "director" in config_file.read_text()

    def test_strips_empty_strings(self, client, patch_config):
        r = client.put("/config/keywords",
                       json={"include": ["engineer", "", "  "], "exclude": []})
        assert r.status_code == 200
        assert "" not in r.json()["include"]
        assert "  " not in r.json()["include"]

    def test_strips_whitespace(self, client, patch_config):
        r = client.put("/config/keywords",
                       json={"include": ["  solutions engineer  "], "exclude": []})
        assert r.status_code == 200
        assert "solutions engineer" in r.json()["include"]

    def test_empty_lists_allowed(self, client, patch_config, config_file):
        r = client.put("/config/keywords",
                       json={"include": [], "exclude": []})
        assert r.status_code == 200
        assert r.json()["include"] == []
        assert r.json()["exclude"] == []
