"""
bulk_apply_links.py -- insert recommended links into Webflow blog bodies.

Reads link-recommendations-approved.csv. For each source: snapshots, inserts
each recommended link, PATCHes item. Handles both body halves.

Idempotent (skips if link already present).

USAGE:
  # Dry-run:
  python scripts/bulk_apply_links.py --recommendations out/link-recs.csv \
      --snapshot-dir snapshots/ --tier-filter T3,T4

  # Apply:
  python scripts/bulk_apply_links.py --recommendations out/link-recs.csv \
      --snapshot-dir snapshots/ --tier-filter T3,T4 --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse, sys, time
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.snapshots import snapshot_all_blogs
from lib.link_utils import insert_link_in_body, link_count



# ---------- UTM handling for T0 CTA targets ----------
import json as _json
from pathlib import Path as _Path

_T0_PAGES = None

def _load_t0_pages():
    global _T0_PAGES
    if _T0_PAGES is not None:
        return _T0_PAGES
    p = _Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
    if not p.exists():
        _T0_PAGES = set()
    else:
        try:
            _T0_PAGES = {u.rstrip("/") for u in _json.load(open(p)).get("t0_money_pages", [])}
        except Exception:
            _T0_PAGES = set()
    return _T0_PAGES


_UTM_CTA_PAGES = None

def _load_utm_cta_pages():
    """Pages that receive UTM-tagged CTA links (policy Jul-2026: LP landing
    pages ONLY, listed under 'utm_cta_pages' in category_grammar_rules.json).
    Other T0 money pages get clean URLs — internal UTMs reset GA4 session
    attribution and create parameter URL variants for crawlers."""
    global _UTM_CTA_PAGES
    if _UTM_CTA_PAGES is not None:
        return _UTM_CTA_PAGES
    p = _Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
    if not p.exists():
        _UTM_CTA_PAGES = set()
    else:
        try:
            _UTM_CTA_PAGES = {u.rstrip("/") for u in _json.load(open(p)).get("utm_cta_pages", [])}
        except Exception:
            _UTM_CTA_PAGES = set()
    return _UTM_CTA_PAGES


def append_utm_if_t0(target_url, source_url):
    """If target is a UTM-tracked CTA landing page (LP pages only, per policy),
    append the standard body-CTA UTM parameters so those CTA clicks stay
    trackable. All other targets (including non-LP T0 money pages) are
    returned unchanged.

    Pattern matches what already exists on Propelld body CTAs:
        ?utm_source=website&utm_adgroup=blog-body&utm_campaign=<source-path>
    """
    if target_url.rstrip("/") not in _load_utm_cta_pages():
        return target_url
    # Don't double-append if target already has query string
    if "?" in target_url:
        return target_url
    src = source_url.rstrip("/")
    return f"{target_url}?utm_source=website&utm_adgroup=blog-body&utm_campaign={src}"

HALT_ERROR_RATE = 0.05

def load_recs(path):
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    if "approved" not in df.columns:
        df["approved"] = True
    return df[df["approved"] == True]

def choose_body_field(bodies, position):
    if position in ("intro","first-h2","mid"):
        return "post-body"
    if bodies.get("post-body-2nd-half"):
        return "post-body-2nd-half"
    return "post-body"

def apply_to_item(client, item, recs, dry_run):
    fd = item.get("fieldData", {})
    bodies = get_blog_body(item)
    before = {k: link_count(v) for k, v in bodies.items()}
    inserted, skipped = 0, 0
    for _, rec in recs.iterrows():
        field = choose_body_field(bodies, rec["suggested_position"])
        current = bodies[field]
        # Idempotency: skip if either the raw target or the UTM'd variant
        # is already in the body.
        raw_tgt = rec["target_url"]
        utm_tgt = append_utm_if_t0(raw_tgt, rec["source_url"])
        if raw_tgt in current or (utm_tgt != raw_tgt and utm_tgt in current):
            skipped += 1
            continue
        # Prefer refined_anchor (Option B — context-aware pass) if present + non-empty,
        # else fall back to library anchor_text.
        anchor = rec.get("refined_anchor") if "refined_anchor" in rec else None
        if not anchor or (isinstance(anchor, float) and pd.isna(anchor)):
            anchor = rec["anchor_text"]
        # Apply UTM params for T0 CTA targets so marketing attribution survives
        final_target = append_utm_if_t0(rec["target_url"], rec["source_url"])
        bodies[field] = insert_link_in_body(current, anchor,
            final_target, rec["suggested_position"])
        inserted += 1
    after = {k: link_count(v) for k, v in bodies.items()}
    log = {"item_id": item["id"], "slug": fd.get("slug"),
        "recs_processed": len(recs), "inserted": inserted,
        "skipped_already_present": skipped,
        "before_links_total": sum(before.values()),
        "after_links_total": sum(after.values())}
    if dry_run or inserted == 0:
        log["status"] = "dry-run" if dry_run else "no-change"
        return log
    original = get_blog_body(item)
    patch = {k: bodies[k] for k in BLOG_BODY_FIELDS if bodies[k] != original[k]}
    try:
        client.update_item(COLLECTIONS["blog_posts"], item["id"], patch)
        log["status"] = "patched"
    except Exception as e:
        log["status"] = f"error: {e}"
    return log

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recommendations", default="data/link-recommendations.csv")
    p.add_argument("--snapshot-dir", default="snapshots/")
    p.add_argument("--tier-filter", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--skip-snapshot", action="store_true")
    p.add_argument("--limit", type=int, default=0,
                   help="Only process first N source posts (0 = all). "
                        "Use for small-batch testing before full-scale apply.")
    a = p.parse_args()

    print(f"Loading recommendations from {a.recommendations}...")
    recs = load_recs(a.recommendations)
    print(f"  Approved: {len(recs):,}")
    if a.tier_filter:
        allowed = set(a.tier_filter.split(","))
        recs = recs[recs["source_tier"].isin(allowed)]
        print(f"  After filter: {len(recs):,}")

    client = WebflowClient()
    if not a.skip_snapshot and a.apply:
        print(f"\nSnapshotting to {a.snapshot_dir}...")
        snap_path, manifest = snapshot_all_blogs(a.snapshot_dir, client, dry_run=False)
        print(f"  Snapshotted {len(manifest)} posts to {snap_path}")

    print("\nBuilding slug -> item_id index...")
    slug_to_id = {}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        slug_to_id[item["fieldData"].get("slug")] = item["id"]
    print(f"  {len(slug_to_id)} posts indexed")

    logs, errors, processed = [], 0, 0
    grouped = list(recs.groupby("source_url"))
    if a.limit > 0:
        grouped = grouped[:a.limit]
        print(f"  --limit applied: processing first {len(grouped)} source posts")
    print(f"\nProcessing {len(grouped)} source posts...")
    for source_url, post_recs in grouped:
        slug = source_url.split("/")[-1]
        item_id = slug_to_id.get(slug)
        if not item_id:
            logs.append({"source_url":source_url, "status":"no-matching-item"})
            errors += 1
            continue
        item = client.get_item(COLLECTIONS["blog_posts"], item_id)
        log = apply_to_item(client, item, post_recs, dry_run=not a.apply)
        logs.append(log)
        if str(log.get("status","")).startswith("error"):
            errors += 1
        processed += 1
        if processed % 25 == 0:
            print(f"  {processed}/{len(grouped)}  errors:{errors}")
        if processed > 20 and errors / processed > HALT_ERROR_RATE:
            print(f"\n! HALT: error rate {errors/processed:.1%}")
            break
        time.sleep(0.2)

    Path("out").mkdir(exist_ok=True)
    