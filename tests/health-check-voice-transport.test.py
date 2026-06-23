#!/usr/bin/env python3
"""
Tests for check_voice_transport in src/health-check.py.

Covers the log-scan logic that classifies Gemini transport closes:
  a) no log file → warn
  b) no startup banner → warn
  c) clean log (no closes) → ok
  d) 1000-close (healthy) → ok
  e) 1008-close then setup-complete → ok (recovered)
  f) 1008-close as last event (mid-cycle window) → warn (NOT fail)
  g) 1011-close without GoAway → fail
  h) GoAway + 1011-close (idle timeout) → ok
  i) 1006-close → warn (DNS blip, same as before)
  j) 1008-close + >20 CONNECTING lines → fail (stuck)
  k) multiple cycles: 1008 → recover → 1008 (last event) → warn

Regression guard for the 2026-06-15 fix: code=1008 health probes that fire
inside the ~30s reconnect window must produce warn, not fail. Before the fix
the catch-all else-branch flagged every unrecognised code as fail, causing
persistent red-on-dashboard through entirely normal self-healing cycles.

Run: python3 tests/health-check-voice-transport.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

BANNER = "Sutando — Voice Interface"
OK_VOICE = {"status": "ok"}


def run_with_log(log_lines: list[str] | None):
    """Write log_lines into a temp workspace and call check_voice_transport.
    Pass None to skip writing the log file at all."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        if log_lines is not None:
            logs_dir = td / "logs"
            logs_dir.mkdir()
            (logs_dir / "voice-agent.log").write_text("\n".join(log_lines))
        orig = hc.WORKSPACE_DIR
        try:
            hc.WORKSPACE_DIR = td
            return hc.check_voice_transport(OK_VOICE)
        finally:
            hc.WORKSPACE_DIR = orig


def close_line(code: int, reason: str = "reason") -> str:
    return f'10:00:00.000 [VoiceSession] Transport closed (state=ACTIVE code={code} reason="{reason}")'


SETUP_COMPLETE = "10:00:01.000 [VoiceSession] Gemini setup complete (clientConnected=true)"
GOAWAY = "09:59:00.000 [VoiceSession] GoAway from Gemini"
CONNECTING = "10:00:00.000 [Health] state=CONNECTING"


# ---------------------------------------------------------------------------
# test cases
# ---------------------------------------------------------------------------

def case_a_no_log() -> list[str]:
    r = run_with_log(None)
    if r["status"] != "warn":
        return [f"a) missing log should be warn, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_b_no_banner() -> list[str]:
    r = run_with_log(["10:00:00.000 [VoiceSession] some line without a banner"])
    if r["status"] != "warn":
        return [f"b) missing banner should be warn, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_c_clean_log() -> list[str]:
    r = run_with_log([BANNER, SETUP_COMPLETE])
    if r["status"] != "ok":
        return [f"c) clean log should be ok, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_d_healthy_close_1000() -> list[str]:
    r = run_with_log([BANNER, SETUP_COMPLETE, close_line(1000, "Normal Closure")])
    if r["status"] != "ok":
        return [f"d) 1000-close should be ok (healthy), got {r['status']!r}: {r['detail']!r}"]
    return []


def case_e_1008_then_recover() -> list[str]:
    """1008 close followed by setup-complete — health probe fires after reconnect."""
    r = run_with_log([BANNER, SETUP_COMPLETE, close_line(1008, "The operation was aborted."), SETUP_COMPLETE])
    if r["status"] != "ok":
        return [f"e) 1008 then setup-complete should be ok (recovered), got {r['status']!r}: {r['detail']!r}"]
    return []


def case_f_1008_mid_cycle() -> list[str]:
    """1008 as last log event — health probe fires in the ~30s reconnect window.
    Must be WARN, not FAIL. Regression guard for the 2026-06-15 fix."""
    r = run_with_log([BANNER, SETUP_COMPLETE, close_line(1008, "The operation was aborted.")])
    if r["status"] != "warn":
        return [f"f) 1008 mid-cycle must be warn (not fail), got {r['status']!r}: {r['detail']!r}"]
    if "1008" not in r["detail"]:
        return [f"f) warn detail should mention code 1008, got: {r['detail']!r}"]
    return []


def case_g_1011_no_goaway() -> list[str]:
    """1011 without GoAway → real quota/upstream failure → fail."""
    r = run_with_log([BANNER, SETUP_COMPLETE, close_line(1011, "exceeded your current quota")])
    if r["status"] != "fail":
        return [f"g) 1011 without GoAway should be fail, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_h_goaway_then_1011() -> list[str]:
    """GoAway + 1011 = idle timeout path — normal lifecycle, not a failure."""
    r = run_with_log([BANNER, SETUP_COMPLETE, GOAWAY, close_line(1011, "The service is currently unavailable.")])
    if r["status"] != "ok":
        return [f"h) GoAway+1011 (idle timeout) should be ok, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_i_1006_dns_blip() -> list[str]:
    """1006 abnormal network close — downgraded to warn when DNS resolves."""
    r = run_with_log([BANNER, SETUP_COMPLETE, close_line(1006, "abnormal")])
    # DNS check runs live; result is warn (DNS ok) or fail (DNS broken).
    # Either is acceptable — just not "ok" or a crash.
    if r["status"] not in ("warn", "fail"):
        return [f"i) 1006 close should be warn or fail, got {r['status']!r}: {r['detail']!r}"]
    return []


def case_j_1008_stuck() -> list[str]:
    """1008 close followed by >20 CONNECTING lines = stuck, not self-healing."""
    lines = [BANNER, SETUP_COMPLETE, close_line(1008, "The operation was aborted.")]
    lines += [CONNECTING] * 21  # 21 > threshold of 20
    r = run_with_log(lines)
    if r["status"] != "fail":
        return [f"j) 1008 + >20 CONNECTING should be fail (stuck), got {r['status']!r}: {r['detail']!r}"]
    if not r.get("_stuck_connecting"):
        return [f"j) _stuck_connecting flag should be set, detail: {r['detail']!r}"]
    return []


def case_k_multiple_cycles_last_mid() -> list[str]:
    """Multiple 1008 → recover cycles; last event is 1008 (mid-cycle). Must warn."""
    lines = [
        BANNER,
        SETUP_COMPLETE,
        close_line(1008, "The operation was aborted."),
        SETUP_COMPLETE,
        close_line(1008, "The operation was aborted."),
        SETUP_COMPLETE,
        close_line(1008, "The operation was aborted."),
        # no trailing SETUP_COMPLETE — probe caught mid-cycle
    ]
    r = run_with_log(lines)
    if r["status"] != "warn":
        return [f"k) multi-cycle ending in 1008 must be warn, got {r['status']!r}: {r['detail']!r}"]
    return []


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("a", case_a_no_log),
        ("b", case_b_no_banner),
        ("c", case_c_clean_log),
        ("d", case_d_healthy_close_1000),
        ("e", case_e_1008_then_recover),
        ("f", case_f_1008_mid_cycle),
        ("g", case_g_1011_no_goaway),
        ("h", case_h_goaway_then_1011),
        ("i", case_i_1006_dns_blip),
        ("j", case_j_1008_stuck),
        ("k", case_k_multiple_cycles_last_mid),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nAll voice-transport health-check invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
