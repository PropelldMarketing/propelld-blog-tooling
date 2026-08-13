"""
Offline tests for the freshness machinery. No network, no API tokens.

Run:  python -m pytest tests/test_freshness.py -q
  or: python tests/test_freshness.py
"""
import sys
import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bs4 import BeautifulSoup

from lib.freshness_utils import (
    extract_candidates, scan_title, classify_lane, load_rules,
    current_session, pure_substitution_check, evidence_supports,
    apply_substitution, substitute_in_html, norm_ws, changed_new_tokens)

TODAY = datetime.date(2026, 8, 12)
RULES = load_rules(str(REPO / "data" / "freshness_rules.json"))


# ---------------------------------------------------------------- detection

def test_detects_stale_year_and_context():
    html = "<p>JEE Main 2024 registration will begin soon. Apply early.</p>"
    cands = extract_candidates(html, "post-body")
    assert any(c["claim_type"] == "year" and c["matched_text"] == "2024"
               for c in cands)
    assert all(c["location"] == "p" for c in cands)


def test_detects_dates_fees_sessions_phrases():
    html = ("<p>The exam is on 15 March 2025. Fee: ₹1,000. "
            "Registration is open for the 2024-25 session.</p>")
    types = {c["claim_type"] for c in extract_candidates(html, "post-body")}
    assert {"date", "fee", "registration_phrase", "session_range"} <= types


def test_table_cell_location_flagged_td():
    html = "<table><tr><td><p>CAT 2024</p></td></tr></table>"
    cands = extract_candidates(html, "post-body")
    assert cands and all(c["location"] == "td" for c in cands)


def test_anchor_text_flagged():
    html = '<p>See <a href="/x">CAT 2024 dates</a> here.</p>'
    cands = extract_candidates(html, "post-body")
    assert cands and all(c["location"] == "a" for c in cands)


def test_title_scan():
    cands = scan_title("CAT 2025 Exam Date and Registration")
    assert cands and cands[0]["location"] == "title"


# ---------------------------------------------------------------- triage

def _cand(claim_type, matched, context, location="p"):
    return {"claim_type": claim_type, "matched_text": matched,
            "context": context, "location": location, "field": "post-body"}


def test_current_and_future_years_ignored():
    lane, _ = classify_lane(_cand("year", "2026", "the 2026 exam"), RULES, TODAY)
    assert lane == "IGNORE"
    lane, _ = classify_lane(_cand("year", "2027", "the 2027 intake"), RULES, TODAY)
    assert lane == "IGNORE"


def test_historical_reference_ignored():
    lane, reason = classify_lane(
        _cand("year", "2023", "In 2023, the exam pattern changed significantly"),
        RULES, TODAY)
    assert lane == "IGNORE" and "historical" in reason


def test_ambiguous_old_year_goes_to_llm():
    lane, _ = classify_lane(_cand("year", "2024", "CAT 2024 syllabus overview"),
                            RULES, TODAY)
    assert lane == "LLM"


def test_factual_claims_go_to_lane_b():
    # In-window date (July 2026 vs test-today 2026-08-12) and phrases -> B.
    for ct, txt in [("date", "20 July 2026"),
                    ("registration_phrase", "registration is open")]:
        lane, _ = classify_lane(_cand(ct, txt, "x %s y" % txt), RULES, TODAY)
        assert lane == "B", ct


def test_future_date_ignored():
    lane, reason = classify_lane(
        _cand("date", "15 March 2027", "the exam is on 15 March 2027"),
        RULES, TODAY)
    assert lane == "IGNORE" and "future" in reason


def test_fee_needs_staleness_signal():
    # Bare fee, no year anywhere: ignore (would only spam the queue).
    lane, reason = classify_lane(
        _cand("fee", "₹2,50,000", "the total fee is ₹2,50,000 per year"),
        RULES, TODAY)
    assert lane == "IGNORE" and "staleness signal" in reason
    # Fee near current/future year: ignore.
    lane, reason = classify_lane(
        _cand("fee", "₹2,50,000", "fees 2026: the total is ₹2,50,000"),
        RULES, TODAY)
    assert lane == "IGNORE"
    # Fee near an old year: verify.
    lane, _ = classify_lane(
        _cand("fee", "₹2,50,000", "fees for 2024: the total is ₹2,50,000"),
        RULES, TODAY)
    assert lane == "B"


