"""
Blog post snapshot / restore.

Snapshot format (compatible with any local, Drive, or S3 destination):
  <snapshot_dir>/<YYYY-MM-DD>/<slug>.json
  {
    "item_id": "...",
    "slug": "...",
    "snapshotted_at": "ISO-8601",
    "post-body": "...",
    "post-body-2nd-half": "...",
    "faqs": [...]   # optional
  }

The manifest at <snapshot_dir>/<YYYY-MM-DD>/manifest.json
lists every snapshotted item + hash for verification.
"""

import os
import json
import hashlib
import datetime
from pathlib import Path

from .webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS


def today_dir():
    return datetime.date.today().isoformat()


def snapshot_all_blogs(dest_dir, client=None, dry_run=True):
    """Snapshot all blog post bodies. Returns path to snapshot dir + manifest."""
    client = client or WebflowClient()
    ts_dir = Path(dest_dir) / today_dir()
    if not dry_run:
        ts_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        record = {
            "item_id": item["id"],
            "slug": fd.get("slug"),
            "snapshotted_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        for f in BLOG_BODY_FIELDS:
            record[f] = fd.get(f, "") or ""
        # Hash both bodies for tamper detection
        body_str = record["post-body"] + "\n" + record["post-body-2nd-half"]
        record["body_hash"] = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

        if not dry_run:
            with open(ts_dir / f"{fd['slug']}.json", "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

        manifest.append({
            "item_id": record["item_id"],
            "slug": record["slug"],
            "body_hash": record["body_hash"],
        })

    if not dry_run:
        with open(ts_dir / "manifest.json", "w") as f:
            json.dump({
                "date": today_dir(),
                "count": len(manifest),
                "items": manifest,
            }, f, indent=2)

    return str(ts_dir), manifest


def load_snapshot(snapshot_dir, slug):
    """Load a single post's snapshot."""
    p = Path(snapshot_dir) / f"{slug}.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def restore_post(snapshot_dir, slug, client=None, dry_run=True):
    """Restore a single post's body fields from snapshot."""
    client = client or WebflowClient()
    rec = load_snapshot(snapshot_dir, slug)
    patch = {f: rec[f] for f in BLOG_BODY_FIELDS if f in rec}
    if dry_run:
        return {"slug": slug, "would_restore": True, "hash": rec["body_hash"]}
    client.update_item(COLLECTIONS["blog_posts"], rec["item_id"], patch)
    return {"slug": slug, "restored": True, "hash": rec["body_hash"]}
