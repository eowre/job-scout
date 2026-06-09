"""
test_parser.py — Unit tests for ai/parser.py.

The Ollama HTTP call is mocked throughout — no real network calls are made.
"""
import json
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from ai.parser import _build_prompt, _extract_json, parse_job


# ---------------------------------------------------------------------------
# _extract_json — strip markdown fences / leading prose
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

    def test_leading_prose_skipped(self):
        """Model sometimes prefixes with a sentence before the JSON."""
        raw = 'Sure! Here is the JSON:\n{"key": "value"}'
        assert _extract_json(raw) == '{"key": "value"}'


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
        assert "x" * 4001 not in prompt

    def test_curly_braces_in_description_dont_raise(self):
        """Curly braces in job descriptions must not break formatting."""
        desc = "Requirements: {Python, Go} or {Java}"
        prompt = _build_prompt("SWE", "Acme", desc)
        assert "{Python, Go}" in prompt


# ---------------------------------------------------------------------------
# parse_job — async function with mocked _call_ollama
# ---------------------------------------------------------------------------

VALID_RESPONSE = json.dumps({
    "parsed_summary": "A great role at a great company.",
    "compensation": "$120k–$160k",
    "responsibilities": ["Write code", "Review PRs"],
    "years_of_experience": 3,
})

_LONG_DESC = "Description with enough content. " * 10


@pytest.mark.asyncio
async def test_parse_job_success():
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = VALID_RESPONSE
        result = await parse_job(
            "Software Engineer", "Acme",
            "We need an engineer with 3+ years. Looking for someone to write code "
            "and review PRs. Compensation is $120k–$160k.",
        )

    assert result["parsed_summary"] == "A great role at a great company."
    assert result["compensation"] == "$120k–$160k"
    assert result["years_of_experience"] == 3
    assert isinstance(result["responsibilities"], list)


@pytest.mark.asyncio
async def test_parse_job_fenced_response():
    fenced = f"```json\n{VALID_RESPONSE}\n```"
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = fenced
        result = await parse_job("SWE", "Co", _LONG_DESC)

    assert result.get("parsed_summary") is not None


@pytest.mark.asyncio
async def test_parse_job_invalid_json_returns_empty():
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "not valid json {{{"
        result = await parse_job("SWE", "Co", _LONG_DESC)

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
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("Unexpected error")
        result = await parse_job("SWE", "Co", _LONG_DESC)

    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_connection_error_returns_empty():
    """Ollama not running — ClientConnectorError should return {} gracefully."""
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError("Connection refused")
        )
        result = await parse_job("SWE", "Co", _LONG_DESC)

    assert result == {}


@pytest.mark.asyncio
async def test_parse_job_null_yoe():
    response_data = {**json.loads(VALID_RESPONSE), "years_of_experience": None}
    with patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = json.dumps(response_data)
        result = await parse_job("SWE", "Co", _LONG_DESC)

    assert result["years_of_experience"] is None
