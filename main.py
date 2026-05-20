import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from config import CHECK_INTERVAL_HOURS
from database import init_db
from routers import resume, jobs, analysis, scrape, found_jobs, keywords
from services import scraper_service


async def _interval_loop():
    """Scan every CHECK_INTERVAL_HOURS hours."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)
        await scraper_service.run_scan()


async def _cron_loop():
    """Guarantee scans at 08:00 and 20:00 UTC daily."""
    FIRE_HOURS = (8, 20)
    while True:
        now = datetime.now(timezone.utc)
        # Find the next target hour today or tomorrow
        candidates = [
            now.replace(hour=h, minute=0, second=0, microsecond=0)
            for h in FIRE_HOURS
        ]
        future = [t for t in candidates if t > now]
        if future:
            next_fire = min(future)
        else:
            # Both passed today — next is 08:00 tomorrow
            from datetime import timedelta
            next_fire = now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)

        wait = (next_fire - datetime.now(timezone.utc)).total_seconds()
        await asyncio.sleep(max(0, wait))
        await scraper_service.run_scan()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print("✓ Database initialized")

    asyncio.create_task(_interval_loop())
    asyncio.create_task(_cron_loop())

    print(f"✓ Scraper scheduled: every {CHECK_INTERVAL_HOURS}h + cron at 08:00/20:00 UTC")
    print("✓ Job Scout running at http://localhost:8000")
    yield


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
app.include_router(found_jobs.router)
app.include_router(keywords.router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse("static/app.html")


@app.get("/health")
def health():
    return {"status": "ok"}
