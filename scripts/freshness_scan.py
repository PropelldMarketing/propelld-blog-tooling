#!/usr/bin/env python3
"""
Phase 0+1: deterministic staleness scan + rule triage. $0, read-only.

Scans blog post bodies (both fields) + titles for staleness candidates
(years, dates, session ranges, fees, registration phrases), then triages
each candidate into a lane:

    A      mechanical, whitelisted rule -> may auto-apply
    B      factual -> needs source evidence, human queue by default
    LLM    needs model judgment (freshness_plan.py decides)
    IGNORE historical reference etc. (kept in output with reason: full funnel)

Reads posts from the Webflow API, or from a local snapshot dir with
--snapshot-dir (free, offline; use a restored snapshot artifact).

Output: out/freshness-inventory.csv, one row per candidate claim.
--apply is accepted for workflow uniformity; script is always read-only.
"""

import os
import sys
import csv
import argparse
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.freshness_utils import (extract_candidates, scan_title, classify_lane,
                                 load_rules, claim_priority)

COLUMNS = ["slug", "item_id", "url", "category", "tier", "field", "location",
           "block_id", "claim_type", "matched_text", "context", "lane",
           "lane_reason", "priority"]


def load_tier_map(tier_file, additions):
    import pandas as pd
    df = pd.read_excel(tier_file)[["url", "tier", "category", "title"]]
    if Path(additions).exists():
        add = pd.read_csv(additions)[["url", "tier", "category", "title"]]
        df = pd.concat([df, add], ignore_index=True)
    df = df.drop_duplicates(subset="url", keep="first")
    out = {}
    for r in df.itertuples(index=False):
        slug = str(r.url).rstrip("/").split("/")[-1]
        if slug:  # never let the site root produce an empty slug key
            out[slug] = {"url": r.url, "tier": r.tier, "category": r.category,
                         "title": r.title}
    return out


def iter_posts_api(client):
    from lib.webflow_client import COLLECTIONS, get_blog_body
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        bodies = get_blog_body(item)
        yield {"slug": fd.get("slug", ""), "item_id": item["id"],
               "name": fd.get("name", ""), **bodies}


def iter_posts_snapshot(snap_dir):
    import json
    for p in sorted(Path(snap_dir).glob("*.json")):
        if p.name == "manifest.json":
            continue
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
        yield {"slug": rec.get("slug", ""), "item_id": rec.get("item_id", ""),
               "name": rec.get("name", ""),
               "post-body": rec.get("post-body", ""),
               "post-body-2nd-half": rec.get("post-body-2nd-half", "")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="Accepted for workflow uniformity; scan is always read-only.")
    ap.add_argument("--category", default="Exams & Counselling",
                    help='Category filter; "all" scans everything.')
    ap.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    ap.add_argument("--additions", default="data/tier-map-additions.csv")
    ap.add_argument("--rules", default="data/freshness_rules.json")
    ap.add_argument("--snapshot-dir", default="",
                    help="Scan a local snapshot dir instead of the API.")
    ap.add_argument("--limit", type=int, default=0, help="Max posts (0 = all).")
    ap.add_argument("--output", default="out/freshness-inventory.csv")
    a = ap.parse_args()

    rules = load_rules(a.rules)
    tiers = load_tier_map(a.tier_file, a.additions)
    today = datetime.date.today()

    if a.snapshot_dir:
        posts = iter_posts_snapshot(a.snapshot_dir)
    else:
        from lib.webflow_client import WebflowClient
        posts = iter_posts_api(WebflowClient())

    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    funnel = {"posts_seen": 0, "posts_scanned": 0, "posts_skipped_category": 0,
              "posts_unmapped": 0, "A": 0, "B": 0, "LLM": 0, "IGNORE": 0}

    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for post in posts:
            funnel["posts_seen"] += 1
            meta = tiers.get(post["slug"])
            if meta is None:
                funnel["posts_unmapped"] += 1
                meta = {"url": "/site/blog/" + post["slug"], "tier": "",
                        "category": ""}
            if a.category.lower() != "all" and \
                    str(meta["category"]).lower() != a.category.lower():
                funnel["posts_skipped_category"] += 1
                continue
            funnel["posts_scanned"] += 1
            cands = scan_title(post.get("name", ""))
            for field in ("post-body", "post-body-2nd-half"):
                cands.extend(extract_candidates(post.get(field, ""), field))
            for c in cands:
                lane, reason = classify_lane(c, rules, today)
                funnel[lane] += 1
                w.writerow({"slug": post["slug"], "item_id": post["item_id"],
                            "url": meta["url"], "category": meta["category"],
                            "tier": meta["tier"], "lane": lane,
                            "lane_reason": reason,
                            "priority": claim_priority(
                                c, today,
                                rules.get("relevance_window_days", 45)),
                            **c})
            f.flush()  # checkpoint per post
            if a.limit and funnel["posts_scanned"] >= a.limit:
                break

    print("Funnel: %s" % funnel)
    print("Wrote %s" % a.output)


if __name__ == "__main__":
    main()
