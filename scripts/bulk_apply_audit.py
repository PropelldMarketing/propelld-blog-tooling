"""
bulk_apply_audit.py -- execute KILL + REWRITE actions from an audit inventory.

Reads out/internal-links-inventory.csv (from audit_internal_links.py), filters
to rows with action=KILL or action=REWRITE, and applies the actions surgically
to Webflow blog bodies:

  KILL rows:
    - duplicate-target: keep first N occurrences, unwrap the rest.
      Policy: N=2 for T0 money-page targets (preserves CTA density),
              N=1 for everything else (SEO first-link rule).
    - reverse-waterfall-3plus, target-not-in-tier-map, etc:
      unwrap the single flagged link.

  REWRITE rows:
    - anchor-garbage / empty-anchor / url-as-anchor:
      replace the link's anchor text with a phrase from the anchor library
      (least-used variant), so the sentence still reads naturally.

Safety:
  - Auto-snapshots all blog posts before starting (unless --skip-snapshot).
  - Dry-run by default (no --apply). Prints the delta per source without
    touching Webflow.
  - Idempotent: if a link is already gone / already correct, skips gracefully.
  - Halts if error rate > 5% (5+ errors after processing 20 posts).
  - Tier / reason / limit filters for phased rollout.

USAGE:
  # Dry-run all KILL+REWRITE actions (safe, no changes):
  python scripts/bulk_apply_audit.py

  # Wave A — anchor rewrites + true 404 kills only, apply:
  python scripts/bulk_apply_audit.py --reason anchor-garbage,empty-anchor,url-as-anchor,target-not-in-tier-map --apply

  # Wave B — CTA duplicate trim to max 2 per post:
  python scripts/bulk_apply_audit.py --reason duplicate-target --apply

  # Wave C — reverse-waterfall kills:
  python scripts/bulk_apply_audit.py --reason reverse-waterfall-3plus --apply

  # Filter by source tier (e.g. only touch T4 posts first):
  python scripts/bulk_apply_audit.py --tier-filter T4 --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse
import json
import sys
import time
from collections import defaultdict, Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.link_utils import (
    remove_link, remove_duplicate_links, rewrite_anchor, link_count, normalize_url
)

HALT_ERROR_RATE = 0.05
CTA_MAX_PER_POST = 2  # for T0 money pages
DEFAULT_MAX_PER_POST = 1  # for all other targets (first-link rule)


def load_t0_pages():
    p = Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
    if not p.exists():
        return set()
    try:
        return {u.rstrip("/") for u in json.load(open(p)).get("t0_money_pages", [])}
    except Exception:
        return set()


def load_anchor_library():
    p = Path(__file__).parent.parent / "data" / "anchor_library_starter.json"
    if not p.exists():
        return {}
    try:
        return json.load(open(p)).get("destinations", {})
    except Exception:
        return {}


def pick_replacement_anchor(target_url, library, used_counter):
    """Pick least-used anchor variant for target. Fallback to generic phrase."""
    variants = library.get(target_url, [])
    if not variants:
        # Fallback: derive from URL slug
        slug = target_url.rstrip("/").split("/")[-1].replace("-", " ")
        return slug[:60] if slug else "learn more"
    # Sort by usage count (asc), then take least-used
    variants_sorted = sorted(variants, key=lambda a: used_counter.get(a, 0))
    choice = variants_sorted[0]
    used_counter[choice] += 1
    return choice


def load_audit(path):
    df = pd.read_csv(path) if path.endswith(".csv") else pd.read_excel(path)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    return df


def apply_to_item(item, source_rows, t0_pages, anchor_library, used_anchors, dry_run):
    """
    Apply all KILL/REWRITE actions for a single source post.
    Returns (log_dict, patch_dict_or_None).
    """
    fd = item.get("fieldData", {})
    slug = fd.get("slug", "")
    bodies = get_blog_body(item)
    original = dict(bodies)
    before_links = {k: link_count(v) for k, v in bodies.items()}

    kills_done, rewrites_done, skipped = 0, 0, 0
    errors = []

    # Split into REWRITEs (do first — before the link is removed by a KILL) and KILLs
    rewrites = source_rows[source_rows["action"] == "REWRITE"]
    kills = source_rows[source_rows["action"] == "KILL"]

    # --- REWRITEs ---
    # If the CSV has 'refined_action' and 'refined_anchor' columns (from
    # refine_audit_rewrites.py), those take precedence. That means:
    #   refined_action == "rewrite"    -> use refined_anchor
    #   refined_action == "kill"       -> unwrap the link entirely (treat as KILL)
    #   refined_action == "leave-alone" -> skip
    # If no refined_* columns, fall back to library lookup (old behavior).
    for _, row in rewrites.iterrows():
        field = row.get("source_body_field") or "post-body"
        if field not in bodies:
            skipped += 1
            continue
        tgt = row["target_url"]
        old_anchor = str(row.get("anchor_text", "")).strip() or None

        refined_action = str(row.get("refined_action", "")).strip().lower() if "refined_action" in row.index else ""
        refined_anchor_val = row.get("refined_anchor") if "refined_anchor" in row.index else None
        if isinstance(refined_anchor_val, float) and pd.isna(refined_anchor_val):
            refined_anchor_val = None

        if refined_action == "leave-alone":
            skipped += 1
            continue
        if refined_action == "kill":
            new_html, n = remove_link(bodies[field], tgt, anchor_text=old_anchor)
            if n > 0:
                bodies[field] = new_html
                kills_done += 1
            else:
                skipped += 1
            continue
        if refined_action == "rewrite" and refined_anchor_val:
            new_anchor = str(refined_anchor_val).strip()
        else:
            # No refinement available — fall back to library lookup.
            # This is the OLD context-blind path; it's here only as a safety
            # net when the CSV wasn't refined. Vaishali-approved runs
            # should always have refined_* columns.
            new_anchor = pick_replacement_anchor(tgt, anchor_library, used_anchors)

        new_html, n = rewrite_anchor(bodies[field], tgt, new_anchor, old_anchor=old_anchor)
        if n > 0:
            bodies[field] = new_html
            rewrites_done += 1
        else:
            skipped += 1  # link already gone or already good

    # --- KILLs (duplicate-target first via batch, then singleton KILLs) ---
    # Group duplicate-target KILLs by (field, target_url) so we can apply max-N policy
    dup_kills = kills[kills["reason"] == "duplicate-target"]
    other_kills = kills[kills["reason"] != "duplicate-target"]

    for field in BLOG_BODY_FIELDS:
        if field not in bodies:
            continue
        # Targets that have duplicate-target KILL rows in THIS field
        dup_in_field = dup_kills[dup_kills["source_body_field"] == field]
        for tgt in dup_in_field["target_url"].unique():
            keep_n = CTA_MAX_PER_POST if tgt in t0_pages else DEFAULT_MAX_PER_POST
            new_html, n = remove_duplicate_links(bodies[field], tgt, keep_n=keep_n)
            if n > 0:
                bodies[field] = new_html
                kills_done += n

    # Singleton KILLs (reverse-waterfall, unknown, etc): unwrap the specific link
    for _, row in other_kills.iterrows():
        field = row.get("source_body_field") or "post-body"
        if field not in bodies:
            skipped += 1
            continue
        tgt = row["target_url"]
        old_anchor = str(row.get("anchor_text", "")).strip() or None
        new_html, n = remove_link(bodies[field], tgt, anchor_text=old_anchor)
        if n > 0:
            bodies[field] = new_html
            kills_done += 1
        else:
            skipped += 1

    after_links = {k: link_count(v) for k, v in bodies.items()}
    changed_fields = [k for k in BLOG_BODY_FIELDS if bodies.get(k) != original.get(k)]

    log = {
        "item_id": item.get("id"),
        "slug": slug,
        "actions_kill": len(kills),
        "actions_rewrite": len(rewrites),
        "kills_done": kills_done,
        "rewrites_done": rewrites_done,
        "skipped": skipped,
        "before_links_total": sum(before_links.values()),
        "after_links_total": sum(after_links.values()),
        "fields_changed": ",".join(changed_fields),
    }
    if dry_run:
        log["status"] = "dry-run"
        return log, None
    if not changed_fields:
        log["status"] = "no-change"
        return log, None

    patch = {k: bodies[k] for k in changed_fields}
    return log, patch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default="data/internal-links-inventory.csv",
                   help="Path to audit inventory CSV")
    p.add_argument("--snapshot-dir", default="snapshots/",
                   help="Where to write pre-change snapshot")
    p.add_argument("--tier-filter", default=None,
                   help="Only process source posts matching these tiers (comma-separated)")
    p.add_argument("--reason", default=None,
                   help="Only apply rows matching these reason values (comma-separated)")
    p.add_argument("--action", default="KILL,REWRITE",
                   help="Which actions to apply (default KILL,REWRITE)")
    p.add_argument("--limit", type=int, default=0,
                   help="Limit to first N source posts")
    p.add_argument("--skip-snapshot", action="store_true",
                   help="Skip pre-run snapshot (dangerous — only if you took one manually)")
    p.add_argument("--apply", action="store_true",
                   help="Actually PATCH Webflow. Without this, dry-run only.")
    p.add_argument("--sleep", type=float, default=0.15,
                   help="Seconds between API calls (default 0.15)")
    p.add_argument("--output-log", default="out/bulk-apply-audit-log.csv")
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx",
                   help="Tier file for --tier-filter lookups")
    a = p.parse_args()

    print(f"Loading audit from {a.audit}...")
    df = load_audit(a.audit)
    print(f"  Total rows: {len(df):,}")

    allowed_actions = set(x.strip() for x in a.action.split(","))
    df = df[df["action"].isin(allowed_actions)]
    print(f"  After --action filter ({a.action}): {len(df):,}")

    if a.reason:
        allowed_reasons = set(x.strip() for x in a.reason.split(","))
        df = df[df["reason"].isin(allowed_reasons)]
        print(f"  After --reason filter ({a.reason}): {len(df):,}")

    if a.tier_filter:
        tf = pd.read_excel(a.tier_file) if a.tier_file.endswith(".xlsx") else pd.read_csv(a.tier_file)
        for old, new in [("URL", "url"), ("Tier", "tier")]:
            if old in tf.columns and new not in tf.columns:
                tf = tf.rename(columns={old: new})
        tf["url"] = tf["url"].str.rstrip("/")
        allowed_tiers = set(x.strip() for x in a.tier_filter.split(","))
        source_tiers = tf.set_index("url")["tier"].to_dict()
        df["_src_tier"] = df["source_url"].map(source_tiers)
        df = df[df["_src_tier"].isin(allowed_tiers)]
        df = df.drop(columns=["_src_tier"])
        print(f"  After --tier-filter ({a.tier_filter}): {len(df):,}")

    if len(df) == 0:
        print("\nNothing to apply. Adjust filters or check audit contents.")
        sys.exit(0)

    print("\nReason breakdown of what we'll apply:")
    print(df.groupby(["action", "reason"]).size().to_string())

    t0_pages = load_t0_pages()
    anchor_library = load_anchor_library()
    used_anchors = Counter()
    print(f"\nT0 money pages loaded: {len(t0_pages)}")
    print(f"Anchor library destinations: {len(anchor_library)}")

    # Snapshot
    client = WebflowClient()
    if a.apply and not a.skip_snapshot:
        print(f"\nSnapshotting all blog posts to {a.snapshot_dir}...")
        from lib.snapshots import snapshot_all_blogs
        snap_path, manifest = snapshot_all_blogs(a.snapshot_dir, client, dry_run=False)
        print(f"  Snapshotted {len(manifest)} posts to {snap_path}")

    # Build slug→item_id index
    print("\nIndexing live blog posts...")
    slug_to_item_id = {}
    all_items_by_id = {}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s:
            slug_to_item_id[s] = item["id"]
            all_items_by_id[item["id"]] = item  # cache full items to avoid extra GETs
    print(f"  {len(slug_to_item_id)} posts indexed")

    grouped = list(df.groupby("source_url"))
    if a.limit > 0:
        grouped = grouped[:a.limit]
        print(f"  --limit applied: processing first {len(grouped)} sources")

    print(f"\nProcessing {len(grouped)} source posts...")
    logs = []
    errors = 0
    processed = 0
    for source_url, rows in grouped:
        slug = source_url.rsplit("/", 1)[-1]
        item_id = slug_to_item_id.get(slug)
        if not item_id:
            logs.append({"source_url": source_url, "status": "no-matching-item"})
            errors += 1
            processed += 1
            continue
        item = all_items_by_id.get(item_id) or client.get_item(COLLECTIONS["blog_posts"], item_id)
        try:
            log, patch = apply_to_item(item, rows, t0_pages, anchor_library,
                                        used_anchors, dry_run=not a.apply)
            if patch and a.apply:
                client.update_item(COLLECTIONS["blog_posts"], item_id, patch)
                log["status"] = "patched"
                time.sleep(a.sleep)
            elif not patch and a.apply:
                log["status"] = "no-change"
            logs.append(log)
        except Exception as e:
            logs.append({"source_url": source_url, "slug": slug, "status": f"error: {str(e)[:200]}"})
            errors += 1

        processed += 1
        if processed % 25 == 0:
            print(f"  [{processed}/{len(grouped)}]  errors:{errors}")
        if processed > 20 and errors / processed > HALT_ERROR_RATE:
            print(f"\n! HALT: error rate {errors/processed:.1%} exceeds threshold")
            break

    Path(a.output_log).parent.mkdir(parents=True, exist_ok=True)
    log_df = pd.DataFrame(logs)
    log_df.to_csv(a.output_log, index=False)
    print(f"\n✓ Wrote {a.output_log}")

    # Summary
    print("\n=== SUMMARY ===")
    if "status" in log_df.columns:
        print(log_df["status"].value_counts().to_string())
    if "kills_done" in log_df.columns:
        print(f"\nTotal kills applied:    {log_df['kills_done'].fillna(0).sum():,.0f}")
        print(f"Total rewrites applied: {log_df['rewrites_done'].fillna(0).sum():,.0f}")
    print(f"Errors: {errors}")
    if not a.apply:
        print("\nDRY-RUN. Pass --apply to actually patch Webflow.")


if __name__ == "__main__":
    main()
