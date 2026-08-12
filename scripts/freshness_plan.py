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
COLUMNS = ["slug", "item_id", "url", "tier", "field", "location", "claim_type",
           "lane", "matched_text", "context", "old_text", "new_text",
           "confidence", "reasoning", "source_url", "evidence_quote",
           "status", "validation_error"]
TERMINAL = ("planned", "queued", "not-stale", "ignored", "dropped")

STATE_LOCK = threading.Lock()
FETCH_CACHE = {}


# ---------------------------------------------------------------- sources

def load_sources(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def exam_key_for(post_title, slug, sources):
    hay = ("%s %s" % (post_title, slug.replace("-", " "))).lower()
    best = None
    for key in sources["exams"]:
        if re.search(r"\b%s\b" % re.escape(key), hay):
            if best is None or len(key) > len(best):
                best = key  # longest match wins ("jee advanced" over "jee")
    return best


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
    resp = client.messages.create(model=MODEL, max_tokens=4000,
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

def domain_ok(url, domains):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def validate_decision(dec, cand, source_texts, domains):
    """Full code-side validation. Returns (ok, error)."""
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
    if not src or not domain_ok(src, domains):
        return False, "source_url not on whitelist: %s" % src
    stext = source_texts.get(src, "")
    if norm_ws(ev).lower() not in stext.lower():
        return False, "evidence_quote not verbatim in fetched source"
    ok, missing = evidence_supports(old, new, ev)
    if not ok:
        return False, "changed tokens not in evidence: %s" % missing
    return True, ""


# ---------------------------------------------------------------- per post

def plan_lane_a(cand, rules, today):
    """Mechanical plans, $0. Currently: whitelisted session rollovers."""
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
    else:
        row.update({"status": "dropped",
                    "validation_error": "no lane-a planner for claim_type"})
    return row


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
    source_texts, fetch_errs = {}, []
    if entry:
        for u in entry["urls"]:
            text, err = fetch_source_text(u, budget)
            if text:
                source_texts[u] = text
            elif err:
                fetch_errs.append("%s: %s" % (u, err))
    domains = (entry or sources["fallback"])["domains"]

    if not source_texts:
        why = ("no source mapping for post" if not entry
               else "; ".join(fetch_errs)[:200])
        for c in factual:
            rows.append({**c, "status": "queued", "confidence": "LOW",
                         "reasoning": "unverifiable: %s" % why})
        return rows

    cand_json = json.dumps([
        {"id": i, "claim_type": c["claim_type"], "location": c["location"],
         "matched_text": c["matched_text"], "context": c["context"]}
        for i, c in enumerate(factual)], ensure_ascii=False)
    excerpts = "\n\n".join("SOURCE %s:\n%s" % (u, relevant_excerpts(t, today))
                           for u, t in source_texts.items())
    prompt = PROMPT.format(today=today.isoformat(),
                           session=current_session(
                               today, rules.get("session_start_month", 4)),
                           title=title, sources=excerpts, candidates=cand_json)

    def run_round(p):
        obj = call_llm(llm_client, p, budget)
        return {d.get("id"): d for d in obj.get("decisions", [])}

    try:
        decisions = run_round(prompt)
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
            ok, err = validate_decision(d, c, source_texts, domains)
            if ok:
                resolved[i] = _finalize(c, d)
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
                ok, err2 = validate_decision(d, c, source_texts, domains)
                resolved[i] = (_finalize(c, d) if ok else
                               {**c, "status": "dropped",
                                "validation_error": err2})
            else:
                resolved[i] = {**c, "status": "dropped",
                               "validation_error": err}
    rows.extend(resolved[i] for i in sorted(resolved))
    return rows


def _finalize(c, d):
    conf = d.get("confidence", "LOW").upper()
    reasoning = d.get("reasoning", "")
    # Policy ceilings enforced in code, not prompts:
    if c["location"] in ("title", "td") and conf == "HIGH":
        conf = "MEDIUM"
        reasoning += " [capped to MEDIUM: %s changes always queue]" % c["location"]
    status = "planned" if conf in ("HIGH", "MEDIUM") else "queued"
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
    if a.limit:
        groups = groups[:a.limit]

    llm_client = None
    if a.apply:
        import anthropic
        llm_client = anthropic.Anthropic()
    ctx = {"rules": load_rules(a.rules), "sources": load_sources(a.sources),
           "today": datetime.date.today(), "llm": llm_client,
           "budget": {"llm": a.max_llm_calls, "fetches": a.max_fetches},
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
