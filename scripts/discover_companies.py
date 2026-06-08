#!/usr/bin/env python3
"""
discover_companies.py — Batch ATS discovery for new companies.

Reads a CSV of company names + domains, probes each for their ATS
(Greenhouse / Lever / Ashby / generic Playwright), and writes confirmed
entries to a staging JSON file.  Skips companies already in companies.json.

Usage (inside Docker container):
    python scripts/discover_companies.py
    python scripts/discover_companies.py --limit 50
    python scripts/discover_companies.py --merge          # also append to companies.json
    python scripts/discover_companies.py --seeds path/to/other.csv

The staging file is written to scripts/discovered.json so you can review
before merging.  Re-running is safe — already-staged companies are skipped
unless you pass --redo.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional
import aiohttp
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT         = Path(__file__).parent.parent
_COMPANIES    = _ROOT / "companies.json"
_SEEDS_CSV    = Path(__file__).parent / "company_seeds.csv"
_STAGING      = Path(__file__).parent / "discovered.json"

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

def _ok(msg):    print(f"  {_GREEN}✓{_RESET}  {msg}")
def _warn(msg):  print(f"  {_YELLOW}?{_RESET}  {msg}")
def _err(msg):   print(f"  {_RED}✗{_RESET}  {msg}")
def _dim(msg):   print(f"  {_DIM}{msg}{_RESET}")

# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------
_STOP_WORDS = {"ai", "inc", "llc", "ltd", "corp", "technologies", "technology",
               "labs", "lab", "systems", "solutions", "software", "health",
               "security", "cloud", "data", "analytics", "platform"}

def _slugify(name: str) -> str:
    """Convert a company name to a lowercase hyphenated slug."""
    s = name.lower()
    s = re.sub(r"[''`]", "", s)           # remove apostrophes
    s = re.sub(r"[^a-z0-9]+", "-", s)    # non-alnum → hyphen
    s = s.strip("-")
    return s

def _slug_variants(name: str) -> list[str]:
    """
    Return candidate ATS slugs to try, most-likely first.
    e.g. "dbt Labs" → ["dbt-labs", "dbtlabs", "dbt"]
         "Scale AI"  → ["scale-ai", "scaleai", "scale"]
    """
    base = _slugify(name)
    compact = base.replace("-", "")
    words = base.split("-")
    # strip trailing stop-word to get short slug (e.g. "cohere-ai" → "cohere")
    short = "-".join(w for w in words if w not in _STOP_WORDS) or base

    seen: list[str] = []
    for v in [base, compact, short]:
        if v and v not in seen:
            seen.append(v)
    return seen

# ---------------------------------------------------------------------------
# ATS-specific probes (fast HTTP, no Playwright)
# ---------------------------------------------------------------------------

_TIMEOUT = aiohttp.ClientTimeout(total=10)

async def _try_greenhouse(session: aiohttp.ClientSession, name: str, slug: str) -> Optional[dict]:
    url = f"https://api.greenhouse.io/v1/boards/{slug}/departments"
    try:
        async with session.get(url, timeout=_TIMEOUT) as r:
            if r.status == 200:
                return {
                    "name": name,
                    "ats": "greenhouse",
                    "id": slug,
                    "url": f"https://boards.greenhouse.io/{slug}",
                    "enabled": True,
                }
    except Exception:
        pass
    return None


async def _try_lever(session: aiohttp.ClientSession, name: str, slug: str) -> Optional[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1"
    try:
        async with session.get(url, timeout=_TIMEOUT) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if isinstance(data, list):
                    return {
                        "name": name,
                        "ats": "lever",
                        "id": slug,
                        "url": f"https://jobs.lever.co/{slug}",
                        "enabled": True,
                    }
    except Exception:
        pass
    return None


_ASHBY_QUERY_MINIMAL = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings { id title }
  }
}
"""

async def _try_ashby(session: aiohttp.ClientSession, name: str, slug: str) -> Optional[dict]:
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": slug},
        "query": _ASHBY_QUERY_MINIMAL,
    }
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    try:
        async with session.post(url, json=payload, timeout=_TIMEOUT) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if data.get("data") and not data.get("errors"):
                    return {
                        "name": name,
                        "ats": "ashby",
                        "id": slug,
                        "url": f"https://jobs.ashbyhq.com/{slug}",
                        "enabled": True,
                    }
    except Exception:
        pass
    return None


async def _probe_apis(
    session: aiohttp.ClientSession,
    name: str,
    domain: str,
    sem: asyncio.Semaphore,
) -> Optional[dict]:
    """Try Greenhouse → Lever → Ashby for each slug variant. Return first hit."""
    variants = _slug_variants(name)
    async with sem:
        for slug in variants:
            result = await _try_greenhouse(session, name, slug)
            if result:
                return result
        for slug in variants:
            result = await _try_lever(session, name, slug)
            if result:
                return result
        for slug in variants:
            result = await _try_ashby(session, name, slug)
            if result:
                return result
    return None  # needs Playwright fallback

