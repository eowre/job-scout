"""
keywords.py — Read and update the job keyword filter lists at runtime.

Keywords live in config.py but are exposed here as a live API so the
Analytics tab can display and edit them without a server restart.
Changes are written back to config.py and hot-reloaded into the
scraper module so the next scan picks them up immediately.
"""
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config

router = APIRouter(prefix="/config/keywords", tags=["keywords"])

CONFIG_PATH = Path(__file__).parent.parent / "config.py"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_list(src: str, var: str) -> list[str]:
    """Extract a Python list literal from the config source text."""
    pattern = rf'{var}\s*=\s*\[(.*?)\]'
    m = re.search(pattern, src, re.DOTALL)
    if not m:
        return []
    items = re.findall(r'"([^"]+)"|\'([^\']+)\'', m.group(1))
    return [a or b for a, b in items]


def _write_list(src: str, var: str, values: list[str]) -> str:
    """Replace a list literal in the config source text."""
    items = "\n".join(f'    "{v}",' for v in values)
    new_block = f'{var} = [\n{items}\n]'
    pattern = rf'{var}\s*=\s*\[.*?\]'
    return re.sub(pattern, new_block, src, flags=re.DOTALL)


def _reload():
    """Hot-reload the config module and push changes into scraper._matches."""
    import importlib
    importlib.reload(config)
    # Patch the scraper module's globals so the running process sees new values
    try:
        from services import scraper
        scraper.JOB_KEYWORDS    = config.JOB_KEYWORDS
        scraper.EXCLUDE_KEYWORDS = config.EXCLUDE_KEYWORDS
    except Exception:
        pass  # scraper may not be imported yet on first load


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def get_keywords():
    """Return current include and exclude keyword lists."""
    return {
        "include": config.JOB_KEYWORDS,
        "exclude": config.EXCLUDE_KEYWORDS,
    }


class KeywordsUpdate(BaseModel):
    include: list[str]
    exclude: list[str]


@router.put("")
def update_keywords(body: KeywordsUpdate):
    """Overwrite both keyword lists and persist to config.py."""
    # Validate — no empty strings
    include = [k.strip() for k in body.include if k.strip()]
    exclude = [k.strip() for k in body.exclude if k.strip()]

    src = CONFIG_PATH.read_text(encoding="utf-8")
    src = _write_list(src, "JOB_KEYWORDS",    include)
    src = _write_list(src, "EXCLUDE_KEYWORDS", exclude)
    CONFIG_PATH.write_text(src, encoding="utf-8")

    _reload()

    return {"include": include, "exclude": exclude}
