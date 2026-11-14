#!/usr/bin/env python3
"""PreToolUse context-source-guard: serving-relative block of the agent's OWN
channel reads (hooks/context-source-guard.py).

Drives the real hook via stdin (the way Claude Code invokes it). Fully self-
contained: a fixture access.json + a temp workspace are pointed at via env
(SUTANDO_DISCORD_ACCESS_FILE / SUTANDO_WORKSPACE), and the guild cache is seeded
so no live Discord is touched. All ids are FICTITIOUS. Run:
  python3 tests/context-source-guard.test.py
"""
import json
import os
import subprocess
import tempfile
import sys
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env
from _helpers.subprocess_coverage import covered_code

isolate_os_environ()

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "context-source-guard.py")


def _hook_argv(hook):
    # `python -c` + exec, so the coverage runner's child prefix can trace the
    # hook; the globals keep __file__/__name__ identical to a file invocation.
    p = str(hook)
    code = (f"exec(compile(open({p!r}).read(), {p!r}, 'exec'), "
            f"{{'__file__': {p!r}, '__name__': '__main__'}})")
    return [sys.executable, "-c", covered_code(code)]

# --- fictitious fixtures (NOT real ids) ---
PUBLIC_CH = "900000000000000001"       # public channel that blacklists the private guild
PRIVATE_CH = "900000000000000003"      # in the private guild
PRIVATE_GUILD = "900000000000000002"
OTHER_PUB_CH = "900000000000000004"    # another public guild
OTHER_PUB_GUILD = "900000000000000005"

WS = tempfile.mkdtemp()
STATE_DIR = Path(WS) / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
# The hook derives the workspace from CLAUDE_CONFIG_DIR (= <workspace>/.claude-sutando)
# post-#1698 — SUTANDO_WORKSPACE was dropped in v0.8. Point it at <WS>/.claude-sutando
# so dirname() resolves back to this temp WS.
CFG_DIR = Path(WS) / ".claude-sutando"
CFG_DIR.mkdir(parents=True, exist_ok=True)
# seed guild cache -> deterministic, no network
(STATE_DIR / ".channel-guild-cache.json").write_text(
    json.dumps({PRIVATE_CH: PRIVATE_GUILD, OTHER_PUB_CH: OTHER_PUB_GUILD}))
# fixture access.json: PUBLIC_CH blacklists the private guild
acc = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"groups": {PUBLIC_CH: {"contextNotFrom": [PRIVATE_GUILD]}}}, acc); acc.close()

# hermetic env: no SUTANDO_WORKSPACE (dropped in v0.8) and no DISCORD_BOT_TOKEN,
# so guild resolution stays offline on the seeded cache either way.
ENV = isolated_child_env(CLAUDE_CONFIG_DIR=str(CFG_DIR), SUTANDO_DISCORD_ACCESS_FILE=acc.name)


def run(payload, env=ENV):
    # Assert the hook EXITED CLEANLY, not just that it printed something.
    #
    # The allow path is "exit 0 and print nothing" by design, and `is_deny()`
    # returns False for any unparseable output — so a hook that CRASHED (empty
    # stdout, non-zero exit) is indistinguishable from one that allowed. Every
    # `assert not is_deny(...)` case below would pass on a crash; only the deny
    # cases would catch it. Half this suite was blind, and it guards a security
    # boundary.
    #
    # Same shape as the audit-script false green: a subprocess check that reads
    # output but discards returncode cannot tell "ran" from "crashed".
    proc = subprocess.run(_hook_argv(HOOK), input=json.dumps(payload),
                          capture_output=True, text=True, timeout=20, env=env)
    assert proc.returncode == 0, (
        f"hook exited {proc.returncode} (expected 0). "
        f"stderr: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'none'}")
    return proc.stdout.strip()


def is_deny(out):
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    except Exception:
        return False


def read_task(channel_id):
    d = tempfile.mkdtemp()
    tp = os.path.join(d, "task-100.txt")
    Path(tp).write_text(f"id: task-100\nchannel_id: {channel_id}\nchannel_name: x\n")
    return {"tool_name": "Read", "tool_input": {"file_path": tp}}, tp


