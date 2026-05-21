#!/usr/bin/env python3
"""
debug_company.py — Step-by-step scraper debugger for a single company.

Usage:
    python scripts/debug_company.py "Acme Corp"
    python scripts/debug_company.py "Acme Corp" --show-all-links
    python scripts/debug_company.py "Acme Corp" --no-fetch
    python scripts/debug_company.py --list

Flags:
    --show-all-links   Print every link Playwright found (not just keyword matches)
    --no-fetch         Stop after keyword filtering; don't call ATS APIs
    --list             Print all companies in companies.json and exit
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure UTF-8 output on Windows so box-drawing chars render correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from playwright.async_api import async_playwright

# Import scraper internals directly — this is a dev tool, not production code
from services.scraper import (
    _HEADERS,
    _TIMEOUT,
    _BOT_BLOCKED_CODES,
    _get_job_links,
    _find_open_roles_link,
    _parse_raw_links,
    _load_and_wait,
    _detect_ats,
    _matches,
    _stable_id,
    _company_slug,
    _fetch_greenhouse_job,
    _fetch_lever_job,
    _fetch_ashby_jobs,
    _fetch_generic_description,
    ATS_GREENHOUSE,
    ATS_LEVER,
    ATS_ASHBY,
    ATS_GENERIC,
    ScrapedJob,
    JOB_KEYWORDS,
    EXCLUDE_KEYWORDS,
)

COMPANIES_PATH = Path(__file__).resolve().parent.parent / "companies.json"

# ---------------------------------------------------------------------------
# Terminal colours (ANSI — works in Windows Terminal, PowerShell 7, git bash)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass

_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_GREEN = "\033[32m"
_RED   = "\033[31m"
_YEL   = "\033[33m"
_CYAN  = "\033[36m"
_RESET = "\033[0m"


def _h(text: str) -> None:
    """Section header."""
    width = 70
    print(f"\n{_BOLD}{_CYAN}{'─' * width}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {text}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─' * width}{_RESET}")


def _ok(msg: str)   -> None: print(f"  {_GREEN}✓{_RESET}  {msg}")
def _skip(msg: str) -> None: print(f"  {_DIM}✗  {msg}{_RESET}")
def _warn(msg: str) -> None: print(f"  {_YEL}⚠  {msg}{_RESET}")
def _err(msg: str)  -> None: print(f"  {_RED}✗  {msg}{_RESET}")
def _info(msg: str) -> None: print(f"  {msg}")
def _dim(msg: str)  -> None: print(f"  {_DIM}{msg}{_RESET}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_companies() -> list:
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _find_company(name: str) -> Optional[dict]:
    for c in _load_companies():
        if c["name"].lower() == name.lower():
            return c
    return None


def _explain_match(title: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (matched_keyword, excluded_keyword) — at most one is set."""
    t = title.lower()
    excluded_by = next((k for k in EXCLUDE_KEYWORDS if k.lower() in t), None)
    if excluded_by:
        return None, excluded_by
    matched_by = next((k for k in JOB_KEYWORDS if k.lower() in t), None)
    return matched_by, None


# ---------------------------------------------------------------------------
# Main debug runner
# ---------------------------------------------------------------------------

