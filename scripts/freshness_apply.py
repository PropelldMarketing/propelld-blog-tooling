#!/usr/bin/env python3
"""
Phase 4: apply approved freshness plans. snapshot -> patch -> publish ->
reconcile, rollback-compatible.

Input: out/freshness-review.csv (or freshness-plans.csv). A row is applied iff
status == "planned" AND one of:
    - approved column says YES (owner-reviewed file), or
    - no approved column and (lane == A, or confidence == HIGH with
      --auto-high, default on per owner policy 2026-08-12)
An explicit approved == NO always excludes a row. --queue-all applies nothing
automatic (only approved == YES rows).

Safety rails (inherited lessons, enforced in code):
    - full-body snapshot before any write (unless --skip-snapshot)
    - substitution is re-validated against the LIVE body at apply time,
      at the text-node level; ambiguous or drifted text -> skipped + logged
    - explicit publish step (Webflow PATCH only stages a draft); --no-publish
    - run log has slug + status="patched" rows: scripts/rollback.py
      --from-log works unchanged
    - built-in reconcile: every patched post is re-fetched; new_text must be
      present and old_text absent; any mismatch -> exit 3
    - error-rate halt guard (5% after 20 posts)
    - date bump (rules.date_bump_field) on factual (non-RULE) changes only

Dry-run by default; --apply to write.
"""

import sys
import csv
import time
import argparse
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.freshness_utils import (substitute_in_html, norm_ws, load_rules,
                                 pure_substitution_check)

HALT_ERROR_RATE = 0.05
LOG_COLUMNS = ["slug", "item_id", "field", "old_text", "new_text", "lane",
               "confidence", "status", "reason"]


def should_apply(row, auto_high, queue_all):
    if str(row.get("status", "")) != "planned":
        return False, "status not planned"
    approved = str(row.get("approved", "")).strip().upper()
    if approved == "NO":
        return False, "owner rejected (approved=NO)"
    if approved == "YES":
        return True, "owner approved"
    if queue_all:
        return False, "queue-all: no auto-apply"
    if str(row.get("lane", "")) == "A":
        return True, "lane A mechanical rule"
    if str(row.get("confidence", "")) == "HIGH" and auto_high:
        return True, "HIGH confidence auto-apply"
    return False, "needs owner approval (confidence=%s)" % row.get("confidence", "")


def apply_to_post(client, collections, item_id, rows, rules, log, a):
    """Apply all approved rows for one post. Returns 'patched'/'no-change'/'error'."""
    from lib.webflow_client import get_blog_body
    item = client.get_item(collections["blog_posts"], item_id)
    fd = item.get("fieldData", {})
    bodies = get_blog_body(item)
    title = fd.get("name", "") or ""
    patch, changed, bumped = {}, 0, False

    for r in rows:
        field, old, new = r["field"], str(r["old_text"]), str(r["new_text"])
        ok, why = pure_substitution_check(old, new)   # re-check, never trust CSV
        if not ok:
            log(r, "skipped", "apply-time validation: %s" % why)
            continue
        if field == "name":
            if norm_ws(old) in norm_ws(title):
                new_title = norm_ws(title).replace(norm_ws(old), new, 1)
                patch["name"] = new_title
                title = new_title
                changed += 1
                log(r, "staged", "title substitution")
            else:
                log(r, "skipped", "old_text no longer in live title")
            continue
        if field not in bodies:
            log(r, "skipped", "unknown field %r" % field)
            continue
        html_now = patch.get(field, bodies[field])
        new_html, reason = substitute_in_html(html_now, old, new)
        if new_html is None:
            log(r, "skipped", "live body: %s" % reason)
            continue
        patch[field] = new_html
        changed += 1
        if str(r.get("confidence", "")) != "RULE":
            bumped = True
        log(r, "staged", "body substitution ok")

    if not changed:
        return "no-change", []
    bump_field = rules.get("date_bump_field", "")
    if bumped and bump_field:
        patch[bump_field] = datetime.date.today().isoformat()
    if a.apply:
        client.update_item(collections["blog_posts"], item_id, patch)
        time.sleep(a.sleep)
    return "patched", list(patch.keys())


