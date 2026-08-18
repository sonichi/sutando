#!/usr/bin/env python3
"""A TCC denial must not be reported as a successful capture (#2942).

`screencapture` exits 0 when Screen Recording permission is denied — it writes a
desktop-only frame and returns success. `check=True` only catches a non-zero
exit, so the success path ran normally and the agent narrated a wallpaper back
as "your screen". Denial and success were byte-identical to every caller.
"""
import importlib.util
import json
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


# Bind the unbound gate to the recorder so the real implementation runs against a
# fake responder. Resolved leniently: absent at the merge-base, so the control reports.
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

# 3. UNKNOWABLE -> allow. Failing closed turns a working capture into a hard
#    failure wherever the symbol cannot resolve — worse than the bug being fixed.
allowed, sent = run_gate(None)
check("an unknowable preflight does NOT block", allowed, True)
check("...and emits no response of its own", sent, [])

# 4. The real preflight must be callable and must never return a truthy string
#    or other non-bool that would make `is False` silently unreachable.
_probe = getattr(m, "screen_capture_permitted", None)
real = _probe() if _probe else "<no probe>"
check("the real preflight returns True/False/None", real in (True, False, None), True)

# 5. The PROMPTING variant must never be INVOKED — it raises a user-visible dialog.
#    Checked over identifiers, not raw text: the name also appears in a docstring.
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
    # `find` not `index`: absent raises. But -1 must FAIL rather than compare
    # less-than-everything, so presence is required explicitly.
    _gi = body.find("_require_screen_permission")
    _si = body.find("screencapture")
    check(f"{route} gates BEFORE screencapture runs",
          _gi != -1 and (_si == -1 or _gi < _si), True)

class _SkipSection(Exception):
    """Raised to skip section 7 wholesale when the probe does not exist."""


# 7. THE FAIL-SAFE BRANCHES. An untested `except` is indistinguishable from one
#    that never runs; drive all three. Absent at the merge-base -> skip, not crash.
_HAS_PROBE = hasattr(m, "_PREFLIGHT") and hasattr(m, "screen_capture_permitted")
_saved = m._PREFLIGHT if _HAS_PROBE else None
try:
    if not _HAS_PROBE:
        for _n in ("an unresolvable symbol yields None",
                   "a raising preflight yields None, not a crash",
                   "a failed library load yields None",
                   "...and is cached, so it is attempted only once"):
            check(_n, "<no probe>", None)
        raise _SkipSection
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
except _SkipSection:
    pass
finally:
    if _HAS_PROBE:
        m._PREFLIGHT = _saved

# 8. THE LOAD SUCCESS PATH, deterministically. On Linux CI find_library returns
#    None and the load raises, so inject a fake library to reach the branch.
if _HAS_PROBE:
    import ctypes
    import ctypes.util

    class _FakeFn:
        restype = None
        argtypes = None
        def __call__(self):
            return True

    class _FakeCG:
        def __init__(self):
            self.CGPreflightScreenCaptureAccess = _FakeFn()

    _of, _ol = ctypes.util.find_library, ctypes.cdll.LoadLibrary
    _saved2 = m._PREFLIGHT
    try:
        ctypes.util.find_library = lambda _n: "fake-coregraphics"
        ctypes.cdll.LoadLibrary = lambda _p: _FakeCG()
        m._PREFLIGHT = "unset"
        check("the load SUCCESS path resolves and returns a bool",
              m.screen_capture_permitted(), True)
        check("...and caches the resolved function rather than re-loading",
              callable(m._PREFLIGHT), True)
    finally:
        ctypes.util.find_library, ctypes.cdll.LoadLibrary = _of, _ol
        m._PREFLIGHT = _saved2

# 9. THE HANDLER CALL SITES. Binding the gate directly never runs the two guard
#    lines, so a gate wired into neither handler would still pass everything above.
if _HAS_PROBE:
    class _FakeReq:
        def __init__(self, path):
            self.path = path
            self.headers = {"X-Sutando-Capture-Token": m.CAPTURE_TOKEN}
            self.sent = []
        def _send_json(self, status, payload):
            self.sent.append((status, payload))
        def _require_capture_token(self):
            return True
        def _require_screen_permission(self):
            return m.Handler._require_screen_permission(self)

    _orig = m.screen_capture_permitted
    m.screen_capture_permitted = lambda: False
    try:
        for route, path in (("_handle_capture", "/capture"),
                            ("_handle_capture_video", "/capture-video")):
            r = _FakeReq(path)
            getattr(m.Handler, route)(r)
            check(f"{route} refuses a denied capture at its gate",
                  [st for st, _ in r.sent], [503])
            check(f"{route} produces no frame when denied",
                  any("path" in pl for _, pl in r.sent), False)
    finally:
        m.screen_capture_permitted = _orig

# A VERIFIED grant and an UNKNOWABLE one must not be byte-identical (#2961 review):
# the gate fails open on `None`, which is right, but a caller could not tell.
class _Probe:
    def __init__(self): self.sent = []
    def _send_json(self, st, pl): m.Handler._send_json(self, st, pl)
    def send_response(self, st): self._st = st
    def send_header(self, *a): pass
    def end_headers(self): pass
    @property
    def wfile(self):
        outer = self
        class _W:
            def write(self, b): outer.sent.append((outer._st, json.loads(b.decode())))
        return _W()

_orig = m.screen_capture_permitted
_bodies = {}
try:
    for verdict, expected in ((True, "granted"), (None, "unknown")):
        m.screen_capture_permitted = lambda v=verdict: v
        r = _Probe()
        check(f"gate passes when permission is {verdict!r}",
              m.Handler._require_screen_permission(r), True)
        r._send_json(200, {"status": "ok", "path": "/tmp/x.png"})
        _bodies[expected] = r.sent[-1][1]
        check(f"a 200 carries permission={expected!r}",
              _bodies[expected].get("permission"), expected)
    # The actual defect: these two were byte-identical before this change.
    check("granted and unknown success bodies now DIFFER",
          _bodies["granted"] != _bodies["unknown"], True)
    # An error body keeps its own shape — no stamp.
    m.screen_capture_permitted = lambda: False
    r = _Probe()
    m.Handler._require_screen_permission(r)
    check("a 503 denial body is not stamped", "permission" in r.sent[-1][1], False)
finally:
    m.screen_capture_permitted = _orig

print(("FAILED: " + ", ".join(fails)) if fails else "screen-capture preflight: all checks passed")
sys.exit(1 if fails else 0)
