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


def highlight_in_context(context, matched):
    """Show the flagged value inside its surrounding sentence."""
    ctx, m = html.escape(norm_ws(str(context))), html.escape(norm_ws(str(matched)))
    return ctx.replace(m, "<mark>%s</mark>" % m, 1) if m and m in ctx else ctx


def render_post_group(parts, slug, grp, row_renderer, headers):
    url = grp.iloc[0]["url"]
    parts.append("<div class='post'><h2>%s</h2><div class='meta'>"
                 "<a href='https://propelld.com%s'>%s</a> · tier %s</div>"
                 % (html.escape(slug), html.escape(str(url)),
                    html.escape(str(url)), html.escape(str(grp.iloc[0]["tier"]))))
    parts.append("<table><tr>%s</tr>" %
                 "".join("<th>%s</th>" % h for h in headers))
    for r in grp.itertuples():
        parts.append(row_renderer(r))
    parts.append("</table></div>")


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
    # Calibration: reviewer sets approved=NO and writes WHY here. The flag
    # reasons drive validator/rule tightening and autonomy promotions.
    actionable["flag_reason"] = ""
    Path(a.review_out).parent.mkdir(parents=True, exist_ok=True)
    actionable.to_csv(a.review_out, index=False)

    # Two sections: concrete before/after proposals first, then unverified
    # flags (no proposed value) rendered with their surrounding context.
    # 2026-08-13 owner feedback: mixing them made 70-80% of rows look blank.
    has_change = actionable["new_text"].astype(str).str.strip() != ""
    proposals = actionable[has_change]
    unverified = actionable[~has_change]

    def badges_for(r):
        extra = ""
        tier = str(getattr(r, "source_tier", "") or "")
        if tier and tier != "official":
            extra += "<span class='badge LOW'>%s source</span>" % html.escape(tier)
        if str(getattr(r, "priority", "") or "") == "high":
            extra += "<span class='badge HIGH'>priority</span>"
        return ("<span class='badge %s'>%s</span>"
                "<span class='badge %s'>%s</span>"
                "<span class='badge'>%s/%s</span>%s"
                % (r.status, r.status, r.confidence or "LOW",
                   r.confidence or "?", html.escape(str(r.field)),
                   html.escape(str(r.location)), extra))

    def proposal_row(r):
        src = ("<div class='src'><a href='%s'>source</a>: %s</div>"
               % (html.escape(str(r.source_url)),
                  html.escape(str(r.evidence_quote)[:220]))
               if r.source_url else "")
        return ("<tr><td>%s</td><td>%s</td><td>%s<div class='reason'>%s</div>%s</td></tr>"
                % (html.escape(str(r.old_text)),
                   highlight(str(r.old_text), str(r.new_text)),
                   badges_for(r), html.escape(str(r.reasoning)), src))

    def unverified_row(r):
        return ("<tr><td>%s</td><td>%s<div class='reason'>%s</div></td></tr>"
                % (highlight_in_context(r.context, r.matched_text),
                   badges_for(r), html.escape(str(r.reasoning))))

    parts = ["<html><head><meta charset='utf-8'><style>%s</style></head><body>" % CSS,
             "<h1>Freshness review</h1>",
             "<p>%d proposed changes across %d posts, plus %d flagged-but-"
             "unverified items. Edit <code>approved</code> (YES/NO) and "
             "<code>flag_reason</code> in <code>%s</code>, then run "
             "freshness_apply.</p>"
             % (len(proposals), proposals["slug"].nunique(), len(unverified),
                html.escape(a.review_out))]

    parts.append("<h1>1. Proposed changes (yellow = new text)</h1>")
    for slug, grp in proposals.groupby("slug", sort=True):
        render_post_group(parts, slug, grp, proposal_row,
                          ["Before", "After", "Why"])

    parts.append("<h1>2. Flagged but unverified (no proposed value)</h1>"
                 "<p>The scanner flagged these as possibly stale but no "
                 "official source could confirm a current value, so no change "
                 "is proposed. Yellow = the flagged value in its context. "
                 "Nothing here will be applied; review is optional and these "
                 "shrink as sources are added.</p>")
    for slug, grp in unverified.groupby("slug", sort=True):
        render_post_group(parts, slug, grp, unverified_row,
                          ["Flagged text in context", "Why"])
    parts.append("</body></html>")

    Path(a.html_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.html_out).write_text("\n".join(parts), encoding="utf-8")
    print("Wrote %s (%d rows) and %s" % (a.html_out, len(actionable), a.review_out))


if __name__ == "__main__":
    main()
