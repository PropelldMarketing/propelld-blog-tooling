"""
plan_insertions.py — v6 "plan globally, write locally" insertion planner.

Architecture (rebuilt Jul-2026 after v5 review):

  Stage A (deterministic)  Extract paragraphs from BOTH body fields
                           (post-body + post-body-2nd-half — v5 ignored the
                           2nd half entirely). Rank candidate targets with
                           IDF-weighted token overlap × tier weight ×
                           category boost. v5 bug fixed: tier map now loads
                           the Tier column, so tier weighting actually works.

  Stage B (deterministic)  WRAP-SCAN: search each post's text nodes for
                           literal occurrences of the target's anchor-library
                           variants / title phrases. Every hit is a
                           guaranteed natural insertion with ~0 added chars
                           and NO LLM involvement.

  Stage C (deterministic)  GLOBAL ALLOCATOR: selects the final edge list for
                           the whole run under every cap simultaneously:
                             - per post: 3-5 insertions, max 2 T0 CTAs,
                               max 1 insertion per paragraph
                             - per target: tier inbound budget
                               (tier_inbound_targets in grammar JSON)
                             - per anchor string: global repetition cap
                             - waterfall + category/bridge legality
                           Priority: wraps first, then bridges by relevance.
                           First-come-alphabetical allocation is gone.

  Stage D (LLM)            BRIDGE-WRITER: only edges with no wrap hit go to
                           the model, one small call per source. A coded
                           scaffold ban ("similar to X", "much like Y", ...)
                           rejects formulaic bridges and retries once with
                           the failure named; still-failing edges are dropped
                           and their slot refilled where possible.

USAGE (same dispatch flags as v5):
  python scripts/plan_insertions.py --dry-run --limit 50
  python scripts/plan_insertions.py --apply --limit 50
  python scripts/plan_insertions.py --apply                 # full catalogue
  python scripts/plan_insertions.py --apply --no-llm        # wraps only, $0

Output CSV gains two columns over v5: `field` (which body field the
paragraph lives in) and `insertion_type` (wrap | bridge).

Env: WEBFLOW_API_TOKEN, ANTHROPIC_API_KEY (not needed with --no-llm)
"""

import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.webflow_client import WebflowClient, COLLECTIONS, get_blog_body
from lib.link_utils import extract_links, normalize_url

try:
    import anthropic
except ImportError:
    anthropic = None  # allowed for --dry-run / --no-llm / tests

MODEL = "claude-sonnet-4-6"
MAX_BODY_CHARS_PER_PARA = 800
MAX_PARAGRAPHS = 40            # across both fields (v5: 25, one field)

# ---------------- policy constants ----------------
PER_POST_MAX = 5
PER_POST_TARGET = 4
PER_POST_MIN = 3
PER_POST_T0_MAX = 2            # Wave B policy, now actually enforced
ANCHOR_REPEAT_CAP = 25         # same exact anchor → same target, per run
MAX_VISIBLE_DELTA = 60
CANDIDATE_POOL = 25            # v5: 10-15

TIER_RANK = {"T0": 0, "T1P": 1, "T1": 2, "T2": 3, "T3": 4, "T4": 5}

BRIDGE_WHITELIST = {
    ("Exams & Counselling", "Education Loans"),
    ("Exams & Counselling", "Finance & Credit Education"),
    ("Study Abroad", "Education Loans"),
    ("Courses & Careers", "Education Loans"),
}

META_PHRASES = [
    "in this blog", "in this guide", "in this article", "you'll learn",
    "you will learn", "you'll discover", "this guide explains",
    "this post covers", "we'll cover", "by the end of this blog",
    "here's a breakdown of", "here are:",
]

# Phrases that make inserted links read algorithmic. A bridge sentence that
# INTRODUCES one of these (i.e. phrase not present in the original sentence)
# is rejected and retried. Checking "introduced" keeps legitimate uses in the
# author's own prose safe.
SCAFFOLD_PHRASES = [
    "similar to", "much like", "just like", "akin to", "also see",
    "for more", "learn more", "check out", "explore", "such as the",
    "including the", "see our", "refer to", "don't forget to",
    "be sure to", "you can also",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by",
    "with", "from", "is", "are", "your", "our", "this", "that", "how", "what",
    "why", "which", "vs", "guide", "best", "top",
}

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


