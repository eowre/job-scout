"""
scraper.py — Async multi-ATS job scraper with description fetching.

Supported ATS platforms:
  • Greenhouse  — list via embed API (old boards.greenhouse.io OR new
                  job-boards.greenhouse.io, tried in order), description
                  via boards-api v1 / job-boards API v1 (same fallback)
  • Lever       — list + description in single call (?mode=json)
  • Ashby       — GraphQL (includes descriptionHtml)
  • Generic     — BeautifulSoup HTML fallback
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import (
    JOB_KEYWORDS,
    EXCLUDE_KEYWORDS,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT_SECS,
)

logger = logging.getLogger(__name__)


@dataclass
class ScrapedJob:
    job_id:      str
    company:     str
    title:       str
    url:         str
    description: str = ""
    location:    str = ""
    department:  str = ""
    posted_date: Optional[datetime] = None


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 date string tolerantly; return None on failure."""
    if not s:
        return None
    try:
        # Python 3.11+ handles Z; strip it for older compat
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_ms_timestamp(ms) -> Optional[datetime]:
    """Parse a Unix millisecond timestamp to a naive UTC datetime."""
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000)
    except Exception:
        return None


def _matches(title: str, description: str = "") -> bool:
    title_lower = title.lower()
    desc_lower  = description.lower()

    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in title_lower:
            return False

    for kw in JOB_KEYWORDS:
        if kw.lower() in title_lower or kw.lower() in desc_lower[:500]:
            return True

    return False


def _strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# Greenhouse  (supports both legacy boards.greenhouse.io and new job-boards.greenhouse.io)
# ---------------------------------------------------------------------------

# Tried in order; first that returns ≥1 job wins.
_GH_LIST_ENDPOINTS = [
    "https://boards.greenhouse.io/embed/job_board/json?for={co_id}",
    "https://job-boards.greenhouse.io/embed/job_board/json?for={co_id}",
]

# Tried in order for per-job description; first 200 with content wins.
_GH_DESC_ENDPOINTS = [
    "https://boards-api.greenhouse.io/v1/boards/{co_id}/jobs/{job_id}",
    "https://job-boards.greenhouse.io/api/v1/boards/{co_id}/jobs/{job_id}",
]


async def _fetch_greenhouse_description(
    session: aiohttp.ClientSession, co_id: str, numeric_id: int
) -> str:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)
    for template in _GH_DESC_ENDPOINTS:
        url = template.format(co_id=co_id, job_id=numeric_id)
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                content = data.get("content", "")
                if content:
                    return _strip_html(content)
        except Exception:
            continue
    return ""


