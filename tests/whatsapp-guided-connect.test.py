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

print("1. NDJSON pairing-code event becomes a PAIR_CODE line")
r, out = routed(['{"type": "pair_code", "code": "ABCD-1234"}'], qr_dir)
check("PAIR_CODE: ABCD-1234" in out, "pairing code relayed")

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

if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("ALL PASS")
