"""
insert_planned_links.py -- execute Haiku's insertion plans surgically.

Reads out/insertion-plans.csv (from plan_insertions.py). For each plan with
action=insert, finds the original_sentence in the source post's body and
replaces it with new_sentence (which already contains the markdown-format
[anchor](url) link that we convert to HTML).

Design principles:
  - Sentence-level surgical replacement (not paragraph-level, not append).
  - Skip any plan where original_sentence isn't found VERBATIM (fail-safe).
  - Skip any plan with a validation_error from the planner.
  - UTM parameters auto-appended when target is a T0 CTA page.
  - Snapshot all posts before any writes.
  - Idempotent — skips if target URL already in body.
  - Halt on error rate > 5%.

USAGE:
  # Dry-run (see what would happen, no writes):
  python scripts/insert_planned_links.py

  # Actually apply:
  python scripts/insert_planned_links.py --apply

  # Apply only a small batch (10 posts):
  python scripts/insert_planned_links.py --limit 10 --apply

Env: WEBFLOW_API_TOKEN
"""

import argparse
import re
import sys
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup, NavigableString

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.link_utils import link_count, extract_links, normalize_url

# Reuse UTM helper from bulk_apply_links (already tested)
if "scripts.bulk_apply_links" in sys.modules:
    del sys.modules["scripts.bulk_apply_links"]
from scripts.bulk_apply_links import append_utm_if_t0, _load_t0_pages

HALT_ERROR_RATE = 0.05
MAX_SENTENCE_DELTA = 60  # Skip insertions that add more than this many characters
                          # to the original sentence. Prevents bloat. Set via
                          # --max-delta CLI flag if you want to override.
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


# Patterns Haiku sometimes hallucinates — insertions using these must be skipped
BAD_URL_PATTERNS = [
    r'^https?://example\.',       # example.com and similar
    r'^https?://www\.example\.',
    r'^\(?link\)?$',              # literal "link" or "(link)"
    r'^\(?url\)?$',
    r'^#$',                        # placeholder anchor
    r'^javascript:',
]


def is_bad_url(href):
    """Return True if href is a hallucinated placeholder we should refuse to insert."""
    if not href:
        return True
    h = href.strip().lower()
    for pat in BAD_URL_PATTERNS:
        if re.match(pat, h):
            return True
    return False


def normalize_href(href, source_url):
    """Normalize an href to an absolute /site/... path.
    Handles:
      - relative paths ('../foo', './foo', 'foo') → resolve against source
      - propelld.com full URLs → strip host
      - Haiku hallucinations 'https://site/foo' (fake host) → strip to /site/foo
      - 'site/blog/foo' (missing leading slash) → prepend /
      - absolute /site/ paths → unchanged
      - Sonnet malformations: '/ site/', '// site/', trailing/inner whitespace
    """
    # First, aggressive whitespace cleanup — remove any spaces INSIDE the URL
    # (Sonnet occasionally emits '/ site/...' or '//site/...')
    href = href.strip()
    # Collapse spaces around slashes: '/ site' → '/site', 'site /blog' → 'site/blog'
    href = re.sub(r'/\s+', '/', href)
    href = re.sub(r'\s+/', '/', href)
    # Collapse double slashes (except in 'https://' scheme)
    if "://" in href:
        scheme, rest = href.split("://", 1)
        rest = re.sub(r'/+', '/', rest)
        href = f"{scheme}://{rest}"
    else:
        href = re.sub(r'/+', '/', href)
    # Strip protocol + host if present
    for prefix in ("https://propelld.com", "http://propelld.com",
                   "https://www.propelld.com", "http://www.propelld.com"):
        if href.startswith(prefix):
            href = href[len(prefix):]
            break
    # Haiku hallucination pattern: "https://site/blog/foo" (missing propelld.com)
    m = re.match(r'^https?://(site/.+)$', href)
    if m:
        href = "/" + m.group(1)
    if not href:
        return href
    # Already absolute path
    if href.startswith("/"):
        return href
    # Missing leading slash but is a /site/ path
    if href.startswith("site/"):
        return "/" + href
    # Relative — resolve against source
    src_dir = source_url.rsplit("/", 1)[0] + "/"
    while href.startswith("../"):
        href = href[3:]
        src_dir = src_dir.rstrip("/").rsplit("/", 1)[0] + "/"
    if href.startswith("./"):
        href = href[2:]
    return src_dir + href


# Match both markdown [anchor](url) AND HTML <a href="url">anchor</a>
HTML_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)


VISIBLE_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
VISIBLE_HTML_LINK = re.compile(r'<a\s+[^>]*>([^<]*)</a>', re.IGNORECASE | re.DOTALL)


