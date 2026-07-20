"""
plan_insertions.py -- LLM-based smart insertion planner.

For each source post, sends the full body + candidate target links to
Claude Haiku. The model decides:
  - Which candidates are actually relevant to this source (skips MBA→MBBS
    style topical mismatches).
  - Which specific paragraph + sentence to modify for each insertion.
  - How to rewrite that sentence to naturally include the anchor + link,
    with minimal deviation from original meaning/tone.
  - Skips any candidate that has no natural fit.

Output: out/insertion-plans.csv — one row per insertion decision, with the
exact original sentence, exact new sentence (with <a> tag baked in), and
reasoning. Consumed by preview_insertion_plans.py + insert_planned_links.py.

USAGE:
  # Dry-run first 5 posts (prints prompts, no API cost):
  python scripts/plan_insertions.py --limit 5 --dry-run

  # Plan 10 posts for preview:
  python scripts/plan_insertions.py --limit 10 --apply

  # Full run against T3/T4 posts:
  python scripts/plan_insertions.py --tier-filter T3,T4 --apply

Env: WEBFLOW_API_TOKEN, ANTHROPIC_API_KEY
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body

try:
    import anthropic
except ImportError:
    print("ERROR: `anthropic` package not installed. Run: pip install anthropic")
    sys.exit(1)

MODEL = "claude-sonnet-4-6"
MAX_BODY_CHARS_PER_PARA = 800  # trim long paragraphs to keep prompt reasonable
MAX_PARAGRAPHS = 25            # ignore body past this — usually not worth linking to


def load_recs(path):
    df = pd.read_csv(path) if path.endswith(".csv") else pd.read_excel(path)
    df["source_url"] = df["source_url"].str.rstrip("/")
    df["target_url"] = df["target_url"].str.rstrip("/")
    return df


def load_tier_titles(path):
    """Get title + category per URL from posts-with-tiers.xlsx."""
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
    for old, new in [("URL", "url"), ("Title", "title"), ("Category", "category")]:
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})
    df["url"] = df["url"].str.rstrip("/")
    return df.set_index("url")[["title", "category"]].to_dict("index")


def extract_paragraphs(html, max_chars=MAX_BODY_CHARS_PER_PARA, max_paras=MAX_PARAGRAPHS):
    """
    Return a list of {idx, text} for the top N paragraphs in the body.
    We only give the model plain text (not HTML) so it can reason about
    sentences without needing to escape.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for i, p in enumerate(soup.find_all("p")[:max_paras]):
        txt = p.get_text(separator=" ").strip()
        if not txt or len(txt) < 20:  # skip tiny/empty paragraphs
            continue
        if len(txt) > max_chars:
            txt = txt[:max_chars] + " …"
        out.append({"idx": i, "text": txt})
    return out


