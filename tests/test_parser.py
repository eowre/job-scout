"""
test_parser.py — Unit tests for ai/parser.py.

The Anthropic API is mocked throughout — no real API calls are made.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from ai.parser import _build_prompt, _extract_json, parse_job


# ---------------------------------------------------------------------------
# _extract_json — strip markdown fences
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        raw = '{"key": "value"}'
        assert _extract_json(raw) == '{"key": "value"}'

    def test_strips_json_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        assert _extract_json(raw) == '{"key": "value"}'

    def test_strips_plain_fence(self):
        raw = '```\n{"key": "value"}\n```'
        assert _extract_json(raw) == '{"key": "value"}'

    def test_whitespace_trimmed(self):
        raw = '  \n  {"key": "value"}  \n  '
        assert _extract_json(raw).strip() == '{"key": "value"}'

    def test_empty_string(self):
        assert _extract_json("") == ""


# ---------------------------------------------------------------------------
# _build_prompt — placeholder substitution
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_title_substituted(self):
        prompt = _build_prompt("Software Engineer", "Acme", "desc")
        assert "Software Engineer" in prompt
        assert "TITLE_PLACEHOLDER" not in prompt

    def test_company_substituted(self):
        prompt = _build_prompt("SWE", "Acme Corp", "desc")
        assert "Acme Corp" in prompt
        assert "COMPANY_PLACEHOLDER" not in prompt

    def test_description_substituted(self):
        prompt = _build_prompt("SWE", "Acme", "We are looking for engineers.")
        assert "We are looking for engineers." in prompt
        assert "DESCRIPTION_PLACEHOLDER" not in prompt

    def test_description_truncated_at_4000(self):
        long_desc = "x" * 5000
        prompt = _build_prompt("SWE", "Acme", long_desc)
        # Only 4000 chars of description should appear
        assert "x" * 4001 not in prompt

    def test_curly_braces_in_description_dont_raise(self):
        """Curly braces in job descriptions must not break formatting."""
        desc = "Requirements: {Python, Go} or {Java}"
        # Should not raise
        prompt = _build_prompt("SWE", "Acme", desc)
        assert "{Python, Go}" in prompt


# ---------------------------------------------------------------------------
# parse_job — full async function with mocked Anthropic client
# ---------------------------------------------------------------------------

def _make_response(text: str):
    """Build a minimal mock that looks like an Anthropic messages response."""
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    response.stop_reason = "end_turn"
    return response


VALID_RESPONSE = json.dumps({
    "parsed_summary": "A great role at a great company.",
    "compensation": "$120k–$160k",
    "responsibilities": ["Write code", "Review PRs"],
    "years_of_experience": 3,
})


@pytest.mark.asyncio
async def test_parse_job_success():
    mock_create = AsyncMock(return_value=_make_response(VALID_RESPONSE))
    with patch("ai.parser._get_client") as mock_client_fn:
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("Software Engineer", "Acme", "We need an engineer with 3+ years. Looking for someone to write code and review PRs. Compensation is $120k–$160k.")

    assert result["parsed_summary"] == "A great role at a great company."
    assert result["compensation"] == "$120k–$160k"
    assert result["years_of_experience"] == 3
    assert isinstance(result["responsibilities"], list)


@pytest.mark.asyncio
async def test_parse_job_fenced_response():
    fenced = f"```json\n{VALID_RESPONSE}\n```"
    mock_create = AsyncMock(return_value=_make_response(fenced))
    with patch("ai.parser._get_client") as mock_client_fn:
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result.get("parsed_summary") is not None


@pytest.mark.asyncio
async def test_parse_job_invalid_json_returns_empty():
    mock_create = AsyncMock(return_value=_make_response("not valid json {{{"))
    with patch("ai.parser._get_client") as mock_client_fn:
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_empty_description_returns_empty():
    result = await parse_job("SWE", "Co", "")
    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_short_description_returns_empty():
    result = await parse_job("SWE", "Co", "Too short")
    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_api_exception_returns_empty():
    mock_create = AsyncMock(side_effect=Exception("API error"))
    with patch("ai.parser._get_client") as mock_client_fn:
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_null_yoe():
    response_data = {**json.loads(VALID_RESPONSE), "years_of_experience": None}
    mock_create = AsyncMock(return_value=_make_response(json.dumps(response_data)))
    with patch("ai.parser._get_client") as mock_client_fn:
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result["years_of_experience"] is None


@pytest.mark.asyncio
async def test_parse_job_rate_limit_retries_then_succeeds():
    """429 on first attempt should retry and succeed on second."""
    success = _make_response(VALID_RESPONSE)
    mock_create = AsyncMock(
        side_effect=[anthropic.RateLimitError("rate limited", response=MagicMock(), body={}), success]
    )
    with patch("ai.parser._get_client") as mock_client_fn, \
         patch("ai.parser.asyncio.sleep", new_callable=AsyncMock):   # skip real sleep
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result.get("parsed_summary") is not None
    assert mock_create.call_count == 2


@pytest.mark.asyncio
async def test_parse_job_rate_limit_exhausted_returns_empty():
    """Persistent 429 across all attempts should return {}."""
    mock_create = AsyncMock(
        side_effect=anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
    )
    with patch("ai.parser._get_client") as mock_client_fn, \
         patch("ai.parser.asyncio.sleep", new_callable=AsyncMock):
        mock_client_fn.return_value.messages.create = mock_create
        result = await parse_job("SWE", "Co", "Description " * 10)

    assert result == {}
    assert mock_create.call_count == 4   # _RETRY_ATTEMPTS
