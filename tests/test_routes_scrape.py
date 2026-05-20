"""
test_routes_scrape.py — Endpoint tests for /scrape router.

External HTTP calls and Playwright are mocked throughout.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# GET /scrape/status
# ---------------------------------------------------------------------------

class TestScrapeStatus:
    def test_returns_status(self, client):
        r = client.get("/scrape/status")
        assert r.status_code == 200
        data = r.json()
        assert "running" in data
        assert "last_run" in data

    def test_not_running_by_default(self, client):
        r = client.get("/scrape/status")
        assert r.json()["running"] is False


# ---------------------------------------------------------------------------
# GET /scrape/companies
# ---------------------------------------------------------------------------

class TestListCompanies:
    def test_returns_list(self, client, patch_companies):
        r = client.get("/scrape/companies")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 4
        assert data[0]["name"] == "Acme Corp"

    def test_company_has_expected_fields(self, client, patch_companies):
        r = client.get("/scrape/companies")
        co = r.json()[0]
        assert "name" in co
        assert "ats" in co
        assert "url" in co
        assert "enabled" in co


# ---------------------------------------------------------------------------
# PATCH /scrape/companies/{name} — toggle enabled
# ---------------------------------------------------------------------------

class TestToggleCompany:
    def test_disable_company(self, client, patch_companies):
        r = client.patch("/scrape/companies/Acme Corp",
                         json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        companies = client.get("/scrape/companies").json()
        acme = next(c for c in companies if c["name"] == "Acme Corp")
        assert acme["enabled"] is False

    def test_enable_company(self, client, patch_companies):
        # Generic Ltd starts disabled
        r = client.patch("/scrape/companies/Generic Ltd",
                         json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["enabled"] is True

    def test_not_found(self, client, patch_companies):
        r = client.patch("/scrape/companies/No Such Company",
                         json={"enabled": True})
        assert r.status_code == 404

    def test_case_insensitive(self, client, patch_companies):
        r = client.patch("/scrape/companies/acme corp",
                         json={"enabled": False})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# PUT /scrape/companies/{name} — full update
# ---------------------------------------------------------------------------

class TestUpdateCompany:
    def test_update_url(self, client, patch_companies):
        new_url = "https://acme.com/jobs"
        r = client.put("/scrape/companies/Acme Corp",
                       json={"url": new_url})
        assert r.status_code == 200
        assert r.json()["url"] == new_url

        companies = client.get("/scrape/companies").json()
        acme = next(c for c in companies if c["name"] == "Acme Corp")
        assert acme["url"] == new_url

    def test_update_ats(self, client, patch_companies):
        r = client.put("/scrape/companies/Acme Corp",
                       json={"ats": "lever", "id": "acme-lever"})
        assert r.status_code == 200
        assert r.json()["ats"] == "lever"

    def test_update_multiple_fields(self, client, patch_companies):
        r = client.put("/scrape/companies/Lever Co",
                       json={"url": "https://new.url", "enabled": False})
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == "https://new.url"
        assert body["enabled"] is False

    def test_not_found(self, client, patch_companies):
        r = client.put("/scrape/companies/Nonexistent",
                       json={"url": "https://x.com"})
        assert r.status_code == 404

    def test_empty_body_ok(self, client, patch_companies):
        """Sending no fields should succeed without changing anything."""
        r = client.put("/scrape/companies/Acme Corp", json={})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /scrape/runs
# ---------------------------------------------------------------------------

class TestScrapeRuns:
    def test_returns_list(self, client):
        r = client.get("/scrape/runs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# POST /scrape/trigger
# ---------------------------------------------------------------------------

class TestTriggerScan:
    def test_trigger_starts_scan(self, client):
        with patch("services.scraper_service._running", False), \
             patch("asyncio.create_task") as mock_task:
            r = client.post("/scrape/trigger")
            assert r.status_code == 200
            assert r.json()["started"] is True
            mock_task.assert_called_once()

    def test_trigger_rejected_when_running(self, client):
        with patch("services.scraper_service._running", True):
            r = client.post("/scrape/trigger")
            assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /scrape/validate/one
# ---------------------------------------------------------------------------

class TestValidateOne:
    def test_reachable_url(self, client):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            r = client.post("/scrape/validate/one",
                            json={"name": "Test Co", "url": "https://example.com/careers"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["http_status"] == 200

    def test_no_url(self, client):
        r = client.post("/scrape/validate/one",
                        json={"name": "Test Co", "url": ""})
        assert r.status_code == 200
        assert r.json()["status"] == "no_url"
