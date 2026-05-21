"""
scraper.py — Hybrid browser-first + ATS-API scraper.

Flow per company:
  1. Playwright renders company.url (the careers page) to find real job links.
  2. Each matching link is inspected to detect which ATS it belongs to.
  3. ATS-hosted jobs (Greenhouse / Lever / Ashby) are fetched via their
     structured APIs — giving clean title, description, location, department,
     and posted_date without further browser rendering.
  4. Jobs on internal/custom career sites fall back to aiohttp + BeautifulSoup.

URL detection:
  Greenhouse : job-boards.greenhouse.io/{co_id}/jobs/{job_id}
               boards.greenhouse.io/{co_id}/jobs/{job_id}
  Lever      : jobs.lever.co/{co_id}/{job_uuid}
  Ashby      : jobs.ashbyhq.com/{co_id}/{job_id}
  Generic    : everything else

Setup (one-time after install):
    playwright install chromium
"""

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext

from config import (
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT_SECS,
)
import config as _config

# These are module-level variables so the keywords router can hot-patch them
# at runtime without a server restart (importlib.reload + scraper.JOB_KEYWORDS = ...)
JOB_KEYWORDS:     list = list(_config.JOB_KEYWORDS)
EXCLUDE_KEYWORDS: list = list(_config.EXCLUDE_KEYWORDS)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Option D: per-company verbose logging
#
# Set SCOUT_DEBUG_COMPANY="Acme Corp" (case-insensitive) before starting the
# server to get detailed INFO logs for every decision made for that company:
#   SCOUT_DEBUG_COMPANY="Acme Corp" uvicorn main:app --reload
# ---------------------------------------------------------------------------
_DEBUG_COMPANY: str = os.getenv("SCOUT_DEBUG_COMPANY", "").lower().strip()


def _dlog(co_name: str, msg: str) -> None:
    """Emit a verbose log line only when SCOUT_DEBUG_COMPANY matches co_name."""
    if _DEBUG_COMPANY and co_name.lower() == _DEBUG_COMPANY:
        logger.info("[DBG:%s] %s", co_name, msg)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
_BROWSER_SEM_SIZE = 6
_HTTP_SEM_SIZE    = MAX_CONCURRENT_REQUESTS

_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScrapedJob:
    job_id:      str
    company:     str
    title:       str
    url:         str          # real application-page link (from career page)
    description: str = ""
    location:    str = ""
    department:  str = ""
    posted_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _stable_id(company_slug: str, url: str) -> str:
    digest = hashlib.md5(url.encode()).hexdigest()[:12]
    return f"web_{company_slug}_{digest}"

def _company_slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")

def _matches(title: str) -> bool:
    t = title.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in t:
            return False
    return any(kw.lower() in t for kw in JOB_KEYWORDS)

def _absolute(href: str, base: str) -> str:
    if href.startswith("//"):
        return (urlparse(base).scheme or "https") + ":" + href
    if href.startswith("http"):
        return href
    return urljoin(base, href)

def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None

def _parse_ms(ms) -> Optional[datetime]:
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000)
    except Exception:
        return None

def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# ATS URL detection
# ---------------------------------------------------------------------------

# Each pattern returns (ats_type, co_id, job_id) or None
_GH_PATTERN  = re.compile(
    r"https?://(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I
)
_LV_PATTERN  = re.compile(
    r"https?://jobs\.lever\.co/([^/]+)/([A-Za-z0-9\-]+)", re.I
)
_ASH_PATTERN = re.compile(
    r"https?://jobs\.ashbyhq\.com/([^/?#]+)/([A-Za-z0-9\-]+)", re.I
)

ATS_GENERIC    = "generic"
ATS_GREENHOUSE = "greenhouse"
ATS_LEVER      = "lever"
ATS_ASHBY      = "ashby"


def _detect_ats(url: str) -> Tuple[str, str, str]:
    """Return (ats_type, co_id, job_id). Falls back to ('generic', '', '')."""
    m = _GH_PATTERN.match(url)
    if m:
        return ATS_GREENHOUSE, m.group(1), m.group(2)
    m = _LV_PATTERN.match(url)
    if m:
        return ATS_LEVER, m.group(1), m.group(2)
    m = _ASH_PATTERN.match(url)
    if m:
        return ATS_ASHBY, m.group(1), m.group(2)
    return ATS_GENERIC, "", ""