def test_fee_regex_requires_digit():
    from lib.freshness_utils import FEE_RE
    assert FEE_RE.search("pay Rs, later") is None
    assert FEE_RE.search("pay Rs. 1,200 now").group(0) == "Rs. 1,200"
    assert FEE_RE.search("₹4.9 lakh").group(0) == "₹4.9"


def test_anchor_never_auto():
    lane, reason = classify_lane(
        _cand("session_range", "2024-25",
              "for the current academic session 2024-25", location="a"),
        RULES, TODAY)
    assert lane == "B" and "anchor" in reason


def test_lane_a_session_rollover_whitelist():
    lane, reason = classify_lane(
        _cand("session_range", "2024-25",
              "fees for the current academic session 2024-25 are listed"),
        RULES, TODAY)
    assert lane == "A" and "session-rollover-current" in reason


def test_current_session_arithmetic():
    assert current_session(datetime.date(2026, 8, 12), 4) == "2026-27"
    assert current_session(datetime.date(2026, 2, 1), 4) == "2025-26"


# ---------------------------------------------------------------- validators

def test_pure_substitution_accepts_fact_changes():
    ok, _ = pure_substitution_check("the exam is on 15 March 2025",
                                    "the exam is on 22 March 2026")
    assert ok
    ok, _ = pure_substitution_check("fee is ₹1,000 for general",
                                    "fee is ₹1,200 for general")
    assert ok
    ok, _ = pure_substitution_check("registration is open", "registration is closed")
    assert ok


def test_pure_substitution_rejects_rewording():
    ok, why = pure_substitution_check("the exam is on 15 March 2025",
                                      "the exam will likely be on 22 March 2026")
    assert not ok
    ok, why = pure_substitution_check("the fee is ₹1,000",
                                      "the fee is ₹1,000 approximately")
    assert not ok and "pure" in why.lower() or "insert" in why.lower()


def test_pure_substitution_rejects_noop_and_empty():
    assert not pure_substitution_check("same text", "same text")[0]
    assert not pure_substitution_check("", "x")[0]


def test_evidence_supports_requires_changed_tokens():
    ok, missing = evidence_supports(
        "exam on 15 March 2025", "exam on 22 March 2026",
        "The examination will be held on 22 March 2026 across all centres.")
    assert ok, missing
    ok, missing = evidence_supports(
        "exam on 15 March 2025", "exam on 22 March 2026",
        "Registration opens in January.")
    assert not ok and missing


def test_evidence_number_normalization():
    ok, _ = evidence_supports("fee ₹1,000", "fee ₹1,200",
                              "The application fee is Rs. 1,200 for general.")
    assert ok


# ---------------------------------------------------------------- surgery

def test_substitution_preserves_inline_markup():
    html = ('<p>The <strong>JEE Main</strong> exam is on 15 March 2025 and '
            '<a href="/apply">applications</a> are open.</p>')
    new, reason = substitute_in_html(html, "15 March 2025", "22 March 2026")
    assert new and "22 March 2026" in new
    assert "<strong>JEE Main</strong>" in new and 'href="/apply"' in new


def test_substitution_never_touches_anchor_text():
    html = '<p>See <a href="/x">CAT 2024 dates</a> now.</p>'
    new, reason = substitute_in_html(html, "CAT 2024", "CAT 2026")
    assert new is None and "anchor" in reason


def test_substitution_table_cell_in_place():
    html = ("<table><tr><td><p>Exam date</p></td>"
            "<td><p>15 March 2025</p></td></tr></table>")
    new, reason = substitute_in_html(html, "15 March 2025", "22 March 2026")
    assert new and "22 March 2026" in new
    soup = BeautifulSoup(new, "html.parser")
    assert len(soup.find_all("td")) == 2          # no cell eaten
    assert soup.find_all("td")[0].get_text() == "Exam date"


