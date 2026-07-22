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




# ---- Relevance-based candidate selection (added 20-Jul) ----
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by",
    "with", "from", "is", "are", "your", "our", "this", "that", "how", "what",
    "why", "which", "vs", "and", "guide", "best", "top",
}


def _tokenize(text):
    """Lowercase, split on non-word, remove stopwords, keep tokens ≥3 chars."""
    if not text:
        return set()
    import re as _re
    tokens = _re.findall(r"[a-z0-9]+", str(text).lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


def _slug_tokens(url):
    """Extract meaningful tokens from a URL slug."""
    if not url:
        return set()
    slug = url.rstrip("/").split("/")[-1]
    return _tokenize(slug.replace("-", " "))


def _tier_weight(tier):
    """Higher = better. T0/T1P/T1 get boosts as authority destinations."""
    return {"T0": 1.5, "T1P": 1.4, "T1": 1.3, "T2": 1.1, "T3": 1.0, "T4": 0.9}.get(tier, 0.9)


def _safe_str(v):
    """Coerce pandas NaN / None / floats to a clean string, never crashes."""
    if v is None:
        return ""
    if isinstance(v, float) and str(v) == "nan":
        return ""
    return str(v)


def compute_candidates_by_relevance(source_url, source_title, source_category,
                                    tier_info, t0_pages, limit=15):
    """
    Rank all tier-map posts + T0 pages by relevance to source, return top N.
    Relevance = weighted keyword overlap (title + slug tokens) × tier weight.
    """
    source_title = _safe_str(source_title)
    source_category = _safe_str(source_category)
    src_tokens = _tokenize(source_title) | _slug_tokens(source_url)
    if not src_tokens:
        return []

    candidates = []
    for tgt_url, meta in tier_info.items():
        if tgt_url == source_url.rstrip("/"):
            continue
        tgt_title = _safe_str(meta.get("title", ""))
        tgt_category = _safe_str(meta.get("category", ""))
        tgt_tier = _safe_str(meta.get("tier", "T4")) or "T4"
        tgt_tokens = _tokenize(tgt_title) | _slug_tokens(tgt_url)
        overlap = len(src_tokens & tgt_tokens)
        if overlap == 0:
            continue
        # Jaccard-ish but favors overlap size
        jaccard = overlap / max(len(src_tokens | tgt_tokens), 1)
        score = jaccard * _tier_weight(tgt_tier)
        # Boost same-category
        if source_category and tgt_category == source_category:
            score *= 1.3
        # Slug fallback if title is empty
        slug_fallback = tgt_url.rsplit("/", 1)[-1].replace("-", " ")
        candidates.append({
            "target_url": tgt_url,
            "target_title": tgt_title or slug_fallback,
            "target_category": tgt_category or "?",
            "target_tier": tgt_tier,
            "suggested_anchor": (tgt_title[:60] if tgt_title else slug_fallback[:60]),
            "relevance_score": round(score, 3),
        })

    # Always include T0 money pages as candidates (they can always be a good CTA target)
    for t0_url in t0_pages:
        if t0_url.rstrip("/") == source_url.rstrip("/"):
            continue
        if any(c["target_url"] == t0_url for c in candidates):
            continue
        candidates.append({
            "target_url": t0_url,
            "target_title": t0_url.rsplit("/", 1)[-1].replace("-", " ").title(),
            "target_category": "Money Page",
            "target_tier": "T0",
            "suggested_anchor": "education loan options",
            "relevance_score": 0.5,  # baseline T0 always considered
        })

    candidates.sort(key=lambda c: c["relevance_score"], reverse=True)
    return candidates[:limit]


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

CANDIDATE INTERNAL LINKS (up to 10 for you to choose from — pick 3-5 that fit best):
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
  - TARGET 3-5 insertions per post. Fewer is OK if candidates aren't natural
    fits, but try to reach at least 3 if the candidates plausibly work.
  - Insert at most 5 links per post (hard ceiling).
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
       ", such as [X](url)" or "(see [X](url))". Aim for +40 chars or less.
    ❌ LONG BRIDGE: "..., similar to how [X](url) works for other engineering
       exams and shows pathways to alternate colleges." Prefer wrap or short
       bridge; the executor will filter insertions that add more than 60 chars.

  - HIGH-RELEVANCE candidates should produce SHORTER insertions, not longer.
    Topical closeness means the connection is obvious and doesn't need
    explaining. If you find yourself writing 'similar to how X works for
    Y in Z contexts', truncate to just 'similar to [X]' or wrap around an
    existing phrase in the sentence.

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
  - Prefer relevance over quantity — 3 great insertions beat 5 mediocre ones.

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
    # Extract JSON: use raw_decode so trailing content (a stray period,
    # a "```" fence, an apologetic note) after the JSON object doesn't
    # break parsing. Previous impl used text.rfind("}") which broke when
    # Sonnet appended commentary containing a "}".
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON in response: {text[:300]}")
    decoder = json.JSONDecoder()
    try:
        obj, _idx = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        # Fall back: try old find-last-brace path so we don't regress on
        # rare responses that pack multiple objects.
        end = text.rfind("}")
        if end == -1 or end <= start:
            raise ValueError(f"JSON parse failed ({e}): {text[start:start+300]}")
        obj = json.loads(text[start:end + 1])
    return obj


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
        # Stratified sample by source_tier so limit gives DIVERSE spread across
        # tiers, not just first N alphabetically (which biases to top tiers).
        import random as _random
        _random.seed(42)  # reproducible
        by_tier = {}
        for source_url, source_recs in grouped:
            tier = str(source_recs["source_tier"].iloc[0]) if "source_tier" in source_recs.columns else "?"
            by_tier.setdefault(tier, []).append((source_url, source_recs))
        # Sample proportionally
        total_sources = sum(len(v) for v in by_tier.values())
        picked = []
        for tier, items in by_tier.items():
            share = max(1, round(a.limit * len(items) / total_sources))
            _random.shuffle(items)
            picked.extend(items[:share])
        # Trim to exact limit
        _random.shuffle(picked)
        grouped = picked[:a.limit]
        # Log the distribution
        tier_counts = {}
        for _u, _r in grouped:
            t = str(_r["source_tier"].iloc[0]) if "source_tier" in _r.columns else "?"
            tier_counts[t] = tier_counts.get(t, 0) + 1
        print(f"\nProcessing {len(grouped)} sources (stratified sample by tier): {dict(sorted(tier_counts.items()))}")
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

        # Build candidates by RELEVANCE to source (not just category-tier order).
        # This gives Sonnet source-specific candidates instead of generic pillars.
        t0_pages_list = []
        try:
            import json as _json
            _grammar_p = Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
            if _grammar_p.exists():
                t0_pages_list = _json.load(open(_grammar_p)).get("t0_money_pages", [])
        except Exception:
            pass
        candidates = compute_candidates_by_relevance(
            source_url, source_title, source_category, tier_info, t0_pages_list, limit=10
        )

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