def _is_ats_board(url: str) -> bool:
    """
    Return True if url points to an ATS board/listing page rather than an
    individual job posting.

    An ATS board URL is on a known ATS host but does NOT match any of the
    individual-job patterns (which all require a job-ID path segment).

    Examples:
      boards.greenhouse.io/acme              → True  (board)
      boards.greenhouse.io/acme/jobs/12345   → False (individual job)
      jobs.lever.co/acme                     → True  (board)
      jobs.lever.co/acme/some-uuid           → False (individual job)
      jobs.ashbyhq.com/pylon-labs            → True  (board)
      jobs.ashbyhq.com/pylon-labs/some-uuid  → False (individual job)
    """
    if not url:
        return False
    # If it already matches an individual job pattern it is NOT a board
    if _GH_PATTERN.match(url) or _LV_PATTERN.match(url) or _ASH_PATTERN.match(url):
        return False
    # On a known ATS host but no job-ID → board-level page
    parsed = urlparse(url)
    return any(host in parsed.netloc for host in _ATS_HOSTS)


# ---------------------------------------------------------------------------
# Step 1 — Playwright: render career page, extract matching job links
# ---------------------------------------------------------------------------

_EXTRACT_JS = """
() => {
    const links = [];
    document.querySelectorAll('a[href]').forEach(el => {
        const text = (el.innerText || el.textContent || '').trim();
        const href = el.getAttribute('href') || '';
        if (text && href) links.push({ text, href });
    });
    return links;
}
"""

# Matches navigation links that lead to a dedicated job-listings sub-page.
# Intentionally broad so we catch common phrasings across many company sites.
_OPEN_ROLES_RE = re.compile(
    r"(?:"
    r"open\s+(?:roles?|positions?|jobs?)"
    r"|all\s+(?:open\s+)?(?:jobs?|roles?|positions?|openings?)"
    r"|view\s+(?:all\s+)?(?:jobs?|roles?|positions?|openings?|opportunities)"
    r"|see\s+(?:all\s+)?(?:jobs?|roles?|positions?|openings?)"
    r"|current\s+(?:job\s+)?openings?"
    r"|job\s+openings?"
    r"|(?:explore|browse)\s+(?:jobs?|roles?|opportunities)"
    r")",
    re.I,
)

# ATS hosts that are safe to follow even if they differ from the career-page domain
_ATS_HOSTS = {"greenhouse.io", "lever.co", "ashbyhq.com"}


def _parse_raw_links(raw: list, base_url: str) -> List[Tuple[str, str]]:
    """Convert the raw JS link list into de-duplicated (text, abs_href) pairs."""
    pairs, seen = [], set()
    for item in raw:
        text = (item.get("text") or "").strip()
        href = (item.get("href") or "").strip()
        if not text or not href or href.startswith("mailto:"):
            continue
        abs_href = _absolute(href, base_url)
        if abs_href in seen:
            continue
        seen.add(abs_href)
        pairs.append((text, abs_href))
    return pairs


def _find_open_roles_link(pairs: List[Tuple[str, str]], base_url: str) -> Optional[str]:
    """
    Scan page links for a 'view all jobs / open roles' navigation link.

    Returns the first URL that:
      • has link text matching _OPEN_ROLES_RE
      • is not the same page we're already on
      • is on the same domain OR on a known ATS host
    Returns None if no such link is found.
    """
    base_parsed = urlparse(base_url)
    for text, url in pairs:
        if not _OPEN_ROLES_RE.search(text):
            continue
        parsed = urlparse(url)
        if url.rstrip("/") == base_url.rstrip("/"):
            continue  # already on this page
        same_domain = parsed.netloc == base_parsed.netloc
        is_ats = any(host in parsed.netloc for host in _ATS_HOSTS)
        if same_domain or is_ats:
            return url
    return None


async def _load_and_wait(page, url: str) -> None:
    """Navigate to url and wait for the page to settle."""
    await page.goto(url, wait_until="domcontentloaded",
                    timeout=REQUEST_TIMEOUT_SECS * 1000)
    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except Exception:
        await page.wait_for_timeout(2_500)


