"""
publish_facet_items.py
Bulk-publish all draft facet items across Courses, Lenders, Regions, and
Exams Accepted collections.

WHEN TO RUN: only after (a) the Facet hub page templates are live in Webflow
Designer AND (b) each draft item has at least hub-intro + hub-meta-title +
hub-meta-description populated. Use check_facet_content.py first to see what
is incomplete.

USAGE:
  python publish_facet_items.py                  # dry-run (default)
  python publish_facet_items.py --apply          # actually publish
  python publish_facet_items.py --collection courses  # one collection only

ENV:
  WEBFLOW_API_TOKEN   required
"""

import argparse
import os
import sys
import time
import json
import requests

# Site + collection IDs (from the May 2026 audit)
SITE_ID = "63a98d7ca3749777345ba1fd"
COLLECTIONS = {
    "courses":          {"id": "6a0d870f601c9d1e423a0bd2", "expected_total": 20},
    "lenders":          {"id": "6a0d8710807b4792f70f774a", "expected_total": 24},
    "regions":          {"id": "6a0d8718faae524c59d4df50", "expected_total": 26},
    "exams_accepted":   {"id": "66de84c04907156359f7fa2d", "expected_total": 94},
}

API_BASE = "https://api.webflow.com/v2"
HEADERS_TEMPLATE = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

CHUNK_SIZE = 100  # Webflow allows up to 100 item IDs per publish call


def headers(token):
    return {**HEADERS_TEMPLATE, "Authorization": f"Bearer {token}"}


def list_items(token, collection_id, limit=100):
    """Paginate through all items in a collection."""
    items = []
    offset = 0
    while True:
        r = requests.get(
            f"{API_BASE}/collections/{collection_id}/items",
            headers=headers(token),
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items.extend(data.get("items", []))
        pagination = data.get("pagination", {})
        if len(items) >= pagination.get("total", 0):
            break
        offset += limit
    return items


def is_content_complete(item):
    """An item is ready to publish if it has hub-intro AND hub-meta-title
    AND hub-meta-description populated. hub-title-h1 is also required but
    is populated on all current items."""
    fd = item.get("fieldData", {})
    required = ["hub-title-h1", "hub-intro", "hub-meta-title", "hub-meta-description"]
    return all(fd.get(f) for f in required)


def publish_items(token, collection_id, item_ids, dry_run=True):
    """Publish a batch of item IDs in a collection."""
    if not item_ids:
        return {"published": 0, "skipped": 0}

    if dry_run:
        return {"published": 0, "skipped": len(item_ids), "dry_run": True}

    # Chunk into batches of CHUNK_SIZE
    published = 0
    for i in range(0, len(item_ids), CHUNK_SIZE):
        chunk = item_ids[i:i + CHUNK_SIZE]
        r = requests.post(
            f"{API_BASE}/collections/{collection_id}/items/publish",
            headers=headers(token),
            json={"itemIds": chunk},
            timeout=60,
        )
        if r.status_code >= 400:
            print(f"  ! Publish error for chunk {i}-{i+len(chunk)}: {r.status_code} {r.text}")
            r.raise_for_status()
        published += len(chunk)
        time.sleep(1.0)  # Throttle: 1 chunk/sec
    return {"published": published, "skipped": 0}


def process_collection(token, name, info, dry_run):
    coll_id = info["id"]
    print(f"\n=== {name.upper()} (collection {coll_id}) ===")

    items = list_items(token, coll_id)
    drafts = [i for i in items if i.get("isDraft")]
    published_items = [i for i in items if not i.get("isDraft")]

    print(f"  Total items: {len(items)} (expected ~{info['expected_total']})")
    print(f"  Currently published: {len(published_items)}")
    print(f"  Currently draft:     {len(drafts)}")

    # Filter drafts to those with complete content
    ready = [i for i in drafts if is_content_complete(i)]
    not_ready = [i for i in drafts if not is_content_complete(i)]

    print(f"  Drafts ready to publish (have hub-intro + meta-title + meta-desc): {len(ready)}")
    print(f"  Drafts NOT ready (missing required content fields):                {len(not_ready)}")

    if not_ready:
        print(f"  Listing 5 incomplete items as examples:")
        for i in not_ready[:5]:
            fd = i["fieldData"]
            missing = [k for k in ("hub-intro", "hub-meta-title", "hub-meta-description") if not fd.get(k)]
            print(f"    - {fd.get('name'):40} | missing: {', '.join(missing)}")
        if len(not_ready) > 5:
            print(f"    (and {len(not_ready) - 5} more)")

    if not ready:
        print(f"  → Nothing to publish in {name}.")
        return {"published": 0, "skipped": 0, "incomplete": len(not_ready)}

    if dry_run:
        print(f"  DRY-RUN: would publish {len(ready)} items. Pass --apply to actually publish.")
    else:
        print(f"  PUBLISHING {len(ready)} items in chunks of {CHUNK_SIZE}...")

    ids = [i["id"] for i in ready]
    result = publish_items(token, coll_id, ids, dry_run)
    result["incomplete"] = len(not_ready)
    return result


def main():
    parser = argparse.ArgumentParser(description="Bulk-publish draft facet items.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually publish. Default is dry-run.")
    parser.add_argument("--collection",
                        choices=list(COLLECTIONS.keys()) + ["all"],
                        default="all",
                        help="Which collection to process (default: all).")
    args = parser.parse_args()

    token = os.environ.get("WEBFLOW_API_TOKEN")
    if not token:
        print("ERROR: WEBFLOW_API_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)

    targets = [args.collection] if args.collection != "all" else list(COLLECTIONS.keys())
    summary = {}
    for name in targets:
        info = COLLECTIONS[name]
        try:
            summary[name] = process_collection(token, name, info, dry_run=not args.apply)
        except requests.HTTPError as e:
            print(f"  ! Aborting {name} due to API error: {e}")
            summary[name] = {"error": str(e)}

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_pub = sum(s.get("published", 0) for s in summary.values())
    total_skip = sum(s.get("skipped", 0) for s in summary.values())
    total_incomplete = sum(s.get("incomplete", 0) for s in summary.values())
    print(f"Published:  {total_pub}")
    print(f"Skipped:    {total_skip}  (dry-run or already-published)")
    print(f"Incomplete: {total_incomplete}  (missing required content; run check_facet_content.py)")
    print()
    for name, s in summary.items():
        print(f"  {name:20} {json.dumps(s)}")


if __name__ == "__main__":
    main()
