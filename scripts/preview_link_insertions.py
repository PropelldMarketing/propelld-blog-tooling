"""
preview_link_insertions.py -- generate side-by-side before/after previews
of what bulk_apply_links would insert into blog bodies.

Zero writes to Webflow. Pure preview so a human can eyeball whether
recommended insertions look right BEFORE any actual apply happens.

Picks N source posts (sampled across tiers by default), fetches their
current bodies from Webflow, simulates the exact insertion logic from
bulk_apply_links (including UTM handling + refined_anchor), and writes
a single self-contained HTML file with per-post before/after diffs.

USAGE:
  # Preview 10 sample posts (default):
  python scripts/preview_link_insertions.py --apply

  # Preview specific tiers:
  python scripts/preview_link_insertions.py --sample-size 5 --tier-filter T4 --apply

  # Preview specific posts:
  python scripts/preview_link_insertions.py \
      --slugs cat-cut-off-scores-2024,tnea-cutoff --apply

Output: out/preview.html — open in a browser to review

Env: WEBFLOW_API_TOKEN
"""

import argparse
import html as _html
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body
from lib.link_utils import insert_link_in_body, link_count

# Reuse the UTM logic from bulk_apply_links
if "scripts.bulk_apply_links" in sys.modules:
    del sys.modules["scripts.bulk_apply_links"]
from scripts.bulk_apply_links import append_utm_if_t0


def load_recs(path):
    df = pd.read_csv(path) if path.endswith(".csv") else pd.read_excel(path)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    return df


def choose_body_field(bodies, position):
    if position in ("intro", "first-h2", "mid"):
        return "post-body"
    if bodies.get("post-body-2nd-half"):
        return "post-body-2nd-half"
    return "post-body"


