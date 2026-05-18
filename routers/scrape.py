import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, ScrapeRun
from services import scraper_service

router = APIRouter(prefix="/scrape", tags=["scrape"])

COMPANIES_PATH = Path(__file__).parent.parent / "companies.json"


@router.post("/trigger")
async def trigger_scan():
    if scraper_service._running:
        raise HTTPException(status_code=409, detail="A scan is already in progress.")
    asyncio.create_task(scraper_service.run_scan())
    return {"started": True, "message": "Scan triggered."}


@router.get("/status")
def get_status():
    return scraper_service.get_status()


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    runs = db.query(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(20).all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at,
            "completed_at": r.completed_at,
            "companies_scraped": r.companies_scraped,
            "jobs_found": r.jobs_found,
            "jobs_new": r.jobs_new,
        }
        for r in runs
    ]


@router.get("/companies")
def list_companies():
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        return json.load(f)


class CompanyToggle(BaseModel):
    enabled: bool


@router.patch("/companies/{name}")
def toggle_company(name: str, update: CompanyToggle):
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    found = False
    for c in companies:
        if c["name"].lower() == name.lower():
            c["enabled"] = update.enabled
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Company '{name}' not found.")

    with open(COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2)

    return {"ok": True, "name": name, "enabled": update.enabled}
