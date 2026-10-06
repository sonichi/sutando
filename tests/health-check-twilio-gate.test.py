#!/usr/bin/env python3
"""
Regression tests for the phone-stack gate — twilio_configured() in
src/health-check.py and twilio_creds_present() in src/startup.sh — driven
through the same cases so the two cannot drift, plus startup.sh's phone
block itself: no public tunnel unless the server came up behind it.

Incident (2026-07-02): `.env` on a host with NO Twilio setup carries the
template's commented placeholder (`# TWILIO_ACCOUNT_SID=ACxxxxxxxxx`). The
old substring test in health-check (and the unanchored grep in startup.sh's
phone block) matched it, so every boot started conversation-server checks and
a PUBLIC ngrok tunnel to :3100 with nothing behind it, plus a bogus "Update
Twilio webhook" warning.

The gate asks for everything conversation-server.ts exits without — account
SID, auth token AND phone number — each resolving from an active .env line or
the Keychain vault (`vault set …`). Review of #4666: a gate that opened on the
SID alone reopened the 2026-07-02 hole from the other side. The documented
setup vaults the SID + token first and `twilio-setup.py buy` writes
TWILIO_PHONE_NUMBER later; in that gap the server exited at once and startup
still ran `ngrok http 3100` — a public URL to a dead port on every restart,
and hosts that merely held a SID in the vault started the phone stack unasked.

The shell side runs the real src/startup.sh function, extracted by its
anchors, with a fake `security` on PATH standing in for the Keychain and the
process environment scrubbed of TWILIO_*; the Python side gets the same fake
through the `vault_get` seam. The shell gate is driven twice: with the
resolver (`PY` set — the production path) and without it (`PY` empty — the
anchored grep fallback, which cannot see the vault).

Gate cases (python == startup; grep fallback in parentheses when it differs):
  a) commented placeholders                         → False
  b) SID + token in .env, no number (the setup gap)  → False
  c) SID + token + number in .env                    → True
  d) SID with empty value, token + number            → False
  e) no TWILIO line at all                           → False
  f) indented active lines, all three                → True
  g) vault SID only, placeholder in .env             → False
  h) vault SID + token, no number (the setup gap)    → False
  i) vault all three, placeholder in .env            → True  (grep: False)
  j) vault all three, no .env at all                 → True  (grep: False)
  k) .env SID + token, number in the vault           → True  (grep: False)
  l) .env SID + number, token in the vault           → True  (grep: False)

Phone-block cases (the real block from src/startup.sh, its services stubbed).
The bind probe is `lsof -ti :3100 -sTCP:LISTEN`, and the tunnel opens only
when a LISTEN socket belongs to the conversation-server process itself: the
pid startup.sh launched (or a descendant — the bound node runs under a
backgrounded function and tsx) in the start branch, one of pgrep's pids in
the already-running branch. A bare `lsof -i :3100` also answers for a stale
client connection or a foreign listener, and the old already-running branch
trusted pgrep alone; both published a public tunnel to a socket that was not
the server's. The `lsof` stub refuses any call without `-sTCP:LISTEN`.
  1) vault SID + token, no number             → server not launched, no ngrok
  2) all three, server binds :3100 (a child)  → server launched, ngrok started
  3) all three, server exits                  → server launched, NO ngrok
  4) all three, server never binds            → server launched, NO ngrok
  5) foreign process listens on :3100, server exits (port in use) → NO ngrok
  6) foreign process listens on :3100, server alive, never binds  → NO ngrok
  7) already running (pgrep), nothing listens on :3100            → NO ngrok
  8) already running (pgrep), that pid listens on :3100           → ngrok started
  9) already running (pgrep), a foreign pid listens on :3100      → NO ngrok

Run: python3 tests/health-check-twilio-gate.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

STARTUP = (REPO / "src" / "startup.sh").read_text()
_m = re.search(r"^twilio_creds_present\(\) \{\n.*?^\}\n", STARTUP, re.S | re.M)
assert _m, "twilio_creds_present() not found in src/startup.sh"
GATE_FN = _m.group(0)
for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
    assert var in GATE_FN, f"the gate no longer asks for {var}"
assert "channel_token.py" in GATE_FN and '--has "$_var"' in GATE_FN, "the gate lost its resolver tier"
assert 'grep -qE "^[[:space:]]*${_var}=[^[:space:]]"' in GATE_FN, "the gate lost its anchored grep fallback"

# The whole phone block: the gate function through the closing `fi` of the
# SKIP_PHONE / phone_stack_enabled / twilio_creds_present chain.
_b = re.search(r"^# 8\. Phone conversation server.*?^  echo \"  ~ conversation server \(no Twilio creds — optional\)\"\nfi\n",
               STARTUP, re.S | re.M)
assert _b, "the phone block was not found in src/startup.sh"
PHONE_BLOCK = _b.group(0)
assert "ngrok http 3100" in PHONE_BLOCK, "the phone block lost its ngrok start"

SHIM_DIR = Path(tempfile.mkdtemp(prefix="twilio-gate-shim-"))
# `security find-generic-password -a sutando -s KEY -w` answers from FAKE_VAULT_<KEY>.
(SHIM_DIR / "security").write_text(
    "#!/bin/sh\n"
    'key=""; prev=""\n'
    'for a in "$@"; do [ "$prev" = "-s" ] && key="$a"; prev="$a"; done\n'
    'eval "v=\\${FAKE_VAULT_$key:-}"\n'
    '[ "$1" = find-generic-password ] && [ -n "$v" ] && { printf "%s\\n" "$v"; exit 0; }\n'
    "exit 44\n")
(SHIM_DIR / "security").chmod(0o755)


def fake_vault(values: dict[str, str]):
    def get(key: str) -> str:
        if values.get(key):
            return values[key]
        raise KeyError(key)
    return get


def _shell_env(vault: dict[str, str], resolver: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("TWILIO_") and not k.startswith("FAKE_VAULT_")}
    env["PATH"] = f"{SHIM_DIR}{os.pathsep}{env.get('PATH', '')}"
    for key, value in vault.items():
        env[f"FAKE_VAULT_{key}"] = value
    env["PY"] = sys.executable if resolver else ""
    env["REPO"] = str(REPO)
    return env


def startup_gate(env_content: str | None, vault: dict[str, str], resolver: bool) -> bool:
    with tempfile.TemporaryDirectory() as d:
        if env_content is not None:
            (Path(d) / ".env").write_text(env_content)
        script = f"cd {shlex.quote(d)}\n{GATE_FN}\ntwilio_creds_present"
        r = subprocess.run(["bash", "-c", script], env=_shell_env(vault, resolver),
                           capture_output=True, text=True)
        # `elif twilio_creds_present` opens on 0 and closes on anything else (grep's 2
        # for a missing .env included); a crash would show on stderr.
        assert r.stderr == "", f"gate crashed rc={r.returncode}: {r.stderr}"
        return r.returncode == 0


def phone_block(env_content: str, vault: dict[str, str], server: str,
                foreign_listener: bool = False, running: str | None = None) -> dict:
    """Run the real phone block with its services stubbed.

    `server` is what the stubbed conversation-server does: 'binds' (a child
    of the launched pid is the :3100 listener, stays up), 'exits' (returns at
    once, like the real one on a missing credential or a port in use), or
    'hangs' (stays up, never binds). `foreign_listener` parks a live process
    that is not the server on :3100 before the block runs. `running` drives
    the already-running branch: 'listener' (pgrep names the pid that listens
    on :3100), 'silent' (pgrep names a live pid, nothing listens), 'foreign'
    (pgrep names a live pid, a different live pid listens). The block's
    literal /tmp/ log paths are redirected into the sandbox so the harness
    never writes over a running host's logs. `lsof`, `pgrep`, `ngrok`, `curl`
    and `sleep` are shell functions, so the block resolves them first; `ps`
    is the real one, so the ancestor walk runs against live pids. `lsof`
    answers only `-sTCP:LISTEN` queries, with the pid in `bound`.
    """
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".env").write_text(env_content)
        stubs = {
            "binds": "/bin/sleep 3 & echo $! > bound; wait",
            "exits": "exit 1",
            "hangs": "/bin/sleep 3",
        }[server]
        setup = []
        if foreign_listener:
            setup.append("/bin/sleep 3 > /dev/null 2>&1 & echo $! > bound")
        if running == "listener":
            setup.append("/bin/sleep 3 > /dev/null 2>&1 & echo $! > running; cp running bound")
        elif running == "silent":
            setup.append("/bin/sleep 3 > /dev/null 2>&1 & echo $! > running")
        elif running == "foreign":
            setup.append("/bin/sleep 3 > /dev/null 2>&1 & echo $! > running")
            setup.append("/bin/sleep 3 > /dev/null 2>&1 & echo $! > bound")
        script = "\n".join([
            f"cd {shlex.quote(d)}",
            "VERIFY_SETTLE_S=1",
            *setup,
            "phone_stack_enabled() { return 0; }",
            f"run_node_service() {{ touch launched; {stubs}; }}",
            'lsof() { case "$*" in *-sTCP:LISTEN*) cat bound 2>/dev/null;; '
            '*) echo "lsof without -sTCP:LISTEN: $*" >&2; return 2;; esac; }',
            'pgrep() { case "$*" in *conversation-server*) cat running 2>/dev/null;; *) return 1;; esac; }',
            "ngrok() { touch ngrok-started; /bin/sleep 3; }",
            'curl() { printf \'{"tunnels":[{"public_url":"https://t.ngrok-free.app"}]}\'; }',
            "sleep() { /bin/sleep 0.1; }",
            PHONE_BLOCK.replace("/tmp/", d.rstrip("/") + "/"),
        ])
        r = subprocess.run(["bash", "-c", script], env=_shell_env(vault, resolver=True),
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stderr == "", f"phone block failed rc={r.returncode}: {r.stderr}"
        return {
            "launched": (Path(d) / "launched").exists(),
            "ngrok": (Path(d) / "ngrok-started").exists(),
            "out": r.stdout,
        }


PLACEHOLDER = "# TWILIO_ACCOUNT_SID=ACxxxxxxxxx\n# TWILIO_AUTH_TOKEN=xxx\n# TWILIO_PHONE_NUMBER=+1xxx\n"
ALL_THREE = "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_AUTH_TOKEN=tok\nTWILIO_PHONE_NUMBER=+14155550100\n"
VAULT_ALL = {"TWILIO_ACCOUNT_SID": "ACvault", "TWILIO_AUTH_TOKEN": "tokvault", "TWILIO_PHONE_NUMBER": "+14155550100"}
VAULT_SID_TOKEN = {"TWILIO_ACCOUNT_SID": "ACvault", "TWILIO_AUTH_TOKEN": "tokvault"}

# (name, .env content or None, vault values, expected, expected without the resolver)
CASES = [
    ("a) commented placeholders", PLACEHOLDER, {}, False, False),
    ("b) SID + token, no number (setup gap)", "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_AUTH_TOKEN=tok\n", {}, False, False),
    ("c) SID + token + number", ALL_THREE, {}, True, True),
    ("d) empty SID value", "TWILIO_ACCOUNT_SID=\nTWILIO_AUTH_TOKEN=tok\nTWILIO_PHONE_NUMBER=+1415\n", {}, False, False),
    ("e) absent", "OPENAI_API_KEY=sk-x\n", {}, False, False),
    ("f) indented active lines", "  TWILIO_ACCOUNT_SID=AC123abc\n  TWILIO_AUTH_TOKEN=tok\n  TWILIO_PHONE_NUMBER=+1415\n", {}, True, True),
    ("g) vault SID only, placeholder in .env", PLACEHOLDER, {"TWILIO_ACCOUNT_SID": "ACvault"}, False, False),
    ("h) vault SID + token, no number (setup gap)", PLACEHOLDER, VAULT_SID_TOKEN, False, False),
    ("i) vault all three, placeholder in .env", PLACEHOLDER, VAULT_ALL, True, False),
    ("j) vault all three, no .env", None, VAULT_ALL, True, False),
    ("k) .env SID + token, vault number", "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_AUTH_TOKEN=tok\n",
     {"TWILIO_PHONE_NUMBER": "+14155550100"}, True, False),
    ("l) .env SID + number, vault token", "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_PHONE_NUMBER=+1415\n",
     {"TWILIO_AUTH_TOKEN": "tokvault"}, True, False),
]

# (name, .env content, vault values, server behaviour, foreign listener, already-running mode,
#  expect launched, expect ngrok, expected output fragment)
BLOCK_CASES = [
    ("1) vault SID + token, no number: phone stack stays off", PLACEHOLDER, VAULT_SID_TOKEN, "binds", False, None,
     False, False, "no Twilio creds"),
    ("2) all three, server binds: tunnel opens", ALL_THREE, {}, "binds", False, None,
     True, True, "ngrok (https://t.ngrok-free.app)"),
    ("3) all three, server exits: no tunnel to a dead port", ALL_THREE, {}, "exits", False, None,
     True, False, "conversation server exited"),
    ("4) all three, server never binds: no tunnel", ALL_THREE, {}, "hangs", False, None,
     True, False, "did not bind port 3100 within 1s"),
    ("5) foreign listener on :3100, server exits (port in use): no tunnel to it", ALL_THREE, {}, "exits", True, None,
     True, False, "held by pid"),
    ("6) foreign listener on :3100, server alive but not bound: no tunnel to it", ALL_THREE, {}, "hangs", True, None,
     True, False, "held by pid"),
    ("7) already running, nothing listens: no tunnel", ALL_THREE, {}, "binds", False, "silent",
     False, False, "nothing listens on port 3100"),
    ("8) already running, its pid listens: tunnel opens", ALL_THREE, {}, "binds", False, "listener",
     False, True, "conversation server (already running)"),
    ("9) already running, a foreign pid listens: no tunnel to it", ALL_THREE, {}, "binds", False, "foreign",
     False, False, "held by pid"),
]


def main() -> int:
    fails = []
    print("gate:")
    for name, env, vault, expected, expected_grep in CASES:
        got_py = hc.twilio_configured(env or "", vault_get=fake_vault(vault))
        got_sh = startup_gate(env, vault, resolver=True)
        got_grep = startup_gate(env, vault, resolver=False)
        ok = got_py == expected == got_sh and got_grep == expected_grep
        status = "PASS" if ok else "FAIL"
        print(f"  {status} {name} (python={got_py}, startup={got_sh}, grep-fallback={got_grep}, "
              f"expected={expected}/{expected_grep})")
        if not ok:
            fails.append(name)
    print("phone block:")
    for name, env, vault, server, foreign, running, want_launched, want_ngrok, fragment in BLOCK_CASES:
        got = phone_block(env, vault, server, foreign_listener=foreign, running=running)
        ok = got["launched"] == want_launched and got["ngrok"] == want_ngrok and fragment in got["out"]
        status = "PASS" if ok else "FAIL"
        print(f"  {status} {name} (launched={got['launched']}, ngrok={got['ngrok']}, "
              f"expected={want_launched}/{want_ngrok}, {'saw' if fragment in got['out'] else 'MISSING'} {fragment!r})")
        if not ok:
            print("    output was:\n" + "".join("      " + l + "\n" for l in got["out"].splitlines()))
            fails.append(name)
    if fails:
        print(f"\n{len(fails)} failure(s): {fails}")
        return 1
    print("All twilio-gate tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
