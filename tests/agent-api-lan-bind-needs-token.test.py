#!/usr/bin/env python3
"""agent-api refuses to listen beyond loopback without SUTANDO_API_TOKEN.

With no token every route answers whoever reaches the port, so a LAN bind without one would
open the agent's state to every device on the network. The predicate, then the real entry
point: it exits 2 with an error naming the fix, before binding anything.

Run: python3 tests/agent-api-lan-bind-needs-token.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

import os
import subprocess
import sys
import tempfile
from pathlib import Path

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import local_api_gate as gate  # noqa: E402

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    for bind in ("127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1", "[::1]"):
        check(gate.lan_bind_refusal(bind, "") is None, f"loopback {bind!r} needs no token")
    for bind in ("0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "my-mac.local", ""):
        why = gate.lan_bind_refusal(bind, "")
        check(bool(why) and "SUTANDO_API_TOKEN" in why and "AGENT_API_BIND=127.0.0.1" in why,
              f"{bind!r} without a token is refused, naming both fixes")
        check(gate.lan_bind_refusal(bind, "tok") is None, f"{bind!r} with a token is allowed")

    ws = tempfile.mkdtemp(prefix="agent-api-lan-bind-")
    env = dict(os.environ, SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=ws, SUTANDO_TELEMETRY="0",
               SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1", AGENT_API_BIND="0.0.0.0", SUTANDO_API_TOKEN="")
    r = subprocess.run([sys.executable, str(REPO / "src" / "agent-api.py")], env=env, capture_output=True,
                       text=True, timeout=60)
    check(r.returncode == 2, f"the entry point exits 2 ({r.returncode})")
    check("refusing to listen on 0.0.0.0" in r.stderr and "SUTANDO_API_TOKEN" in r.stderr,
          f"the startup error names the fix ({r.stderr.strip()[-160:]!r})")
    check("Sutando Agent API" not in r.stdout, "it never reached the listening line")

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS: a LAN bind needs SUTANDO_API_TOKEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