def test_substitution_fails_safe_across_inline_tags():
    html = "<p>The exam is on <em>15 March</em> 2025 this year.</p>"
    new, reason = substitute_in_html(html, "15 March 2025", "22 March 2026")
    assert new is None and "spans inline markup" in reason


def test_substitution_ambiguous_refused():
    html = "<p>2024-25 fees. Also 2024-25 hostel fees.</p>"
    new, reason = substitute_in_html(html, "2024-25", "2026-27")
    assert new is None and "ambiguous" in reason


def test_substitution_whitespace_normalized_match():
    html = "<p>The exam is on 15  March\n 2025 in centres.</p>"
    new, reason = substitute_in_html(html, "15 March 2025", "22 March 2026")
    assert new and "22 March 2026" in new


def test_changed_new_tokens():
    assert changed_new_tokens("a 2024 b", "a 2026 b") == ["2026"]


# ---------------------------------------------------------------- plan/apply glue

def test_should_apply_policy():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_apply import should_apply
    row = {"status": "planned", "lane": "B", "confidence": "HIGH"}
    assert should_apply(row, auto_high=True, queue_all=False)[0]
    assert not should_apply(row, auto_high=False, queue_all=False)[0]
    assert not should_apply(row, auto_high=True, queue_all=True)[0]
    assert not should_apply({**row, "approved": "NO"}, True, False)[0]
    assert should_apply({**row, "confidence": "MEDIUM", "approved": "YES"},
                        True, False)[0]
    assert should_apply({"status": "planned", "lane": "A", "confidence": "RULE"},
                        True, False)[0]
    assert not should_apply({"status": "queued", "lane": "B",
                             "confidence": "HIGH"}, True, False)[0]


def test_finalize_policy_ceilings():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import _finalize
    d = {"old_text": "CAT 2024", "new_text": "CAT 2026", "confidence": "HIGH",
         "reasoning": "r", "evidence_quote": "e", "source_url": "s"}
    base = {"slug": "x", "field": "post-body"}
    # Body prose keeps HIGH -> planned (auto-apply eligible).
    r = _finalize({**base, "location": "p"}, dict(d), RULES)
    assert r["confidence"] == "HIGH" and r["status"] == "planned"
    # Titles, table cells, headings: capped to MEDIUM -> needs approval.
    for loc in ("title", "td", "h2", "h3"):
        r = _finalize({**base, "location": loc}, dict(d), RULES)
        assert r["confidence"] == "MEDIUM" and r["status"] == "planned", loc
    # Anchor text: always queued, never applied.
    r = _finalize({**base, "location": "a"}, dict(d), RULES)
    assert r["status"] == "queued" and "anchor" in r["reasoning"]


def test_finalize_autonomy_promotion_via_config():
    """Promoting a location to auto-apply is a config edit, no code change."""
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import _finalize
    d = {"old_text": "CAT 2024", "new_text": "CAT 2026", "confidence": "HIGH",
         "reasoning": "r", "evidence_quote": "e", "source_url": "s"}
    base = {"slug": "x", "field": "post-body", "location": "h2"}
    promoted = {"autonomy": {"auto_locations": ["p", "li", "h2"],
                             "never_locations": ["a"]}}
    r = _finalize(base, dict(d), promoted)
    assert r["confidence"] == "HIGH" and r["status"] == "planned"
    # And demoting everything (empty auto list) queues even prose HIGHs.
    locked = {"autonomy": {"auto_locations": [], "never_locations": ["a"]}}
    r = _finalize({**base, "location": "p"}, dict(d), locked)
    assert r["confidence"] == "MEDIUM"


