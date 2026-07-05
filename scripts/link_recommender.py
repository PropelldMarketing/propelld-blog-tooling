"""
link_recommender.py -- generate per-post link recommendations.

For each post, produces recommended outbound links respecting:
  - Tier quotas (category_grammar_rules.json)
  - Category grammar (required link types per category)
  - Commercial waterfall (lower tier -> equal or higher tier only)
  - Deficiency-based processing order within tier

USAGE:
  python scripts/link_recommender.py --tier-file out/posts-with-tiers.xlsx \
      --anchor-library data/anchor_library_starter.json \
      --output out/link-recommendations.csv --apply
"""

import argparse, json, random, sys
from pathlib import Path
from collections import defaultdict
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

def load_grammar():
    return json.load(open(Path(__file__).parent.parent / "data" / "category_grammar_rules.json"))

def load_anchors(path):
    return json.load(open(path)).get("destinations", {})

def pick_anchor(lib, target, used, fallback_title):
    variants = lib.get(target, [])
    if not variants:
        return fallback_title[:80] if fallback_title else "learn more"
    variants_sorted = sorted(variants, key=lambda a: used.get(a, 0))
    choice = random.choice(variants_sorted[:min(3, len(variants_sorted))])
    used[choice] += 1
    return choice

def suggest_position(src_tier, tgt_tier, rank):
    order = {"T0":6,"T1P":5,"T1":4,"T2":3,"T3":2,"T4":1}
    if tgt_tier == "T0": return "conclusion"
    if order.get(tgt_tier,0) >= 5:
        return "intro" if rank == 1 else "mid"
    if rank <= 3: return "mid"
    return "pre-conclusion"

def recommend_for_post(source, df, grammar, anchor_lib, used_anchors, t0_pages):
    src_tier = source["tier"]
    src_cat = source["category"]
    src_url = source["url"]
    quota = grammar["tier_outbound_quotas"].get(src_tier, {"min":3,"max":5})
    n_target = quota["max"]
    cat_rules = grammar["categories"].get(src_cat, {})
    required = cat_rules.get("required_links", {})

    order = ["T4","T3","T2","T1","T1P","T0"]
    src_idx = order.index(src_tier) if src_tier in order else 0
    same_or_higher = df[df["tier"].apply(lambda t: t in order and order.index(t) >= src_idx)]
    same_cat = same_or_higher[same_or_higher["category"] == src_cat]
    same_cat = same_cat[same_cat["url"] != src_url]

    recs, used_targets = [], set()

    # 1. Category pillars
    n_pillars = required.get("category_pillars", 1)
    for _, p in same_cat[same_cat["tier"] == "T1P"].head(n_pillars * 3).iterrows():
        if p["url"] in used_targets or len(recs) >= n_target: continue
        anchor = pick_anchor(anchor_lib, p["url"], used_anchors, p.get("title",""))
        recs.append({"source_url":src_url, "source_tier":src_tier, "source_category":src_cat,
            "target_url":p["url"], "target_tier":p["tier"], "target_category":p["category"],
            "anchor_text":anchor, "suggested_position":suggest_position(src_tier, p["tier"], len(recs)+1),
            "rationale":"category-pillar"})
        used_targets.add(p["url"])
        if sum(1 for r in recs if r["rationale"]=="category-pillar") >= n_pillars: break

    # 2. Siblings
    n_sib = required.get("sibling_intra_category", 2)
    sibs = same_cat[(same_cat["tier"].isin(["T1","T2","T3"])) & (~same_cat["url"].isin(used_targets))]
    sibs = sibs.sort_values("priority_score", ascending=False)
    for _, s in sibs.head(n_sib).iterrows():
        if len(recs) >= n_target: break
        anchor = pick_anchor(anchor_lib, s["url"], used_anchors, s.get("title",""))
        recs.append({"source_url":src_url, "source_tier":src_tier, "source_category":src_cat,
            "target_url":s["url"], "target_tier":s["tier"], "target_category":s["category"],
            "anchor_text":anchor, "suggested_position":suggest_position(src_tier, s["tier"], len(recs)+1),
            "rationale":"sibling-post"})
        used_targets.add(s["url"])

    # 3. T0 money page
    if required.get("t0_money_page", 0) > 0 and t0_pages:
        for t0 in t0_pages:
            if t0 not in used_targets and len(recs) < n_target:
                anchor = pick_anchor(anchor_lib, t0, used_anchors, "")
                recs.append({"source_url":src_url, "source_tier":src_tier, "source_category":src_cat,
                    "target_url":t0, "target_tier":"T0", "target_category":"Money Page",
                    "anchor_text":anchor, "suggested_position":"conclusion",
                    "rationale":"t0-money-page"})
                used_targets.add(t0)
                break
    return recs

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", required=True)
    p.add_argument("--anchor-library", required=True)
    p.add_argument("--output", default="out/link-recommendations.csv")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    random.seed(a.seed)
    grammar = load_grammar()
    anchor_lib = load_anchors(a.anchor_library)

    print(f"Loading tiers from {a.tier_file}...")
    df = pd.read_excel(a.tier_file) if a.tier_file.endswith(".xlsx") else pd.read_csv(a.tier_file)
    for old,new in [("URL","url"),("Tier","tier"),("Category","category"),
                    ("Priority Score (0-100)","priority_score"),("Title","title")]:
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old:new})
    df["url"] = df["url"].str.rstrip("/")
    if "priority_score" not in df.columns:
        df["priority_score"] = 50
    print(f"  Posts: {len(df)}")

    t0_pages = grammar.get("t0_money_pages", [])
    if "deficiency" not in df.columns:
        df["deficiency"] = 0

    tier_order = ["T1P","T1","T2","T3","T4"]
    df["_to"] = df["tier"].apply(lambda t: tier_order.index(t) if t in tier_order else 99)
    df = df.sort_values(["_to","deficiency"], ascending=[True,False])

    used_anchors = defaultdict(int)
    all_recs = []
    print("Generating recommendations...")
    for _, source in df.iterrows():
        all_recs.extend(recommend_for_post(source, df, grammar, anchor_lib, used_anchors, t0_pages))

    rec_df = pd.DataFrame(all_recs)
    print(f"  Total recs: {len(rec_df):,}")
    print(f"  Avg recs/source: {len(rec_df) / max(len(df),1):.1f}")
    print("\nBy rationale:")
    print(rec_df["rationale"].value_counts() if len(rec_df) else "(none)")

    if not a.apply:
        print(f"\nDRY-RUN. Pass --apply to write {a.output}")
        return
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    rec_df.to_csv(a.output, index=False)
    print(f"✓ Wrote {a.output}")

if __name__ == "__main__":
    main()