async def _get_job_links(
    context: BrowserContext,
    career_url: str,
    co_name: str,
) -> List[Tuple[str, str]]:
    """
    Render career_url and return (title, abs_href) pairs for all anchors.

    Two-step navigation:
      1. Land on the career page.
      2. If an 'open roles / all jobs' navigation link is found, follow it
         first — many career pages are landing pages whose actual job list
         lives one click away.
    """
    page = await context.new_page()
    try:
        await _load_and_wait(page, career_url)

        raw: list = await page.evaluate(_EXTRACT_JS)
        pairs = _parse_raw_links(raw, career_url)

        _dlog(co_name, f"ALL LINKS on career landing page ({len(pairs)} total):")
        for _t, _u in pairs:
            _dlog(co_name, f"  {_t!r:60s}  {_u}")

        # --- Two-step: follow an "open roles" navigation link if present ------
        nav_url = _find_open_roles_link(pairs, career_url)
        if nav_url:
            logger.debug(f"[{co_name}] Open-roles nav link found → {nav_url}")
            _dlog(co_name, f"NAVIGATING to open-roles sub-page: {nav_url}")
            await _load_and_wait(page, nav_url)
            raw = await page.evaluate(_EXTRACT_JS)
            pairs = _parse_raw_links(raw, nav_url)
            _dlog(co_name, f"ALL LINKS on open-roles sub-page ({len(pairs)} total):")
            for _t, _u in pairs:
                _dlog(co_name, f"  {_t!r:60s}  {_u}")
        # ----------------------------------------------------------------------

        logger.debug(f"[{co_name}] {len(pairs)} total links (nav_followed={nav_url is not None})")
        return pairs
    except Exception as exc:
        logger.warning(f"[{co_name}] Page load failed: {exc}")
        return []
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Step 2a — Greenhouse API
# ---------------------------------------------------------------------------

_GH_DESC_ENDPOINTS = [
    "https://job-boards.greenhouse.io/api/v1/boards/{co_id}/jobs/{job_id}",
    "https://boards-api.greenhouse.io/v1/boards/{co_id}/jobs/{job_id}",
]

async def _fetch_greenhouse_job(
    session: aiohttp.ClientSession,
    co_id: str,
    job_id: str,
    title: str,
    url: str,
    co_name: str,
    co_slug: str,
    http_sem: asyncio.Semaphore,
) -> Optional[ScrapedJob]:
    async with http_sem:
        for tmpl in _GH_DESC_ENDPOINTS:
            api_url = tmpl.format(co_id=co_id, job_id=job_id)
            _dlog(co_name, f"  GH API  GET {api_url}")
            try:
                async with session.get(api_url, timeout=_TIMEOUT) as r:
                    _dlog(co_name, f"  GH API  → HTTP {r.status}")
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                    _dlog(co_name, f"  GH API  title={data.get('title')!r}  location={data.get('location', {}).get('name')!r}  desc_len={len(data.get('content') or '')}")
                    return ScrapedJob(
                        job_id      = _stable_id(co_slug, url),
                        company     = co_name,
                        title       = data.get("title", title),
                        url         = url,
                        description = _strip_html(data.get("content", "")),
                        location    = (data.get("location") or {}).get("name", ""),
                        department  = (data.get("departments") or [{}])[0].get("name", ""),
                        posted_date = _parse_iso(data.get("updated_at", "")),
                    )
            except Exception:
                continue
    # API failed — fall back to title-only entry so the job isn't lost
    return ScrapedJob(job_id=_stable_id(co_slug, url), company=co_name,
                      title=title, url=url)


# ---------------------------------------------------------------------------
# Step 2b — Lever API
# ---------------------------------------------------------------------------

