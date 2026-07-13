"""
refine_audit_rewrites.py -- context-aware anchor rewriting via Claude Haiku.

Reads audit CSV, filters to REWRITE rows, fetches each source post body
from Webflow, extracts the sentence containing the flagged anchor, and
asks Claude Haiku for a natural rewrite in that context.

Handles three outcomes per row:
  - REWRITE: refined_anchor is a phrase that fits the sentence
  - KILL: no good rewrite exists — better to unwrap the link entirely
  - LEAVE-ALONE: current anchor is fine or replacement would hurt more
    than help

Output:
  - out/audit-rewrites-refined.csv — full row-level data for bulk_apply_audit
  - out/audit-rewrites-for-vaishali.xlsx — human-readable review file with
    Claude's suggestions + reasoning + editable vaishali_decision column

USAGE:
  # Dry-run (checks setup, prints prompt for first 5 rows, no API cost):
  python scripts/refine_audit_rewrites.py --limit 5 --dry-run

  # Full refinement:
  python scripts/refine_audit_rewrites.py --apply

Env:
  WEBFLOW_API_TOKEN  (fetch source bodies)
  ANTHROPIC_API_KEY  (Claude Haiku)

Cost estimate:
  ~30 REWRITE rows × 1 Haiku call each. ~800 input + 200 output tokens per
  call. ~$0.03 total.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body
from lib.link_utils import normalize_url

try:
    import anthropic
except ImportError:
    print("ERROR: `anthropic` package not installed. Run: pip install anthropic")
    sys.exit(1)

MODEL = "claude-haiku-4-5-20251001"
CONTEXT_CHARS = 250  # each side of the anchor


def load_anchor_library():
    p = Path(__file__).parent.parent / "data" / "anchor_library_starter.json"
    if not p.exists():
        return {}
    return json.load(open(p)).get("destinations", {})


def html_to_text_preserving_positions(html):
    """Return a plain-text version of the HTML that we can search for anchor context."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ")


def find_anchor_context(html, target_url, anchor_text):
    """
    Return the ~250 chars before + anchor + ~250 chars after in plain text.
    Matches by target_url (normalized) + anchor_text (exact after strip).
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    target_norm = normalize_url(target_url)
    anchor_norm = (anchor_text or "").strip()

    for a in soup.find_all("a", href=True):
        if normalize_url(a["href"]) != target_norm:
            continue
        current_anchor = a.get_text().strip()
        if anchor_norm and current_anchor != anchor_norm:
            continue
        # Get the whole text of the parent + siblings around it
        parent = a.parent
        if parent is None:
            continue
        # Reconstruct: get text of ALL siblings before + anchor + siblings after
        parent_text = parent.get_text(separator=" ")
        # Where's the anchor in that text? Find its offset by iterating children
        idx = parent_text.find(current_anchor)
        if idx == -1:
            continue
        before = parent_text[:idx].strip()
        after = parent_text[idx + len(current_anchor):].strip()
        # Truncate
        before = before[-CONTEXT_CHARS:] if len(before) > CONTEXT_CHARS else before
        after = after[:CONTEXT_CHARS]
        return {
            "before": before,
            "anchor": current_anchor,
            "after": after,
        }
    return None


def build_prompt(row, ctx, library):
    slug = row["target_url"].rstrip("/").split("/")[-1]
    target_topic = slug.replace("-", " ")
    variants = library.get(row["target_url"].rstrip("/"), [])
    variants_str = "\n".join(f"    - \"{v}\"" for v in variants[:5]) or "    (no library variants)"

    return f"""You are refining an internal link's anchor text on a Propelld blog post.

CONTEXT (the anchor is in the middle):
    "...{ctx['before']}[ANCHOR: "{ctx['anchor']}" → {row['target_url']}]{ctx['after']}..."

CURRENT ANCHOR: "{ctx['anchor']}"
CURRENT ANCHOR ISSUE: {row['reason']}  (from an internal-link audit)
TARGET URL: {row['target_url']}
TARGET IS ABOUT: {target_topic}

Anchor library variants for this target (from our SEO library):
{variants_str}

Task: decide the best action for this specific link IN CONTEXT.

Return JSON:
{{
    "action": "rewrite" | "kill" | "leave-alone",
    "new_anchor": "..." (only if action=rewrite; 2-8 words),
    "reasoning": "1 short sentence"
}}

Rules:
- If the current anchor is generic (click here, read more, empty, raw URL), pick a phrase that (a) fits the sentence grammar, (b) does NOT repeat words already in "before" or "after", (c) describes the target concept.
- If the sentence right before the link already names the target topic, prefer a short forward-reference like "here", "our guide", "read more" (but not literally "read more" as anchor).
- If replacing the anchor would cause repetition or weird grammar, return "leave-alone" — original stays.
- If the sentence would read better with the link removed (e.g., the link add nothing and the anchor was pure filler), return "kill".
- NEVER use "click here", "read more", "learn more", "this article" as new_anchor.
- Match tone: informative, Indian audience, plain English.

