"""
test_notifier.py — Tests for alert-keyword filtering in services/notifier.py.

Two layers are tested:

1. Settings API (PUT /settings) — verifies the endpoint correctly accepts,
   strips, and returns alert_keywords exactly as the Settings tab sends them.

2. _is_alert_worthy — verifies the notifier reads keywords from load_settings
   and matches titles correctly. We patch load_settings to return specific
   keyword lists (simulating what the Settings tab would have stored).
"""
import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings_with_keywords(keywords: list[str]) -> dict:
    """Return a minimal load_settings() dict with the given keyword list."""
    return {
        "discord_webhook_url": "",
        "slack_webhook_url":   "",
        "alert_keywords":      json.dumps(keywords),
        "check_interval_hours": "2",
    }


def _put_keywords(client, keywords: list[str]):
    """Call PUT /settings as the Settings tab does and return parsed body."""
    resp = client.put("/settings", json={"alert_keywords": keywords})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Settings API — stores and returns alert_keywords correctly
# ---------------------------------------------------------------------------

class TestSettingsApi:
    def test_saves_and_returns_keyword_list(self, client):
        with patch("database.save_setting"), \
             patch("database.load_settings", return_value=_settings_with_keywords(["solution", "forward deployed", "fde"])):
            data = _put_keywords(client, ["solution", "forward deployed", "fde"])
        assert "solution" in data["alert_keywords"]
        assert "forward deployed" in data["alert_keywords"]

    def test_strips_whitespace_from_keywords(self, client):
        with patch("database.save_setting"), \
             patch("database.load_settings", return_value=_settings_with_keywords(["solution", "fde"])):
            data = _put_keywords(client, ["  solution  ", " fde "])
        assert "solution" in data["alert_keywords"]
        assert "fde" in data["alert_keywords"]

    def test_removes_blank_entries(self, client):
        with patch("database.save_setting"), \
             patch("database.load_settings", return_value=_settings_with_keywords(["solution"])):
            data = _put_keywords(client, ["solution", "", "  "])
        assert data["alert_keywords"] == ["solution"]

    def test_returns_list_not_json_string(self, client):
        with patch("database.save_setting"), \
             patch("database.load_settings", return_value=_settings_with_keywords(["solution"])):
            data = _put_keywords(client, ["solution"])
        assert isinstance(data["alert_keywords"], list)


# ---------------------------------------------------------------------------
# _is_alert_worthy — reads keywords from settings DB at call time
# ---------------------------------------------------------------------------

class TestIsAlertWorthy:
    """Each test patches load_settings to simulate what the Settings tab stored."""

    from services.notifier import _is_alert_worthy  # imported at class body level for brevity

    def test_solution_matches(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(["solution", "forward deployed"])):
            assert _is_alert_worthy("Solutions Engineer") is True

    def test_solution_case_insensitive(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(["solution"])):
            assert _is_alert_worthy("SOLUTION ARCHITECT") is True
            assert _is_alert_worthy("Solution Consultant") is True

    def test_non_matching_title(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(["solution", "forward deployed"])):
            assert _is_alert_worthy("Software Engineer") is False

    def test_empty_title(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(["solution"])):
            assert _is_alert_worthy("") is False

    def test_all_default_keywords_match(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(
            ["solution", "forward deployed", "forward-deployed", "fde"]
        )):
            assert _is_alert_worthy("Forward Deployed Engineer") is True
            assert _is_alert_worthy("Forward-Deployed Engineer") is True
            assert _is_alert_worthy("FDE Platform Specialist") is True
            assert _is_alert_worthy("Solution Architect") is True

    def test_substring_match(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords(["solution"])):
            assert _is_alert_worthy("Pre-solution Sales Engineer") is True

    def test_empty_keywords_never_match(self):
        from services.notifier import _is_alert_worthy
        with patch("database.load_settings", return_value=_settings_with_keywords([])):
            assert _is_alert_worthy("Solutions Engineer") is False
