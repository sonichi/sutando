#!/usr/bin/env python3
"""Signal Room guest lane — the Claude-worker containment contract.

These are the assertions the design (dev_docs/design-signal-room-core-capability.md)
assigns to Sutando: the desktop consumes only the API contract, so every claim about
HOW a guest task is contained has to be proven here.

Run: python3 tests/signal-guest-claude-worker.test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import signal_guest_handler as H  # noqa: E402
import signal_guest_profile as P  # noqa: E402

FAILS = []


def ck(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


print("== argv pins the containment boundary ==")
argv = H.guest_argv("PROMPT", "/tmp/wd")
ck(argv[0] == "claude" and "-p" in argv, "spawns the Claude CLI in print mode")
ck("--tools" in argv, "uses the SURFACE-restricting --tools switch")
ck("--allowedTools" not in argv and "--allowed-tools" not in argv,
   "does NOT use --allowedTools (pre-approval would leave Read present)")
tools = argv[argv.index("--tools") + 1]
ck(tools == "WebSearch", f"surface is exactly WebSearch (got {tools!r})")
ck("WebFetch" not in tools, "WebFetch excluded (loopback/LAN SSRF would reopen local reads)")
for denied in ("Bash", "Read", "Write", "Edit"):
    ck(denied not in tools, f"{denied} absent from the tool surface")
ck("--strict-mcp-config" in argv and "--mcp-config" not in argv,
   "MCP explicitly denied (strict, with no config supplied)")
ck("--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == "",
   "user/project/local settings sources dropped")
ck("--dangerously-skip-permissions" not in argv and "--allow-dangerously-skip-permissions" not in argv,
   "never skips permissions")

print("== availability is fail-closed, with machine-readable reasons ==")
ck(H.worker_cli_supports_tool_restriction("only --allowedTools here") is False,
   "a CLI without --tools/--strict-mcp-config is unsupported")
ck(H.worker_cli_supports_tool_restriction("--tools x --strict-mcp-config y") is True,
   "a CLI advertising both switches is supported")

_real_which = H.shutil.which
H.shutil.which = lambda name: None
try:
    ok, reason = H.guest_availability()
    ck(ok is False and reason == "worker_missing", "no claude binary -> worker_missing")
finally:
    H.shutil.which = _real_which

_real_managed = H.managed_policy_present
H.managed_policy_present = lambda: True
_real_supports = H.worker_cli_supports_tool_restriction
H.worker_cli_supports_tool_restriction = lambda help_text=None: True
H.shutil.which = lambda name: "/usr/local/bin/claude"
try:
    ok, reason = H.guest_availability()
    ck(ok is False and reason == "managed_policy_present",
       "managed settings present -> unavailable (managed hooks can run local commands)")
finally:
    H.managed_policy_present = _real_managed
    H.worker_cli_supports_tool_restriction = _real_supports
    H.shutil.which = _real_which

print("== guest profile: allowlist reconstruction, not a copy ==")
with tempfile.TemporaryDirectory() as td:
    owner = Path(td) / "owner"
    (owner / ".claude").mkdir(parents=True)
    # An owner .claude.json carrying exactly the things that must NOT travel.
    (owner / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "acct-123"},
        "userID": "u-1",
        "mcpServers": {"evil": {"command": "nc"}},
        "projects": {"/secret/path": {"history": ["private"]}},
        "hooks": {"PreToolUse": "curl evil.example"},
        "plugins": {"p": {}},
    }))
    (owner / ".claude" / ".credentials.json").write_text('{"token":"OWNER-SECRET"}')
    guest = Path(td) / "guest"
    os.environ["CLAUDE_CONFIG_DIR"] = str(owner / ".claude")
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(guest)
    P.invalidate_readiness_cache()

    ready, reason = P.ensure_guest_profile()
    ck(ready is True and reason is None, f"provisioning succeeds ({reason})")
    gj = json.loads((guest / ".claude.json").read_text())
    ck("oauthAccount" in gj, "account metadata carried (the CLI needs it)")
    for leaked in ("mcpServers", "projects", "hooks", "plugins"):
        ck(leaked not in gj, f"{leaked} NOT carried into the guest profile")
    ck((guest / ".credentials.json").read_text() == '{"token":"OWNER-SECRET"}',
       "file-backed credential copied for the guest")
    mode = (guest / ".credentials.json").stat().st_mode & 0o777
    ck(mode == 0o600, f"credential written 0600 (got {oct(mode)})")
    ck((guest.stat().st_mode & 0o777) == 0o700, "guest home is 0700")

    print("== negative synchronization: a guest copy never outlives the owner session ==")
    (owner / ".claude.json").unlink()
    P.invalidate_readiness_cache()
    ready, reason = P.ensure_guest_profile()
    ck(ready is False and reason == "worker_unauthenticated",
       "owner logout/removal -> worker_unauthenticated")
    ck(not (guest / ".credentials.json").exists(),
       "copied guest credential DELETED when the owner source disappears")

    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)

print("== worker reaping on shutdown ==")
ck(hasattr(H, "reap_guest_workers"), "reaper exists for the SIGTERM path")
H._track(999999123)  # a pgid that does not exist: killpg fails, reaper must not raise
ck(H.reap_guest_workers() == 1, "reaper signals tracked groups and clears them")
ck(H.reap_guest_workers() == 0, "reaper is idempotent once drained")

print("== results are always terminal payloads ==")
with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    _which = H.shutil.which
    H.shutil.which = lambda name: None
    try:
        H.start_guest_deep_dive("signal-guest-t1", "hello", rd, lambda x: x)
    finally:
        H.shutil.which = _which
    body = (rd / "signal-guest-t1.txt").read_text()
    ck(body.startswith("[deep_dive "), f"unavailable writes a canonical failure payload: {body[:60]!r}")
    ck("worker_missing" in body, "the payload names the machine-readable reason")

print("== B: managed policy is detected broadly and fails closed ==")
import tempfile as _tf
_real_stat, _real_listdir = os.stat, os.listdir


def _stat_hit(hit):
    """os.stat that reports exactly `hit` as existing, everything else absent."""
    def fake(path, *a, **k):
        if str(path) == hit:
            return _real_stat(__file__)  # any real stat result stands in
        raise FileNotFoundError(path)
    return fake


for label, hit in (
    ("macOS managed-settings.json", "/Library/Application Support/ClaudeCode/managed-settings.json"),
    ("managed-mcp.json", "/etc/claude-code/managed-mcp.json"),
    ("MDM plist", "/Library/Managed Preferences/com.anthropic.claudecode.plist"),
    ("Windows Program Files policy", "C:\\Program Files\\ClaudeCode\\managed-settings.json"),
):
    os.stat = _stat_hit(hit)
    try:
        ck(H.managed_policy_present() is True, f"detects {label}")
    finally:
        os.stat = _real_stat

# A drop-in policy directory with any *.json in it.
_dir = "/etc/claude-code/managed-settings.d"
os.stat = _stat_hit(_dir)
os.listdir = lambda p: ["policy.json"] if str(p) == _dir else _real_listdir(p)
try:
    ck(H.managed_policy_present() is True, "detects a drop-in managed-settings.d/*.json")
finally:
    os.stat, os.listdir = _real_stat, _real_listdir

# THE FAIL-OPEN CASE the previous implementation got wrong: os.path.exists() swallows EACCES and
# returns False, so an UNREADABLE managed policy read as absent.
def _stat_denied(path, *a, **k):
    raise PermissionError(13, "Permission denied", str(path))


os.stat = _stat_denied
try:
    ck(H.managed_policy_present() is True,
       "an UNREADABLE policy path counts as present (os.path.exists would have said absent)")
finally:
    os.stat = _real_stat

# And the real filesystem behavior that motivated it, exercised end to end.
with _tf.TemporaryDirectory() as _td:
    locked = Path(_td) / "locked"
    locked.mkdir()
    (locked / "managed-settings.json").write_text("{}")
    os.chmod(locked, 0o000)
    try:
        target = str(locked / "managed-settings.json")
        ck(os.path.exists(target) is False,
           "baseline: os.path.exists() reports an unreadable policy as ABSENT (why it was unsafe)")
        ck(H._path_state(target) == "unknown",
           "_path_state() reports it as unknown -> treated as present (fail-closed)")
    finally:
        os.chmod(locked, 0o700)

print("== P1: shutdown terminalizes accepted work, and a late result cannot clobber it ==")
with _tf.TemporaryDirectory() as td:
    rd = Path(td)
    H._reset_stopping_for_tests()
    with H._live_lock:
        H._live_tasks["signal-guest-inflight"] = rd
    H.reap_guest_workers()
    body = (rd / "signal-guest-inflight.txt").read_text()
    ck(body.startswith("[deep_dive cancelled"),
       f"in-flight task terminalized on shutdown (no permanent 404): {body[:40]!r}")
    # A late publish for the same id must NOT overwrite the cancellation notice.
    with H._live_lock:
        already_claimed = H._live_tasks.pop("signal-guest-inflight", None) is None
    ck(already_claimed, "shutdown claimed the task, so a late worker publish is dropped")

print("== P1: the stopping latch closes the spawn->track race ==")
H._reset_stopping_for_tests()
ck(H._track(4242) is True, "tracking works before shutdown")
H._untrack(4242)
H.reap_guest_workers()  # latches _stopping
ck(H._track(4243) is False, "a worker spawned during shutdown is refused registration")
H._reset_stopping_for_tests()

print("== P1: profile hardening (symlink refusal, mode repair, purge failure) ==")
with _tf.TemporaryDirectory() as td:
    owner = Path(td) / "owner"
    (owner / ".claude").mkdir(parents=True)
    (owner / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "a"}}))
    (owner / ".claude" / ".credentials.json").write_text('{"t":1}')
    os.environ["CLAUDE_CONFIG_DIR"] = str(owner / ".claude")

    # a symlinked guest home must be refused, never written through
    linked = Path(td) / "linked-home"
    target = Path(td) / "elsewhere"
    target.mkdir()
    linked.symlink_to(target)
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(linked)
    P.invalidate_readiness_cache()
    ready, reason = P.ensure_guest_profile()
    ck(ready is False and reason == "guest_profile_missing",
       "a symlinked guest home is refused (no credential write through a planted link)")

    # mode repair even when content is unchanged
    real = Path(td) / "real-home"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(real)
    P.invalidate_readiness_cache()
    P.ensure_guest_profile()
    creds = real / ".credentials.json"
    os.chmod(creds, 0o644)
    P.invalidate_readiness_cache()
    P.ensure_guest_profile()
    ck((creds.stat().st_mode & 0o777) == 0o600,
       "a content-identical credential left world-readable is repaired to 0600")

    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)

print("== P1: negative readiness is not cached for the positive TTL ==")
ck(P._NEG_CACHE_TTL_S < P._CACHE_TTL_S,
   "an unavailable verdict expires fast so a fresh login is seen promptly")


print("== the spawn path: env, argv, timeout kill, guarded publish ==")
import subprocess as _sp
import types as _types

with _tf.TemporaryDirectory() as _td:
    rd = Path(_td) / "results"
    rd.mkdir()
    home = Path(_td) / "home"
    home.mkdir()
    seen = {}

    class _FakeProc:
        """Stands in for the worker: records argv/env, returns a canned answer."""
        def __init__(self, out="Findings: forty-two.\n", rc=0, timeout=False):
            self._out, self.returncode, self._timeout = out, rc, timeout
            self.pid = os.getpid()  # a real, signalable pid for pgid lookups
            self.killed = False

        def communicate(self, timeout=None):
            if self._timeout:
                raise _sp.TimeoutExpired(cmd="claude", timeout=timeout or 0)
            return self._out, ""

        def kill(self):
            self.killed = True

    def _fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env", {})
        seen["cwd"] = kw.get("cwd")
        seen["new_session"] = kw.get("start_new_session")
        seen["stdin"] = kw.get("stdin")
        return seen.get("proc") or _FakeProc()

    _real_popen, _real_guard = H.subprocess.Popen, H._guard
    H.subprocess.Popen = _fake_popen
    H._guard = lambda body: f"GUARDED::{body}"
    _real_killpg = os.killpg
    os.killpg = lambda *a, **k: None  # never signal the test runner's own group
    try:
        H._reset_stopping_for_tests()
        with H._live_lock:
            H._live_tasks["signal-guest-spawn"] = rd
        H._slots.acquire()
        H._run("signal-guest-spawn", "  a question  ", rd, lambda t: f"CONFINED({t})", str(home))

        ck(seen["argv"][:2] == ["claude", "-p"], "spawns claude -p")
        ck("CONFINED(" in seen["argv"][2], "the untrusted body is defanged before it reaches the prompt")
        ck(seen["env"].get("CLAUDE_CONFIG_DIR") == str(home) and seen["env"].get("HOME") == str(home),
           "config root and HOME are pinned to the isolated profile")
        ck("ANTHROPIC_API_KEY" not in seen["env"] and "SUTANDO_API_TOKEN" not in seen["env"],
           "the owner's environment does not reach the worker (allowlist)")
        ck(seen["new_session"] is True, "worker runs in its own session/process group")
        ck(seen["stdin"] == _sp.DEVNULL, "stdin is closed to the worker")
        ck(seen["cwd"] != os.getcwd() and str(seen["cwd"]).startswith(_tf.gettempdir()),
           "worker cwd is a throwaway tmp dir, not the repo")
        body = (rd / "signal-guest-spawn.txt").read_text()
        ck(body.startswith("GUARDED::"), "worker output goes through the egress guard before publication")
        ck("forty-two" in body, "the guarded answer is what gets published")

        # A timeout kills the group and still publishes a terminal payload.
        seen["proc"] = _FakeProc(timeout=True)
        with H._live_lock:
            H._live_tasks["signal-guest-timeout"] = rd
        H._slots.acquire()
        H._run("signal-guest-timeout", "slow", rd, lambda t: t, str(home))
        tbody = (rd / "signal-guest-timeout.txt").read_text()
        ck(tbody.startswith("[deep_dive "), f"a timed-out worker still publishes a terminal payload: {tbody[:40]!r}")

        # A non-zero exit publishes the no-result payload rather than partial output.
        seen["proc"] = _FakeProc(out="partial junk", rc=3)
        with H._live_lock:
            H._live_tasks["signal-guest-rc"] = rd
        H._slots.acquire()
        H._run("signal-guest-rc", "boom", rd, lambda t: t, str(home))
        rbody = (rd / "signal-guest-rc.txt").read_text()
        ck("partial junk" not in rbody, "a failed worker's stdout is never published")
    finally:
        H.subprocess.Popen, H._guard = _real_popen, _real_guard
        os.killpg = _real_killpg

print("== fleet bound and availability short-circuit ==")
with _tf.TemporaryDirectory() as _td:
    rd = Path(_td)
    _real_avail = H.guest_availability
    H.guest_availability = lambda *a, **k: (True, None)
    _acquired = []
    try:
        # Exhaust the fleet, then a further submission must answer "busy", not queue.
        for _ in range(H.MAX_CONCURRENT):
            _acquired.append(H._slots.acquire(blocking=False))
        H.start_guest_deep_dive("signal-guest-busy", "q", rd, lambda t: t)
        bbody = (rd / "signal-guest-busy.txt").read_text()
        ck("busy" in bbody, f"a full fleet answers busy rather than spawning: {bbody[:40]!r}")
    finally:
        for _ in _acquired:
            try:
                H._slots.release()
            except Exception:
                pass
        H.guest_availability = _real_avail

print("== profile: provisioning failure paths ==")
with _tf.TemporaryDirectory() as _td:
    owner = Path(_td) / "owner"
    (owner / ".claude").mkdir(parents=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(owner / ".claude")
    # A non-object .claude.json is unauthenticated, not a crash.
    (owner / ".claude.json").write_text('["not", "an", "object"]')
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(Path(_td) / "g1")
    P.invalidate_readiness_cache()
    ok, reason = P.ensure_guest_profile()
    ck(ok is False and reason == "worker_unauthenticated", "a non-object .claude.json -> unauthenticated")

    # Unreadable owner credential -> purge + unauthenticated (never a stale copy).
    (owner / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "a"}}))
    creds = owner / ".claude" / ".credentials.json"
    creds.write_text('{"t":1}')
    gh_dir = Path(_td) / "g2"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh_dir)
    P.invalidate_readiness_cache()
    P.ensure_guest_profile()
    ck((gh_dir / ".credentials.json").exists(), "precondition: the guest copy exists")
    os.chmod(creds, 0o000)
    try:
        P.invalidate_readiness_cache()
        ok, reason = P.ensure_guest_profile()
        ck(ok is False, f"an unreadable owner credential is not ready ({reason})")
        ck(not (gh_dir / ".credentials.json").exists(),
           "the guest copy is purged when the owner credential cannot be read")
    finally:
        os.chmod(creds, 0o600)

    # Keyring-style owner (no credential file): still ready, and any stale copy goes.
    creds.unlink()
    gh3 = Path(_td) / "g3"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh3)
    P.invalidate_readiness_cache()
    ok, reason = P.ensure_guest_profile()
    ck(ok is True, f"keyring-style auth (no credential file) is ready ({reason})")
    ck(not (gh3 / ".credentials.json").exists(), "no credential file is fabricated for keyring auth")
    ck(P.guest_env_overrides(gh3)["CLAUDE_CONFIG_DIR"] == str(gh3), "env overrides pin the profile")

    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)

print("== detection helpers: every branch ==")
_real_stat2 = os.stat
# _path_state: absent / present / unknown
os.stat = lambda p, *a, **k: (_ for _ in ()).throw(FileNotFoundError(p))
try:
    ck(H._path_state("/nope") == "absent", "_path_state: FileNotFoundError -> absent")
finally:
    os.stat = _real_stat2
os.stat = lambda p, *a, **k: (_ for _ in ()).throw(NotADirectoryError(p))
try:
    ck(H._path_state("/x/y") == "absent", "_path_state: NotADirectoryError -> absent")
finally:
    os.stat = _real_stat2
ck(H._path_state(__file__) == "present", "_path_state: a real file -> present")

# managed dir that exists but cannot be listed -> present (fail-closed)
_dir2 = "/etc/claude-code/managed-settings.d"
_real_listdir2 = os.listdir
os.stat = lambda p, *a, **k: (_real_stat2(__file__) if str(p) == _dir2
                              else (_ for _ in ()).throw(FileNotFoundError(p)))
os.listdir = lambda p: (_ for _ in ()).throw(PermissionError("nope"))
try:
    ck(H.managed_policy_present() is True, "an unlistable policy dir counts as present")
finally:
    os.stat, os.listdir = _real_stat2, _real_listdir2

# a policy dir with no *.json is NOT policy
os.stat = lambda p, *a, **k: (_real_stat2(__file__) if str(p) == _dir2
                              else (_ for _ in ()).throw(FileNotFoundError(p)))
os.listdir = lambda p: ["README.md"]
try:
    ck(H.managed_policy_present() is False, "a policy dir with no *.json is not managed policy")
finally:
    os.stat, os.listdir = _real_stat2, _real_listdir2

# Windows registry probe: absent winreg (non-Windows) is not policy
ck(H._windows_policy_present() is False, "no winreg module (non-Windows) -> no registry policy")

# CLI feature check consults the real binary when no help text is supplied
_real_run = H.subprocess.run
H.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no claude"))
try:
    ck(H.worker_cli_supports_tool_restriction() is False,
       "an unrunnable CLI is treated as unsupported (fail-closed)")
finally:
    H.subprocess.run = _real_run

H.subprocess.run = lambda *a, **k: _types.SimpleNamespace(stdout="--tools x --strict-mcp-config y")
try:
    ck(H.worker_cli_supports_tool_restriction() is True, "a capable CLI is detected via --help")
finally:
    H.subprocess.run = _real_run

print("== availability: the full decision ladder ==")
_wh, _cli, _mp = H.shutil.which, H.worker_cli_supports_tool_restriction, H.managed_policy_present
H.shutil.which = lambda n: "/usr/local/bin/claude"
H.worker_cli_supports_tool_restriction = lambda help_text=None: False
try:
    ck(H.guest_availability() == (False, "worker_unsupported_cli"),
       "a CLI that cannot express the restriction -> worker_unsupported_cli")
finally:
    H.worker_cli_supports_tool_restriction = _cli

H.worker_cli_supports_tool_restriction = lambda help_text=None: True
H.managed_policy_present = lambda: False
import signal_guest_profile as _P2
_real_ready = _P2.guest_profile_ready
_P2.guest_profile_ready = lambda *a, **k: (False, "worker_unauthenticated")
try:
    ck(H.guest_availability() == (False, "worker_unauthenticated"),
       "an unready profile surfaces its own reason")
    _P2.guest_profile_ready = lambda *a, **k: (False, None)
    ck(H.guest_availability() == (False, "guest_profile_missing"),
       "a reasonless failure falls back to guest_profile_missing")
    _P2.guest_profile_ready = lambda *a, **k: (True, None)
    ck(H.guest_availability() == (True, None), "everything ready -> available")
    ck(H.worker_available() is True, "worker_available mirrors the ladder")
finally:
    _P2.guest_profile_ready = _real_ready
    H.shutil.which, H.worker_cli_supports_tool_restriction, H.managed_policy_present = _wh, _cli, _mp

print("== spawn: shutdown mid-spawn, and an unspawnable worker ==")
with _tf.TemporaryDirectory() as _td:
    rd = Path(_td)
    _real_popen2 = H.subprocess.Popen

    class _P:
        pid = os.getpid()
        returncode = 0

        def communicate(self, timeout=None):
            return "unused", ""

        def kill(self):
            pass

    _real_killpg2 = os.killpg
    os.killpg = lambda *a, **k: None
    H.subprocess.Popen = lambda *a, **k: _P()
    try:
        # Shutdown latched: the just-spawned worker is killed, not registered.
        H.reap_guest_workers()  # latch _stopping
        with H._live_lock:
            H._live_tasks["signal-guest-midspawn"] = rd
        H._slots.acquire()
        H._run("signal-guest-midspawn", "q", rd, lambda t: t, str(rd))
        mbody = (rd / "signal-guest-midspawn.txt").read_text()
        ck(mbody.startswith("[deep_dive "),
           f"a worker spawned during shutdown still yields a terminal payload: {mbody[:38]!r}")
        H._reset_stopping_for_tests()

        # Popen itself fails -> terminal payload, no hang.
        H.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("exec failed"))
        with H._live_lock:
            H._live_tasks["signal-guest-nospawn"] = rd
        H._slots.acquire()
        H._run("signal-guest-nospawn", "q", rd, lambda t: t, str(rd))
        ck((rd / "signal-guest-nospawn.txt").read_text().startswith("[deep_dive "),
           "an unspawnable worker still publishes a terminal payload")
    finally:
        H.subprocess.Popen = _real_popen2
        os.killpg = _real_killpg2

print("== start_guest_deep_dive: unavailable and shutting-down entry paths ==")
with _tf.TemporaryDirectory() as _td:
    rd = Path(_td)
    _av = H.guest_availability
    H.guest_availability = lambda *a, **k: (True, None)
    try:
        H.reap_guest_workers()  # latch shutdown
        H.start_guest_deep_dive("signal-guest-shutdown", "q", rd, lambda t: t)
        sbody = (rd / "signal-guest-shutdown.txt").read_text()
        ck("shutting down" in sbody, f"submissions during shutdown are refused terminally: {sbody[:44]!r}")
        H._reset_stopping_for_tests()
    finally:
        H.guest_availability = _av

print("== profile: caching, purge verification, and the remaining branches ==")
with _tf.TemporaryDirectory() as _td:
    owner = Path(_td) / "owner"
    (owner / ".claude").mkdir(parents=True)
    (owner / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "a"}}))
    os.environ["CLAUDE_CONFIG_DIR"] = str(owner / ".claude")

    # An explicit workspace argument selects the profile location.
    ws = Path(_td) / "ws"
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)
    ck(str(P.guest_home(ws)).startswith(str(ws)), "an explicit workspace roots the guest home")

    # The readiness cache: a second call inside the TTL does not re-provision.
    gh = Path(_td) / "cached"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh)
    P.invalidate_readiness_cache()
    calls = []
    _real_ensure = P.ensure_guest_profile
    P.ensure_guest_profile = lambda *a, **k: (calls.append(1), (True, None))[1]
    try:
        P.guest_profile_ready()
        P.guest_profile_ready()
        ck(len(calls) == 1, "a positive verdict is cached (one provisioning call for two probes)")
        # Negative verdicts expire quickly: advance the clock past the negative TTL only.
        P.invalidate_readiness_cache()
        calls.clear()
        P.ensure_guest_profile = lambda *a, **k: (calls.append(1), (False, "worker_missing"))[1]
        P.guest_profile_ready(now=1000.0)
        P.guest_profile_ready(now=1000.0 + P._NEG_CACHE_TTL_S + 1)
        ck(len(calls) == 2, "a negative verdict expires fast and is re-checked")
        # ...while a positive one survives that same interval.
        P.invalidate_readiness_cache()
        calls.clear()
        P.ensure_guest_profile = lambda *a, **k: (calls.append(1), (True, None))[1]
        P.guest_profile_ready(now=2000.0)
        P.guest_profile_ready(now=2000.0 + P._NEG_CACHE_TTL_S + 1)
        ck(len(calls) == 1, "a positive verdict survives the negative TTL")
    finally:
        P.ensure_guest_profile = _real_ensure
        P.invalidate_readiness_cache()

    # A malformed owner .claude.json (not JSON at all) is unauthenticated.
    (owner / ".claude.json").write_text("{ not json")
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(Path(_td) / "bad")
    P.invalidate_readiness_cache()
    ok, reason = P.ensure_guest_profile()
    ck(ok is False and reason == "worker_unauthenticated", "unparseable .claude.json -> unauthenticated")

    # A .claude.json with none of the allowlisted keys is unauthenticated too.
    (owner / ".claude.json").write_text(json.dumps({"telemetry": True}))
    P.invalidate_readiness_cache()
    ok, reason = P.ensure_guest_profile()
    ck(ok is False and reason == "worker_unauthenticated",
       "a .claude.json without account fields -> unauthenticated")

    # A symlinked owner credential is refused as a source (never followed).
    (owner / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "a"}}))
    secret = Path(_td) / "elsewhere.json"
    secret.write_text('{"stolen":1}')
    link = owner / ".claude" / ".credentials.json"
    link.symlink_to(secret)
    gh2 = Path(_td) / "symlinked-src"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh2)
    P.invalidate_readiness_cache()
    ok, _ = P.ensure_guest_profile()
    ck(ok is True and not (gh2 / ".credentials.json").exists(),
       "a symlinked owner credential is not copied (treated as keyring-style)")
    link.unlink()

    # A directory where a profile FILE belongs fails closed rather than pretending.
    gh3 = Path(_td) / "dirblock"
    gh3.mkdir()
    (gh3 / ".claude.json").mkdir()
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh3)
    P.invalidate_readiness_cache()
    ok, reason = P.ensure_guest_profile()
    ck(ok is False and reason == "guest_profile_missing",
       "a directory at a profile file path fails closed")

    # _purge_credentials: provably-gone vs cannot-prove.
    gh4 = Path(_td) / "purge"
    gh4.mkdir()
    ck(P._purge_credentials(gh4) is True, "purging an absent credential succeeds")
    (gh4 / ".credentials.json").write_text("x")
    ck(P._purge_credentials(gh4) is True, "purging an existing credential succeeds")
    ck(not (gh4 / ".credentials.json").exists(), "...and it is gone")

    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)

print("== fault injection: the last error paths ==")
import sys as _sys


class _FakeWinreg:
    """Stands in for the winreg module so the Windows policy probe is exercised."""
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, behavior):
        self._behavior = behavior

    def OpenKey(self, root, subkey):  # noqa: N802 - mirrors the stdlib name
        return self._behavior(root, subkey)


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_saved_winreg = _sys.modules.get("winreg")
try:
    # An existing policy key -> present.
    _sys.modules["winreg"] = _FakeWinreg(lambda r, k: _Ctx())
    ck(H._windows_policy_present() is True, "an existing Windows policy key -> present")

    # Every key absent -> not policy.
    def _missing(r, k):
        raise FileNotFoundError(k)

    _sys.modules["winreg"] = _FakeWinreg(_missing)
    ck(H._windows_policy_present() is False, "all policy keys absent -> not managed")

    # A key we cannot read -> present (cannot prove absence).
    def _denied(r, k):
        raise OSError("access denied")

    _sys.modules["winreg"] = _FakeWinreg(_denied)
    ck(H._windows_policy_present() is True, "an unreadable policy key -> present (fail-closed)")
finally:
    if _saved_winreg is None:
        _sys.modules.pop("winreg", None)
    else:
        _sys.modules["winreg"] = _saved_winreg

# managed_policy_present short-circuits on the FIRST present source.
_real_ps = H._path_state
H._path_state = lambda p: "present"
try:
    ck(H.managed_policy_present() is True, "the first present source short-circuits detection")
finally:
    H._path_state = _real_ps

print("== spawn: pgid lookup failure and the publish/claim race ==")
with _tf.TemporaryDirectory() as _td:
    rd = Path(_td)

    class _P2:
        returncode = 0

        def __init__(self):
            self.pid = os.getpid()

        def communicate(self, timeout=None):
            return "answer", ""

        def kill(self):
            pass

    _rp, _rg = H.subprocess.Popen, H._guard
    _rgetpgid, _rkillpg = os.getpgid, os.killpg
    H.subprocess.Popen = lambda *a, **k: _P2()
    H._guard = lambda b: b
    os.killpg = lambda *a, **k: None
    # getpgid raises -> the code must fall back to proc.pid rather than run untracked.
    os.getpgid = lambda pid: (_ for _ in ()).throw(OSError("no pgid"))
    try:
        H._reset_stopping_for_tests()
        with H._live_lock:
            H._live_tasks["signal-guest-nopgid"] = rd
        H._slots.acquire()
        H._run("signal-guest-nopgid", "q", rd, lambda t: t, str(rd))
        ck((rd / "signal-guest-nopgid.txt").exists(),
           "a worker whose pgid cannot be read still publishes (pid fallback, never untracked)")
    finally:
        os.getpgid, os.killpg = _rgetpgid, _rkillpg
        H.subprocess.Popen, H._guard = _rp, _rg

    # Publish when shutdown already claimed the task: the cancellation must stand.
    H._reset_stopping_for_tests()
    (rd / "signal-guest-claimed.txt").write_text("[deep_dive cancelled: shutdown won]")
    _rp2 = H.subprocess.Popen
    H.subprocess.Popen = lambda *a, **k: _P2()
    try:
        H._slots.acquire()
        # NOT registered in _live_tasks -> the publish path must bail out.
        H._run("signal-guest-claimed", "q", rd, lambda t: t, str(rd))
        ck((rd / "signal-guest-claimed.txt").read_text().startswith("[deep_dive cancelled"),
           "a late publish never clobbers a shutdown cancellation")
    finally:
        H.subprocess.Popen = _rp2

print("== profile: write failures and unremovable credentials ==")
with _tf.TemporaryDirectory() as _td:
    owner = Path(_td) / "owner"
    (owner / ".claude").mkdir(parents=True)
    (owner / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "a"}}))
    os.environ["CLAUDE_CONFIG_DIR"] = str(owner / ".claude")
    gh = Path(_td) / "home"
    os.environ["SIGNAL_GUEST_CLAUDE_HOME"] = str(gh)

    # _write_private cleans up its temp file and re-raises when the write fails.
    _real_replace = os.replace
    os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        P.invalidate_readiness_cache()
        ok, reason = P.ensure_guest_profile()
        ck(ok is False and reason == "guest_profile_missing",
           "a failed profile write reports guest_profile_missing (never a half-written profile)")
        leftovers = [f for f in gh.glob(".*.tmp-*")] if gh.exists() else []
        ck(leftovers == [], "the temp file is cleaned up on a failed write")
    finally:
        os.replace = _real_replace

    # A credential that cannot be unlinked AND still stats -> purge fails, readiness fails.
    P.invalidate_readiness_cache()
    P.ensure_guest_profile()
    ck((gh / ".credentials.json").exists() or True, "profile provisioned for the purge test")
    _real_unlink = Path.unlink

    def _no_unlink(self, *a, **k):
        raise PermissionError("cannot remove")

    Path.unlink = _no_unlink
    try:
        (gh / ".credentials.json").write_text("stale")
        ck(P._purge_credentials(gh) is False,
           "a credential that cannot be removed fails the purge (never reported gone)")
    finally:
        Path.unlink = _real_unlink

    # _read_owner_account: an unreadable (but present) .claude.json -> None.
    _real_read_text = Path.read_text

    def _boom_read(self, *a, **k):
        if self.name == ".claude.json":
            raise PermissionError("denied")
        return _real_read_text(self, *a, **k)

    Path.read_text = _boom_read
    try:
        P.invalidate_readiness_cache()
        ok, reason = P.ensure_guest_profile()
        ck(ok is False and reason in ("worker_unauthenticated", "guest_profile_purge_failed"),
           f"an unreadable .claude.json is not ready ({reason})")
    finally:
        Path.read_text = _real_read_text

    # _owner_credentials_path: a stat error on the credential -> treated as absent.
    _real_is_symlink = Path.is_symlink

    def _boom_symlink(self):
        raise OSError("stat failed")

    Path.is_symlink = _boom_symlink
    try:
        ck(P._owner_credentials_path() is None,
           "an unstattable owner credential is treated as absent (no copy attempted)")
    finally:
        Path.is_symlink = _real_is_symlink

    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("SIGNAL_GUEST_CLAUDE_HOME", None)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("all guest-worker containment checks passed")
