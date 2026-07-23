"""
Offline tests for the v6 planner (wrap scanner, global allocator, scaffold
ban, both-fields support) and executor field routing. No Webflow, no API.

Run: python -m pytest tests/test_v6_planner.py -v
"""
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.plan_insertions import (
    extract_paragraph_records, find_wrap, phrase_candidates, allocate,
    scaffold_introduced, validate_bridge_decision, compute_candidates,
    edge_allowed, Allocator, PER_POST_MAX, PER_POST_T0_MAX, build_idf,
)
from scripts.insert_planned_links import process_source


# ---------- Stage A: both fields extracted ----------

def _bodies():
    first = ("<p>In this blog you'll learn everything about loans.</p>"
             "<p>The CAT exam pattern changed in 2025 and percentile math matters.</p>"
             "<p>Banks evaluate collateral before sanctioning amounts.</p>")
    second = ("<p>Repayment moratorium periods vary widely by lender today.</p>"
              "<p>Apply early to avoid last-minute processing delays.</p>")
    return {"post-body": first, "post-body-2nd-half": second}


def test_paragraphs_come_from_both_fields():
    recs = extract_paragraph_records(_bodies())
    fields = {r["field"] for r in recs}
    assert fields == {"post-body", "post-body-2nd-half"}          # BUG-2 fixed
    # labels continuous, pidx per-field
    second = [r for r in recs if r["field"] == "post-body-2nd-half"]
    assert second[0]["pidx"] == 0


def test_meta_first_last_marked_ineligible():
    recs = extract_paragraph_records(_bodies())
    assert recs[0]["eligible"] is False           # meta ("in this blog") + intro
    assert recs[-1]["eligible"] is False          # outro
    assert all(r["eligible"] for r in recs[1:-1])


# ---------- Stage B: wrap scanner ----------

def test_wrap_scan_finds_phrase_and_builds_valid_row():
    bodies = _bodies()
    soups = {f: BeautifulSoup(h, "html.parser") for f, h in bodies.items()}
    recs = extract_paragraph_records(bodies)
    row = find_wrap(soups, recs, "/site/blog/cat-exam-pattern",
                    ["CAT exam pattern"])
    assert row is not None
    assert row["insertion_type"] == "wrap"
    assert row["anchor"] == "CAT exam pattern"
    assert "[CAT exam pattern](/site/blog/cat-exam-pattern)" in row["new_sentence"]
    # original sentence must exist verbatim in that paragraph's text node
    p = soups[row["field"]].find_all("p")[row["paragraph_idx"]]
    assert row["original_sentence"] in p.get_text(separator=" ")


def test_wrap_scan_reaches_second_half():
    bodies = _bodies()
    soups = {f: BeautifulSoup(h, "html.parser") for f, h in bodies.items()}
    recs = extract_paragraph_records(bodies)
    row = find_wrap(soups, recs, "/site/blog/moratorium", ["repayment moratorium periods"])
    assert row is not None and row["field"] == "post-body-2nd-half"


def test_wrap_scan_skips_ineligible_and_linked_text():
    bodies = {"post-body":
              "<p>Intro paragraph mentions CAT exam pattern here already ok.</p>"
              "<p>Existing <a href='/x'>CAT exam pattern</a> link is here now.</p>"
              "<p>Final outro paragraph mentions CAT exam pattern once more.</p>"}
    soups = {f: BeautifulSoup(h, "html.parser") for f, h in bodies.items()}
    recs = extract_paragraph_records(bodies)
    row = find_wrap(soups, recs, "/site/blog/cat", ["CAT exam pattern"])
    assert row is None   # P0=intro, P1 text inside <a>, P2=outro


def test_phrase_candidates_filters_lengths():
    phs = phrase_candidates("/site/blog/x", "A Complete Guide To Education Loan Interest Rates In India For Students 2026", {})
    for ph in phs:
        assert 2 <= len(ph.split()) <= 8


# ---------- Stage C: allocator ----------

def _edge(src, tgt, tier="T2", rel=1.0, field="post-body", pidx=1, anchor="anchor text"):
    return {"source_url": src, "target_url": tgt, "target_tier": tier,
            "relevance": rel, "field": field, "paragraph_idx": pidx,
            "anchor": anchor, "original_sentence": "s", "new_sentence": "n",
            "insertion_type": "wrap", "reasoning": ""}

