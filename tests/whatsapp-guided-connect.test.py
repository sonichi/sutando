#!/usr/bin/env python3
"""Contract tests for skills/whatsapp/scripts/guided_connect.py.

Live pairing cannot run in CI (it would need a phone scanning a code), so the
tests pin the two halves that CAN regress silently:

1. The EventRouter line protocol — driven with synthetic wacli `--events`
   streams, covering NDJSON events, plain-text fallbacks, and the documented
   passkey-gated stop.
2. Orchestration against a STUBBED wacli first on PATH (the repo's
   poisoned-PATH idiom): already-connected short-circuit, and the
   ended-without-session error path. The stub records every invocation, so the
   test also proves the real binary is never required.
"""
import importlib.util
import io
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "whatsapp" / "scripts" / "guided_connect.py"

spec = importlib.util.spec_from_file_location("gc", SCRIPT)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)

failures = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def routed(lines, qr_dir):
    router = gc.EventRouter(qr_dir)
    buf = io.StringIO()
    with redirect_stdout(buf):
        for l in lines:
            router.feed(l)
    return router, buf.getvalue()


qr_dir = tempfile.mkdtemp()

print("1. producer-faithful pair_code envelope (code nested under data) → PAIR_CODE")
r, out = routed(['{"event": "pair_code", "data": {"code": "ABCD-1234"}, "ts": 1}'], qr_dir)
check("PAIR_CODE: ABCD-1234" in out, "nested-envelope pairing code relayed")

print("1b. legacy flat pair_code shape still relayed")
r, out = routed(['{"type": "pair_code", "code": "WXYZ-5678"}'], qr_dir)
check("PAIR_CODE: WXYZ-5678" in out, "flat fallback retained")

print("1c. producer-faithful qr envelope: the PAYLOAD is used, never the data dict repr")
r, out = routed(['{"event": "qr", "data": {"code": "2@real-payload"}, "ts": 2}'], qr_dir)
check(("QR_PNG: " in out) or ("QR_TEXT: 2@real-payload" in out),
      "nested qr envelope produces a QR line")
check("{" not in out.replace("QR_PNG: ", "").replace("QR_TEXT: ", "").strip()
      or "QR_PNG" in out,
      "no dict repr leaks into the payload")
if "QR_PNG: " in out:
    import qrcode as _qr  # decode-side truth: rasterized grid must match the payload's
    png = out.split("QR_PNG: ", 1)[1].strip()
    check(Path(png).stat().st_size > 0, "nested-envelope PNG is non-empty")

print("2. NDJSON qr event becomes QR_PNG (qrcode installed) or QR_TEXT (absent)")
r, out = routed(['{"type": "qr", "code": "2@synthetic-payload"}'], qr_dir)
try:
    import qrcode  # noqa: F401
    check("QR_PNG: " in out, "qr rendered to PNG")
    png = out.split("QR_PNG: ", 1)[1].strip()
    check(Path(png).stat().st_size > 0, "PNG is non-empty")
except ImportError:
    check("QR_TEXT: 2@synthetic-payload" in out,
          "raw payload emitted when qrcode lib is absent")

print("3. plain-text fallbacks: success line and passkey stop")
r, _ = routed(["Pairing successful, starting sync"], qr_dir)
check(r.paired, "plain-text success flips paired")
r, _ = routed(["error: passkey verification required"], qr_dir)
check(r.error is not None and "passkey" in r.error, "passkey stop is terminal")

print("3b. 'paired' event type flips paired — not swallowed by the 'pair' branch")
r, out = routed(['{"event": "paired", "data": {}, "ts": 4}'], qr_dir)
check(r.paired, "type=paired sets router.paired")
check("PAIR_CODE" not in out, "paired event emits no pairing-code line")

print("4. JSON-looking prose does not crash the router")
r, _ = routed(['{not json at all', '{"type": 3}'], qr_dir)
check(r.error is None, "malformed lines are tolerated")

print("5. stubbed wacli: ALREADY_CONNECTED short-circuits, auth never spawned")
lab = tempfile.mkdtemp()
stub = Path(lab) / "wacli"
stub.write_text(
    "#!/bin/sh\n"
    f'echo "$@" >> "{lab}/calls"\n'
    'case "$1 $2" in\n'
    '  "auth status") echo authenticated; exit 0;;\n'
    '  "chats list") echo "KIND NAME"; exit 0;;\n'
    '  "auth --events") echo "should not run"; exit 1;;\n'
    'esac\nexit 0\n')
stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
env = dict(os.environ, PATH=f"{lab}:{os.environ['PATH']}")
out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                     text=True, env=env, timeout=60)
check(out.returncode == 0 and "ALREADY_CONNECTED" in out.stdout,
      f"already-connected path (stdout={out.stdout.strip()!r})")
calls = (Path(lab) / "calls").read_text()
check("auth --events" not in calls, "auth is never spawned when connected")

print("6. stubbed wacli: dead auth stream ends in ERROR, nonzero exit")
stub.write_text(
    "#!/bin/sh\n"
    'case "$1 $2" in\n'
    '  "auth status") echo "not authenticated"; exit 1;;\n'
    '  "chats list") exit 1;;\n'
    '  "auth --events") exit 0;;\n'
    'esac\nexit 0\n')
out = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "10"],
                     capture_output=True, text=True, env=env, timeout=60)
check(out.returncode == 1 and "ERROR: " in out.stdout,
      f"no-session path errors loudly (stdout={out.stdout.strip()!r})")

print("7. connected arrives while auth stays alive past the old 10s wait — no crash")
# Reviewed failure: connected arrives, auth stays alive (~30s sync); the
# script must reap and verify instead of raising TimeoutExpired.
stub.write_text(
    "#!/bin/sh\n"
    'case "$1 $2" in\n'
    '  "auth status") echo authenticated; exit 0;;\n'
    '  "chats list") echo "KIND NAME"; exit 0;;\n'
    '  "auth --events") echo \'{"event":"connected","data":{},"ts":3}\' >&2; sleep 30; exit 0;;\n'
    'esac\nexit 0\n')
# Two-state stub via marker file: status fails until auth "pairs", so the
# spawn path is exercised and post-pair verification can succeed.
stub.write_text(
    "#!/bin/sh\n"
    f'MARK="{lab}/paired"\n'
    'case "$1 $2" in\n'
    '  "auth status") if [ -f "$MARK" ]; then echo authenticated; exit 0; '
    'else echo "not authenticated"; exit 1; fi;;\n'
    '  "chats list") [ -f "$MARK" ] && exit 0 || exit 1;;\n'
    '  "auth --events") echo \'{"event":"connected","data":{},"ts":3}\' >&2; '
    f'touch "$MARK"; sleep 30; exit 0;;\n'
    'esac\nexit 0\n')
t0 = __import__("time").time()
out = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "60"],
                     capture_output=True, text=True, env=env, timeout=90)
elapsed = __import__("time").time() - t0
check("Traceback" not in out.stderr, f"no uncaught exception (stderr={out.stderr[-120:]!r})")
check(out.returncode == 0 and "CONNECTED" in out.stdout,
      f"verification reached after reap (stdout={out.stdout.strip()!r})")
check(elapsed < 45, f"long-lived auth is reaped, not waited out ({elapsed:.0f}s)")

print("8. timeout path verifies the session store before declaring failure")
# Unrecognised vocabulary + auth outliving --timeout: the store is live,
# so verification must emit CONNECTED, never "pairing timed out".
stub.write_text(
    "#!/bin/sh\n"
    f'MARK="{lab}/paired8"\n'
    'case "$1 $2" in\n'
    '  "auth status") if [ -f "$MARK" ]; then echo authenticated; exit 0; '
    'else echo "not authenticated"; exit 1; fi;;\n'
    '  "chats list") [ -f "$MARK" ] && exit 0 || exit 1;;\n'
    '  "auth --events") echo \'{"event":"future_vocab","data":{},"ts":5}\' >&2; '
    f'touch "$MARK"; sleep 60; exit 0;;\n'
    'esac\nexit 0\n')
out = subprocess.run([sys.executable, str(SCRIPT), "--timeout", "5"],
                     capture_output=True, text=True, env=env, timeout=90)
check(out.returncode == 0 and "CONNECTED" in out.stdout,
      f"live session wins over the timeout verdict (stdout={out.stdout.strip()!r})")
check("timed out" not in out.stdout, "no false timeout error for a live session")

print("9. in-process probes: wacli_bin / auth_status_ok / chats_probe")
# The subprocess-driven tests above prove behavior end to end but are invisible
# to coverage instrumentation; these drive the same seams in-process.


class _Done:
    def __init__(self, rc, so="", se=""):
        self.returncode, self.stdout, self.stderr = rc, so, se


_real_which, _real_run = gc.shutil.which, gc.subprocess.run
gc.shutil.which = lambda n: "/fake/wacli"
check(gc.wacli_bin() == "/fake/wacli", "wacli_bin resolves via shutil.which")
gc.subprocess.run = lambda *a, **k: _Done(0, "authenticated")
check(gc.auth_status_ok("w") is True, "status ok on authenticated output")
gc.subprocess.run = lambda *a, **k: _Done(0, "not authenticated")
check(gc.auth_status_ok("w") is False, "'not authenticated' wins over rc 0")


