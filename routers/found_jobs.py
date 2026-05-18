import asyncio
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, DiscoveredJob, Job

router = APIRouter(prefix="/found-jobs", tags=["found-jobs"])


def _serialize(j: DiscoveredJob) -> dict:
    responsibilities = []
    if j.responsibilities:
        try:
            responsibilities = json.loads(j.responsibilities)
        except Exception:
            pass
    return {
        "id": j.id,
        "ats_job_id": j.ats_job_id,
        "company": j.company,
        "title": j.title,
        "url": j.url,
        "location": j.location,
        "department": j.department,
        "raw_description": j.raw_description,
        "parsed_summary": j.parsed_summary,
        "compensation": j.compensation,
        "responsibilities": responsibilities,
        "parsed_at": j.parsed_at.isoformat() if j.parsed_at else None,
        "added_to_pipeline": j.added_to_pipeline,
        "pipeline_job_id": j.pipeline_job_id,
        "discovered_at": j.discovered_at.isoformat() if j.discovered_at else None,
    }


@router.get("")
def list_found_jobs(
    q: Optional[str] = Query(None),
    company: Optional[str] = Query(None),
    added: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(DiscoveredJob)
    if q:
        like = f"%{q}%"
        query = query.filter(
            DiscoveredJob.title.ilike(like) | DiscoveredJob.company.ilike(like)
        )
    if company:
        query = query.filter(DiscoveredJob.company.ilike(f"%{company}%"))
    if added is not None:
        query = query.filter(DiscoveredJob.added_to_pipeline == added)

    total = query.count()
    jobs = query.order_by(DiscoveredJob.discovered_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "jobs": [_serialize(j) for j in jobs]}


@router.get("/{job_id}")
def get_found_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(DiscoveredJob).filter(DiscoveredJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _serialize(job)


@router.post("/{job_id}/promote")
def promote_to_pipeline(job_id: int, db: Session = Depends(get_db)):
    """Copy a discovered job into the pipeline Jobs table."""
    discovered = db.query(DiscoveredJob).filter(DiscoveredJob.id == job_id).first()
    if not discovered:
        raise HTTPException(status_code=404, detail="Job not found")
    if discovered.added_to_pipeline and discovered.pipeline_job_id:
        return {"pipeline_job_id": discovered.pipeline_job_id, "already_added": True}

    jd_text = discovered.parsed_summary or discovered.raw_description or ""
    pipeline_job = Job(
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
    db.add(pipeline_job)
    db.commit()
    db.refresh(pipeline_job)

    discovered.added_to_pipeline = True
    discovered.pipeline_job_id = pipeline_job.id
    db.commit()

    return {"pipeline_job_id": pipeline_job.id, "already_added": False}


@router.post("/{job_id}/reparse")
async def reparse_job(job_id: int, db: Session = Depends(get_db)):
    """Re-run Claude parsing on a discovered job."""
    job = db.query(DiscoveredJob).filter(DiscoveredJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.raw_description:
        raise HTTPException(status_code=400, detail="No description to parse")

    asyncio.create_task(_reparse(job.id, job.title, job.company, job.raw_description))
    return {"status": "parsing queued"}


async def _reparse(job_id: int, title: str, company: str, raw_description: str):
    from ai.parser import parse_job
    from database import SessionLocal
    result = await parse_job(title, company, raw_description)
    if not result:
        return
    db = SessionLocal()
    try:
        job = db.query(DiscoveredJob).filter(DiscoveredJob.id == job_id).first()
        if not job:
            return
        job.parsed_summary = result.get("parsed_summary")
        job.compensation = result.get("compensation")
        responsibilities = result.get("responsibilities", [])
        job.responsibilities = json.dumps(responsibilities) if responsibilities else None
        job.parsed_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


@router.delete("/{job_id}")
def delete_found_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(DiscoveredJob).filter(DiscoveredJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.added_to_pipeline:
        raise HTTPException(status_code=409, detail="Job is in pipeline — remove from pipeline first")
    db.delete(job)
    db.commit()
    return {"deleted": True}
