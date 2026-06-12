from ai.client import complete_json

STRUCTURE_PROMPT = """You are a resume parser and editor. You will be given:
1. The original resume as raw text
2. A list of bullet changes the user has accepted (original → tailored version)

Your job is to reconstruct the full resume as structured JSON, applying the accepted bullet changes.

Original resume text:
<resume>
{resume_text}
</resume>

Accepted bullet changes (apply these — swap original for tailored):
<accepted_changes>
{accepted_changes}
</accepted_changes>

Denied bullets (keep original text for these):
<denied_bullets>
{denied_bullets}
</denied_bullets>

Instructions:
- Parse the resume into structured sections
- For each bullet in experience: if it matches an accepted original, use the tailored version instead
- Matching may be fuzzy — use your judgment if the original is slightly different due to formatting
- Preserve ALL other content exactly as-is
- If a summary was tailored (from the analysis), include it in the summary field

Respond ONLY with valid JSON in this exact format:
{{
  "name": "<full name>",
  "contact": "<email | phone | location | linkedin — joined with  |  >",
  "summary": "<professional summary or empty string>",
  "experience": [
    {{
      "title": "<job title>",
      "company": "<company name>",
      "dates": "<date range>",
      "bullets": ["<bullet>", "<bullet>"]
    }}
  ],
  "education": [
    {{
      "degree": "<degree and field>",
      "school": "<school name>",
      "dates": "<date range>",
      "notes": "<GPA, honors, etc or empty string>"
    }}
  ],
  "skills": ["<skill category: skill1, skill2>"],
  "extras": ["<certifications, awards, publications, etc>"]
}}
"""


async def structure_resume(resume_text: str, accepted: list, denied: list) -> dict:
    accepted_lines = "\n".join(
        f"- ORIGINAL: {b['original']}\n  TAILORED: {b['tailored']}"
        for b in accepted
    ) or "None"

    denied_lines = "\n".join(f"- {b['original']}" for b in denied) or "None"

    prompt = STRUCTURE_PROMPT.format(
        resume_text=resume_text,
        accepted_changes=accepted_lines,
        denied_bullets=denied_lines,
    )

    return await complete_json(prompt, max_tokens=4096)
