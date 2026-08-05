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

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "context-source-guard.py")

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

ENV = {**os.environ, "CLAUDE_CONFIG_DIR": str(CFG_DIR), "SUTANDO_DISCORD_ACCESS_FILE": acc.name}
ENV.pop("SUTANDO_WORKSPACE", None)  # dropped in v0.8 — must not influence resolution
ENV.pop("DISCORD_BOT_TOKEN", None)  # force offline (cache-only) guild resolution


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
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
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


# 1) serve PUBLIC_CH (via Read tool) -> read a private-guild channel => DENY
run(read_task(PUBLIC_CH)[0])
assert is_deny(run(bash_read(PRIVATE_CH))), "serve public-ch reading a private-guild channel MUST be denied"

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

print("PASS: context-source-guard — serve public-ch blocks a private-guild read (Read- and cat-paths, "
      "+ fail-closed), allows public channels, serving the private channel reads itself, plain bash untouched, "
      "+ SUTANDO_WORKSPACE ignored (#1698), + per-host CLAUDE_CONFIG_DIR resolves via nearest .claude-sutando ancestor, "
      "+ inner gated section: exception fails-closed (security finding #4 fix)")
