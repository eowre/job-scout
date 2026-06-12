"""
test_auth.py — Account registration, sessions, and per-user data isolation.
"""
from database import Job, Resume


class TestAuthFlow:
    def test_me_requires_login(self, client):
        client.cookies.clear()
        assert client.get("/auth/me").status_code == 401

    def test_register_and_me(self, client):
        me = client.get("/auth/me").json()
        assert me["username"] == "testuser"

    def test_duplicate_username_rejected(self, client):
        r = client.post("/auth/register", json={"username": "testuser", "password": "anotherpass1"})
        assert r.status_code == 409

    def test_login_wrong_password(self, client):
        client.cookies.clear()
        r = client.post("/auth/login", json={"username": "testuser", "password": "wrongpassword"})
        assert r.status_code == 401

    def test_login_logout(self, client):
        client.cookies.clear()
        r = client.post("/auth/login", json={"username": "testuser", "password": "testpassword1"})
        assert r.status_code == 200
        assert client.get("/auth/me").status_code == 200
        client.post("/auth/logout")
        assert client.get("/auth/me").status_code == 401

    def test_jobs_require_login(self, client):
        client.cookies.clear()
        assert client.get("/jobs/").status_code == 401
        assert client.get("/resume/list").status_code == 401


class TestDataIsolation:
    def test_first_user_adopts_orphan_data(self, client, db):
        # conftest registered the first user; orphan rows were adopted
        job = db.query(Job).filter(Job.user_id.is_(None)).first()
        assert job is None

    def test_users_see_only_their_jobs(self, client, db):
        # User 1 creates a job
        r = client.post("/jobs/", json={"title": "Mine", "jd_text": "JD text"})
        assert r.status_code == 200
        my_job_id = r.json()["id"]

        # Second user registers and sees an empty pipeline
        client.cookies.clear()
        r = client.post("/auth/register", json={"username": "other", "password": "otherpass123"})
        assert r.status_code == 200
        assert client.get("/jobs/").json() == []
        # And cannot fetch user 1's job
        assert client.get(f"/jobs/{my_job_id}").status_code == 404

    def test_users_see_only_their_resumes(self, client, db):
        db.add(Resume(user_id=client.user["id"], filename="mine.pdf", raw_text="text"))
        db.commit()
        assert len(client.get("/resume/list").json()) == 1

        client.cookies.clear()
        client.post("/auth/register", json={"username": "other2", "password": "otherpass123"})
        assert client.get("/resume/list").json() == []

    def test_promotion_is_per_user(self, client, db):
        from database import DiscoveredJob
        d = DiscoveredJob(ats_job_id="iso1", company="Acme", title="SWE",
                          url="https://x.com/1", raw_description="desc")
        db.add(d)
        db.commit()
        db.refresh(d)

        r = client.post(f"/found-jobs/{d.id}/promote")
        assert r.status_code == 200
        assert client.get(f"/found-jobs/{d.id}").json()["added_to_pipeline"] is True

        # Second user sees it as not promoted, and can promote independently
        client.cookies.clear()
        client.post("/auth/register", json={"username": "other3", "password": "otherpass123"})
        assert client.get(f"/found-jobs/{d.id}").json()["added_to_pipeline"] is False
        r2 = client.post(f"/found-jobs/{d.id}/promote")
        assert r2.status_code == 200
        assert r2.json()["already_added"] is False
        assert r2.json()["pipeline_job_id"] != r.json()["pipeline_job_id"]