async def _scrape_greenhouse(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    co_id   = company.get("id", "")
    co_name = company["name"]
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)

    # Try old domain first, fall back to new domain if it returns 0 jobs.
    data = None
    for template in _GH_LIST_ENDPOINTS:
        url = template.format(co_id=co_id)
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                candidate = await resp.json(content_type=None)
                if candidate.get("jobs"):
                    data = candidate
                    logger.debug(f"[{co_name}] Greenhouse: using {url}")
                    break
        except Exception:
            continue

    if not data:
        logger.warning(f"[{co_name}] Greenhouse: both endpoints empty or unreachable")
        return []

    try:
        jobs = []
        fetch_tasks = []
        raw_jobs = []

        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not _matches(title):
                continue
            dept = ""
            if j.get("departments"):
                dept = j["departments"][0].get("name", "")
            numeric_id = j.get("id", "")
            raw_jobs.append((j, dept, numeric_id))
            fetch_tasks.append(_fetch_greenhouse_description(session, co_id, numeric_id))

        if not raw_jobs:
            return []

        descriptions = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for (j, dept, numeric_id), desc in zip(raw_jobs, descriptions):
            title = j.get("title", "")
            jobs.append(ScrapedJob(
                job_id      = f"gh_{co_id}_{numeric_id}",
                company     = co_name,
                title       = title,
                url         = j.get("absolute_url", ""),
                location    = j.get("location", {}).get("name", ""),
                department  = dept,
                description = desc if isinstance(desc, str) else "",
                posted_date = _parse_iso(j.get("updated_at", "")),
            ))
        return jobs

    except Exception as exc:
        logger.warning(f"[{co_name}] Greenhouse error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------
async def _scrape_lever(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    co_id   = company.get("id", "")
    co_name = company["name"]
    url     = f"https://api.lever.co/v0/postings/{co_id}?mode=json"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)

        jobs = []
        for j in data:
            title = j.get("text", "")
            if not _matches(title):
                continue

            # Lever returns plain-text description and structured lists in the same call
            description = j.get("descriptionPlain", "").strip()
            if not description:
                lists = j.get("lists", [])
                description = "\n\n".join(
                    f"{section['text']}\n{_strip_html(section.get('content', ''))}"
                    for section in lists
                ).strip()

            job_id = j.get("id", "")
            cats   = j.get("categories", {})
            jobs.append(ScrapedJob(
                job_id      = f"lv_{co_id}_{job_id}",
                company     = co_name,
                title       = title,
                url         = j.get("hostedUrl", f"https://jobs.lever.co/{co_id}/{job_id}"),
                location    = cats.get("location", ""),
                department  = cats.get("team", ""),
                description = description,
                posted_date = _parse_ms_timestamp(j.get("createdAt")),
            ))
        return jobs

    except Exception as exc:
        logger.warning(f"[{co_name}] Lever error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Ashby (GraphQL)
# ---------------------------------------------------------------------------
_ASHBY_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings {
      id
      title
      locationName
      departmentName
      isRemote
      externalLink
      descriptionHtml
      publishedDate
    }
  }
}
"""

async def _scrape_ashby(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    co_id   = company.get("id", "")
    co_name = company["name"]
    url     = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": co_id},
        "query": _ASHBY_QUERY,
    }

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)

        postings = data.get("data", {}).get("jobBoard", {}).get("jobPostings", [])
        jobs = []
        for j in postings:
            title = j.get("title", "")
            if not _matches(title):
                continue
            job_id   = j.get("id", "")
            location = j.get("locationName", "")
            if not location and j.get("isRemote"):
                location = "Remote"
            jobs.append(ScrapedJob(
                job_id      = f"ash_{co_id}_{job_id}",
                company     = co_name,
                title       = title,
                url         = j.get("externalLink") or f"https://jobs.ashbyhq.com/{co_id}/{job_id}",
                location    = location,
                department  = j.get("departmentName", ""),
                description = _strip_html(j.get("descriptionHtml", "")),
                posted_date = _parse_iso(j.get("publishedDate", "")),
            ))
        return jobs

    except Exception as exc:
        logger.warning(f"[{co_name}] Ashby error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Generic HTML fallback
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

async def _scrape_generic(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    co_name     = company["name"]
    careers_url = company.get("url", "")
    if not careers_url:
        return []

    try:
        async with session.get(
            careers_url, headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return []
            html = await resp.text(errors="replace")

        soup   = BeautifulSoup(html, "lxml")
        parsed = urlparse(careers_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        # Grab any visible page text as a rough description
        page_text = soup.get_text(" ", strip=True)[:3000]

        seen_urls: set = set()
        jobs: List[ScrapedJob] = []

        for link in soup.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            if not text or len(text) < 5 or len(text) > 200:
                continue
            if not _matches(text):
                continue

            href = link["href"]
            if href.startswith("//"):
                href = parsed.scheme + ":" + href
            elif href.startswith("/"):
                href = base + href
            elif not href.startswith("http"):
                href = careers_url.rstrip("/") + "/" + href

            if href in seen_urls:
                continue
            seen_urls.add(href)

            slug = hash(text + href) % 10_000_000
            jobs.append(ScrapedJob(
                job_id      = f"gen_{co_name.lower().replace(' ', '_')}_{slug}",
                company     = co_name,
                title       = text,
                url         = href,
                description = page_text,
            ))

        return jobs

    except Exception as exc:
        logger.warning(f"[{co_name}] Generic scrape error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
_SCRAPERS = {
    "greenhouse": _scrape_greenhouse,
    "lever":      _scrape_lever,
    "ashby":      _scrape_ashby,
    "generic":    _scrape_generic,
}


async def scrape_company(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    ats     = company.get("ats", "generic")
    scraper = _SCRAPERS.get(ats, _scrape_generic)
    return await scraper(session, company)


async def scrape_all(companies: list) -> List[ScrapedJob]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def bounded(session, company):
        async with semaphore:
            try:
                jobs = await scrape_company(session, company)
                if jobs:
                    logger.info(f"  [{company['name']}] {len(jobs)} matching job(s)")
                return jobs
            except Exception as exc:
                logger.error(f"  [{company['name']}] Unhandled: {exc}")
                return []

    connector = aiohttp.TCPConnector(ssl=False, limit=MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks   = [bounded(session, c) for c in companies if c.get("enabled", True)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs: List[ScrapedJob] = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
    return all_jobs
