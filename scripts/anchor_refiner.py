"""
anchor_refiner.py -- context-aware anchor refinement pass (Option B).

Reads link-recommendations.csv from link_recommender.py, fetches each source
post's body from Webflow, then asks Claude Haiku to pick or craft a natural
anchor phrase that fits the source's content and reads well in prose.

Design:
  - Skips T1P + T1 sources (Vaishali edits those manually in Phase 4).
  - For T2/T3/T4 sources (which auto-apply), it refines each rec's anchor.
  - Batches all targets for one source into ONE Haiku call (saves API cost).
  - Adds `refined_anchor` column; keeps original `anchor_text` as fallback.
  - Idempotent — writes to a new file; safe to re-run.
  - bulk_apply_links.py should prefer `refined_anchor` if present, else `anchor_text`.

USAGE:
  # Dry-run first 10 sources (checks setup, no API cost):
  python scripts/anchor_refiner.py --input out/link-recommendations.csv \
      --output out/link-recommendations-refined.csv --limit 10 --dry-run

  # Full refinement:
  python scripts/anchor_refiner.py --input out/link-recommendations.csv \
      --output out/link-recommendations-refined.csv --apply

  # Filter to specific tier(s):
  python scripts/anchor_refiner.py --input out/link-recommendations.csv \
      --output out/link-recommendations-refined.csv --tier-filter T3,T4 --apply

Env:
  WEBFLOW_API_TOKEN  -- required (fetch source bodies)
  ANTHROPIC_API_KEY  -- required (Claude Haiku)

Cost estimate:
  ~1,300 sources × 1 Haiku call each. At ~1,500 input tokens + ~300 output
  per call = ~1.95M input + 390K output tokens. Haiku pricing (~$0.25/M in,
  $1.25/M out) = ~$0.50 total.
"""

import argparse, json, os, re, sys, time
from html.parser import HTMLParser
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body

try:
    import anthropic
except ImportError:
    print("ERROR: `anthropic` package not installed. Run: pip install anthropic")
    sys.exit(1)

MODEL = "claude-haiku-4-5-20251001"
MAX_BODY_CHARS = 6000  # trim body to fit context economically
SKIP_TIERS_DEFAULT = {"T1P", "T1"}  # Vaishali edits these anyway


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self.parts.append(data)


def html_to_text(html):
    if not html:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


def load_anchor_library(path):
    return json.load(open(path)).get("destinations", {})


def build_prompt(source_slug, source_title, source_text, recs, library):
    """
    Builds a single Haiku prompt that gets refined anchors for every target
    on this source post. Returns a JSON-list back.
    """
    body = source_text[:MAX_BODY_CHARS]
    lines = [
        f"You are refining internal-link anchor text for a Propelld blog post.",
        f"",
        f"SOURCE POST:",
        f"  Title: {source_title}",
        f"  Slug: {source_slug}",
        f"  Excerpt (first {MAX_BODY_CHARS} chars):",
        f"  ---",
        body,
        f"  ---",
        f"",
        f"For each target URL below, pick or lightly rephrase an anchor that would",
        f"read naturally when inserted into a sentence in the source post above.",
        f"Guidelines:",
        f"  - Prefer 3-7 word phrases; avoid meta-title-style anchors.",
        f"  - Match the source post's tone (informative, Indian audience, plain English).",
        f"  - Never use 'click here', 'read more', 'learn more', 'this article'.",
        f"  - If a suggested variant fits well, USE IT verbatim (return exactly that).",
        f"  - If no variant fits, write a new short phrase that would fit the source's context.",
        f"  - Prefer partial-match anchors over exact-keyword anchors.",
        f"",
        f"TARGETS ({len(recs)}):",
    ]
    for i, r in enumerate(recs, 1):
        variants = library.get(r["target_url"], [])
        vlist = " | ".join(variants[:5]) if variants else "(no library entries — please craft one)"
        lines.append(f"  {i}. Target: {r['target_url']}")
        lines.append(f"     Rationale: {r['rationale']}  Position: {r['suggested_position']}")
        lines.append(f"     Current anchor: {r['anchor_text']}")
        lines.append(f"     Library variants: {vlist}")
        lines.append("")

    lines.append("OUTPUT:")
    lines.append("Return a JSON array of exactly " + str(len(recs)) + " strings, one anchor per target, in order. No other text.")
    lines.append('Example: ["compare education loan interest", "check TNEA cutoff", "SBI education loan"]')
    return "\n".join(lines)


