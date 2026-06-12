"""
routers/settings.py — Runtime app configuration.

GET  /settings                → full settings dict
PUT  /settings                → update settings (persists to DB, live immediately)
GET  /settings/ollama-models  → installed Ollama models + reachability
POST /settings/test-parse     → run a sample JD through the current parser
POST /settings/test-discord   → send a test Discord message
POST /settings/test-slack     → send a test Slack message
"""
import json
import logging
import time

import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.parser import get_parser_config, update_parser_config
from database import load_settings, save_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

_ALLOWED_KEYS = {
    "parser_backend", "ollama_model", "ollama_base_url", "anthropic_model",
    "discord_webhook_url", "slack_webhook_url",
    "alert_keywords",       # stored as JSON string
    "check_interval_hours",
    "auto_tailor_min_score",
}


class SettingsUpdate(BaseModel):
    parser_backend:       str | None = None
    ollama_model:         str | None = None
    ollama_base_url:      str | None = None
    anthropic_model:      str | None = None
    discord_webhook_url:  str | None = None
    slack_webhook_url:    str | None = None
    alert_keywords:       list[str] | None = None   # list in, stored as JSON
    check_interval_hours: int | None = None
    auto_tailor_min_score: int | None = None


@router.get("")
def get_settings():
    """Return all live settings, with alert_keywords parsed back to a list."""
    s = load_settings()
    try:
        s["alert_keywords"] = json.loads(s.get("alert_keywords", "[]"))
    except Exception:
        s["alert_keywords"] = []
    try:
        s["check_interval_hours"] = int(s.get("check_interval_hours", 2))
    except Exception:
        s["check_interval_hours"] = 2
    try:
        s["auto_tailor_min_score"] = int(s.get("auto_tailor_min_score", 70))
    except Exception:
        s["auto_tailor_min_score"] = 70
    return s


@router.put("")
def put_settings(body: SettingsUpdate):
    """Update settings — persists to DB and hot-swaps parser config."""
    raw = body.model_dump()
    updates: dict = {}

    for key, value in raw.items():
        if value is None or key not in _ALLOWED_KEYS:
            continue
        # Validate parser_backend
        if key == "parser_backend" and value not in ("ollama", "anthropic"):
            raise HTTPException(400, detail="parser_backend must be 'ollama' or 'anthropic'.")
        # Serialise alert_keywords list → JSON string
        if key == "alert_keywords":
            value = json.dumps([k.strip() for k in value if k.strip()])
        # Serialise interval
        if key == "check_interval_hours":
            value = str(int(value))
        if key == "auto_tailor_min_score":
            value = str(max(0, min(100, int(value))))
        updates[key] = value

    if not updates:
        raise HTTPException(400, detail="No valid settings provided.")

    for key, value in updates.items():
        save_setting(key, value)

    # Hot-swap parser config keys
    parser_keys = {"parser_backend", "ollama_model", "ollama_base_url", "anthropic_model"}
    parser_updates = {k: v for k, v in updates.items() if k in parser_keys}
    if parser_updates:
        update_parser_config(parser_updates)

    return get_settings()


@router.get("/ollama-models")
async def get_ollama_models():
    """Proxy Ollama /api/tags — returns installed models + reachability."""
    cfg = get_parser_config()
    base_url = cfg.get("ollama_base_url", "http://localhost:11434").rstrip("/")
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base_url}/api/tags") as resp:
                if resp.status != 200:
                    return {"models": [], "reachable": False}
                data = await resp.json(content_type=None)
        models = [
            {"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)}
            for m in (data.get("models") or [])
        ]
        return {"models": models, "reachable": True}
    except Exception as exc:
        logger.debug(f"Ollama unreachable: {exc}")
        return {"models": [], "reachable": False}


class TestParseRequest(BaseModel):
    title:       str = "Senior Software Engineer"
    company:     str = "Acme Corp"
    description: str = (
        "We are looking for a Senior Software Engineer with 5+ years of experience "
        "in Python and distributed systems. You will design and build scalable APIs, "
        "lead code reviews, and mentor junior engineers. The role offers $150k-$190k "
        "base salary plus equity. You must have strong experience with cloud infrastructure "
        "(AWS or GCP), containerisation (Docker/Kubernetes), and CI/CD pipelines. "
        "Experience with data pipelines or ML systems is a plus."
    )


@router.post("/test-parse")
async def test_parse(body: TestParseRequest):
    """Run a job description through the current parser backend and return the result."""
    from ai.parser import parse_job
    t0 = time.monotonic()
    result = await parse_job(body.title, body.company, body.description)
    elapsed = round(time.monotonic() - t0, 1)
    cfg = get_parser_config()
    return {
        "result":  result,
        "elapsed_s": elapsed,
        "backend": cfg["parser_backend"],
        "model":   cfg["ollama_model"] if cfg["parser_backend"] == "ollama" else cfg["anthropic_model"],
        "success": bool(result),
    }


class WebhookTestRequest(BaseModel):
    url: str


@router.post("/test-discord")
async def test_discord(body: WebhookTestRequest):
    """Send a test embed to a Discord webhook URL."""
    if not body.url:
        raise HTTPException(400, detail="No URL provided.")
    embed = {
        "title":  "✅ Job Scout — test notification",
        "color":  0x818CF8,
        "description": "Your Discord webhook is configured correctly!",
        "footer": {"text": "Job Scout • settings test"},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(body.url, json={"embeds": [embed]}) as resp:
                if resp.status in (200, 204):
                    return {"ok": True}
                text = await resp.text()
                return {"ok": False, "detail": f"HTTP {resp.status}: {text[:200]}"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


@router.post("/test-slack")
async def test_slack(body: WebhookTestRequest):
    """Send a test message to a Slack webhook URL."""
    if not body.url:
        raise HTTPException(400, detail="No URL provided.")
    payload = {"text": "✅ *Job Scout* — your Slack webhook is configured correctly!"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(body.url, json=payload) as resp:
                if resp.status == 200:
                    return {"ok": True}
                text = await resp.text()
                return {"ok": False, "detail": f"HTTP {resp.status}: {text[:200]}"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
