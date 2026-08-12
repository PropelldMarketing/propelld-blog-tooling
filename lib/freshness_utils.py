"""
Freshness utilities: staleness claim detection, lane triage, and safe
text-node-level substitution.

Design constraints inherited from the internal-linking project (do not relax):
- NEVER rebuild HTML from text. All edits splice a single text node.
- Table cells: edited cell-in-place at the text-node level, never block-wise.
- Verbatim matching is whitespace-normalized (collapse runs to single spaces).
- Every changed fact token must be provable: Lane A by whitelisted rule,
  Lane B by quoted evidence from an official source.
- Pure substitution only: token-level diff may contain REPLACEMENTS of
  fact-shaped tokens, nothing else. No rewording ever auto-applies.
"""

import re
import json
import difflib
import datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------- patterns

MONTHS = ("january february march april may june july august september "
          "october november december").split()
MONTHS_ABBR = [m[:3] for m in MONTHS]
MONTH_RE = r"(?:%s)" % "|".join([m.capitalize() for m in MONTHS] +
                                [m.capitalize() + r"\.?" for m in MONTHS_ABBR])

YEAR_RE = re.compile(r"\b(19[89]\d|20\d{2})\b")
# "2024-25", "2024–25", "2024-2025" session ranges
SESSION_RE = re.compile(r"\b(20\d{2})\s*[-–—]\s*((?:20)?\d{2})\b")
# "15 March 2025", "March 15, 2025", "15th Mar 2025"
DATE_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+%s,?\s+20\d{2}|%s\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2})\b"
    % (MONTH_RE, MONTH_RE))
FEE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s?[\d,]+(?:\.\d+)?", re.I)
REG_PHRASE_RE = re.compile(
    r"\b(registrations?|applications?)\s+(?:is|are|will\s+be|now)?\s*"
    r"(open|closed|ongoing|live|started|closing)\b", re.I)
NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]20\d{2}\b")

CLAIM_PATTERNS = [
    ("date", DATE_RE),
    ("numeric_date", NUMERIC_DATE_RE),
    ("session_range", SESSION_RE),
    ("fee", FEE_RE),
    ("registration_phrase", REG_PHRASE_RE),
    ("year", YEAR_RE),          # last: broadest
]

HISTORICAL_MARKERS = re.compile(
    r"\b(in|back in|since|until|till|was|were|had|previous(?:ly)?|earlier|"
    r"compared (?:to|with)|history|introduced|launched|changed|before)\b", re.I)

# Tokens that a pure substitution is allowed to change.
FACT_TOKEN_RE = re.compile(
    r"^\W*(?:"
    r"(?:19|20)\d{2}(?:[-–](?:20)?\d{2})?"          # year / session range
    r"|\d{1,2}(?:st|nd|rd|th)?"                          # day number
    r"|%s"                                               # month names
    r"|(?:₹|Rs\.?|INR)?[\d,]+(?:\.\d+)?"                 # amounts
    r"|open|closed|ongoing|live|started|closing|begun|ended"
    r"|\d{1,2}[/.]\d{1,2}[/.]20\d{2}"
    r")\W*$" % MONTH_RE, re.I)

CONTEXT_CHARS = 140


# ---------------------------------------------------------------- rules file

def load_rules(path="data/freshness_rules.json"):
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"lane_a_rules": [], "session_start_month": 4, "date_bump_field": ""}


def current_session(today=None, session_start_month=4):
    """Indian academic session label for `today`, e.g. '2026-27'."""
    today = today or datetime.date.today()
    y = today.year if today.month >= session_start_month else today.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


# ---------------------------------------------------------------- detection

def _location_of(node):
    """Structural location for a text node: 'td' wins, then nearest block."""
    if node.find_parent(["td", "th"]) is not None:
        return "td"
    if node.find_parent("a") is not None:
        return "a"
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li", "p"):
        if node.find_parent(tag) is not None:
            return tag
    return "other"