# ================= data loading =================

def load_tier_map(path):
    """URL → {tier, category, title}. v5's load_tier_titles dropped the Tier
    column, silently disabling tier weighting — fixed here."""
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path)
    ren = {}
    for old, new in [("URL", "url"), ("Tier", "tier"),
                     ("Category", "category"), ("Title", "title")]:
        if old in df.columns and new not in df.columns:
            ren[old] = new
    df = df.rename(columns=ren)
    df["url"] = df["url"].astype(str).str.rstrip("/")
    cols = [c for c in ["tier", "category", "title"] if c in df.columns]
    return df.set_index("url")[cols].to_dict("index")


def load_grammar():
    p = Path(__file__).parent.parent / "data" / "category_grammar_rules.json"
    try:
        return json.load(open(p))
    except Exception:
        return {}


def load_anchor_library():
    p = Path(__file__).parent.parent / "data" / "anchor_library_starter.json"
    try:
        return {normalize_url(k): v for k, v in
                json.load(open(p)).get("destinations", {}).items()}
    except Exception:
        return {}


def tier_budget(tier, grammar):
    """Per-target inbound budget for this run, from tier_inbound_targets."""
    t = (grammar.get("tier_inbound_targets") or {}).get(tier, {})
    return int(t.get("target_max", 999999))


# ================= text utils =================

def _tokenize(text):
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+", str(text).lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


def _slug_tokens(url):
    if not url:
        return set()
    slug = url.rstrip("/").split("/")[-1]
    return _tokenize(slug.replace("-", " "))


def build_idf(tier_map):
    """IDF over title+slug tokens of the whole catalogue, so ubiquitous
    tokens ('loan', 'education') stop dominating relevance the way raw
    Jaccard let them."""
    docs = []
    for url, meta in tier_map.items():
        docs.append(_tokenize(meta.get("title", "")) | _slug_tokens(url))
    n = max(len(docs), 1)
    df_counts = {}
    for d in docs:
        for t in d:
            df_counts[t] = df_counts.get(t, 0) + 1
    return {t: math.log(n / c) for t, c in df_counts.items()}


def _tier_weight(tier):
    return {"T0": 1.5, "T1P": 1.4, "T1": 1.3, "T2": 1.1,
            "T3": 1.0, "T4": 0.9}.get(tier, 0.9)


def relevance_score(src_tokens, tgt_tokens, idf):
    inter = src_tokens & tgt_tokens
    if not inter:
        return 0.0
    num = sum(idf.get(t, 1.0) for t in inter)
    den = math.sqrt(sum(idf.get(t, 1.0) for t in src_tokens) *
                    sum(idf.get(t, 1.0) for t in tgt_tokens)) or 1.0
    return num / den


def visible_text_len(s):
    s = str(s or "")
    s = MD_LINK_RE.sub(r"\1", s)
    s = re.sub(r"<a\s+[^>]*>([^<]*)</a>", r"\1", s, flags=re.I | re.S)
    return len(s)


def scaffold_introduced(original_sentence, new_sentence):
    """Return the first scaffold phrase the edit INTRODUCED, or None."""
    o = " " + re.sub(r"\s+", " ", str(original_sentence).lower()) + " "
    n = " " + re.sub(r"\s+", " ", str(new_sentence).lower()) + " "
    n = MD_LINK_RE.sub(r"\1", n)
    for ph in SCAFFOLD_PHRASES:
        if ph in n and ph not in o:
            return ph
    return None


# ================= Stage A: paragraphs + candidates =================

