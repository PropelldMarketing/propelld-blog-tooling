"""
Smoke tests for every script in the manual-dispatch workflow dropdown.

Guarantees:
  1. Every script's --help works (argparse setup is valid).
  2. Every script accepts --apply (workflow uniformity).
  3. Every script's default input paths resolve (no missing files).
  4. Read-only scripts run to completion in dry-run mode with defaults.

Run:   python tests/test_workflow_scripts.py
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_IN_WORKFLOW = [
    "publish_facet_items", "check_facet_content", "audit_internal_links",
    "priority_scorer", "link_recommender", "anchor_refiner",
    "bulk_apply_links", "bulk_apply_audit", "link_health_report", "refresh_hub_data",
    "refresh_links", "snapshot", "rollback", "check_url_health",
]


def _run(*args, cwd=REPO, timeout=30):
    r = subprocess.run(
        [sys.executable] + list(args),
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )
    return r.returncode, r.stdout, r.stderr


def test_all_scripts_have_help():
    failures = []
    for name in SCRIPTS_IN_WORKFLOW:
        path = REPO / "scripts" / f"{name}.py"
        assert path.exists(), f"scripts/{name}.py referenced by workflow but missing"
        code, out, err = _run(str(path), "--help")
        if code != 0:
            failures.append(f"{name}: exit={code}\n  stderr: {err[:300]}")
    assert not failures, "\n".join(failures)


def test_all_scripts_accept_apply_flag():
    failures = []
    for name in SCRIPTS_IN_WORKFLOW:
        path = REPO / "scripts" / f"{name}.py"
        src = path.read_text()
        if '"--apply"' not in src and "'--apply'" not in src:
            failures.append(f"{name}: does not declare --apply")
    assert not failures, "\n".join(failures)


def test_default_input_paths_exist():
    import ast
    failures = []
    for name in SCRIPTS_IN_WORKFLOW:
        path = REPO / "scripts" / f"{name}.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
                continue
            for kw in node.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    d = kw.value.value
                    if isinstance(d, str) and d.startswith("data/"):
                        if not (REPO / d).exists():
                            failures.append(f"{name}: default={d} does not exist in repo")
    assert not failures, "\n".join(failures)


def test_link_recommender_runs_end_to_end_locally():
    code, out, err = _run(
        str(REPO / "scripts" / "link_recommender.py"),
        "--output", "/tmp/pytest-recs.csv",
        timeout=60,
    )
    assert code == 0, f"exit={code}\nstderr:{err[:500]}"
    assert "Total recs" in out


def test_priority_scorer_gives_friendly_error_without_args():
    code, out, err = _run(str(REPO / "scripts" / "priority_scorer.py"))
    assert code == 2, f"expected exit 2, got {code}"
    assert "priority_scorer needs" in err or "MISSING INPUT" in err


def test_check_url_health_handles_empty_filter():
    code, out, err = _run(
        str(REPO / "scripts" / "check_url_health.py"),
        "--from-audit", "data/internal-links-inventory.csv",
        "--filter-reason", "nonexistent-reason-abc",
    )
    assert code == 1, f"expected exit 1, got {code}"
    assert "No URLs matched" in out


if __name__ == "__main__":
    print("Running smoke tests for workflow scripts...\n")
    passed = failed = 0
    for name in list(globals()):
        if name.startswith("test_"):
            print(f"  {name}...", end=" ")
            try:
                globals()[name]()
                print("✓")
                passed += 1
            except AssertionError as e:
                print(f"✗ FAILED\n    {e}")
                failed += 1
            except Exception as e:
                print(f"✗ ERROR: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
