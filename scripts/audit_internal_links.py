"""
audit_internal_links.py -- inventory + classify every internal link in blog bodies.

Fetches Blog Posts via Webflow API (or reads snapshot). Extracts links from
BOTH body halves (post-body + post-body-2nd-half). Classifies each per
strategy doc sec 7 (KEEP / KILL / REWRITE / REVIEW).

USAGE:
  python scripts/audit_internal_links.py --tier-file out/posts-with-tiers.xlsx \
      --output out/internal-links-inventory.csv \
      --audit-summary out/audit-summary.xlsx --apply

  # From snapshot:
  python scripts/audit_internal_links.py --tier-file out/posts-with-tiers.xlsx \
      --from-snapshot snapshots/2026-06-15/ --apply

Env: WEBFLOW_API_TOKEN (only for live)
"""

import argparse, json, sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.link_utils import extract_links

ANCHOR_GARBAGE = {"click here","read more","this article","link","website","here","more","learn more"}

def load_tier_map(f):
    df = pd.read_excel(f) if f.endswith(".xlsx") else pd.read_csv(f)
    for old,new in [("URL","url"),("Tier","tier"),("Category","category")]:
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old:new})
    df["url"] = df["url"].str.rstrip("/")
    return df.set_index("url")[["tier","category"]].to_dict("index")

def inventory_from_snapshot(snap_dir):
    records = []
    for jp in Path(snap_dir).glob("*.json"):
        if jp.name == "manifest.json": continue
        rec = json.load(open(jp))
        src = "/site/blog/" + rec["slug"]
        for field in BLOG_BODY_FIELDS:
            html = rec.get(field, "")
            for link in extract_links(html):
                records.append({"source_url":src, "source_body_field":field,
                    "target_url":link["href"], "anchor_text":link["anchor"],
                    "position_in_field":link["position"]})
    return records

def inventory_from_live(client):
    records = []
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        src = "/site/blog/" + str(fd.get("slug",""))
        for field, html in get_blog_body(item).items():
            for link in extract_links(html):
                records.append({"source_url":src, "source_body_field":field,
                    "target_url":link["href"], "anchor_text":link["anchor"],
                    "position_in_field":link["position"]})
    return records

def classify(row, tier_map):
    """
    Classification rules (updated 10 Jul 2026):
      KILL     - extreme reverse waterfall (T1P/T1 linking DOWN to T4 across
                 categories); duplicate target within same source body
      REWRITE  - anchor is 'click here' / 'read more' / raw URL / empty
      REVIEW   - target not in tier map (needs check_url_health.py to resolve);
                 moderate reverse waterfall (down 2+ tiers cross-category);
                 cross-category with no known bridge exception
      KEEP     - everything else
    Notes:
      - Same-category downward links are KEPT (pillar hubs are supposed to link
        to their supporting posts within their own category).
      - We NEVER kill based on "not in tier map" alone — that only means the
        Screaming Frog crawl didn\'t capture it, not that the URL is dead.
        Route to REVIEW instead, then verify with check_url_health.py.
    """
    src = row["source_url"].rstrip("/")
    tgt = row["target_url"].rstrip("/")
    anchor = str(row["anchor_text"]).strip().lower()
    src_meta = tier_map.get(src, {})
    tgt_meta = tier_map.get(tgt, {})
    src_tier = src_meta.get("tier", "?")
    tgt_tier = tgt_meta.get("tier", "?")
    src_cat = src_meta.get("category")
    tgt_cat = tgt_meta.get("category")

    # 1. Anchor garbage (highest priority)
    if not anchor:
        return "REWRITE", "empty-anchor"
    if anchor in ANCHOR_GARBAGE:
        return "REWRITE", "anchor-garbage"
    if anchor.startswith("http") and len(anchor) > 20:
        return "REWRITE", "url-as-anchor"

    # 2. Target not in tier map — DO NOT assume dead. The tier map only contains
    #    posts captured by the Screaming Frog crawl. Absence can mean: new post,
    #    canonicalized post, existing redirect, or true 404. Only a live HTTP check
    #    can distinguish those. Flag for REVIEW so check_url_health.py + a human
    #    can resolve. Never KILL from this signal alone.
    if tgt.startswith("/site/blog/") and tgt_tier == "?":
        return "REVIEW", "target-not-in-tier-map"

    # 3. Tier waterfall (relaxed):
    order = ["T4", "T3", "T2", "T1", "T1P", "T0"]
    if src_tier in order and tgt_tier in order:
        drop = order.index(src_tier) - order.index(tgt_tier)  # >0 means going DOWN
        if drop > 0:
            if src_cat and tgt_cat and src_cat == tgt_cat:
                pass  # same-cat downward = OK (hub outlinks)
            elif drop >= 3:
                return "KILL", "reverse-waterfall-3plus"
            elif drop == 2:
                return "REVIEW", "reverse-waterfall-2"
            # drop==1 cross-cat = keep (mild)

    # 4. Cross-category with no bridge (T0 money pages always exempt)
    if src_cat and tgt_cat and src_cat != tgt_cat and tgt_tier != "T0":
        bridge = (src_cat == "Exams & Counselling" and tgt_cat in ("Education Loans", "Finance & Credit Education")) or \
                 (tgt_cat == "Education Loans" and src_cat in ("Study Abroad", "Courses & Careers"))
        if not bridge:
            return "REVIEW", "cross-category"

    return "KEEP", "compliant"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--from-snapshot", default=None)
    p.add_argument("--output", default="out/internal-links-inventory.csv")
    p.add_argument("--audit-summary", default="out/audit-summary.xlsx")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    print("Loading tier map...")
    tier_map = load_tier_map(a.tier_file)
    print(f"  Posts: {len(tier_map)}")

    if a.from_snapshot:
        print(f"Inventorying from {a.from_snapshot}...")
        records = inventory_from_snapshot(a.from_snapshot)
    else:
        print("Inventorying live from Webflow...")
        records = inventory_from_live(WebflowClient())

    df = pd.DataFrame(records)
    print(f"  Links found: {len(df):,}")
    if df.empty:
        print("No links.")
        return

    df["_dup"] = df["source_url"] + "|" + df["target_url"]
    df = df.sort_values(["source_url","position_in_field"])
    df["_first"] = ~df.duplicated("_dup", keep="first")

    print("Classifying...")
    acts = df.apply(lambda r: classify(r, tier_map), axis=1)
    df["action"] = [x[0] for x in acts]
    df["reason"] = [x[1] for x in acts]
    df.loc[~df["_first"], "action"] = "KILL"
    df.loc[~df["_first"], "reason"] = "duplicate-target"
    df = df.drop(columns=["_dup","_first"])

    print("\nActions:")
    print(df["action"].value_counts())

    if not a.apply:
        print("\nDRY-RUN. Pass --apply.")
        return

    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.output, index=False)
    print(f"✓ Wrote {a.output}")

    summary = pd.DataFrame([{"Action":ac, "Count":len(df[df.action==ac]),
        "% of total": round(100*len(df[df.action==ac])/len(df),1)}
        for ac in ["KEEP","KILL","REWRITE","REVIEW"]])
    with pd.ExcelWriter(a.audit_summary, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="summary", index=False)
        df.to_excel(w, sheet_name="all_links", index=False)
        for ac in ["KILL","REWRITE","REVIEW"]:
            df[df.action==ac].to_excel(w, sheet_name=f"{ac.lower()}_list", index=False)
    print(f"✓ Wrote {a.audit_summary}")

if __name__ == "__main__":
    main()
