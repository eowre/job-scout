import json
import logging
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_SYSTEM = (
    "You are a job-posting analyst. Extract structured information from job descriptions. "
    "Always respond with valid JSON only — no markdown, no explanation."
)

_PROMPT = """Given this job posting, extract:
1. A 2-3 sentence plain-English summary of the role.
2. Compensation (salary range, equity, or null if not mentioned).
3. The 4-7 most important responsibilities as short bullet strings.

Job Title: {title}
Company: {company}

Description:
{description}

Respond ONLY with this JSON shape:
{{
  "parsed_summary": "<2-3 sentence summary>",
  "compensation": "<salary/comp string or null>",
  "responsibilities": ["<bullet 1>", "<bullet 2>", ...]
}}"""


async def parse_job(title: str, company: str, raw_description: str) -> dict:
    """Return parsed fields for a job posting. Never raises — returns empty dict on failure."""
    if not raw_description or len(raw_description.strip()) < 50:
        return {}

    prompt = _PROMPT.format(
        title=title,
        company=company,
        description=raw_description[:4000],
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"Parser JSON decode error for '{title}' @ {company}: {exc}")
        return {}
    except Exception as exc:
        logger.warning(f"Parser error for '{title}' @ {company}: {exc}")
        return {}
