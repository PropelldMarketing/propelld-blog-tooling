"""
refresh_hub_data.py -- nightly recompute of post-count + related-articles.

Reads all Blog Posts + all Categories + all Facet items. Computes:

1. post-count on every Category, Course, Lender, Region, Exam item
   (number of blog posts referencing each item).

2. related-articles on every Blog Post (top 6 same-category posts, scored
   by facet overlap). Scoring per Rayan's handoff:
     courses: 3, exams: 2, lenders: 2, regions: 1, tags: 1
   Ties broken by lastUpdated desc. Falls back to 3 most-recent same-
   category posts if no facet overlap.

Idempotent: skips fields whose computed value already matches current.

IMPORTANT: PATCHes update the DRAFT state of items. Live site is unaffected
until you publish. Publish via Webflow Designer once the run completes.

USAGE:
  python scripts/refresh_hub_data.py                # dry-run (default)
  python scripts/refresh_hub_data.py --apply        # actually write
  python scripts/refresh_hub_data.py --apply --skip-post-count
  python scripts/refresh_hub_data.py --apply --skip-related

Env: WEBFLOW_API_TOKEN
"""

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS

SCORING_WEIGHTS = {"courses": 3, "exams": 2, "lenders": 2, "regions": 1, "tags": 1}
HALT_ERROR_RATE = 0.05


def compute_related(post, candidates):
    """Return top 6 related post IDs, scored by facet overlap. Empty if none."""
    fd = post["fieldData"]
    scored = []
    for cand in candidates:
        ofd = cand["fieldData"]
        score = 0
        for facet, weight in SCORING_WEIGHTS.items():
            a = set(fd.get(facet) or [])
            b = set(ofd.get(facet) or [])
            score += len(a & b) * weight
        last = cand.get("lastUpdated", "")
        scored.append((score, last, cand["id"]))
    # Sort by score desc, then lastUpdated desc (both descending)
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = [t[2] for t in scored if t[0] > 0][:6]
    if not top and candidates:
        # Fallback: 3 most-recent same-category posts
        by_time = sorted(candidates, key=lambda p: p.get("lastUpdated", ""), reverse=True)
        top = [p["id"] for p in by_time[:3]]
    return top


def update_post_counts(client, all_posts, apply_writes):
    """Recompute post-count on Categories + 4 Facet collections."""
    print("\n== Step 1: post-count ==")

    # Category counts
    cat_counts = Counter()
    facet_counts = {k: Counter() for k in ["courses", "lenders", "regions", "exams"]}
    for p in all_posts:
        fd = p["fieldData"]
        cat_id = fd.get("category")
        if cat_id:
            cat_counts[cat_id] += 1
        for facet in facet_counts:
            for ref in (fd.get(facet) or []):
                facet_counts[facet][ref] += 1

    # Update Categories
    print("  Updating Categories...")
    n_changed = 0
    for item in client.list_items(COLLECTIONS["categories"]):
        new_count = int(cat_counts.get(item["id"], 0))
        if item["fieldData"].get("post-count") != new_count:
            print(f"    {item['fieldData'].get('name'):40} -> {new_count}")
            if apply_writes:
                client.update_item(COLLECTIONS["categories"], item["id"],
                                    {"post-count": new_count})
            n_changed += 1
    print(f"  Categories changed: {n_changed}")

    # Update Facet collections
    for facet, coll_key in [("courses", "courses"), ("lenders", "lenders"),
                              ("regions", "regions"), ("exams", "exams_accepted")]:
        print(f"  Updating {facet.title()}...")
        n_changed = 0
        for item in client.list_items(COLLECTIONS[coll_key]):
            new_count = int(facet_counts[facet].get(item["id"], 0))
            if item["fieldData"].get("post-count") != new_count:
                if apply_writes:
                    client.update_item(COLLECTIONS[coll_key], item["id"],
                                        {"post-count": new_count})
                n_changed += 1
        print(f"    {facet.title()} items changed: {n_changed}")


def update_related_articles(client, all_posts, apply_writes):
    """Recompute related-articles field on every Blog Post."""
    print("\n== Step 2: related-articles ==")

    # Group posts by category
    by_cat = defaultdict(list)
    no_cat = 0
    for p in all_posts:
        cat_id = p["fieldData"].get("category")
        if cat_id:
            by_cat[cat_id].append(p)
        else:
            no_cat += 1

    print(f"  Posts by category: {sum(len(v) for v in by_cat.values())}")
    print(f"  Posts with no category (skipped): {no_cat}")

    n_updated = 0
    n_processed = 0
    n_errors = 0

    for post in all_posts:
        n_processed += 1
        cat_id = post["fieldData"].get("category")
        if not cat_id:
            continue
        candidates = [p for p in by_cat[cat_id] if p["id"] != post["id"]]
        if not candidates:
            continue

        top6 = compute_related(post, candidates)
        current = post["fieldData"].get("related-articles") or []

        if set(current) != set(top6) and top6:
            if apply_writes:
                try:
                    client.update_item(COLLECTIONS["blog_posts"], post["id"],
                                        {"related-articles": top6})
                except Exception as e:
                    print(f"    ERROR on {post['fieldData'].get('slug')}: {e}")
                    n_errors += 1
                    if n_processed > 20 and n_errors / n_processed > HALT_ERROR_RATE:
                        print(f"\n! HALT: error rate {n_errors/n_processed:.1%} > {HALT_ERROR_RATE:.0%}")
                        return
                    continue
            n_updated += 1

        if n_processed % 100 == 0:
            print(f"  Processed {n_processed}/{len(all_posts)} | Updated: {n_updated} | Errors: {n_errors}")

    print(f"\n  Total posts processed: {n_processed}")
    print(f"  Related-articles updated: {n_updated}")
    print(f"  Errors: {n_errors}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes. Default is dry-run.")
    parser.add_argument("--skip-post-count", action="store_true",
                        help="Skip step 1 (post-count).")
    parser.add_argument("--skip-related", action="store_true",
                        help="Skip step 2 (related-articles).")
    args = parser.parse_args()

    print(f"refresh_hub_data.py -- Mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    client = WebflowClient()

    print("\nFetching all Blog Posts (this takes 3-5 min)...")
    all_posts = list(client.list_items(COLLECTIONS["blog_posts"]))
    print(f"  Fetched {len(all_posts)} posts")

    if not args.skip_post_count:
        update_post_counts(client, all_posts, args.apply)

    if not args.skip_related:
        update_related_articles(client, all_posts, args.apply)

    print("\n" + "=" * 60)
    if args.apply:
        print("✓ DONE. All updates PATCHed as drafts.")
        print()
        print("NEXT STEP: publish the site in Webflow Designer to push")
        print("the new drafts live. Until published, the live site is")
        print("unchanged.")
    else:
        print("✓ DRY-RUN complete. No changes written.")
        print("Re-run with --apply to write.")


if __name__ == "__main__":
    main()
