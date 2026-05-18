import asyncio
import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Limit concurrent Anthropic calls so we don't slam the API when a scan
# surfaces dozens of new jobs at once.
_sem = asyncio.Semaphore(4)

_client: anthropic.AsyncAnthropic | None = None


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
    """Return parsed fields for a job posting. Never raises — returns {} on failure."""
    if not raw_description or len(raw_description.strip()) < 50:
        return {}

    prompt = _build_prompt(title, company, raw_description)
    client = _get_client()

    async with _sem:
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

        except json.JSONDecodeError as exc:
            logger.warning(
                f"Parser JSON decode error for '{title}' @ {company}: {exc} | "
                f"raw={repr(raw_text[:200] if 'raw_text' in dir() else 'N/A')}"
            )
            return {}
        except Exception as exc:
            logger.warning(f"Parser error for '{title}' @ {company}: {exc}")
            return {}
