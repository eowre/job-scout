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
