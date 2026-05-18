import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

STAGE_CONTEXT = {
    "Applied": "The candidate has just applied and wants to send a brief follow-up to confirm receipt and express enthusiasm.",
    "Phone Screen": "The candidate had a phone screen and wants to send a thank-you note that reinforces key points discussed.",
    "Interview": "The candidate completed an interview and wants a thank-you email that references specific conversation topics and reaffirms fit.",
    "Offer": "The candidate received an offer and wants a professional response acknowledging it (not negotiating, just acknowledging receipt).",
    "Rejected": "The candidate was rejected and wants a gracious, professional response that keeps the door open for future opportunities.",
}

FOLLOWUP_PROMPT = """You are a professional career coach helping draft a follow-up email.

Job Details:
- Title: {title}
- Company: {company}
- Stage: {stage}

Context for this stage: {stage_context}

Job Description summary:
<jd>
{jd_snippet}
</jd>

Additional notes from candidate:
{notes}

Write a concise, professional follow-up email for this stage. Keep it under 150 words. Be warm but not sycophantic.
Do NOT use generic filler phrases like "I wanted to reach out" or "I hope this email finds you well."

Format:
Subject: <subject line>

<email body>
"""


async def draft_followup(title: str, company: str, stage: str, jd_text: str, notes: str = "") -> str:
    jd_snippet = jd_text[:800] if len(jd_text) > 800 else jd_text
    stage_context = STAGE_CONTEXT.get(stage, "The candidate wants to send a professional follow-up.")

    prompt = FOLLOWUP_PROMPT.format(
        title=title,
        company=company or "the company",
        stage=stage,
        stage_context=stage_context,
        jd_snippet=jd_snippet,
        notes=notes or "None provided."
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text.strip()
