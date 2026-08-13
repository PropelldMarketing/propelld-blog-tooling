#!/usr/bin/env python3
"""
Phase 2+3: evidence gathering + patch planning for freshness candidates.

Input:  out/freshness-inventory.csv  (from freshness_scan.py)
Output: out/freshness-plans.csv      (one row per candidate, full funnel)

Lanes:
  A    -> mechanical plan generated in code from the whitelisted rule ($0).
  B/LLM-> official source pages fetched (data/freshness_sources.json),
          Sonnet judges staleness and proposes a MINIMAL substitution with
          confidence + reasoning + verbatim evidence quote.

Code-enforced validators (LLM output is never trusted):
  - old_text must appear whitespace-normalized in the candidate's context
  - pure substitution: only fact-shaped tokens may change
  - every changed fact token must appear in the evidence quote
  - the evidence quote must appear verbatim in the fetched source text
  - the source domain must be on the exam's whitelist
  - title/table-cell changes are capped at MEDIUM (queue) regardless of LLM
One retry with the failure named; then dropped with reason.

Statuses: planned | queued | not-stale | ignored | dropped | error:<msg>
Only `planned` rows are ever applied; freshness_apply.py decides which
(Lane A + HIGH auto; everything else needs approved=YES from the human).

Checkpointing: output flushed after EVERY post. --resume re-reads a partial
output and skips only posts whose rows are all terminal (error rows are
retried, unlike the sister project's resume).

Requires --apply (spend on LLM + fetches) or --dry-run ($0: Lane A plans
only, factual lanes queued unverified).
"""

import os
import re
import sys
import csv
import json
import argparse
import datetime
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.freshness_utils import (load_rules, current_session, norm_ws,
                                 pure_substitution_check, evidence_supports,
                                 SESSION_RE)