async def _fetch_lever_job(
    session: aiohttp.ClientSession,
    co_id: str,
    job_id: str,
    title: str,
    url: str,
    co_name: str,
    co_slug: str,
    http_sem: asyncio.Semaphore,
) -> Optional[ScrapedJob]:
    api_url = f"https://api.lever.co/v0/postings/{co_id}/{job_id}?mode=json"
    _dlog(co_name, f"  LV API  GET {api_url}")
    async with http_sem:
        try:
            async with session.get(api_url, timeout=_TIMEOUT) as r:
                _dlog(co_name, f"  LV API  → HTTP {r.status}")
                if r.status != 200:
                    raise ValueError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
                _dlog(co_name, f"  LV API  title={data.get('text')!r}  desc_len={len(data.get('descriptionPlain') or '')}")

            desc = data.get("descriptionPlain", "").strip()
            if not desc:
                lists = data.get("lists", [])
                desc = "\n\n".join(
                    f"{s['text']}\n{_strip_html(s.get('content',''))}"
                    for s in lists
                ).strip()
            cats = data.get("categories", {})
            return ScrapedJob(
                job_id      = _stable_id(co_slug, url),
                company     = co_name,
                title       = data.get("text", title),
                url         = data.get("hostedUrl", url),
                description = desc,
                location    = cats.get("location", ""),
                department  = cats.get("team", ""),
                posted_date = _parse_ms(data.get("createdAt")),
            )
        except Exception:
            return ScrapedJob(job_id=_stable_id(co_slug, url), company=co_name,
                              title=title, url=url)


# ---------------------------------------------------------------------------
# Step 2c — Ashby GraphQL (batch: fetch whole board, filter to found IDs)
# ---------------------------------------------------------------------------

_ASHBY_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings {
      id title locationName departmentName isRemote
      externalLink descriptionHtml publishedDate
    }
  }
}
"""

async def _fetch_ashby_jobs(
    session: aiohttp.ClientSession,
    co_id: str,
    wanted_ids: set,          # job IDs discovered on the career page
    wanted_urls: Dict[str, Tuple[str, str]],  # job_id -> (title, url)
    co_name: str,
    co_slug: str,
    http_sem: asyncio.Semaphore,
) -> List[ScrapedJob]:
    """Fetch the full Ashby board and return only jobs whose ID is in wanted_ids."""
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": co_id},
        "query": _ASHBY_QUERY,
    }
    api_url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    _dlog(co_name, f"  ASH API POST {api_url}  org={co_id!r}  wanted_ids={wanted_ids}")
    async with http_sem:
        try:
            async with session.post(api_url, json=payload, timeout=_TIMEOUT) as r:
                _dlog(co_name, f"  ASH API → HTTP {r.status}")
                if r.status != 200:
                    raise ValueError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except Exception as exc:
            logger.warning(f"[{co_name}] Ashby API error: {exc}")
            # Fall back: return minimal ScrapedJobs from the Playwright links
            return [
                ScrapedJob(job_id=_stable_id(co_slug, url), company=co_name,
                           title=title, url=url)
                for _, (title, url) in wanted_urls.items()
            ]

    postings = (data.get("data") or {}).get("jobBoard", {}).get("jobPostings", [])
    _dlog(co_name, f"  ASH API → {len(postings)} total postings in board, filtering to {len(wanted_ids)} wanted")
    jobs = []
    for j in postings:
        jid = j.get("id", "")
        if jid not in wanted_ids:
            continue
        _dlog(co_name, f"  ASH API   matched posting id={jid!r}  title={j.get('title')!r}")
        title, url = wanted_urls.get(jid, (j.get("title", ""), ""))
        if not url:
            url = j.get("externalLink") or f"https://jobs.ashbyhq.com/{co_id}/{jid}"
        loc = j.get("locationName", "")
        if not loc and j.get("isRemote"):
            loc = "Remote"
        jobs.append(ScrapedJob(
            job_id      = _stable_id(co_slug, url),
            company     = co_name,
            title       = j.get("title", title),
            url         = url,
            description = _strip_html(j.get("descriptionHtml", "")),
            location    = loc,
            department  = j.get("departmentName", ""),
            posted_date = _parse_iso(j.get("publishedDate", "")),
        ))
    return jobs


# ---------------------------------------------------------------------------
# Bot-detection codes — trigger automatic Playwright fallback everywhere
# ---------------------------------------------------------------------------

# Shared with routers/scrape.py (imported there as `from services.scraper import _BOT_BLOCKED_CODES`)
_BOT_BLOCKED_CODES: set = {403, 406, 429, 503}


# ---------------------------------------------------------------------------
# Shared HTML → text extractor (used by generic path + browser fallback)
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    """Strip boilerplate tags and extract up to 5000 chars of meaningful text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "noscript", "iframe"]):
        tag.decompose()
    for selector in ["main", "article", '[role="main"]',
                     ".job-description", "#job-description",
                     ".description", "#description", ".content"]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 200:
                return text[:5000]
    return soup.get_text(" ", strip=True)[:5000]


