from ai.client import complete_json

TAILOR_PROMPT = """You are an expert resume writer. Your job is to tailor resume bullets to better match a job description, without fabricating experience.

Resume text:
<resume>
{resume_text}
</resume>

Job Description:
<job_description>
{jd_text}
</job_description>

Gaps and missing keywords identified:
<gaps>
{gaps}
</gaps>

<missing_keywords>
{missing_keywords}
</missing_keywords>
{gap_context_section}
Instructions:
1. Extract 5-8 existing resume bullets that are most relevant or could be strengthened
2. For each, rewrite it to better reflect the JD's language and priorities — do NOT invent new experience
3. Also surface 2-3 bullets from the resume that are currently buried but are actually highly relevant
4. Use strong action verbs and quantify wherever the original data supports it
5. Where the candidate has provided context for a gap, prioritise finding or adapting bullets that address that gap using only real experience described

Respond ONLY with a valid JSON object in this exact format:
{{
  "tailored_bullets": [
    {{
      "original": "<original bullet text>",
      "tailored": "<rewritten bullet>",
      "reason": "<why this change helps>"
    }}
  ],
  "surfaced_bullets": [
    {{
      "bullet": "<existing bullet that's underutilized>",
      "why_relevant": "<explanation of relevance to this JD>"
    }}
  ],
  "summary_suggestion": "<1-3 sentence tailored professional summary for this role>"
}}
"""

GAP_CONTEXT_BLOCK = """
The candidate has provided context about their experience relevant to specific gaps:
<candidate_gap_context>
{entries}
</candidate_gap_context>

Use this context to better tailor bullets — surface or adapt real resume content that addresses these gaps.
"""

RETAILOR_BULLET_PROMPT = """You are an expert resume writer refining a single resume bullet based on user feedback.

Original bullet:
<original>
{original}
</original>

Previous tailored version (that the user wants improved):
<previous_tailored>
{previous_tailored}
</previous_tailored>

Job Description:
<job_description>
{jd_text}
</job_description>

User's additional context / feedback:
<user_context>
{user_context}
</user_context>

Instructions:
- Rewrite the bullet incorporating the user's feedback
- Do NOT invent experience that isn't in the original
- Keep it concise (1-2 lines max)
- Use a strong action verb

Respond ONLY with a valid JSON object:
{{
  "tailored": "<rewritten bullet>",
  "reason": "<what changed and why>"
}}
"""


async def retailor_bullet(original: str, previous_tailored: str, jd_text: str, user_context: str) -> dict:
    prompt = RETAILOR_BULLET_PROMPT.format(
        original=original,
        previous_tailored=previous_tailored,
        jd_text=jd_text,
        user_context=user_context
    )

    return await complete_json(prompt, max_tokens=512)


async def tailor_resume(resume_text: str, jd_text: str, gaps: list, missing_keywords: list, gap_contexts: list = None) -> dict:
    gap_context_section = ""
    if gap_contexts:
        entries = "\n".join(
            f'- Gap: "{gc["gap"]}" → Candidate\'s experience: "{gc["experience"]}"'
            for gc in gap_contexts
        )
        gap_context_section = GAP_CONTEXT_BLOCK.format(entries=entries)

    prompt = TAILOR_PROMPT.format(
        resume_text=resume_text,
        jd_text=jd_text,
        gaps="\n".join(f"- {g}" for g in gaps),
        missing_keywords=", ".join(missing_keywords),
        gap_context_section=gap_context_section
    )

    return await complete_json(prompt, max_tokens=2048)
