"""
onboard_new_posts.py — weekly cron: find newly published blog posts that the
linking system doesn't know yet, register them, and hand them to the planner.

Without this, every new post starts life with no internal links and the
problem this project solved regrows. Runs weekly via automation.yml:
  onboard -> plan_insertions --sources-file -> insert_planned_links

Tier/category are assigned by title keywords (tier T3 default) and recorded
in data/tier-map-additions.csv, which load_tier_map() merges automatically.
The quarterly re-score can fold them into the main tier map properly.

USAGE:
  python scripts/onboard_new_posts.py --dry-run
  python scripts/onboard_new_posts.py --apply

Env: WEBFLOW_API_TOKEN
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS

CATEGORY_RULES = [
    (r"loan|emi|interest|collateral|moratorium|lender|nbfc|finance", "Education Loans"),
    (r"exam|result|cutoff|cut-off|counselling|predictor|admit|syllabus|rank|percentile|answer key", "Exams & Counselling"),
    (r"abroad|usa|uk|canada|germany|australia|ireland|zealand|visa|ielts|gre|toefl|sat", "Study Abroad"),
    (r"credit|cibil|tax|80e|score", "Finance & Credit Education"),
]


def infer_category(title, slug):
    text = f"{title} {slug}".lower()
    for pat, cat in CATEGORY_RULES:
        if re.search(pat, text):
            return cat
    return "Courses & Careers"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--additions", default="data/tier-map-additions.csv")
    p.add_argument("--out-list", default="out/new-posts.txt")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    df = pd.read_excel(a.tier_file) if a.tier_file.endswith(".xlsx") else pd.read_csv(a.tier_file)
    ren = {o: n for o, n in [("URL", "url"), ("Tier", "tier"),
                             ("Category", "category"), ("Title", "title")] if o in df.columns}
    df = df.rename(columns=ren)
    known = set(df["url"].astype(str).str.rstrip("/"))
    add_p = Path(a.additions)
    if add_p.exists():
        known |= set(pd.read_csv(add_p)["url"].astype(str).str.rstrip("/"))

    client = WebflowClient()
    new_rows = []
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        fd = item.get("fieldData", {})
        slug = str(fd.get("slug", "") or "")
        if not slug:
            continue
        url = f"/site/blog/{slug}"
        if url in known:
            continue
        title = str(fd.get("name", "") or slug.replace("-", " "))
        new_rows.append({"url": url, "tier": "T3",
                         "category": infer_category(title, slug),
                         "title": title})

    print(f"New posts found: {len(new_rows)}")
    for r in new_rows[:20]:
        print(f"  {r['url']} [{r['category']}]")
    if a.dry_run or not new_rows:
        Path(a.out_list).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out_list).write_text("")
        return

    add = pd.DataFrame(new_rows)
    if add_p.exists():
        add = pd.concat([pd.read_csv(add_p), add], ignore_index=True)
    add_p.parent.mkdir(parents=True, exist_ok=True)
    add.to_csv(add_p, index=False)
    Path(a.out_list).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_list).write_text("\n".join(r["url"] for r in new_rows) + "\n")
    print(f"✓ Registered {len(new_rows)} posts in {a.additions}")
    print(f"✓ Wrote {a.out_list} for the planner")


if __name__ == "__main__":
    main()
