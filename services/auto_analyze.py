"""One-click pipeline: discovered job -> pipeline -> score -> (tailor -> resume).

Runs as a background task; progress is tracked in-memory per discovered job so
the UI can poll for live status.
"""
import json
from datetime import datetime

from ai.resume_gen import structure_resume
from ai.scorer import score_fit
from ai.tailor import tailor_resume
from database import DiscoveredJob, Job, Resume, SessionLocal, load_settings
from services.resume_builder import build_resume_files, flatten_resume, record_generation

# (user_id, discovered_job_id) -> {stage, detail, ...result fields}
_status: dict[tuple[int, int], dict] = {}

STAGES = ("promoting", "scoring", "tailoring", "generating")


def get_status(user_id: int, discovered_id: int) -> dict | None:
    return _status.get((user_id, discovered_id))


def is_running(user_id: int, discovered_id: int) -> bool:
    s = _status.get((user_id, discovered_id))
    return bool(s) and s["stage"] in STAGES


def promote_discovered(db, discovered: DiscoveredJob, user_id: int) -> Job:
    """Copy a discovered job into the user's pipeline (idempotent per user)."""
    existing = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.ats_job_id == discovered.ats_job_id)
        .first()
    )
    if existing:
        return existing

    jd_text = discovered.parsed_summary or discovered.raw_description or ""
    job = Job(
        user_id=user_id,
        title=discovered.title,
        company=discovered.company,
        jd_text=jd_text,
        stage="Saved",
        source="scraped",
        ats_job_id=discovered.ats_job_id,
        job_url=discovered.url,
        contact_info=json.dumps({
            k: v for k, v in {
                "location": discovered.location,
                "department": discovered.department,
            }.items() if v
        }),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _tailor_threshold() -> int:
    try:
        return int(load_settings().get("auto_tailor_min_score", 70))
    except (TypeError, ValueError):
        return 70


async def run_auto_analyze(discovered_id: int, resume_id: int, user_id: int) -> None:
    status = {"stage": "promoting", "detail": "Adding to pipeline…", "started_at": datetime.utcnow().isoformat()}
    _status[(user_id, discovered_id)] = status
    db = SessionLocal()
    try:
        discovered = db.query(DiscoveredJob).filter(DiscoveredJob.id == discovered_id).first()
        resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user_id).first()
        if not discovered or not resume:
            status.update(stage="error", detail="Job or resume not found.")
            return

        job = promote_discovered(db, discovered, user_id)
        if not job.jd_text:
            status.update(stage="error", detail="This job has no description to score against.")
            return
        status["pipeline_job_id"] = job.id

        status.update(stage="scoring", detail="Scoring fit against your resume…")
        result = await score_fit(resume.raw_text, job.jd_text)
        job.fit_score = result["overall_score"]
        job.fit_breakdown = json.dumps(result)
        job.resume_id = resume.id
        db.commit()

        score = result["overall_score"]
        threshold = _tailor_threshold()
        status.update(score=score, recommendation=result["recommendation"], threshold=threshold)

        if score < threshold:
            status.update(
                stage="skipped",
                detail=f"Score {score} is below the auto-tailor threshold ({threshold}). Job saved to pipeline.",
            )
            return

        status.update(stage="tailoring", detail="Tailoring resume bullets…")
        tailored = await tailor_resume(
            resume.raw_text, job.jd_text,
            result.get("gaps", []), result.get("missing_keywords", []),
        )
        # Auto-accept every suggested bullet — no human in the loop here.
        bullets = [
            {"original": b.get("original", ""), "tailored": b.get("tailored", ""), "status": "accepted"}
            for b in tailored.get("tailored_bullets", [])
        ]
        job.tailored_bullets = json.dumps(bullets)
        db.commit()

        status.update(stage="generating", detail="Building tailored resume files…")
        structured = await structure_resume(resume.raw_text, bullets, [])
        job.tailored_resume_text = flatten_resume(structured)
        docx_path, pdf_path = build_resume_files(job, structured)
        entry = record_generation(db, job, docx_path, pdf_path)

        status.update(
            stage="done",
            detail=f"Tailored resume generated (score {score}).",
            generated_id=entry.id,
            docx_url=f"/analysis/download-generated/{entry.id}?fmt=docx",
            pdf_url=f"/analysis/download-generated/{entry.id}?fmt=pdf" if pdf_path else None,
        )
    except Exception as e:
        status.update(stage="error", detail=f"{type(e).__name__}: {e}")
    finally:
        db.close()
