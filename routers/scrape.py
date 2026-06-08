import asyncio
import json
from pathlib import Path
from typing import Optional

import aiohttp
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


@router.post("/company/{name}")
async def scrape_one_company(name: str):
    """Re-scrape a single company by name and persist any new jobs found."""
    result = await scraper_service.run_company_scan(name)
    if "error" in result and "not found" in result["error"]:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


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


@router.get("/zero-results")
def zero_result_companies(db: Session = Depends(get_db)):
    """
    RCA helper: which companies returned 0 keyword-matching jobs on the most
    recent scan, plus how many of the last N runs they've struck out on —
    useful for spotting a one-off blip vs. a scraper that needs attention.
    """
    runs = (
        db.query(ScrapeRun)
        .filter(ScrapeRun.zero_result_companies.isnot(None))
        .order_by(ScrapeRun.started_at.desc())
        .limit(10)
        .all()
    )
    if not runs:
        return {"last_run_at": None, "companies": []}

    # How many of the last `len(runs)` runs did each company strike out on?
    streaks: dict[str, int] = {}
    for r in runs:
        try:
            names = json.loads(r.zero_result_companies) or []
        except Exception:
            names = []
        for name in names:
            streaks[name] = streaks.get(name, 0) + 1

    latest = runs[0]
    try:
        latest_names = json.loads(latest.zero_result_companies) or []
    except Exception:
        latest_names = []

    # Pull career-page URLs from companies.json so the user can jump straight
    # to the source when investigating.
    url_by_name = {}
    try:
        with open(COMPANIES_PATH, encoding="utf-8") as f:
            for c in json.load(f):
                url_by_name[c["name"]] = c.get("url", "")
    except Exception:
        pass

    companies = [
        {
            "name": name,
            "url": url_by_name.get(name, ""),
            "streak": streaks.get(name, 1),
            "checked_runs": len(runs),
        }
        for name in latest_names
    ]
    # Worst offenders first
    companies.sort(key=lambda c: (-c["streak"], c["name"]))

    return {
        "last_run_at": latest.started_at,
        "checked_runs": len(runs),
        "companies": companies,
    }


@router.get("/companies")
def list_companies():
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Company mutation models ───────────────────────────────────────────────────

class CompanyToggle(BaseModel):
    enabled: bool


class CompanyEdit(BaseModel):
    ats:     Optional[str]  = None
    id:      Optional[str]  = None
    url:     Optional[str]  = None
    enabled: Optional[bool] = None


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


@router.put("/companies/{name}")
def update_company(name: str, update: CompanyEdit):
    """Update ats, id, url, and/or enabled for an existing company."""
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    found = False
    updated = {}
    for c in companies:
        if c["name"].lower() == name.lower():
            if update.ats     is not None: c["ats"]     = update.ats;     updated["ats"]     = update.ats
            if update.id      is not None: c["id"]      = update.id;      updated["id"]      = update.id
            if update.url     is not None: c["url"]     = update.url;     updated["url"]     = update.url
            if update.enabled is not None: c["enabled"] = update.enabled; updated["enabled"] = update.enabled
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Company '{name}' not found.")

    with open(COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2)

    return {"ok": True, "name": name, **updated}


# ── Validation helpers ────────────────────────────────────────────────────────
# Two-pass validation:
#   Pass 1 — fast aiohttp GET for all companies (concurrent, lightweight)
#   Pass 2 — Playwright browser check for any that returned a bot-block code
#             (403, 406, 429, 503) so the result reflects what the real scraper sees

from services.scraper import browser_check_urls, _BOT_BLOCKED_CODES

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _http_check(session: aiohttp.ClientSession, co: dict) -> dict:
    """Fast HTTP check — pass 1."""
    name = co.get("name", "")
    url  = co.get("url", "").strip()
    base = {"name": name, "url": url, "http_status": 0}

    if not url:
        return {**base, "status": "no_url"}

    try:
        async with session.get(
            url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True
        ) as r:
            status = "ok" if r.status < 400 else "error"
            return {**base, "status": status, "http_status": r.status}

    except asyncio.TimeoutError:
        return {**base, "status": "timeout"}
    except Exception as exc:
        return {**base, "status": "error", "error": str(exc)[:120]}


@router.get("/validate")
async def validate_companies():
    """
    Two-pass validation:
    1. HTTP GET all companies (fast).
    2. For bot-blocked responses (403 etc.), retry with a real Playwright browser
       and report whether the scraper would actually succeed.
    """
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    # Pass 1 — HTTP check all companies
    sem = asyncio.Semaphore(15)

    async def bounded(session, co):
        async with sem:
            return await _http_check(session, co)

    connector = aiohttp.TCPConnector(ssl=False, limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = list(await asyncio.gather(*[bounded(session, c) for c in companies]))

    # Pass 2 — browser check anything that looks bot-blocked
    blocked = [
        r for r in results
        if r["status"] == "error" and r.get("http_status") in _BOT_BLOCKED_CODES
    ]
    if blocked:
        urls_to_check = [r["url"] for r in blocked]
        browser_results = await browser_check_urls(urls_to_check)
        for r in results:
            br = browser_results.get(r["url"])
            if not br:
                continue
            r["browser_checked"] = True
            r["browser_http_status"] = br["status"]
            if br["ok"]:
                r["status"] = "ok_browser"  # HTTP blocked but browser got through
            elif br["error"]:
                r["browser_error"] = br["error"]

    return results


class ValidateOneRequest(BaseModel):
    name: str = "test"
    url:  str = ""


@router.post("/validate/one")
async def validate_one(req: ValidateOneRequest):
    """
    Test a single company URL. Does the two-pass check inline so the
    edit modal always gets the most accurate result.
    """
    co = {"name": req.name, "url": req.url}
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        result = await _http_check(session, co)

    # If bot-blocked, immediately retry with browser
    if result["status"] == "error" and result.get("http_status") in _BOT_BLOCKED_CODES:
        url = result["url"]
        browser_results = await browser_check_urls([url])
        br = browser_results.get(url)
        if br:
            result["browser_checked"] = True
            result["browser_http_status"] = br["status"]
            if br["ok"]:
                result["status"] = "ok_browser"
            elif br["error"]:
                result["browser_error"] = br["error"]

    return result