def build_prompt(source_url, source_title, source_category, paragraphs, candidates):
    """Build the Haiku prompt for one source post."""
    para_block = "\n".join(f"[P{p['idx']}] {p['text']}" for p in paragraphs)
    cand_block = "\n".join(
        f"  {j+1}. {c['target_url']}\n"
        f"     Title: {c['target_title']}\n"
        f"     Category: {c['target_category']} | Tier: {c['target_tier']}\n"
        f"     Suggested anchor (starting point): {c['suggested_anchor']}"
        for j, c in enumerate(candidates)
    )
    return f"""You are helping insert internal links into a Propelld blog post naturally, one paragraph at a time.

SOURCE POST
  URL: {source_url}
  Title: {source_title}
  Category: {source_category}

BODY (paragraphs numbered P0, P1, P2, ...):
{para_block}

CANDIDATE INTERNAL LINKS (up to 15 for you to choose from — pick 4-6 that fit best):
{cand_block}

Your task: for EACH candidate, decide one of:

  1. INSERT — this link is topically relevant AND fits naturally in a specific
     sentence in the body.
       - Pick ONE paragraph (by number P#)
       - Pick ONE existing sentence in that paragraph to modify
       - Rewrite that sentence to naturally include an <a href="..."> anchor link
       - Minimally change the original sentence — same meaning, same tone
       - Do not repeat words already in the surrounding sentence as the anchor

  2. SKIP — this link is not relevant to this post, OR there is no sentence
     where inserting it would read naturally.

CONSTRAINTS
  - TARGET 4-6 insertions per post. Fewer is OK if candidates aren't natural
    fits, but try to reach at least 4 if the candidates plausibly work.
  - Insert at most 6 links per post (hard ceiling).
  - No 2 insertions in the same paragraph (distribute them across the body).
  - Never insert into the very first or very last paragraph (intro/CTA area).
  - Never invent an original_sentence — it must appear VERBATIM in the paragraph text above.
  - Anchor text: 3-8 words, describes the target, natural in the sentence.
  - Never use "click here", "read more", "learn more", "this article" as anchors.

  - INSERTION STYLE — STRONGLY PREFER WRAP-AROUND over bridge-append:
    ✅ WRAP-AROUND (ideal): find an existing keyword/phrase in the sentence that
       matches the target's topic, and wrap that phrase with the link. Add at
       most 1-2 words of clarifier if needed.
       Example:
         old: "the State Bank of India (SBI) leads the way"
         new: "the [State Bank of India (SBI) education loan](url) leads the way"
         added: 14 chars ✓
    ✅ SHORT BRIDGE (acceptable if wrap doesn't fit): add a short clause like
       ", such as [X](url)" or "(see [X](url))". Cap at ~40 added characters.
    ❌ LONG BRIDGE (skip this candidate): "..., similar to how [X](url) works
       for other engineering exams and shows pathways to alternate colleges."
       If your insertion adds more than 50 characters to the sentence, either
       tighten the phrasing or SKIP the candidate. Bloated sentences are worse
       than no insertion.

  - SUBSTANTIVE vs CATEGORICAL relevance — the linked concept must actually
    be DISCUSSED in the source paragraph, not just categorically related.
    ✅ Substantive: paragraph talks about SBI loans → link to SBI loan page
    ✅ Substantive: paragraph talks about percentile calculations → link to
       another exam's marks-to-rank calculation
    ❌ Categorical only: paragraph is about JEE result dates → linking to
       "VITEEE cutoff scores" because both are "engineering exams" is TOO
       LOOSE — dates ≠ cutoffs. Skip this candidate.
    Ask yourself: if a reader clicks this link, does the destination page
    address the exact concept the sentence is discussing? If no → SKIP.
  - PUNCTUATION: NEVER use em dashes (— or --) in your new_sentence, even if
    the original uses them. Prefer commas, colons, "such as", "including",
    "like", parenthetical (...), or splitting into two sentences.
  - The rewrite should feel like a light edit, not a restructure. Preserve the
    original sentence's voice and rhythm.
  - AVOID META-CONTENT PARAGRAPHS. Never insert a link into a sentence that
    describes what THIS blog post will cover, e.g., paragraphs containing:
      - "In this blog", "In this guide", "In this article"
      - "You'll learn", "You'll discover", "You'll read about"
      - "This guide explains", "This post covers", "we'll cover"
      - "By the end of this blog"
      - Bullet-list-intro phrases like "Here's a breakdown of:", "Here are:"
    These are meta-content signposts where an inline link creates a false
    signal that the linked topic is one of the blog's subjects. Skip these
    paragraphs entirely — pick a body paragraph that discusses a specific
    fact or comparison.
  - Prefer relevance over quantity — 4 great insertions beat 6 mediocre ones.

CRITICAL FAILURE MODES — READ CAREFULLY

  ❌ FORBIDDEN: URL placeholders
      new_sentence: "much like [EAMCET rank](link) works..."
      new_sentence: "see [MBA syllabus](https://example.com/mba)..."
      new_sentence: "check [SBI loan](url)..."
      new_sentence: "learn [BTech fees](site/blog/btech-fees)..."   ← missing leading /

  ✅ REQUIRED: use the EXACT target_url string from the candidate above.
      Copy-paste the URL verbatim from the CANDIDATE INTERNAL LINKS list.
      No shortening, no editing, no examples.

  ❌ FORBIDDEN: paraphrasing original_sentence
      Candidate paragraph has: "Students can check the fee structure online."
      YOU write original_sentence: "Students may verify fees online."   ← REPHRASED, will fail

  ✅ REQUIRED: copy the original_sentence character-for-character from the
      paragraph text above. Include exact punctuation, casing, and any inline
      formatting artifacts. If you can't find a sentence you want to modify
      exactly as-is, SKIP that candidate.

  ❌ FORBIDDEN: new_sentence without a markdown link
      new_sentence: "much like EAMCET rank calculations work"   ← no [anchor](url)

  ✅ REQUIRED: new_sentence MUST contain exactly one `[anchor text](target_url)`
      markdown link, using the exact target_url from the candidate.


OUTPUT — return ONLY this JSON, no other text:
{{
  "decisions": [
    {{
      "candidate_num": 1,
      "action": "insert",
      "target_url": "/site/blog/exact-url-copied-from-candidate",
      "paragraph_idx": N,
      "original_sentence": "EXACT text copied verbatim from paragraph P# above (character-for-character)",
      "new_sentence": "the SAME sentence with a minimally-inserted [anchor phrase](/site/blog/exact-url-copied-from-candidate) using markdown link format",
      "anchor": "the anchor phrase you used (must match what's between [ and ] in new_sentence)",
      "reasoning": "1 short sentence"
    }},
    {{
      "candidate_num": 2,
      "action": "skip",
      "target_url": "/site/blog/exact-url-copied-from-candidate",
      "reasoning": "why (topic mismatch, no natural fit, no sentence I can modify verbatim, etc.)"
    }}
  ]
}}"""


