"""
snapshot.py -- back up every blog post's HTML body content to disk.

Fetches all Blog Posts from Webflow, saves each as a JSON file containing
both body halves (post-body + post-body-2nd-half) + a hash for tamper
detection. Writes a manifest.json listing all snapshotted posts.

Used before any bulk edit — this is the rollback substrate for rollback.py.

USAGE:
  python scripts/snapshot.py                    # dry-run (default)
  python scripts/snapshot.py --apply            # actually snapshot
  python scripts/snapshot.py --apply --out out/snapshots/

Runs from Fayik's terminal OR via GitHub Actions (manual dispatch).
When run via GitHub Actions, the out/snapshots/ folder is auto-uploaded
as an artifact you can download from the workflow run page.

Env: WEBFLOW_API_TOKEN required
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.snapshots import snapshot_all_blogs
from lib.webflow_client import WebflowClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="out/snapshots",
                        help="Directory to write snapshots into (default: out/snapshots)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write files. Default is dry-run (counts only).")
    args = parser.parse_args()

    print(f"Snapshot destination: {args.out}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    client = WebflowClient()
    print("Connecting to Webflow, fetching all blog posts...")
    print("(this may take 5-10 minutes for ~1,377 posts due to rate limiting)")
    print()

    snap_path, manifest = snapshot_all_blogs(args.out, client, dry_run=not args.apply)

    print()
    print("=" * 60)
    print(f"Snapshot summary")
    print("=" * 60)
    print(f"  Snapshot dir: {snap_path}")
    print(f"  Posts covered: {len(manifest)}")
    if args.apply:
        print(f"  Files written: {len(manifest)} JSONs + manifest.json")
        print()
        print("To download this snapshot to your Google Drive:")
        print("  1. If running via GitHub Actions: download the workflow artifact")
        print("  2. Extract the zip")
        print(f"  3. Move the {Path(snap_path).name} folder to your propelld-blog-snapshots Drive folder")
        print()
        print(f"To restore any post later: rollback.py --from-snapshot {snap_path} --apply")
    else:
        print(f"  DRY-RUN: no files written. Run with --apply to save.")


if __name__ == "__main__":
    main()
