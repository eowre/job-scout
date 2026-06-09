"""
test_parser.py — Unit tests for ai/parser.py.

Both backends are tested by patching the low-level call functions
(_call_ollama and _call_anthropic) — no real network calls are made.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import anthropic
import pytest

from ai.parser import _build_prompt, _extract_json, parse_job


# ---------------------------------------------------------------------------
# _extract_json — strip markdown fences / leading prose
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"key": "value"}') == '{"key": "value"}'

    def test_strips_json_fence(self):
        assert _extract_json('```json\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_strips_plain_fence(self):
        assert _extract_json('```\n{"key": "value"}\n```') == '{"key": "value"}'

    def test_whitespace_trimmed(self):
        assert _extract_json('  \n  {"key": "value"}  \n  ').strip() == '{"key": "value"}'

    def test_empty_string(self):
        assert _extract_json("") == ""

    def test_leading_prose_skipped(self):
        assert _extract_json('Sure! Here is the JSON:\n{"key": "value"}') == '{"key": "value"}'


# ---------------------------------------------------------------------------
# _build_prompt — placeholder substitution
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_title_substituted(self):
        p = _build_prompt("Software Engineer", "Acme", "desc")
        assert "Software Engineer" in p and "TITLE_PLACEHOLDER" not in p

    def test_company_substituted(self):
        p = _build_prompt("SWE", "Acme Corp", "desc")
        assert "Acme Corp" in p and "COMPANY_PLACEHOLDER" not in p

    def test_description_substituted(self):
        p = _build_prompt("SWE", "Acme", "We are looking for engineers.")
        assert "We are looking for engineers." in p and "DESCRIPTION_PLACEHOLDER" not in p

    def test_description_truncated_at_4000(self):
        p = _build_prompt("SWE", "Acme", "x" * 5000)
        assert "x" * 4001 not in p

    def test_curly_braces_in_description_dont_raise(self):
        p = _build_prompt("SWE", "Acme", "Requirements: {Python, Go}")
        assert "{Python, Go}" in p


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_RESPONSE = json.dumps({
    "parsed_summary": "A great role at a great company.",
    "compensation": "$120k–$160k",
    "responsibilities": ["Write code", "Review PRs"],
    "years_of_experience": 3,
})

_LONG_DESC = "We are looking for an experienced engineer. " * 10


# ---------------------------------------------------------------------------
# parse_job — Ollama backend
# ---------------------------------------------------------------------------

class TestParseJobOllama:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.return_value = VALID_RESPONSE
            result = await parse_job("Software Engineer", "Acme", _LONG_DESC)

        assert result["parsed_summary"] == "A great role at a great company."
        assert result["compensation"] == "$120k–$160k"
        assert result["years_of_experience"] == 3
        assert isinstance(result["responsibilities"], list)

    @pytest.mark.asyncio
    async def test_fenced_json_response(self):
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.return_value = f"```json\n{VALID_RESPONSE}\n```"
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result.get("parsed_summary") is not None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.return_value = "not valid json {{{"
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_connection_error_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.side_effect = aiohttp.ClientConnectorError(
                connection_key=None, os_error=OSError("Connection refused")
            )
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.side_effect = ValueError("Ollama HTTP 500: internal error")
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_null_yoe(self):
        data = {**json.loads(VALID_RESPONSE), "years_of_experience": None}
        with patch.dict("ai.parser._config", {"parser_backend": "ollama"}), \
             patch("ai.parser._call_ollama", new_callable=AsyncMock) as mock:
            mock.return_value = json.dumps(data)
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result["years_of_experience"] is None


# ---------------------------------------------------------------------------
# parse_job — Anthropic backend
# ---------------------------------------------------------------------------

class TestParseJobAnthropic:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = VALID_RESPONSE
            result = await parse_job("Software Engineer", "Acme", _LONG_DESC)

        assert result["parsed_summary"] == "A great role at a great company."
        assert result["compensation"] == "$120k–$160k"
        assert result["years_of_experience"] == 3

    @pytest.mark.asyncio
    async def test_fenced_json_response(self):
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = f"```json\n{VALID_RESPONSE}\n```"
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result.get("parsed_summary") is not None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = "not valid json {{{"
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.side_effect = anthropic.RateLimitError(
                "rate limited", response=MagicMock(), body={}
            )
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_api_exception_returns_empty(self):
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("API error")
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result == {}

    @pytest.mark.asyncio
    async def test_null_yoe(self):
        data = {**json.loads(VALID_RESPONSE), "years_of_experience": None}
        with patch.dict("ai.parser._config", {"parser_backend": "anthropic"}), \
             patch("ai.parser._call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = json.dumps(data)
            result = await parse_job("SWE", "Co", _LONG_DESC)

        assert result["years_of_experience"] is None


# ---------------------------------------------------------------------------
# Backend-agnostic edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_description_returns_empty():
    result = await parse_job("SWE", "Co", "")
    assert result == {}


@pytest.mark.asyncio
async def test_short_description_returns_empty():
    result = await parse_job("SWE", "Co", "Too short")
    assert result == {}
