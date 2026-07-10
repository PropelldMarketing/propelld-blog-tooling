"""
check_facet_content.py
Audits all 164 facet items for content completeness BEFORE publish.

Outputs an xlsx with one sheet per collection, listing every item and
which required content fields are missing. Use this to drive the
content-writing sprint that needs to happen before publish_facet_items.

USAGE:
  python check_facet_content.py             # prints summary to console
  python check_facet_content.py --output out/facet-content-audit.xlsx

ENV:
  WEBFLOW_API_TOKEN   required
"""

import argparse
import os
import sys
import requests
import pandas as pd

SITE_ID = "63a98d7ca3749777345ba1fd"
COLLECTIONS = {
    "Courses":          "6a0d870f601c9d1e423a0bd2",
    "Lenders":          "6a0d8710807b4792f70f774a",
    "Regions":          "6a0d8718faae524c59d4df50",
    "Exams Accepted":   "66de84c04907156359f7fa2d",
}

REQUIRED_FIELDS = ["hub-title-h1", "hub-intro", "hub-meta-title", "hub-meta-description"]
OPTIONAL_FIELDS = ["hub-featured-image", "companion-facets"]


def list_items(token, collection_id):
    items = []
    offset = 0
    while True:
        r = requests.get(
            f"https://api.webflow.com/v2/collections/{collection_id}/items",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"limit": 100, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        items.extend(d.get("items", []))
        if len(items) >= d.get("pagination", {}).get("total", 0):
            break
        offset += 100
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="out/facet-content-audit.xlsx")
    parser.add_argument("--apply", action="store_true",
                        help="Accepted for workflow-uniformity; read-only script.")
    args = parser.parse_args()

    token = os.environ.get("WEBFLOW_API_TOKEN")
    if not token:
        print("ERROR: WEBFLOW_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    sheets = {}
    for coll_name, coll_id in COLLECTIONS.items():
        print(f"Auditing {coll_name}...")
        items = list_items(token, coll_id)
        rows = []
        for item in items:
            fd = item.get("fieldData", {})
            row = {
                "Name": fd.get("name"),
                "Slug": fd.get("slug"),
                "Status": "Draft" if item.get("isDraft") else "Published",
                "H1 ready": "yes" if fd.get("hub-title-h1") else "MISSING",
                "Intro ready": "yes" if fd.get("hub-intro") else "MISSING",
                "Meta title ready": "yes" if fd.get("hub-meta-title") else "MISSING",
                "Meta desc ready": "yes" if fd.get("hub-meta-description") else "MISSING",
                "Featured image": "yes" if fd.get("hub-featured-image") else "—",
                "Companion facets": "yes" if fd.get("companion-facets") else "—",
                "post-count": fd.get("post-count", 0),
                "Required missing": ", ".join(
                    f for f in REQUIRED_FIELDS if not fd.get(f)
                ) or "—",
                "Ready to publish": "READY" if all(fd.get(f) for f in REQUIRED_FIELDS) else "NO",
            }
            rows.append(row)
        sheets[coll_name] = pd.DataFrame(rows).sort_values(["Ready to publish", "Name"])

    summary_rows = []
    for name, df in sheets.items():
        summary_rows.append({
            "Collection": name,
            "Total items": len(df),
            "Drafts": (df["Status"] == "Draft").sum(),
            "Published": (df["Status"] == "Published").sum(),
            "Ready to publish": (df["Ready to publish"] == "READY").sum(),
            "Need content writing": (df["Ready to publish"] == "NO").sum(),
        })
    summary = pd.DataFrame(summary_rows)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with pd.ExcelWriter(args.output, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="SUMMARY", index=False)
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=False)

    print()
    print(summary.to_string(index=False))
    print(f"\n✓ Full audit saved to {args.output}")


if __name__ == "__main__":
    main()
