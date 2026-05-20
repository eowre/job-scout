"""
scraper_service.py — Orchestrates scheduled and on-demand job scans.

Triggered by:
  • asyncio interval loop  (every CHECK_INTERVAL_HOURS hours)
  • asyncio cron loop      (08:00 and 20:00 UTC daily minimum)
  • POST /scrape/trigger   (manual on-demand from UI)
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import SessionLocal, DiscoveredJob, ScrapeRun
from services.scraper import scrape_all
from services.notifier import send_alert

logger = logging.getLogger(__name__)

COMPANIES_PATH = Path(__file__).parent.parent / "companies.json"

_running = False
_last_run: Optional[datetime] = None
_last_result: dict = {}


def load_companies() -> list:
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)
    return [c for c in companies if c.get("enabled", True)]


def get_status() -> dict:
    return {
        "running": _running,
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_result": _last_result,
    }


async def _parse_and_save(discovered_id: int, title: str, company: str, raw_description: str):
    """Parse a single job with Claude haiku and persist the results."""
    from ai.parser import parse_job
    result = await parse_job(title, company, raw_description)
    if not result:
        return

    db = SessionLocal()
    try:
        job = db.query(DiscoveredJob).filter(DiscoveredJob.id == discovered_id).first()
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
        logger.info(f"  Parsed: {title} @ {company}")
    except Exception as exc:
        logger.warning(f"  Parse save failed for '{title}': {exc}")
    finally:
        db.close()


async def run_company_scan(company_name: str) -> dict:
    """Scrape a single company by name and persist any new jobs found."""
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    company = next(
        (c for c in companies if c["name"].lower() == company_name.lower()), None
    )
    if not company:
        return {"error": f"Company '{company_name}' not found"}

    logger.info(f"Single-company scan: {company['name']}")
    try:
        all_jobs = await scrape_all([company])

        db = SessionLocal()
        new_count = 0
        new_discovered: list[tuple[int, str, str, str]] = []
        try:
            for job in all_jobs:
                existing = db.query(DiscoveredJob).filter(
                    DiscoveredJob.ats_job_id == job.job_id
                ).first()
                if existing:
                    continue
                db_job = DiscoveredJob(
                    ats_job_id=job.job_id,
                    company=job.company,
                    title=job.title,
                    url=job.url,
                    location=job.location,
                    department=job.department,
                    raw_description=job.description,
                    posted_date=job.posted_date,
                )
                db.add(db_job)
                db.commit()
                db.refresh(db_job)
                new_count += 1
                new_discovered.append((db_job.id, job.title, job.company, job.description))
                logger.info(f"  + {job.title} @ {job.company}")
        finally:
            db.close()

        for (did, title, co, desc) in new_discovered:
            asyncio.create_task(_parse_and_save(did, title, co, desc))

        logger.info(f"Single-company scan done — {len(all_jobs)} found, {new_count} new")
        return {"jobs_found": len(all_jobs), "jobs_new": new_count}

    except Exception as exc:
        logger.error(f"Single-company scan failed for '{company_name}': {exc}", exc_info=True)
        return {"error": str(exc)}


async def run_scan() -> dict:
    global _running, _last_run, _last_result

    if _running:
        logger.info("Scan skipped — already in progress")
        return {"skipped": True, "reason": "scan already in progress"}

    _running = True
    started_at = datetime.utcnow()
    logger.info("=" * 50)
    logger.info("Job Scout scan started")

    try:
        companies = load_companies()
        all_jobs = await scrape_all(companies)
        logger.info(f"Scraped {len(all_jobs)} keyword-matching jobs from {len(companies)} companies")

        db = SessionLocal()
        new_count = 0
        new_discovered: list[tuple[int, str, str, str]] = []
        try:
            for job in all_jobs:
                existing = db.query(DiscoveredJob).filter(
                    DiscoveredJob.ats_job_id == job.job_id
                ).first()
                if existing:
                    continue

                db_job = DiscoveredJob(
                    ats_job_id=job.job_id,
                    company=job.company,
                    title=job.title,
                    url=job.url,
                    location=job.location,
                    department=job.department,
                    raw_description=job.description,
                    posted_date=job.posted_date,
                )
                db.add(db_job)
                db.commit()
                db.refresh(db_job)
                new_count += 1
                new_discovered.append((db_job.id, job.title, job.company, job.description))
                logger.info(f"  + {job.title} @ {job.company}")

                try:
                    await send_alert(job)
                except Exception as exc:
                    logger.warning(f"Notification failed for {job.title}: {exc}")

            run = ScrapeRun(
                started_at=started_at,
                completed_at=datetime.utcnow(),
                companies_scraped=len(companies),
                jobs_found=len(all_jobs),
                jobs_new=new_count,
            )
            db.add(run)
            db.commit()
        finally:
            db.close()

        # Fire off parsing as background tasks — don't block the scan result
        for (did, title, company, desc) in new_discovered:
            asyncio.create_task(_parse_and_save(did, title, company, desc))

        _last_run = datetime.utcnow()
        _last_result = {
            "companies_scraped": len(companies),
            "jobs_found": len(all_jobs),
            "jobs_new": new_count,
        }
        logger.info(f"Scan complete — {new_count} new job(s), {new_count} parsing task(s) queued")
        logger.info("=" * 50)
        return _last_result

    except Exception as exc:
        logger.error(f"Scan failed: {exc}", exc_info=True)
        return {"error": str(exc)}
    finally:
        _running = False
