#!/usr/bin/env python3
"""A bridge liveness pattern must match the DAEMON and nothing that merely
mentions it.

`service_registry()` probes the three DM bridges with `pgrep -f <pattern>`.
Those patterns were unanchored, so they matched any process whose argv contains
the script name — most concretely:

    python3 src/discord-bridge.py send <channel> <text>

which is the one-off REST send used to post from outside the daemon (documented
in `src/discord-bridge.py`'s `__main__` dispatch). Measured on a live host with
such a send in flight:

    pgrep -f 'discord-bridge\\.py'    -> ['2405', '97572']   <- decoy AND daemon
    pgrep -f 'discord-bridge\\.py$'   -> ['97572']           <- daemon only

So for the life of that send, a DEAD daemon would have reported `running`. The
window is short and needs the daemon to be down at that instant, so this is a
narrow false-green rather than a standing one — but the direction is the bad
one, and the fix is the `$` the gateway row in the same registry already uses.

The assertions below run the patterns against argv STRINGS rather than real
processes: what is under test is the pattern's discrimination, and a test that
spawns processes would be timing-dependent for no extra coverage. The live pgrep
numbers above are the provenance; these are the pinned contract.

Run:  python3 tests/services-status-liveness-patterns.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "services_status", REPO / "src" / "services_status.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:300]}")


#: (service id, the daemon's real argv, an argv that only MENTIONS the script).
#: Daemon forms are how `src/startup.sh` launches them — interpreter then script
#: path, nothing after — confirmed against `ps -o args=` on a live host.
CASES = [
    ("discord-bridge",
     "/opt/homebrew/.../Python /Users/x/sutando/src/discord-bridge.py",
     "/opt/homebrew/.../Python src/discord-bridge.py send 123 hello"),
    ("slack-bridge",
     "/opt/homebrew/.../Python /Users/x/sutando/src/slack-bridge.py",
     "/bin/bash -c tail -f logs/x && python3 src/slack-bridge.py --once"),
    ("telegram-bridge",
     "/opt/homebrew/.../Python /Users/x/sutando/src/telegram-bridge.py",
     "/opt/homebrew/.../Python src/telegram-bridge.py --once"),
]

#: The residual the anchor does NOT remove, pinned deliberately so it is a known
#: limit rather than a later surprise. `$` discriminates on TRAILING ARGS, so a
#: process whose argv simply ENDS with the script path still matches:
#:     /usr/bin/vim src/telegram-bridge.py
#:     python3 -m py_compile src/discord-bridge.py
#: My own first draft of this file used the vim form as a decoy and the suite
#: caught it — which is the evidence that these assertions bite.
#: pgrep-on-argv cannot identify a daemon; the real fix is a per-bridge status
#: sidecar or pidfile (the gateway already has one, which is why its row can
#: fall back rather than rely on the pattern). Out of scope here.
KNOWN_RESIDUAL = "/usr/bin/vim /Users/x/sutando/src/discord-bridge.py"

registry = {s["id"]: s for s in ss.service_registry()}

print("services-status bridge liveness patterns")

check("all three bridge rows are present in the registry",
      all(cid in registry for cid, _, _ in CASES),
      str(sorted(registry)))

for cid, daemon_argv, decoy_argv in CASES:
    probe = registry[cid]["probe"]
    pattern = probe[-1]

    # `pgrep -f` applies the pattern with re.search semantics over full argv.
    matches_daemon = re.search(pattern, daemon_argv) is not None
    matches_decoy = re.search(pattern, decoy_argv) is not None

    check(f"{cid}: still matches the real daemon argv", matches_daemon,
          f"pattern {pattern!r} vs {daemon_argv!r}")
    check(f"{cid}: does NOT match a process that only mentions the script",
          not matches_decoy,
          f"pattern {pattern!r} matched {decoy_argv!r} — a dead daemon would "
          f"read `running` while this ran")

# --- control: the assertions above are about the ANCHOR, not the fixture -----
# Without this, a pattern that matched nothing at all would pass every
# "does NOT match" line and half the suite would be vacuous.
for cid, daemon_argv, decoy_argv in CASES:
    unanchored = registry[cid]["probe"][-1].rstrip("$")
    check(f"{cid}: control — the UNANCHORED form does match the decoy",
          re.search(unanchored, decoy_argv) is not None,
          f"{unanchored!r} vs {decoy_argv!r} — if this fails the decoy is wrong, "
          f"not the pattern")

# --- the residual, asserted as KNOWN rather than left to be rediscovered ----
resid_pat = registry["discord-bridge"]["probe"][-1]
check("KNOWN LIMIT: an argv that merely ENDS with the script still matches",
      re.search(resid_pat, KNOWN_RESIDUAL) is not None,
      "if this ever fails the anchor got stricter — good, but update the note")

# The gateway row already used `$`; assert it stays that way so the convention
# is enforced rather than remembered.
gw = registry.get("gateway")
check("the gateway row's pattern is anchored too (unchanged)",
      gw is not None and str(gw["probe"][-1]).endswith("$"),
      str(gw["probe"] if gw else None))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — liveness patterns match the daemon, not mentions of it")
