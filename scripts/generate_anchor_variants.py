"""
generate_anchor_variants.py — expand the anchor library to (nearly) every
blog target so the wrap scanner has phrases to find for all of them.

The starter library covers 119 destinations; the other ~1,200 blog posts
had no curated variants, which limits wrap-around hits (the safest, highest
quality insertion type). This script asks Haiku for 3 natural anchor
variants per uncovered target and merges everything into
data/anchor_library_full.json (starter entries always win).

Cost: ~1,200 targets in batches of 25 ≈ 50 small Haiku calls ≈ $1-2.

USAGE:
  python scripts/generate_anchor_variants.py --dry-run
  python scripts/generate_anchor_variants.py --apply
  python scripts/generate_anchor_variants.py --apply --limit 100

Env: ANTHROPIC_API_KEY
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.link_utils import normalize_url

try:
    import anthropic
except ImportError:
    anthropic = None

MODEL = "claude-haiku-4-5"
BATCH = 25

PROMPT = """For each blog post below, write exactly 3 natural anchor-text variants a writer would use to link to it from another article.

Rules per variant:
- 2-6 words, plain noun phrase, no quotes, no "click here"/"read more"
- Must contain at least one distinctive word from the post's own title
- Natural inline English (things like "CLAT 2026 exam dates", "SBI loan moratorium rules")
- No years unless the year is essential to the topic
- The 3 variants must differ from each other meaningfully

POSTS:
{items}

Return ONLY JSON: {{"variants": {{"<url>": ["v1", "v2", "v3"], ...}}}}"""


def load_tiers(path):
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
    ren = {o: n for o, n in [("URL", "url"), ("Title", "title"),
                             ("Category", "category")] if o in df.columns}
    df = df.rename(columns=ren)
    df["url"] = df["url"].astype(str).str.rstrip("/")
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--starter", default="data/anchor_library_starter.json")
    p.add_argument("--output", default="data/anchor_library_full.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    starter = json.load(open(a.starter))
    covered = {normalize_url(k) for k in starter.get("destinations", {})}
    df = load_tiers(a.tier_file)
    todo = [(r.url, str(getattr(r, "title", "") or ""))
            for r in df.itertuples()
            if r.url.startswith("/site/blog/") and r.url not in covered]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(covered)} covered by starter | {len(todo)} targets to generate")
    if a.dry_run:
        items = "\n".join(f"- {u} | title: {t}" for u, t in todo[:BATCH])
        print(PROMPT.format(items=items)[:2000])
        return
    if anthropic is None:
        print("ERROR: pip install anthropic")
        sys.exit(1)
    ac = anthropic.Anthropic()

    generated = {}
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        items = "\n".join(f"- {u} | title: {t}" for u, t in batch)
        try:
            resp = ac.messages.create(
                model=a.model, max_tokens=3000,
                messages=[{"role": "user", "content": PROMPT.format(items=items)}])
            text = resp.content[0].text
            start = text.find("{")
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            for url, variants in obj.get("variants", {}).items():
                url = normalize_url(url)
                clean = []
                for v in variants:
                    v = str(v).strip().strip('"')
                    if 2 <= len(v.split()) <= 6 and "click here" not in v.lower():
                        clean.append(v)
                if clean:
                    generated[url] = clean[:3]
        except Exception as e:
            print(f"  batch {i//BATCH}: error {str(e)[:120]}")
        if (i // BATCH) % 5 == 4:
            print(f"  [{i + len(batch)}/{len(todo)}] generated")
        time.sleep(a.sleep)

    merged = dict(starter)
    dest = dict(starter.get("destinations", {}))
    for url, variants in generated.items():
        if url not in {normalize_url(k) for k in dest}:
            dest[url] = variants
    merged["destinations"] = dest
    merged["_generated_note"] = (f"{len(generated)} destinations auto-generated "
                                 f"by generate_anchor_variants.py ({a.model}); "
                                 "starter entries take precedence.")
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    json.dump(merged, open(a.output, "w"), indent=1, ensure_ascii=False)
    print(f"✓ Wrote {a.output}: {len(dest)} total destinations "
          f"({len(generated)} new)")


if __name__ == "__main__":
    main()
