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

# --- the DEFAULT ps_runner, which every case above injects past ------------------
# CI reported exactly two uncovered changed lines: the default `ps_runner` closure.
# Every assertion above supplies its own, so the real one never ran — the shape of
# "tested the seam, never the thing behind it" that this PR's review already caught
# once. Exercised here against THIS test process's own pid: `ps eww -p <self>` is
# available on both macOS and the Linux CI runner, always returns a live process, and
# its argv cannot contain `--name sutando-core`, so the contract answer is None.
# Deterministic and hermetic — no live core required.
import os as _os

_default_ps = hc.core_env_has_proxy_url(
    socket_path="/tmp/probe.sock",
    tmux_runner=lambda sock, *a: R(0, f"{_os.getpid()}\n"),
)  # ps_runner deliberately omitted -> the production closure runs
check("the DEFAULT ps_runner executes and yields the contract answer",
      _default_ps is None,
      "this process is not the core, so `--name sutando-core` is absent -> None")

# control: the default path really did SHELL OUT rather than short-circuiting. A pid
# that cannot exist makes `ps` exit non-zero; if that is indistinguishable from the
# line above, the test proves nothing about the closure.
_dead = hc.core_env_has_proxy_url(
    socket_path="/tmp/probe.sock",
    tmux_runner=lambda sock, *a: R(0, "2147483647\n"),
)
check("control: the default runner reaches a real `ps` (dead pid also -> None)",
      _dead is None)

# --- the lookalike SESSION NAME (PR #2530 review, john-the-dev) -------------------
# `f"--name {session}" in argv` is a SUBSTRING test, so `--name sutando-core-watcher`
# satisfied it and a prefix-named sibling was accepted as the core. The reviewer
# reproduced it on a sole pane: returned True where the contract answer is None.
#
# This is the SAME lookalike class as the `ANTHROPIC_BASE_URL_OLD` control already in
# this file. I wrote the guard for the env-var axis and then opened the identical hole
# on the session-name axis — which is why both axes now get an assertion.
WATCHER_ARGV = "claude --name sutando-core-watcher --model opus"

check("a PREFIX-named sibling is not the core (--name sutando-core-watcher)",
      probe(R(0, "6648\n"), R(0, f"{WATCHER_ARGV} {ENV_WITH_PROXY}")) is None,
      "substring matching on '--name sutando-core' accepts the watcher and returns True")

check("a SUFFIX-named sibling is not the core either",
      probe(R(0, "6648\n"), R(0, f"claude --name x-sutando-core {ENV_WITH_PROXY}")) is None)

check("the =-joined spelling IS accepted (--name=sutando-core)",
      probe(R(0, "6648\n"), R(0, f"claude --name=sutando-core {ENV_WITH_PROXY}")) is True)

check("control: exact --name still matches (the fix did not over-tighten)",
      probe(R(0, "6648\n"), R(0, f"{ARGV} {ENV_WITH_PROXY}")) is True)

check("a watcher and the real core in one session -> the CORE's answer",
      hc.core_env_has_proxy_url(
          socket_path="/tmp/probe.sock",
          tmux_runner=lambda sock, *a: R(0, "7777\n6648\n"),
          ps_runner=lambda pid: R(0, f"{ARGV} {ENV_NO_PROXY}") if str(pid) == "6648"
                                else R(0, f"{WATCHER_ARGV} {ENV_WITH_PROXY}")) is False)

# --- the socket must come from THIS host (PR #2530 review, qingyun-wu) ------------
# `_live_core_socket()` globs every synced state/cores/*.alive and takes the freshest,
# which the workspace contract permits to be another MACHINE's. The reviewer built two
# fresh records (local at N-1, peer at N) and the probe targeted the peer's socket;
# that socket is absent locally so the tri-state degraded to None — correct behaviour,
# wrong target, and it suppresses the warning this check exists to raise.
import json as _json
import pathlib as _pl
import tempfile as _tf
import time as _t

_ws = _pl.Path(_tf.mkdtemp())
_cores = _ws / "state" / "cores"
_cores.mkdir(parents=True)
_local_label = sorted(hc._local_host_labels())[0]
(_cores / f"{_local_label}.alive").write_text(_json.dumps({"socket": "/tmp/local-core.sock"}))
(_cores / "PeerHost.alive").write_text(_json.dumps({"socket": "/tmp/peer-core.sock"}))
# make the PEER strictly newer, which is the case that used to win
_now = _t.time()
_os.utime(_cores / f"{_local_label}.alive", (_now - 5, _now - 5))
_os.utime(_cores / "PeerHost.alive", (_now, _now))

