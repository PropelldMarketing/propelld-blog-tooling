"""
rollback_audit_rewrites.py -- targeted rollback of anchor rewrites from
bulk_apply_audit.py, restoring each anchor to its original text from the
audit CSV.

Use this when you've run bulk_apply_audit with anchor rewrites but the
result doesn't look right and you want to undo just those rewrites (not
kills). Reads data/internal-links-inventory.csv, finds every REWRITE row,
and PATCHes the live Webflow item to restore the original anchor.

Matches links by target_url within the specified body field. If the current
anchor is different from the audit's expected new anchor, still resets to
the original — this is a "force restore" for the anchor text, not a
verification pass.

USAGE:
  # Dry-run:
  python scripts/rollback_audit_rewrites.py

  # Apply:
  python scripts/rollback_audit_rewrites.py --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.link_utils import rewrite_anchor


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default="data/internal-links-inventory.csv",
                   help="Audit inventory CSV containing original anchor_text values")
    p.add_argument("--apply", action="store_true",
                   help="Actually PATCH Webflow. Without this, dry-run only.")
    p.add_argument("--sleep", type=float, default=0.15,
                   help="Seconds between API calls")
    p.add_argument("--output-log", default="out/rollback-rewrites-log.csv")
    a = p.parse_args()

    print(f"Loading audit from {a.audit}...")
    df = pd.read_csv(a.audit) if a.audit.endswith(".csv") else pd.read_excel(a.audit)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    rewrites = df[df["action"] == "REWRITE"]
    print(f"  REWRITE rows to restore: {len(rewrites)}")

    if len(rewrites) == 0:
        print("Nothing to rollback.")
        return

    print("\nAffected posts:")
    for src in rewrites["source_url"].unique():
        print(f"  {src}")

    print("\nIndexing live blog posts...")
    client = WebflowClient()
    slug_to_item = {}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s:
            slug_to_item[s] = item
    print(f"  {len(slug_to_item)} posts indexed")

    logs = []
    errors = 0
    for source_url, group in rewrites.groupby("source_url"):
        slug = source_url.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            logs.append({"source_url": source_url, "status": "no-matching-item"})
            errors += 1
            continue

        bodies = get_blog_body(item)
        original = dict(bodies)
        restored = 0
        skipped = 0

        for _, row in group.iterrows():
            field = row.get("source_body_field") or "post-body"
            if field not in bodies:
                skipped += 1
                continue
            target = row["target_url"]
            original_anchor = str(row["anchor_text"]) if pd.notna(row["anchor_text"]) else ""
            new_html, n = rewrite_anchor(bodies[field], target, original_anchor)
            if n > 0:
                bodies[field] = new_html
                restored += 1
            else:
                skipped += 1

        changed_fields = [k for k in BLOG_BODY_FIELDS if bodies.get(k) != original.get(k)]
        log = {
            "source_url": source_url,
            "slug": slug,
            "rewrites_in_audit": len(group),
            "restored": restored,
            "skipped": skipped,
            "fields_changed": ",".join(changed_fields),
        }

        if not a.apply:
            log["status"] = "dry-run"
        elif not changed_fields:
            log["status"] = "no-change"
        else:
            patch = {k: bodies[k] for k in changed_fields}
            try:
                client.update_item(COLLECTIONS["blog_posts"], item["id"], patch)
                log["status"] = "patched"
                time.sleep(a.sleep)
            except Exception as e:
                log["status"] = f"error: {str(e)[:200]}"
                errors += 1

        logs.append(log)

    log_df = pd.DataFrame(logs)
    Path(a.output_log).parent.mkdir(parents=True, exist_ok=True)
    log_df.to_csv(a.output_log, index=False)
    print(f"\n✓ Wrote {a.output_log}")

    print("\n=== SUMMARY ===")
    if "status" in log_df.columns:
        print(log_df["status"].value_counts().to_string())
    print(f"Total restored: {log_df['restored'].fillna(0).sum():,.0f}")
    print(f"Errors: {errors}")
    if not a.apply:
        print("\nDRY-RUN. Pass --apply to actually restore.")


if __name__ == "__main__":
    main()
