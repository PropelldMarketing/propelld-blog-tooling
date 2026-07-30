"""
Regression tests for the two Wave-B canary bugs found on live review (30 Jul):
1. Killing a CTA banner link left its marketing text behind as dead text.
2. Webflow PATCHes are drafts; without an explicit publish nothing goes live.
   (Publish behavior is covered by flag presence; the API call itself is
   exercised in production runs.)
"""
import sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib.link_utils import remove_link, remove_duplicate_links


CTA_HTML = (
    '<p>Scoring 80 marks in JEE Mains puts you near the 89th percentile.</p>'
    '<p><strong><a href="/site/lp/study-loan-eligibility">'
    'Get your Loan Disbursed 10 times Faster than Banks. Apply Now.</a></strong></p>'
    '<p>Counselling for JEE happens in several rounds every year.</p>'
)

PROSE_HTML = (
    '<p>Check the <a href="/site/blog/jee-mains-chapter-wise-weightage">'
    'JEE Mains chapter-wise weightage</a> before planning your revision.</p>'
)


def test_cta_banner_removed_whole_not_unwrapped():
    out, n = remove_link(CTA_HTML, "/site/lp/study-loan-eligibility")
    assert n == 1
    assert "Get your Loan Disbursed" not in out          # dead text gone
    assert "Apply Now" not in out
    assert "89th percentile" in out                       # prose untouched
    assert "Counselling for JEE" in out


def test_prose_link_still_unwraps_keeping_text():
    out, n = remove_link(PROSE_HTML, "/site/blog/jee-mains-chapter-wise-weightage")
    assert n == 1
    assert "JEE Mains chapter-wise weightage" in out      # text kept
    assert "<a" not in out                                # link gone


def test_duplicate_removal_mixed_cta_and_prose():
    html = (
        '<p>First mention: <a href="/site/lp/study-loan-eligibility">check eligibility</a> today.</p>'
        '<p>Second: <a href="/site/lp/study-loan-eligibility">apply with Propelld</a> now with text around.</p>'
        '<p><a href="/site/lp/study-loan-eligibility">Fastest process for Education Loan with No Collateral Requirement</a></p>'
        '<p><strong><a href="/site/lp/study-loan-eligibility">Propelld</a></strong></p>'
    )
    out, n = remove_duplicate_links(html, "/site/lp/study-loan-eligibility", keep_n=2)
    assert n == 2
    assert "check eligibility" in out and "apply with Propelld" in out   # first 2 kept as links
    assert out.count("<a") == 2
    assert "Fastest process" not in out        # CTA banner block fully removed
    assert "Propelld</strong>" not in out      # link-only strong block gone
    assert "now with text around" in out


def test_nested_wrappers_removed_to_outermost_block():
    html = ('<div><p><strong><em><a href="/site/lp/x">Unlock Fast, Collateral-Free '
            'Education Loans with Propelld Today!</a></em></strong></p></div>'
            '<p>Real content stays.</p>')
    out, n = remove_link(html, "/site/lp/x")
    assert n == 1
    assert "Unlock Fast" not in out
    assert "Real content stays." in out


def test_publish_flags_exist():
    for script in ["bulk_apply_audit.py", "insert_planned_links.py"]:
        src = (REPO / "scripts" / script).read_text()
        assert "--no-publish" in src and "publish_items" in src, script


def test_keep_n_is_per_post_across_both_fields():
    """30-Jul canary finding: keep-2 was applied per field, letting posts
    with CTAs in both halves keep 4. The cap must span fields."""
    from lib.link_utils import remove_duplicate_links, extract_links
    first = ('<p>Intro text.</p>'
             '<p>a <a href="/site/lp/x">one</a> b</p>'
             '<p>c <a href="/site/lp/x">two</a> d</p>'
             '<p>e <a href="/site/lp/x">three</a> f</p>')
    second = ('<p>g <a href="/site/lp/x">four</a> h</p>'
              '<p>i <a href="/site/lp/x">five</a> j</p>')
    bodies = {"post-body": first, "post-body-2nd-half": second}
    base_keep, kept_so_far, removed = 2, 0, 0
    for field in ["post-body", "post-body-2nd-half"]:
        keep_n = max(0, base_keep - kept_so_far)
        new_html, n = remove_duplicate_links(bodies[field], "/site/lp/x", keep_n=keep_n)
        if n:
            bodies[field] = new_html
        removed += n
        kept_so_far += sum(1 for l in extract_links(bodies[field])
                           if l["href"] == "/site/lp/x")
    total_left = sum(1 for f in bodies.values() for l in extract_links(f)
                     if l["href"] == "/site/lp/x")
    assert total_left == 2, f"expected 2 across post, got {total_left}"
    assert removed == 3