def visible_text_len(s):
    """Return length of sentence with markdown/HTML link markup stripped, so the
    delta filter measures reader-visible growth rather than URL noise. A long
    URL like /site/blog/eligibility-criteria-for-study-abroad-education-loan
    adds ~60 chars of markup even when the sentence visually only grows by 20."""
    s = str(s or "")
    s = VISIBLE_MD_LINK.sub(r'\1', s)     # [anchor](url) -> anchor
    s = VISIBLE_HTML_LINK.sub(r'\1', s)   # <a ...>anchor</a> -> anchor
    return len(s)


def md_link_to_html(new_sentence, source_url, preview_marker=False):
    """Convert links in new_sentence to normalized HTML <a> tags.

    Handles BOTH input formats Haiku can produce:
      - Markdown: [anchor](url)     ← convert to HTML
      - HTML: <a href="url">anchor</a>  ← normalize href, keep as HTML

    For each link:
      - Normalize URL to absolute /site/... path (strip fake hosts, resolve relative)
      - Reject known-bad hallucinations (example.com, '(link)', etc.) — return
        the sentence with that link REMOVED entirely rather than inserting broken.
      - Apply UTM params for T0 CTA targets
      - Add data-preview-new="1" for preview mode only
    """
    marker = ' data-preview-new="1"' if preview_marker else ''

    def make_link(anchor, href):
        if is_bad_url(href):
            # Return just the anchor text — no broken link
            return anchor
        normalized = normalize_href(href, source_url)
        if is_bad_url(normalized):
            return anchor
        final = append_utm_if_t0(normalized, source_url)
        return f'<a href="{final}"{marker}>{anchor}</a>'

    def md_repl(m):
        return make_link(m.group(1), m.group(2).strip())

    def html_repl(m):
        return make_link(m.group(2), m.group(1).strip())

    # Order matters: convert markdown first (it doesn't overlap with HTML tags)
    result = MD_LINK_RE.sub(md_repl, new_sentence)
    # Then normalize any existing HTML tags (updates href, adds marker)
    result = HTML_LINK_RE.sub(html_repl, result)
    return result


def _norm_with_map(s):
    """Collapse whitespace runs to single spaces while keeping a map from
    each normalized-char index back to the original string index."""
    norm, idx_map = [], []
    prev_space = False
    for i, ch in enumerate(s):
        if ch.isspace():
            if norm and not prev_space:
                norm.append(" ")
                idx_map.append(i)
            prev_space = True
        else:
            norm.append(ch)
            idx_map.append(i)
            prev_space = False
    if norm and norm[-1] == " ":
        norm.pop()
        idx_map.pop()
    return "".join(norm), idx_map


def apply_insertion_to_paragraph(soup, paragraph_idx, original_sentence, new_sentence_html):
    """
    Find paragraph N in soup, locate original_sentence, and replace it with
    new_sentence_html WITHOUT destroying the paragraph's other inline markup
    (existing <a> links, <strong>, <em>, ...).

    Strategy: the sentence must live entirely inside ONE text node (the
    common case for prose). We splice only that node: text-before +
    parsed(new_sentence_html) + text-after. Text inside existing <a> tags
    is never touched.

    If the sentence spans multiple inline elements we SKIP (fail-safe)
    rather than flatten the paragraph, because flattening deletes existing
    links and formatting.

    Returns (bool_ok, reason_if_not).
    """
    paras = soup.find_all("p")
    if paragraph_idx >= len(paras):
        return False, f"paragraph P{paragraph_idx} out of range"
    p = paras[paragraph_idx]

    orig_norm = re.sub(r"\s+", " ", str(original_sentence)).strip()
    if not orig_norm:
        return False, "empty original_sentence"

    for node in p.find_all(string=True):
        if node.find_parent("a") is not None:
            continue  # never splice inside an existing link
        raw = str(node)
        norm, idx_map = _norm_with_map(raw)
        pos = norm.find(orig_norm)
        if pos == -1:
            continue
        raw_start = idx_map[pos]
        raw_end = idx_map[pos + len(orig_norm) - 1] + 1
        before, after = raw[:raw_start], raw[raw_end:]
        frag = BeautifulSoup(new_sentence_html, "html.parser")
        pieces = []
        if before:
            pieces.append(NavigableString(before))
        pieces.extend(list(frag.children))
        if after:
            pieces.append(NavigableString(after))
        for piece in pieces:
            node.insert_before(piece)
        node.extract()
        return True, "ok"

    combined_norm = re.sub(r"\s+", " ", p.get_text(separator=" ")).strip()
    if orig_norm in combined_norm:
        return False, "sentence spans inline markup (skipped to preserve links/formatting)"
    return False, "original_sentence not in paragraph text"


