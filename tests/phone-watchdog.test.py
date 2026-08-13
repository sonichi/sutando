"""
Tests for the phone-stack watchdog:
  - src/phone-watchdog.sh (health probe + recovery decision)
  - src/launchd/com.sutando.phone-watchdog.plist (well-formed template)

Discovered by CI's Python test runner alongside other *.test.py files.

The watchdog's real recovery (re-running startup.sh) and the launchd wiring are
NOT exercised here — they need a live phone stack + macOS launchd. These tests
pin the decision logic hermetically via HEALTH_URL (probe target) and DRY_RUN
(print the recovery action instead of running it), so a regression in "when does
it recover / what does it run" is caught without any real stack.
"""

import http.server
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import socket
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WD = os.path.join(REPO, "src", "phone-watchdog.sh")
PLIST = os.path.join(REPO, "src", "launchd", "com.sutando.phone-watchdog.plist")

_pass = 0
_fail = 0


def ok(label):
    global _pass
    print(f"  PASS: {label}")
    _pass += 1


def fail(label, detail=""):
    global _fail
    print(f"  FAIL: {label}{' — ' + detail if detail else ''}", file=sys.stderr)
    _fail += 1


def run(env_extra, cwd=REPO, script=WD):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", script], capture_output=True, text=True, env=env, timeout=30
    )


class _OK(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


# ── Test 1: healthy public /health → exit 0, no recovery ──────────────────────
srv = http.server.HTTPServer(("127.0.0.1", 0), _OK)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
try:
    r = run({"HEALTH_URL": f"http://127.0.0.1:{port}/health", "DRY_RUN": "1"})
    if r.returncode == 0 and "would run" not in r.stdout:
        ok("healthy /health → exit 0, no recovery")
    else:
        fail("healthy path", f"rc={r.returncode} out={r.stdout[:120]!r}")
finally:
    srv.shutdown()

# ── Test 2: unreachable /health + DRY_RUN → prints recovery, exit 0 ───────────
# Port 1 is not listening → curl fails → watchdog decides to recover.
r = run({"HEALTH_URL": "http://127.0.0.1:1/health", "DRY_RUN": "1"})
if (r.returncode == 0 and "then run:" in r.stdout and "startup.sh" in r.stdout
        and "would stop listeners" in r.stdout):
    ok("unreachable /health → recovery decided (DRY_RUN), default = startup.sh")
else:
    fail("unhealthy path", f"rc={r.returncode} out={r.stdout[:160]!r}")

# ── Test: a WEDGED-but-resident stack is actually stopped before recovery ─────
# The defect this covers: startup.sh starts each service only when `pgrep` finds
# none, so a resident-but-unhealthy process made recovery a silent no-op.
def _wedged_listener(port):
    """A process that HOLDS the port and never answers /health — the exact state
    `pgrep` calls 'already running' and the public probe calls dead."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    return srv


_wedge_port = 0
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
_wedge_port = _s.getsockname()[1]
_s.close()

_holder = subprocess.Popen(
    [sys.executable, "-c",
     "import socket,time,sys\n"
     "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
     f"s.bind(('127.0.0.1',{_wedge_port})); s.listen(1)\n"
     "time.sleep(120)\n"],
)
time.sleep(1.0)

_held = subprocess.run(
    ["lsof", "-nP", "-iTCP:%d" % _wedge_port, "-sTCP:LISTEN", "-t"],
    capture_output=True, text=True,
).stdout.split()
if str(_holder.pid) in _held:
    ok("fixture: a resident process is holding the port")
else:
    fail("fixture: port holder not detected", f"lsof={_held!r} pid={_holder.pid}")

_r = run({
    "HEALTH_URL": "http://127.0.0.1:1/health",   # unreachable => unhealthy
    "PHONE_PORT": str(_wedge_port),
    "NGROK_API_PORT": str(_wedge_port),
    "RECOVER_CMD": "echo RECOVERED",
})
time.sleep(1.0)
_still = subprocess.run(
    ["lsof", "-nP", "-iTCP:%d" % _wedge_port, "-sTCP:LISTEN", "-t"],
    capture_output=True, text=True,
).stdout.split()
if not _still:
    ok("a wedged listener is stopped, so recovery is not a silent no-op")
else:
    fail("wedged listener survived recovery", f"still listening: {_still!r}")
if "RECOVERED" in _r.stdout:
    ok("recovery still runs after the stack is freed")
else:
    fail("recovery did not run", f"out={_r.stdout[:160]!r}")
try:
    _holder.kill()
except OSError:
    pass


# ── Test 3: RECOVER_CMD override is honored ───────────────────────────────────
r = run({
    "HEALTH_URL": "http://127.0.0.1:1/health",
    "DRY_RUN": "1",
    "RECOVER_CMD": "echo CUSTOM_RECOVERY",
})
if r.returncode == 0 and "CUSTOM_RECOVERY" in r.stdout:
    ok("RECOVER_CMD override honored")
else:
    fail("recover override", f"rc={r.returncode} out={r.stdout[:160]!r}")

# ── Test 4: no webhook configured (no .env) → silent no-op, exit 0 ────────────
with tempfile.TemporaryDirectory() as fake:
    os.makedirs(os.path.join(fake, "src"))
    shutil.copy(WD, os.path.join(fake, "src", "phone-watchdog.sh"))
    # no .env in `fake` → WEBHOOK_BASE_URL unresolved → nothing to supervise.
    # Unset any inherited HEALTH_URL so the .env path is what's exercised.
    env = {k: v for k, v in os.environ.items() if k != "HEALTH_URL"}
    r = subprocess.run(
        ["bash", os.path.join(fake, "src", "phone-watchdog.sh")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    if r.returncode == 0 and r.stdout.strip() == "":
        ok("no webhook configured → silent no-op, exit 0")
    else:
        fail("no-webhook no-op", f"rc={r.returncode} out={r.stdout[:120]!r}")

# ── Test 5: launchd plist template is well-formed + correctly shaped ──────────
try:
    with open(PLIST, "rb") as f:
        # __TOKENS__ are literal-safe for the XML parse (they sit inside strings)
        data = plistlib.load(f)
    if (
        data.get("Label") == "com.sutando.phone-watchdog"
        and data.get("StartInterval") == 120
        and any("phone-watchdog.sh" in a for a in data.get("ProgramArguments", []))
    ):
        ok("plist template well-formed (label, 120s interval, runs the script)")
    else:
        fail("plist shape", f"{data!r}")
except Exception as e:
    fail("plist parse", str(e))


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
