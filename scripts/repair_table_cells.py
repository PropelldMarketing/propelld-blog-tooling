"""
repair_table_cells.py — restore table-cell text wiped by the CTA-block
removal bug (06-Aug): link-only cells like <td><p><a>Credila</a></p></td>
lost their text when the duplicate link was killed.

For every blog post: compares each LIVE table against the same table in the
pre-cleanup snapshot (2026-07-30). Any live cell that is now EMPTY where the
snapshot had text gets that text restored as PLAIN text (no link — the link
removal itself was intended). Only empty cells are ever touched, so the
script cannot damage anything. Skips tables whose shape changed.

USAGE (workflow: set snapshot_run_id=30544503579 to auto-restore snapshots):
  python scripts/repair_table_cells.py --from-snapshot snapshots/2026-07-30/            # dry-run
  python scripts/repair_table_cells.py --from-snapshot snapshots/2026-07-30/ --apply

Env: WEBFLOW_API_TOKEN
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body

FIELDS = ["post-body", "post-body-2nd-half"]


def restore_cells(live_html, snap_html):
    """Return (new_html, n_restored, notes). Only fills EMPTY live cells."""
    if not live_html or "<t" not in live_html or not snap_html:
        return live_html, 0, []
    live = BeautifulSoup(live_html, "html.parser")
    snap = BeautifulSoup(snap_html, "html.parser")
    lt, st = live.find_all("table"), snap.find_all("table")
    notes, restored = [], 0
    for ti in range(min(len(lt), len(st))):
        lrows = lt[ti].find_all("tr")
        srows = st[ti].find_all("tr")
        if len(lrows) != len(srows):
            notes.append(f"table{ti}: row-count changed, skipped")
            continue
        for ri, (lr, sr) in enumerate(zip(lrows, srows)):
            lc = lr.find_all(["td", "th"])
            sc = sr.find_all(["td", "th"])
            if len(lc) != len(sc):
                notes.append(f"table{ti}r{ri}: cell-count changed, skipped")
                continue
            for ci, (lcell, scell) in enumerate(zip(lc, sc)):
                stext = scell.get_text(" ", strip=True)
                if not stext:
                    continue                      # was empty before too
                ltext = lcell.get_text(" ", strip=True)
                # Full-restore policy (07-Aug): bring back the ORIGINAL cell
                # content including its link. Eligible cells are those the
                # bug touched: now empty, or plain-text-restored earlier
                # (same text, no link). Any other live content: never touch.
                if lcell.find("a") is not None:
                    continue
                norm = lambda s: " ".join(str(s).split()).lower()
                if ltext and norm(ltext) != norm(stext):
                    continue
                if not scell.find("a") and not ltext:
                    # plain-text cell wiped: restore text only
                    pass
                elif not scell.find("a"):
                    continue                      # already identical plain text
                lcell.clear()
                frag = BeautifulSoup(scell.decode_contents(), "html.parser")
                for ch in list(frag.contents):
                    lcell.append(ch)
                restored += 1
    return str(live), restored, notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from-snapshot", default="snapshots/2026-07-30/")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--slugs", default=None)
    p.add_argument("--sleep", type=float, default=0.15)
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--output-log", default="out/repair-table-cells-log.csv")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    snap_dir = Path(a.from_snapshot)
    if not snap_dir.exists():
        print(f"ERROR: {snap_dir} not found — set snapshot_run_id=30544503579 "
              f"in the workflow so the pre-cleanup snapshot is restored")
        sys.exit(1)

    client = WebflowClient()
    want = set(a.slugs.split(",")) if a.slugs else None
    logs, patched_ids, processed = [], [], 0
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        slug = str(fd.get("slug", "") or "")
        if not slug or (want and slug not in want):
            continue
        snap_p = snap_dir / f"{slug}.json"
        if not snap_p.exists():
            continue
        bodies = get_blog_body(item)
        if "<t" not in (bodies.get("post-body", "") + bodies.get("post-body-2nd-half", "")):
            continue
        snap = json.load(open(snap_p))
        patch, total, all_notes = {}, 0, []
        for f in FIELDS:
            new_html, n, notes = restore_cells(bodies.get(f, ""), snap.get(f, ""))
            all_notes += notes
            if n > 0:
                patch[f] = new_html
                total += n
        if total == 0:
            continue
        processed += 1
        logs.append({"slug": slug, "cells_restored": total,
                     "notes": "; ".join(all_notes)[:200],
                     "status": "would-patch" if not a.apply else "patched"})
        if a.apply:
            client.update_item(COLLECTIONS["blog_posts"], item["id"], patch)
            patched_ids.append(item["id"])
            time.sleep(a.sleep)
        if processed % 25 == 0:
            print(f"  [{processed}] posts needing repair so far...")

    if a.apply and patched_ids and not a.no_publish:
        print(f"Publishing {len(patched_ids)} repaired items live...")
        client.publish_items(COLLECTIONS["blog_posts"], patched_ids)
        print("  ✓ Published")

    Path(a.output_log).parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(logs)
    df.to_csv(a.output_log, index=False)
    total_cells = int(df["cells_restored"].sum()) if len(df) else 0
    print(f"\n{'APPLIED' if a.apply else 'DRY-RUN'}: {len(df)} posts, "
          f"{total_cells} cells restored")


if __name__ == "__main__":
    main()
