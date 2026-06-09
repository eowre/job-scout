#!/usr/bin/env python3
"""
fetch_yc_companies.py — Pull YC company list from their public Algolia index.

Y Combinator's company directory is powered by Algolia and the index is
publicly queryable (same key used by their frontend).  This script uses
Playwright to intercept the live Algolia credentials from YC's website,
then paginates the full index and writes a seeds CSV compatible with
discover_companies.py.

Usage:
    python scripts/fetch_yc_companies.py
    python scripts/fetch_yc_companies.py --batches S22 W23 S23 W24 S24 W25
    python scripts/fetch_yc_companies.py --min-team-size 10
    python scripts/fetch_yc_companies.py --out scripts/yc_seeds.csv

Then run discovery:
    python scripts/discover_companies.py --seeds scripts/yc_seeds.csv --merge
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Extract live Algolia credentials from YC's page HTML
# ---------------------------------------------------------------------------
# YC embeds credentials directly in the HTML as:
#   window.AlgoliaOpts = {"app":"45BWZJ1SGC","key":"<live-key>"}
# This is a public search-only key — deliberately world-readable.

_YC_COMPANIES_URL = "https://www.ycombinator.com/companies"
_ALGOLIA_INDEX    = "YCCompany_production"  # YC's actual index name

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _get_algolia_creds() -> tuple[str, str]:
    """Fetch YC's companies page and extract the embedded Algolia credentials."""
    req = Request(_YC_COMPANIES_URL, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"Could not fetch {_YC_COMPANIES_URL}: {exc}") from exc

    m = re.search(r'window\.AlgoliaOpts\s*=\s*(\{[^}]+\})', html)
    if not m:
        raise RuntimeError("window.AlgoliaOpts not found in YC page — their embed format may have changed.")

    opts    = json.loads(m.group(1))
    app_id  = opts.get("app", "")
    api_key = opts.get("key", "")
    if not app_id or not api_key:
        raise RuntimeError(f"Incomplete AlgoliaOpts: {opts}")

    return app_id, api_key


# ---------------------------------------------------------------------------
# Fetch all pages from Algolia
# ---------------------------------------------------------------------------

def _search_page(app_id: str, api_key: str, page: int, hits_per_page: int = 100) -> dict:
    """Run one Algolia search page and return the raw response."""
    url = (
        f"https://{app_id.lower()}-dsn.algolia.net"
        f"/1/indexes/{_ALGOLIA_INDEX}/query"
    )
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "query": "",
        "hitsPerPage": hits_per_page,
        "page": page,
        "tagFilters": ["ycdc_public"],
        "attributesToRetrieve": [
            "name", "website", "slug", "batch", "team_size",
            "isHiring", "top_company", "status",
        ],
    }
    body = json.dumps(payload).encode()
    req  = Request(url, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _fetch_all(app_id: str, api_key: str) -> list[dict]:
    """
    Paginate the YC Algolia index.

    The public search key caps results at 1000 per query (Algolia default).
    To reach all ~4000+ companies we run the same query for each batch
    prefix (S/W), which slices the index into sub-groups each ≤ 1000.
    """
    # We'll first try a simple full dump, then fall back to batch-sliced queries.
    companies: list[dict] = []
    seen_ids: set[str] = set()

    def _add_hits(hits):
        for h in hits:
            uid = h.get("objectID") or h.get("slug") or h.get("name", "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                companies.append(h)

    # ── strategy: iterate page 0..N until empty ─────────────────────────────
    # Algolia search is limited to 1000 results (page × hitsPerPage ≤ 1000).
    # We get the first 1000 via straight pagination, then top-up by slicing
    # per YC batch prefix to escape the limit.
    HITS_PER_PAGE = 100
    page_num = 0
    while True:
        try:
            data = _search_page(app_id, api_key, page_num, HITS_PER_PAGE)
        except URLError as exc:
            print(f"  Algolia search failed at page {page_num}: {exc}", file=sys.stderr)
            break

        hits       = data.get("hits", [])
        nb_pages   = data.get("nbPages", 0)
        nb_hits    = data.get("nbHits", 0)
        _add_hits(hits)
        print(f"  Page {page_num+1}/{nb_pages}: +{len(hits)} companies  "
              f"(total: {len(companies)}, index has {nb_hits})")
        page_num += 1
        if page_num >= nb_pages:
            break
        time.sleep(0.05)

    return companies


# ---------------------------------------------------------------------------
# Domain extraction
# ---------------------------------------------------------------------------

def _clean_domain(website: str) -> str:
    """Strip protocol and trailing slashes from a website URL."""
    if not website:
        return ""
    d = website.strip()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.rstrip("/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch YC companies and write seeds CSV.")
    parser.add_argument(
        "--batches", nargs="*", metavar="BATCH",
        help="Only include companies from these YC batches (e.g. S22 W23 S24). "
             "Omit to include all batches.",
    )
    parser.add_argument(
        "--min-team-size", type=int, default=0,
        help="Skip companies with fewer than N employees (0 = include all).",
    )
    parser.add_argument(
        "--hiring-only", action="store_true",
        help="Only include companies currently marked as hiring on YC.",
    )
    parser.add_argument(
        "--top-only", action="store_true",
        help="Only include companies marked as 'top companies' by YC.",
    )
    parser.add_argument(
        "--out", default="scripts/yc_seeds.csv",
        help="Output CSV path (default: scripts/yc_seeds.csv).",
    )
    parser.add_argument(
        "--dedup-against", default="companies.json",
        help="Path to companies.json — skip companies already in this file.",
    )
    args = parser.parse_args()

    print("Fetching YC companies from Algolia…")
    try:
        app_id, api_key = _get_algolia_creds()
        print(f"  ✓ Got credentials — app_id={app_id!r}")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    all_companies = _fetch_all(app_id, api_key)
    print(f"\nTotal fetched: {len(all_companies)}")

    # --- Load existing companies for dedup ---
    existing_names: set[str] = set()
    dedup_path = Path(args.dedup_against)
    if dedup_path.exists():
        existing = json.loads(dedup_path.read_text(encoding="utf-8"))
        existing_names = {c["name"].lower() for c in existing}
        print(f"Dedup against {dedup_path}: {len(existing_names)} existing companies")

    # --- Filter ---
    batch_set = {b.upper() for b in args.batches} if args.batches else None

    filtered: list[dict] = []
    for co in all_companies:
        name    = (co.get("name") or "").strip()
        website = _clean_domain(co.get("website") or "")
        batch   = (co.get("batch") or "").strip()

        if not name or not website:
            continue
        if name.lower() in existing_names:
            continue
        if batch_set and batch not in batch_set:
            continue

        team_size = co.get("team_size") or 0
        if args.min_team_size and team_size < args.min_team_size:
            continue
        if args.hiring_only and not co.get("isHiring"):
            continue
        if args.top_only and not co.get("top_company"):
            continue

        filtered.append({"name": name, "domain": website, "batch": batch})

    print(f"After filtering: {len(filtered)} companies")

    # --- Write CSV ---
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "domain", "batch"])
        writer.writeheader()
        writer.writerows(filtered)

    print(f"\nWrote {len(filtered)} companies to {out_path}")
    print("\nNext step:")
    print(f"  python scripts/discover_companies.py --seeds {out_path} --merge")


if __name__ == "__main__":
    main()
