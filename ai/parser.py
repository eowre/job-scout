import asyncio
import collections
import json
import logging
import os
import re
import time

import aiohttp
import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime config
#
# Settings are loaded from the database on startup (see database.load_settings
# + main.py lifespan).  The UI writes changes back via PUT /settings, which
# calls update_parser_config() here so the new values take effect immediately
# without a container restart.
#
# .env / environment variables seed the DB defaults on first boot only.
# After that the DB is the source of truth.
#
# Keys:
#   parser_backend   "ollama" | "anthropic"
#   ollama_base_url  e.g. "http://host.docker.internal:11434"
#   ollama_model     e.g. "llama3.1:8b"
#   anthropic_model  e.g. "claude-haiku-4-5-20251001"
# ---------------------------------------------------------------------------

_config: dict = {
    "parser_backend":  os.getenv("PARSER_BACKEND",    "ollama"),
    "ollama_base_url": os.getenv("OLLAMA_BASE_URL",   "http://localhost:11434"),
    "ollama_model":    os.getenv("OLLAMA_MODEL",       "llama3.1:8b"),
    "anthropic_model": os.getenv("ANTHROPIC_MODEL",    "claude-haiku-4-5-20251001"),
}


def get_parser_config() -> dict:
    """Return a snapshot of the current parser configuration."""
    return dict(_config)


def update_parser_config(updates: dict) -> None:
    """
    Apply a dict of setting updates to the live config.
    Changes take effect on the next parse_job() call.
    """
    _config.update({k: v for k, v in updates.items() if k in _config})
    logger.info(
        f"Parser config updated: backend={_config['parser_backend']} "
        f"model={_config['ollama_model'] if _config['parser_backend'] == 'ollama' else _config['anthropic_model']}"
    )


# ---------------------------------------------------------------------------
# Concurrency — env-var only (not hot-swappable; changing needs a restart)
#
#   OLLAMA_CONCURRENCY  parallel Ollama requests    (default: 2)
#   ANTHROPIC_CONCURRENCY  parallel Anthropic calls (default: 3)
# ---------------------------------------------------------------------------

_OLLAMA_CONCURRENCY     = int(os.getenv("OLLAMA_CONCURRENCY",     "2"))
_ANTHROPIC_CONCURRENCY  = int(os.getenv("ANTHROPIC_CONCURRENCY",  "3"))
_OLLAMA_TIMEOUT_S       = int(os.getenv("OLLAMA_TIMEOUT_S",       "120"))

_ollama_sem    = asyncio.Semaphore(_OLLAMA_CONCURRENCY)
_anthropic_sem = asyncio.Semaphore(_ANTHROPIC_CONCURRENCY)

# ---------------------------------------------------------------------------
# Anthropic rate-limiting (50 RPM cap on free/low tier — target 40 RPM)
# ---------------------------------------------------------------------------

_ANTHROPIC_RPM     = 40
_ANTHROPIC_RETRIES = 4
_ANTHROPIC_RETRY_S = 5

_anthropic_client:     anthropic.AsyncAnthropic | None = None
_anthropic_timestamps: collections.deque               = collections.deque()
_anthropic_rate_lock                                   = asyncio.Lock()

# ---------------------------------------------------------------------------
# Prompt  (shared by both backends)
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a job-posting analyst. Extract structured information from job "
    "descriptions. Always respond with valid JSON only — no markdown fences, "
    "no explanation, no preamble."
)

_PROMPT = """Given this job posting, extract:
1. A 2-3 sentence plain-English summary of the role.
2. Compensation (salary range, equity, or null if not mentioned).
3. The 4-7 most important responsibilities as short bullet strings.
4. Minimum years of experience required as an integer (e.g. if "3-5 years" use 3; if "entry level" use 0; if not mentioned use null).

Job Title: TITLE_PLACEHOLDER
Company: COMPANY_PLACEHOLDER

Description:
DESCRIPTION_PLACEHOLDER

Respond ONLY with this JSON shape (no markdown, no extra text):
{
  "parsed_summary": "<2-3 sentence summary>",
  "compensation": "<salary/comp string or null>",
  "responsibilities": ["<bullet 1>", "<bullet 2>", ...],
  "years_of_experience": <integer or null>
}"""


