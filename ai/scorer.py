"""Resume-vs-JD fit scoring."""
from ai.client import complete_json

# Thresholds used both in the prompt and to sanity-check the model's output.
APPLY_THRESHOLD = 70
TAILOR_THRESHOLD = 50

SCORER_PROMPT = """You are an expert technical recruiter and career coach. Analyze the fit between a candidate's resume and a job description.

Resume:
<resume>
{resume_text}
</resume>

Job Description:
<job_description>
{jd_text}
</job_description>

Respond ONLY with a valid JSON object in this exact format:
{{
  "overall_score": <integer 0-100>,
  "recommendation": "apply" | "skip" | "apply_with_tailoring",
  "recommendation_reason": "<1-2 sentence explanation>",
  "breakdown": {{
    "skills_match": <integer 0-100>,
    "experience_level": <integer 0-100>,
    "domain_keywords": <integer 0-100>,
    "red_flags": <integer 0-100>
  }},
  "strengths": ["<bullet>", "<bullet>", "<bullet>"],
  "gaps": ["<bullet>", "<bullet>", "<bullet>"],
  "red_flags": ["<bullet or empty list>"],
  "missing_keywords": ["<keyword>", "<keyword>"]
}}

Score explanations:
- overall_score: weighted average of breakdown scores
- skills_match: how well technical/soft skills align
- experience_level: does years/seniority match the role
- domain_keywords: coverage of industry/role-specific terms
- red_flags: 100 = no red flags, 0 = serious mismatches
- recommendation: "apply" if score >= {apply_threshold}, "apply_with_tailoring" if {tailor_threshold}-{apply_upper}, "skip" if < {tailor_threshold}
"""

_LIST_FIELDS = ("strengths", "gaps", "red_flags", "missing_keywords")


def recommendation_for(score: int) -> str:
    if score >= APPLY_THRESHOLD:
        return "apply"
    if score >= TAILOR_THRESHOLD:
        return "apply_with_tailoring"
    return "skip"


def _validate(result: dict) -> dict:
    """Clamp and normalise the model's output so downstream code can trust it."""
    score = result.get("overall_score")
    if not isinstance(score, (int, float)):
        raise ValueError(f"Scorer returned a non-numeric overall_score: {score!r}")
    result["overall_score"] = max(0, min(100, int(score)))

    if result.get("recommendation") not in ("apply", "skip", "apply_with_tailoring"):
        result["recommendation"] = recommendation_for(result["overall_score"])

    breakdown = result.get("breakdown")
    result["breakdown"] = breakdown if isinstance(breakdown, dict) else {}

    for field in _LIST_FIELDS:
        value = result.get(field)
        result[field] = value if isinstance(value, list) else []

    return result


async def score_fit(resume_text: str, jd_text: str) -> dict:
    prompt = SCORER_PROMPT.format(
        resume_text=resume_text,
        jd_text=jd_text,
        apply_threshold=APPLY_THRESHOLD,
        tailor_threshold=TAILOR_THRESHOLD,
        apply_upper=APPLY_THRESHOLD - 1,
    )
    return _validate(await complete_json(prompt, max_tokens=1024))
