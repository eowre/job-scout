import asyncio
import collections
import json
import logging
import os
import re
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting
#
# Anthropic free/low tier: 50 RPM and 10,000 output tokens/min for Haiku.
# We target 40 RPM (10 below the cap) to stay safely under the limit even
# during scan bursts.  Two mechanisms work together:
#
#   _throttle()  — sliding-window gate: tracks timestamps of the last 60s and
#                  sleeps until capacity is available before recording a new one.
#   _sem         — limits concurrent in-flight calls so the event loop isn't
#                  flooded with tasks all racing through _throttle at once.
#
# On top of that, parse_job retries up to _RETRY_ATTEMPTS times with
# exponential back-off on RateLimitError, releasing the semaphore before
# sleeping so it doesn't block other tasks.
# ---------------------------------------------------------------------------

_RATE_LIMIT_RPM = 40          # stay 10 under the 50 RPM hard cap
_CONCURRENCY    = 3           # max simultaneous in-flight API calls
_RETRY_ATTEMPTS = 4           # 1 original attempt + 3 retries
_RETRY_BASE_S   = 5           # back-off base: 5 s, 10 s, 20 s

_sem                           = asyncio.Semaphore(_CONCURRENCY)
_timestamps: collections.deque = collections.deque()
_rate_lock                     = asyncio.Lock()

_client: anthropic.AsyncAnthropic | None = None


async def _throttle() -> None:
    """
    Block until the sliding-window rate allows another request, then record
    this call's timestamp.  The lock is released during any sleep so other
    coroutines can check concurrently.
    """
    while True:
        async with _rate_lock:
            now = time.monotonic()
            # Evict timestamps outside the rolling 60-second window
            while _timestamps and _timestamps[0] < now - 60.0:
                _timestamps.popleft()
            if len(_timestamps) < _RATE_LIMIT_RPM:
                _timestamps.append(now)
                return          # slot acquired, proceed
            # Full — calculate how long until the oldest slot expires
            wait = 60.0 - (now - _timestamps[0]) + 0.05
        # Lock released here; other coroutines can check while we wait
        await asyncio.sleep(max(wait, 0))


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


_SYSTEM = (
    "You are a job-posting analyst. Extract structured information from job descriptions. "
    "Always respond with valid JSON only — no markdown fences, no explanation."
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

Respond ONLY with this JSON shape:
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
    """Strip optional markdown code fences that the model sometimes adds."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text)
    if fenced:
        return fenced.group(1).strip()
    return text


async def parse_job(title: str, company: str, raw_description: str) -> dict:
    """
    Return parsed fields for a job posting.  Never raises — returns {} on any
    unrecoverable failure.

    Rate limiting:  each attempt acquires _sem and calls _throttle() before
    hitting the API.  On RateLimitError the semaphore is released before the
    back-off sleep so other tasks can proceed while this one waits.
    """
    if not raw_description or len(raw_description.strip()) < 50:
        return {}

    prompt = _build_prompt(title, company, raw_description)
    client = _get_client()
    raw_text = ""

    for attempt in range(_RETRY_ATTEMPTS):
        retry_wait: float = 0.0

        async with _sem:
            await _throttle()
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=768,
                    system=_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_text = response.content[0].text if response.content else ""
                text = _extract_json(raw_text)

                if not text:
                    logger.warning(
                        f"Parser got empty response for '{title}' @ {company} "
                        f"(stop_reason={response.stop_reason})"
                    )
                    return {}

                return json.loads(text)

            except anthropic.RateLimitError:
                if attempt >= _RETRY_ATTEMPTS - 1:
                    logger.warning(
                        f"Parser rate-limited: giving up after {_RETRY_ATTEMPTS} "
                        f"attempts for '{title}' @ {company}"
                    )
                    return {}
                retry_wait = _RETRY_BASE_S * (2 ** attempt)   # 5 s, 10 s, 20 s
                logger.warning(
                    f"Parser rate-limited for '{title}' @ {company} — "
                    f"retry {attempt + 1}/{_RETRY_ATTEMPTS - 1} in {retry_wait}s"
                )

            except json.JSONDecodeError as exc:
                logger.warning(
                    f"Parser JSON decode error for '{title}' @ {company}: {exc} | "
                    f"raw={repr(raw_text[:200])}"
                )
                return {}

            except Exception as exc:
                logger.warning(f"Parser error for '{title}' @ {company}: {exc}")
                return {}

        # Semaphore released above before sleeping — other tasks can proceed
        if retry_wait:
            await asyncio.sleep(retry_wait)

    return {}  # exhausted all retries