# ---------------------------------------------------------------------------
# Step 2d — Generic fallback: aiohttp first, Playwright if bot-blocked
# ---------------------------------------------------------------------------

async def _render_page_html(
    browser: Browser,
    url: str,
    browser_sem: asyncio.Semaphore,
) -> str:
    """Render a URL in a headless browser and return raw HTML."""
    async with browser_sem:
        context = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=REQUEST_TIMEOUT_SECS * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                await page.wait_for_timeout(1_500)
            return await page.content()
        except Exception:
            return ""
        finally:
            await page.close()
            await context.close()


async def _fetch_generic_description(
    session: aiohttp.ClientSession,
    browser: Browser,
    url: str,
    http_sem: asyncio.Semaphore,
    browser_sem: asyncio.Semaphore,
) -> str:
    """
    Fetch a job page description.
    1. Try a fast aiohttp GET.
    2. If the server returns a bot-block code, transparently retry with
       a real Playwright browser (same fallback used in validation).
    """
    html: Optional[str] = None

    async with http_sem:
        try:
            async with session.get(url, timeout=_TIMEOUT,
                                   allow_redirects=True) as r:
                if r.status in _BOT_BLOCKED_CODES:
                    html = None  # signal: need browser
                    logger.debug(f"Bot-blocked ({r.status}) on {url} — retrying with browser")
                elif r.status == 200:
                    html = await r.text(errors="replace")
        except Exception:
            pass

    # Bot-blocked → fall back to Playwright
    if html is None:
        html = await _render_page_html(browser, url, browser_sem)

    return _html_to_text(html) if html else ""


# ---------------------------------------------------------------------------
# Step 3 — Orchestrate one company
# ---------------------------------------------------------------------------

