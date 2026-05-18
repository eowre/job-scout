"""
scraper_service.py — Orchestrates scheduled and on-demand job scans.

Triggered by:
  • APScheduler IntervalTrigger  (every CHECK_INTERVAL_HOURS hours)
  • APScheduler CronTrigger      (08:00 and 20:00 UTC daily minimum)
  • POST /scrape/trigger          (manual on-demand from UI or external cron)
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import SessionLocal, Job, ScrapeRun
from services.scraper import scrape_all
from services.notifier import send_alert

logger = logging.getLogger(__name__)

COMPANIES_PATH = Path(__file__).parent.parent / "companies.json"

# In-process state — safe because asyncio is single-threaded
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
        try:
            for job in all_jobs:
                existing = db.query(Job).filter(Job.ats_job_id == job.job_id).first()
                if existing:
                    continue

                meta = {}
                if job.location:
                    meta["location"] = job.location
                if job.department:
                    meta["department"] = job.department

                db_job = Job(
                    title=job.title,
                    company=job.company,
                    jd_text="",
                    stage="Saved",
                    source="scraped",
                    ats_job_id=job.job_id,
                    job_url=job.url,
                    contact_info=json.dumps(meta) if meta else "",
                )
                db.add(db_job)
                db.commit()
                new_count += 1
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

        _last_run = datetime.utcnow()
        _last_result = {
            "companies_scraped": len(companies),
            "jobs_found": len(all_jobs),
            "jobs_new": new_count,
        }
        logger.info(f"Scan complete — {new_count} new job(s)")
        logger.info("=" * 50)
        return _last_result

    except Exception as exc:
        logger.error(f"Scan failed: {exc}", exc_info=True)
        return {"error": str(exc)}
    finally:
        _running = False
