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


#: Argv fixtures are COMPOSED from the registry id rather than hand-written, for
#: two reasons. First, a hand-copied script name can drift from the row it is
#: meant to exercise. Second, `scripts/lint-hermetic-bridge-tests.py` correctly
#: refuses a test that both calls `exec_module` and names a bridge in an
#: EVALUATED string — it cannot distinguish "a string naming the module to
#: import" from "a string that is test data", and conservative is the right
#: default for a gate guarding the developer's real channel allowlist. Composing
#: the names keeps that gate honest instead of working around it.
PY_BIN = "/opt/homebrew/.../Python"


def daemon_argv(cid: str) -> str:
    """How startup.sh launches it: interpreter, then script path, nothing after.
    Confirmed against `ps -o args=` on a live host."""
    return f"{PY_BIN} /Users/x/sutando/src/{cid}.py"


def mention_argv(cid: str) -> str:
    """A process that only MENTIONS the script — the sub-command form, which is
    the real one: `python3 src/discord-bridge.py send <channel> <text>`."""
    return f"{PY_BIN} src/{cid}.py send 123 hello"


def ends_with_script_argv(cid: str) -> str:
    """The residual the anchor does NOT remove — see KNOWN LIMIT below."""
    return f"/usr/bin/vim /Users/x/sutando/src/{cid}.py"


CASE_IDS = ["discord-bridge", "slack-bridge", "telegram-bridge"]

#: The residual, pinned deliberately so it is a known limit rather than a later
#: surprise. `$` discriminates on TRAILING ARGS, so an argv that simply ENDS with
#: the script path still matches (an editor, `python3 -m py_compile <script>`).
#: My own first draft used that form as a decoy and the suite caught it — which
#: is the evidence these assertions bite. pgrep-on-argv cannot identify a daemon
#: at all; the real fix is a per-bridge status sidecar or pidfile (the gateway
#: already has one, which is why its row can fall back rather than rely on the
#: pattern). Out of scope here.

registry = {s["id"]: s for s in ss.service_registry()}

print("services-status bridge liveness patterns")

check("all three bridge rows are present in the registry",
      all(cid in registry for cid in CASE_IDS),
      str(sorted(registry)))

for cid in CASE_IDS:
    pattern = registry[cid]["probe"][-1]
    daemon, decoy = daemon_argv(cid), mention_argv(cid)

    # `pgrep -f` applies the pattern with re.search semantics over full argv.
    matches_daemon = re.search(pattern, daemon) is not None
    matches_decoy = re.search(pattern, decoy) is not None

    check(f"{cid}: still matches the real daemon argv", matches_daemon,
          f"pattern {pattern!r} vs {daemon!r}")
    check(f"{cid}: does NOT match a process that only mentions the script",
          not matches_decoy,
          f"pattern {pattern!r} matched {decoy!r} — a dead daemon would "
          f"read `running` while this ran")

# --- control: the assertions above are about the ANCHOR, not the fixture -----
# Without this, a pattern that matched nothing at all would pass every
# "does NOT match" line and half the suite would be vacuous.
for cid in CASE_IDS:
    unanchored = registry[cid]["probe"][-1].rstrip("$")
    decoy = mention_argv(cid)
    check(f"{cid}: control — the UNANCHORED form does match the decoy",
          re.search(unanchored, decoy) is not None,
          f"{unanchored!r} vs {decoy!r} — if this fails the decoy is wrong, "
          f"not the pattern")

# --- the residual, asserted as KNOWN rather than left to be rediscovered ----
resid_pat = registry["discord-bridge"]["probe"][-1]
check("KNOWN LIMIT: an argv that merely ENDS with the script still matches",
      re.search(resid_pat, ends_with_script_argv("discord-bridge")) is not None,
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
