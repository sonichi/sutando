#!/usr/bin/env python3
"""A TCC denial must not be reported as a successful capture (#2942).

`screencapture` exits 0 when Screen Recording permission is denied — it writes a
desktop-only frame and returns success. `check=True` only catches a non-zero
exit, so the success path ran normally and the agent narrated a wallpaper back
as "your screen". Denial and success were byte-identical to every caller.
"""
import importlib.util
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "src" / "screen-capture-server.py"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got {got!r}, want {want!r}")
        fails.append(name)


def load():
    spec = importlib.util.spec_from_file_location("scs_under_test", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = load()


class _Recorder:
    """Stands in for the handler: captures what _send_json would emit."""
    def __init__(self):
        self.sent = []

    def _send_json(self, status, payload):
        self.sent.append((status, payload))


# The gate is an unbound method on the handler class; bind it to the recorder so
# the real implementation runs against a fake responder. Absent at the merge-base
# — resolved leniently so the control reports which behaviours differ instead of
# dying on an AttributeError at import time.
GATE = getattr(m.Handler, "_require_screen_permission", None)


def run_gate(permitted):
    # Both the probe and the gate are absent at the merge-base; resolve leniently
    # so the control reports which behaviours differ rather than dying on import.
    orig = getattr(m, "screen_capture_permitted", None)
    m.screen_capture_permitted = lambda: permitted
    try:
        if GATE is None:
            return "<no gate>", []
        r = _Recorder()
        allowed = GATE(r)
        return allowed, r.sent
    finally:
        if orig is None:
            delattr(m, "screen_capture_permitted")
        else:
            m.screen_capture_permitted = orig


# 1. Explicit denial -> blocked, with a distinguishable payload.
allowed, sent = run_gate(False)
check("an explicit denial blocks the capture", allowed, False)
check("...and emits exactly one response", len(sent), 1)
status, payload = sent[0] if sent else (None, {})
check("...with a non-2xx status", status, 503)
check("...whose status field is 'denied', not 'ok'", payload.get("status"), "denied")
check("...and carries a remedy the operator can act on",
      "Screen & System Audio" in payload.get("remedy", ""), True)

# 2. CONTROL — granted must pass through silently. Without this, a gate that
#    always blocked would satisfy every assertion above.
allowed, sent = run_gate(True)
check("a granted permission allows the capture", allowed, True)
check("...and emits no response of its own", sent, [])

# 3. UNKNOWABLE -> allow. Failing closed here would turn a working capture into
#    a hard failure wherever the preflight symbol cannot be resolved, which is a
#    worse regression than the bug being fixed.
allowed, sent = run_gate(None)
check("an unknowable preflight does NOT block", allowed, True)
check("...and emits no response of its own", sent, [])

# 4. The real preflight must be callable and must never return a truthy string
#    or other non-bool that would make `is False` silently unreachable.
_probe = getattr(m, "screen_capture_permitted", None)
real = _probe() if _probe else "<no probe>"
check("the real preflight returns True/False/None", real in (True, False, None), True)

# 5. The PROMPTING variant must never be INVOKED: it raises a system dialog,
#    which would make an internal capture user-visible. Checked over identifiers
#    rather than raw text — the name legitimately appears in a docstring saying
#    not to use it, and a substring test cannot tell that from a call.
import ast

src = MOD.read_text()
tree = ast.parse(src)
idents = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
idents |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
idents |= {c.value for c in ast.walk(tree)
           if isinstance(c, ast.Constant) and isinstance(c.value, str) and "\n" not in c.value}
check("CGRequestScreenCaptureAccess is never referenced as code",
      "CGRequestScreenCaptureAccess" in idents, False)
check("CGPreflightScreenCaptureAccess IS referenced as code",
      "CGPreflightScreenCaptureAccess" in idents, True)

# 6. Both capture routes are gated, before any side effect. A gate wired into
#    only /capture leaves /capture-video reporting denial as success.
for route in ("_handle_capture", "_handle_capture_video"):
    body = src.split(f"def {route}(self) -> None:", 1)[1][:900] if f"def {route}" in src else ""
    check(f"{route} calls the permission gate",
          "_require_screen_permission" in body, True)
    check(f"{route} gates BEFORE screencapture runs",
          body.index("_require_screen_permission") < (body.index("screencapture")
                                                      if "screencapture" in body else len(body)), True)

# 7. THE FAIL-SAFE BRANCHES. Each `except` in the probe decides what happens
#    when the platform will not answer, and an untested fail-safe is
#    indistinguishable from one that never runs. Drive all three.
_saved = m._PREFLIGHT
try:
    m._PREFLIGHT = None                       # resolved once, unavailable
    check("an unresolvable symbol yields None", m.screen_capture_permitted(), None)

    def _boom():
        raise OSError("simulated CoreGraphics failure")
    m._PREFLIGHT = _boom                      # resolves, then raises when called
    check("a raising preflight yields None, not a crash",
          m.screen_capture_permitted(), None)

    # The load path itself: force find_library to miss, so LoadLibrary raises.
    import ctypes.util
    _orig_find = ctypes.util.find_library
    ctypes.util.find_library = lambda _name: "no-such-framework-xyz"
    try:
        m._PREFLIGHT = "unset"                # force a fresh resolution attempt
        check("a failed library load yields None", m.screen_capture_permitted(), None)
        check("...and is cached, so it is attempted only once", m._PREFLIGHT, None)
    finally:
        ctypes.util.find_library = _orig_find
finally:
    m._PREFLIGHT = _saved

print(("FAILED: " + ", ".join(fails)) if fails else "screen-capture preflight: all checks passed")
sys.exit(1 if fails else 0)
