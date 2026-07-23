"""
Regression tests for the July-2026 executor/audit fixes:

  BUG-1  apply_insertion_to_paragraph must NOT destroy existing links or
         inline formatting (old version flattened the paragraph to text).
  BUG-3  idempotency must be exact-path, not substring (prefix collisions).
  UTM    only LP CTA pages (utm_cta_pages) get UTM params, not all T0.
  AUDIT  duplicate-target kills must allow 2 occurrences for T0/CTA pages.
  SAFETY link-count invariant refuses writes that lose links.

Run: python -m pytest tests/test_insertion_fixes.py -v
"""
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.insert_planned_links import apply_insertion_to_paragraph
from scripts.bulk_apply_links import append_utm_if_t0
from scripts.audit_internal_links import mark_duplicate_kills
from lib.link_utils import extract_links, normalize_url


# ---------- BUG-1: splice preserves existing markup ----------

def test_insertion_preserves_existing_links_and_formatting():
    html = ('<p>Intro here. <a href="/site/blog/old-post">an old link</a> must stay. '
            'Students can check the fee structure online. '
            '<strong>Bold text</strong> must stay too.</p>')
    soup = BeautifulSoup(html, "html.parser")
    ok, reason = apply_insertion_to_paragraph(
        soup, 0,
        "Students can check the fee structure online.",
        'Students can check the <a href="/site/blog/fees">fee structure</a> online.')
    out = str(soup)
    assert ok, reason
    assert '<a href="/site/blog/old-post">an old link</a>' in out   # old link intact
    assert '<strong>Bold text</strong>' in out                       # formatting intact
    assert '/site/blog/fees' in out                                  # new link added
    assert len(BeautifulSoup(out, "html.parser").find_all("a")) == 2


def test_sentence_spanning_inline_markup_is_skipped_not_flattened():
    html = '<p>The <strong>SBI education loan</strong> is popular among students. Filler.</p>'
    soup = BeautifulSoup(html, "html.parser")
    ok, reason = apply_insertion_to_paragraph(
        soup, 0,
        "The SBI education loan is popular among students.",
        'rewritten <a href="/site/x">x</a>')
    assert not ok
    assert "spans inline markup" in reason
    assert "<strong>SBI education loan</strong>" in str(soup)  # untouched


def test_never_splices_inside_existing_link():
    html = '<p><a href="/site/blog/a">Check the fee structure online</a> today.</p>'
    soup = BeautifulSoup(html, "html.parser")
    ok, reason = apply_insertion_to_paragraph(
        soup, 0, "Check the fee structure online",
        'x <a href="/site/y">y</a>')
    assert not ok  # text lives inside <a>; must not be touched


def test_whitespace_normalization_still_matches():
    html = '<p>Alpha beta.  Students   can\n check fees online. Gamma.</p>'
    soup = BeautifulSoup(html, "html.parser")
    ok, reason = apply_insertion_to_paragraph(
        soup, 0,
        "Students can check fees online.",
        'Students can check <a href="/site/blog/fees">fees</a> online.')
    assert ok, reason
    assert '/site/blog/fees' in str(soup)


def test_out_of_range_paragraph():
    soup = BeautifulSoup("<p>only one</p>", "html.parser")
    ok, reason = apply_insertion_to_paragraph(soup, 5, "x", "y")
    assert not ok and "out of range" in reason


# ---------- BUG-3: exact-path idempotency semantics ----------

def test_normalized_targets_have_no_prefix_collision():
    body = '<p><a href="/site/education-loan-emi-calculator">EMI calc</a></p>'
    existing = {l["href"] for l in extract_links(body)}
    assert "/site/education-loan-emi-calculator" in existing
    # the old substring check would have blocked this:
    assert normalize_url("/site/education-loan") not in existing


def test_utm_variant_counts_as_same_target():
    body = '<p><a href="/site/lp/study-loan-eligibility?utm_source=website">CTA</a></p>'
    existing = {l["href"] for l in extract_links(body)}
    assert normalize_url("/site/lp/study-loan-eligibility") in existing


# ---------- UTM: LP-only policy ----------

def test_utm_appended_for_lp_cta_page():
    out = append_utm_if_t0("/site/lp/study-loan-eligibility", "/site/blog/some-post")
    assert out.startswith("/site/lp/study-loan-eligibility?utm_source=website")
    assert "utm_adgroup=blog-body" in out
    assert "utm_campaign=/site/blog/some-post" in out


def test_no_utm_for_regular_t0_money_pages():
    for t0 in ["/site/education-loan", "/site/college-loan",
               "/site/mba-education-loan", "/site/education-loan-emi-calculator"]:
        assert append_utm_if_t0(t0, "/site/blog/x") == t0


def test_no_double_utm():
    already = "/site/lp/study-loan-eligibility?utm_source=website"
    assert append_utm_if_t0(already, "/site/blog/x") == already


# ---------- AUDIT: T0/CTA duplicate allowance ----------

def _df(rows):
    return pd.DataFrame(rows, columns=["source_url", "target_url",
                                       "position_in_field", "action", "reason"])

T0 = ["/site/education-loan", "/site/lp/study-loan-eligibility"]

def test_second_cta_to_lp_is_protected():
    df = _df([
        ("/site/blog/p1", "/site/lp/study-loan-eligibility", 0, "KEEP", "compliant"),
        ("/site/blog/p1", "/site/lp/study-loan-eligibility", 5, "KEEP", "compliant"),
    ])
    df, dups = mark_duplicate_kills(df, T0, t0_allowance=2)
    assert dups.sum() == 0
    assert (df["action"] == "KEEP").all()


def test_third_cta_still_killed():
    df = _df([
        ("/site/blog/p1", "/site/education-loan", 0, "KEEP", "compliant"),
        ("/site/blog/p1", "/site/education-loan", 3, "KEEP", "compliant"),
        ("/site/blog/p1", "/site/education-loan", 7, "KEEP", "compliant"),
    ])
    df, dups = mark_duplicate_kills(df, T0, t0_allowance=2)
    assert dups.sum() == 1
    killed = df[df["action"] == "KILL"]
    assert list(killed["position_in_field"]) == [7]  # first two kept, in order


def test_non_t0_duplicates_still_killed_keeping_first():
    df = _df([
        ("/site/blog/p1", "/site/blog/target-x", 2, "KEEP", "compliant"),
        ("/site/blog/p1", "/site/blog/target-x", 8, "KEEP", "compliant"),
        ("/site/blog/p2", "/site/blog/target-x", 1, "KEEP", "compliant"),  # other source: fine
    ])
    df, dups = mark_duplicate_kills(df, T0, t0_allowance=2)
    assert dups.sum() == 1
    killed = df[df["action"] == "KILL"]
    assert list(killed["position_in_field"]) == [8]
    assert (df[df["source_url"] == "/site/blog/p2"]["action"] == "KEEP").all()