def extract_candidates(html, field):
    """Scan one body field's HTML for staleness candidates.

    Returns rows: {field, location, claim_type, matched_text, context}.
    Overlapping matches are deduplicated: the first (most specific)
    claim_type wins for a given span.
    """
    out = []
    if not html or not html.strip():
        return out
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=True):
        text = str(node)
        if not text.strip():
            continue
        loc = _location_of(node)
        taken = []
        for ctype, rx in CLAIM_PATTERNS:
            for m in rx.finditer(text):
                span = (m.start(), m.end())
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue
                taken.append(span)
                lo = max(0, m.start() - CONTEXT_CHARS)
                hi = min(len(text), m.end() + CONTEXT_CHARS)
                out.append({
                    "field": field,
                    "location": loc,
                    "claim_type": ctype,
                    "matched_text": m.group(0),
                    "context": re.sub(r"\s+", " ", text[lo:hi]).strip(),
                })
    return out


def scan_title(title):
    """Scan a plain-text title (no HTML)."""
    out = []
    for ctype, rx in CLAIM_PATTERNS:
        for m in rx.finditer(title or ""):
            out.append({
                "field": "name",
                "location": "title",
                "claim_type": ctype,
                "matched_text": m.group(0),
                "context": title,
            })
    # dedupe overlaps: keep first claim_type per matched span text
    seen, deduped = set(), []
    for r in out:
        if r["matched_text"] in seen:
            continue
        seen.add(r["matched_text"])
        deduped.append(r)
    return deduped


# ---------------------------------------------------------------- triage

def _years_in(s):
    return [int(y) for y in YEAR_RE.findall(s or "")]


def classify_lane(cand, rules, today=None):
    """Deterministic triage. Returns (lane, reason).

    Lanes: 'A' mechanical auto (whitelisted rule), 'B' factual queue,
    'LLM' needs model judgment, 'IGNORE' with reason.
    Bias: when unsure, LLM (which itself biases to queue). Never A unless
    a whitelisted rule matches exactly.
    """
    today = today or datetime.date.today()
    ctx = cand["context"]
    years = _years_in(cand["matched_text"])

    # Anything inside an anchor: queue-only, never auto (link semantics).
    if cand["location"] == "a":
        return "B", "inside-anchor: queue-only"

    # Future or current references are not stale.
    if cand["claim_type"] == "year" and years and max(years) >= today.year:
        return "IGNORE", "year is current or future"
    if cand["claim_type"] == "session_range":
        m = SESSION_RE.search(cand["matched_text"])
        start = int(m.group(1))
        cur = current_session(today, rules.get("session_start_month", 4))
        if start >= int(cur[:4]):
            return "IGNORE", "session is current or future"

    # Whitelisted Lane A rules (each rule: id, claim_type, context_regex,
    # location list). Reasoning is recorded per-rule.
    for rule in rules.get("lane_a_rules", []):
        if rule.get("claim_type") != cand["claim_type"]:
            continue
        if cand["location"] not in rule.get("locations", []):
            continue
        if re.search(rule["context_regex"], ctx, re.I):
            return "A", "lane-a-rule:%s (%s)" % (rule["id"], rule.get("why", ""))

    # Clear historical framing on bare years -> ignore.
    if cand["claim_type"] == "year" and years:
        window = ctx
        if max(years) <= today.year - 2 and HISTORICAL_MARKERS.search(window):
            return "IGNORE", "historical reference (marker word near old year)"
        return "LLM", "old year, framing unclear"

    # Dates, fees, registration phrases, sessions: factual -> B (evidence).
    if cand["claim_type"] in ("date", "numeric_date", "fee",
                              "registration_phrase", "session_range"):
        past = years and max(years) < today.year
        return "B", ("stale-shaped factual claim" if past
                     else "factual claim, verify against source")
    return "LLM", "unclassified"


# ---------------------------------------------------------------- validators