# ---------------------------------------------------------------------------
# Playwright fallback — visit careers page and detect ATS from links
# ---------------------------------------------------------------------------

_ATS_BOARD_RE = re.compile(
    r"https?://(?:"
    r"boards\.greenhouse\.io/(?P<gh>[^/?#]+)"
    r"|job-boards\.greenhouse\.io/(?P<gh2>[^/?#]+)"
    r"|jobs\.lever\.co/(?P<lv>[^/?#]+)"
    r"|jobs\.ashbyhq\.com/(?P<ash>[^/?#]+)"
    r")",
    re.I,
)

_CAREER_PATHS = ["/careers", "/jobs", "/about/careers", "/company/careers",
                 "/work-with-us", "/join-us", "/join", "/openings"]


async def _playwright_probe(browser, name: str, domain: str) -> Optional[dict]:
    """Visit the company's careers page and look for embedded ATS board links."""
    base = domain.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    )
    page = await context.new_page()

    try:
        # Try /careers first, then other common paths, then the bare domain
        urls_to_try = [f"{base}{p}" for p in _CAREER_PATHS] + [base]
        html = ""
        final_url = ""

        for url in urls_to_try:
            try:
                resp = await page.goto(url, timeout=15_000, wait_until="domcontentloaded")
                if resp and resp.ok:
                    await page.wait_for_timeout(1500)  # let JS render
                    html = await page.content()
                    final_url = page.url
                    break
            except Exception:
                continue

        if not html:
            return None

        # Search page source for embedded ATS board URLs
        for m in _ATS_BOARD_RE.finditer(html):
            gh  = m.group("gh") or m.group("gh2")
            lv  = m.group("lv")
            ash = m.group("ash")
            if gh:
                return {"name": name, "ats": "greenhouse", "id": gh,
                        "url": f"https://boards.greenhouse.io/{gh}", "enabled": True}
            if lv:
                return {"name": name, "ats": "lever", "id": lv,
                        "url": f"https://jobs.lever.co/{lv}", "enabled": True}
            if ash and "/" not in ash:  # skip if it looks like a job-posting path
                return {"name": name, "ats": "ashby", "id": ash,
                        "url": f"https://jobs.ashbyhq.com/{ash}", "enabled": True}

        # No embedded ATS found — record as generic with the careers URL we landed on
        careers_url = final_url or f"{base}/careers"
        return {"name": name, "ats": "generic", "id": "",
                "url": careers_url, "enabled": True}

    except Exception:
        return None
    finally:
        await context.close()


# ---------------------------------------------------------------------------
# Main discovery logic
# ---------------------------------------------------------------------------

async def discover(
    seeds: list[dict],
    existing_names: set[str],
    already_staged: set[str],
    limit: Optional[int],
    playwright_fallback: bool,
) -> list[dict]:
    """
    Run ATS discovery for all seeds not already known.
    Returns list of newly-discovered company dicts.
    """
    candidates = [
        s for s in seeds
        if s["name"].lower() not in existing_names
        and s["name"].lower() not in already_staged
        and s.get("domain", "").lower() not in ("skip", "")
    ]
    if limit:
        candidates = candidates[:limit]

    total = len(candidates)
    print(f"\n{'='*60}")
    print(f"  Discovering {total} companies  "
          f"({len(seeds) - total} already known / staged / skipped)")
    print(f"{'='*60}\n")

    results: list[dict] = []
    needs_playwright: list[dict] = []

    # ── Phase 1: concurrent API probes (no browser) ─────────────────────────
    sem = asyncio.Semaphore(10)
    connector = aiohttp.TCPConnector(ssl=False, limit=30)

    async def _probe_with_seed(session, seed):
        result = await _probe_apis(session, seed["name"], seed["domain"], sem)
        return seed, result

    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [_probe_with_seed(session, s) for s in candidates]
        done_count = 0
        for coro in asyncio.as_completed(coros):
            try:
                seed, result = await coro
            except Exception as exc:
                _err(f"API probe exception — {exc}")
                done_count += 1
                continue

            done_count += 1
            pct = done_count * 100 // total
            if result:
                _ok(f"[{pct:3d}%] {seed['name']:<35} → {result['ats']:<12} id={result['id']!r}")
                results.append(result)
            else:
                _warn(f"[{pct:3d}%] {seed['name']:<35} → needs Playwright")
                needs_playwright.append(seed)

    # ── Phase 2: Playwright fallback for unknowns ────────────────────────────
    if playwright_fallback and needs_playwright:
        print(f"\n  Playwright fallback: {len(needs_playwright)} companies\n")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            pw_sem = asyncio.Semaphore(3)  # limit concurrent browser tabs

            async def _bounded_pw(seed):
                async with pw_sem:
                    return await _playwright_probe(browser, seed["name"], seed["domain"])

            async def _pw_with_seed(seed):
                result = await _bounded_pw(seed)
                return seed, result

            pw_coros = [_pw_with_seed(s) for s in needs_playwright]
            pw_done = 0
            for coro in asyncio.as_completed(pw_coros):
                try:
                    seed, result = await coro
                except Exception as exc:
                    seed = {"name": "unknown"}
                    result = None
                    _err(f"Playwright exception — {exc}")

                pw_done += 1
                if result:
                    ats_label = result["ats"]
                    if ats_label == "generic":
                        _warn(f"[{pw_done}/{len(needs_playwright)}] {seed['name']:<35} "
                              f"→ generic  {result['url']}")
                    else:
                        _ok(f"[{pw_done}/{len(needs_playwright)}] {seed['name']:<35} "
                            f"→ {ats_label:<12} id={result['id']!r}")
                    results.append(result)
                else:
                    _err(f"[{pw_done}/{len(needs_playwright)}] {seed['name']:<35} → failed")

            await browser.close()
    elif needs_playwright:
        print(f"\n  {_DIM}Skipping {len(needs_playwright)} companies that need "
              f"Playwright (pass --no-skip-playwright to include){_RESET}")

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_seeds(path: Path) -> list[dict]:
    seeds = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name   = row.get("name", "").strip()
            domain = row.get("domain", "").strip()
            if name and domain and domain.lower() != "skip":
                seeds.append({"name": name, "domain": domain})
    return seeds


