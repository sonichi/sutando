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

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("all guest-worker containment checks passed")