def call_haiku(client, prompt):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # extract JSON array robustly
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array in response: {text[:200]}")
    arr = json.loads(text[start:end + 1])
    if not isinstance(arr, list):
        raise ValueError("Response not a list")
    return [str(x).strip() for x in arr]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="data/link-recommendations.csv", help="link-recommendations.csv from link_recommender.py")
    p.add_argument("--output", default="out/link-recommendations-refined.csv")
    p.add_argument("--anchor-library",
                   default=str(Path(__file__).parent.parent / "data" / "anchor_library_starter.json"))
    p.add_argument("--tier-filter", default=None, help="Comma-separated source tiers to refine (default: all except T1P,T1)")
    p.add_argument("--skip-tiers", default="T1P,T1", help="Source tiers to skip (default: T1P,T1 — reviewed by Vaishali)")
    p.add_argument("--limit", type=int, default=0, help="Limit to first N sources (0 = no limit)")
    p.add_argument("--dry-run", action="store_true", help="Don't call the API, just show what would happen")
    p.add_argument("--apply", action="store_true", help="Required to actually run the refinement")
    p.add_argument("--sleep", type=float, default=0.1, help="Sleep between API calls (seconds)")
    a = p.parse_args()

    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    print(f"Loading recommendations from {a.input}...")
    df = pd.read_csv(a.input) if a.input.endswith(".csv") else pd.read_excel(a.input)
    print(f"  Total recs: {len(df):,}")

    # Filter
    if a.tier_filter:
        allowed = set(a.tier_filter.split(","))
        df = df[df["source_tier"].isin(allowed)]
        print(f"  After tier-filter ({a.tier_filter}): {len(df):,}")
    else:
        skip = set(a.skip_tiers.split(",")) if a.skip_tiers else set()
        if skip:
            df = df[~df["source_tier"].isin(skip)]
            print(f"  After skip ({a.skip_tiers}): {len(df):,}")

    if len(df) == 0:
        print("Nothing to refine.")
        return

    print(f"Loading anchor library from {a.anchor_library}...")
    library = load_anchor_library(a.anchor_library)
    print(f"  Curated destinations: {len(library)}")

    # Group by source
    grouped = list(df.groupby("source_url"))
    if a.limit > 0:
        grouped = grouped[:a.limit]
        print(f"  LIMIT applied: refining first {len(grouped)} sources")
    print(f"  Sources to refine: {len(grouped)}")

    if a.dry_run:
        print("\nDRY-RUN mode. Sample of what would be sent to Haiku:\n")
        source_url, recs = grouped[0]
        # We can't fetch bodies in dry-run without WEBFLOW_API_TOKEN, so mock
        prompt = build_prompt(source_url.split("/")[-1], "(source title)", "(source body would go here)",
                              recs.to_dict("records"), library)
        print(prompt[:2000] + ("\n...(truncated)" if len(prompt) > 2000 else ""))
        print(f"\nWould call Haiku {len(grouped)} times.")
        return

    print("\nInitializing clients...")
    wf = WebflowClient()
    ac = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    print("  OK")

    # Build slug -> item cache
    print("Fetching all Blog Post items (for body lookups)...")
    slug_to_item = {}
    for item in wf.list_items(COLLECTIONS["blog_posts"]):
        slug_to_item[item["fieldData"].get("slug")] = item
    print(f"  {len(slug_to_item)} items indexed")

    refined_rows = []
    failed = 0
    for i, (source_url, recs) in enumerate(grouped, 1):
        slug = source_url.split("/")[-1]
        item = slug_to_item.get(slug)
        if not item:
            print(f"  [{i}/{len(grouped)}] SKIP {slug} (not in Webflow)")
            for _, r in recs.iterrows():
                r = r.to_dict()
                r["refined_anchor"] = r.get("anchor_text", "")
                r["refined_source"] = "no-item"
                refined_rows.append(r)
            failed += 1
            continue

        fd = item.get("fieldData", {})
        title = fd.get("name", "")
        bodies = get_blog_body(item)
        body_text = html_to_text(" ".join(bodies.values()))
        rec_list = recs.to_dict("records")
        prompt = build_prompt(slug, title, body_text, rec_list, library)

        try:
            refined = call_haiku(ac, prompt)
            if len(refined) != len(rec_list):
                raise ValueError(f"Got {len(refined)} anchors for {len(rec_list)} targets")
            for r, ra in zip(rec_list, refined):
                r["refined_anchor"] = ra
                r["refined_source"] = "haiku"
                refined_rows.append(r)
        except Exception as e:
            print(f"  [{i}/{len(grouped)}] ERROR {slug}: {e}")
            for r in rec_list:
                r["refined_anchor"] = r.get("anchor_text", "")
                r["refined_source"] = f"error: {str(e)[:60]}"
                refined_rows.append(r)
            failed += 1

        if i % 25 == 0:
            print(f"  [{i}/{len(grouped)}] processed  fails:{failed}")
        time.sleep(a.sleep)

    out_df = pd.DataFrame(refined_rows)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(a.output, index=False)
    print(f"\n✓ Wrote {a.output}")
    print(f"  Rows: {len(out_df):,}")
    print(f"  Refined by Haiku: {(out_df['refined_source']=='haiku').sum():,}")
    print(f"  Fell back to library anchor: {(out_df['refined_source']!='haiku').sum():,}")


if __name__ == "__main__":
    main()
