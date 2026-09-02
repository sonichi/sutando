#!/usr/bin/env python3
"""Unit coverage for src/delivery/router.py — the Result Router fallback & audit
policy (spec §4/§7/§9). Pure functions, no I/O, so no fixtures/stubs needed.

Run: python3 tests/result-router-fallback.test.py
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("result_router", _ROOT / "src" / "delivery" / "router.py")
rr = importlib.util.module_from_spec(spec)
# Register before exec so @dataclass can resolve string annotations (the module
# uses `from __future__ import annotations`); mirrors a normal `import`.
sys.modules["result_router"] = rr
spec.loader.exec_module(rr)

_failures = []
def check(cond, msg):
    if not cond:
        _failures.append(msg)

# ── §9.2 late_result_body ────────────────────────────────────────────────────
PFX = rr.LATE_RESULT_PREFIX
check(PFX == "[late result — session ended]", "prefix string is the exact §9.2 wording")

# non-empty body → prefix on its own line + blank line + body
out = rr.late_result_body("Report is ready: 3 items.")
check(out.startswith(PFX + "\n\n"), "non-empty body: prefix then blank line")
check(out.endswith("Report is ready: 3 items."), "non-empty body: original body preserved")

# empty / whitespace body → bare prefix (still surfaced, not swallowed)
check(rr.late_result_body("") == PFX, "empty body → bare prefix")
check(rr.late_result_body("   \n ") == PFX, "whitespace-only body → bare prefix")

# idempotent: already-prefixed body is unchanged (guards double-prefix)
once = rr.late_result_body("hello")
twice = rr.late_result_body(once)
check(once == twice, "late_result_body is idempotent")
# leading-whitespace before an existing prefix is still recognized as prefixed
check(rr.late_result_body("  " + PFX + "\n\nx") == "  " + PFX + "\n\nx", "leading ws + existing prefix unchanged")
# None-safe
check(rr.late_result_body(None) == PFX, "None body → bare prefix (no crash)")

# ── §4 is_fallback_trigger ───────────────────────────────────────────────────
for t in ("session_closed", "delivery_error", "over_limit", "channel_gated"):
    check(rr.is_fallback_trigger(t) is True, f"{t} IS a fallback trigger")
for n in ("slow_delivery", "user_idle", "empty_result"):
    check(rr.is_fallback_trigger(n) is False, f"{n} is NOT a fallback trigger (§4)")
check(rr.is_fallback_trigger("some_unknown_reason") is False, "unknown reason → False (fail-safe)")
check(rr.is_fallback_trigger("") is False, "empty reason → False")

# ── §9.3 delivery_failure_notice ─────────────────────────────────────────────
f = rr.DeliveryFailure(task_id="task-abc", tier="owner", surface="discord", error="403 Forbidden")
note = rr.delivery_failure_notice(f)
for token in ("task-abc", "owner", "discord", "403 Forbidden"):
    check(token in note, f"failure notice names {token!r}")
check("did not see this" in note, "failure notice is loud (names the miss)")

# unknown tier renders as 'unknown', not empty
f2 = rr.DeliveryFailure(task_id="task-x", tier="", surface="slack", error="timeout")
check("unknown" in rr.delivery_failure_notice(f2), "empty tier → 'unknown'")

# frozen dataclass (facts are immutable once captured)
try:
    f.error = "mutated"  # type: ignore[misc]
    check(False, "DeliveryFailure must be frozen")
except Exception:
    check(True, "DeliveryFailure is frozen")

# ── §7 audit_line ────────────────────────────────────────────────────────────
line = rr.audit_line("task-9", "delivered", "telegram", "2026-07-07T02:40:00Z")
parts = line.split("\t")
check(parts == ["2026-07-07T02:40:00Z", "task-9", "delivered", "telegram"],
      "audit line is tab-separated ts/task/disposition/surface")
check("\n" not in line, "audit line is single-line (grep-friendly)")

if _failures:
    print(f"FAIL ({len(_failures)}):")
    for m in _failures:
        print("  -", m)
    raise SystemExit(1)
print("result-router-fallback: all checks passed")
