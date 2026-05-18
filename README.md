# Job Scout

An AI-powered job search platform that automatically discovers openings from 100+ company job boards, scores your fit, tailors your resume, and tracks your applications — all in one place.

## Features

- **Auto-Scout** — scrapes Greenhouse, Lever, Ashby, and generic career pages on a schedule (every 2 hours + guaranteed daily cron). Trigger on-demand from the UI or an external cron job via `POST /scrape/trigger`.
- **Fit Scoring** — Claude analyzes your resume against the job description and gives an 0–100 score with a full breakdown (skills, experience, keywords, red flags).
- **Resume Tailoring** — AI rewrites your weakest bullets to match the JD language, without fabricating experience. Accept, deny, or re-tailor each bullet with feedback.
- **Resume Generation** — outputs a polished PDF and DOCX from your tailored resume, auto-scaled to fit one page.
- **Pipeline Kanban** — drag-and-drop board to track applications from Saved → Offer.
- **Follow-up Emails** — stage-aware email drafts (applied, phone screen, interview, offer, rejected).
- **Discord / Slack Notifications** — instant alerts when new matching jobs are found.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + SQLAlchemy (SQLite) |
| Scheduler | APScheduler (interval + cron triggers) |
| Scraper | aiohttp + BeautifulSoup (async, 15 concurrent) |
| AI | Anthropic Claude (claude-sonnet-4-6 / claude-haiku-4-5) |
| PDF | reportlab |
| DOCX | Node.js + docx library |
| Frontend | Alpine.js (no build step) |

## Quickstart

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/job-scout.git
cd job-scout

# 2. Python deps
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 3. Node deps (for DOCX generation)
npm install

# 4. Configure
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY, and optionally DISCORD/SLACK webhook URLs

# 5. Run
uvicorn main:app --reload
# Open http://localhost:8000
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | For scoring, tailoring, resume generation |
| `DISCORD_WEBHOOK_URL` | No | Discord alerts for new jobs |
| `SLACK_WEBHOOK_URL` | No | Slack alerts for new jobs |

### Tuning the scraper (`config.py`)

- **`JOB_KEYWORDS`** — titles that qualify (default: software/solutions/platform/devops/SRE roles)
- **`EXCLUDE_KEYWORDS`** — titles to skip (uncomment `intern`, `director`, etc.)
- **`CHECK_INTERVAL_HOURS`** — how often the background thread re-scans (default: 2)

### Enabling / disabling companies

Toggle companies in the Scout → Companies tab in the UI, or edit `companies.json` directly.

## Scraper triggers

| Trigger | How |
|---|---|
| Background (automatic) | APScheduler interval — every `CHECK_INTERVAL_HOURS` |
| Daily minimum | APScheduler cron — 08:00 and 20:00 UTC |
| On-demand (UI) | Scout page → "Scan Now" button |
| External cron | `curl -X POST http://localhost:8000/scrape/trigger` |

## Workflow

1. **Scout tab** — new jobs appear automatically in your Pipeline as they're discovered.
2. **Pipeline tab** — open a scraped job, paste its full description, then load it into the Analyzer.
3. **Analyze tab** — score your fit, tailor your bullets, generate a PDF/DOCX resume.
4. **Pipeline tab** — move the card through stages and draft follow-up emails as you progress.

## Project structure

```
job-scout/
├── main.py                 # FastAPI app + APScheduler lifespan
├── config.py               # Keywords, scraping settings, notification config
├── database.py             # SQLAlchemy models (Resume, Job, ScrapeRun)
├── companies.json          # 100+ company job board configs
├── generate_resume.js      # Node.js DOCX builder
├── routers/
│   ├── resume.py           # PDF upload & extraction
│   ├── jobs.py             # CRUD + pipeline stage management
│   ├── analysis.py         # Score, tailor, followup, generate, rescore
│   └── scrape.py           # Trigger, status, runs, company toggles
├── services/
│   ├── scraper.py          # Async multi-ATS scraper (Greenhouse/Lever/Ashby/Generic)
│   ├── notifier.py         # Discord & Slack webhook alerts
│   └── scraper_service.py  # Orchestrates scans, dedup, DB writes
├── ai/
│   ├── scorer.py           # Job fit analysis
│   ├── tailor.py           # Resume bullet tailoring & re-tailoring
│   ├── followup.py         # Follow-up email drafting
│   ├── resume_gen.py       # Resume structure reconstruction
│   └── pdf_gen.py          # reportlab PDF generation
└── static/
    └── app.html            # Single-file Alpine.js frontend
```
