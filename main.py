from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

from config import CHECK_INTERVAL_HOURS
from database import init_db
from routers import resume, jobs, analysis, scrape
from services import scraper_service

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print("✓ Database initialized")

    # Continuous interval scan
    scheduler.add_job(
        scraper_service.run_scan,
        trigger=IntervalTrigger(hours=CHECK_INTERVAL_HOURS),
        id="interval_scan",
        name="Interval scan",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Guaranteed daily minimum at 08:00 and 20:00 UTC
    scheduler.add_job(
        scraper_service.run_scan,
        trigger=CronTrigger(hour="8,20", timezone="UTC"),
        id="cron_scan",
        name="Daily cron scan",
        replace_existing=True,
        misfire_grace_time=600,
    )

    scheduler.start()
    print(f"✓ Scraper scheduled: every {CHECK_INTERVAL_HOURS}h + cron at 08:00/20:00 UTC")
    print("✓ Job Scout running at http://localhost:8000")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Job Scout", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resume.router)
app.include_router(jobs.router)
app.include_router(analysis.router)
app.include_router(scrape.router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse("static/app.html")


@app.get("/health")
def health():
    return {"status": "ok"}