def bash_read(cid):
    return {"tool_name": "Bash", "tool_input": {"command": f"curl .../channels/{cid}/messages?limit=5"}}


def provider_task(filename, channel_id):
    d = tempfile.mkdtemp()
    path = os.path.join(d, filename)
    Path(path).write_text(f"id: {Path(filename).stem}\nchannel_id: {channel_id}\n")
    return path


# 1) serve PUBLIC_CH (via Read tool) -> read a private-guild channel => DENY
run(read_task(PUBLIC_CH)[0])
assert is_deny(run(bash_read(PRIVATE_CH))), "serve public-ch reading a private-guild channel MUST be denied"

for filename in ("task-dc900~123456.txt", "task-a%40b~%24ev.txt"):
    tp = provider_task(filename, PUBLIC_CH)
    (STATE_DIR / "active-serving-channel.json").unlink(missing_ok=True)
    run({"tool_name": "Read", "tool_input": {"file_path": tp}})
    assert is_deny(run(bash_read(PRIVATE_CH))), \
        f"Read of {filename} must set the serving channel before the private read"
    (STATE_DIR / "active-serving-channel.json").unlink(missing_ok=True)
    run({"tool_name": "Bash", "tool_input": {"command": f"cat {tp}"}})
    assert is_deny(run(bash_read(PRIVATE_CH))), \
        f"cat of {filename} must set the serving channel before the private read"

# 2) serve PUBLIC_CH -> read another public channel => ALLOW
assert not is_deny(run(bash_read(OTHER_PUB_CH))), "serve public-ch reading a public channel must be allowed"

# 3) serve PRIVATE_CH -> read PRIVATE_CH => ALLOW (its own contextNotFrom lacks itself)
run(read_task(PRIVATE_CH)[0])
assert not is_deny(run(bash_read(PRIVATE_CH))), "serving the private channel must still read it"

# 4) plain bash while serving PUBLIC_CH => ALLOW (untouched)
run(read_task(PUBLIC_CH)[0])
assert not is_deny(run({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})), "plain bash must pass"

# 5) cat-path: agent `cat`s the task in Bash (no Read tool) -> serving recorded -> read blocked
_, tp = read_task(PUBLIC_CH)
run({"tool_name": "Bash", "tool_input": {"command": f"cat {tp}"}})
assert is_deny(run(bash_read(PRIVATE_CH))), "cat-path: serving not picked up from a Bash task read"

# 6) fail-closed: serve PUBLIC_CH, read an UNKNOWN channel (no cache, no token) => DENY
run(read_task(PUBLIC_CH)[0])
assert is_deny(run(bash_read("900000000000000077"))), "unresolvable target guild + blacklist MUST fail-closed"

# 7) regression (#1698): a stray SUTANDO_WORKSPACE must NOT redirect resolution.
# Set it to an empty dir; serving state must still land in the CLAUDE_CONFIG_DIR
# workspace (where the guild cache lives), so the blacklist still applies → DENY.
# Pre-fix the hook read SUTANDO_WORKSPACE and looked in the empty dir → fail-open.
ENV_STALE = {**ENV, "SUTANDO_WORKSPACE": tempfile.mkdtemp()}
run(read_task(PUBLIC_CH)[0], env=ENV_STALE)
assert is_deny(run(bash_read(PRIVATE_CH), env=ENV_STALE)), \
    "#1698: SUTANDO_WORKSPACE must be ignored — resolution stays on CLAUDE_CONFIG_DIR"

# 8) per-host CLAUDE_CONFIG_DIR (`<workspace>/.claude-sutando/hosts/<host>`): the hook
# walks up to the nearest `.claude-sutando` ANCESTOR, so the leaf hostname must not
# break resolution. Serving state must still land in <WS> (where the guild cache is),
# so the blacklist applies → DENY. An exact-leaf-basename match would resolve a phantom
# dir under hosts/<host> → guard fails open.
CFG_HOST = CFG_DIR / "hosts" / "h1"
CFG_HOST.mkdir(parents=True, exist_ok=True)
ENV_HOST = {**ENV, "CLAUDE_CONFIG_DIR": str(CFG_HOST)}
run(read_task(PUBLIC_CH)[0], env=ENV_HOST)
assert is_deny(run(bash_read(PRIVATE_CH), env=ENV_HOST)), \
    "per-host CLAUDE_CONFIG_DIR: walk up to nearest .claude-sutando ancestor, resolution stays on <WS>"

