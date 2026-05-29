"""
notifier.py — Discord and Slack webhook alerts for newly discovered jobs.
"""
import logging

import aiohttp

from config import DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)


async def _discord(job) -> None:
    fields = [
        {"name": "Company",  "value": job.company,                    "inline": True},
        {"name": "Location", "value": job.location or "Not specified", "inline": True},
    ]
    if job.department:
        fields.append({"name": "Team", "value": job.department, "inline": True})

    embed = {
        "title":  f"{job.title}",
        "url":    job.url,
        "color":  0x818CF8,
        "fields": fields,
        "footer": {"text": "Job Scout • new listing"},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                logger.error(f"Discord webhook failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Discord: {job.title} @ {job.company}")


async def _slack(job) -> None:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{job.title}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Company:*\n{job.company}"},
                {"type": "mrkdwn", "text": f"*Location:*\n{job.location or 'Not specified'}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{job.url}|View Job Posting>"},
        },
        {"type": "divider"},
    ]

    payload = {
        "text": f"New job: {job.title} at {job.company}",
        "blocks": blocks,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(SLACK_WEBHOOK_URL, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Slack webhook failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Slack: {job.title} @ {job.company}")


async def send_alert(job) -> None:
    sent = False

    if DISCORD_WEBHOOK_URL:
        await _discord(job)
        sent = True

    if SLACK_WEBHOOK_URL:
        await _slack(job)
        sent = True

    if not sent:
        print(f"\n[NEW JOB] {job.title} @ {job.company} — {job.url}")


async def send_scan_summary(companies_scraped: int, jobs_found: int, jobs_new: int) -> None:
    """Send a single summary message at the end of each scan."""
    if not DISCORD_WEBHOOK_URL:
        return

    if jobs_new > 0:
        color = 0x34D399   # green — found something
        title = f"🎯 {jobs_new} new job{'s' if jobs_new != 1 else ''} found"
    else:
        color = 0x475569   # grey — nothing new
        title = "🔭 Scan complete — no new jobs"

    embed = {
        "title": title,
        "color": color,
        "fields": [
            {"name": "Companies scanned", "value": str(companies_scraped), "inline": True},
            {"name": "Total matches",     "value": str(jobs_found),        "inline": True},
            {"name": "New this scan",     "value": str(jobs_new),          "inline": True},
        ],
        "footer": {"text": "Job Scout • scan summary"},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                logger.error(f"Discord scan summary failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Discord scan summary sent ({jobs_new} new)")