def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def pure_substitution_check(old_text, new_text):
    """The only allowed edit is replacing fact-shaped tokens.

    Token-level opcodes must be 'equal' or 'replace' where BOTH sides of
    every replaced token are fact-shaped. No inserts, no deletes, no
    rewording. Returns (ok, reason).
    """
    ot, nt = norm_ws(old_text).split(), norm_ws(new_text).split()
    if not ot or not nt:
        return False, "empty old/new text"
    if ot == nt:
        return False, "no change"
    sm = difflib.SequenceMatcher(a=ot, b=nt, autojunk=False)
    changed = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op != "replace" or (i2 - i1) != (j2 - j1):
            return False, "not a pure substitution (insert/delete/reshape)"
        for tok in ot[i1:i2] + nt[j1:j2]:
            if not FACT_TOKEN_RE.match(tok):
                return False, "non-fact token changed: %r" % tok
            changed += 1
    if changed == 0:
        return False, "no fact tokens changed"
    return True, "ok"


def changed_new_tokens(old_text, new_text):
    """The new-side tokens that differ from old (the asserted facts)."""
    ot, nt = norm_ws(old_text).split(), norm_ws(new_text).split()
    sm = difflib.SequenceMatcher(a=ot, b=nt, autojunk=False)
    toks = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            toks.extend(nt[j1:j2])
    return toks


def _norm_fact(tok):
    t = re.sub(r"[^\w₹]", "", tok).lower()
    # Currency markers vary between page and source (₹ / Rs. / INR):
    # compare the numeric part.
    return re.sub(r"^(?:₹|rs|inr)", "", t)


def evidence_supports(old_text, new_text, evidence_quote):
    """Every changed fact token in new_text must appear in the evidence.

    Numbers must match exactly (commas stripped); words case-insensitive.
    Returns (ok, missing_tokens).
    """
    ev = " " + re.sub(r"[^\w₹]+", " ", evidence_quote or "").lower() + " "
    ev_compact = ev.replace(" ", "")
    missing = []
    for tok in changed_new_tokens(old_text, new_text):
        t = _norm_fact(tok)
        if not t:
            continue
        if (" %s " % t) in ev or t in ev_compact:
            continue
        missing.append(tok)
    return (len(missing) == 0), missing


# ---------------------------------------------------------------- surgery

def _norm_with_map(s):
    """Collapse whitespace runs while mapping normalized idx -> raw idx."""
    norm, idx_map, prev_space = [], [], False
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


def apply_substitution(soup, old_text, new_text):
    """Replace old_text with new_text inside a SINGLE text node.

    Plain-text splice only (new_text is never parsed as HTML). Works
    identically inside <td><p> because the edit is at the text-node level:
    cell-in-place, never block-wise. Never touches text inside <a>.

    Returns (ok, reason). Fails safe if the text spans inline markup.
    """
    old_norm = norm_ws(old_text)
    if not old_norm:
        return False, "empty old_text"
    hits, anchor_hits, total = [], 0, 0
    for node in soup.find_all(string=True):
        norm, idx_map = _norm_with_map(str(node))
        if node.find_parent("a") is not None:
            if old_norm in norm:
                anchor_hits += 1
            continue
        # Count EVERY occurrence, including repeats within one node.
        pos = norm.find(old_norm)
        while pos != -1:
            total += 1
            hits.append((node, norm, idx_map, pos))
            pos = norm.find(old_norm, pos + 1)
    if not hits:
        if anchor_hits:
            return False, "old_text only inside anchor text (never edited)"
        full = norm_ws(soup.get_text(separator=" "))
        if old_norm in full:
            return False, "old_text spans inline markup (skipped, fail-safe)"
        return False, "old_text not found"
    if total > 1 or anchor_hits:
        return False, ("old_text ambiguous (%d occurrences); needs wider context"
                       % (total + anchor_hits))
    node, norm, idx_map, pos = hits[0]
    raw = str(node)
    raw_start = idx_map[pos]
    raw_end = idx_map[pos + len(old_norm) - 1] + 1
    node.replace_with(NavigableString(raw[:raw_start] + new_text + raw[raw_end:]))
    return True, "ok"


def substitute_in_html(html, old_text, new_text):
    """Parse -> substitute -> serialize. Returns (new_html_or_None, reason)."""
    soup = BeautifulSoup(html, "html.parser")
    ok, reason = apply_substitution(soup, old_text, new_text)
    return (str(soup), reason) if ok else (None, reason)
