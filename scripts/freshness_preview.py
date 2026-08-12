#!/usr/bin/env python3
"""
Side-by-side before/after preview + review CSV for freshness plans.

Input:  out/freshness-plans.csv
Output: out/freshness-preview.html  (yellow highlight = changed text,
        grouped by post, with lane/confidence/reasoning/source links)
        out/freshness-review.csv    (plans + `approved` column; YES prefilled
        for Lane A and HIGH rows, blank otherwise — the owner edits this file
        and feeds it to freshness_apply.py)

--apply accepted for workflow uniformity; script is always read-only
with respect to Webflow.
"""

import sys
import html
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.freshness_utils import changed_new_tokens, norm_ws

CSS = """
body{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}
.post{border:1px solid #ddd;border-radius:8px;margin:18px 0;padding:14px}
.post h2{margin:0 0 4px;font-size:17px}
.meta{color:#666;font-size:12px;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{border:1px solid #eee;padding:8px;vertical-align:top;text-align:left}
mark{background:#ffec80}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;margin-right:6px}
.A{background:#d4f7d4}.HIGH{background:#d4f7d4}.MEDIUM{background:#fff3c4}
.LOW{background:#ffd9d9}.RULE{background:#d4e8f7}
.queued{background:#fff3c4}.planned{background:#e8f7e8}
.reason{color:#444;font-size:12px;margin-top:4px}
.src{font-size:12px}
"""


def highlight(old, new):
    toks = set(changed_new_tokens(old, new))
    out = []
    for t in norm_ws(new).split():
        out.append("<mark>%s</mark>" % html.escape(t) if t in toks
                   else html.escape(t))
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser(description="Freshness preview builder")
    ap.add_argument("--apply", action="store_true",
                    help="Accepted for workflow uniformity; always read-only.")
    ap.add_argument("--plans", default="out/freshness-plans.csv")
    ap.add_argument("--html-out", default="out/freshness-preview.html")
    ap.add_argument("--review-out", default="out/freshness-review.csv")
    a = ap.parse_args()

    import pandas as pd
    df = pd.read_csv(a.plans).fillna("")
    actionable = df[df["status"].isin(["planned", "queued"])].copy()

    actionable["approved"] = [
        "YES" if (r.lane == "A" or r.confidence == "HIGH") and r.status == "planned"
        else "" for r in actionable.itertuples()]
    Path(a.review_out).parent.mkdir(parents=True, exist_ok=True)
    actionable.to_csv(a.review_out, index=False)

    parts = ["<html><head><meta charset='utf-8'><style>%s</style></head><body>" % CSS,
             "<h1>Freshness review — %d changes across %d posts</h1>"
             % (len(actionable), actionable["slug"].nunique()),
             "<p>Yellow = changed text. Edit <code>approved</code> in "
             "<code>%s</code> (YES/NO), then run freshness_apply.</p>"
             % html.escape(a.review_out)]
    for slug, grp in actionable.groupby("slug", sort=True):
        url = grp.iloc[0]["url"]
        parts.append("<div class='post'><h2>%s</h2><div class='meta'>"
                     "<a href='https://propelld.com%s'>%s</a> · tier %s</div>"
                     % (html.escape(slug), html.escape(str(url)),
                        html.escape(str(url)), html.escape(str(grp.iloc[0]["tier"]))))
        parts.append("<table><tr><th style='width:38%'>Before</th>"
                     "<th style='width:38%'>After</th><th>Why</th></tr>")
        for r in grp.itertuples():
            badges = ("<span class='badge %s'>%s</span>"
                      "<span class='badge %s'>%s</span>"
                      "<span class='badge'>%s/%s</span>"
                      % (r.status, r.status, r.confidence or "LOW",
                         r.confidence or "?", html.escape(str(r.field)),
                         html.escape(str(r.location))))
            src = ("<div class='src'><a href='%s'>source</a>: %s</div>"
                   % (html.escape(str(r.source_url)),
                      html.escape(str(r.evidence_quote)[:220]))
                   if r.source_url else "")
            parts.append(
                "<tr><td>%s</td><td>%s</td><td>%s<div class='reason'>%s</div>%s</td></tr>"
                % (html.escape(str(r.old_text)),
                   highlight(str(r.old_text), str(r.new_text)) if r.new_text
                   else "<i>needs human: no verified value</i>",
                   badges, html.escape(str(r.reasoning)), src))
        parts.append("</table></div>")
    parts.append("</body></html>")

    Path(a.html_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.html_out).write_text("\n".join(parts), encoding="utf-8")
    print("Wrote %s (%d rows) and %s" % (a.html_out, len(actionable), a.review_out))


if __name__ == "__main__":
    main()
