import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import aiohttp
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


_ASHBY_COUNT_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) { jobPostings { id } }
}
"""

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


@router.get("/validate")
async def validate_companies():
    """Hit each company's ATS endpoint and return live status + total job count."""
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    TIMEOUT = aiohttp.ClientTimeout(total=15)
    sem = asyncio.Semaphore(12)

    async def check_one(session: aiohttp.ClientSession, co: dict) -> dict:
        ats    = co.get("ats", "generic")
        co_id  = co.get("id", "")
        name   = co["name"]
        base   = {"name": name, "ats": ats, "total_jobs": None, "http_status": 0}

        async with sem:
            try:
                if ats == "greenhouse":
                    url = f"https://boards.greenhouse.io/embed/job_board/json?for={co_id}"
                    async with session.get(url, timeout=TIMEOUT) as r:
                        if r.status != 200:
                            return {**base, "status": "error", "http_status": r.status}
                        data  = await r.json(content_type=None)
                        count = len(data.get("jobs", []))
                        return {**base, "status": "ok" if count else "empty",
                                "http_status": 200, "total_jobs": count}

                elif ats == "lever":
                    url = f"https://api.lever.co/v0/postings/{co_id}?mode=json"
                    async with session.get(url, timeout=TIMEOUT) as r:
                        if r.status != 200:
                            return {**base, "status": "error", "http_status": r.status}
                        data  = await r.json(content_type=None)
                        count = len(data) if isinstance(data, list) else 0
                        return {**base, "status": "ok" if count else "empty",
                                "http_status": 200, "total_jobs": count}

                elif ats == "ashby":
                    url     = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
                    payload = {
                        "operationName": "ApiJobBoardWithTeams",
                        "variables": {"organizationHostedJobsPageName": co_id},
                        "query": _ASHBY_COUNT_QUERY,
                    }
                    async with session.post(url, json=payload, timeout=TIMEOUT) as r:
                        if r.status != 200:
                            return {**base, "status": "error", "http_status": r.status}
                        data  = await r.json(content_type=None)
                        board = (data.get("data") or {}).get("jobBoard") or {}
                        count = len(board.get("jobPostings") or [])
                        return {**base, "status": "ok" if count else "empty",
                                "http_status": 200, "total_jobs": count}

                else:  # generic
                    url = co.get("url", "")
                    if not url:
                        return {**base, "status": "no_url"}
                    async with session.get(url, headers=_HEADERS, timeout=TIMEOUT,
                                           allow_redirects=True) as r:
                        ok = r.status < 400
                        return {**base, "status": "ok" if ok else "error",
                                "http_status": r.status}

            except asyncio.TimeoutError:
                return {**base, "status": "timeout"}
            except Exception as exc:
                return {**base, "status": "error", "error": str(exc)[:120]}

    connector = aiohttp.TCPConnector(ssl=False, limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[check_one(session, c) for c in companies])

    return list(results)