async def debug_company(
    company: dict,
    show_all_links: bool,
    no_fetch: bool,
) -> None:
    co_name    = company["name"]
    career_url = company.get("url", "").strip()
    co_slug    = _company_slug(co_name)

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"{_BOLD}  DEBUG SCRAPE: {co_name}{_RESET}")
    print(f"{_BOLD}{'═' * 70}{_RESET}")
    _info(f"Career URL : {career_url or '(none)'}")
    _info(f"ATS type   : {company.get('ats', 'unknown')}")
    _info(f"Enabled    : {company.get('enabled', True)}")
    _info(f"Include KW : {len(JOB_KEYWORDS)} keywords")
    _info(f"Exclude KW : {len(EXCLUDE_KEYWORDS)} keywords")

    if not career_url:
        _err("No career URL configured — cannot scrape. Set it via the UI or companies.json.")
        return

    # -----------------------------------------------------------------------
    # Stage 1 — Playwright (with open-roles navigation)
    # -----------------------------------------------------------------------
    _h("STAGE 1 — Playwright: render career page")
    _info(f"Launching headless Chromium → {career_url}")

    all_links: List[Tuple[str, str]] = []
    nav_url: Optional[str] = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            # Step 1a — land on the career page
            await _load_and_wait(page, career_url)
            raw = await page.evaluate(
                """() => {
                    const links = [];
                    document.querySelectorAll('a[href]').forEach(el => {
                        const text = (el.innerText || el.textContent || '').trim();
                        const href = el.getAttribute('href') || '';
                        if (text && href) links.push({ text, href });
                    });
                    return links;
                }"""
            )
            landing_pairs = _parse_raw_links(raw, career_url)
            _info(f"Landing page: {len(landing_pairs)} links found")

            # Step 1b — check for open-roles navigation link
            nav_url = _find_open_roles_link(landing_pairs, career_url)
            if nav_url:
                _ok(f"Open-roles nav link found → {nav_url}")
                _info("Navigating to sub-page…")
                await _load_and_wait(page, nav_url)
                raw = await page.evaluate(
                    """() => {
                        const links = [];
                        document.querySelectorAll('a[href]').forEach(el => {
                            const text = (el.innerText || el.textContent || '').trim();
                            const href = el.getAttribute('href') || '';
                            if (text && href) links.push({ text, href });
                        });
                        return links;
                    }"""
                )
                all_links = _parse_raw_links(raw, nav_url)
                _info(f"Open-roles sub-page: {len(all_links)} links found")
            else:
                _dim("No open-roles navigation link detected — using landing page links")
                all_links = landing_pairs
        finally:
            await page.close()
            await context.close()
            await browser.close()

    if not all_links:
        _err("Playwright returned zero links.")
        _warn("Possible causes:")
        _warn("  • The page requires interaction (click, scroll) to load jobs")
        _warn("  • Bot detection blocked Playwright before the DOM was ready")
        _warn("  • The career URL redirects to a page with no <a href> anchors")
        _warn("  • JS loads jobs via XHR — check the page in a real browser first")
        return

    print(f"\n  Found {_BOLD}{len(all_links)}{_RESET} total links"
          + (f" (from sub-page: {nav_url})" if nav_url else " (from landing page)"))

    # -----------------------------------------------------------------------
    # Stage 2 — Keyword filtering
    # -----------------------------------------------------------------------
    _h("STAGE 2 — Keyword filtering")
    _dim(f"Include keywords: {JOB_KEYWORDS}")
    _dim(f"Exclude keywords: {EXCLUDE_KEYWORDS}")
    print()

    matching: List[Tuple[str, str]] = []
    skipped_count = 0

    for title, url in all_links:
        matched_kw, excluded_kw = _explain_match(title)
        if excluded_kw:
            skipped_count += 1
            if show_all_links:
                _skip(f"{title:<60s}  [excluded: {excluded_kw!r}]")
        elif matched_kw:
            matching.append((title, url))
            _ok(f"{title:<60s}  [matched: {matched_kw!r}]")
        else:
            skipped_count += 1
            if show_all_links:
                _skip(f"{title}")

    if not show_all_links and skipped_count:
        _dim(f"({skipped_count} non-matching links hidden — use --show-all-links to see them)")

    print()
    _info(f"Result: {_BOLD}{len(matching)} matched{_RESET} / {len(all_links)} total links")

    if not matching:
        print()
        _warn("No jobs matched your keywords.")
        _warn("Things to check:")
        _warn("  1. Run with --show-all-links to see what titles Playwright found")
        _warn("  2. The page might show job titles only after user interaction")
        _warn("  3. Job titles might be in a different element (not <a> text)")
        _warn("  4. Your keyword list might be too narrow for this company's naming")
        return

    if no_fetch:
        print()
        _dim("--no-fetch: stopping after keyword filter")
        return

    # -----------------------------------------------------------------------
    # Stage 3 — ATS detection
    # -----------------------------------------------------------------------
    _h("STAGE 3 — ATS detection")

    gh_jobs:  List[Tuple[str, str, str, str]] = []
    lv_jobs:  List[Tuple[str, str, str, str]] = []
    ash_jobs: Dict[str, Dict] = {}
    gen_jobs: List[Tuple[str, str]] = []

    for title, url in matching:
        ats_type, ats_co_id, ats_job_id = _detect_ats(url)
        label = {
            ATS_GREENHOUSE: f"{_GREEN}greenhouse{_RESET}",
            ATS_LEVER:      f"{_GREEN}lever     {_RESET}",
            ATS_ASHBY:      f"{_GREEN}ashby     {_RESET}",
            ATS_GENERIC:    f"{_YEL}generic   {_RESET}",
        }.get(ats_type, ats_type)

        print(f"  [{label}]  co_id={ats_co_id!r:<22s}  job_id={ats_job_id!r}")
        _dim(f"              title : {title}")
        _dim(f"              url   : {url}")
        print()

        if ats_type == ATS_GREENHOUSE:
            gh_jobs.append((ats_co_id, ats_job_id, title, url))
        elif ats_type == ATS_LEVER:
            lv_jobs.append((ats_co_id, ats_job_id, title, url))
        elif ats_type == ATS_ASHBY:
            ash_jobs.setdefault(ats_co_id, {})[ats_job_id] = (title, url)
        else:
            gen_jobs.append((title, url))

    # -----------------------------------------------------------------------
    # Stage 4 — ATS API fetching
    # -----------------------------------------------------------------------
    _h("STAGE 4 — Fetching job details from ATS APIs")

    http_sem    = asyncio.Semaphore(5)
    browser_sem = asyncio.Semaphore(3)
    connector   = aiohttp.TCPConnector(ssl=False)

    jobs: List[ScrapedJob] = []
    fetch_errors: List[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        async with aiohttp.ClientSession(connector=connector, headers=_HEADERS) as session:

            # Greenhouse
            for ats_co_id, ats_job_id, title, url in gh_jobs:
                _info(f"Greenhouse: {title!r}")
                _dim(f"  co_id={ats_co_id!r}  job_id={ats_job_id!r}")
                result = await _fetch_greenhouse_job(
                    session, ats_co_id, ats_job_id, title, url,
                    co_name, co_slug, http_sem,
                )
                if result:
                    jobs.append(result)
                    _ok(f"  title={result.title!r}  location={result.location!r}  "
                        f"dept={result.department!r}  desc={len(result.description)} chars")
                else:
                    _err(f"  No result returned")
                    fetch_errors.append(title)
                print()

            # Lever
            for ats_co_id, ats_job_id, title, url in lv_jobs:
                _info(f"Lever: {title!r}")
                _dim(f"  co_id={ats_co_id!r}  job_id={ats_job_id!r}")
                result = await _fetch_lever_job(
                    session, ats_co_id, ats_job_id, title, url,
                    co_name, co_slug, http_sem,
                )
                if result:
                    jobs.append(result)
                    _ok(f"  title={result.title!r}  location={result.location!r}  "
                        f"dept={result.department!r}  desc={len(result.description)} chars")
                else:
                    _err(f"  No result returned")
                    fetch_errors.append(title)
                print()

            # Ashby
            for ats_co_id, id_map in ash_jobs.items():
                wanted_ids  = set(id_map.keys())
                wanted_urls = id_map
                _info(f"Ashby: {len(wanted_ids)} job(s)  org={ats_co_id!r}")
                results = await _fetch_ashby_jobs(
                    session, ats_co_id, wanted_ids, wanted_urls,
                    co_name, co_slug, http_sem,
                )
                if results:
                    jobs.extend(results)
                    for r in results:
                        _ok(f"  title={r.title!r}  location={r.location!r}  "
                            f"dept={r.department!r}  desc={len(r.description)} chars")
                else:
                    _err(f"  No results returned for org {ats_co_id!r}")
                    fetch_errors.extend(id_map.keys())
                print()

            # Generic
            for title, url in gen_jobs:
                _info(f"Generic (aiohttp + Playwright fallback): {title!r}")
                _dim(f"  url={url}")
                desc = await _fetch_generic_description(
                    session, browser, url, http_sem, browser_sem,
                )
                job = ScrapedJob(
                    job_id=_stable_id(co_slug, url),
                    company=co_name,
                    title=title,
                    url=url,
                    description=desc,
                )
                jobs.append(job)
                if desc:
                    _ok(f"  desc={len(desc)} chars")
                else:
                    _warn(f"  No description fetched — bot detection or empty page")
                print()

        await browser.close()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    _h("SUMMARY")
    _info(f"Links on page      : {len(all_links)}")
    _info(f"Keyword matches    : {len(matching)}")
    _info(f"Jobs fetched       : {len(jobs)}")
    _info(f"With description   : {sum(1 for j in jobs if j.description)}")
    _info(f"Without description: {sum(1 for j in jobs if not j.description)}")
    if fetch_errors:
        _warn(f"Fetch errors       : {len(fetch_errors)}")
        for e in fetch_errors:
            _warn(f"  • {e}")

    if jobs:
        print()
        _info("Jobs that would be saved to the database:")
        for j in jobs:
            status = f"{_GREEN}desc ✓{_RESET}" if j.description else f"{_YEL}no desc{_RESET}"
            _info(f"  [{status}]  {j.title}  |  {j.location or 'no location'}  |  {j.url}")

    print(f"\n{_BOLD}{'═' * 70}{_RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug the scraper for a single company.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("company", nargs="?", help="Company name (must match companies.json)")
    parser.add_argument("--show-all-links", action="store_true",
                        help="Print every link found, not just keyword matches")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Stop after keyword filtering; skip ATS API calls")
    parser.add_argument("--list", action="store_true",
                        help="List all companies in companies.json and exit")
    args = parser.parse_args()

    if args.list:
        companies = _load_companies()
        print(f"\n{'Name':<35s}  {'ATS':<12s}  {'Enabled':<7s}  URL")
        print("─" * 90)
        for c in companies:
            enabled = "yes" if c.get("enabled", True) else "no"
            print(f"  {c['name']:<33s}  {c.get('ats', ''):<12s}  {enabled:<7s}  {c.get('url', '')}")
        print()
        return

    if not args.company:
        parser.print_help()
        sys.exit(1)

    company = _find_company(args.company)
    if not company:
        print(f"\n{_RED}Company {args.company!r} not found in companies.json.{_RESET}")
        print("Use --list to see available companies.\n")
        sys.exit(1)

    # Windows requires ProactorEventLoop for Playwright subprocess support
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(debug_company(company, args.show_all_links, args.no_fetch))


if __name__ == "__main__":
    main()