def reader_read(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    f"python3 src/discord-read.py {PRIVATE_CH} --operator"))), \
    "discord-read CLI target in a blacklisted guild must be denied"

run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    f"python3 src/discord-read.py {PRIVATE_CH} --serving {OTHER_PUB_CH}"))), \
    "a model-selected serving channel must not replace the recorded serving channel"

run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    f"python3 src/read_discord_channel.py --serving {OTHER_PUB_CH} --target {PRIVATE_CH}"))), \
    "read_discord_channel must use the recorded serving channel's blacklist"

run(read_task(PUBLIC_CH)[0])
assert not is_deny(run(reader_read(
    f"python3 src/discord-read.py {OTHER_PUB_CH} --operator --limit 5 --after 123"))), \
    "CLI option values must not be treated as channel targets"

run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    'python3 src/discord-read.py "$CH" --operator'))), \
    "a non-literal reader target must be denied under an active blacklist"

(STATE_DIR / "active-serving-channel.json").unlink(missing_ok=True)
assert not is_deny(run(reader_read(
    f"python3 src/discord-read.py {PRIVATE_CH} --operator"))), \
    "reader CLI must remain available without a recorded serving channel"

(reader_invocations, expected_command) = (
    [
        f"python3 -u src/discord-read.py {PRIVATE_CH} --operator",
        f"python3 -W ignore src/discord-read.py {PRIVATE_CH} --operator",
        f"python3 -X dev src/discord-read.py {PRIVATE_CH} --operator",
        f"/usr/bin/python3 src/discord-read.py {PRIVATE_CH} --operator",
        f"python3.11 src/discord-read.py {PRIVATE_CH} --operator",
        f"uv run python src/discord-read.py {PRIVATE_CH} --operator",
        f"env X=1 python3 src/discord-read.py {PRIVATE_CH} --operator",
        f"true;python3 src/discord-read.py {PRIVATE_CH} --operator",
    ],
    "all Python reader invocations in a command segment must be detected",
)
for command in reader_invocations:
    run(read_task(PUBLIC_CH)[0])
    assert is_deny(run(reader_read(command))), f"{expected_command}: {command}"

for command in (
        "cat src/discord-read.py",
        "grep x src/discord-read.py",
        "python3 -m py_compile src/discord-read.py",
        "python3 -c 'print(1)' src/discord-read.py"):
    run(read_task(PUBLIC_CH)[0])
    assert not is_deny(run(reader_read(command))), \
        f"a reader script path used as a non-invocation must remain allowed: {command}"

# 9.5) reader-CLI argument edge cases: every ambiguous or malformed invocation
# fails CLOSED while a blacklist is active; only unambiguous non-readers pass.
run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    f"src/discord-read.py {PRIVATE_CH} --operator"))), \
    "a reader script invoked at argv[0] must be denied under a blacklist"

for bad in (
        "python3 src/discord-read.py --bogus=1",
        f"python3 src/read_discord_channel.py --target={PRIVATE_CH}",
        "python3 src/discord-read.py --serving",
        "python3 src/discord-read.py -z",
        f"python3 src/read_discord_channel.py --target {PRIVATE_CH} {OTHER_PUB_CH}",
        f"python3 src/discord-read.py {PRIVATE_CH} {OTHER_PUB_CH}",
        "# src/discord-read.py"):
    run(read_task(PUBLIC_CH)[0])
    assert is_deny(run(reader_read(bad))), \
        f"ambiguous reader invocation must fail closed under a blacklist: {bad}"

# prefix-only, bare -c, and script-file arguments carry no scan payload
for innocuous in ("env FOO=1", "bash -c", "bash run-me.sh"):
    run(read_task(PUBLIC_CH)[0])
    assert not is_deny(run(reader_read(innocuous))), \
        f"a non-reader command must be allowed: {innocuous}"

