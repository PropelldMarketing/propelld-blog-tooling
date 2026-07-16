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
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, BLOG_BODY_FIELDS, get_blog_body
from lib.link_utils import link_count

# Reuse UTM helper from bulk_apply_links (already tested)
if "scripts.bulk_apply_links" in sys.modules:
    del sys.modules["scripts.bulk_apply_links"]
from scripts.bulk_apply_links import append_utm_if_t0

HALT_ERROR_RATE = 0.05
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
    """
    href = href.strip()
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


def apply_insertion_to_paragraph(soup, paragraph_idx, original_sentence, new_sentence_html):
    """
    Find paragraph N in soup, locate original_sentence in its text, and
    replace with new_sentence_html (which contains an <a> tag).

    Returns (bool_ok, reason_if_not).
    """
    paras = soup.find_all("p")
    if paragraph_idx >= len(paras):
        return False, f"paragraph P{paragraph_idx} out of range"
    p = paras[paragraph_idx]

    # Walk the text nodes and find the one containing the start of original_sentence
    text_nodes = list(p.find_all(string=True))
    combined = "".join(str(t) for t in text_nodes)
    orig_norm = re.sub(r"\s+", " ", original_sentence).strip()
    combined_norm = re.sub(r"\s+", " ", combined).strip()

    if orig_norm not in combined_norm:
        return False, "original_sentence not in paragraph text"

    # Simplest reliable approach: rebuild paragraph inner HTML by replacing the
    # sentence in the plaintext. This DOES lose inline formatting within that
    # paragraph (bold/italic) but preserves the paragraph's surrounding context.
    # For prose blog posts, this is acceptable.
    #
    # Alternative approach for later: walk text nodes and do a fine-grained
    # split preserving inline tags. More brittle so skipping for now.

    # Build new paragraph HTML: use plaintext of paragraph, replace the sentence,
    # then wrap in <p> tag preserving attributes.
    para_text = p.get_text(separator=" ")
    para_text_norm = re.sub(r"\s+", " ", para_text).strip()
    # Find case-preserving version of orig in para_text
    start_idx = para_text_norm.find(orig_norm)
    if start_idx == -1:
        return False, "orig_sentence normalization mismatch"
    # Replace in the normalized text
    new_para_text = (para_text_norm[:start_idx] +
                     new_sentence_html +
                     para_text_norm[start_idx + len(orig_norm):])
    # Parse the new inner HTML and swap into <p>
    new_inner = BeautifulSoup(new_para_text, "html.parser")
    p.clear()
    for child in list(new_inner.children):
        p.append(child)
    return True, "ok"


def process_source(item, plans_for_source, dry_run):
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

    soup = BeautifulSoup(bodies["post-body"], "html.parser")

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

        # Idempotency: skip if target already in body (raw or UTM'd)
        raw_tgt = str(plan["target_url"]).rstrip("/")
        utm_tgt = append_utm_if_t0(raw_tgt, source_url)
        if raw_tgt in str(soup) or utm_tgt in str(soup):
            skipped += 1
            continue

        # Convert markdown link in new_sentence to HTML (with UTM handling)
        new_sentence_html = md_link_to_html(str(plan["new_sentence"]), source_url)

        try:
            ok, reason = apply_insertion_to_paragraph(
                soup,
                int(plan["paragraph_idx"]),
                str(plan["original_sentence"]),
                new_sentence_html,
            )
            if ok:
                applied += 1
            else:
                skipped += 1
                errors.append(reason)
        except Exception as e:
            skipped += 1
            errors.append(f"exception: {str(e)[:100]}")

    if applied > 0:
        bodies["post-body"] = str(soup)

    after_links = {k: link_count(v) for k, v in bodies.items()}
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
            log, patch = process_source(item, plans_for_source, dry_run=not a.apply)
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
