"""
notifier.py — Discord and Slack webhook alerts for newly discovered jobs.

Webhook URLs and alert keywords are read from the settings DB at send time
so changes made in the Settings UI take effect immediately without a restart.
"""
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)


def _get_settings() -> dict:
    """Pull the live notification settings from the DB."""
    from database import load_settings
    s = load_settings()
    discord = s.get("discord_webhook_url", "")
    slack   = s.get("slack_webhook_url",   "")
    try:
        alert_kw = json.loads(s.get("alert_keywords", "[]"))
    except Exception:
        alert_kw = ["forward deployed", "forward-deployed", "fde"]
    return {"discord": discord, "slack": slack, "alert_keywords": alert_kw}


def _is_alert_worthy(title: str) -> bool:
    if not title:
        return False
    cfg = _get_settings()
    title_lower = title.lower()
    return any(kw in title_lower for kw in cfg["alert_keywords"])


async def _discord(job, webhook_url: str) -> None:
    fields = [
        {"name": "Company",  "value": job.company,                    "inline": True},
        {"name": "Location", "value": job.location or "Not specified", "inline": True},
    ]
    if job.department:
        fields.append({"name": "Team", "value": job.department, "inline": True})

    embed = {
        "title":  job.title,
        "url":    job.url,
        "color":  0x818CF8,
        "fields": fields,
        "footer": {"text": "Job Scout • new listing"},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={"embeds": [embed]}) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                logger.error(f"Discord webhook failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Discord: {job.title} @ {job.company}")


async def _slack(job, webhook_url: str) -> None:
    blocks = [
        {"type": "header",  "text": {"type": "plain_text", "text": job.title, "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Company:*\n{job.company}"},
            {"type": "mrkdwn", "text": f"*Location:*\n{job.location or 'Not specified'}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"<{job.url}|View Job Posting>"}},
        {"type": "divider"},
    ]
    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={"text": f"New job: {job.title} at {job.company}", "blocks": blocks}) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Slack webhook failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Slack: {job.title} @ {job.company}")


async def send_alert(job) -> None:
    if not _is_alert_worthy(job.title):
        logger.debug(f"  → Alert skipped (not alert-worthy): {job.title} @ {job.company}")
        return

    cfg  = _get_settings()
    sent = False

    if cfg["discord"]:
        await _discord(job, cfg["discord"])
        sent = True
    if cfg["slack"]:
        await _slack(job, cfg["slack"])
        sent = True
    if not sent:
        print(f"\n[NEW JOB] {job.title} @ {job.company} — {job.url}")


async def send_scan_summary(companies_scraped: int, jobs_found: int, jobs_new: int,
                             fde_new: int = 0) -> None:
    cfg = _get_settings()
    if not cfg["discord"]:
        return

    if fde_new > 0:
        color = 0x34D399
        title = f"🎯 {fde_new} alert-worthy job{'s' if fde_new != 1 else ''} found!"
    elif jobs_new > 0:
        color = 0x818CF8
        title = f"🔭 Scan complete — {jobs_new} new job{'s' if jobs_new != 1 else ''}"
    else:
        color = 0x475569
        title = "🔭 Scan complete — no new jobs"

    embed = {
        "title":  title,
        "color":  color,
        "fields": [
            {"name": "Companies scanned", "value": str(companies_scraped), "inline": True},
            {"name": "Total matches",     "value": str(jobs_found),        "inline": True},
            {"name": "New this scan",     "value": str(jobs_new),          "inline": True},
            {"name": "Alert-worthy (new)","value": str(fde_new),           "inline": True},
        ],
        "footer": {"text": "Job Scout • scan summary"},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(cfg["discord"], json={"embeds": [embed]}) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                logger.error(f"Discord scan summary failed {resp.status}: {text[:200]}")
            else:
                logger.info(f"  → Discord scan summary sent ({jobs_new} new)")
