#!/usr/bin/env python3
"""A fresh `quota-state.json` is not evidence that the CORE is proxy-routed.

`check_quota_telemetry` warned only when the file was STALE. But the file is written
by the credential proxy on behalf of whatever talks to it — not by the core — so a
fresh file proves only that *something* routed. Measured on this host 2026-08-02:

    before a one-off `ANTHROPIC_BASE_URL=... claude -p ...`
        quota-telemetry  warn  "quota state is 18h stale while the agent is working"
    after that single throwaway request
        quota-telemetry  ok    "quota state present (updated 3m ago)"

The production core was equally unrouted in both readings, and `QUOTA_STATE_STALE_SEC`
is 6h, so one probe bought six hours of false green. `core_env_has_proxy_url()` closes
that by asking the running process instead of the artifact.

The tri-state is the whole design, so it is what these assertions pin:

    True   env read, variable present   -> routed
    False  env read, variable absent    -> NOT routed (the only downgrading answer)
    None   env not readable at all      -> unknown; must NEVER become False

`ps eww` prints argv and then the environment, and prints argv ALONE for a process
whose environment this user may not read. "No KEY=VALUE tokens" is therefore
indistinguishable from "an empty environment", which is why the helper requires at
least one pair before it will answer False. Without that gate a permission failure
would manufacture exactly the false warning this check exists to remove — the same
defect, pointed the other way.

Run:  python3 tests/quota-telemetry-core-env.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:300]}")


class R:
    """Minimal CompletedProcess stand-in (returncode + stdout is the whole contract)."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


#: A real `ps eww` line: argv first, then the environment. Taken from this host's own
#: core (pid 6648) and trimmed — the shape is what matters, not the values.
ARGV = "claude --name sutando-core --model opus --remote-control Sutando"
ENV_NO_PROXY = "SUTANDO_CORE_SESSION=1 SUTANDO_CORE_RUNTIME=claude SUTANDO_HOST_LABEL=Chis-MacBook-Pro"
ENV_WITH_PROXY = ENV_NO_PROXY + " ANTHROPIC_BASE_URL=http://localhost:7846"


def probe(panes: R | None, ps: R | None):
    return hc.core_env_has_proxy_url(
        socket_path="/tmp/probe.sock",
        tmux_runner=lambda sock, *a: panes,
        ps_runner=lambda pid: ps,
    )


print("quota-telemetry: core env probe (tri-state)")

check("env carries ANTHROPIC_BASE_URL -> True",
      probe(R(0, "6648\n"), R(0, f"{ARGV} {ENV_WITH_PROXY}")) is True)

check("env read and variable ABSENT -> False (the only downgrading answer)",
      probe(R(0, "6648\n"), R(0, f"{ARGV} {ENV_NO_PROXY}")) is False)

# --- every unknown must be None, never False -------------------------------------
check("argv only, no KEY=VALUE at all (env not readable) -> None, NOT False",
      probe(R(0, "6648\n"), R(0, ARGV)) is None,
      "a permission failure prints argv alone; treating that as False would "
      "manufacture the false warning this check exists to remove")

check("tmux session missing (rc!=0) -> None",
      probe(R(1, ""), R(0, f"{ARGV} {ENV_WITH_PROXY}")) is None)

check("tmux unavailable (runner returns None) -> None",
      probe(None, R(0, f"{ARGV} {ENV_WITH_PROXY}")) is None)

check("pane pid not numeric -> None",
      probe(R(0, "not-a-pid\n"), R(0, f"{ARGV} {ENV_NO_PROXY}")) is None)

check("empty pane output -> None",
      probe(R(0, "\n"), R(0, f"{ARGV} {ENV_NO_PROXY}")) is None)

check("ps fails (rc!=0) -> None",
      probe(R(0, "6648\n"), R(1, "")) is None)

check("ps raises -> None",
      hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                                tmux_runner=lambda sock, *a: R(0, "6648\n"),
                                ps_runner=lambda pid: (_ for _ in ()).throw(OSError("boom"))) is None)

