#!/usr/bin/env python3
"""core-supervisor-relay.py — the COMMUNICATOR (outbound ESCALATE).

Reads the monitor's `core-supervisor.json` signal (written by core-input-watch.py,
the M1 monitor) and, when the core hits a HARD blocker that only the user can
clear — `blocked-human` (login / an unrecognized prompt) or `logged-out` — routes
ONE "action needed" message to the owner wherever they are, reusing Sutando's
existing relay: a macOS notification always, plus an optional channel via
task-progress `notify.py` (Discord / Slack / Telegram / phone).

This is the ESCALATE layer's multi-channel surface (design: notes/design-core-
supervisor.md), complementing the in-app "Action needed" banner: the banner is
ONE surface; the communicator meets the user on whatever channel they're active
on. It NEVER acts on the core — sending a keystroke back into the prompt is the
separate, opt-in "actor" (M4). It only surfaces.

Why only blocked-human / logged-out escalate here:
  * They are user-actionable AND user-only: no seed or auto-answer can clear a
    /login or an unrecognized prompt — the human must.
  * `crashed` and `hung` are handled by RECOVER (bounded restart), not by nagging
    the user — a restart, not a human keystroke, is the fix. Escalating them here
    would be noise. (If a restart loop exhausts its budget, THAT escalation is the
    RECOVER layer's to raise, with its own message.)

Debounce: the (state, prompt) hash is persisted; a prompt that persists across
many monitor ticks escalates EXACTLY ONCE. A new/different prompt re-escalates.
This keeps the communicator high-signal — the owner is interrupted only when the
core enters a genuinely new stuck state.

Usage (one cycle — for a cron or the monitor loop to call each tick):
  core-supervisor-relay.py --signal <ws>/state/core-supervisor.json \
      --state-file <ws>/state/core-supervisor-relay.state \
      [--notify-source discord --notify-channel <id>] [--no-macos] [--dry-run]

The signal path is passed EXPLICITLY (--signal); this module never resolves the
workspace itself — the caller owns that (same discipline as the monitor's --out).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

# Hard blockers only the USER can clear → escalate to the owner's channel.
# crashed/hung belong to RECOVER (restart), not to user-escalation.
HARD_ESCALATE = {"blocked-human", "logged-out"}


def _sig_hash(signal: dict) -> str:
    """Stable hash of the escalation-relevant fields (state + prompt)."""
    key = f"{signal.get('state')}\x00{signal.get('prompt') or ''}"
    return hashlib.sha1(key.encode()).hexdigest()


def should_escalate(signal: dict, last_hash):
    """Pure decision. Returns (escalate: bool, new_last_hash: str|None).

    - Non-hard-blocker states never escalate; the persisted hash is left as-is so
      a later return to the SAME blocker (after a transient healthy tick) is still
      considered already-seen and does not double-notify.
    - A hard blocker escalates only when its (state,prompt) hash differs from the
      last escalated one (debounce) — a persistent prompt fires exactly once.
    """
    state = signal.get("state")
    if state not in HARD_ESCALATE:
        return False, last_hash
    h = _sig_hash(signal)
    if h == last_hash:
        return False, last_hash
    return True, h


def _is_login_class(signal: dict) -> bool:
    """Auth blockers need a GUI /login on the host — no reply or app tap can
    clear them (sonichi#2397). Root cause per #2402: a fresh CLAUDE_CONFIG_DIR
    always requires /login; a locked keychain (SSH spawn) only blocks
    completing it — hence the remedy must run from a GUI context."""
    return signal.get("state") == "logged-out" or signal.get("kind") == "login"


def compose_message(signal: dict) -> str:
    """The owner-facing 'action needed' line: what's stuck + a prompt excerpt."""
    detail = signal.get("detail") or signal.get("state") or "core needs attention"
    kind = signal.get("kind")
    prompt = (signal.get("prompt") or "").strip()
    # First non-empty prompt line is the most informative single line.
    excerpt = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
    parts = [f"⚠️ Agent needs you — {detail}"]
    if kind and kind not in detail:
        parts.append(f"({kind})")
    msg = " ".join(parts)
    if excerpt:
        # The remedy is the actionable half, so it keeps its length; the prompt
        # echo is what gives way to stay inside the message-length bound.
        msg += f": {excerpt[:110]}"
    if _is_login_class(signal):
        host = platform.node().split(".")[0] or "the host"
        msg += (f" — needs GUI /login on {host}: open Terminal there, run"
                " `bash src/restart.sh` from the repo, then complete /login."
                " A chat reply can't resolve this.")
    else:
        # Names no console on purpose: the core may be embedded and the socket is
        # env-overridable, so no fixed target is correct from here.
        host = platform.node().split(".")[0] or "the host"
        msg += (f" — answer it where the core is running on {host}."
                " A chat reply can't answer it.")
    return msg


# ---- emit adapters (best-effort; a failed channel never crashes the cycle) --- #
def _macos_notify(message: str) -> None:  # pragma: no cover - external I/O (osascript)
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title "Sutando · Agent Shepherd"'],
            capture_output=True, timeout=8)
    except Exception:
        pass


