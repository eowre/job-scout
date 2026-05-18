import json
import os
import subprocess
import tempfile
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, Job, Resume
from ai.scorer import score_fit
from ai.tailor import tailor_resume, retailor_bullet
from ai.followup import draft_followup
from ai.resume_gen import structure_resume
from ai.pdf_gen import generate_pdf

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

    job.tailored_resume_text = _flatten_resume(structured)
    db.commit()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as jf:
        json.dump(structured, jf)
        json_path = jf.name

    generated_dir = os.path.join(os.path.dirname(__file__), "..", "generated")
    os.makedirs(generated_dir, exist_ok=True)
    safe_title = "".join(c for c in f"{job.company}_{job.title}" if c.isalnum() or c in " _-")[:50].strip()
    filename = f"job{job.id}_{safe_title.replace(' ', '_')}_tailored.docx"
    docx_path = os.path.abspath(os.path.join(generated_dir, filename))

    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generate_resume.js"))

    try:
        result = subprocess.run(
            ["node", script_path, json_path, docx_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Resume generation failed: {result.stderr}")
    finally:
        os.unlink(json_path)

    if not os.path.exists(docx_path):
        raise HTTPException(status_code=500, detail="Resume file was not created.")

    pdf_filename = filename.replace(".docx", ".pdf")
    pdf_path = os.path.join(generated_dir, pdf_filename)
    try:
        generate_pdf(structured, pdf_path)
        job.generated_pdf_path = pdf_path
    except Exception as e:
        print(f"PDF generation warning: {e}")

    job.generated_resume_path = docx_path
    db.commit()

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


def _flatten_resume(structured: dict) -> str:
    lines = []
    if structured.get("name"):
        lines.append(structured["name"])
    if structured.get("contact"):
        lines.append(structured["contact"])
    if structured.get("summary"):
        lines.append("\nSUMMARY\n" + structured["summary"])
    for job in structured.get("experience", []):
        lines.append(f"\n{job.get('title', '')} at {job.get('company', '')} ({job.get('dates', '')})")
        for b in job.get("bullets", []):
            lines.append(f"- {b}")
    for edu in structured.get("education", []):
        lines.append(f"\n{edu.get('degree', '')} - {edu.get('school', '')} ({edu.get('dates', '')})")
        if edu.get("notes"):
            lines.append(edu["notes"])
    if structured.get("skills"):
        lines.append("\nSKILLS")
        for s in structured["skills"]:
            lines.append(f"- {s}")
    if structured.get("extras"):
        lines.append("\nADDITIONAL")
        for e in structured["extras"]:
            lines.append(f"- {e}")
    return "\n".join(lines)
