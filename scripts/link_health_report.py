"""
link_health_report.py -- weekly link-graph metrics.

Fetches live Blog Posts, joins with tier file, computes:
- Gini coefficient of inbound link distribution
- Per-tier inbound stats vs targets
- Orphan count
- Aakash/Allen inbound counts
- Anchor over-optimization detection

Runs weekly via GitHub Actions (Mon 04:00 IST).

USAGE:
  python scripts/link_health_report.py --tier-file out/posts-with-tiers.xlsx \
      --output out/link-health-report.xlsx

Env: WEBFLOW_API_TOKEN
"""

import argparse, sys
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body
from lib.link_utils import extract_links

HERO_PAGES = ["/site/blog/aakash-institute-fee-structure", "/site/blog/allen-kota-fees"]
TIER_TARGETS = {"T1P":(200,300), "T1":(30,60), "T2":(15,30), "T3":(5,15), "T4":(2,5)}

def gini(values):
    v = np.array(sorted(values), dtype=float)
    if v.sum() == 0 or len(v) == 0:
        return 0.0
    n = len(v)
    idx = np.arange(1, n + 1)
    return float((2 * (idx * v).sum() - (n + 1) * v.sum()) / (n * v.sum()))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--output", default="out/link-health-report.xlsx")
    a = p.parse_args()

    print("Loading tier map...")
    tiers = pd.read_excel(a.tier_file) if a.tier_file.endswith(".xlsx") else pd.read_csv(a.tier_file)
    for old,new in [("URL","url"),("Tier","tier"),("Category","category")]:
        if old in tiers.columns and new not in tiers.columns:
            tiers = tiers.rename(columns={old:new})
    tiers["url"] = tiers["url"].str.rstrip("/")
    tier_map = tiers.set_index("url")[["tier","category"]].to_dict("index")
    print(f"  {len(tier_map)} posts")

    print("Fetching live blog posts...")
    client = WebflowClient()
    inbound = Counter()
    outbound = Counter()
    anchor_dest = defaultdict(Counter)
    post_urls = []

    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        src = "/site/blog/" + str(fd.get("slug",""))
        post_urls.append(src)
        for html in get_blog_body(item).values():
            for link in extract_links(html):
                inbound[link["href"]] += 1
                outbound[src] += 1
                anchor_dest[link["anchor"]][link["href"]] += 1

    print(f"  Fetched {len(post_urls)} posts")

    per_tier = defaultdict(list)
    for u in post_urls:
        t = tier_map.get(u, {}).get("tier", "?")
        per_tier[t].append(inbound.get(u, 0))

    tier_stats = []
    for tier in ["T1P","T1","T2","T3","T4"]:
        counts = per_tier.get(tier, [])
        if counts:
            tmin, tmax = TIER_TARGETS[tier]
            tier_stats.append({
                "Tier": tier, "Post count": len(counts),
                "Mean inbound": round(np.mean(counts), 1),
                "Median inbound": int(np.median(counts)),
                "Target range": f"{tmin}-{tmax}",
                "% within target": round(100 * sum(tmin <= c <= tmax for c in counts) / len(counts), 1)})

    all_inbound = [inbound.get(u, 0) for u in post_urls]
    gini_val = gini(all_inbound)
    orphans = [u for u in post_urls if inbound.get(u, 0) <= 2]
    hero_stats = [{"URL":h, "Inbound":inbound.get(h,0), "Target":">=80"} for h in HERO_PAGES]

    anchor_over = []
    for anchor, dests in anchor_dest.items():
        for target, count in dests.items():
            if count > 50:
                anchor_over.append({"Anchor":anchor, "Target":target, "Count":count})

    top_inbound = pd.DataFrame([{"URL":u, "Inbound":c,
        "Tier":tier_map.get(u,{}).get("tier","?")} for u, c in inbound.most_common(50)])

    summary = pd.DataFrame([
        ["Gini coefficient", round(gini_val, 3), ">= 0.55"],
        ["Total posts", len(post_urls), "-"],
        ["Total internal links", sum(inbound.values()), "-"],
        ["Orphan posts (<=2 inbound)", len(orphans), "0"],
        ["Aakash inbound", inbound.get(HERO_PAGES[0], 0), ">= 80"],
        ["Allen inbound", inbound.get(HERO_PAGES[1], 0), ">= 80"],
        ["Anchor over-opt hits (>50)", len(anchor_over), "0"],
    ], columns=["Metric","Value","Target"])

    print("\n=== HEALTH SUMMARY ===")
    print(summary.to_string(index=False))
    print("\n=== TIER STATS ===")
    print(pd.DataFrame(tier_stats).to_string(index=False))

    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(a.output, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="summary", index=False)
        pd.DataFrame(tier_stats).to_excel(w, sheet_name="by-tier", index=False)
        pd.DataFrame(hero_stats).to_excel(w, sheet_name="hero-pages", index=False)
        top_inbound.to_excel(w, sheet_name="top-50-inbound", index=False)
        pd.DataFrame({"URL":orphans}).to_excel(w, sheet_name="orphans", index=False)
        if anchor_over:
            pd.DataFrame(anchor_over).to_excel(w, sheet_name="anchor-over-opt", index=False)

    print(f"\n✓ Wrote {a.output}")

if __name__ == "__main__":
    main()