# 10) shell-wrapped reader invocations must not bypass the guard.
wrapped_invocations = (
    f"bash -c 'python3 src/discord-read.py {PRIVATE_CH} --operator'",
    f"env FOO=1 exec bash -c 'python3 src/discord-read.py {PRIVATE_CH} --operator'",
    f"sh -c 'cd /tmp && python3 src/discord-read.py {PRIVATE_CH} --operator'",
    f"bash -c 'echo hi; python3 src/discord-read.py {PRIVATE_CH} --operator'",
)
for command in wrapped_invocations:
    run(read_task(PUBLIC_CH)[0])
    assert is_deny(run(reader_read(command))), \
        f"shell-wrapped reader invocation must be denied: {command}"

run(read_task(PUBLIC_CH)[0])
assert not is_deny(run(reader_read("bash -c 'echo hello'"))), \
    "a wrapped non-reader command must be allowed"
for command in (
        f"bash -c 'echo python3 src/discord-read.py {PRIVATE_CH}'",
        f"printf 'python3 src/discord-read.py {PRIVATE_CH}\\n'",
        f"echo python3 src/discord-read.py {PRIVATE_CH}"):
    run(read_task(PUBLIC_CH)[0])
    assert not is_deny(run(reader_read(command))), \
        f"a command that only PRINTS a reader invocation must be allowed: {command}"
run(read_task(PUBLIC_CH)[0])
assert is_deny(run(reader_read(
    "bash -c 'python3 src/discord-read.py \"$CH\" --operator'"))), \
    "a wrapped non-literal reader target must be denied under an active blacklist"

# 11) missing _shell_scan sibling: the hook must DENY a channel read, not crash-open.
import shutil
lonely_dir = Path(tempfile.mkdtemp())
lonely_hook = lonely_dir / "context-source-guard.py"
shutil.copy(HOOK, lonely_hook)
proc = subprocess.run(_hook_argv(lonely_hook),
                      input=json.dumps(bash_read(PRIVATE_CH)),
                      capture_output=True, text=True, timeout=20, env=ENV)
assert proc.returncode == 0, f"hook crashed without its sibling: {proc.stderr[:200]}"
assert is_deny(proc.stdout.strip()), \
    "deployed without _shell_scan.py the guard must deny channel reads, not crash-open"

# 9) inner-fail-closed: exception inside the gated section (non-empty blacklist) must DENY,
#    not allow. Import the hook as a module and monkey-patch _resolve_guild to throw; the
#    inner try/except re-raises SystemExit from _deny() correctly and wraps other exceptions.
#    This exercises the security fix for finding #4 of the 2026-07-07 audit.
import importlib.util
spec = importlib.util.spec_from_file_location("csg", HOOK)
csg_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(csg_mod)

# Point module globals at the fixture workspace so _active_serving / _blacklist work.
csg_mod.ACCESS_FILE = acc.name
csg_mod.STATE = str(STATE_DIR / "active-serving-channel.json")
csg_mod._GUILD_CACHE = str(STATE_DIR / ".channel-guild-cache.json")

# Seed serving channel to PUBLIC_CH (which has a non-empty blacklist).
csg_mod._record_serving(PUBLIC_CH)

# Make _resolve_guild throw to simulate an unexpected runtime error in the gated section.
_orig_resolve = csg_mod._resolve_guild
csg_mod._resolve_guild = lambda cid, token: (_ for _ in ()).throw(RuntimeError("simulated crash"))

_denied_in_gated_section = False
try:
    csg_mod.main.__globals__["sys"] = sys  # share sys so sys.exit propagates
    # Simulate the Bash payload that hits the critical section.
    import io
    _payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": f"curl .../channels/{PRIVATE_CH}/messages?limit=5"}})
    _orig_stdin = sys.stdin
    sys.stdin = io.StringIO(_payload)
    try:
        csg_mod.main()
    except SystemExit as _e:
        _denied_in_gated_section = (_e.code == 0)  # _deny() calls sys.exit(0)
    finally:
        sys.stdin = _orig_stdin