async def _scrape_company(
    browser: Browser,
    session: aiohttp.ClientSession,
    company: dict,
    browser_sem: asyncio.Semaphore,
    http_sem: asyncio.Semaphore,
) -> List[ScrapedJob]:
    co_name    = company["name"]
    career_url = company.get("url", "").strip()
    co_slug    = _company_slug(co_name)

    if not career_url:
        logger.debug(f"[{co_name}] No URL configured — skipped")
        return []

    # --- Step 1: render career page ---
    async with browser_sem:
        context = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        try:
            all_links = await _get_job_links(context, career_url, co_name)
        finally:
            await context.close()

    # Filter by keyword match
    matching = [(title, url) for title, url in all_links if _matches(title)]

    if _DEBUG_COMPANY and co_name.lower() == _DEBUG_COMPANY:
        _dlog(co_name, f"KEYWORD FILTER  include={JOB_KEYWORDS}  exclude={EXCLUDE_KEYWORDS}")
        for _t, _u in all_links:
            if _matches(_t):
                _dlog(co_name, f"  MATCH  {_t!r}")
            else:
                _excl = next((k for k in EXCLUDE_KEYWORDS if k.lower() in _t.lower()), None)
                _incl = next((k for k in JOB_KEYWORDS if k.lower() in _t.lower()), None)
                reason = f"excluded by {_excl!r}" if _excl else ("no include match" if not _incl else f"excluded by {_excl!r}")
                _dlog(co_name, f"  SKIP   {_t!r}  ({reason})")
        _dlog(co_name, f"  → {len(matching)} matched out of {len(all_links)} links")

    # --- Step 1.5: ATS board expansion ---
    # Career pages sometimes link to an ATS board homepage rather than individual
    # job pages — either because the link text matched a keyword (e.g. "Engineering
    # Roles" → boards.greenhouse.io/acme) or because the board link text didn't
    # match any keyword at all.  Detect every board-level ATS URL in all_links,
    # navigate into it, collect the real job-level links, and add any that pass
    # keyword filtering to `matching`.
    board_urls = {u for _, u in all_links if _is_ats_board(u)}
    if board_urls:
        seen_job_urls = {u for _, u in matching}
        _dlog(co_name, f"BOARD EXPANSION: {len(board_urls)} board-level ATS URL(s) in page links")

        for board_url in board_urls:
            logger.debug(f"[{co_name}] Board-level ATS URL detected → {board_url}")
            _dlog(co_name, f"  navigating into board: {board_url}")

            async with browser_sem:
                ctx = await browser.new_context(
                    user_agent=_HEADERS["User-Agent"],
                    java_script_enabled=True,
                    ignore_https_errors=True,
                )
                try:
                    board_job_links = await _get_job_links(ctx, board_url, co_name)
                finally:
                    await ctx.close()

            # Drop the board URL from matching (replace it with real job URLs)
            matching = [(t, u) for t, u in matching if u != board_url]

            # Add any keyword-matching individual job links not already seen
            new_matches = [
                (t, u) for t, u in board_job_links
                if _matches(t) and u not in seen_job_urls
            ]
            matching.extend(new_matches)
            seen_job_urls.update(u for _, u in new_matches)

            _dlog(co_name, f"  → {len(board_job_links)} links on board, {len(new_matches)} new keyword match(es)")
            if new_matches:
                logger.info(f"[{co_name}] Board expansion added {len(new_matches)} job(s) from {board_url}")

    if not matching:
        logger.debug(f"[{co_name}] No matching job titles found")
        return []

    logger.info(f"[{co_name}] {len(matching)} matching job(s) found — detecting ATS…")

    # --- Step 2: group by detected ATS ---
    gh_jobs:  List[Tuple[str, str, str, str]] = []  # (co_id, job_id, title, url)
    lv_jobs:  List[Tuple[str, str, str, str]] = []
    ash_jobs: Dict[str, Dict] = {}  # co_id -> {job_id: (title, url)}
    gen_jobs: List[Tuple[str, str]] = []             # (title, url)

    _dlog(co_name, "ATS DETECTION per matching link:")
    for title, url in matching:
        ats_type, co_id, job_id = _detect_ats(url)
        _dlog(co_name, f"  [{ats_type:<10s}] co_id={co_id!r:<22s} job_id={job_id!r:<38s} title={title!r}")
        if ats_type == ATS_GREENHOUSE:
            gh_jobs.append((co_id, job_id, title, url))
        elif ats_type == ATS_LEVER:
            lv_jobs.append((co_id, job_id, title, url))
        elif ats_type == ATS_ASHBY:
            if co_id not in ash_jobs:
                ash_jobs[co_id] = {}
            ash_jobs[co_id][job_id] = (title, url)
        else:
            gen_jobs.append((title, url))

    ats_counts = []
    if gh_jobs:  ats_counts.append(f"{len(gh_jobs)} greenhouse")
    if lv_jobs:  ats_counts.append(f"{len(lv_jobs)} lever")
    if ash_jobs: ats_counts.append(f"{sum(len(v) for v in ash_jobs.values())} ashby")
    if gen_jobs: ats_counts.append(f"{len(gen_jobs)} generic")
    logger.info(f"[{co_name}] → {', '.join(ats_counts)}")

    # --- Step 3: fetch via appropriate method ---
    tasks = []

    for co_id, job_id, title, url in gh_jobs:
        tasks.append(_fetch_greenhouse_job(
            session, co_id, job_id, title, url, co_name, co_slug, http_sem))

    for co_id, job_id, title, url in lv_jobs:
        tasks.append(_fetch_lever_job(
            session, co_id, job_id, title, url, co_name, co_slug, http_sem))

    for co_id, id_map in ash_jobs.items():
        wanted_ids = set(id_map.keys())
        wanted_urls = id_map  # job_id -> (title, url)
        tasks.append(_fetch_ashby_jobs(
            session, co_id, wanted_ids, wanted_urls, co_name, co_slug, http_sem))

    for title, url in gen_jobs:
        async def _generic_job(t=title, u=url):
            desc = await _fetch_generic_description(
                session, browser, u, http_sem, browser_sem)
            return ScrapedJob(
                job_id=_stable_id(co_slug, u), company=co_name,
                title=t, url=u, description=desc)
        tasks.append(_generic_job())

    results = await asyncio.gather(*tasks, return_exceptions=True)

    jobs: List[ScrapedJob] = []
    for r in results:
        if isinstance(r, list):
            jobs.extend(r)
        elif isinstance(r, ScrapedJob):
            jobs.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"[{co_name}] fetch error: {r}")

    return jobs