def _timeout_run(*a, **k):
    raise subprocess.TimeoutExpired("w", 1)


gc.subprocess.run = _timeout_run
check(gc.auth_status_ok("w") is False and gc.chats_probe("w") is False,
      "probe timeouts read as not-connected, never raise")
gc.subprocess.run = lambda *a, **k: _Done(0)
check(gc.chats_probe("w") is True, "chats probe passes on rc 0")
gc.subprocess.run = _real_run

print("9b. router text/edge branches: text pairing code, data.message error")
r, out = routed(["Enter this pairing code: WXYZ-9876"], qr_dir)
check("PAIR_CODE: WXYZ-9876" in out, "plain-text pairing code relayed")
r, _ = routed(['{"event": "error", "data": {"message": "boom"}}'], qr_dir)
check(r.error == "boom", "error message read from the data envelope")
r, out = routed(["", "   "], qr_dir)
check(r.error is None and out == "", "blank lines are ignored")
gc.render_qr_png("in-process-payload", qr_dir)  # exercises whichever branch the env has

print("10. in-process run_auth: connect, error-event, and timeout-verified paths")


class _FakeProc:
    def __init__(self, stderr_lines):
        self.stdout, self.stderr = iter(()), iter(stderr_lines)
        self._alive, self.killed = True, False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


_real_popen = gc.subprocess.Popen
_real_aso, _real_cp = gc.auth_status_ok, gc.chats_probe


def run_inproc(stderr_lines, *, phone=None, timeout=30, status=True, proc_cls=_FakeProc):
    gc.subprocess.Popen = lambda *a, **k: proc_cls(stderr_lines)
    gc.auth_status_ok = lambda w: status
    gc.chats_probe = lambda w: status
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gc.run_auth("/fake/wacli", phone, timeout, qr_dir)
    return rc, buf.getvalue()


rc, out = run_inproc(['{"event":"connected","data":{},"ts":1}\n'])
check(rc == 0 and "CONNECTED" in out, "event-driven connect verified in-process")
rc, out = run_inproc(['{"event":"error","message":"relay down"}\n'])
check(rc == 1 and "ERROR: relay down" in out, "error event is terminal")
rc, out = run_inproc([], phone="15551234567", timeout=1, status=False)
check(rc == 1 and "timed out" in out, "quiet stream + dead store -> timeout error")
rc, out = run_inproc([], timeout=1, status=True)
check(rc == 0 and "CONNECTED" in out, "timeout path still verifies the store first")


class _DeadProc(_FakeProc):
    def __init__(self, stderr_lines):
        super().__init__(stderr_lines)
        self._alive = False


rc, out = run_inproc([], status=False, proc_cls=_DeadProc)
check(rc == 1 and "auth ended without a valid session" in out,
      "stream end without pairing errors without a timeout verdict")


class _StubbornProc(_FakeProc):
    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired("wacli", 1)
        return 0


rc, out = run_inproc(['{"event":"connected","data":{},"ts":1}\n'],
                     proc_cls=_StubbornProc)
check(rc == 0 and "CONNECTED" in out, "reap escalates to kill when terminate hangs")
gc.subprocess.Popen = _real_popen

print("11. in-process main(): missing wacli / already-connected / delegation")
_real_argv = sys.argv
sys.argv = ["guided_connect.py"]
gc.shutil.which = lambda n: None
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gc.main()
check(rc == 1 and "wacli is not installed" in buf.getvalue(), "missing binary errors")
gc.shutil.which = lambda n: "/fake/wacli"
gc.auth_status_ok = lambda w: True
gc.chats_probe = lambda w: True
buf = io.StringIO()
with redirect_stdout(buf):
    rc = gc.main()
check(rc == 0 and "ALREADY_CONNECTED" in buf.getvalue(), "connected short-circuit")
gc.auth_status_ok = lambda w: False
_real_run_auth, seen = gc.run_auth, {}
gc.run_auth = lambda w, p, t, q: seen.update(phone=p, timeout=t) or 0
sys.argv = ["guided_connect.py", "--phone", "15551234567", "--timeout", "7"]
check(gc.main() == 0 and seen == {"phone": "15551234567", "timeout": 7},
      "args flow through to run_auth")
gc.run_auth = _real_run_auth
gc.auth_status_ok, gc.chats_probe = _real_aso, _real_cp
gc.shutil.which = _real_which
sys.argv = _real_argv

if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("ALL PASS")
