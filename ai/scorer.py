import json
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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
- recommendation: "apply" if score >= 70, "apply_with_tailoring" if 50-69, "skip" if < 50
"""


async def score_fit(resume_text: str, jd_text: str) -> dict:
    prompt = SCORER_PROMPT.format(resume_text=resume_text, jd_text=jd_text)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)
