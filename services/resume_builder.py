"""Builds tailored resume files (DOCX + PDF) for a pipeline job and records
each generation in the generated_resumes history table."""
import json
import os
import subprocess
import tempfile
from datetime import datetime

from ai.pdf_gen import generate_pdf
from database import GeneratedResume, Job

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GENERATED_DIR = os.path.join(_ROOT, "generated")
_DOCX_SCRIPT = os.path.join(_ROOT, "generate_resume.js")


def _base_filename(job: Job) -> str:
    safe = "".join(c for c in f"{job.company}_{job.title}" if c.isalnum() or c in " _-")[:50].strip()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"job{job.id}_{safe.replace(' ', '_')}_{stamp}_tailored"


def build_resume_files(job: Job, structured: dict) -> tuple[str, str | None]:
    """Render the structured resume to DOCX (and best-effort PDF).

    Returns (docx_path, pdf_path). Raises RuntimeError if the DOCX fails.
    """
    os.makedirs(GENERATED_DIR, exist_ok=True)
    base = _base_filename(job)
    docx_path = os.path.join(GENERATED_DIR, base + ".docx")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as jf:
        json.dump(structured, jf)
        json_path = jf.name

    try:
        result = subprocess.run(
            ["node", _DOCX_SCRIPT, json_path, docx_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Resume generation failed: {result.stderr}")
    finally:
        os.unlink(json_path)

    if not os.path.exists(docx_path):
        raise RuntimeError("Resume file was not created.")

    pdf_path: str | None = os.path.join(GENERATED_DIR, base + ".pdf")
    try:
        generate_pdf(structured, pdf_path)
    except Exception as e:
        print(f"PDF generation warning: {e}")
        pdf_path = None

    return docx_path, pdf_path


def flatten_resume(structured: dict) -> str:
    """Plain-text rendering of a structured resume (used for re-scoring)."""
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


def record_generation(db, job: Job, docx_path: str, pdf_path: str | None) -> GeneratedResume:
    """Persist file paths on the job (latest) and append to generation history."""
    job.generated_resume_path = docx_path
    job.generated_pdf_path = pdf_path
    entry = GeneratedResume(
        job_id=job.id,
        resume_id=job.resume_id,
        docx_path=docx_path,
        pdf_path=pdf_path,
        fit_score=job.fit_score,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
