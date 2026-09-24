#!/usr/bin/env python3
"""
Regression tests for the phone-stack gate — twilio_configured() in
src/health-check.py and twilio_creds_present() in src/startup.sh — driven
through the same cases so the two cannot drift.

Incident (2026-07-02): `.env` on a host with NO Twilio setup carries the
template's commented placeholder (`# TWILIO_ACCOUNT_SID=ACxxxxxxxxx`). The
old substring test in health-check (and the unanchored grep in startup.sh's
phone block) matched it, so every boot started conversation-server checks and
a PUBLIC ngrok tunnel to :3100 with nothing behind it, plus a bogus "Update
Twilio webhook" warning.

The gate has a vault tier: `vault set TWILIO_ACCOUNT_SID …` with nothing
usable in .env opens it, because the phone server reads the vault too. The
shell side runs the real src/startup.sh function, extracted by its anchors,
with a fake `security` on PATH standing in for the Keychain and the process
environment scrubbed of TWILIO_*; the Python side gets the same fake through
the `vault_get` seam. The shell gate is driven twice: with the resolver
(`PY` set — the production path) and without it (`PY` empty — the anchored
grep fallback, which cannot see the vault).

Cases:
  a) commented placeholder            → False
  b) active SID with value            → True
  c) active SID with empty value      → False
  d) no TWILIO line at all            → False
  e) indented active SID              → True
  f) vault-only, placeholder in .env  → True  (grep fallback: False)
  g) vault-only, no .env at all       → True  (grep fallback: False)

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

_m = re.search(r"^twilio_creds_present\(\) \{\n.*?^\}\n", (REPO / "src" / "startup.sh").read_text(), re.S | re.M)
assert _m, "twilio_creds_present() not found in src/startup.sh"
GATE_FN = _m.group(0)
assert "channel_token.py" in GATE_FN and "--has TWILIO_ACCOUNT_SID" in GATE_FN, "the gate lost its resolver tier"
assert "grep -qE '^[[:space:]]*TWILIO_ACCOUNT_SID=[^[:space:]]'" in GATE_FN, "the gate lost its anchored grep fallback"

SHIM_DIR = Path(tempfile.mkdtemp(prefix="twilio-gate-shim-"))
(SHIM_DIR / "security").write_text(
    "#!/bin/sh\n"
    '[ "$1" = find-generic-password ] && [ -n "${FAKE_VAULT_SID:-}" ] && { printf "%s\\n" "$FAKE_VAULT_SID"; exit 0; }\n'
    "exit 44\n")
(SHIM_DIR / "security").chmod(0o755)


def fake_vault(sid: str):
    def get(key: str) -> str:
        if key == "TWILIO_ACCOUNT_SID" and sid:
            return sid
        raise KeyError(key)
    return get


def startup_gate(env_content: str | None, vault_sid: str, resolver: bool) -> bool:
    with tempfile.TemporaryDirectory() as d:
        if env_content is not None:
            (Path(d) / ".env").write_text(env_content)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TWILIO_")}
        env["PATH"] = f"{SHIM_DIR}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_VAULT_SID"] = vault_sid
        env["PY"] = sys.executable if resolver else ""
        env["REPO"] = str(REPO)
        script = f"cd {shlex.quote(d)}\n{GATE_FN}\ntwilio_creds_present"
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        # `elif twilio_creds_present` opens on 0 and closes on anything else (grep's 2
        # for a missing .env included); a crash would show on stderr.
        assert r.stderr == "", f"gate crashed rc={r.returncode}: {r.stderr}"
        return r.returncode == 0


# (name, .env content or None, vault value, expected, expected without the resolver)
CASES = [
    ("a) commented placeholder", "# TWILIO_ACCOUNT_SID=ACxxxxxxxxx\n# TWILIO_AUTH_TOKEN=xxx\n", "", False, False),
    ("b) active SID", "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_AUTH_TOKEN=tok\n", "", True, True),
    ("c) empty value", "TWILIO_ACCOUNT_SID=\n", "", False, False),
    ("d) absent", "OPENAI_API_KEY=sk-x\n", "", False, False),
    ("e) indented active SID", "  TWILIO_ACCOUNT_SID=AC123abc\n", "", True, True),
    ("f) vault-only, placeholder in .env", "# TWILIO_ACCOUNT_SID=ACxxxxxxxxx\n", "ACvault", True, False),
    ("g) vault-only, no .env", None, "ACvault", True, False),
]


def main() -> int:
    fails = []
    for name, env, vault_sid, expected, expected_grep in CASES:
        got_py = hc.twilio_configured(env or "", vault_get=fake_vault(vault_sid))
        got_sh = startup_gate(env, vault_sid, resolver=True)
        got_grep = startup_gate(env, vault_sid, resolver=False)
        ok = got_py == expected == got_sh and got_grep == expected_grep
        status = "PASS" if ok else "FAIL"
        print(f"  {status} {name} (python={got_py}, startup={got_sh}, grep-fallback={got_grep}, "
              f"expected={expected}/{expected_grep})")
        if not ok:
            fails.append(name)
    if fails:
        print(f"\n{len(fails)} failure(s): {fails}")
        return 1
    print("All twilio-gate tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