def extract_paragraph_records(bodies, max_chars=MAX_BODY_CHARS_PER_PARA,
                              max_paras=MAX_PARAGRAPHS):
    """Paragraphs from BOTH body fields with a continuous prompt label.
    Each record: {label, field, pidx, text, eligible, why_ineligible}.
    pidx is the <p> index WITHIN its field (what the executor needs)."""
    recs = []
    label = 0
    for field in ["post-body", "post-body-2nd-half"]:
        html = bodies.get(field, "") or ""
        if not html.strip():
            continue
        soup = BeautifulSoup(html, "html.parser")
        for pidx, p in enumerate(soup.find_all("p")):
            if label >= max_paras:
                break
            txt = p.get_text(separator=" ").strip()
            if not txt or len(txt) < 20:
                continue
            recs.append({"label": label, "field": field, "pidx": pidx,
                         "text": txt[:max_chars], "eligible": True,
                         "why_ineligible": ""})
            label += 1
    if not recs:
        return recs
    recs[0]["eligible"] = False
    recs[0]["why_ineligible"] = "intro"
    recs[-1]["eligible"] = False
    recs[-1]["why_ineligible"] = "outro/CTA"
    for r in recs:
        low = r["text"].lower()
        if any(mp in low for mp in META_PHRASES):
            r["eligible"] = False
            r["why_ineligible"] = "meta-content"
    return recs


def compute_candidates(source_url, source_meta, tier_map, t0_pages, idf,
                       existing_targets, limit=CANDIDATE_POOL):
    src_tokens = _tokenize(source_meta.get("title", "")) | _slug_tokens(source_url)
    src_cat = str(source_meta.get("category", "") or "")
    out = []
    for tgt_url, meta in tier_map.items():
        if tgt_url == source_url.rstrip("/") or tgt_url in existing_targets:
            continue
        tgt_tokens = _tokenize(meta.get("title", "")) | _slug_tokens(tgt_url)
        base = relevance_score(src_tokens, tgt_tokens, idf)
        if base <= 0:
            continue
        tier = str(meta.get("tier", "T4") or "T4")
        score = base * _tier_weight(tier)
        if src_cat and str(meta.get("category", "")) == src_cat:
            score *= 1.3
        out.append({"target_url": tgt_url,
                    "target_title": meta.get("title", "") or
                    tgt_url.rsplit("/", 1)[-1].replace("-", " "),
                    "target_category": str(meta.get("category", "") or "?"),
                    "target_tier": tier,
                    "relevance": round(score, 4)})
    # T0 money pages are candidates too — but scored on actual token overlap
    # against a floor, NOT a flat 0.5 for everyone (that flat baseline is why
    # v5 put /site/education-loan into 56% of posts).
    for t0_url in t0_pages:
        t0n = normalize_url(t0_url)
        if t0n == source_url.rstrip("/") or t0n in existing_targets:
            continue
        if any(c["target_url"] == t0n for c in out):
            continue
        base = relevance_score(src_tokens, _slug_tokens(t0n), idf)
        out.append({"target_url": t0n,
                    "target_title": t0n.rsplit("/", 1)[-1].replace("-", " ").title(),
                    "target_category": "Money Page", "target_tier": "T0",
                    "relevance": round(max(base * _tier_weight("T0"), 0.05), 4)})
    out.sort(key=lambda c: c["relevance"], reverse=True)
    return out[:limit]


def edge_allowed(src_tier, tgt_tier, src_cat, tgt_cat):
    """Waterfall + category legality for NEW links (mirrors audit tolerances):
    T0 always allowed; same-category any direction (pillar→child is how
    clusters work); cross-category only via the bridge whitelist."""
    if tgt_tier == "T0":
        return True
    if src_cat and tgt_cat and src_cat == tgt_cat:
        return True
    return (src_cat, tgt_cat) in BRIDGE_WHITELIST


# ================= Stage B: wrap scanner =================

def phrase_candidates(target_url, target_title, anchor_lib):
    """Phrases worth scanning for, per target: library variants first
    (they're hand-curated), then the cleaned title."""
    phrases = list(anchor_lib.get(normalize_url(target_url), []))
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", str(target_title or "")).strip()
    title = re.sub(r"\s*[:|—-]\s*\d{4}.*$", "", title).strip()
    if title:
        phrases.append(title)
    seen, out = set(), []
    for ph in phrases:
        w = ph.split()
        if not (2 <= len(w) <= 8) or len(ph) < 8:
            continue
        k = ph.lower()
        if k not in seen:
            seen.add(k)
            out.append(ph)
    return out