def _channel_notify(message: str, source: str, channel: str) -> bool:  # pragma: no cover - external I/O (notify.py subprocess)
    """Route through the existing task-progress relay (notify.py). Returns True
    only when the send actually landed (notify.py exit 0), so the caller can
    decide whether to debounce — a failed channel send must NOT suppress a retry."""
    notify = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""),
                          "skills", "task-progress", "scripts", "notify.py")
    if not os.path.isfile(notify):
        return False
    try:
        r = subprocess.run([sys.executable, notify, "--source", source,
                            "--channel-id", channel, "--message", message],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _load_last_hash(state_file):
    if not state_file:
        return None
    try:
        with open(state_file) as f:
            d = json.load(f)
        return d.get("last_hash") if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _save_last_hash(state_file, h):
    if not state_file:
        return
    try:
        # A cwd-relative --state-file (e.g. "relay.state") has an empty dirname;
        # os.makedirs("") raises FileNotFoundError (an OSError), which the except
        # below would swallow — silently disabling debounce persistence so the
        # relay re-escalates every cycle. Only create the dir when there is one.
        d = os.path.dirname(state_file)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_hash": h}, f)
        os.replace(tmp, state_file)
    except OSError:  # pragma: no cover - best-effort debounce persistence
        pass


def run_cycle(signal, state_file, *, macos=True, source="", channel="", dry_run=False):
    """One escalation cycle. Returns the message emitted, or None if suppressed."""
    escalate, new_hash = should_escalate(signal, _load_last_hash(state_file))
    if not escalate:
        return None
    msg = compose_message(signal)
    if dry_run:
        return msg
    if macos:
        _macos_notify(msg)
    # Debounce only when delivery actually landed. If a channel was selected but
    # its send failed, do NOT persist the hash — re-escalate next cycle so a
    # transient/misconfigured channel can't permanently swallow the alert (macOS
    # alone must not suppress the real channel). macOS-only (no channel selected)
    # still debounces — the local notification IS the delivery there.
    channel_ok = True
    if source and channel:
        channel_ok = _channel_notify(msg, source, channel)
    if channel_ok:
        _save_last_hash(state_file, new_hash)
    return msg


# Surfaces task-progress notify.py can actually DELIVER to. Other values that
# land in last-owner-activity.json ("voice", "github-commits", …) are activity
# signals, not deliverable channels — never route an escalation to them.
# Beyond the static set, any source with a configured channel dir
# ($CLAUDE_CONFIG_DIR/channels/<source>/ containing a *.env) counts — that
# mirrors notify.py's own resolution rule, so a NEW homeserver bridge (e.g.
# "dev-ag2space") becomes routable by creating its config dir, no code change.
_DELIVERABLE_SURFACES = {"discord", "slack", "telegram", "ag2space"}

# Must stay identical to notify.py's slug rule (sender/probe alignment): dots
# only BETWEEN alphanumerics, so traversal shapes never reach the path probe.
_SOURCE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]|\.(?=[a-z0-9]))*$")


def _is_deliverable(source):
    if source in _DELIVERABLE_SURFACES:
        return True
    if not source or not _SOURCE_SLUG_RE.match(source):
        return False
    # Probe for exactly what notify.py's sender reads, mirroring its FULL
    # resolution contract (review P1 x2 on #2701 — filename alone was not
    # enough): (1) same three-tier base, CLAUDE_CONFIG_DIR -> CLAUDE_HOME ->
    # ~/.claude; (2) same realpath containment — a channel entry symlinked
    # OUTSIDE channels/ is one the sender refuses, so probing it deliverable
    # would recreate the selected-then-send-fails class via a different
    # mismatch. If notify.py ever learns more filenames (#2686's try-both),
    # widen BOTH sides together.
    base = (os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
            or os.path.join(os.path.expanduser("~"), ".claude"))
    channels_dir = os.path.join(base, "channels")
    env_path = os.path.join(channels_dir, source, ".env")
    if not os.path.isfile(env_path):
        return False
    real_env = os.path.realpath(env_path)
    real_root = os.path.realpath(channels_dir)
    return real_env.startswith(real_root + os.sep)


def resolve_active_target(activity_path):
    """Read state/last-owner-activity.json → (source, channel_id) for the owner's
    MOST-RECENTLY-ACTIVE channel, so a hard blocker reaches them where they are.

    Returns ("", "") — meaning "no channel target, macOS-only" — when the file is
    missing/malformed, the active surface isn't a deliverable channel, or no
    routable `channel_id` was recorded (older activity writers, or a non-message
    surface). Degrading to macOS-only is always safe; we never guess a channel.
    """
    try:
        with open(activity_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return "", ""
    except (OSError, ValueError):
        return "", ""
    source = str(data.get("channel", "")).strip()
    channel = str(data.get("channel_id", "")).strip()
    if _is_deliverable(source) and channel:
        return source, channel
    return "", ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Escalate a hard-blocked core to the owner.")
    ap.add_argument("--signal", required=True, help="path to core-supervisor.json")
    ap.add_argument("--state-file", default="", help="debounce state (last escalated hash)")
    ap.add_argument("--notify-source", default="", help="task-progress source (discord/slack/telegram)")
    ap.add_argument("--notify-channel", default="", help="channel/chat id for --notify-source")
    ap.add_argument("--active-from", default="",
                    help="path to state/last-owner-activity.json — auto-target the owner's "
                         "active channel when --notify-source/--notify-channel aren't given")
    ap.add_argument("--no-macos", action="store_true", help="suppress the macOS notification")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    # Explicit --notify-* wins; else auto-resolve the owner's active channel.
    source, channel = a.notify_source, a.notify_channel
    if not (source and channel) and a.active_from:
        source, channel = resolve_active_target(a.active_from)

    try:
        with open(a.signal) as f:
            signal = json.load(f)
        if not isinstance(signal, dict):
            signal = {}
    except (OSError, ValueError):
        return 0  # no signal yet → nothing to escalate (degrade quietly)

    msg = run_cycle(signal, a.state_file, macos=not a.no_macos,
                    source=source, channel=channel, dry_run=a.dry_run)
    if msg:
        print(("DRY-RUN " if a.dry_run else "escalated: ") + msg)
        return 0
    print(f"no escalation (state={signal.get('state')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
