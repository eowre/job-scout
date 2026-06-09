"""
routers/settings.py — Runtime parser configuration.

GET  /settings              → current settings dict
PUT  /settings              → update one or more settings (persists to DB)
GET  /settings/ollama-models → list of models installed in Ollama
"""
import logging

import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.parser import get_parser_config, update_parser_config
from database import save_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

_ALLOWED_KEYS = {"parser_backend", "ollama_model", "ollama_base_url", "anthropic_model"}


class SettingsUpdate(BaseModel):
    parser_backend:  str | None = None
    ollama_model:    str | None = None
    ollama_base_url: str | None = None
    anthropic_model: str | None = None


@router.get("")
def get_settings():
    """Return the current live parser configuration."""
    return get_parser_config()


@router.put("")
def put_settings(body: SettingsUpdate):
    """
    Update one or more parser settings.  Changes take effect immediately
    (no container restart needed) and are persisted to the database.
    """
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k in _ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid settings provided.")

    if "parser_backend" in updates and updates["parser_backend"] not in ("ollama", "anthropic"):
        raise HTTPException(status_code=400, detail="parser_backend must be 'ollama' or 'anthropic'.")

    for key, value in updates.items():
        save_setting(key, value)

    update_parser_config(updates)
    return get_parser_config()


@router.get("/ollama-models")
async def get_ollama_models():
    """
    Proxy Ollama's /api/tags endpoint and return a list of installed model names.
    Returns an empty list (not an error) if Ollama is unreachable.
    """
    cfg = get_parser_config()
    base_url = cfg.get("ollama_base_url", "http://localhost:11434").rstrip("/")
    url = f"{base_url}/api/tags"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"models": [], "reachable": False}
                data = await resp.json(content_type=None)
        models = [m["name"] for m in (data.get("models") or [])]
        return {"models": models, "reachable": True}
    except Exception as exc:
        logger.debug(f"Ollama unreachable at {url}: {exc}")
        return {"models": [], "reachable": False}
