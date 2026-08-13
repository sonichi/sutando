#!/usr/bin/env python3
"""`read-quota.py` must not contradict the proxy's own `available` flag.

Covers the extracted predicate AND the production call site, with a control
that reverts the call site so these cannot stay green against the defect.

Run: python3 tests/quota-available-prefers-proxy-flag.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"


def _load():
    """Import is impossible: module scope sys.exit(1)s when quota-state is absent.
    Extract from real source so a rename fails loudly instead of passing."""
    src = SCRIPT.read_text()
    marker = "def resolve_available("
    if marker not in src:
        raise AssertionError(
            "read-quota.py no longer defines resolve_available() — the "
            "implementation moved. Update this test to match the new shape "
            "rather than leaving it green against code that is gone."
        )
    start = src.index(marker)
    rest = src[start:]
    # The function ends at the next top-level `def `/`if `/`class ` line.
    end = len(rest)
    for i, line in enumerate(rest.split("\n")):
        if i and line and not line[0].isspace():
            end = sum(len(l) + 1 for l in rest.split("\n")[:i])
            break
    ns: dict = {}
    exec(rest[:end], ns)  # noqa: S102 - the extracted production function
    return ns["resolve_available"]


resolve_available = _load()


def _check(status, proxy, expected):
    got = resolve_available(status, proxy)
    assert got is expected, (
        f"status={status!r} proxy_available={proxy!r} -> {got!r}, "
        f"expected {expected!r}"
    )


def test_proxy_true_with_allowed_warning_is_available():
    """The reported defect: returned False, so --gate exited 1 mid-quota."""
    _check("allowed_warning", True, True)


def test_explicit_rejected_wins_over_optimistic_flag():
    """A stale `available: true` must not mask a real rejection."""
    _check("rejected", True, False)


def test_proxy_false_is_authoritative_over_allowed_status():
    _check("allowed", False, False)


def test_absent_flag_falls_back_to_allowed():
    _check("allowed", None, True)


def test_absent_flag_rejected_is_unavailable():
    _check("rejected", None, False)


def test_absent_flag_unknown_stays_unavailable():
    """Deliberately conservative, and deliberately NOT health-check's rule:
    declining to page and declining to proceed have opposite fail-safes."""
    _check("unknown", None, False)


def test_absent_flag_allowed_warning_still_unavailable():
    """Design boundary: allowlisting the status instead of trusting the flag
    would make this True, and regress on the next unlisted status."""
    _check("allowed_warning", None, False)


def test_non_bool_flag_is_not_trusted():
    """A string/None/number is not evidence; fall back rather than coerce."""
    _check("allowed_warning", "true", False)
    _check("allowed_warning", 1, False)


# --- production call site, not just the extracted predicate ---

LIVE_DEFECT = {
    "available": True,
    "headers": {
        "anthropic-ratelimit-unified-status": "allowed_warning",
        "anthropic-ratelimit-unified-5h-utilization": "0.10",
        "anthropic-ratelimit-unified-7d-utilization": "0.76",
    },
}


def _run_real_script(tmp, *args, mutate=None):
    """Mirror the four-parent layout read-quota.py walks and stub the resolver, so
    the real source runs against a controlled quota-state instead of the live one."""
    scripts = tmp / "skills" / "quota-tracker" / "scripts"
    scripts.mkdir(parents=True)
    (tmp / "src").mkdir()
    (tmp / "state").mkdir()
    src = SCRIPT.read_text()
    if mutate is not None:
        src = mutate(src)
    (scripts / "read-quota.py").write_text(src)
    (tmp / "src" / "workspace_default.py").write_text(
        "from pathlib import Path\n"
        "def status_read_path(name):\n"
        f"    return Path({str(tmp)!r}) / 'state' / name\n"
    )
    (tmp / "state" / "quota-state.json").write_text(json.dumps(LIVE_DEFECT))
    return subprocess.run(
        [sys.executable, str(scripts / "read-quota.py"), *args],
        capture_output=True, text=True,
    )


def test_gate_exits_zero_through_the_real_call_site():
    with tempfile.TemporaryDirectory() as d:
        r = _run_real_script(Path(d), "--gate")
    assert r.returncode == 0, (
        f"--gate exited {r.returncode} on allowed_warning + available:true; "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )


def test_json_reports_available_through_the_real_call_site():
    with tempfile.TemporaryDirectory() as d:
        r = _run_real_script(Path(d), "--json")
    payload = json.loads(r.stdout)
    assert payload["available"] is True, payload
    assert payload["status"] == "allowed_warning", payload


def test_control_reverted_call_site_fails_the_two_tests_above():
    """Control: with the call site back on the broken expression --gate must exit
    1, or the two call-site tests are not gating the defect at all."""
    def revert(src):
        old = 'available = resolve_available(status, data.get("available"))'
        assert old in src, "call site moved; update this control, do not delete it"
        return src.replace(old, 'available = status == "allowed"', 1)

    with tempfile.TemporaryDirectory() as d:
        r = _run_real_script(Path(d), "--gate", mutate=revert)
    assert r.returncode == 1, (
        f"reverted call site still exited {r.returncode} — the call-site tests "
        "would stay green against the broken predicate"
    )


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"All {len(tests)} quota-available tests passed.")


if __name__ == "__main__":
    main()
