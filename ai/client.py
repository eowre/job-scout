"""Shared Anthropic client and response helpers for the ai/ modules."""
import json
import os
import re

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DEFAULT_MODEL = "claude-sonnet-4-6"

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def extract_json(raw: str) -> dict:
    """Parse a model response that should be a JSON object.

    Tolerates markdown code fences and stray prose around the object.
    """
    raw = raw.strip()
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span (model added prose around it)
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start:end + 1])
        raise


async def complete(prompt: str, max_tokens: int = 1024, model: str = DEFAULT_MODEL) -> str:
    message = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


async def complete_json(prompt: str, max_tokens: int = 1024, model: str = DEFAULT_MODEL) -> dict:
    return extract_json(await complete(prompt, max_tokens=max_tokens, model=model))