def sample_sources(recs, sample_size, tier_filter=None, seed=42):
    """Sample a diverse set of source posts to preview."""
    df = recs.copy()
    if tier_filter:
        df = df[df["source_tier"].isin(set(tier_filter.split(",")))]
    unique_sources = df["source_url"].unique().tolist()
    if len(unique_sources) <= sample_size:
        return unique_sources
    # Try to sample across tiers evenly
    by_tier = {}
    for src in unique_sources:
        t = df[df["source_url"] == src]["source_tier"].iloc[0]
        by_tier.setdefault(t, []).append(src)
    random.seed(seed)
    picked = []
    per_tier = max(1, sample_size // max(len(by_tier), 1))
    for t, srcs in by_tier.items():
        random.shuffle(srcs)
        picked.extend(srcs[:per_tier])
    return picked[:sample_size]


def render_preview_html(previews):
    """Build a single self-contained HTML file with all previews."""
    css = """
    <style>
      body { font-family: -apple-system, sans-serif; max-width: 1400px;
             margin: 30px auto; padding: 0 20px; color: #222; }
      h1 { border-bottom: 3px solid #333; padding-bottom: 8px; }
      .post { margin: 40px 0; border: 1px solid #ddd; border-radius: 6px;
              overflow: hidden; }
      .post-header { background: #f4f4f4; padding: 12px 16px;
                     border-bottom: 1px solid #ddd; }
      .post-header h2 { margin: 0 0 4px; font-size: 18px; color: #06c; }
      .post-meta { font-size: 12px; color: #666; }
      .diff { display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
              background: #ddd; }
      .diff-col { background: white; padding: 20px; overflow: auto;
                  max-height: 500px; }
      .diff-col h3 { margin-top: 0; font-size: 14px; text-transform: uppercase;
                     color: #666; letter-spacing: 1px; }
      .before h3 { color: #b00; }
      .after h3 { color: #080; }
      .diff-col a { color: #06c; text-decoration: underline; }
      .link-highlight { background: #ff0; padding: 2px; border-radius: 2px; }
      .insertion-list { margin: 0; padding: 12px 16px; background: #f9f9f9;
                        border-top: 1px solid #eee; font-size: 13px; }
      .insertion-list li { margin: 4px 0; }
      .stats { color: #666; font-size: 13px; margin-top: 8px; }
      .warn { color: #b60; font-weight: bold; }
    </style>
    """
    body_parts = ["<h1>Link Insertion Preview — bulk_apply_links dry-run</h1>",
                  f"<p>Generated on {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M IST')} — "
                  f"showing {len(previews)} sample posts. No changes made to Webflow.</p>"]

    for pv in previews:
        before_html = pv["before_html"]
        after_html = pv["after_html"]
        # Highlight new links in after
        for tgt, anchor in pv["insertions"]:
            marker = f'<a href="{_html.escape(tgt, quote=True)}">{_html.escape(anchor)}</a>'
            after_html = after_html.replace(
                marker,
                f'<span class="link-highlight">{marker}</span>',
                1
            )

        insertions_str = "".join(
            f'<li><b>{_html.escape(a)}</b> → <code>{_html.escape(t)}</code></li>'
            for t, a in pv["insertions"]
        )

        body_parts.append(f"""
        <div class="post">
          <div class="post-header">
            <h2>{_html.escape(pv["slug"])}</h2>
            <div class="post-meta">
              Source tier: <b>{pv.get("source_tier", "?")}</b> |
              Field: {pv.get("field", "post-body")} |
              Insertions attempted: {len(pv["insertions"])}
            </div>
            <div class="stats">
              Before: {pv["before_links"]} links → After: {pv["after_links"]} links
              ({pv["after_links"] - pv["before_links"]:+d})
            </div>
          </div>
          <ol class="insertion-list">{insertions_str or "<li>(no insertions applied — see error/skip below)</li>"}</ol>
          <div class="diff">
            <div class="diff-col before">
              <h3>Before</h3>
              {before_html}
            </div>
            <div class="diff-col after">
              <h3>After (with proposed insertions highlighted)</h3>
              {after_html}
            </div>
          </div>
        </div>
        """)

    return "<!DOCTYPE html><html><head><meta charset='utf-8'>" + css + "</head><body>" + "".join(body_parts) + "</body></html>"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recommendations", default="data/link-recommendations.csv")
    p.add_argument("--refined", default=None,
                   help="Optional refined recs CSV (from anchor_refiner). If present, "
                        "uses refined_anchor and refined_action columns.")
    p.add_argument("--sample-size", type=int, default=10)
    p.add_argument("--tier-filter", default=None,
                   help="Sample only from these source tiers")
    p.add_argument("--slugs", default=None,
                   help="Comma-separated list of specific slugs to preview")
    p.add_argument("--output", default="out/preview.html")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--apply", action="store_true",
                   help="Required to actually run. This is read-only either way but "
                        "we keep the flag for workflow consistency.")
    a = p.parse_args()

    print(f"Loading recommendations from {a.recommendations}...")
    recs = load_recs(a.recommendations)
    print(f"  {len(recs):,} total recs")

    # If a refined file is provided, merge in the refined_anchor column
    if a.refined and Path(a.refined).exists():
        print(f"Merging in refined anchors from {a.refined}...")
        refined = load_recs(a.refined)
        keep_cols = ["source_url", "target_url"]
        for extra in ("refined_anchor", "refined_action"):
            if extra in refined.columns:
                keep_cols.append(extra)
        recs = recs.merge(
            refined[keep_cols], on=["source_url", "target_url"], how="left"
        )
        print(f"  refined coverage: {(~recs.get('refined_anchor', pd.Series()).isna()).sum() if 'refined_anchor' in recs.columns else 0} of {len(recs)}")

    # Pick sample sources
    if a.slugs:
        sources = ["/site/blog/" + s.strip() for s in a.slugs.split(",")]
    else:
        sources = sample_sources(recs, a.sample_size, a.tier_filter, a.seed)
    print(f"\nSample size: {len(sources)}")
    for s in sources:
        print(f"  {s}")

    # Fetch bodies from Webflow
    print(f"\nFetching bodies from Webflow...")
    client = WebflowClient()
    slug_to_item = {}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        slug = item.get("fieldData", {}).get("slug")
        if slug and f"/site/blog/{slug}" in sources:
            slug_to_item[slug] = item
    print(f"  Cached {len(slug_to_item)} of {len(sources)} sources")

    # Simulate insertion per source
    previews = []
    for src in sources:
        slug = src.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            previews.append({
                "slug": slug,
                "before_html": "<p>(post not found in Webflow)</p>",
                "after_html": "<p>(post not found in Webflow)</p>",
                "insertions": [],
                "before_links": 0,
                "after_links": 0,
            })
            continue
        bodies = get_blog_body(item)
        original = dict(bodies)

        # Get recommendations for this source
        source_recs = recs[recs["source_url"] == src]
        insertions = []
        for _, rec in source_recs.iterrows():
            field = choose_body_field(bodies, rec.get("suggested_position", "mid"))
            current = bodies[field]

            # Anchor priority: refined_anchor > refined_action skip > anchor_text
            refined_action = str(rec.get("refined_action", "")).strip().lower() if "refined_action" in rec.index else ""
            if refined_action == "leave-alone":
                continue
            refined_anchor = rec.get("refined_anchor") if "refined_anchor" in rec.index else None
            if isinstance(refined_anchor, float) and pd.isna(refined_anchor):
                refined_anchor = None
            anchor = refined_anchor or rec["anchor_text"]
            if isinstance(anchor, float) and pd.isna(anchor):
                anchor = rec.get("target_url", "learn more").rsplit("/", 1)[-1].replace("-", " ")

            # UTM handling for T0 CTA targets
            final_target = append_utm_if_t0(rec["target_url"], src)

            # Skip if already present
            if final_target in current or rec["target_url"] in current:
                continue

            bodies[field] = insert_link_in_body(current, anchor, final_target,
                                                 rec.get("suggested_position", "mid"))
            insertions.append((final_target, anchor))

        # Use the post-body as the "main" body for preview (most substantive)
        main_field = "post-body"
        previews.append({
            "slug": slug,
            "source_tier": source_recs["source_tier"].iloc[0] if len(source_recs) else "?",
            "field": main_field,
            "before_html": original.get(main_field, ""),
            "after_html": bodies.get(main_field, ""),
            "insertions": insertions,
            "before_links": sum(link_count(v) for v in original.values()),
            "after_links": sum(link_count(v) for v in bodies.values()),
        })

    # Render HTML
    html_out = render_preview_html(previews)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(html_out, encoding="utf-8")
    print(f"\n✓ Wrote {a.output}")
    print(f"  Open in a browser to review before/after diffs")

    # Also print a per-post summary
    print("\n=== SUMMARY ===")
    for pv in previews:
        print(f"  {pv['slug']:<50} {pv['before_links']:>3} → {pv['after_links']:>3}  ({len(pv['insertions'])} insertions)")


if __name__ == "__main__":
    main()
