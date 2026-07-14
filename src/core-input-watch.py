#!/usr/bin/env python3
"""core-input-watch.py — the core supervisor MONITOR (M1).

The detached bundled core runs Claude Code's interactive TUI with no TTY. Most
first-run gates are pre-seeded so the core never stops on them, but some
interactions inherently need the user (/login) or can't be predicted (a
mid-session permission request, a model/selection prompt, an unknown dialog).
With no TTY the core silently BLOCKS, "alive but stuck".

This is the MONITOR layer of the core supervisor (design: notes/design-core-
supervisor.md). Each tick it writes a single `state/core-supervisor.json` the
desktop app reads for BOTH its status display AND the "Action needed" banner. It
never acts; PREVENT (seeds), AUTO-ANSWER, ESCALATE (the banner), and RECOVER
(restart) are the other layers.

Relationship to runtime-health.py (#2092) — ONE derivation core, no drift.
------------------------------------------------------------------------------
`runtime-health.py` is the owner-designed coarse health signal the desktop
Console renders as a plain-English status strip: {offline, needs_login, working,
idle, unknown}. This supervisor does NOT re-derive liveness independently — that
would give two tmux/process derivations that drift (the review's `naming_conflict`
on #2100). Instead it CONSUMES `runtime_health.derive()` as its base and REFINES
those 5 coarse states into the 8 supervisor states, adding only what escalation
needs (which gate, the prompt text, can-we-auto-answer). The refinement is a pure
mapping, so the two vocabularies provably agree on "is it running / logged in /
idle / wedged":

    runtime-health health   →   supervisor state
    ---------------------       ----------------
    offline                 →   crashed
    needs_login             →   logged-out        (unless an ACTIVE gate shows, below)
    working                 →   running
    idle                    →   idle-ready
    unknown (status stale)  →   hung
    (any, + gateway down)   →   gateway-down       (gateway probe is bundled-specific)
    (any, + active gate)    →   blocked-known / blocked-human   (net-new: pane classify)

runtime-health.json stays the Console's coarse view (unchanged, its consumers
untouched); core-supervisor.json is the escalation-facing refinement of the same
derivation. Net-new here vs runtime-health = the gate classifier (classify),
the auto-answer decision (auto_answer), and the escalation state machine.

Signal schema (state/core-supervisor.json):
  { "state": "<state>", "detail": "<human string>",
    "prompt": "<pane excerpt if blocked, else null>",
    "kind": "<gate kind if blocked, else null>",
    "session": "sutando-core" }

How this runs (the launch + consumer live in the desktop bundle):
  * On-demand one-shot (in-repo, mirrors runtime-health.py's invocation model):
      core-input-watch.py --socket <sock> --out <ws>/state/core-supervisor.json --once
  * Continuous supervision loop: launched by the Tauri desktop bundle
    (ag2space-cinny-desktop #62) alongside the detached core; the "Action needed"
    banner + login button that CONSUME core-supervisor.json are #64 / cinny #130.

Usage:
  core-input-watch.py --socket <tmux.sock> --session sutando-core \
      --out <workspace>/state/core-supervisor.json [--app-data <dir>] \
      [--interval 3] [--stable 2] [--once]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import time


# ---- Shared derivation core: runtime-health.py (#2092). -------------------- #
# Loaded via importlib because the filename has a dash. This is THE single
# liveness/health derivation both the Console and this supervisor read from —
# reusing it (rather than re-implementing tmux/process probes here) is what keeps
# the two state vocabularies from drifting.
def _load_runtime_health():
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime-health.py")
    spec = importlib.util.spec_from_file_location("runtime_health", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- ESCALATE-detection: interactive-prompt signatures. First match classifies.
# Specific so the idle "❯ " prompt (ready for a task) is NEVER flagged. This is
# the net-new layer over runtime-health: it identifies WHICH gate the core is
# stuck at so ESCALATE can show the prompt and AUTO-ANSWER can decide.
_SIGNATURES = [
    ("folder-trust", re.compile(r"trust the files in this folder|Do you trust", re.I)),
    ("bypass-permissions", re.compile(r"Bypass Permissions mode|Yes, I accept", re.I)),
    ("login", re.compile(r"Select login method|Paste code here|Browser didn'?t open", re.I)),
    ("press-enter", re.compile(r"Press Enter to continue", re.I)),
    ("selection", re.compile(r"(❯\s*\d+\.|\bSelect\b).*", re.S)),
    ("permission", re.compile(r"Do you want to (proceed|allow)|Allow this action|permission to", re.I)),
]
_AWAIT_HINT = re.compile(
    r"Esc to cancel|Enter to confirm|Press Enter|Paste code|to accept|❯\s*\d+\.", re.I)
_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|for agents\b", re.I)

# Gates that need a human (can't be auto-answered): login + any unrecognized
# selection/permission. The rest (trust/bypass/press-enter) are known-safe.
_HUMAN_GATES = {"login", "selection", "permission", "unknown"}

# --- M4 AUTO-ANSWER decision (Layer 2), PURE + report-only. -----------------
# This returns WHICH keystroke would safely dismiss a gate; it does NOT send it —
# a separate, opt-in supervisor actor does that (kept OUT of the report-only
# monitor loop). The allowlist is deliberately TINY and strictly non-destructive:
# EVERYTHING not explicitly listed (every _HUMAN_GATE, every unknown/ambiguous
# state) returns None → ESCALATE. Expanding it is an owner-reviewed change.
_AUTO_ANSWER = {
    # "Press Enter to continue…" — purely informational (e.g. the post-login
    # confirmation). Pressing Enter only proceeds; it grants nothing and is not
    # destructive. The one gate safe to auto-dismiss.
    "press-enter": "Enter",
}


def auto_answer(kind):
    """M4 decision: the safe keystroke to auto-dismiss `kind`, or None → ESCALATE.

    SAFETY INVARIANT: only strictly non-destructive, capability-granting-nothing
    gates are answerable. A human gate (login/selection/permission/unknown) or any
    kind not in the allowlist ALWAYS returns None — the supervisor escalates,
    never guesses. Notably folder-trust + bypass-permissions are handled by the
    PREVENT seeds; if they ever surface at runtime they ESCALATE — we never
    auto-accept a trust / dangerous-mode prompt without the operator's explicit
    per-install opt-in.
    """
    if kind in _HUMAN_GATES:
        return None
    return _AUTO_ANSWER.get(kind)


def classify(pane: str):
    """Return (kind, excerpt) if the pane is awaiting user input, else None."""
    tail = "\n".join([ln for ln in pane.splitlines() if ln.strip()][-14:])
    if not _AWAIT_HINT.search(tail):
        return None
    if _IDLE.search(tail) and not any(rx.search(tail) for _, rx in _SIGNATURES[:5]):
        return None
    for kind, rx in _SIGNATURES:
        if rx.search(tail):
            return kind, tail
    # No specific signature — but an input affordance IS present and this is NOT
    # the idle prompt. That means an UNFORESEEN prompt. Surface it rather than
    # leave a silent dead-end (owner's no-dead-end requirement 2026-07-14): we
    # cannot enumerate every possible TUI question, so ANY await-affordance that
    # isn't the known idle-ready state is treated as needing the user.
    return "unknown", tail


# runtime-health health string → supervisor state, for the non-gate branches.
_BASE_TO_STATE = {
    "offline": ("crashed", "core process/session not found"),
    "needs_login": ("logged-out", "core not authenticated (needs /login)"),
    "idle": ("idle-ready", "ready for a task"),
    "working": ("running", "actively processing"),
    # "unknown" = runtime-health saw a live session but a stale/absent core-status
    # ("running" that never advanced) → wedged. That IS the supervisor's `hung`.
    "unknown": ("hung", "core alive but stalled (status stale, no recognized prompt)"),
}


def compose_state(pane, base_health, gateway_alive):
    """Refine runtime-health's coarse `base_health` into a supervisor state.

    `base_health` ∈ {offline, needs_login, working, idle, unknown} comes from
    runtime_health.derive() — the SHARED derivation. This function adds only the
    escalation-specific refinements: an active gate in the pane (finest signal),
    and the bundled-gateway-down state. Returns (state, detail, prompt, kind).
    """
    if base_health == "offline":
        return "crashed", _BASE_TO_STATE["offline"][1], None, None
    # An ACTIVE interactive gate in the pane is the finest signal — it distinguishes
    # "sitting at a prompt waiting for input" from the coarse health (e.g. the live
    # /login MENU, which runtime-health's needs_login markers don't match). Check it
    # first so we carry the prompt text + kind for ESCALATE / AUTO-ANSWER.
    hit = classify(pane) if pane else None
    if hit:
        kind, excerpt = hit
        if kind in _HUMAN_GATES:
            return "blocked-human", f"awaiting user: {kind}", excerpt, kind
        return "blocked-known", f"at known gate: {kind}", excerpt, kind
    if base_health == "needs_login":
        return "logged-out", _BASE_TO_STATE["needs_login"][1], None, None
    if not gateway_alive:
        return "gateway-down", "core up but relay gateway not running", None, None
    state, detail = _BASE_TO_STATE.get(base_health, _BASE_TO_STATE["unknown"])
    if state == "hung":
        tail = "\n".join([ln for ln in (pane or "").splitlines() if ln.strip()][-14:])
        return "hung", detail, tail or None, "unknown"
    return state, detail, None, None


# ---- Bundled-context probes (NOT part of runtime-health's coarse health). --- #
def _pgrep(pattern):
    try:
        return subprocess.run(["pgrep", "-f", pattern],
                              capture_output=True, timeout=6).returncode == 0
    except Exception:
        return False


def gateway_alive(app_data):
    # The bundled gateway runs from the app-data interpreter (see console_status:
    # keepalive cd's into $ENGINE so the argv is relative — match the app-unique
    # runtime-python dir, which uniquely scopes to THIS app's gateway). Gateway
    # liveness is a supervisor concern the Console's coarse health folds into a
    # detail string; here it is elevated to its own state, so it stays local.
    if app_data:
        return _pgrep(os.path.join(app_data, "engine", "runtime", "python"))
    return _pgrep("remote-gateway-bridge")


def capture(socket, session):
    try:
        out = subprocess.run(["tmux", "-S", socket, "capture-pane", "-p", "-t", f"{session}:0"],
                             capture_output=True, text=True, timeout=8)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--session", default="sutando-core")
    ap.add_argument("--out", required=True)
    ap.add_argument("--app-data", default="", help="app-support dir (for gateway probe)")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--stable", type=int, default=2,
                    help="consecutive identical prompt polls before escalating (debounce)")
    ap.add_argument("--once", action="store_true", help="one tick then exit (for tests/probes)")
    a = ap.parse_args()

    # Point the SHARED runtime-health derivation at THIS core's socket/session so
    # the supervisor and the Console's status strip read the same liveness core.
    os.environ["SUTANDO_TMUX_SOCKET"] = a.socket
    rh = _load_runtime_health()
    rh.TMUX_SOCKET = a.socket
    rh.SESSION = a.session

    last_sig = None
    stable_prompt = 0
    last_prompt = None
    while True:
        pane = capture(a.socket, a.session)
        base = rh.derive()  # shared: offline|needs_login|working|idle|unknown
        state, detail, prompt, kind = compose_state(
            pane or "", base.get("health", "unknown"), gateway_alive(a.app_data))

        # Debounce prompt escalation: only surface once the SAME prompt persists
        # (not a menu the core is actively navigating through).
        if state in ("blocked-human", "blocked-known"):
            stable_prompt = stable_prompt + 1 if prompt == last_prompt else 1
            last_prompt = prompt
            if stable_prompt < a.stable:
                state, detail, prompt, kind = "running", "processing (prompt settling)", None, None
        else:
            stable_prompt = 0
            last_prompt = None

        sig = (state, prompt)
        if sig != last_sig:
            _atomic_write(a.out, {"state": state, "detail": detail,
                                  "prompt": prompt, "kind": kind, "session": a.session})
            last_sig = sig
        if a.once:
            return
        time.sleep(a.interval)  # pragma: no cover - daemon heartbeat (tests use --once)


if __name__ == "__main__":
    main()