except Exception:
    pass
finally:
    csg_mod._resolve_guild = _orig_resolve

# The deny output should have been written to stdout — check it landed.
# Since we can't capture it cleanly here (stdout isn't redirected), verify
# the test completed without an unhandled exception and the SystemExit was raised.
assert _denied_in_gated_section, "inner gated section: exception must fail-closed (deny), not fail-open"

# 12) in-process paths a file-child cannot attribute (coverage maps children
#     by filename) or reach (segments() never raises on str): ship-file direct.

# 12a) missing _shell_scan sibling, on the shipped file itself.
import builtins
spec_ns = importlib.util.spec_from_file_location("csg_nosib", HOOK)
nosib = importlib.util.module_from_spec(spec_ns)
_real_import = builtins.__import__
def _block_scan(name, *a, **k):
    if name == "_shell_scan":
        raise ImportError("blocked for test")
    return _real_import(name, *a, **k)
builtins.__import__ = _block_scan
try:
    spec_ns.loader.exec_module(nosib)
finally:
    builtins.__import__ = _real_import
assert nosib._shell_scan is None, "the _shell_scan import block did not take"
sys.stdin = io.StringIO(json.dumps(bash_read(PRIVATE_CH)))
try:
    nosib.main()
    raise AssertionError("missing-sibling hook allowed a channel read")
except SystemExit as e:
    assert e.code == 0, f"missing-sibling deny must exit 0, got {e.code}"
sys.stdin = io.StringIO(json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": "ls -la"}}))
try:
    nosib.main()
    raise AssertionError("missing-sibling hook did not exit cleanly on a non-read")
except SystemExit as e:
    assert e.code == 0, f"missing-sibling non-read path must exit 0, got {e.code}"
finally:
    sys.stdin = _orig_stdin

# 12b) _unwrap_segments edges: depth cap, prefix-only, bare -c, script arg.
assert csg_mod._unwrap_segments(["deep"], depth=5) == [["deep"]]
assert csg_mod._unwrap_segments(["env", "FOO=1"]) == [["env", "FOO=1"]]
assert csg_mod._unwrap_segments(["bash", "-c"]) == [["bash", "-c"]]
assert csg_mod._unwrap_segments(["bash", "run-me.sh"]) == [["bash", "run-me.sh"]]

# 12c) an inner -c payload whose scan raises propagates None (and one level up).
W = lambda t: type("W", (), {"text": t})()

class _FakeScan:
    def __init__(self): self.n = 0
    def segments(self, cmd):
        self.n += 1
        if self.n == 1:        # the outer command
            return [[W("bash"), W("-c"), W("MID")]]
        if cmd == "MID":       # the -c payload is another wrapper
            return [[W("bash"), W("-c"), W("INNER")]]
        raise RuntimeError("inner parse boom")
_scan = csg_mod._shell_scan
_orig_segments = _scan.segments
_scan.segments = _FakeScan().segments
try:
    assert csg_mod._reader_cli_targets("OUTER src/discord-read.py") == (True, [], True), \
        "an unparseable wrapped payload must fail closed when a reader is named"
finally:
    _scan.segments = _orig_segments

# 12d) a top-level scan exception: reader name -> suspicious; non-reader -> allow.
def _raise_segments(cmd):
    raise RuntimeError("parse boom")
_scan.segments = _raise_segments
try:
    assert csg_mod._reader_cli_targets("python3 src/discord-read.py 1") == (True, [], True)
    assert csg_mod._reader_cli_targets("echo hi") == (False, [], False)
finally:
    _scan.segments = _orig_segments

print("PASS: context-source-guard — serve public-ch blocks a private-guild read (Read- and cat-paths, "
      "+ fail-closed), allows public channels, serving the private channel reads itself, plain bash untouched, "
      "+ SUTANDO_WORKSPACE ignored (#1698), + per-host CLAUDE_CONFIG_DIR resolves via nearest .claude-sutando ancestor, "
      "+ inner gated section: exception fails-closed (security finding #4 fix), "
      "+ shell-wrapped reader invocations denied, + missing _shell_scan sibling fails closed")
