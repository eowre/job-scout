"""
test_routes_found_jobs.py — Endpoint tests for /found-jobs router.
"""
import json
from datetime import datetime

import pytest

from database import DiscoveredJob, Job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(db, **kwargs) -> DiscoveredJob:
    defaults = dict(
        ats_job_id=f"test_{id(kwargs)}",
        company="Test Co",
        title="Software Engineer",
        url="https://example.com/job/1",
        location="Remote",
        department="Engineering",
        raw_description="We are looking for a software engineer with Python experience.",
        discovered_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    job = DiscoveredJob(**defaults)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _promote_for_user(db, client, discovered: DiscoveredJob) -> Job:
    """Put a discovered job in the test user's pipeline (per-user semantics)."""
    job = Job(
        user_id=client.user["id"],
        title=discovered.title,
        company=discovered.company,
        jd_text=discovered.raw_description or "",
        ats_job_id=discovered.ats_job_id,
        source="scraped",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# ---------------------------------------------------------------------------
# GET /found-jobs
# ---------------------------------------------------------------------------

class TestListFoundJobs:
    def test_empty(self, client):
        r = client.get("/found-jobs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["jobs"] == []

    def test_returns_jobs(self, client, db):
        _make_job(db, ats_job_id="j1", title="Software Engineer")
        _make_job(db, ats_job_id="j2", title="DevOps Engineer")
        r = client.get("/found-jobs")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_search_by_title(self, client, db):
        _make_job(db, ats_job_id="s1", title="Solutions Engineer")
        _make_job(db, ats_job_id="s2", title="Marketing Manager")
        r = client.get("/found-jobs?q=solutions")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["jobs"][0]["title"] == "Solutions Engineer"

    def test_search_by_company(self, client, db):
        _make_job(db, ats_job_id="c1", company="Stripe", title="SWE")
        _make_job(db, ats_job_id="c2", company="Notion", title="SWE")
        r = client.get("/found-jobs?q=stripe")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_filter_added_to_pipeline(self, client, db):
        promoted = _make_job(db, ats_job_id="p1")
        _promote_for_user(db, client, promoted)
        _make_job(db, ats_job_id="p2")
        r = client.get("/found-jobs?added=true")
        assert r.json()["total"] == 1

    def test_filter_not_added(self, client, db):
        promoted = _make_job(db, ats_job_id="np1")
        _promote_for_user(db, client, promoted)
        _make_job(db, ats_job_id="np2")
        r = client.get("/found-jobs?added=false")
        assert r.json()["total"] == 1

    def test_pagination(self, client, db):
        for i in range(5):
            _make_job(db, ats_job_id=f"pg{i}")
        r = client.get("/found-jobs?limit=2&skip=0")
        assert len(r.json()["jobs"]) == 2
        assert r.json()["total"] == 5

    def test_serialize_fields(self, client, db):
        _make_job(db, ats_job_id="field_test", title="SWE", company="Acme",
                  location="NYC", department="Eng")
        r = client.get("/found-jobs")
        job = r.json()["jobs"][0]
        assert "id" in job
        assert "ats_job_id" in job
        assert "scouted_at" in job
        assert "added_to_pipeline" in job


# ---------------------------------------------------------------------------
# GET /found-jobs/analytics
# ---------------------------------------------------------------------------

class TestAnalytics:
    def test_analytics_empty(self, client):
        r = client.get("/found-jobs/analytics")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["avg_yoe"] is None

    def test_analytics_counts(self, client, db):
        _make_job(db, ats_job_id="a1", company="Acme", years_of_experience=3.0)
        _make_job(db, ats_job_id="a2", company="Acme", years_of_experience=5.0)
        _make_job(db, ats_job_id="a3", company="Stripe", years_of_experience=None)
        r = client.get("/found-jobs/analytics")
        data = r.json()
        assert data["total"] == 3
        assert data["avg_yoe"] == 4.0
        assert data["yoe_coverage"] == 2

    def test_top_companies(self, client, db):
        for i in range(3):
            _make_job(db, ats_job_id=f"tc{i}", company="BigCo")
        _make_job(db, ats_job_id="tc_other", company="SmallCo")
        r = client.get("/found-jobs/analytics")
        companies = {c["name"]: c["count"] for c in r.json()["top_companies"]}
        assert companies["BigCo"] == 3
        assert companies["SmallCo"] == 1

    def test_in_pipeline_count(self, client, db):
        promoted = _make_job(db, ats_job_id="pip1")
        _promote_for_user(db, client, promoted)
        _make_job(db, ats_job_id="pip2")
        data = client.get("/found-jobs/analytics").json()
        assert data["in_pipeline"] == 1


# ---------------------------------------------------------------------------
# GET /found-jobs/{job_id}
# ---------------------------------------------------------------------------

class TestGetFoundJob:
    def test_get_existing(self, client, db):
        job = _make_job(db, ats_job_id="gj1")
        r = client.get(f"/found-jobs/{job.id}")
        assert r.status_code == 200
        assert r.json()["id"] == job.id

    def test_get_nonexistent(self, client):
        r = client.get("/found-jobs/99999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /found-jobs/{job_id}/promote
# ---------------------------------------------------------------------------

class TestPromoteJob:
    def test_promote_creates_pipeline_job(self, client, db):
        job = _make_job(db, ats_job_id="promo1")
        r = client.post(f"/found-jobs/{job.id}/promote")
        assert r.status_code == 200
        data = r.json()
        assert "pipeline_job_id" in data
        assert data["already_added"] is False

        pipeline_job = db.query(Job).filter(Job.id == data["pipeline_job_id"]).first()
        assert pipeline_job is not None
        assert pipeline_job.user_id == client.user["id"]
        assert pipeline_job.ats_job_id == job.ats_job_id

        # The listing reflects the promotion for this user
        listed = client.get(f"/found-jobs/{job.id}").json()
        assert listed["added_to_pipeline"] is True
        assert listed["pipeline_job_id"] == data["pipeline_job_id"]

    def test_promote_twice_returns_already_added(self, client, db):
        job = _make_job(db, ats_job_id="promo2")
        r1 = client.post(f"/found-jobs/{job.id}/promote")
        r2 = client.post(f"/found-jobs/{job.id}/promote")
        assert r2.status_code == 200
        assert r2.json()["already_added"] is True
        assert r2.json()["pipeline_job_id"] == r1.json()["pipeline_job_id"]

    def test_promote_nonexistent(self, client):
        r = client.post("/found-jobs/99999/promote")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /found-jobs/{job_id}
# ---------------------------------------------------------------------------

class TestDeleteFoundJob:
    def test_delete_job(self, client, db):
        job = _make_job(db, ats_job_id="del1")
        r = client.delete(f"/found-jobs/{job.id}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        r2 = client.get(f"/found-jobs/{job.id}")
        assert r2.status_code == 404

    def test_cannot_delete_pipeline_job(self, client, db):
        job = _make_job(db, ats_job_id="del2")
        _promote_for_user(db, client, job)
        r = client.delete(f"/found-jobs/{job.id}")
        assert r.status_code == 409

    def test_delete_nonexistent(self, client):
        r = client.delete("/found-jobs/99999")
        assert r.status_code == 404
