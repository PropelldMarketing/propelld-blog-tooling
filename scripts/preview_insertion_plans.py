"""
preview_insertion_plans.py -- HTML preview of what insert_planned_links.py
would do, without touching Webflow.

Reads out/insertion-plans.csv, fetches source bodies, simulates each
insertion (via the same sentence-level replacement logic as the executor),
and writes out/insertion-preview.html with before/after per source.

USAGE:
  python scripts/preview_insertion_plans.py --apply

  # Preview specific sources:
  python scripts/preview_insertion_plans.py --slugs cat-cut-off-scores-2024,tnea-cutoff --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse
import html as _html
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body
from lib.link_utils import link_count

if "scripts.insert_planned_links" in sys.modules:
    del sys.modules["scripts.insert_planned_links"]
from scripts.insert_planned_links import md_link_to_html, apply_insertion_to_paragraph


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plans", default="data/insertion-plans.csv",
                   help="Plans CSV. Default: data/ (committed). Also accepts out/ (workflow output).")
    p.add_argument("--output", default="out/insertion-preview.html")
    p.add_argument("--slugs", default=None, help="Comma-separated slugs to preview")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    print(f"Loading plans from {a.plans}...")
    df = pd.read_csv(a.plans)
    insertions = df[df.get("action", "") == "insert"]
    print(f"  Insertion rows: {len(insertions):,}")

    # Pick sources
    if a.slugs:
        wanted = {"/site/blog/" + s.strip() for s in a.slugs.split(",")}
        sources = [u for u in insertions["source_url"].unique() if u in wanted]
    else:
        sources = list(insertions["source_url"].unique())[:a.limit]
    print(f"  Previewing {len(sources)} sources")

    print("Fetching bodies from Webflow...")
    client = WebflowClient()
    slug_to_item = {}
    wanted_slugs = {s.rsplit("/", 1)[-1] for s in sources}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s in wanted_slugs:
            slug_to_item[s] = item

    previews = []
    for src in sources:
        slug = src.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            previews.append({"slug": slug, "before": "", "after": "", "insertions": []})
            continue
        bodies = get_blog_body(item)
        original_html = bodies.get("post-body", "")
        soup = BeautifulSoup(original_html, "html.parser")

        applied = []
        errors = []
        for _, plan in insertions[insertions["source_url"] == src].iterrows():
            # pandas returns float('nan') for empty cells — treat as "no error"
            val_err = plan.get("validation_error")
            has_val_err = (val_err is not None
                           and not (isinstance(val_err, float) and str(val_err) == "nan")
                           and str(val_err).strip() not in ("", "nan"))
            if has_val_err:
                errors.append(f"validation: {val_err}")
                continue
            new_html = md_link_to_html(str(plan["new_sentence"]), src, preview_marker=True)
            try:
                ok, reason = apply_insertion_to_paragraph(
                    soup,
                    int(plan["paragraph_idx"]),
                    str(plan["original_sentence"]),
                    new_html,
                )
                if ok:
                    applied.append({
                        "target": plan["target_url"],
                        "anchor": plan.get("anchor", ""),
                        "para": int(plan["paragraph_idx"]),
                        "old": str(plan["original_sentence"])[:120],
                        "new": str(plan["new_sentence"])[:250],
                        "why": plan.get("reasoning", ""),
                    })
                else:
                    errors.append(f"{plan.get('target_url','?')}: {reason}")
            except Exception as e:
                errors.append(f"exception: {str(e)[:100]}")

        after_html = str(soup)
        previews.append({
            "slug": slug,
            "source_url": src,
            "before": original_html,
            "after": after_html,
            "before_links": link_count(original_html),
            "after_links": link_count(after_html),
            "insertions": applied,
            "errors": errors,
        })

    # Render HTML
    css = """
    <style>
      body { font-family: -apple-system, sans-serif; max-width: 1500px;
             margin: 30px auto; padding: 0 20px; }
      h1 { border-bottom: 3px solid #333; padding-bottom: 8px; }
      .post { margin: 40px 0; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }
      .post-header { background: #f4f4f4; padding: 12px 16px; border-bottom: 1px solid #ddd; }
      .post-header h2 { margin: 0 0 4px; font-size: 18px; color: #06c; }
      .stats { color: #666; font-size: 13px; margin-top: 4px; }
      .plan { background: #eef; border-left: 3px solid #46e; padding: 10px 14px;
              margin: 8px 0; font-size: 13px; }
      .plan .old { color: #a00; text-decoration: line-through; }
      .plan .new { color: #080; }
      .plan .why { color: #666; font-style: italic; margin-top: 6px; }
      .errors { background: #fee; border-left: 3px solid #c33; padding: 10px 14px;
                margin: 8px 0; font-size: 13px; }
      .diff { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: #ddd; }
      .diff-col { background: white; padding: 20px; overflow: auto; max-height: 600px; }
      .diff-col h3 { margin-top: 0; font-size: 14px; text-transform: uppercase;
                     color: #666; letter-spacing: 1px; }
      .before h3 { color: #b00; }
      .after h3 { color: #080; }
      .after a[data-preview-new="1"] {
        background: #ffeb3b;
        padding: 3px 5px; border-radius: 3px;
        outline: 2px solid #ff9800;
        font-weight: bold;
      }
      .no-inserts-warning { background: #fee; border-left: 4px solid #c33;
        padding: 12px 16px; margin: 10px 0; color: #900; font-weight: bold; }
      .success-banner { background: #efe; border-left: 4px solid #080;
        padding: 12px 16px; margin: 10px 0; color: #060; }
    </style>
    """

    total_applied = sum(len(pv["insertions"]) for pv in previews)
    total_errors = sum(len(pv["errors"]) for pv in previews)
    posts_with_no_inserts = sum(1 for pv in previews if len(pv["insertions"]) == 0)
    body_parts = [f"<h1>Insertion plan preview — {len(previews)} sources</h1>",
                  f"<p>Generated {pd.Timestamp.now():%Y-%m-%d %H:%M IST}.</p>",
                  f"<p><b>Total insertions applied:</b> {total_applied}<br>"
                  f"<b>Total errors/skips:</b> {total_errors}<br>"
                  f"<b>Posts with zero insertions:</b> {posts_with_no_inserts} of {len(previews)}</p>",
                  f"<p>NEW inserted links appear <span style='background:#ffeb3b; padding:2px 5px; outline:2px solid #ff9800; font-weight:bold; border-radius:3px;'>like this</span> in the AFTER column. If you don't see any highlights in a post, no insertions actually applied for that post (check the errors block for why).</p>"]

    for pv in previews:
        plans_html = "".join(
            f"<div class='plan'>"
            f"<b>Insert</b> → <code>{_html.escape(a['target'])}</code> in P{a['para']}<br>"
            f"<div class='old'>{_html.escape(a['old'])}</div>"
            f"<div class='new'>{_html.escape(a['new'])}</div>"
            f"<div class='why'>{_html.escape(a['why'])}</div>"
            f"</div>"
            for a in pv["insertions"]
        )
        errors_html = ""
        if pv["errors"]:
            errors_html = "<div class='errors'><b>Skipped/errors:</b><br>" + "<br>".join(_html.escape(e) for e in pv["errors"]) + "</div>"

        body_parts.append(f"""
        <div class="post">
          <div class="post-header">
            <h2>{_html.escape(pv["slug"])}</h2>
            <div class="stats">
              Insertions applied: <b>{len(pv["insertions"])}</b> |
              Errors/skips: <b>{len(pv["errors"])}</b> |
              Links: {pv["before_links"]} → {pv["after_links"]} ({pv["after_links"] - pv["before_links"]:+d})
            </div>
            {'<div class="no-inserts-warning">⚠ NO INSERTIONS APPLIED to this post. Check errors below.</div>' if len(pv["insertions"]) == 0 else '<div class="success-banner">✓ ' + str(len(pv["insertions"])) + ' insertion(s) applied — highlighted in yellow below.</div>'}
          </div>
          {plans_html}
          {errors_html}
          <div class="diff">
            <div class="diff-col before"><h3>BEFORE</h3>{pv["before"]}</div>
            <div class="diff-col after"><h3>AFTER (new links highlighted yellow)</h3>{pv["after"]}</div>
          </div>
        </div>
        """)

    html = "<!DOCTYPE html><html><head><meta charset='utf-8'>" + css + "</head><body>" + "".join(body_parts) + "</body></html>"
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(html, encoding="utf-8")
    print(f"\n✓ Wrote {a.output}")
    print(f"  Open in a browser to review")


if __name__ == "__main__":
    main()
