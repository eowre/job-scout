"""
test_scraper.py — Unit tests for services/scraper.py pure functions.

No network calls, no Playwright, no browser — all logic is tested in isolation.
"""
import hashlib
from datetime import datetime

import pytest

import services.scraper as scraper
from services.scraper import (
    _absolute,
    _company_slug,
    _detect_ats,
    _find_open_roles_link,
    _html_to_text,
    _is_ats_board,
    _matches,
    _parse_iso,
    _parse_ms,
    _parse_raw_links,
    _stable_id,
    ATS_ASHBY,
    ATS_GENERIC,
    ATS_GREENHOUSE,
    ATS_LEVER,
    _BOT_BLOCKED_CODES,
)


# ---------------------------------------------------------------------------
# _matches — keyword filtering
# ---------------------------------------------------------------------------

class TestMatches:
    def setup_method(self):
        """Give every test a clean, known keyword set."""
        self._orig_include = scraper.JOB_KEYWORDS[:]
        self._orig_exclude = scraper.EXCLUDE_KEYWORDS[:]
        scraper.JOB_KEYWORDS     = ["software engineer", "solutions engineer", "devops"]
        scraper.EXCLUDE_KEYWORDS = ["intern", "director"]

    def teardown_method(self):
        scraper.JOB_KEYWORDS     = self._orig_include
        scraper.EXCLUDE_KEYWORDS = self._orig_exclude

    def test_exact_include(self):
        assert _matches("Software Engineer") is True

    def test_include_case_insensitive(self):
        assert _matches("DEVOPS Engineer") is True

    def test_include_substring(self):
        assert _matches("Senior Software Engineer II") is True

    def test_no_match(self):
        assert _matches("Marketing Manager") is False

    def test_exclude_overrides_include(self):
        assert _matches("Software Engineer Intern") is False

    def test_exclude_case_insensitive(self):
        assert _matches("Software Engineer - Director") is False

    def test_empty_title(self):
        assert _matches("") is False

    def test_multi_word_keyword(self):
        assert _matches("Solutions Engineer, West Coast") is True

    def test_no_include_keywords(self):
        scraper.JOB_KEYWORDS = []
        assert _matches("Software Engineer") is False

    def test_no_exclude_keywords(self):
        scraper.EXCLUDE_KEYWORDS = []
        assert _matches("Software Engineer Intern") is True


# ---------------------------------------------------------------------------
# _detect_ats — URL pattern detection
# ---------------------------------------------------------------------------

class TestDetectAts:
    # Greenhouse
    def test_greenhouse_new_domain(self):
        url = "https://job-boards.greenhouse.io/figma/jobs/5551686004"
        ats, co_id, job_id = _detect_ats(url)
        assert ats == ATS_GREENHOUSE
        assert co_id == "figma"
        assert job_id == "5551686004"

    def test_greenhouse_old_domain(self):
        url = "https://boards.greenhouse.io/stripe/jobs/1234567"
        ats, co_id, job_id = _detect_ats(url)
        assert ats == ATS_GREENHOUSE
        assert co_id == "stripe"
        assert job_id == "1234567"

    # Lever
    def test_lever(self):
        url = "https://jobs.lever.co/notion/abc-123-def"
        ats, co_id, job_id = _detect_ats(url)
        assert ats == ATS_LEVER
        assert co_id == "notion"
        assert job_id == "abc-123-def"

    # Ashby
    def test_ashby(self):
        url = "https://jobs.ashbyhq.com/linear/some-uuid-here"
        ats, co_id, job_id = _detect_ats(url)
        assert ats == ATS_ASHBY
        assert co_id == "linear"
        assert job_id == "some-uuid-here"

    # Generic
    def test_generic_internal(self):
        url = "https://careers.acme.com/jobs/123"
        ats, co_id, job_id = _detect_ats(url)
        assert ats == ATS_GENERIC
        assert co_id == ""
        assert job_id == ""

    def test_generic_empty(self):
        ats, _, _ = _detect_ats("")
        assert ats == ATS_GENERIC

    def test_generic_unrelated_domain(self):
        ats, _, _ = _detect_ats("https://example.com/careers")
        assert ats == ATS_GENERIC


# ---------------------------------------------------------------------------
# _stable_id — MD5-based job ID
# ---------------------------------------------------------------------------

class TestStableId:
    def test_format(self):
        result = _stable_id("acme_corp", "https://example.com/job/1")
        assert result.startswith("web_acme_corp_")
        assert len(result) == len("web_acme_corp_") + 12

    def test_deterministic(self):
        url = "https://example.com/job/42"
        assert _stable_id("co", url) == _stable_id("co", url)

    def test_different_urls_differ(self):
        assert _stable_id("co", "https://x.com/1") != _stable_id("co", "https://x.com/2")

    def test_different_slugs_differ(self):
        url = "https://x.com/1"
        assert _stable_id("co_a", url) != _stable_id("co_b", url)

    def test_uses_md5(self):
        url = "https://example.com/job/1"
        expected_digest = hashlib.md5(url.encode()).hexdigest()[:12]
        assert _stable_id("co", url).endswith(expected_digest)


