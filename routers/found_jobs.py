import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
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
        "years_of_experience": j.years_of_experience,
        "parsed_at": j.parsed_at.isoformat() if j.parsed_at else None,
        "posted_date": j.posted_date.isoformat() if j.posted_date else None,
        "scouted_at": j.discovered_at.isoformat() if j.discovered_at else None,
        "added_to_pipeline": j.added_to_pipeline,
        "pipeline_job_id": j.pipeline_job_id,
    }


@router.get("/analytics")
def get_analytics(db: Session = Depends(get_db)):
    """Summary stats and breakdowns for all discovered jobs."""
    jobs = db.query(DiscoveredJob).all()

    total = len(jobs)
    parsed = sum(1 for j in jobs if j.parsed_at)

    # YOE
    yoe_values = [j.years_of_experience for j in jobs if j.years_of_experience is not None]
    avg_yoe = round(sum(yoe_values) / len(yoe_values), 1) if yoe_values else None
    yoe_dist: dict[str, int] = {}
    for y in yoe_values:
        if y == 0:
            bucket = "Entry (0)"
        elif y <= 2:
            bucket = "Junior (1-2)"
        elif y <= 4:
            bucket = "Mid (3-4)"
        elif y <= 7:
            bucket = "Senior (5-7)"
        else:
            bucket = "Staff/Principal (8+)"
        yoe_dist[bucket] = yoe_dist.get(bucket, 0) + 1

    # Companies — top 15 for the bar chart, full dict for validation panel
    company_counts = Counter(j.company for j in jobs)
    companies = [{"name": c, "count": n} for c, n in company_counts.most_common(15)]
    company_job_counts = dict(company_counts)  # all companies, keyed by name

    # Title keywords — split titles into words, keep meaningful ones
    _STOP = {"and", "or", "the", "a", "an", "of", "for", "in", "at", "to", "with",
             "senior", "staff", "principal", "lead", "jr", "junior", "associate",
             "remote", "hybrid", "contract", "full", "time", "part"}
    word_counts: Counter = Counter()
    for j in jobs:
        for word in j.title.lower().split():
            word = word.strip("(),/–-")
            if len(word) > 2 and word not in _STOP:
                word_counts[word] += 1
    top_keywords = [{"keyword": w, "count": n} for w, n in word_counts.most_common(15)]

    # Compensation
    jobs_with_comp = sum(1 for j in jobs if j.compensation and j.compensation.lower() not in ("null", "none", ""))

    # Pipeline rate
    in_pipeline = sum(1 for j in jobs if j.added_to_pipeline)

    return {
        "total": total,
        "parsed": parsed,
        "avg_yoe": avg_yoe,
        "yoe_coverage": len(yoe_values),
        "yoe_distribution": yoe_dist,
        "jobs_with_comp": jobs_with_comp,
        "in_pipeline": in_pipeline,
        "top_companies": companies,
        "top_keywords": top_keywords,
        "company_job_counts": company_job_counts,
    }


@router.get("")
def list_found_jobs(
    q: Optional[str] = Query(None),
    company: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    yoe_max: Optional[float] = Query(None),
    has_comp: Optional[bool] = Query(None),
    hide_old: bool = Query(True),   # hide jobs scouted more than 7 days ago
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
    if location:
        query = query.filter(DiscoveredJob.location.ilike(f"%{location}%"))
    if yoe_max is not None:
        # Include jobs with null YOE (unspecified = could be any level)
        query = query.filter(
            (DiscoveredJob.years_of_experience == None) |
            (DiscoveredJob.years_of_experience <= yoe_max)
        )
    if has_comp:
        query = query.filter(
            DiscoveredJob.compensation.isnot(None),
            DiscoveredJob.compensation != "",
            ~DiscoveredJob.compensation.ilike("%null%"),
            ~DiscoveredJob.compensation.ilike("%none%"),
        )
    if hide_old:
        cutoff = datetime.utcnow() - timedelta(days=7)
        query = query.filter(DiscoveredJob.discovered_at >= cutoff)
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
        yoe = result.get("years_of_experience")
        job.years_of_experience = float(yoe) if yoe is not None else None
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
