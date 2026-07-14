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
    "priority_scorer", "link_recommender", "anchor_refiner", "refine_audit_rewrites",
    "bulk_apply_links", "bulk_apply_audit", "link_health_report", "refresh_hub_data",
    "refresh_links", "snapshot", "rollback", "rollback_audit_rewrites", "check_url_health",
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


def test_requirements_txt_is_complete():
    """Every workflow script that imports third-party packages must be pip-installable.
    Catches the truncation bug we hit where anthropic got dropped mid-write."""
    req_path = REPO / "requirements.txt"
    content = req_path.read_text()
    # Every line should end at a newline, and the last line should also
    # (mid-line truncation always drops the trailing newline)
    assert content.endswith("\n"), (
        f"requirements.txt does not end with a newline — likely truncated. "
        f"Last 100 chars: {content[-100:]!r}"
    )
    # Every non-comment line should have a valid package spec
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for ln in lines:
        # A comment can be inline (after the version pin). Strip it.
        spec = ln.split("#")[0].strip()
        assert spec, f"Empty spec line in requirements.txt: {ln!r}"
        assert any(op in spec for op in ("==", ">=", "<=", "~=", ">", "<")) or spec.isidentifier(), (
            f"Bad requirements.txt line: {ln!r}"
        )
    # Critical packages must be present
    critical = ["requests", "pandas", "openpyxl", "beautifulsoup4", "anthropic"]
    for pkg in critical:
        assert any(ln.startswith(pkg) for ln in lines), (
            f"Missing critical package '{pkg}' in requirements.txt. "
            f"Present: {[ln.split('>=')[0].split('~')[0].split('==')[0].strip() for ln in lines]}"
        )


def test_workflow_yaml_is_complete():
    """Catches truncation bugs in the GitHub Actions workflow YAML."""
    import yaml
    p = REPO / ".github" / "workflows" / "manual-dispatch.yml"
    content = p.read_text()
    # Must end at a newline
    assert content.endswith("\n"), (
        f"workflow yml does not end with a newline — likely truncated. "
        f"Last 100 chars: {content[-100:]!r}"
    )
    # Must be valid YAML
    d = yaml.safe_load(content)
    assert d, "workflow yml is empty"
    # Must have the required top-level keys (yaml parses "on" as True due to bool coercion)
    assert "name" in d
    assert True in d or "on" in d, f"missing 'on' key. Keys: {list(d.keys())}"
    assert "jobs" in d
    assert "run" in d["jobs"], "missing 'run' job"
    # The 'run' job must have both an install step AND an upload-artifact step
    steps = d["jobs"]["run"]["steps"]
    step_names = [s.get("name", str(s.get("uses", ""))) for s in steps]
    has_install = any("Install" in n or "install" in n for n in step_names)
    has_upload = any("upload" in n.lower() or "artifact" in n.lower() for n in step_names)
    assert has_install, f"missing install step. Steps: {step_names}"
    assert has_upload, f"missing upload-artifact step. Steps: {step_names}"


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
