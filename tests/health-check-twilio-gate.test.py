#!/usr/bin/env python3
"""
Regression tests for twilio_configured(): the phone-stack gate must not be
fooled by the commented template placeholder.

Incident (2026-07-02): `.env` on a host with NO Twilio setup carries the
template's commented placeholder (`# TWILIO_ACCOUNT_SID=ACxxxxxxxxx`). The
old substring test in health-check (and the unanchored grep in startup.sh's
phone block) matched it, so every boot started conversation-server checks and
a PUBLIC ngrok tunnel to :3100 with nothing behind it, plus a bogus "Update
Twilio webhook" warning.

Cases:
  a) commented placeholder            → False
  b) active SID with value            → True
  c) active SID with empty value      → False
  d) no TWILIO line at all            → False
  e) indented active SID              → True (matches startup.sh's grep)

Run: python3 tests/health-check-twilio-gate.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

# The startup.sh gate must stay in sync — test the same cases through the
# exact grep pattern used there.
STARTUP_GREP = r"^[[:space:]]*TWILIO_ACCOUNT_SID=[^[:space:]]"


def startup_gate(env_content: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write(env_content)
        path = f.name
    try:
        r = subprocess.run(["grep", "-qE", STARTUP_GREP, path])
        return r.returncode == 0
    finally:
        Path(path).unlink(missing_ok=True)


CASES = [
    ("a) commented placeholder", "# TWILIO_ACCOUNT_SID=ACxxxxxxxxx\n# TWILIO_AUTH_TOKEN=xxx\n", False),
    ("b) active SID", "TWILIO_ACCOUNT_SID=AC123abc\nTWILIO_AUTH_TOKEN=tok\n", True),
    ("c) empty value", "TWILIO_ACCOUNT_SID=\n", False),
    ("d) absent", "OPENAI_API_KEY=sk-x\n", False),
    ("e) indented active SID", "  TWILIO_ACCOUNT_SID=AC123abc\n", True),
]


def main() -> int:
    fails = []
    for name, env, expected in CASES:
        got_py = hc.twilio_configured(env)
        got_sh = startup_gate(env)
        status = "PASS" if (got_py == expected == got_sh) else "FAIL"
        print(f"  {status} {name} (python={got_py}, grep={got_sh}, expected={expected})")
        if status == "FAIL":
            fails.append(name)
    if fails:
        print(f"\n{len(fails)} failure(s): {fails}")
        return 1
    print("All twilio-gate tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