GRAMMAR = {"tier_inbound_targets": {"T2": {"target_max": 2}, "T0": {"target_max": 999999}}}
T0 = ["/site/education-loan", "/site/lp/study-loan-eligibility"]


def test_per_post_max_respected():
    wraps = [_edge("/site/blog/s1", f"/site/blog/t{i}", pidx=i) for i in range(9)]
    grammar = {"tier_inbound_targets": {"T2": {"target_max": 100}}}
    w, b, alloc = allocate(wraps, [], grammar, T0)
    assert len(w) == PER_POST_MAX


def test_t0_cap_two_per_post():
    wraps = [_edge("/site/blog/s1", "/site/education-loan", tier="T0", pidx=1, anchor="a1"),
             _edge("/site/blog/s1", "/site/lp/study-loan-eligibility", tier="T0", pidx=2, anchor="a2"),
             _edge("/site/blog/s1", "/site/education-loan-emi-calculator", tier="T0", pidx=3, anchor="a3")]
    grammar = {"tier_inbound_targets": {"T0": {"target_max": 999999}}}
    t0 = T0 + ["/site/education-loan-emi-calculator"]
    w, b, alloc = allocate(wraps, [], grammar, t0)
    assert len(w) == PER_POST_T0_MAX


def test_target_inbound_budget_respected():
    wraps = [_edge(f"/site/blog/s{i}", "/site/blog/hot-target", pidx=1) for i in range(6)]
    w, b, alloc = allocate(wraps, [], GRAMMAR, T0)
    assert len(w) == 2       # T2 budget capped at 2 in GRAMMAR


def test_one_insertion_per_paragraph():
    wraps = [_edge("/site/blog/s1", "/site/blog/t1", pidx=1),
             _edge("/site/blog/s1", "/site/blog/t2", pidx=1)]
    grammar = {"tier_inbound_targets": {"T2": {"target_max": 100}}}
    w, b, alloc = allocate(wraps, [], grammar, T0)
    assert len(w) == 1


def test_anchor_repetition_cap():
    grammar = {"tier_inbound_targets": {"T2": {"target_max": 10000}}}
    wraps = [_edge(f"/site/blog/s{i}", "/site/blog/t", pidx=1, anchor="same anchor")
             for i in range(30)]
    w, b, alloc = allocate(wraps, [], grammar, T0)
    from scripts.plan_insertions import ANCHOR_REPEAT_CAP
    assert len(w) == min(ANCHOR_REPEAT_CAP, 10000, 30) == ANCHOR_REPEAT_CAP


def test_edge_legality():
    assert edge_allowed("T3", "T0", "Exams & Counselling", "Money Page")
    assert edge_allowed("T2", "T2", "Education Loans", "Education Loans")
    assert edge_allowed("T3", "T1", "Study Abroad", "Education Loans")     # bridge
    assert not edge_allowed("T3", "T1", "Study Abroad", "Courses & Careers")


# ---------- Stage D: scaffold ban ----------

def test_scaffold_introduced_detects_new_phrase():
    orig = "The CAT exam pattern changed in 2025."
    bad = "The CAT exam pattern changed in 2025, similar to [XAT pattern](/site/blog/xat)."
    assert scaffold_introduced(orig, bad) == "similar to"


def test_scaffold_in_original_prose_is_allowed():
    orig = "Much like last year, the CAT exam pattern changed."
    new = "Much like last year, the [CAT exam pattern](/site/blog/cat) changed."
    assert scaffold_introduced(orig, new) is None


def test_validate_bridge_decision_end_to_end():
    recs = extract_paragraph_records(_bodies())
    para_by_label = {r["label"]: r for r in recs}
    alloc = Allocator({}, T0)
    d = {"action": "insert", "target_url": "/site/blog/collateral-guide",
         "paragraph_label": 2,
         "original_sentence": "Banks evaluate collateral before sanctioning amounts.",
         "new_sentence": "Banks evaluate [collateral requirements](/site/blog/collateral-guide) before sanctioning amounts.",
         "anchor": "collateral requirements"}
    ok, why, d = validate_bridge_decision(d, para_by_label,
                                          {"/site/blog/collateral-guide"}, alloc,
                                          "/site/blog/src")
    assert ok, why
    assert d["_field"] == "post-body" and d["_pidx"] == 2