def _build_prompt(title: str, company: str, description: str) -> str:
    return (
        _PROMPT
        .replace("TITLE_PLACEHOLDER", title)
        .replace("COMPANY_PLACEHOLDER", company)
        .replace("DESCRIPTION_PLACEHOLDER", description[:4000])
    )


def _extract_json(text: str) -> str:
    """Strip optional markdown fences or leading prose."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text)
    if fenced:
        return fenced.group(1).strip()
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    return text


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

async def _call_ollama(prompt: str, system: str) -> str:
    cfg     = get_parser_config()
    url     = cfg["ollama_base_url"].rstrip("/") + "/v1/chat/completions"
    payload = {
        "model":           cfg["ollama_model"],
        "messages":        [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "temperature":     0.1,
        "stream":          False,
        "response_format": {"type": "json_object"},
    }
    timeout = aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ValueError(f"Ollama HTTP {resp.status}: {body[:300]}")
            data = await resp.json(content_type=None)

    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"Ollama returned no choices: {data}")
    return choices[0].get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )
    return _anthropic_client


async def _anthropic_throttle() -> None:
    while True:
        async with _anthropic_rate_lock:
            now = time.monotonic()
            while _anthropic_timestamps and _anthropic_timestamps[0] < now - 60.0:
                _anthropic_timestamps.popleft()
            if len(_anthropic_timestamps) < _ANTHROPIC_RPM:
                _anthropic_timestamps.append(now)
                return
            wait = 60.0 - (now - _anthropic_timestamps[0]) + 0.05
        await asyncio.sleep(max(wait, 0))


async def _call_anthropic(prompt: str, system: str) -> str:
    cfg    = get_parser_config()
    client = _get_anthropic_client()

    for attempt in range(_ANTHROPIC_RETRIES):
        retry_wait: float = 0.0
        async with _anthropic_sem:
            await _anthropic_throttle()
            try:
                response = await client.messages.create(
                    model=cfg["anthropic_model"],
                    max_tokens=768,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text if response.content else ""

            except anthropic.RateLimitError:
                if attempt >= _ANTHROPIC_RETRIES - 1:
                    raise
                retry_wait = _ANTHROPIC_RETRY_S * (2 ** attempt)
                logger.warning(
                    f"[anthropic] Rate-limited — retry {attempt + 1}/{_ANTHROPIC_RETRIES - 1} "
                    f"in {retry_wait}s"
                )

        if retry_wait:
            await asyncio.sleep(retry_wait)

    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def parse_job(title: str, company: str, raw_description: str) -> dict:
    """
    Return parsed fields for a job posting.  Never raises — returns {} on
    any unrecoverable failure.

    Backend is selected by the live config (changeable at runtime via the
    Settings UI without restarting the container).

    Fields returned on success:
        parsed_summary      str
        compensation        str | None
        responsibilities    list[str]
        years_of_experience int | None
    """
    if not raw_description or len(raw_description.strip()) < 50:
        return {}

    prompt  = _build_prompt(title, company, raw_description)
    backend = get_parser_config()["parser_backend"]
    raw_text = ""

    try:
        if backend == "anthropic":
            raw_text = await _call_anthropic(prompt, _SYSTEM)
        else:
            async with _ollama_sem:
                raw_text = await _call_ollama(prompt, _SYSTEM)

        text = _extract_json(raw_text)
        if not text:
            logger.warning(f"[{backend}] Empty response for '{title}' @ {company}")
            return {}

        return json.loads(text)

    except json.JSONDecodeError as exc:
        logger.warning(
            f"[{backend}] JSON decode error for '{title}' @ {company}: {exc} | "
            f"raw={repr(raw_text[:300])}"
        )
        return {}

    except aiohttp.ClientConnectorError:
        cfg = get_parser_config()
        logger.warning(
            f"[ollama] Cannot reach Ollama at {cfg['ollama_base_url']} — "
            f"is it running?  Try: ollama serve"
        )
        return {}

    except anthropic.RateLimitError:
        logger.warning(
            f"[anthropic] Rate limit exhausted after {_ANTHROPIC_RETRIES} attempts "
            f"for '{title}' @ {company}"
        )
        return {}

    except Exception as exc:
        logger.warning(f"[{backend}] Parser error for '{title}' @ {company}: {exc}")
        return {}