def test_plan_validators_reject_unaudited_source():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import validate_decision
    cand = {"context": "exam on 15 March 2025 in centres"}
    dec = {"old_text": "exam on 15 March 2025",
           "new_text": "exam on 22 March 2026",
           "evidence_quote": "held on 22 March 2026",
           "source_url": "https://some-coaching-blog.com/x"}
    # The cited URL was fetched but is NOT in the audited tiers map.
    ok, err = validate_decision(dec, cand,
                                {"https://some-coaching-blog.com/x":
                                 "held on 22 march 2026"},
                                {})
    assert not ok and "audited" in err


def test_plan_validators_accept_good_decision():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import validate_decision
    cand = {"context": "The exam is on 15 March 2025 in all centres."}
    src = "https://jeemain.nta.nic.in/"
    dec = {"old_text": "exam is on 15 March 2025",
           "new_text": "exam is on 22 March 2026",
           "evidence_quote": "The examination will be held on 22 March 2026.",
           "source_url": src}
    ok, err = validate_decision(
        dec, cand, {src: "notice: The examination will be held on 22 March 2026."},
        {src: "official"})
    assert ok, err


def test_tier_of_audit_gate():
    sys.path.insert(0, str(REPO / "scripts"))
    import json
    from freshness_plan import tier_of
    src_cfg = json.load(open(REPO / "data" / "freshness_sources.json"))
    assert tier_of("https://jeemain.nta.nic.in/x", src_cfg) == "official"
    assert tier_of("https://mcc.nic.in/ug", src_cfg) == "official"
    assert tier_of("https://www.careers360.com/exams/x", src_cfg) == "secondary"
    assert tier_of("https://engineering.careers360.com/x", src_cfg) == "secondary"
    assert tier_of("https://random-coaching-blog.com/x", src_cfg) is None


def test_exam_key_matches_plurals_and_aliases():
    sys.path.insert(0, str(REPO / "scripts"))
    import json
    from freshness_plan import exam_key_for
    src_cfg = json.load(open(REPO / "data" / "freshness_sources.json"))
    assert exam_key_for("100 Marks in JEE Mains Percentile",
                        "100-marks-in-jee-mains-percentile", src_cfg) == "jee main"
    assert exam_key_for("", "ap-pgcet-exam", src_cfg) == "ap pgcet"
    assert exam_key_for("", "ctet-exam", src_cfg) == "ctet"
    assert exam_key_for("NEET-UG counselling", "x", src_cfg) == "neet"


def test_corroboration_count():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import corroboration_count
    dec = {"old_text": "fee is ₹1,000", "new_text": "fee is ₹1,200"}
    texts = {"https://a.careers360.com/x": "the fee is Rs 1,200 now",
             "https://shiksha.com/y": "application fee: ₹1,200",
             "https://collegedunia.com/z": "fee unchanged at ₹1,000"}
    assert corroboration_count(dec, texts) == 2


def test_relevance_window_ignores_passed_events():
    # Today is 2026-08-12 in tests: April 2026 passed -> ignore;
    # July 2026 (within 45d) -> verify; future -> verify.
    lane, reason = classify_lane(
        _cand("date", "15 April 2026", "the exam was held on 15 April 2026"),
        RULES, TODAY)
    assert lane == "IGNORE" and "relevance window" in reason
    lane, _ = classify_lane(
        _cand("date", "20 July 2026", "counselling starts 20 July 2026"),
        RULES, TODAY)
    assert lane == "B"
    lane, _ = classify_lane(
        _cand("date", "10 December 2026", "the exam is on 10 December 2026"),
        RULES, TODAY)
    assert lane == "B"


def test_claim_priority_window():
    from lib.freshness_utils import claim_priority
    assert claim_priority({"matched_text": "20 July 2026"}, TODAY) == "high"
    assert claim_priority({"matched_text": "25 August 2026"}, TODAY) == "high"
    assert claim_priority({"matched_text": "10 December 2026"}, TODAY) == "normal"
    assert claim_priority({"matched_text": "no date here"}, TODAY) == "normal"