# ---------------------------------------------------------------------------
# _company_slug
# ---------------------------------------------------------------------------

class TestCompanySlug:
    def test_lowercase(self):
        assert _company_slug("Acme Corp") == "acme_corp"

    def test_hyphens_to_underscores(self):
        assert _company_slug("My-Company") == "my_company"

    def test_spaces_to_underscores(self):
        assert _company_slug("Big Tech Co") == "big_tech_co"

    def test_already_clean(self):
        assert _company_slug("stripe") == "stripe"


# ---------------------------------------------------------------------------
# _absolute — URL resolution
# ---------------------------------------------------------------------------

class TestAbsolute:
    def test_already_absolute(self):
        assert _absolute("https://x.com/job", "https://base.com") == "https://x.com/job"

    def test_protocol_relative(self):
        result = _absolute("//cdn.x.com/img.png", "https://base.com")
        assert result == "https://cdn.x.com/img.png"

    def test_root_relative(self):
        result = _absolute("/jobs/123", "https://acme.com/careers")
        assert result == "https://acme.com/jobs/123"

    def test_relative(self):
        result = _absolute("jobs/123", "https://acme.com/careers/")
        assert result == "https://acme.com/careers/jobs/123"


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_valid_utc(self):
        dt = _parse_iso("2024-03-15T10:30:00Z")
        assert isinstance(dt, datetime)
        assert dt.year == 2024 and dt.month == 3 and dt.day == 15

    def test_valid_offset(self):
        dt = _parse_iso("2024-06-01T00:00:00+05:00")
        assert isinstance(dt, datetime)

    def test_empty_string(self):
        assert _parse_iso("") is None

    def test_none(self):
        assert _parse_iso(None) is None

    def test_invalid(self):
        assert _parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# _parse_ms
# ---------------------------------------------------------------------------

class TestParseMs:
    def test_valid(self):
        dt = _parse_ms(1_700_000_000_000)
        assert isinstance(dt, datetime)

    def test_string_input(self):
        dt = _parse_ms("1700000000000")
        assert isinstance(dt, datetime)

    def test_invalid(self):
        assert _parse_ms("abc") is None

    def test_none(self):
        assert _parse_ms(None) is None


# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------

class TestHtmlToText:
    def test_strips_tags(self):
        html = "<p>Hello <strong>world</strong></p>"
        assert "Hello" in _html_to_text(html)
        assert "<" not in _html_to_text(html)

    def test_removes_nav(self):
        html = "<nav>Menu</nav><main>Job content here and more text to pass length check.</main>"
        result = _html_to_text(html)
        assert "Menu" not in result
        assert "Job content" in result

    def test_prefers_main(self):
        html = "<header>Ignore</header><main>" + "Job description. " * 20 + "</main>"
        result = _html_to_text(html)
        assert "Ignore" not in result
        assert "Job description" in result

    def test_empty_string(self):
        assert _html_to_text("") == ""

    def test_truncates_at_5000(self):
        html = "<p>" + "x" * 10_000 + "</p>"
        assert len(_html_to_text(html)) <= 5000


# ---------------------------------------------------------------------------
# _BOT_BLOCKED_CODES
# ---------------------------------------------------------------------------

class TestBotBlockedCodes:
    def test_contains_403(self):
        assert 403 in _BOT_BLOCKED_CODES

    def test_contains_429(self):
        assert 429 in _BOT_BLOCKED_CODES

    def test_200_not_blocked(self):
        assert 200 not in _BOT_BLOCKED_CODES

    def test_404_not_blocked(self):
        # 404 is a real error, not bot-detection
        assert 404 not in _BOT_BLOCKED_CODES


# ---------------------------------------------------------------------------
# _parse_raw_links
# ---------------------------------------------------------------------------

class TestParseRawLinks:
    def test_basic(self):
        raw = [{"text": "Software Engineer", "href": "https://example.com/jobs/1"}]
        pairs = _parse_raw_links(raw, "https://example.com/careers")
        assert pairs == [("Software Engineer", "https://example.com/jobs/1")]

    def test_deduplicates(self):
        raw = [
            {"text": "SWE", "href": "https://example.com/jobs/1"},
            {"text": "SWE duplicate", "href": "https://example.com/jobs/1"},
        ]
        pairs = _parse_raw_links(raw, "https://example.com")
        assert len(pairs) == 1

    def test_skips_mailto(self):
        raw = [{"text": "Email us", "href": "mailto:jobs@example.com"}]
        assert _parse_raw_links(raw, "https://example.com") == []

    def test_skips_empty_text(self):
        raw = [{"text": "", "href": "https://example.com/jobs/1"}]
        assert _parse_raw_links(raw, "https://example.com") == []

    def test_skips_empty_href(self):
        raw = [{"text": "SWE", "href": ""}]
        assert _parse_raw_links(raw, "https://example.com") == []

    def test_resolves_relative_href(self):
        raw = [{"text": "SWE", "href": "/jobs/42"}]
        pairs = _parse_raw_links(raw, "https://example.com/careers")
        assert pairs[0][1] == "https://example.com/jobs/42"


