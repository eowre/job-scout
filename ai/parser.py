import asyncio
import json
import logging
import os
import re

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — set these in your .env or environment
#
#   OLLAMA_BASE_URL     URL of your Ollama instance  (default: http://localhost:11434)
#   OLLAMA_MODEL        Model tag to use              (default: llama3.1:8b)
#   OLLAMA_CONCURRENCY  Parallel requests             (default: 2)
#   OLLAMA_TIMEOUT_S    Per-request timeout seconds   (default: 120)
#
# Recommended models for CPU-only inference (ThinkCentre M900 or similar):
#   llama3.1:8b   — ~5 GB RAM, good JSON output, ~3-5 tok/s on a modern CPU
#   qwen2.5:7b    — slightly better structured output, similar memory footprint
#   mistral:7b    — fast, reliable JSON when prompted correctly
#
# Quick-start on your Linux server:
#   curl -fsSL https://ollama.ai/install.sh | sh
#   ollama pull llama3.1:8b
#   # Ollama registers a systemd service and starts automatically after install.
#   # To confirm it's running:  ollama list
# ---------------------------------------------------------------------------

_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "llama3.1:8b")

# Keep concurrency low — local CPU inference is the bottleneck, not the network.
# Running 2 jobs in parallel on an 8-core CPU is a good starting point; raise
# to 3-4 only if you see the CPU sitting idle between requests.
_CONCURRENCY = int(os.getenv("OLLAMA_CONCURRENCY", "2"))

# CPU inference is slow; 120 s is generous but prevents hanging forever.
_TIMEOUT_S   = int(os.getenv("OLLAMA_TIMEOUT_S", "120"))

_sem = asyncio.Semaphore(_CONCURRENCY)

# ---------------------------------------------------------------------------
# Prompt
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
# Ollama API call
# ---------------------------------------------------------------------------

async def _call_ollama(prompt: str, system: str) -> str:
    """
    POST to Ollama's OpenAI-compatible chat endpoint and return the assistant
    message text.  Raises on HTTP / connection errors so callers can log and
    return {}.
    """
    url = f"{_OLLAMA_BASE_URL}/v1/chat/completions"
    payload = {
        "model": _OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.1,   # low temperature → deterministic, valid JSON
        "stream": False,
        # Force JSON output — supported by llama3.1, qwen2.5, mistral, etc.
        # Ollama silently ignores this for models that don't support it.
        "response_format": {"type": "json_object"},
    }

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
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
# Public API  (same signature as before — callers are unchanged)
# ---------------------------------------------------------------------------

async def parse_job(title: str, company: str, raw_description: str) -> dict:
    """
    Return parsed fields for a job posting.  Never raises — returns {} on any
    unrecoverable failure.

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

    async with _sem:
        try:
            raw_text = await _call_ollama(prompt, _SYSTEM)
            text     = _extract_json(raw_text)

            if not text:
                logger.warning(
                    f"Parser got empty response for '{title}' @ {company}"
                )
                return {}

            return json.loads(text)

        except json.JSONDecodeError as exc:
            logger.warning(
                f"Parser JSON decode error for '{title}' @ {company}: {exc} | "
                f"raw={repr(raw_text[:300])}"
            )
            return {}

        except aiohttp.ClientConnectorError:
            logger.warning(
                f"Parser cannot reach Ollama at {_OLLAMA_BASE_URL} — "
                f"is Ollama running?  Try: ollama serve"
            )
            return {}

        except Exception as exc:
            logger.warning(f"Parser error for '{title}' @ {company}: {exc}")
            return {}