# ---------------------------------------------------------------------------
# Core async implementation
# ---------------------------------------------------------------------------

async def _scrape_all_async(companies: list) -> List[ScrapedJob]:
    enabled = [c for c in companies if c.get("enabled", True)]
    logger.info(f"Hybrid scrape starting — {len(enabled)} companies")

    browser_sem = asyncio.Semaphore(_BROWSER_SEM_SIZE)
    http_sem    = asyncio.Semaphore(_HTTP_SEM_SIZE)
    connector   = aiohttp.TCPConnector(ssl=False, limit=_HTTP_SEM_SIZE)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            async with aiohttp.ClientSession(connector=connector,
                                             headers=_HEADERS) as session:
                tasks   = [_scrape_company(browser, session, c,
                                           browser_sem, http_sem)
                           for c in enabled]
                results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await browser.close()

    all_jobs: List[ScrapedJob] = []
    for company, result in zip(enabled, results):
        if isinstance(result, list):
            all_jobs.extend(result)
        else:
            logger.error(f"[{company['name']}] Unhandled: {result}")

    logger.info(f"Scrape complete — {len(all_jobs)} keyword-matching job(s) total")
    return all_jobs


def _scrape_all_sync(companies: list) -> List[ScrapedJob]:
    """Windows shim: run in a thread with ProactorEventLoop for subprocess support."""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_scrape_all_async(companies))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Public: browser-based URL reachability check (used by validation)
# ---------------------------------------------------------------------------

# HTTP codes that suggest bot-detection rather than a real error
_BOT_BLOCKED_CODES = {403, 406, 429, 503}


async def _browser_check_async(urls: List[str]) -> Dict[str, dict]:
    """
    Load each URL in a headless browser and return a result dict per URL:
      { url: { "status": int, "ok": bool, "error": str|None } }
    """
    sem = asyncio.Semaphore(4)  # lighter than full scrape

    async def _check_one(browser: Browser, url: str) -> Tuple[str, dict]:
        async with sem:
            context = await browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                ignore_https_errors=True,
            )
            page = await context.new_page()
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=20_000
                )
                code = resp.status if resp else 0
                return url, {"status": code, "ok": code < 400, "error": None}
            except Exception as exc:
                return url, {"status": 0, "ok": False, "error": str(exc)[:120]}
            finally:
                await page.close()
                await context.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            tasks = [_check_one(browser, u) for u in urls]
            pairs = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await browser.close()

    result: Dict[str, dict] = {}
    for item in pairs:
        if isinstance(item, tuple):
            url, data = item
            result[url] = data
    return result


def _browser_check_sync(urls: List[str]) -> Dict[str, dict]:
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_browser_check_async(urls))
    finally:
        loop.close()


async def browser_check_urls(urls: List[str]) -> Dict[str, dict]:
    """
    Public entry point — check a list of URLs with a real Chromium browser.
    Handles the Windows ProactorEventLoop requirement automatically.
    Returns { url: { "status": int, "ok": bool, "error": str|None } }
    """
    if not urls:
        return {}
    if sys.platform == "win32":
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return await loop.run_in_executor(pool, _browser_check_sync, urls)
    return await _browser_check_async(urls)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def scrape_all(companies: list) -> List[ScrapedJob]:
    """
    Scrape all enabled companies using the hybrid Playwright + ATS API strategy.
    On Windows, delegates to a ProactorEventLoop thread to support subprocesses.
    """
    if sys.platform == "win32":
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return await loop.run_in_executor(pool, _scrape_all_sync, companies)
    return await _scrape_all_async(companies)