check("a NEWER peer heartbeat does not win — the local socket is chosen",
      hc._local_core_socket(workspace=_ws) == "/tmp/local-core.sock",
      "the freshest *.alive belongs to PeerHost; resolving by mtime alone picks it")

check("control: the OLD resolver does pick the peer (the bug is real, not theoretical)",
      hc._live_core_socket(workspace=_ws) == "/tmp/peer-core.sock")

_os.remove(_cores / f"{_local_label}.alive")
check("no LOCAL heartbeat -> None, not a default socket",
      hc._local_core_socket(workspace=_ws) is None,
      "falling back to the default socket would probe a session this host may not run")

# --- _local_core_socket's own edge paths (CI named these seven lines) -------------
# Every case above passes `workspace=`, so the default-arg path and all four skip
# branches never ran. Same shape as the default-`ps_runner` gap two commits ago:
# the seam is exercised, the code behind it is not.
_ws2 = _pl.Path(_tf.mkdtemp())

check("no state/cores directory at all -> None",
      hc._local_core_socket(workspace=_ws2) is None)

_c2 = _ws2 / "state" / "cores"; _c2.mkdir(parents=True)
_lbl = sorted(hc._local_host_labels())[0]

# stale: local label, valid payload, but the heartbeat is older than the 90s window
(_c2 / f"{_lbl}.alive").write_text(_json.dumps({"socket": "/tmp/stale.sock"}))
_old = _t.time() - 600
_os.utime(_c2 / f"{_lbl}.alive", (_old, _old))
check("a STALE local heartbeat is skipped -> None",
      hc._local_core_socket(workspace=_ws2) is None,
      "a heartbeat older than 90s is not a live core")

# control: the SAME file, freshened, must now resolve — otherwise the line above
# passes for the wrong reason (e.g. the label never matched at all).
_now2 = _t.time()
_os.utime(_c2 / f"{_lbl}.alive", (_now2, _now2))
check("control: freshening that exact file makes it resolve",
      hc._local_core_socket(workspace=_ws2) == "/tmp/stale.sock")

# non-dict payload: `null` decodes fine and would raise AttributeError on .get
(_c2 / f"{_lbl}.alive").write_text("null")
check("a heartbeat decoding to a NON-OBJECT is skipped, not raised",
      hc._local_core_socket(workspace=_ws2) is None)

# malformed JSON -> ValueError branch
(_c2 / f"{_lbl}.alive").write_text("{not json")
check("malformed JSON is skipped, not raised",
      hc._local_core_socket(workspace=_ws2) is None)

# default-arg path: workspace=None must fall back to WORKSPACE_DIR
_saved = hc.WORKSPACE_DIR
try:
    hc.WORKSPACE_DIR = _ws2
    check("workspace=None falls back to WORKSPACE_DIR (default-arg path)",
          hc._local_core_socket() is None,
          "that tree's only heartbeat is malformed, so None is the right answer here")
finally:
    hc.WORKSPACE_DIR = _saved

# and the caller's guard: no local socket -> unknown, never a default-socket probe
# `is None or True` was the first spelling here and it is ALWAYS TRUE — a test that
# cannot fail, inside a PR whose whole subject is checks that cannot fail. Caught by
# both reviewers. Capture the result, then assert the contract on it.
_probed = []
_no_sock = hc.core_env_has_proxy_url(
    tmux_runner=lambda sock, *a: _probed.append(sock) or R(0, "6648\n"),
    ps_runner=lambda pid: R(0, f"{ARGV} {ENV_WITH_PROXY}"))
check("no local socket -> None (the tri-state contract, not merely 'did not crash')",
      _no_sock is None,
      f"returned {_no_sock!r}; a False here would be a bypass claim built on no evidence, "
      f"and a True would be a routing claim built on none")
check("control: and tmux was never called (no fallback to a default socket)",
      _probed == [],
      f"tmux was invoked with {_probed!r} — the guard let a default socket through")

# Both halves are needed and neither implies the other: the first pins WHAT is
# returned, the second pins that nothing was probed to get there. A helper that
# probed a default socket and happened to return None would pass the first alone.

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — unknown stays unknown; only a read environment can say False")
