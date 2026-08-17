"""Decision logic for src/phone-watchdog.sh, pinned hermetically via HEALTH_URL
and DRY_RUN. Real recovery and launchd wiring need a live host, not this."""

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

# startup.sh only starts what `pgrep` cannot find; a wedged resident made
# recovery a silent no-op.
_wedge_port = 0
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
_wedge_port = _s.getsockname()[1]
_s.close()

# Named so the process's OWN argv identifies it as the phone stack. A bare
# `python3 -c` holder tested the blast radius, not the recovery.
_fixture_dir = tempfile.mkdtemp(prefix="phone-watchdog-")
_owned_script = os.path.join(_fixture_dir, "conversation-server-fixture.py")
_alien_script = os.path.join(_fixture_dir, "unrelated-service.py")
_HOLDER_SRC = (
    "import socket,sys,time\n"
    "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "s.bind(('127.0.0.1',int(sys.argv[1]))); s.listen(1)\n"
    "time.sleep(120)\n"
)
for _p in (_owned_script, _alien_script):
    with open(_p, "w") as _f:
        _f.write(_HOLDER_SRC)

_holder = subprocess.Popen([sys.executable, _owned_script, str(_wedge_port)])
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
    ok("a wedged listener the phone stack OWNS is stopped, so recovery is not a no-op")
else:
    fail("wedged listener survived recovery", f"still listening: {_still!r}")

# Paired negative control. Without it the positive case above is satisfied by
# "kill whatever holds the port", which is the defect and not the fix.
_alien_port = 0
_s2 = socket.socket()
_s2.bind(("127.0.0.1", 0))
_alien_port = _s2.getsockname()[1]
_s2.close()

_alien = subprocess.Popen([sys.executable, _alien_script, str(_alien_port)])
time.sleep(1.0)
_alien_held = subprocess.run(
    ["lsof", "-nP", "-iTCP:%d" % _alien_port, "-sTCP:LISTEN", "-t"],
    capture_output=True, text=True,
).stdout.split()
if str(_alien.pid) not in _alien_held:
    fail("fixture: unrelated listener not detected", f"lsof={_alien_held!r}")
else:
    _r2 = run({
        "HEALTH_URL": "http://127.0.0.1:1/health",   # same unreachable probe
        "PHONE_PORT": str(_alien_port),
        "NGROK_API_PORT": str(_alien_port),
        "RECOVER_CMD": "echo RECOVERED",
    })
    time.sleep(1.0)
    _alien_after = subprocess.run(
        ["lsof", "-nP", "-iTCP:%d" % _alien_port, "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True,
    ).stdout.split()
    if str(_alien.pid) in _alien_after:
        ok("an unrelated listener SURVIVES a public-health failure")
    else:
        fail("unrelated listener was killed",
             "port occupancy is not authorization; a WAN/tunnel outage would "
             "take down someone else's process")
    if "not the phone stack" in _r2.stderr:
        ok("the watchdog says why it declined to signal")
    else:
        fail("no decline diagnostic", f"stderr={_r2.stderr[:200]!r}")
    _alien.kill()
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


# ── Test 6: the opt-in is discoverable from README, both directions ───────────
# An installer nothing documents is unowned, and this one restarts processes.
def _readme_documents(readme, needle):
    return any(needle in line for line in readme.splitlines())


try:
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        _readme = f.read()
    _install = "bash src/install-phone-watchdog-launchd.sh"
    _uninstall = "bash src/install-phone-watchdog-launchd.sh --uninstall"
    _missing = [
        label
        for label, needle in (("install", _install), ("uninstall", _uninstall))
        if not _readme_documents(_readme, needle)
    ]
    if _missing:
        fail("README documents the watchdog opt-in", f"missing: {', '.join(_missing)}")
    else:
        ok("README documents the watchdog install and uninstall commands")
except Exception as e:
    fail("README adoption-path read", str(e))


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