Return ONLY the JSON. No preamble."""


def call_haiku(client, prompt):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Extract JSON
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in response: {text[:200]}")
    obj = json.loads(text[start:end + 1])
    return obj


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audit", default="data/internal-links-inventory.csv")
    p.add_argument("--output-csv", default="out/audit-rewrites-refined.csv")
    p.add_argument("--output-xlsx", default="out/audit-rewrites-for-vaishali.xlsx")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true",
                   help="Print prompts for first N rows, don't call the API")
    p.add_argument("--apply", action="store_true",
                   help="Required to actually call Haiku and write outputs")
    p.add_argument("--sleep", type=float, default=0.1)
    a = p.parse_args()

    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    print(f"Loading audit from {a.audit}...")
    df = pd.read_csv(a.audit) if a.audit.endswith(".csv") else pd.read_excel(a.audit)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    rewrites = df[df["action"] == "REWRITE"].reset_index(drop=True)
    print(f"  Total REWRITE rows: {len(rewrites)}")
    if a.limit > 0:
        rewrites = rewrites.head(a.limit)
        print(f"  --limit applied: refining first {len(rewrites)} rows")

    if len(rewrites) == 0:
        print("Nothing to refine.")
        return

    library = load_anchor_library()
    print(f"  Anchor library destinations: {len(library)}")

    # Fetch source posts (once, cache) — need all unique source_urls
    unique_sources = rewrites["source_url"].unique()
    print(f"\nFetching {len(unique_sources)} source posts from Webflow...")
    wf = WebflowClient()
    slug_to_bodies = {}
    for item in wf.list_items(COLLECTIONS["blog_posts"]):
        slug = item.get("fieldData", {}).get("slug")
        if slug and f"/site/blog/{slug}" in unique_sources:
            slug_to_bodies[slug] = get_blog_body(item)
    print(f"  Cached bodies for {len(slug_to_bodies)} of {len(unique_sources)} sources")

    ac = None
    if a.apply:
        ac = anthropic.Anthropic()
        print("  Anthropic client initialized")

    refined_rows = []
    print(f"\nProcessing {len(rewrites)} rewrites...")
    for i, row in rewrites.iterrows():
        slug = row["source_url"].rsplit("/", 1)[-1]
        bodies = slug_to_bodies.get(slug, {})
        field = row.get("source_body_field") or "post-body"
        html = bodies.get(field, "")

        ctx = find_anchor_context(html, row["target_url"], row.get("anchor_text"))
        if not ctx:
            refined_rows.append({
                **row.to_dict(),
                "refined_action": "leave-alone",
                "refined_anchor": "",
                "refiner_reasoning": "could not locate the anchor in body (may have been removed or moved)",
            })
            continue

        prompt = build_prompt(row, ctx, library)

        if a.dry_run:
            print(f"\n--- Row {i+1}/{len(rewrites)}: {slug} ---")
            print(prompt[:1500])
            print("... (truncated)")
            if i >= 4:
                print("\n(Only showing first 5 prompts in dry-run)")
                break
            continue

        try:
            result = call_haiku(ac, prompt)
            refined_rows.append({
                **row.to_dict(),
                "refined_action": result.get("action", "leave-alone"),
                "refined_anchor": result.get("new_anchor", ""),
                "refiner_reasoning": result.get("reasoning", ""),
                "context_before": ctx["before"][-100:],
                "context_after": ctx["after"][:100],
            })
        except Exception as e:
            refined_rows.append({
                **row.to_dict(),
                "refined_action": "leave-alone",
                "refined_anchor": "",
                "refiner_reasoning": f"error: {str(e)[:200]}",
            })
        time.sleep(a.sleep)

    if a.dry_run:
        return

    out_df = pd.DataFrame(refined_rows)
    Path(a.output_csv).parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(a.output_csv, index=False)
    print(f"\n✓ Wrote {a.output_csv}")

    # Vaishali xlsx — small subset of columns for review
    xlsx_cols = ["source_url", "target_url", "anchor_text", "context_before",
                 "context_after", "refined_action", "refined_anchor",
                 "refiner_reasoning"]
    xlsx_df = out_df[[c for c in xlsx_cols if c in out_df.columns]].copy()
    xlsx_df["vaishali_decision (edit if you disagree)"] = ""
    xlsx_df["vaishali_final_anchor (only if you edit)"] = ""
    with pd.ExcelWriter(a.output_xlsx, engine="openpyxl") as w:
        xlsx_df.to_excel(w, sheet_name="rewrites-review", index=False)
    print(f"✓ Wrote {a.output_xlsx}")

    # Summary
    print("\n=== SUMMARY ===")
    if "refined_action" in out_df.columns:
        print(out_df["refined_action"].value_counts().to_string())


if __name__ == "__main__":
    main()