# --- discrimination controls ------------------------------------------------------
# Without these, a helper that returned None for everything would pass every
# "-> None" line above and half this suite would be vacuous.
check("control: the True and False cases differ (helper is not constant)",
      probe(R(0, "6648\n"), R(0, f"{ARGV} {ENV_WITH_PROXY}"))
      is not probe(R(0, "6648\n"), R(0, f"{ARGV} {ENV_NO_PROXY}")))

check("control: a LOOKALIKE var does not satisfy the check",
      probe(R(0, "6648\n"),
            R(0, f"{ARGV} {ENV_NO_PROXY} ANTHROPIC_BASE_URL_OLD=http://x")) is False,
      "prefix matching on 'ANTHROPIC_BASE_URL' without the '=' would wrongly say True")

check("control: the pane pid is passed through to ps, not hardcoded",
      hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                                tmux_runner=lambda sock, *a: R(0, "424242\n"),
                                ps_runner=lambda pid: R(0, f"{ARGV} {ENV_WITH_PROXY}")
                                if str(pid) == "424242" else R(0, ARGV)) is True)

# --- the sibling-window case (PR #2530 review, john-the-dev P1) ---------------
# `list-panes -t =<session>` resolves to the session's CURRENT WINDOW, and this repo
# deliberately keeps sibling windows (gateway, monitor) in the core's session — it heals
# window-scoped so they survive. With `gateway` active, the original code returned the
# GATEWAY's pid and reported on its environment. The reviewer built that case on a real
# tmux and it reproduced. Window NAME is no discriminator either: on this host the core's
# window is auto-named after the claude version (`2.1.220`).
GATEWAY_ARGV = "node /Users/x/sutando/src/remote-gateway-bridge.js"

def multi_pane(order, core_env=ENV_NO_PROXY, gw_env=ENV_WITH_PROXY):
    """order = list of 'core'/'gateway'; the FIRST is what the old code would have read."""
    pids = {"core": "6648", "gateway": "7777"}
    listing = R(0, "\n".join(pids[k] for k in order) + "\n")
    def ps(pid):
        if str(pid) == pids["core"]:
            return R(0, f"{ARGV} {core_env}")
        return R(0, f"{GATEWAY_ARGV} {gw_env}")
    return hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                                     tmux_runner=lambda sock, *a: listing,
                                     ps_runner=ps)

check("sibling window listed FIRST does not hijack the verdict",
      multi_pane(["gateway", "core"]) is False,
      "the gateway carries ANTHROPIC_BASE_URL and the core does not; answering True "
      "would be reporting on a sibling's environment")

check("same session, core listed first — still the core's answer",
      multi_pane(["core", "gateway"]) is False)

check("and it is the CORE's value that is returned, not merely 'not the gateway's'",
      multi_pane(["gateway", "core"], core_env=ENV_WITH_PROXY, gw_env=ENV_NO_PROXY) is True)

check("no pane carries the --name marker -> None (core not in this session)",
      hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                                tmux_runner=lambda sock, *a: R(0, "7777\n"),
                                ps_runner=lambda pid: R(0, f"{GATEWAY_ARGV} {ENV_WITH_PROXY}")) is None)

check("TWO panes both claiming the marker -> None (ambiguous is not evidence)",
      hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                                tmux_runner=lambda sock, *a: R(0, "6648\n6649\n"),
                                ps_runner=lambda pid: R(0, f"{ARGV} {ENV_NO_PROXY}")) is None)

# control: the tmux call must enumerate the whole SESSION, not the active window.
_seen_args = {}
hc.core_env_has_proxy_url(socket_path="/tmp/probe.sock",
                          tmux_runner=lambda sock, *a: (_seen_args.setdefault("a", a), R(0, "6648\n"))[1],
                          ps_runner=lambda pid: R(0, f"{ARGV} {ENV_NO_PROXY}"))
check("control: tmux is invoked with -s (every pane in the session)",
      "-s" in _seen_args.get("a", ()),
      f"args were {_seen_args.get('a')!r} — without -s tmux returns only the current window")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — unknown stays unknown; only a read environment can say False")
