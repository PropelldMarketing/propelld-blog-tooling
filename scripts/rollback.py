"""
rollback.py -- restore blog post bodies from a snapshot.

USAGE:
  # Full rollback:
  python scripts/rollback.py --from-snapshot snapshots/2026-06-15/ --apply

  # Partial:
  python scripts/rollback.py --from-snapshot snapshots/2026-06-15/ \
      --slugs aakash-institute-fee-structure,allen-kota-fees --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse, json, sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient
from lib.snapshots import restore_post

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from-snapshot", required=True)
    p.add_argument("--slugs", default=None)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    snap_dir = Path(a.from_snapshot)
    if not snap_dir.exists():
        print(f"ERROR: {snap_dir} not found")
        sys.exit(1)

    manifest_p = snap_dir / "manifest.json"
    if manifest_p.exists():
        m = json.load(open(manifest_p))
        all_slugs = [it["slug"] for it in m["items"]]
    else:
        all_slugs = [p.stem for p in snap_dir.glob("*.json") if p.stem != "manifest"]

    if a.slugs:
        targets = [s for s in a.slugs.split(",") if s in all_slugs]
        missing = [s for s in a.slugs.split(",") if s not in all_slugs]
        if missing:
            print(f"WARN: {len(missing)} slugs not in snapshot")
    else:
        targets = all_slugs

    print(f"Snapshot: {snap_dir}")
    print(f"Slugs to restore: {len(targets)}")
    if not a.apply:
        print(f"\nDRY-RUN. Would restore {len(targets)} posts.")
        print(f"First 10: {targets[:10]}")
        return

    client = WebflowClient()
    results = []
    for i, slug in enumerate(targets, 1):
        try:
            r = restore_post(str(snap_dir), slug, client=client, dry_run=False)
            results.append(r)
            if i % 25 == 0:
                print(f"  {i}/{len(targets)}")
        except Exception as e:
            results.append({"slug": slug, "error": str(e)})
            print(f"  ERROR {slug}: {e}")

    Path("out").mkdir(exist_ok=True)
    pd.DataFrame(results).to_csv("out/rollback-log.csv", index=False)
    print(f"\n✓ Restored {len([r for r in results if r.get('restored')])} posts")

if __name__ == "__main__":
    main()
