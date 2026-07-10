"""
check_url_health.py -- probe live HTTP status for a list of URLs.

Distinguishes: 200 OK, 301/302 redirect (with target), 404 dead, 5xx,
network errors. Also captures canonical tag if present (many blog URLs
return 200 with a canonical pointing to a "preferred" URL — that's not
a dead link, just a canonicalized duplicate).

USAGE:
  # Check the "target-not-in-tier-map" URLs from a previous audit run:
  python scripts/check_url_health.py --from-audit out/internal-links-inventory.csv \
      --filter-reason target-not-in-tier-map \
      --output out/url-health-report.xlsx

  # Or check an explicit URL list:
  python scripts/check_url_health.py --urls-csv path/to/urls.csv \
      --output out/url-health-report.xlsx

  # Or check ALL unique targets in an audit (slower, full inventory sweep):
  python scripts/check_url_health.py --from-audit out/internal-links-inventory.csv \
      --all-unique-targets --output out/url-health-report.xlsx
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

BASE = "https://www.propelld.com"
UA = "Mozilla/5.0 (compatible; Propelld-Health-Checker/1.0)"
TIMEOUT = 15
DELAY = 0.25  # be polite to production


def normalize(url):
    """Convert a link (path or full URL) to an absolute URL on propelld.com."""
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return BASE + url
    return BASE + "/" + url


def extract_canonical(html):
    m = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', html or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']canonical["\']', html or "", re.I)
    if m:
        return m.group(1)
    return None


def check(url, session):
    """
    Return a dict with the URL's real HTTP behavior.
    """
    abs_url = normalize(url)
    row = {
        "url": url,
        "abs_url": abs_url,
        "status_code": None,
        "final_url": None,
        "redirect_chain_len": 0,
        "canonical": None,
        "verdict": None,
        "notes": "",
    }
    try:
        # HEAD first — cheap. Some Webflow pages don't respond well to HEAD, so fall back.
        r = session.head(abs_url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (405, 501, 400):  # server disallows HEAD
            r = session.get(abs_url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        row["verdict"] = "NETWORK-ERROR"
        row["notes"] = f"{type(e).__name__}: {e}"[:200]
        return row

    row["status_code"] = r.status_code
    row["final_url"] = r.url
    row["redirect_chain_len"] = len(r.history)

    # For 2xx pages we want the canonical, so do a GET if HEAD didn't return body
    canonical = None
    if 200 <= r.status_code < 300:
        try:
            # GET the final URL to inspect canonical
            gr = session.get(r.url, timeout=TIMEOUT, allow_redirects=False)
            canonical = extract_canonical(gr.text)
        except requests.RequestException:
            pass
        row["canonical"] = canonical

    # Verdict
    if r.status_code == 404:
        row["verdict"] = "DEAD-404"
    elif r.status_code in (410,):
        row["verdict"] = "DEAD-410"
    elif 500 <= r.status_code < 600:
        row["verdict"] = f"SERVER-ERROR-{r.status_code}"
    elif 200 <= r.status_code < 300:
        if r.history:
            # Followed a redirect chain and ended at 200 — the ORIGINAL URL is a redirect
            row["verdict"] = "REDIRECT-TO-LIVE"
            row["notes"] = f"301/302 chain to {r.url}"
        elif canonical and normalize(canonical.rstrip("/")) != normalize(url.rstrip("/")):
            row["verdict"] = "LIVE-CANONICALIZED"
            row["notes"] = f"200 OK but canonical points to {canonical}"
        else:
            row["verdict"] = "LIVE"
    else:
        row["verdict"] = f"OTHER-{r.status_code}"

    return row


def load_urls(args):
    if args.urls_csv:
        df = pd.read_csv(args.urls_csv)
        if "url" not in df.columns:
            print(f"ERROR: {args.urls_csv} must have a 'url' column")
            sys.exit(1)
        return df["url"].dropna().unique().tolist()

    if args.from_audit:
        df = pd.read_csv(args.from_audit) if args.from_audit.endswith(".csv") \
            else pd.read_excel(args.from_audit)
        if args.filter_reason:
            df = df[df["reason"] == args.filter_reason]
        if args.filter_action:
            df = df[df["action"] == args.filter_action]
        if args.all_unique_targets:
            urls = df["target_url"].dropna().unique().tolist()
        else:
            urls = df["target_url"].dropna().unique().tolist()
        return urls

    print("ERROR: provide either --urls-csv or --from-audit")
    sys.exit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from-audit", default="out/internal-links-inventory.csv",
                   help="Path to internal-links-inventory.csv from audit_internal_links.py")
    p.add_argument("--filter-reason", default="target-not-in-tier-map",
                   help="Filter audit rows by 'reason' column")
    p.add_argument("--filter-action", default=None,
                   help="Filter audit rows by 'action' column")
    p.add_argument("--all-unique-targets", action="store_true",
                   help="Ignore filters, check every unique target_url in the audit")
    p.add_argument("--urls-csv", default=None,
                   help="Alternative: CSV file with a 'url' column")
    p.add_argument("--output", default="out/url-health-report.xlsx")
    p.add_argument("--delay", type=float, default=DELAY,
                   help="Seconds between requests (default 0.25)")
    p.add_argument("--limit", type=int, default=0,
                   help="Limit to first N URLs (0 = no limit)")
    args = p.parse_args()

    urls = load_urls(args)
    urls = sorted(set(urls))
    if args.limit > 0:
        urls = urls[:args.limit]
    print(f"URLs to check: {len(urls):,}")

    session = requests.Session()
    session.headers["User-Agent"] = UA

    rows = []
    for i, u in enumerate(urls, 1):
        row = check(u, session)
        rows.append(row)
        if i % 25 == 0 or i == len(urls):
            verdicts = {}
            for r in rows:
                verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
            print(f"  [{i}/{len(urls)}] " + ", ".join(f"{k}:{v}" for k, v in verdicts.items()))
        time.sleep(args.delay)

    df = pd.DataFrame(rows)
    print("\n=== VERDICT DISTRIBUTION ===")
    print(df["verdict"].value_counts().to_string())

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    if args.output.endswith(".xlsx"):
        with pd.ExcelWriter(args.output, engine="openpyxl") as w:
            df.to_excel(w, sheet_name="all", index=False)
            for verdict in df["verdict"].unique():
                # sheet name max 31 chars, no special chars
                sheet = re.sub(r'[^\w]', '-', str(verdict))[:31]
                df[df["verdict"] == verdict].to_excel(w, sheet_name=sheet, index=False)
    else:
        df.to_csv(args.output, index=False)
    print(f"\n✓ Wrote {args.output}")


if __name__ == "__main__":
    main()
