import json
import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, GeneratedResume, Job, Resume
from ai.scorer import score_fit
from ai.tailor import tailor_resume, retailor_bullet
from ai.followup import draft_followup
from ai.resume_gen import structure_resume
from services.resume_builder import build_resume_files, flatten_resume, record_generation

router = APIRouter(prefix="/analysis", tags=["analysis"])


class ScoreRequest(BaseModel):
    job_id: int
    resume_id: int


class TailorRequest(BaseModel):
    job_id: int
    resume_id: int
    gap_contexts: list[dict] = []


class FollowupRequest(BaseModel):
    job_id: int
    notes: Optional[str] = ""


class RetailorBulletRequest(BaseModel):
    job_id: int
    original: str
    previous_tailored: str
    user_context: str


class AcceptedBullet(BaseModel):
    original: str
    tailored: str
    status: str


class AcceptBulletsRequest(BaseModel):
    job_id: int
    bullets: list[AcceptedBullet]


@router.post("/score")
async def score_job(req: ScoreRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.jd_text:
        raise HTTPException(status_code=400, detail="This job has no description yet. Add one from the pipeline first.")

    resume = db.query(Resume).filter(Resume.id == req.resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")

    result = await score_fit(resume.raw_text, job.jd_text)

    job.fit_score = result["overall_score"]
    job.fit_breakdown = json.dumps(result)
    job.resume_id = req.resume_id
    db.commit()

    return result


@router.post("/tailor")
async def tailor_job(req: TailorRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.jd_text:
        raise HTTPException(status_code=400, detail="This job has no description yet. Add one from the pipeline first.")

    resume = db.query(Resume).filter(Resume.id == req.resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")

    if job.fit_breakdown:
        breakdown = json.loads(job.fit_breakdown)
        gaps = breakdown.get("gaps", [])
        missing_keywords = breakdown.get("missing_keywords", [])
    else:
        breakdown = await score_fit(resume.raw_text, job.jd_text)
        job.fit_score = breakdown["overall_score"]
        job.fit_breakdown = json.dumps(breakdown)
        gaps = breakdown.get("gaps", [])
        missing_keywords = breakdown.get("missing_keywords", [])

    result = await tailor_resume(resume.raw_text, job.jd_text, gaps, missing_keywords, req.gap_contexts)

    job.tailored_bullets = json.dumps(result.get("tailored_bullets", []))
    job.resume_id = req.resume_id
    db.commit()

    return result


@router.post("/followup")
async def followup_email(req: FollowupRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    email_text = await draft_followup(
        title=job.title,
        company=job.company or "",
        stage=job.stage,
        jd_text=job.jd_text or "",
        notes=req.notes or job.notes or ""
    )

    return {"email": email_text}


@router.post("/retailor-bullet")
async def retailor_bullet_endpoint(req: RetailorBulletRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = await retailor_bullet(
        original=req.original,
        previous_tailored=req.previous_tailored,
        jd_text=job.jd_text or "",
        user_context=req.user_context
    )

    return result


@router.post("/accept-bullets")
def accept_bullets(req: AcceptBulletsRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    resolved = [
        {"original": b.original, "tailored": b.tailored, "status": b.status}
        for b in req.bullets
    ]

    job.tailored_bullets = json.dumps(resolved)
    db.commit()

    accepted = sum(1 for b in req.bullets if b.status == "accepted")
    denied = sum(1 for b in req.bullets if b.status == "denied")
    return {"saved": True, "accepted": accepted, "denied": denied}


@router.post("/generate-resume/{job_id}")
async def generate_resume(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.resume_id:
        raise HTTPException(status_code=400, detail="No resume linked to this job.")
    if not job.tailored_bullets:
        raise HTTPException(status_code=400, detail="No bullet decisions saved. Accept or deny bullets first.")

    resume = db.query(Resume).filter(Resume.id == job.resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")

    bullets = json.loads(job.tailored_bullets)
    accepted = [b for b in bullets if b.get("status") == "accepted"]
    denied   = [b for b in bullets if b.get("status") == "denied"]

    if not accepted and not denied:
        raise HTTPException(status_code=400, detail="No bullet decisions found. Save your decisions first.")

    structured = await structure_resume(resume.raw_text, accepted, denied)

    job.tailored_resume_text = flatten_resume(structured)
    db.commit()

    try:
        docx_path, pdf_path = build_resume_files(job, structured)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    record_generation(db, job, docx_path, pdf_path)
    filename = os.path.basename(docx_path)

    return FileResponse(
        path=docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/download-resume/{job_id}")
def download_resume(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.generated_resume_path or not os.path.exists(job.generated_resume_path):
        raise HTTPException(status_code=404, detail="No generated resume found for this job.")
    filename = os.path.basename(job.generated_resume_path)
    return FileResponse(
        path=job.generated_resume_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/download-resume-pdf/{job_id}")
def download_resume_pdf(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.generated_pdf_path or not os.path.exists(job.generated_pdf_path):
        raise HTTPException(status_code=404, detail="No generated PDF found for this job.")
    filename = os.path.basename(job.generated_pdf_path)
    return FileResponse(
        path=job.generated_pdf_path,
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _serialize_generated(g: GeneratedResume) -> dict:
    return {
        "id": g.id,
        "job_id": g.job_id,
        "fit_score": g.fit_score,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "docx_url": f"/analysis/download-generated/{g.id}?fmt=docx" if g.docx_path else None,
        "pdf_url": f"/analysis/download-generated/{g.id}?fmt=pdf" if g.pdf_path else None,
        "filename": os.path.basename(g.docx_path or g.pdf_path or ""),
    }


@router.get("/generated/{job_id}")
def list_generated_for_job(job_id: int, db: Session = Depends(get_db)):
    """All resumes ever generated for one pipeline job, newest first."""
    rows = (
        db.query(GeneratedResume)
        .filter(GeneratedResume.job_id == job_id)
        .order_by(GeneratedResume.created_at.desc())
        .all()
    )
    return [_serialize_generated(g) for g in rows]


@router.get("/download-generated/{generated_id}")
def download_generated(generated_id: int, fmt: str = "docx", db: Session = Depends(get_db)):
    g = db.query(GeneratedResume).filter(GeneratedResume.id == generated_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="Generated resume not found.")
    path = g.pdf_path if fmt == "pdf" else g.docx_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No {fmt} file on disk for this entry.")
    filename = os.path.basename(path)
    return FileResponse(
        path=path,
        media_type="application/pdf" if fmt == "pdf" else _DOCX_MEDIA,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/rescore/{job_id}")
async def rescore_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.tailored_resume_text:
        raise HTTPException(status_code=400, detail="No tailored resume found. Generate your resume first.")
    if not job.fit_breakdown:
        raise HTTPException(status_code=400, detail="No original score found. Score the job first.")

    new_result = await score_fit(job.tailored_resume_text, job.jd_text)
    original = json.loads(job.fit_breakdown)

    return {
        "original": {
            "overall_score": original.get("overall_score"),
            "breakdown": original.get("breakdown"),
            "recommendation": original.get("recommendation"),
        },
        "tailored": {
            "overall_score": new_result["overall_score"],
            "breakdown": new_result["breakdown"],
            "recommendation": new_result["recommendation"],
        },
        "delta": new_result["overall_score"] - original.get("overall_score", 0),
        "strengths": new_result.get("strengths", []),
        "gaps": new_result.get("gaps", []),
    }