def _merge_into_companies(new_entries: list[dict]) -> int:
    existing = json.loads(_COMPANIES.read_text(encoding="utf-8"))
    existing_lower = {c["name"].lower() for c in existing}
    added = 0
    for entry in new_entries:
        if entry["name"].lower() not in existing_lower:
            existing.append(entry)
            existing_lower.add(entry["name"].lower())
            added += 1
    _COMPANIES.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return added


def main():
    parser = argparse.ArgumentParser(description="Auto-discover company ATSes from a seed list.")
    parser.add_argument("--seeds",   default=str(_SEEDS_CSV), help="CSV seed file (name,domain)")
    parser.add_argument("--limit",   type=int, default=None,  help="Process at most N companies")
    parser.add_argument("--merge",   action="store_true",     help="Append confirmed entries to companies.json")
    parser.add_argument("--redo",    action="store_true",     help="Re-process already-staged companies")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Skip Playwright fallback (only use API probes)")
    args = parser.parse_args()

    seeds = _load_seeds(Path(args.seeds))
    print(f"Loaded {len(seeds)} seeds from {args.seeds}")

    # existing companies
    existing = json.loads(_COMPANIES.read_text(encoding="utf-8"))
    existing_names = {c["name"].lower() for c in existing}

    # already staged
    already_staged: set[str] = set()
    if _STAGING.exists() and not args.redo:
        staged = json.loads(_STAGING.read_text(encoding="utf-8"))
        already_staged = {e["name"].lower() for e in staged}
        print(f"Loaded {len(staged)} already-staged entries (pass --redo to reprocess)")

    start = time.monotonic()
    new_entries = asyncio.run(
        discover(
            seeds=seeds,
            existing_names=existing_names,
            already_staged=already_staged,
            limit=args.limit,
            playwright_fallback=not args.no_playwright,
        )
    )
    elapsed = time.monotonic() - start

    if not new_entries:
        print("\n  No new entries found.")
        return

    # Load existing staging + append
    staged_existing: list[dict] = []
    if _STAGING.exists() and not args.redo:
        staged_existing = json.loads(_STAGING.read_text(encoding="utf-8"))

    staged_all = staged_existing + new_entries
    _STAGING.write_text(
        json.dumps(staged_all, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Count by ATS
    by_ats: dict[str, int] = {}
    for e in new_entries:
        by_ats[e["ats"]] = by_ats.get(e["ats"], 0) + 1

    print(f"\n{'='*60}")
    print(f"  Discovered {len(new_entries)} new companies in {elapsed:.0f}s")
    for ats, count in sorted(by_ats.items(), key=lambda x: -x[1]):
        print(f"    {ats:<12} {count}")
    print(f"  Staged → {_STAGING}")
    print(f"{'='*60}\n")

    if args.merge:
        added = _merge_into_companies(staged_all)
        print(f"  Merged {added} new entries into {_COMPANIES}")
        print(f"  Total companies now: {len(json.loads(_COMPANIES.read_text(encoding='utf-8')))}\n")
    else:
        non_generic = [e for e in new_entries if e["ats"] != "generic"]
        print(f"  Review {_STAGING} then run with --merge to add to companies.json")
        print(f"  Tip: {len([e for e in new_entries if e['ats']=='generic'])} generic entries "
              f"may need manual URL review before merging.\n")


if __name__ == "__main__":
    main()