def _phrase_regex(phrase):
    parts = [re.escape(w) for w in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def find_wrap(field_soups, para_records, target_url, phrases):
    """First eligible wrap for this target. Scans text NODES (never inside
    existing <a>), so the executor's single-node splice is guaranteed to
    apply. Returns a plan-row dict or None."""
    eligible = {(r["field"], r["pidx"]): r for r in para_records if r["eligible"]}
    for phrase in phrases:                       # library variants first
        rx = _phrase_regex(phrase)
        for field, soup in field_soups.items():
            for pidx, p in enumerate(soup.find_all("p")):
                rec = eligible.get((field, pidx))
                if rec is None:
                    continue
                for node in p.find_all(string=True):
                    if node.find_parent("a") is not None:
                        continue
                    raw = str(node)
                    m = rx.search(raw)
                    if not m:
                        continue
                    # sentence containing the match
                    start = 0
                    sentence = None
                    for s in SENT_SPLIT.split(raw):
                        end = start + len(s)
                        if start <= m.start() < end:
                            sentence = s
                            s_off = start
                            break
                        start = end + 1  # split consumed 1+ ws chars; approx
                    if sentence is None or len(sentence.strip()) < 25:
                        continue
                    sent = sentence.strip()
                    lead = len(sentence) - len(sentence.lstrip())
                    ms = m.start() - s_off - lead
                    me = ms + (m.end() - m.start())
                    if ms < 0 or me > len(sent):
                        continue
                    matched = sent[ms:me]
                    new_sent = sent[:ms] + f"[{matched}]({target_url})" + sent[me:]
                    return {"field": field, "paragraph_idx": pidx,
                            "original_sentence": sent, "new_sentence": new_sent,
                            "anchor": matched, "insertion_type": "wrap",
                            "reasoning": f"wrap-scan hit: '{phrase}'"}
    return None


# ================= Stage C: global allocator =================

class Allocator:
    def __init__(self, grammar, t0_pages):
        self.grammar = grammar
        self.t0 = {normalize_url(u) for u in t0_pages}
        self.per_post = {}
        self.per_post_t0 = {}
        self.used_paras = set()          # (source, field, pidx)
        self.target_inbound = {}
        self.anchor_counts = {}          # (target, anchor_lower) → n
        self._budget_cache = {}

    def _budget(self, tier):
        if tier not in self._budget_cache:
            self._budget_cache[tier] = tier_budget(tier, self.grammar)
        return self._budget_cache[tier]

    def can_take(self, edge, anchor=None):
        s, t = edge["source_url"], edge["target_url"]
        if self.per_post.get(s, 0) >= PER_POST_MAX:
            return False
        if t in self.t0 and self.per_post_t0.get(s, 0) >= PER_POST_T0_MAX:
            return False
        if self.target_inbound.get(t, 0) >= self._budget(edge["target_tier"]):
            return False
        if edge.get("field") is not None:
            if (s, edge["field"], edge["paragraph_idx"]) in self.used_paras:
                return False
        if anchor:
            if self.anchor_counts.get((t, anchor.lower()), 0) >= ANCHOR_REPEAT_CAP:
                return False
        return True

    def take(self, edge, anchor=None):
        s, t = edge["source_url"], edge["target_url"]
        self.per_post[s] = self.per_post.get(s, 0) + 1
        if t in self.t0:
            self.per_post_t0[s] = self.per_post_t0.get(s, 0) + 1
        self.target_inbound[t] = self.target_inbound.get(t, 0) + 1
        if edge.get("field") is not None:
            self.used_paras.add((s, edge["field"], edge["paragraph_idx"]))
        if anchor:
            k = (t, anchor.lower())
            self.anchor_counts[k] = self.anchor_counts.get(k, 0) + 1

    def release(self, edge, anchor=None):
        s, t = edge["source_url"], edge["target_url"]
        self.per_post[s] = max(0, self.per_post.get(s, 0) - 1)
        if t in self.t0:
            self.per_post_t0[s] = max(0, self.per_post_t0.get(s, 0) - 1)
        self.target_inbound[t] = max(0, self.target_inbound.get(t, 0) - 1)
        if anchor:
            k = (t, anchor.lower())
            self.anchor_counts[k] = max(0, self.anchor_counts.get(k, 0) - 1)


def allocate(wrap_edges, bridge_edges, grammar, t0_pages):
    """Select final edges. Wraps first (free quality), then bridges by
    relevance, but only up to PER_POST_TARGET per post via bridges — the
    last slot up to PER_POST_MAX is reserved for wraps, which are always
    worth taking."""
    alloc = Allocator(grammar, t0_pages)
    chosen_wraps, chosen_bridges = [], []
    for e in sorted(wrap_edges, key=lambda x: -x["relevance"]):
        if alloc.can_take(e, e["anchor"]):
            alloc.take(e, e["anchor"])
            chosen_wraps.append(e)
    for e in sorted(bridge_edges, key=lambda x: -x["relevance"]):
        if alloc.per_post.get(e["source_url"], 0) >= PER_POST_TARGET:
            continue
        if alloc.can_take(e):
            alloc.take(e)
            chosen_bridges.append(e)
    return chosen_wraps, chosen_bridges, alloc


# ================= Stage D: bridge writer =================

def build_bridge_prompt(source_url, source_title, source_category,
                        para_records, targets):
    paras = [r for r in para_records if r["eligible"]]
    para_block = "\n".join(f"[P{r['label']}] {r['text']}" for r in paras)
    tgt_block = "\n".join(
        f"  {j+1}. {t['target_url']}\n"
        f"     Title: {t['target_title']}\n"
        f"     Category: {t['target_category']} | Tier: {t['target_tier']}"
        for j, t in enumerate(targets))
    banned = ", ".join(f'"{p}"' for p in SCAFFOLD_PHRASES)
    return f"""You are inserting internal links into a Propelld blog post. These specific links were already selected by a relevance system — your ONLY job is finding the right sentence and making a minimal, natural edit.

SOURCE POST
  URL: {source_url}
  Title: {source_title}
  Category: {source_category}

ELIGIBLE PARAGRAPHS (intro, outro and meta paragraphs already removed):
{para_block}

LINKS TO PLACE (place each one if a natural spot exists, else skip it):
{tgt_block}

RULES — every one is enforced by code after you answer:
1. For each target: pick ONE paragraph (P#), ONE existing sentence in it, and rewrite that sentence with exactly one markdown link [anchor](exact-target-url). Copy the URL verbatim.
2. original_sentence: copy it CHARACTER-FOR-CHARACTER from the paragraph. If you can't find a sentence to edit verbatim, skip.
3. Minimal edit. The sentence must keep its meaning, voice and rhythm. Reader-visible growth must stay under {MAX_VISIBLE_DELTA} characters; under 30 is much better.
4. BEST option is always wrapping an existing phrase in the sentence as the anchor (near-zero added characters). Only add connecting words if no wrappable phrase exists.
5. FORBIDDEN connector phrases — your edit may not introduce any of: {banned}. These read as algorithmic filler at catalogue scale. If you feel pulled toward one, either wrap an existing phrase instead or integrate the reference as a plain grammatical part of the sentence (an appositive, an object, a parenthetical of 1-3 words).
6. Anchor text 2-8 words, descriptive of the target. Never "click here"/"read more"/generic verbs.
7. No em dashes (— or --). Max one insertion per paragraph. Never two targets in the same paragraph.
8. The target's concept must actually be discussed in the sentence's paragraph. If it's only category-adjacent, skip.

Return ONLY JSON:
{{
  "decisions": [
    {{"target_num": 1, "action": "insert", "target_url": "...", "paragraph_label": N,
      "original_sentence": "...", "new_sentence": "...", "anchor": "...",
      "reasoning": "1 short sentence"}},
    {{"target_num": 2, "action": "skip", "target_url": "...", "reasoning": "..."}}
  ]
}}"""


def build_retry_prompt(failures):
    items = "\n".join(
        f"  - target {f['target_url']}: your edit was rejected because "
        f"{f['why']}. Original sentence: \"{f['original_sentence']}\". "
        f"Your rejected version: \"{f['new_sentence']}\""
        for f in failures)
    return f"""Some of your insertions were rejected by the validator. Rewrite ONLY these, fixing the named problem. Same JSON format, same rules — especially: no forbidden connector phrases, keep visible growth minimal, prefer wrapping an existing phrase.

REJECTED:
{items}

Return ONLY JSON with a "decisions" array for these targets."""


def call_llm(client, prompt, model=MODEL):
    resp = client.messages.create(model=model, max_tokens=4000,
                                  messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON in response: {text[:300]}")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        end = text.rfind("}")
        if end == -1 or end <= start:
            raise ValueError(f"JSON parse failed ({e}): {text[start:start+300]}")
        obj = json.loads(text[start:end + 1])
    return obj


def _strip_em_dashes(text):
    text = re.sub(r"\s*[—]+\s*", ", ", str(text))
    return re.sub(r"\s*--\s*", ", ", text)


def validate_bridge_decision(d, para_by_label, allowed_targets, alloc, source_url):
    """Returns (ok, why, normalized_decision)."""
    if d.get("action") != "insert":
        return False, "skip", d
    tgt = normalize_url(str(d.get("target_url", "")))
    if tgt not in allowed_targets:
        return False, f"target {tgt} not in allocated list", d
    try:
        label = int(d.get("paragraph_label"))
    except (TypeError, ValueError):
        return False, "bad paragraph_label", d
    rec = para_by_label.get(label)
    if rec is None or not rec["eligible"]:
        return False, f"paragraph P{label} not eligible", d
    orig = str(d.get("original_sentence", "")).strip()
    new = str(d.get("new_sentence", ""))
    if not orig:
        return False, "empty original_sentence", d
    orig_norm = re.sub(r"\s+", " ", orig).strip()
    para_norm = re.sub(r"\s+", " ", rec["text"]).strip()
    if orig_norm not in para_norm:
        return False, "original_sentence not verbatim in paragraph", d
    if "—" not in orig and "--" not in orig:
        new = _strip_em_dashes(new)
        d["new_sentence"] = new
    links = MD_LINK_RE.findall(new)
    if len(links) != 1:
        return False, f"expected exactly 1 markdown link, got {len(links)}", d
    anchor, href = links[0]
    if normalize_url(href.strip()) != tgt:
        return False, "link URL differs from allocated target", d
    if not (2 <= len(anchor.split()) <= 8):
        return False, f"anchor '{anchor}' not 2-8 words", d
    delta = visible_text_len(new) - len(orig)
    if delta > MAX_VISIBLE_DELTA:
        return False, f"adds {delta} visible chars (cap {MAX_VISIBLE_DELTA})", d
    ph = scaffold_introduced(orig, new)
    if ph:
        return False, f"introduces forbidden connector phrase '{ph}'", d
    if (source_url, rec["field"], rec["pidx"]) in alloc.used_paras:
        return False, f"paragraph P{label} already used in this post", d
    if alloc.anchor_counts.get((tgt, anchor.lower()), 0) >= ANCHOR_REPEAT_CAP:
        return False, f"anchor '{anchor}' hit global repetition cap", d
    d["anchor"] = anchor
    d["_field"] = rec["field"]
    d["_pidx"] = rec["pidx"]
    d["target_url"] = tgt
    return True, "", d


# ================= main =================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    p.add_argument("--output", default="out/insertion-plans.csv")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--candidates", type=int, default=CANDIDATE_POOL)
    p.add_argument("--no-llm", action="store_true",
                   help="Wrap-scan + allocation only; skip bridge writing ($0)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()

    if not a.apply and not a.dry_run:
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    print(f"Loading tier map from {a.tier_file}...")
    tier_map = load_tier_map(a.tier_file)
    grammar = load_grammar()
    t0_pages = [normalize_url(u) for u in grammar.get("t0_money_pages", [])]
    anchor_lib = load_anchor_library()
    idf = build_idf(tier_map)
    print(f"  {len(tier_map)} posts | {len(t0_pages)} T0 pages | "
          f"{len(anchor_lib)} anchor-library destinations")

    sources = [(u, m) for u, m in tier_map.items()
               if u.startswith("/site/blog/") and str(m.get("tier")) != "T0"]
    if a.limit > 0:
        random.seed(42)
        by_tier = {}
        for u, m in sources:
            by_tier.setdefault(str(m.get("tier", "?")), []).append((u, m))
        total = len(sources)
        picked = []
        for tier, items in by_tier.items():
            share = max(1, round(a.limit * len(items) / total))
            random.shuffle(items)
            picked.extend(items[:share])
        random.shuffle(picked)
        sources = picked[:a.limit]
        dist = {}
        for u, m in sources:
            dist[str(m.get("tier"))] = dist.get(str(m.get("tier")), 0) + 1
        print(f"Stratified sample of {len(sources)}: {dict(sorted(dist.items()))}")
    else:
        print(f"Processing all {len(sources)} sources")

    print("Fetching bodies from Webflow...")
    wf = WebflowClient()
    want = {u.rsplit("/", 1)[-1] for u, _ in sources}
    slug_to_item = {}
    for item in wf.list_items(COLLECTIONS["blog_posts"]):
        s = item.get("fieldData", {}).get("slug")
        if s in want:
            slug_to_item[s] = item
    print(f"  Cached {len(slug_to_item)} of {len(want)}")

    # -------- Pass 1: deterministic per-source analysis --------
    wrap_edges, bridge_edges = [], []
    source_ctx = {}
    status_rows = []
    for source_url, meta in sources:
        slug = source_url.rsplit("/", 1)[-1]
        item = slug_to_item.get(slug)
        if not item:
            status_rows.append({"source_url": source_url, "status": "no-matching-item"})
            continue
        bodies = get_blog_body(item)
        para_records = extract_paragraph_records(bodies)
        if len([r for r in para_records if r["eligible"]]) < 1 or len(para_records) < 3:
            status_rows.append({"source_url": source_url, "status": "body-too-short"})
            continue
        field_soups = {f: BeautifulSoup(bodies.get(f, "") or "", "html.parser")
                       for f in ["post-body", "post-body-2nd-half"]
                       if (bodies.get(f) or "").strip()}
        existing = set()
        for html in bodies.values():
            for l in extract_links(html):
                existing.add(l["href"])
        cands = compute_candidates(source_url, meta, tier_map, t0_pages, idf,
                                   existing, limit=a.candidates)
        src_tier = str(meta.get("tier", "T4"))
        src_cat = str(meta.get("category", "") or "")
        source_ctx[source_url] = {"slug": slug, "meta": meta,
                                  "para_records": para_records}
        for c in cands:
            if not edge_allowed(src_tier, c["target_tier"], src_cat,
                                c["target_category"]):
                continue
            base = {"source_url": source_url, "target_url": c["target_url"],
                    "target_title": c["target_title"],
                    "target_category": c["target_category"],
                    "target_tier": c["target_tier"], "relevance": c["relevance"]}
            wrap = find_wrap(field_soups, para_records, c["target_url"],
                             phrase_candidates(c["target_url"],
                                               c["target_title"], anchor_lib))
            if wrap:
                wrap_edges.append({**base, **wrap})
            else:
                bridge_edges.append({**base, "field": None, "paragraph_idx": None})

    print(f"\nEdge pool: {len(wrap_edges)} wrap hits, {len(bridge_edges)} bridge candidates")

    # -------- Pass 2: global allocation --------
    chosen_wraps, chosen_bridges, alloc = allocate(wrap_edges, bridge_edges,
                                                   grammar, t0_pages)
    print(f"Allocated: {len(chosen_wraps)} wraps + {len(chosen_bridges)} bridge slots")

    plans = list(status_rows)
    for e in chosen_wraps:
        plans.append({"source_url": e["source_url"],
                      "source_slug": source_ctx[e["source_url"]]["slug"],
                      "field": e["field"], "paragraph_idx": e["paragraph_idx"],
                      "target_url": e["target_url"], "action": "insert",
                      "insertion_type": "wrap", "anchor": e["anchor"],
                      "original_sentence": e["original_sentence"],
                      "new_sentence": e["new_sentence"],
                      "reasoning": e["reasoning"], "validation_error": "",
                      "status": "planned"})

    # -------- Pass 3: LLM bridge writing --------
    by_source = {}
    for e in chosen_bridges:
        by_source.setdefault(e["source_url"], []).append(e)

    if a.dry_run:
        for i, (su, targets) in enumerate(by_source.items()):
            if i >= 2:
                break
            ctx = source_ctx[su]
            print("\n" + "=" * 80)
            print(build_bridge_prompt(su, ctx["meta"].get("title", ""),
                                      ctx["meta"].get("category", ""),
                                      ctx["para_records"], targets)[:3000])
        print(f"\nDRY-RUN: would send {len(by_source)} bridge calls "
              f"covering {len(chosen_bridges)} edges")
        return

    if a.no_llm:
        print("--no-llm: skipping bridge writing; wraps only")
        for e in chosen_bridges:
            alloc.release(e)
    else:
        if anthropic is None:
            print("ERROR: anthropic package missing; use --no-llm or install it")
            sys.exit(1)
        ac = anthropic.Anthropic()
        for i, (su, targets) in enumerate(by_source.items()):
            ctx = source_ctx[su]
            para_by_label = {r["label"]: r for r in ctx["para_records"]}
            allowed = {e["target_url"] for e in targets}
            edge_by_target = {e["target_url"]: e for e in targets}
            settled = set()

            def handle(decisions, failures):
                for d in decisions:
                    tgt = normalize_url(str(d.get("target_url", "")))
                    if tgt in settled:
                        continue
                    ok, why, d = validate_bridge_decision(
                        d, para_by_label, allowed, alloc, su)
                    if ok:
                        e = edge_by_target[tgt]
                        alloc.used_paras.add((su, d["_field"], d["_pidx"]))
                        k = (tgt, d["anchor"].lower())
                        alloc.anchor_counts[k] = alloc.anchor_counts.get(k, 0) + 1
                        settled.add(tgt)
                        plans.append({"source_url": su, "source_slug": ctx["slug"],
                                      "field": d["_field"],
                                      "paragraph_idx": d["_pidx"],
                                      "target_url": tgt, "action": "insert",
                                      "insertion_type": "bridge",
                                      "anchor": d["anchor"],
                                      "original_sentence": d["original_sentence"],
                                      "new_sentence": d["new_sentence"],
                                      "reasoning": d.get("reasoning", ""),
                                      "validation_error": "",
                                      "status": "planned"})
                    elif why not in ("skip",) and d.get("action") == "insert":
                        failures.append({"target_url": tgt, "why": why,
                                         "original_sentence":
                                             str(d.get("original_sentence", ""))[:200],
                                         "new_sentence":
                                             str(d.get("new_sentence", ""))[:200]})

            try:
                prompt = build_bridge_prompt(su, ctx["meta"].get("title", ""),
                                             ctx["meta"].get("category", ""),
                                             ctx["para_records"], targets)
                result = call_llm(ac, prompt, model=a.model)
                failures = []
                handle(result.get("decisions", []), failures)
                if failures:  # one retry with the failure named
                    retry = call_llm(
                        ac, prompt + "\n\n" + build_retry_prompt(failures),
                        model=a.model)
                    handle(retry.get("decisions", []), [])
            except Exception as e:
                plans.append({"source_url": su,
                              "status": f"error: {str(e)[:200]}"})
            # release unfilled bridge slots
            for e in targets:
                if e["target_url"] not in settled:
                    alloc.release(e)
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(by_source)}] bridge calls done")
            time.sleep(a.sleep)

    # -------- output + run report --------
    df = pd.DataFrame(plans)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.output, index=False)
    print(f"\n✓ Wrote {a.output}")

    ins = df[df.get("action").eq("insert")] if "action" in df.columns else pd.DataFrame()
    if len(ins):
        print("\n=== RUN REPORT (v6) ===")
        print(f"Insertions planned: {len(ins)} "
              f"({(ins['insertion_type'] == 'wrap').sum()} wraps, "
              f"{(ins['insertion_type'] == 'bridge').sum()} bridges)")
        per_post = ins.groupby("source_url").size()
        print(f"Sources with insertions: {len(per_post)} | "
              f"mean {per_post.mean():.2f} | median {per_post.median():.0f}")
        print("Per-post distribution:")
        print(per_post.value_counts().sort_index().to_string())
        print("Top 10 targets:")
        print(ins["target_url"].value_counts().head(10).to_string())
        scaf = sum(1 for _, r in ins.iterrows()
                   if scaffold_introduced(r["original_sentence"], r["new_sentence"]))
        print(f"Insertions introducing scaffold phrases: {scaf} (must be 0)")
        t0set = set(t0_pages)
        t0_per_post = ins[ins["target_url"].isin(t0set)].groupby("source_url").size()
        over = (t0_per_post > PER_POST_T0_MAX).sum()
        print(f"Posts exceeding {PER_POST_T0_MAX} T0 CTAs: {over} (must be 0)")


if __name__ == "__main__":
    main()