MODEL = "claude-sonnet-4-6"
MAX_SOURCE_CHARS = 8000
BATCH_SIZE = 25
TIER_PRIORITY = {"T1P": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
COLUMNS = ["slug", "item_id", "url", "tier", "field", "location", "block_id",
           "claim_type", "lane", "priority", "matched_text", "context",
           "old_text", "new_text", "confidence", "reasoning", "source_url",
           "source_tier", "evidence_quote", "status", "validation_error"]
TERMINAL = ("planned", "queued", "not-stale", "ignored", "dropped")

STATE_LOCK = threading.Lock()
FETCH_CACHE = {}


# ---------------------------------------------------------------- sources

def load_sources(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def exam_key_for(post_title, slug, sources):
    hay = ("%s %s" % (post_title, slug.replace("-", " "))).lower()
    for alias, canonical in sources.get("aliases", {}).items():
        hay = hay.replace(alias, canonical)
    best = None
    for key in sources["exams"]:
        # "s?" tolerates plurals: "jee mains" matched nothing in batch 1.
        if re.search(r"\b%ss?\b" % re.escape(key), hay):
            if best is None or len(key) > len(best):
                best = key  # longest match wins ("jee advanced" over "jee")
    return best


def tier_of(url, sources):
    """Audit gate for any URL the discovery step proposes: 'official',
    'secondary', or None (unknown domain, never used)."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return None
    def _match(domains):
        return any(host == d or host.endswith("." + d) for d in domains)
    for entry in sources["exams"].values():
        if _match(entry["domains"]):
            return "official"
    if _match(sources["fallback"]["domains"]):
        return "official"
    if _match(sources.get("secondary_domains", [])):
        return "secondary"
    return None


DISCOVER_PROMPT = """Find web pages that state the CURRENT (as of {today}) values for these possibly-outdated claims from an article titled "{title}":

{claims}

Search for official exam-body / institute pages first, then major Indian education portals. Reply with ONLY JSON: {{"urls": ["...", ...]}} (up to 5 URLs, most authoritative first). No commentary."""


def discover_sources(llm_client, title, claims, sources, budget, today):
    """LLM web-search discovery of source URLs. The URLs are NOT trusted:
    tier_of() rejects unknown domains, and every page is fetched + evidence
    must appear verbatim in the fetched text like any other source."""
    with STATE_LOCK:
        if budget["searches"] <= 0:
            return {}, {}
        budget["searches"] -= 1
    claims_txt = "\n".join("- %s (found as: %s)" %
                           (c["context"][:160], c["matched_text"])
                           for c in claims[:8])
    try:
        resp = llm_client.messages.create(
            model=MODEL, max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": 3}],
            messages=[{"role": "user", "content": DISCOVER_PROMPT.format(
                today=today.isoformat(), title=title, claims=claims_txt)}])
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        start = text.find("{")
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        urls = obj.get("urls", [])[:5]
    except Exception:
        return {}, {}
    texts, tiers = {}, {}
    for u in urls:
        t = tier_of(u, sources)
        if t is None:
            continue  # audit: unknown domains are never used
        page, err = fetch_source_text(u, budget)
        if page:
            texts[u], tiers[u] = page, t
    return texts, tiers


def fetch_source_text(url, budget):
    """Fetch + extract page text. Cached per URL. Returns (text, err)."""
    with STATE_LOCK:
        if url in FETCH_CACHE:
            return FETCH_CACHE[url]
        if budget["fetches"] <= 0:
            return "", "fetch budget exhausted"
        budget["fetches"] -= 1
    import requests
    from bs4 import BeautifulSoup
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PropelldFreshness/1.0)"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = norm_ws(soup.get_text(separator=" "))[:60000]
        result = (text, "")
    except Exception as e:
        result = ("", "fetch failed: %s" % str(e)[:150])
    with STATE_LOCK:
        FETCH_CACHE[url] = result
    return result


def relevant_excerpts(text, today):
    """Windows around freshness-relevant tokens, capped."""
    if not text:
        return ""
    years = [str(today.year - 1), str(today.year), str(today.year + 1)]
    rx = re.compile(r"(%s|registration|application|exam date|last date|fee)"
                    % "|".join(years), re.I)
    spans, out, used = [], [], 0
    for m in rx.finditer(text):
        lo, hi = max(0, m.start() - 200), min(len(text), m.end() + 200)
        if spans and lo <= spans[-1][1]:
            spans[-1] = (spans[-1][0], hi)
        else:
            spans.append((lo, hi))
    for lo, hi in spans:
        if used >= MAX_SOURCE_CHARS:
            break
        chunk = text[lo:hi]
        out.append(chunk)
        used += len(chunk)
    return "\n...\n".join(out)[:MAX_SOURCE_CHARS]


# ---------------------------------------------------------------- LLM

PROMPT = """You are a fact-checker for an Indian education-finance blog. Today is {today}. The current academic session is {session}.

For each numbered candidate below, judge whether the claim is STALE on a page students read today, using ONLY the official source excerpts provided. Propose the SMALLEST possible text substitution: change fact tokens (years, dates, amounts, open/closed) and nothing else. Never reword, never add sentences.

Rules:
- verdict: one of "stale", "not_stale", "historical" (the old year/date is a correct historical reference and must stay), "unverifiable" (sources don't establish the current value).
- For "stale": old_text = a short phrase copied VERBATIM from the candidate's context containing the stale fact (long enough to be unique); new_text = same phrase with only the fact tokens corrected; evidence_quote = a sentence copied VERBATIM from the source excerpts that proves the new value; source_url = which source it came from.
- confidence: "HIGH" only if the source states the new value explicitly and unambiguously; "MEDIUM" if inference was needed; "LOW" otherwise.
- reasoning: one sentence explaining the confidence and the change.
- If sources are empty or irrelevant, verdict = "unverifiable". NEVER guess a date, fee, or year from memory.

Post title: {title}
Source excerpts:
{sources}

Candidates (JSON): {candidates}

Reply with ONLY JSON: {{"decisions": [{{"id": <int>, "verdict": "...", "old_text": "...", "new_text": "...", "confidence": "...", "reasoning": "...", "evidence_quote": "...", "source_url": "..."}}]}}
For non-stale verdicts, omit old_text/new_text/evidence_quote or leave them empty."""


def call_llm(client, prompt, budget):
    with STATE_LOCK:
        if budget["llm"] <= 0:
            raise RuntimeError("LLM call budget exhausted")
        budget["llm"] -= 1
    resp = client.messages.create(model=MODEL, max_tokens=8000,
                                  messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON in LLM response")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        obj = json.loads(text[start:text.rfind("}") + 1])
    return obj


# ---------------------------------------------------------------- validation

def validate_decision(dec, cand, source_texts, tiers):
    """Full code-side validation. Returns (ok, error).

    `tiers` maps every audited, fetched source URL -> 'official'/'secondary'.
    Citing anything outside it (unfetched or unknown domain) is rejected."""
    old, new = dec.get("old_text", ""), dec.get("new_text", "")
    ev, src = dec.get("evidence_quote", ""), dec.get("source_url", "")
    if not old or not new:
        return False, "missing old_text/new_text"
    if norm_ws(old) not in norm_ws(cand["context"]):
        return False, "old_text not verbatim in candidate context"
    ok, why = pure_substitution_check(old, new)
    if not ok:
        return False, "not pure substitution: %s" % why
    if not ev:
        return False, "missing evidence_quote"
    if not src or src not in tiers:
        return False, "source_url not among audited fetched sources: %s" % src
    stext = source_texts.get(src, "")
    if norm_ws(ev).lower() not in stext.lower():
        return False, "evidence_quote not verbatim in fetched source"
    ok, missing = evidence_supports(old, new, ev)
    if not ok:
        return False, "changed tokens not in evidence: %s" % missing
    return True, ""


def corroboration_count(dec, source_texts):
    """Distinct domains whose fetched text supports the changed fact tokens."""
    doms = set()
    for u, txt in source_texts.items():
        ok, _ = evidence_supports(dec["old_text"], dec["new_text"], txt)
        if ok:
            doms.add(urlparse(u).netloc.lower())
    return len(doms)


# ---------------------------------------------------------------- per post

def _context_span(context, matched, words=3):
    """A phrase of ±N words around `matched` inside its context, so the
    apply-time substitution has enough text to be unambiguous."""
    ctx = re.sub(r"\s+", " ", str(context)).strip()
    pos = ctx.find(str(matched))
    if pos == -1:
        return str(matched)
    before = ctx[:pos].split()[-words:]
    after = ctx[pos + len(str(matched)):].split()[:words]
    return " ".join(before + [str(matched)] + after)


def plan_lane_a(cand, rules, today):
    """Mechanical plans, $0: whitelisted session rollovers and label year
    bumps (owner 2026-08-13)."""
    row = dict(cand)
    if cand["claim_type"] == "session_range":
        target = current_session(today, rules.get("session_start_month", 4))
        m = SESSION_RE.search(cand["matched_text"])
        row.update({
            "old_text": cand["matched_text"],
            "new_text": target if m else "",
            "confidence": "RULE",
            "reasoning": cand["lane_reason"],
            "status": "planned" if m else "dropped",
            "validation_error": "" if m else "could not parse session range",
        })
    elif cand["claim_type"] == "year":
        old_span = _context_span(cand["context"], cand["matched_text"])
        row.update({
            "old_text": old_span,
            "new_text": old_span.replace(str(cand["matched_text"]),
                                         str(today.year), 1),
            "confidence": "RULE",
            "reasoning": cand["lane_reason"],
            "status": "planned",
        })
    else:
        row.update({"status": "dropped",
                    "validation_error": "no lane-a planner for claim_type"})
    return row


DATA_CLAIMS = ("date", "numeric_date", "fee", "registration_phrase",
               "session_range")


def _unresolved_data(r):
    """A data claim that is NOT affirmatively current: stale data may be
    sitting on the page."""
    if r.get("claim_type") not in DATA_CLAIMS:
        return False
    st = str(r.get("status", ""))
    if st in ("planned", "not-stale"):
        return False
    why = str(r.get("reasoning", "")) + str(r.get("lane_reason", ""))
    if st == "ignored" and "future" in why:
        return False        # future date: already current
    return True             # queued, dropped, error, ignored-as-passed


def enforce_consistency(all_rows):
    """VITEEE lesson (owner review 2026-08-13): a year label was bumped
    2025->2026 in a table whose 'last date' cells kept 2025's dates, making
    the page claim to be updated while showing stale data. Rule: a planned
    year/session bump is HELD (queued) if unverified data claims share its
    table, or its immediate context. Mutates rows in place."""
    held_reason = (" [held for consistency: nearby dates/fees are not "
                   "verified as current; bumping the year would mislabel "
                   "stale data]")

    def hold(r):
        r["status"], r["confidence"] = "queued", "LOW"
        r["reasoning"] = str(r.get("reasoning", "")) + held_reason

    by_block = {}
    for r in all_rows:
        b = str(r.get("block_id", "") or "")
        if b:
            by_block.setdefault((r.get("field"), b), []).append(r)
    for rows in by_block.values():
        if any(_unresolved_data(r) for r in rows):
            for r in rows:
                if r.get("status") == "planned" and \
                        r.get("claim_type") in ("year", "session_range"):
                    hold(r)

    by_field = {}
    for r in all_rows:
        by_field.setdefault(r.get("field"), []).append(r)
    for rows in by_field.values():
        unres = [str(u.get("matched_text", "")) for u in rows
                 if _unresolved_data(u)
                 and len(str(u.get("matched_text", ""))) >= 6]
        if not unres:
            continue
        for r in rows:
            if r.get("status") == "planned" and r.get("claim_type") == "year":
                ctx = str(r.get("context", ""))
                if any(u in ctx for u in unres):
                    hold(r)
    return all_rows


def process_post(slug, cands, ctx):
    """Plan all candidates for one post. Returns list of output rows."""
    rules, sources, today, llm_client, budget, dry = (
        ctx["rules"], ctx["sources"], ctx["today"], ctx["llm"], ctx["budget"],
        ctx["dry"])
    rows = []
    base = {k: cands[0][k] for k in ("slug", "item_id", "url", "tier")}
    factual = []
    for c in cands:
        if c["lane"] == "IGNORE":
            rows.append({**c, "status": "ignored",
                         "reasoning": c["lane_reason"]})
        elif c["lane"] == "A":
            rows.append(plan_lane_a(c, rules, today))
        else:
            factual.append(c)
    if not factual:
        return rows
    if dry:
        for c in factual:
            rows.append({**c, "status": "queued", "confidence": "LOW",
                         "reasoning": "dry-run: no evidence fetched"})
        return rows

    title = ctx["titles"].get(slug, "")
    key = exam_key_for(title, slug, sources)
    entry = sources["exams"].get(key) if key else None
    source_texts, tiers, fetch_errs = {}, {}, []
    if entry:
        for u in entry["urls"]:
            text, err = fetch_source_text(u, budget)
            if text:
                source_texts[u], tiers[u] = text, "official"
            elif err:
                fetch_errs.append("%s: %s" % (u, err))

    # Discovery pass 1 (2026-08-13 owner direction, go deeper): no mapping
    # or nothing fetchable -> web-search for sources. URLs are audited by
    # tier_of(); unknown domains are never used.
    discovery_used = False
    if not source_texts and llm_client is not None:
        discovery_used = True
        dt, dtiers = discover_sources(llm_client, title, factual, sources,
                                      budget, today)
        source_texts.update(dt)
        tiers.update(dtiers)

    if not source_texts:
        why = ("no source found via mapping or discovery" if not entry
               else "; ".join(fetch_errs)[:200])
        for c in factual:
            rows.append({**c, "status": "queued", "confidence": "LOW",
                         "reasoning": "unverifiable: %s" % why})
        return rows

    def plan_over(cands):
        excerpts = "\n\n".join(
            "SOURCE %s:\n%s" % (u, relevant_excerpts(t, today))
            for u, t in source_texts.items())
        # Chunk: some posts have 200+ candidates (allen-kota-fees hit 278
        # in the first live scan); one giant call truncates. 25 per call.
        out = []
        for lo in range(0, len(cands), BATCH_SIZE):
            out.extend(_plan_batch(cands[lo:lo + BATCH_SIZE], excerpts, title,
                                   source_texts, tiers, ctx))
        return out

    plan_rows = plan_over(factual)

    # Discovery pass 2: claims the LLM judged unverifiable against the
    # mapped sources get one web-search round for additional sources.
    unver = [i for i, r in enumerate(plan_rows)
             if r.get("status") == "queued"
             and str(r.get("reasoning", "")).startswith("unverifiable")]
    if unver and not discovery_used and llm_client is not None:
        dt, dtiers = discover_sources(llm_client, title,
                                      [factual[i] for i in unver], sources,
                                      budget, today)
        new_urls = set(dt) - set(source_texts)
        if new_urls:
            source_texts.update(dt)
            tiers.update(dtiers)
            redo = plan_over([factual[i] for i in unver])
            for j, i in enumerate(unver):
                plan_rows[i] = redo[j]

    return enforce_consistency(rows + plan_rows)


def _plan_batch(factual, excerpts, title, source_texts, tiers, ctx):
    rules, today, llm_client, budget = (ctx["rules"], ctx["today"], ctx["llm"],
                                        ctx["budget"])
    rows = []

    def finalize_tiered(c, d):
        """Apply the secondary-source audit policy, then location ceilings.
        Secondary sources NEVER reach HIGH (never auto-apply); a secondary
        fact needs corroboration on 2+ distinct domains for MEDIUM."""
        src_tier = tiers.get(d.get("source_url", ""), "official")
        if src_tier == "secondary":
            n = corroboration_count(d, source_texts)
            if n >= 2:
                if str(d.get("confidence", "")).upper() == "HIGH":
                    d["confidence"] = "MEDIUM"
                d["reasoning"] = (str(d.get("reasoning", "")) +
                                  " [secondary sources, corroborated on %d "
                                  "domains: capped to MEDIUM]" % n)
            else:
                d["confidence"] = "LOW"
                d["reasoning"] = (str(d.get("reasoning", "")) +
                                  " [single secondary source: needs "
                                  "corroboration, queued]")
        row = _finalize(c, d, rules)
        row["source_tier"] = src_tier
        return row
    cand_json = json.dumps([
        {"id": i, "claim_type": c["claim_type"], "location": c["location"],
         "matched_text": c["matched_text"], "context": c["context"]}
        for i, c in enumerate(factual)], ensure_ascii=False)
    prompt = PROMPT.format(today=today.isoformat(),
                           session=current_session(
                               today, rules.get("session_start_month", 4)),
                           title=title, sources=excerpts, candidates=cand_json)

    def run_round(p):
        obj = call_llm(llm_client, p, budget)
        return {d.get("id"): d for d in obj.get("decisions", [])}

    try:
        decisions = run_round(prompt)
    except Exception:
        # One retry: truncated/malformed JSON killed a full 25-candidate
        # batch in the 2026-08 canary.
        try:
            decisions = run_round(
                prompt + "\n\nYour previous reply was not valid, complete "
                "JSON. Reply again with ONLY the complete JSON object.")
        except Exception as e:
            return rows + [{**c, "status": "error: llm %s" % str(e)[:150]}
                           for c in factual]

    retry_items, resolved = [], {}
    for i, c in enumerate(factual):
        d = decisions.get(i)
        if d is None:
            resolved[i] = {**c, "status": "dropped",
                           "validation_error": "no decision returned"}
            continue
        v = d.get("verdict", "")
        if v == "not_stale":
            resolved[i] = {**c, "status": "not-stale",
                           "reasoning": d.get("reasoning", "")}
        elif v == "historical":
            resolved[i] = {**c, "status": "ignored",
                           "reasoning": "historical: %s" % d.get("reasoning", "")}
        elif v == "unverifiable":
            resolved[i] = {**c, "status": "queued", "confidence": "LOW",
                           "reasoning": "unverifiable: %s" % d.get("reasoning", "")}
        elif v == "stale":
            ok, err = validate_decision(d, c, source_texts, tiers)
            if ok:
                resolved[i] = finalize_tiered(c, d)
            else:
                retry_items.append((i, c, err))
        else:
            resolved[i] = {**c, "status": "dropped",
                           "validation_error": "bad verdict: %r" % v}

    if retry_items:
        named = "\n".join("candidate %d failed validation: %s" % (i, err)
                          for i, _c, err in retry_items)
        try:
            decisions2 = run_round(
                prompt + "\n\nYour previous answer failed these code checks; "
                "fix ONLY these candidates:\n" + named)
        except Exception:
            decisions2 = {}
        for i, c, err in retry_items:
            d = decisions2.get(i)
            if d and d.get("verdict") == "stale":
                ok, err2 = validate_decision(d, c, source_texts, tiers)
                resolved[i] = (finalize_tiered(c, d) if ok else
                               {**c, "status": "dropped",
                                "validation_error": err2})
            else:
                resolved[i] = {**c, "status": "dropped",
                               "validation_error": err}
    rows.extend(resolved[i] for i in sorted(resolved))
    return rows


def _finalize(c, d, rules=None):
    """Policy ceilings enforced in code, not prompts. The autonomy dial lives
    in data/freshness_rules.json ("autonomy"): promoting a location to
    auto-apply after its precision is proven is a config edit, not code.

    Owner policy 2026-08-13: start with body prose (p, li) only; headings,
    titles, table cells queue (a heading's year may label year-specific
    content below it, e.g. "Allen Kota Results 2025"). Anchors never edit:
    their text mirrors a linked post's title (2026-08 canary: 9 HIGH rows)."""
    auto = (rules or {}).get("autonomy", {})
    auto_locs = auto.get("auto_locations", ["p", "li"])
    never_locs = auto.get("never_locations", ["a"])
    conf = d.get("confidence", "LOW").upper()
    reasoning = d.get("reasoning", "")
    if c["location"] not in auto_locs and c["location"] not in never_locs \
            and conf == "HIGH":
        conf = "MEDIUM"
        reasoning += " [capped to MEDIUM: %s changes always queue]" % c["location"]
    status = "planned" if conf in ("HIGH", "MEDIUM") else "queued"
    if c["location"] in never_locs:
        conf, status = "LOW", "queued"
        reasoning += " [anchor text: fix by updating the linked post's title]"
    return {**c, "old_text": d["old_text"], "new_text": d["new_text"],
            "confidence": conf, "reasoning": reasoning,
            "source_url": d.get("source_url", ""),
            "evidence_quote": d.get("evidence_quote", ""), "status": status}


# ---------------------------------------------------------------- main

def main():
    global MODEL
    ap = argparse.ArgumentParser(description="Freshness evidence + patch planner")
    ap.add_argument("--apply", action="store_true",
                    help="Spend: fetch sources + LLM planning.")
    ap.add_argument("--dry-run", action="store_true",
                    help="$0: Lane A plans only; factual lanes queued unverified.")
    ap.add_argument("--inventory", default="out/freshness-inventory.csv")
    ap.add_argument("--sources", default="data/freshness_sources.json")
    ap.add_argument("--rules", default="data/freshness_rules.json")
    ap.add_argument("--tier-file", default="data/posts-with-tiers.xlsx")
    ap.add_argument("--additions", default="data/tier-map-additions.csv")
    ap.add_argument("--output", default="out/freshness-plans.csv")
    ap.add_argument("--resume", default="",
                    help="Previous partial output CSV; completed posts skipped, error posts retried.")
    ap.add_argument("--limit", type=int, default=0, help="Max posts (0 = all).")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-llm-calls", type=int, default=400)
    ap.add_argument("--max-fetches", type=int, default=150)
    ap.add_argument("--max-searches", type=int, default=60,
                    help="Web-search discovery calls (source-finding).")
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()
    if not (a.apply or a.dry_run):
        print("Must pass --apply or --dry-run")
        sys.exit(1)

    MODEL = a.model
    import pandas as pd
    inv = pd.read_csv(a.inventory).fillna("")
    titles = {}
    from scripts.freshness_scan import load_tier_map  # same merge logic
    for slug, meta in load_tier_map(a.tier_file, a.additions).items():
        titles[slug] = str(meta["title"])

    kept_rows = []
    done = set()
    if a.resume and Path(a.resume).exists():
        prev = pd.read_csv(a.resume).fillna("")
        by_slug = prev.groupby("slug")
        for slug, grp in by_slug:
            statuses = set(grp["status"].astype(str))
            if all(any(s.startswith(t) for t in TERMINAL) for s in statuses):
                done.add(slug)
                kept_rows.extend(grp.to_dict("records"))
        print("Resume: %d posts done, retrying the rest" % len(done))

    groups = [(slug, grp.to_dict("records"))
              for slug, grp in inv.groupby("slug") if slug not in done]
    # Budget: recency-priority posts first (owner policy 2026-08-13), then
    # high-value tiers.
    def _grp_key(g):
        recs = g[1]
        has_high = any(str(r.get("priority", "")) == "high" for r in recs)
        return (0 if has_high else 1,
                TIER_PRIORITY.get(str(recs[0].get("tier", "")), 9))
    groups.sort(key=_grp_key)
    if a.limit:
        groups = groups[:a.limit]

    llm_client = None
    if a.apply:
        import anthropic
        llm_client = anthropic.Anthropic()
    ctx = {"rules": load_rules(a.rules), "sources": load_sources(a.sources),
           "today": datetime.date.today(), "llm": llm_client,
           "budget": {"llm": a.max_llm_calls, "fetches": a.max_fetches,
                      "searches": a.max_searches},
           "dry": not a.apply, "titles": titles}

    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    out_f = open(a.output, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in kept_rows:
        writer.writerow(r)
    out_f.flush()

    funnel = {}

    def emit(rows):
        with STATE_LOCK:
            for r in rows:
                st = str(r.get("status", "")).split(":")[0]
                funnel[st] = funnel.get(st, 0) + 1
                writer.writerow(r)
            out_f.flush()   # checkpoint after EVERY post

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(process_post, slug, cands, ctx): slug
                for slug, cands in groups}
        for fut in as_completed(futs):
            slug = futs[fut]
            try:
                emit(fut.result())
            except Exception as e:
                emit([{"slug": slug, "status": "error: %s" % str(e)[:180]}])

    out_f.close()
    print("Funnel: %s" % funnel)
    print("Budget left: %s" % ctx["budget"])
    print("Wrote %s" % a.output)


if __name__ == "__main__":
    main()
