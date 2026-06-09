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
# Backend selection
#
#   PARSER_BACKEND   "ollama" (default) or "anthropic"
#
# Switch by setting the env var in your .env file.  The rest of the app is
# unaffected — parse_job() has the same signature and return value either way.
# ---------------------------------------------------------------------------

_PARSER_BACKEND = os.getenv("PARSER_BACKEND", "ollama").lower().strip()

# ---------------------------------------------------------------------------
# Ollama configuration  (used when PARSER_BACKEND=ollama)
#
#   OLLAMA_BASE_URL     URL of your Ollama instance  (default: http://localhost:11434)
#   OLLAMA_MODEL        Model tag to use              (default: llama3.1:8b)
#   OLLAMA_CONCURRENCY  Parallel in-flight requests   (default: 2)
#   OLLAMA_TIMEOUT_S    Per-request timeout seconds   (default: 120)
#
# Recommended models for CPU-only inference (ThinkCentre M900 or similar):
#   llama3.1:8b    — best all-round; solid JSON output, ~3-5 tok/s on CPU
#   qwen2.5:7b     — slightly better structured output, same memory footprint
#   mistral:7b     — fastest, good JSON compliance
#   gemma2:9b      — strong reasoning, slightly larger (~6 GB)
#
# Pull a model:  ollama pull <model-tag>
# List models:   ollama list
# ---------------------------------------------------------------------------

_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "llama3.1:8b")
_OLLAMA_TIMEOUT_S = int(os.getenv("OLLAMA_TIMEOUT_S", "120"))

# ---------------------------------------------------------------------------
# Anthropic configuration  (used when PARSER_BACKEND=anthropic)
#
#   ANTHROPIC_API_KEY     your Anthropic key  (already in .env)
#   ANTHROPIC_MODEL       model to use        (default: claude-haiku-4-5-20251001)
#
# Rate limiting: Anthropic free/low tier is 50 RPM for Haiku.
# We target 40 RPM with a sliding-window gate + semaphore.
# ---------------------------------------------------------------------------

_ANTHROPIC_MODEL    = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_ANTHROPIC_RPM      = 40    # stay 10 under the 50 RPM hard cap
_ANTHROPIC_RETRIES  = 4     # 1 attempt + 3 retries
_ANTHROPIC_RETRY_S  = 5     # back-off base: 5 s, 10 s, 20 s

_anthropic_client: anthropic.AsyncAnthropic | None = None
_anthropic_timestamps: collections.deque            = collections.deque()
_anthropic_rate_lock                                = asyncio.Lock()

# ---------------------------------------------------------------------------
# Shared concurrency semaphore
#
# For Ollama:    keeps CPU from being overloaded (local inference is the bottleneck)
# For Anthropic: caps simultaneous in-flight API calls
# ---------------------------------------------------------------------------

_CONCURRENCY = int(os.getenv("OLLAMA_CONCURRENCY" if _PARSER_BACKEND == "ollama" else "ANTHROPIC_CONCURRENCY", "2"))
_sem         = asyncio.Semaphore(_CONCURRENCY)

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
    # String substitution avoids str.format() choking on curly braces in descriptions.
    return (
        _PROMPT
        .replace("TITLE_PLACEHOLDER", title)
        .replace("COMPANY_PLACEHOLDER", company)
        .replace("DESCRIPTION_PLACEHOLDER", description[:4000])
    )


def _extract_json(text: str) -> str:
    """Strip optional markdown code fences or leading prose the model adds."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text)
    if fenced:
        return fenced.group(1).strip()
    # Some models prefix with a sentence before the opening brace — skip it.
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    return text


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

async def _call_ollama(prompt: str, system: str) -> str:
    """POST to Ollama's OpenAI-compatible endpoint and return the response text."""
    url     = f"{_OLLAMA_BASE_URL}/v1/chat/completions"
    payload = {
        "model":   _OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "temperature":     0.1,
        "stream":          False,
        # Forces JSON output on models that support it (llama3.1, qwen2.5, mistral…)
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
    """Sliding-window rate limiter — blocks until under _ANTHROPIC_RPM."""
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
    """Call the Anthropic messages API with rate-limiting and retries."""
    client    = _get_anthropic_client()
    raw_text  = ""

    for attempt in range(_ANTHROPIC_RETRIES):
        retry_wait: float = 0.0

        async with _sem:
            await _anthropic_throttle()
            try:
                response = await client.messages.create(
                    model=_ANTHROPIC_MODEL,
                    max_tokens=768,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text if response.content else ""

            except anthropic.RateLimitError:
                if attempt >= _ANTHROPIC_RETRIES - 1:
                    raise
                retry_wait = _ANTHROPIC_RETRY_S * (2 ** attempt)   # 5 s, 10 s, 20 s
                logger.warning(
                    f"Anthropic rate-limited — retry {attempt + 1}/{_ANTHROPIC_RETRIES - 1} "
                    f"in {retry_wait}s"
                )

        if retry_wait:
            await asyncio.sleep(retry_wait)

    return raw_text


# ---------------------------------------------------------------------------
# Public API  (callers are unchanged regardless of backend)
# ---------------------------------------------------------------------------

async def parse_job(title: str, company: str, raw_description: str) -> dict:
    """
    Return parsed fields for a job posting.  Never raises — returns {} on any
    unrecoverable failure.

    Backend is selected by PARSER_BACKEND env var ("ollama" or "anthropic").

    Fields returned on success:
        parsed_summary      str
        compensation        str | None
        responsibilities    list[str]
        years_of_experience int | None
    """
    if not raw_description or len(raw_description.strip()) < 50:
        return {}

    prompt   = _build_prompt(title, company, raw_description)
    raw_text = ""

    try:
        async with _sem:
            if _PARSER_BACKEND == "anthropic":
                raw_text = await _call_anthropic(prompt, _SYSTEM)
            else:
                raw_text = await _call_ollama(prompt, _SYSTEM)

        text = _extract_json(raw_text)
        if not text:
            logger.warning(
                f"[{_PARSER_BACKEND}] Parser got empty response for '{title}' @ {company}"
            )
            return {}

        return json.loads(text)

    except json.JSONDecodeError as exc:
        logger.warning(
            f"[{_PARSER_BACKEND}] JSON decode error for '{title}' @ {company}: {exc} | "
            f"raw={repr(raw_text[:300])}"
        )
        return {}

    except aiohttp.ClientConnectorError:
        logger.warning(
            f"[ollama] Cannot reach Ollama at {_OLLAMA_BASE_URL} — "
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
        logger.warning(f"[{_PARSER_BACKEND}] Parser error for '{title}' @ {company}: {exc}")
        return {}