def test_validate_rejects_scaffold_and_wrong_target():
    recs = extract_paragraph_records(_bodies())
    para_by_label = {r["label"]: r for r in recs}
    alloc = Allocator({}, T0)
    d = {"action": "insert", "target_url": "/site/blog/collateral-guide",
         "paragraph_label": 2,
         "original_sentence": "Banks evaluate collateral before sanctioning amounts.",
         "new_sentence": "Banks evaluate collateral, similar to [collateral rules](/site/blog/collateral-guide), before sanctioning amounts.",
         "anchor": "collateral rules"}
    ok, why, _ = validate_bridge_decision(d, para_by_label,
                                          {"/site/blog/collateral-guide"}, alloc,
                                          "/site/blog/src")
    assert not ok and "forbidden connector" in why
    d2 = dict(d, target_url="/site/blog/not-allocated",
              new_sentence="Banks evaluate [collateral](/site/blog/not-allocated) before sanctioning amounts.")
    ok, why, _ = validate_bridge_decision(d2, para_by_label,
                                          {"/site/blog/collateral-guide"}, alloc,
                                          "/site/blog/src")
    assert not ok


# ---------- candidates: no flat-0.5 T0 dominance, tier weighting live ----------

def test_t0_relevance_not_flat_baseline():
    tier_map = {
        "/site/blog/mba-loan-guide": {"tier": "T1", "category": "Education Loans",
                                       "title": "MBA education loan guide"},
        "/site/blog/neet-cutoff": {"tier": "T3", "category": "Exams & Counselling",
                                    "title": "NEET cutoff analysis"},
    }
    idf = build_idf(tier_map)
    cands = compute_candidates("/site/blog/mba-fees",
                               {"title": "MBA fees and education loan options",
                                "category": "Education Loans"},
                               tier_map, ["/site/education-loan", "/site/neet-college-predictor"],
                               idf, set())
    scores = {c["target_url"]: c["relevance"] for c in cands}
    # education-loan overlaps the source tokens; neet predictor doesn't →
    # they must NOT share a flat baseline score
    assert scores["/site/education-loan"] != scores["/site/neet-college-predictor"]


# ---------- executor: field routing ----------

def _item(bodies):
    return {"fieldData": {"slug": "test-post", **bodies}}


def test_executor_inserts_into_second_half():
    bodies = _bodies()
    plans = pd.DataFrame([{
        "action": "insert", "field": "post-body-2nd-half", "paragraph_idx": 0,
        "target_url": "/site/blog/moratorium",
        "original_sentence": "Repayment moratorium periods vary widely by lender today.",
        "new_sentence": "[Repayment moratorium periods](/site/blog/moratorium) vary widely by lender today.",
        "validation_error": "",
    }])
    log, patch = process_source(_item(bodies), plans, dry_run=False)
    assert log["applied"] == 1, log
    assert patch and "post-body-2nd-half" in patch
    assert "/site/blog/moratorium" in patch["post-body-2nd-half"]
    assert "post-body" not in patch          # first half untouched


def test_executor_v5_plans_without_field_still_work():
    bodies = _bodies()
    plans = pd.DataFrame([{
        "action": "insert", "paragraph_idx": 2,
        "target_url": "/site/blog/collateral-guide",
        "original_sentence": "Banks evaluate collateral before sanctioning amounts.",
        "new_sentence": "Banks evaluate [collateral](/site/blog/collateral-guide) before sanctioning amounts.",
        "validation_error": "",
    }])
    log, patch = process_source(_item(bodies), plans, dry_run=False)
    assert log["applied"] == 1, log
    assert "/site/blog/collateral-guide" in patch["post-body"]


def test_executor_t0_cap_at_apply_time():
    bodies = {"post-body":
              '<p>Intro paragraph text goes here fine.</p>'
              '<p>Alpha sentence one is here. <a href="/site/education-loan">loan</a></p>'
              '<p>Beta paragraph: <a href="/site/college-loan">college</a> link.</p>'
              '<p>Gamma sentence about the MBA loan process today.</p>'
              '<p>Outro paragraph text goes here fine.</p>'}
    plans = pd.DataFrame([{
        "action": "insert", "field": "post-body", "paragraph_idx": 3,
        "target_url": "/site/mba-education-loan",
        "original_sentence": "Gamma sentence about the MBA loan process today.",
        "new_sentence": "Gamma sentence about the [MBA loan process](/site/mba-education-loan) today.",
        "validation_error": "",
    }])
    log, patch = process_source(_item(bodies), plans, dry_run=False)
    # post already carries 2 T0 links → third must be refused
    assert log["applied"] == 0
    assert any("t0-cta-cap" in e for e in [log.get("errors_notes", "")])