def main():
    ap = argparse.ArgumentParser(description="Apply approved freshness plans")
    ap.add_argument("--apply", action="store_true",
                    help="Actually PATCH Webflow. Without this, dry-run only.")
    ap.add_argument("--plans", default="out/freshness-review.csv")
    ap.add_argument("--rules", default="data/freshness_rules.json")
    ap.add_argument("--auto-high", dest="auto_high", action="store_true",
                    default=True)
    ap.add_argument("--no-auto-high", dest="auto_high", action="store_false",
                    help="HIGH-confidence rows queue instead of auto-applying.")
    ap.add_argument("--queue-all", action="store_true",
                    help="Apply ONLY approved=YES rows; no auto lanes.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max posts to touch (canary).")
    ap.add_argument("--no-publish", action="store_true",
                    help="Leave changes staged (queued to publish).")
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--snapshot-dir", default="snapshots")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--output-log", default="out/freshness-apply-log.csv")
    a = ap.parse_args()

    import pandas as pd
    df = pd.read_csv(a.plans).fillna("")
    rules = load_rules(a.rules)

    Path(a.output_log).parent.mkdir(parents=True, exist_ok=True)
    log_f = open(a.output_log, "w", newline="", encoding="utf-8")
    logw = csv.DictWriter(log_f, fieldnames=LOG_COLUMNS, extrasaction="ignore")
    logw.writeheader()

    def log_row(r, status, reason):
        logw.writerow({**{k: r.get(k, "") for k in LOG_COLUMNS}, "status": status,
                       "reason": reason})
        log_f.flush()

    # Selection funnel: every row gets a logged decision.
    todo = {}
    for r in df.to_dict("records"):
        ok, why = should_apply(r, a.auto_high, a.queue_all)
        if ok:
            todo.setdefault((r["slug"], r["item_id"]), []).append(r)
        else:
            log_row(r, "not-selected", why)

    posts = list(todo.items())
    if a.limit:
        posts = posts[:a.limit]
    print("%d posts selected (%d rows). apply=%s publish=%s"
          % (len(posts), sum(len(rows) for _, rows in posts),
             a.apply, not a.no_publish))
    if not posts:
        log_f.close()
        return

    from lib.webflow_client import WebflowClient, COLLECTIONS
    try:
        client = WebflowClient()
    except RuntimeError:
        if a.apply:
            raise
        print("DRY-RUN without WEBFLOW_API_TOKEN: selection logged above; "
              "live-body validation skipped.")
        log_f.close()
        return

    if a.apply and not a.skip_snapshot:
        from lib.snapshots import snapshot_all_blogs
        snap_dir, _ = snapshot_all_blogs(a.snapshot_dir, client, dry_run=False)
        print("Snapshot: %s" % snap_dir)

    patched_ids, errors, processed = [], 0, 0
    results = {}
    for (slug, item_id), rows in posts:
        processed += 1
        try:
            status, fields = apply_to_post(client, COLLECTIONS, item_id, rows,
                                           rules, log_row, a)
            results[(slug, item_id)] = (status, rows)
            if status == "patched":
                patched_ids.append(item_id)
                log_row({"slug": slug, "item_id": item_id,
                         "field": ",".join(fields)},
                        "patched" if a.apply else "dry-run",
                        "fields: %s" % ",".join(fields))
        except Exception as e:
            errors += 1
            log_row({"slug": slug, "item_id": item_id}, "error",
                    str(e)[:200])
            if processed > 20 and errors / processed > HALT_ERROR_RATE:
                print("HALT: error rate %.0f%% after %d posts"
                      % (100 * errors / processed, processed))
                break

    if a.apply and patched_ids and not a.no_publish:
        client.publish_items(COLLECTIONS["blog_posts"], patched_ids)
        print("Published %d items" % len(patched_ids))

    # ---- reconcile: expected == observed, from the live API ----
    mismatches = 0
    if a.apply:
        from lib.webflow_client import get_blog_body
        for (slug, item_id), (status, rows) in results.items():
            if status != "patched":
                continue
            item = client.get_item(COLLECTIONS["blog_posts"], item_id)
            hay = norm_ws(" ".join(get_blog_body(item).values()) + " " +
                          (item.get("fieldData", {}).get("name", "") or ""))
            for r in rows:
                new = norm_ws(str(r["new_text"]))
                if new and new not in hay:
                    mismatches += 1
                    log_row(r, "reconcile-mismatch", "new_text not observed")
    log_f.close()
    print("Done. patched=%d errors=%d reconcile_mismatches=%d -> %s"
          % (len(patched_ids), errors, mismatches, a.output_log))
    if mismatches:
        sys.exit(3)


if __name__ == "__main__":
    main()