# ---------------------------------------------------------------------------
# _find_open_roles_link
# ---------------------------------------------------------------------------

class TestFindOpenRolesLink:
    BASE = "https://acme.com/careers"

    def _pairs(self, *items):
        return list(items)

    def test_finds_open_roles(self):
        pairs = self._pairs(("Open Roles", "https://acme.com/careers/jobs"))
        assert _find_open_roles_link(pairs, self.BASE) == "https://acme.com/careers/jobs"

    def test_finds_all_jobs(self):
        pairs = self._pairs(("All Jobs", "https://acme.com/jobs"))
        assert _find_open_roles_link(pairs, self.BASE) == "https://acme.com/jobs"

    def test_finds_view_all_openings(self):
        pairs = self._pairs(("View All Openings", "https://acme.com/openings"))
        assert _find_open_roles_link(pairs, self.BASE) is not None

    def test_finds_current_openings(self):
        pairs = self._pairs(("Current Openings", "https://acme.com/now"))
        assert _find_open_roles_link(pairs, self.BASE) is not None

    def test_accepts_ats_host(self):
        pairs = self._pairs(("Open Roles", "https://boards.greenhouse.io/acme"))
        assert _find_open_roles_link(pairs, self.BASE) == "https://boards.greenhouse.io/acme"

    def test_rejects_external_non_ats(self):
        pairs = self._pairs(("Open Roles", "https://linkedin.com/company/acme/jobs"))
        assert _find_open_roles_link(pairs, self.BASE) is None

    def test_skips_same_page(self):
        # URL is the same as base — already on this page, don't loop
        pairs = self._pairs(("Open Roles", "https://acme.com/careers"))
        assert _find_open_roles_link(pairs, self.BASE) is None

    def test_skips_same_page_trailing_slash(self):
        pairs = self._pairs(("Open Roles", "https://acme.com/careers/"))
        assert _find_open_roles_link(pairs, self.BASE) is None

    def test_no_match_returns_none(self):
        pairs = self._pairs(("Home", "https://acme.com/"), ("Blog", "https://acme.com/blog"))
        assert _find_open_roles_link(pairs, self.BASE) is None

    def test_case_insensitive(self):
        pairs = self._pairs(("VIEW ALL JOBS", "https://acme.com/jobs"))
        assert _find_open_roles_link(pairs, self.BASE) is not None


# ---------------------------------------------------------------------------
# _is_ats_board
# ---------------------------------------------------------------------------

class TestIsAtsBoard:
    # --- Greenhouse ---
    def test_gh_board_is_board(self):
        assert _is_ats_board("https://boards.greenhouse.io/acme") is True

    def test_gh_board_trailing_slash(self):
        assert _is_ats_board("https://boards.greenhouse.io/acme/") is True

    def test_gh_board_with_query(self):
        # department filter is still a board page, not a job
        assert _is_ats_board("https://boards.greenhouse.io/acme?department=Engineering") is True

    def test_gh_individual_job_is_not_board(self):
        assert _is_ats_board("https://boards.greenhouse.io/acme/jobs/12345") is False

    def test_gh_new_domain_board(self):
        assert _is_ats_board("https://job-boards.greenhouse.io/acme") is True

    def test_gh_new_domain_individual_job(self):
        assert _is_ats_board("https://job-boards.greenhouse.io/acme/jobs/99999") is False

    # --- Lever ---
    def test_lv_board_is_board(self):
        assert _is_ats_board("https://jobs.lever.co/acme") is True

    def test_lv_individual_job_is_not_board(self):
        assert _is_ats_board("https://jobs.lever.co/acme/some-uuid-1234") is False

    # --- Ashby ---
    def test_ash_board_is_board(self):
        assert _is_ats_board("https://jobs.ashbyhq.com/pylon-labs") is True

    def test_ash_individual_job_is_not_board(self):
        assert _is_ats_board("https://jobs.ashbyhq.com/pylon-labs/some-uuid") is False

    # --- Non-ATS ---
    def test_non_ats_is_not_board(self):
        assert _is_ats_board("https://acme.com/careers") is False

    def test_empty_string(self):
        assert _is_ats_board("") is False

    def test_linkedin_is_not_board(self):
        assert _is_ats_board("https://linkedin.com/company/acme/jobs") is False
