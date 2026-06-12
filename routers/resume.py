import os
import pdfplumber
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db, Resume, User
from routers.auth import get_current_user

router = APIRouter(prefix="/resume", tags=["resume"], dependencies=[Depends(get_current_user)])

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("/upload")
async def upload_resume(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    filepath = os.path.join(UPLOAD_DIR, file.filename)
    contents = await file.read()

    with open(filepath, "wb") as f:
        f.write(contents)

    text = ""
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from PDF.")

    resume = Resume(user_id=user.id, filename=file.filename, raw_text=text)
    db.add(resume)
    db.commit()
    db.refresh(resume)

    return {"id": resume.id, "filename": resume.filename, "char_count": len(text)}


@router.get("/list")
def list_resumes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    resumes = db.query(Resume).filter(Resume.user_id == user.id).order_by(Resume.uploaded_at.desc()).all()
    return [{"id": r.id, "filename": r.filename, "uploaded_at": r.uploaded_at} for r in resumes]


@router.get("/{resume_id}")
def get_resume(resume_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found.")
    return {"id": resume.id, "filename": resume.filename, "raw_text": resume.raw_text}