def process_source(item, plans_for_source, dry_run, max_delta=MAX_SENTENCE_DELTA):
    fd = item.get("fieldData", {})
    slug = fd.get("slug", "")
    source_url = f"/site/blog/{slug}"
    bodies = get_blog_body(item)
    original = dict(bodies)
    before_links = {k: link_count(v) for k, v in bodies.items()}

    applied, skipped, errors = 0, 0, []

    # Insertions all go into post-body (the planner only reads post-body)
    if "post-body" not in bodies:
        return {"slug": slug, "status": "no-post-body", "applied": 0, "skipped": 0}

    # v6: plans carry a `field` column (post-body | post-body-2nd-half).
    # Older v5 plans have no field column and default to post-body.
    soups = {}

    def _get_soup(field):
        if field not in soups:
            soups[field] = BeautifulSoup(bodies.get(field, "") or "", "html.parser")
        return soups[field]

    # All targets already linked in this post (any body field), normalized
    existing_targets = set()
    for _f, _html in bodies.items():
        for _l in extract_links(_html):
            existing_targets.add(_l["href"])

    # Per-post T0 CTA cap (max 2, Wave B policy) — counts links already in
    # the post plus ones applied in this run. Belt-and-braces: the v6
    # allocator enforces this at plan time too.
    _t0_set = _load_t0_pages()
    t0_count = sum(1 for t in existing_targets if t in _t0_set)

    for _, plan in plans_for_source.iterrows():
        if plan.get("action") != "insert":
            skipped += 1
            continue
        # pandas NaN check — empty cells shouldn't count as errors
        val_err = plan.get("validation_error")
        has_val_err = (val_err is not None
                       and not (isinstance(val_err, float) and str(val_err) == "nan")
                       and str(val_err).strip() not in ("", "nan"))
        if has_val_err:
            skipped += 1
            errors.append(f"validation: {val_err}")
            continue

        field = str(plan.get("field") or "post-body")
        if field not in ("post-body", "post-body-2nd-half"):
            skipped += 1
            errors.append(f"unknown field: {field}")
            continue
        if not (bodies.get(field) or "").strip():
            skipped += 1
            errors.append(f"field {field} empty on this post")
            continue

        # Delta filter — measure VISIBLE-text growth (stripping link markup),
        # not raw byte growth. Long URLs add ~50-70 chars of markdown/HTML noise
        # even when the reader-visible sentence only grows by 20 chars.
        # Raw filter was falsely rejecting ~30% of otherwise-clean insertions.
        orig_len = len(str(plan.get("original_sentence", "")))
        new_visible = visible_text_len(plan.get("new_sentence", ""))
        delta = new_visible - orig_len
        if max_delta > 0 and delta > max_delta:
            skipped += 1
            errors.append(f"delta-filter: +{delta} visible chars > {max_delta} limit")
            continue

        # Idempotency: skip if target already linked anywhere in the post
        # (both body fields). Exact normalized-path comparison — a raw
        # substring check has prefix collisions (/site/education-loan matches
        # /site/education-loan-emi-calculator) and misses UTM'd variants.
        raw_tgt = normalize_url(str(plan["target_url"]))
        if raw_tgt in existing_targets:
            skipped += 1
            errors.append(f"idempotent-skip: {raw_tgt} already linked")
            continue

        # Per-post T0 CTA cap
        if raw_tgt in _t0_set and t0_count >= 2:
            skipped += 1
            errors.append(f"t0-cta-cap: post already has 2 money-page links")
            continue

        # Convert markdown link in new_sentence to HTML (with UTM handling)
        new_sentence_html = md_link_to_html(str(plan["new_sentence"]), source_url)

        try:
            ok, reason = apply_insertion_to_paragraph(
                _get_soup(field),
                int(plan["paragraph_idx"]),
                str(plan["original_sentence"]),
                new_sentence_html,
            )
            if ok:
                applied += 1
                existing_targets.add(raw_tgt)
                if raw_tgt in _t0_set:
                    t0_count += 1
            else:
                skipped += 1
                errors.append(reason)
        except Exception as e:
            skipped += 1
            errors.append(f"exception: {str(e)[:100]}")

    if applied > 0:
        for _field, _soup in soups.items():
            bodies[_field] = str(_soup)

    after_links = {k: link_count(v) for k, v in bodies.items()}

    # SAFETY INVARIANT: every applied insertion adds exactly one link and
    # must never remove any. If the post ends with fewer links than
    # before + applied, something destroyed existing links — refuse to write.
    total_before = sum(before_links.values())
    total_after = sum(after_links.values())
    if total_after < total_before + applied:
        return {
            "slug": slug,
            "source_url": source_url,
            "planned": len(plans_for_source),
            "applied": applied,
            "skipped": skipped,
            "before_links": total_before,
            "after_links": total_after,
            "status": "INVARIANT-FAILED-LINK-LOSS",
            "errors_notes": (f"link-count invariant failed: before={total_before} "
                             f"+ applied={applied} > after={total_after}; write refused"),
        }, None

    changed = [k for k in BLOG_BODY_FIELDS if bodies.get(k) != original.get(k)]

    log = {
        "slug": slug,
        "source_url": source_url,
        "planned": len(plans_for_source),
        "applied": applied,
        "skipped": skipped,
        "before_links": sum(before_links.values()),
        "after_links": sum(after_links.values()),
        "errors_notes": "; ".join(errors[:3]),
    }

    if dry_run:
        log["status"] = "dry-run"
        return log, None
    if not changed:
        log["status"] = "no-change"
        return log, None
    patch = {k: bodies[k] for k in changed}
    return log, patch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plans", default="data/insertion-plans.csv",
                   help="Plans CSV. Default: data/ (committed). Also accepts out/ (workflow output).")
    p.add_argument("--snapshot-dir", default="snapshots/")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip-snapshot", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--max-delta", type=int, default=MAX_SENTENCE_DELTA,
                   help="Skip insertions whose new_sentence adds more than "
                        f"N chars vs original. Default {MAX_SENTENCE_DELTA}. "
                        "Use 0 to disable filter.")
    p.add_argument("--output-log", default="out/insert-planned-links-log.csv")
    a = p.parse_args()

    print(f"Loading plans from {a.plans}...")
    df = pd.read_csv(a.plans)
    print(f"  Total rows: {len(df):,}")

    # Filter to insertion actions only
    if "action" in df.columns:
        insertion_rows = df[df["action"] == "insert"]
    else:
        print("ERROR: no 'action' column — is this the right file?")
        sys.exit(1)
    print(f"  Insertion actions: {len(insertion_rows):,}")

    # Filter out validation errors (safety net)
    bad = insertion_rows[insertion_rows.get("validation_error", "") != ""]
    if len(bad) > 0:
        print(f"  ⚠ {len(bad)} insertions have validation_error — will be skipped:")
        for reason, cnt in bad["validation_error"].value_counts().head(5).items():
            print(f"     {cnt:>4}  {reason}")

    grouped = list(insertion_rows.groupby("source_url"))
    if a.limit > 0:
        grouped = grouped[:a.limit]
        print(f"  --limit applied: {len(grouped)} sources")
    print(f"\nProcessing {len(grouped)} source posts...")

    client = WebflowClient()
    if a.apply and not a.skip_snapshot:
        print(f"Snapshotting all blog posts to {a.snapshot_dir}...")
        from lib.snapshots import snapshot_all_blogs
        snap_path, manifest = snapshot_all_blogs(a.snapshot_dir, client, dry_run=False)
        print(f"  Snapshotted {len(manifest)} posts to {snap_path}")

    # Build slug -> item index
    print("Indexing blog posts...")
    slug_to_item = {}
    for item in client.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s:
            slug_to_item[s] = item

    logs = []
    errors = 0
    for i, (source_url, plans_for_source) in enumerate(grouped):
        slug = source_url.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            logs.append({"source_url": source_url, "status": "no-matching-item"})
            errors += 1
            continue
        try:
            log, patch = process_source(item, plans_for_source, dry_run=not a.apply, max_delta=a.max_delta)
            if patch and a.apply:
                client.update_item(COLLECTIONS["blog_posts"], item["id"], patch)
                log["status"] = "patched"
                time.sleep(a.sleep)
            elif not patch and a.apply:
                log["status"] = "no-change"
            logs.append(log)
        except Exception as e:
            logs.append({"source_url": source_url, "slug": slug, "status": f"error: {str(e)[:200]}"})
            errors += 1

        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(grouped)}]  errors:{errors}")
        if i + 1 > 20 and errors / (i + 1) > HALT_ERROR_RATE:
            print(f"\n! HALT: error rate {errors/(i+1):.1%} exceeds {HALT_ERROR_RATE:.0%}")
            break

    Path(a.output_log).parent.mkdir(parents=True, exist_ok=True)
    log_df = pd.DataFrame(logs)
    log_df.to_csv(a.output_log, index=False)
    print(f"\n✓ Wrote {a.output_log}")

    print("\n=== SUMMARY ===")
    if "status" in log_df.columns:
        print(log_df["status"].value_counts().to_string())
    if "applied" in log_df.columns:
        print(f"\nTotal insertions applied: {log_df['applied'].fillna(0).sum():,.0f}")
        print(f"Total skipped:            {log_df['skipped'].fillna(0).sum():,.0f}")
    print(f"Errors: {errors}")


if __name__ == "__main__":
    main()
