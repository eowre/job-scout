"""
scraper_service.py — Orchestrates scheduled and on-demand job scans.

Triggered by:
  • asyncio interval loop  (every CHECK_INTERVAL_HOURS hours)
  • asyncio cron loop      (08:00 and 20:00 UTC daily minimum)
  • POST /scrape/trigger   (manual on-demand from UI)

Parse queue:
  New jobs discovered during a scan are enqueued into _parse_queue rather
  than spawned as unbounded asyncio tasks.  _PARSE_WORKERS coroutines drain
  the queue concurrently — providing a hard ceiling on in-flight Claude API
  calls regardless of how many new jobs arrive in one scan.
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

# ---------------------------------------------------------------------------
# Bounded parse queue
#
# Instead of fire-and-forget create_task() for every new job, we enqueue
# work items and let _PARSE_WORKERS workers drain them.  This means:
#   - At most _PARSE_WORKERS jobs are ever in-flight with Claude at once
#   - Any burst (100+ new jobs) is absorbed by the queue, not the event loop
#   - A second scan arriving while the first batch is still parsing just adds
#     to the queue — it doesn't multiply active tasks
#
# The queue is unbounded (no maxsize) so scans never block waiting to enqueue.
# The parser's own semaphore (_CONCURRENCY = 3) and rate limiter (40 RPM)
# remain as a second layer of protection inside each worker.
# ---------------------------------------------------------------------------
_PARSE_WORKERS = 5
_parse_queue: asyncio.Queue  # initialised in start_parse_workers()


async def _parse_and_save(discovered_id: int, title: str, company: str, raw_description: str):
    """Parse a single job with Claude and persist the results."""
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


async def _parse_worker(worker_id: int):
    """
    Long-running coroutine that pulls jobs from _parse_queue and parses them.
    Runs for the lifetime of the application.
    """
    logger.debug(f"Parse worker {worker_id} started")
    while True:
        item = await _parse_queue.get()
        try:
            await _parse_and_save(*item)
        except Exception as exc:
            logger.warning(f"Parse worker {worker_id} error: {exc}")
        finally:
            _parse_queue.task_done()


def enqueue_parse(discovered_id: int, title: str, company: str, raw_description: str):
    """Add a job to the parse queue. Safe to call from any coroutine."""
    _parse_queue.put_nowait((discovered_id, title, company, raw_description))
    logger.debug(f"Parse queued: '{title}' @ {company} (queue depth: {_parse_queue.qsize()})")


async def start_parse_workers():
    """
    Initialise the parse queue and spawn worker coroutines.
    Called once from main.py lifespan — must run inside a running event loop.
    """
    global _parse_queue
    _parse_queue = asyncio.Queue()
    for i in range(_PARSE_WORKERS):
        asyncio.create_task(_parse_worker(i))
    logger.info(f"✓ Parse queue started ({_PARSE_WORKERS} workers)")


def load_companies() -> list:
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)
    return [c for c in companies if c.get("enabled", True)]


def get_status() -> dict:
    depth = _parse_queue.qsize() if "_parse_queue" in globals() and _parse_queue else 0
    return {
        "running": _running,
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_result": _last_result,
        "parse_queue_depth": depth,
    }


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
                enqueue_parse(db_job.id, job.title, job.company, job.description)
                logger.info(f"  + {job.title} @ {job.company}")
        finally:
            db.close()

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
                enqueue_parse(db_job.id, job.title, job.company, job.description)
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
        logger.info(f"Scan complete — {new_count} new job(s) queued for parsing "
                    f"(queue depth: {_parse_queue.qsize()})")
        logger.info("=" * 50)
        return _last_result

    except Exception as exc:
        logger.error(f"Scan failed: {exc}", exc_info=True)
        return {"error": str(exc)}
    finally:
        _running = False
