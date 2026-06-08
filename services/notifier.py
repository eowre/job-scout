"""
notifier.py — Discord and Slack webhook alerts for newly discovered jobs.
"""
import logging

import aiohttp

from config import DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL, ALERT_KEYWORDS

logger = logging.getLogger(__name__)


def _is_alert_worthy(title: str) -> bool:
    """
    Decide whether a job's title is interesting enough to trigger a Discord/
    Slack ping. All matching jobs are still scraped and saved regardless —
    this only gates the noisy per-job notification.
    """
    if not title:
        return False
    title_lower = title.lower()
    return any(kw in title_lower for kw in ALERT_KEYWORDS)


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
    if not _is_alert_worthy(job.title):
        logger.debug(f"  → Alert skipped (not FDE-like): {job.title} @ {job.company}")
        return

    sent = False

    if DISCORD_WEBHOOK_URL:
        await _discord(job)
        sent = True

    if SLACK_WEBHOOK_URL:
        await _slack(job)
        sent = True

    if not sent:
        print(f"\n[NEW JOB] {job.title} @ {job.company} — {job.url}")


async def send_scan_summary(companies_scraped: int, jobs_found: int, jobs_new: int,
                             fde_new: int = 0) -> None:
    """Send a single summary message at the end of each scan.

    `fde_new` is the count of newly-discovered jobs that match the FDE alert
    keywords — surfaced prominently since that's what the user actually cares
    about being pinged for. All jobs are still scraped/recorded regardless.
    """
    if not DISCORD_WEBHOOK_URL:
        return

    if fde_new > 0:
        color = 0x34D399   # green — an FDE-like role showed up
        title = f"🎯 {fde_new} Forward Deployed-style job{'s' if fde_new != 1 else ''} found!"
    elif jobs_new > 0:
        color = 0x818CF8   # indigo — other new jobs, but nothing FDE-like
        title = f"🔭 Scan complete — {jobs_new} new job{'s' if jobs_new != 1 else ''} (none FDE-like)"
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
            {"name": "FDE-like (new)",    "value": str(fde_new),           "inline": True},
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