def test_block_id_on_table_candidates():
    html = ("<p>CAT 2024 intro.</p>"
            "<table><tr><td><p>VITEEE 2025</p></td>"
            "<td><p>20 April 2025</p></td></tr></table>")
    cands = extract_candidates(html, "post-body")
    td = [c for c in cands if c["location"] == "td"]
    assert td and all(c["block_id"] == "table-0" for c in td)
    assert all(c["block_id"] == "" for c in cands if c["location"] == "p")


def test_label_year_bump_rule():
    # Prose label with last year's date -> Lane A.
    lane, reason = classify_lane(
        _cand("year", "2025", "Check the NEET 2025 syllabus and exam pattern"),
        RULES, TODAY)
    assert lane == "A" and "label-year-bump" in reason
    # Adjacent real date -> NOT mechanical.
    lane, _ = classify_lane(
        _cand("year", "2025",
              "NEET 2025 syllabus, exam on 15 March 2025 in centres"),
        RULES, TODAY)
    assert lane != "A"
    # Table location -> not mechanical.
    lane, _ = classify_lane(
        _cand("year", "2025", "NEET 2025 syllabus overview", location="td"),
        RULES, TODAY)
    assert lane != "A"
    # Two years back -> not the simple bump.
    lane, _ = classify_lane(
        _cand("year", "2024", "NEET 2024 syllabus overview"), RULES, TODAY)
    assert lane != "A"


def test_lane_a_year_planner():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import plan_lane_a
    cand = {"claim_type": "year", "matched_text": "2025",
            "context": "Check the NEET 2025 syllabus and exam pattern",
            "lane_reason": "label-year-bump", "field": "post-body",
            "location": "p", "slug": "x"}
    row = plan_lane_a(cand, RULES, TODAY)
    assert row["status"] == "planned" and row["confidence"] == "RULE"
    assert "2026" in row["new_text"] and "2025" in row["old_text"]
    assert row["old_text"] != "2025"  # context span, not the bare year


def test_consistency_pass_holds_table_year_bump():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import enforce_consistency
    rows = [
        {"field": "post-body", "block_id": "table-0", "claim_type": "year",
         "status": "planned", "confidence": "MEDIUM", "reasoning": "",
         "matched_text": "2025", "context": "VITEEE 2025"},
        {"field": "post-body", "block_id": "table-0", "claim_type": "date",
         "status": "queued", "confidence": "LOW",
         "reasoning": "unverifiable: x", "matched_text": "20 April 2025",
         "context": "20 April 2025"},
    ]
    enforce_consistency(rows)
    assert rows[0]["status"] == "queued"
    assert "consistency" in rows[0]["reasoning"]
    # And with the date VERIFIED (planned), the year bump stands.
    rows2 = [dict(rows[0], status="planned", reasoning=""),
             dict(rows[1], status="planned", reasoning="")]
    enforce_consistency(rows2)
    assert rows2[0]["status"] == "planned"


def test_consistency_pass_context_coupling_in_prose():
    sys.path.insert(0, str(REPO / "scripts"))
    from freshness_plan import enforce_consistency
    rows = [
        {"field": "post-body", "block_id": "", "claim_type": "year",
         "status": "planned", "confidence": "HIGH", "reasoning": "",
         "matched_text": "2025",
         "context": "JEE 2025 exam, last date 10 April 2025 to apply"},
        {"field": "post-body", "block_id": "", "claim_type": "date",
         "status": "queued", "confidence": "LOW", "reasoning": "unverifiable",
         "matched_text": "10 April 2025", "context": "last date 10 April 2025"},
    ]
    enforce_consistency(rows)
    assert rows[0]["status"] == "queued"


def test_parse_claim_date():
    from lib.freshness_utils import parse_claim_date
    import datetime as dt
    assert parse_claim_date("15 March 2025") == dt.date(2025, 3, 15)
    assert parse_claim_date("March 15, 2025") == dt.date(2025, 3, 15)
    assert parse_claim_date("15/03/2025") == dt.date(2025, 3, 15)
    assert parse_claim_date("31/02/2025") is None
    assert parse_claim_date("just 2025") is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as e:
                failed += 1
                print("FAIL %s: %s" % (name, e))
    sys.exit(1 if failed else 0)
