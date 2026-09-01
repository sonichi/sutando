#!/usr/bin/env python3
"""`read-quota.py` must not contradict the proxy's own `available` flag.
Covers the predicate in-process plus the production call site and its --gate exit."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"


_STATE = Path(tempfile.mkdtemp(prefix="quota-available-")) / "state"
_MODULE = None


def _load():
    """Unstubbed import is impossible: module scope sys.exit(1)s without quota-state.
    Stub the resolver in sys.modules so the REAL module imports in-process."""
    _STATE.mkdir(parents=True, exist_ok=True)
    (_STATE / "quota-state.json").write_text(json.dumps(
        {"available": True,
         "headers": {"anthropic-ratelimit-unified-status": "allowed_warning"}}))
    stub = types.ModuleType("workspace_default")
    stub.status_read_path = lambda name: _STATE / name
    sys.modules["workspace_default"] = stub
    spec = importlib.util.spec_from_file_location("read_quota_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "resolve_available"):
        raise AssertionError(
            "read-quota.py no longer defines resolve_available() — the "
            "implementation moved. Update this test to match the new shape "
            "rather than leaving it green against code that is gone."
        )
    global _MODULE
    _MODULE = module
    return module.resolve_available

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

PROXY_URL = "http://127.0.0.1:7846"

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
    (scripts / "quota_availability.py").write_text(
        (REPO / "skills" / "quota-tracker" / "scripts" / "quota_availability.py").read_text()
    )
    (tmp / "src" / "workspace_default.py").write_text(
        "from pathlib import Path\n"
        "def status_read_path(name):\n"
        f"    return Path({str(tmp)!r}) / 'state' / name\n"
    )
    (tmp / "state" / "quota-state.json").write_text(json.dumps(LIVE_DEFECT))
    # Pin routed explicitly: inheriting the host's ANTHROPIC_BASE_URL makes these
    # pass on a routed dev box and fail in CI, which is not a test of anything.
    env = {**os.environ, "ANTHROPIC_BASE_URL": PROXY_URL}
    return subprocess.run(
        [sys.executable, str(scripts / "read-quota.py"), *args],
        capture_output=True, text=True, env=env,
    )


def test_main_gate_exits_zero_in_process():
    """In-process so the call site itself is measured; the subprocess variants below
    prove the same contract but run a copy the coverage gate cannot attribute."""
    argv, prev = sys.argv, os.environ.get("ANTHROPIC_BASE_URL")
    sys.argv = ["read-quota.py", "--gate"]
    os.environ["ANTHROPIC_BASE_URL"] = PROXY_URL
    try:
        _MODULE.main()
    except SystemExit as exc:
        assert exc.code == 0, f"--gate exited {exc.code} on allowed_warning + available:true"
    else:
        raise AssertionError("--gate returned without calling sys.exit")
    finally:
        sys.argv = argv
        if prev is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = prev


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
        old = ('decision = availability_decision(\n'
               '        data,\n'
               '        base_url=os.environ.get("ANTHROPIC_BASE_URL"),\n'
               '        stale=stale,\n'
               '    )')
        assert old in src, "call site moved; update this control, do not delete it"
        broken = ('decision = {"routed": True, "available": status == "allowed", '
                  '"unavailable_reason": None}')
        return src.replace(old, broken, 1)

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