def call_haiku(client, prompt):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,   # Sonnet writes longer structured JSON
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Extract JSON
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in response: {text[:300]}")
    return json.loads(text[start:end + 1])


def _strip_em_dashes(text):
    """Replace em dashes with a comma, unless the original used them.
    Haiku sometimes ignores the prompt constraint — this belt-and-suspenders
    ensures em dashes never reach the executor."""
    if not text:
        return text
    # Replace em dash + optional space with ", "
    import re as _re
    text = _re.sub(r'\s*[—]+\s*', ", ", text)
    # Also handle double-hyphen (--) as em dash surrogate
    text = _re.sub(r'\s*--\s*', ", ", text)
    return text


def validate_insertion(decision, paragraphs):
    """Verify the LLM's decision is applyable — original_sentence must actually
    exist in the specified paragraph."""
    # Post-process: strip em dashes from new_sentence unless original had them
    if decision.get("action") == "insert":
        orig = str(decision.get("original_sentence", ""))
        new = str(decision.get("new_sentence", ""))
        if "—" not in orig and "--" not in orig:
            decision["new_sentence"] = _strip_em_dashes(new)
    if decision.get("action") != "insert":
        return None  # skips are fine
    para_idx = decision.get("paragraph_idx")
    orig = decision.get("original_sentence", "").strip()
    if not orig:
        return "empty original_sentence"
    para = next((p for p in paragraphs if p["idx"] == para_idx), None)
    if not para:
        return f"paragraph P{para_idx} not in body"
    if orig not in para["text"]:
        # Try fuzzy: strip whitespace variance
        orig_norm = re.sub(r"\s+", " ", orig).strip()
        para_norm = re.sub(r"\s+", " ", para["text"]).strip()
        if orig_norm not in para_norm:
            return "original_sentence not found verbatim in paragraph"
    new = decision.get("new_sentence", "")
    if not new:
        return "new_sentence is empty"
    # Accept EITHER markdown [anchor](url) OR HTML <a href="url">anchor</a>
    has_md = "](" in new
    has_html = re.search(r'<a\s+href=', new, re.IGNORECASE) is not None
    if not (has_md or has_html):
        return "new_sentence missing any link (neither markdown nor HTML)"
    return None  # OK


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recommendations", default="data/link-recommendations-refined.csv")
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--output", default="out/insertion-plans.csv")
    p.add_argument("--tier-filter", default=None,
                   help="Only plan for these source tiers (comma-separated)")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only first N source posts (0=all)")
    p.add_argument("--sleep", type=float, default=0.2,
                   help="Seconds between Haiku calls")
    p.add_argument("--dry-run", action="store_true",
                   help="Print first 3 prompts, don't hit API")
    p.add_argument("--apply", action="store_true",
                   help="Required to actually call Haiku and write output")
    a = p.parse_args()

    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    print(f"Loading recommendations from {a.recommendations}...")
    recs = load_recs(a.recommendations)
    print(f"  {len(recs):,} recs")

    if a.tier_filter:
        allowed = set(a.tier_filter.split(","))
        recs = recs[recs["source_tier"].isin(allowed)]
        print(f"  After --tier-filter ({a.tier_filter}): {len(recs):,}")

    print(f"Loading tier titles from {a.tier_file}...")
    tier_info = load_tier_titles(a.tier_file)
    print(f"  {len(tier_info)} posts indexed")

    # Group recs by source, cap at 5 candidates
    grouped = list(recs.groupby("source_url"))
    if a.limit > 0:
        grouped = grouped[:a.limit]
        print(f"\nProcessing {len(grouped)} sources (limit applied)")
    else:
        print(f"\nProcessing {len(grouped)} sources")

    # Fetch bodies (once)
    print(f"Fetching bodies from Webflow...")
    wf = WebflowClient()
    unique_slugs = set(u.rsplit("/", 1)[-1] for u, _ in grouped)
    slug_to_item = {}
    for item in wf.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s in unique_slugs:
            slug_to_item[s] = item
    print(f"  Cached {len(slug_to_item)} of {len(unique_slugs)} sources")

    ac = None
    if a.apply:
        ac = anthropic.Anthropic()

    plans = []
    printed = 0
    for i, (source_url, source_recs) in enumerate(grouped):
        slug = source_url.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            plans.append({"source_url": source_url, "status": "no-matching-item"})
            continue
        fd = item.get("fieldData", {})
        source_title = fd.get("name", "")
        source_meta = tier_info.get(source_url, {})
        source_category = source_meta.get("category", "?")

        bodies = get_blog_body(item)
        # Focus on the main body (post-body); ignore 2nd half for now (edge case)
        paragraphs = extract_paragraphs(bodies.get("post-body", ""))
        if len(paragraphs) < 3:
            plans.append({"source_url": source_url, "status": "body-too-short"})
            continue

        # Build candidates (top 5 recs for this source)
        candidates = []
        for _, r in source_recs.head(15).iterrows():  # widened to 15 to help Sonnet find fits when top picks don't work
            tgt_meta = tier_info.get(r["target_url"], {})
            candidates.append({
                "target_url": r["target_url"],
                "target_title": tgt_meta.get("title", r["target_url"].rsplit("/", 1)[-1].replace("-", " ")),
                "target_category": r.get("target_category", tgt_meta.get("category", "?")),
                "target_tier": r.get("target_tier", "?"),
                "suggested_anchor": r.get("refined_anchor") or r.get("anchor_text", ""),
            })

        prompt = build_prompt(source_url, source_title, source_category, paragraphs, candidates)

        if a.dry_run:
            if printed < 3:
                print(f"\n{'=' * 80}\nSample prompt {printed+1}/3 for {source_url}\n{'=' * 80}")
                print(prompt[:3000])
                print("\n...(truncated)")
                printed += 1
            continue

        # Call Haiku
        try:
            result = call_haiku(ac, prompt)
            for d in result.get("decisions", []):
                err = validate_insertion(d, paragraphs)
                plans.append({
                    "source_url": source_url,
                    "source_slug": slug,
                    "target_url": d.get("target_url", ""),
                    "action": d.get("action", "unknown"),
                    "paragraph_idx": d.get("paragraph_idx"),
                    "original_sentence": d.get("original_sentence", ""),
                    "new_sentence": d.get("new_sentence", ""),
                    "anchor": d.get("anchor", ""),
                    "reasoning": d.get("reasoning", ""),
                    "validation_error": err or "",
                    "status": "planned",
                })
        except Exception as e:
            plans.append({
                "source_url": source_url,
                "status": f"error: {str(e)[:200]}",
            })

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(grouped)}] planned")
        time.sleep(a.sleep)

    if a.dry_run:
        return

    df = pd.DataFrame(plans)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.output, index=False)
    print(f"\n✓ Wrote {a.output}")

    if "action" in df.columns:
        print("\n=== ACTION DISTRIBUTION ===")
        print(df["action"].value_counts(dropna=False).to_string())
        if "validation_error" in df.columns:
            bad = df[(df["action"] == "insert") & (df["validation_error"] != "")]
            print(f"\nInsertions with validation errors: {len(bad)}")
            if len(bad):
                print(bad["validation_error"].value_counts().head(5).to_string())


if __name__ == "__main__":
    main()
