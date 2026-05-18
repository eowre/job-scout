import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, Job

router = APIRouter(prefix="/jobs", tags=["jobs"])

VALID_STAGES = ["Saved", "Applied", "Phone Screen", "Interview", "Offer", "Rejected"]


class JobCreate(BaseModel):
    title: str
    company: Optional[str] = ""
    jd_text: str
    contact_info: Optional[str] = ""
    notes: Optional[str] = ""
    resume_id: Optional[int] = None


class JobUpdate(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    stage: Optional[str] = None
    contact_info: Optional[str] = None
    notes: Optional[str] = None
    jd_text: Optional[str] = None


@router.post("/")
def create_job(job: JobCreate, db: Session = Depends(get_db)):
    db_job = Job(
        title=job.title,
        company=job.company,
        jd_text=job.jd_text,
        contact_info=job.contact_info,
        notes=job.notes,
        resume_id=job.resume_id,
        source="manual",
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)
    return _serialize(db_job)


@router.get("/")
def list_jobs(db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.updated_at.desc()).all()
    return [_serialize(j) for j in jobs]


@router.get("/generated/list")
def list_generated(db: Session = Depends(get_db)):
    jobs = (
        db.query(Job)
        .filter(Job.generated_pdf_path.isnot(None))
        .order_by(Job.updated_at.desc())
        .all()
    )
    return [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "stage": j.stage,
            "fit_score": j.fit_score,
            "updated_at": j.updated_at,
            "pdf_url": f"/analysis/download-resume-pdf/{j.id}",
            "docx_url": f"/analysis/download-resume/{j.id}" if j.generated_resume_path else None,
        }
        for j in jobs
    ]


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return _serialize(job)


@router.patch("/{job_id}")
def update_job(job_id: int, update: JobUpdate, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if update.stage and update.stage not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {VALID_STAGES}")

    for field, value in update.dict(exclude_none=True).items():
        setattr(job, field, value)

    db.commit()
    db.refresh(job)
    return _serialize(job)


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    db.delete(job)
    db.commit()
    return {"ok": True}


def _serialize(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "stage": job.stage,
        "source": getattr(job, "source", "manual"),
        "job_url": getattr(job, "job_url", None),
        "fit_score": job.fit_score,
        "fit_breakdown": json.loads(job.fit_breakdown) if job.fit_breakdown else None,
        "tailored_bullets": json.loads(job.tailored_bullets) if job.tailored_bullets else None,
        "original_bullets": json.loads(job.original_bullets) if job.original_bullets else None,
        "contact_info": job.contact_info,
        "notes": job.notes,
        "resume_id": job.resume_id,
        "jd_text": job.jd_text,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "resume_url": f"/analysis/download-resume/{job.id}" if getattr(job, "generated_resume_path", None) else None,
    }
