"""
scraper.py — Async multi-ATS job scraper.

Supported ATS platforms:
  • Greenhouse  https://boards.greenhouse.io/embed/job_board/json?for={id}
  • Lever       https://api.lever.co/v0/postings/{id}?mode=json
  • Ashby       https://jobs.ashbyhq.com/api/non-user-graphql  (GraphQL)
  • Generic     BeautifulSoup HTML fallback for custom career pages
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import List
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


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------
async def _scrape_greenhouse(session: aiohttp.ClientSession, company: dict) -> List[ScrapedJob]:
    co_id   = company.get("id", "")
    co_name = company["name"]
    url     = f"https://boards.greenhouse.io/embed/job_board/json?for={co_id}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)

        jobs = []
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not _matches(title):
                continue
            dept = ""
            if j.get("departments"):
                dept = j["departments"][0].get("name", "")
            jobs.append(ScrapedJob(
                job_id     = f"gh_{co_id}_{j.get('id', '')}",
                company    = co_name,
                title      = title,
                url        = j.get("absolute_url", ""),
                location   = j.get("location", {}).get("name", ""),
                department = dept,
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
            title  = j.get("text", "")
            if not _matches(title):
                continue
            job_id = j.get("id", "")
            cats   = j.get("categories", {})
            jobs.append(ScrapedJob(
                job_id     = f"lv_{co_id}_{job_id}",
                company    = co_name,
                title      = title,
                url        = j.get("hostedUrl", f"https://jobs.lever.co/{co_id}/{job_id}"),
                location   = cats.get("location", ""),
                department = cats.get("team", ""),
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
      id title locationName departmentName isRemote externalLink
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
                job_id     = f"ash_{co_id}_{job_id}",
                company    = co_name,
                title      = title,
                url        = j.get("externalLink") or f"https://jobs.ashbyhq.com/{co_id}/{job_id}",
                location   = location,
                department = j.get("departmentName", ""),
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
                job_id  = f"gen_{co_name.lower().replace(' ', '_')}_{slug}",
                company = co_name,
                title   = text,
                url     = href,
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
