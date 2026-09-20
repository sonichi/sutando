#!/usr/bin/env python3
"""Pane idle-gate and line delivery for a core CLI pane — the consumer-side policy every external task-notifier shares.

A notifier answers two questions before it types into the core's tmux pane: is
the pane ready to receive a line, and how does the line get in. This module owns
both answers for every runtime; a notifier keeps only its own I/O (which pane it
captures, how often it polls, what it logs).

    classify_pane(text, ADAPTERS["codex"])   -> Verdict(state, reason, pending)
        state: idle-ready | busy | pending | abnormal | unknown   (unknown is never idle)
    deliver(line, session, "codex", ...)     -> Outcome mapped from tmux-send-line.sh's exit code

Runtime specifics are DATA on a RuntimeAdapter — prompt glyph, idle footer, gate
signatures, input-affordance hint, placeholder — never branches in the
policy. A gate signature counts only while a current input affordance is on screen
and no empty composer sits under it: a finished turn's prose may say "Select" or
"Press Enter to continue", and above the runtime's own empty prompt it is history.
The Claude constants live here and core-input-watch.py aliases them; the
prompt-line parse lives here and scripts/tmux-send-line.sh calls it.

Boundary: cli_wedge.py stays advisory — it reports idle/moving x healthy/abnormal
and never decides "send". This gate decides, and only deliver() reaches the one
sender, scripts/tmux-send-line.sh. Nothing here calls send-keys.

CLI:
    python3 src/delivery/pane_gate.py classify --runtime codex [--json] < capture.txt
    python3 src/delivery/pane_gate.py pending  --runtime codex < capture.txt
    python3 src/delivery/pane_gate.py composer-text --runtime claude < capture.txt
    python3 src/delivery/pane_gate.py healthy --runtime claude < capture.txt   # exit 0 = accepts input
    python3 src/delivery/pane_gate.py deliver <session> <line> --runtime codex
        [--socket PATH] [--refuse-if-pending] [--skip-if-queued WORD] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

_SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SRC))
from cli_wedge import capture_pane, core_target, frame_abnormal, frame_working  # noqa: E402

REPO = _SRC.parent
SEND_LINE = REPO / "scripts" / "tmux-send-line.sh"

#: Non-blank lines a verdict reads — the same window core-input-watch.py reads.
TAIL_LINES = 14

_SGR = re.compile(r"\x1b\[[0-9;]*m")
# Dim (SGR 2) or the 256-colour GREYSCALE RAMP (232-255): styled runs are hint/ghost
# text only. 200-231 are colour-cube entries, so real coloured input must not match.
_GHOST = re.compile(
    r"\x1b\[(?:2|38;5;(?:23[2-9]|24[0-9]|25[0-5]))m.*?(?=\x1b\[(?:0|22|39)m|$)")

STATES = ("idle-ready", "busy", "pending", "abnormal", "unknown")

# The one owner of "may something type into this pane"; callers ask, never restate.
# `unknown` refuses: a pane that could not be parsed may hold an unsent draft.
UNSAFE_STATES = ("pending", "busy", "abnormal", "unknown")

EXIT_UNSAFE = 3  # `safe` said no, or `pending` found no prompt at all


def state_is_unsafe(state: str) -> bool:
    """True when `state` must not be typed into. Unrecognised states refuse too."""
    return state in UNSAFE_STATES or state not in STATES

# ---- Claude Code pane text. core-input-watch.py imports these three; edit here only.
FABLE_TEXT = re.compile(r"reached your Fable limit|included Fable usage for this week", re.I)
CLAUDE_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|←\s*for agents\b", re.I)
# First match classifies; the idle "❯ " prompt matches none of them.
CLAUDE_GATE_SIGNATURES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("fable-limit", re.compile(r"❯\s*Switch to .{1,80}? and continue", re.I)),
    ("fable-limit-unfocused", FABLE_TEXT),
    ("session-limit", re.compile(r"hit your (?:session|usage|weekly) limit", re.I)),
    ("folder-trust", re.compile(r"trust the files in this folder|Do you trust", re.I)),
    ("bypass-permissions", re.compile(r"Bypass Permissions mode|Yes, I accept", re.I)),
    ("login", re.compile(r"Select login method|Paste code here|Browser didn'?t open", re.I)),
    ("press-enter", re.compile(r"Press Enter to continue", re.I)),
    ("selection", re.compile(r"(❯\s*\d+\.|\bSelect\b).*", re.S)),
    ("permission", re.compile(r"Do you want to (proceed|allow)|Allow this action|permission to", re.I)),
]
# A dialog that holds the input shows how to answer it: a caret on a numbered row or a key hint.
AWAIT_HINT = re.compile(
    r"Esc to cancel|Enter to confirm|Enter to select|to navigate|Press Enter|Paste code|to accept"
    r"|Continuing automatically|❯\s*\d+\.", re.I)


@dataclass(frozen=True)
class RuntimeAdapter:
    name: str
    glyph: str
    idle_ready: "re.Pattern[str]"
    gate_signatures: Tuple[Tuple[str, "re.Pattern[str]"], ...]
    await_hint: "re.Pattern[str]" = AWAIT_HINT


CLAUDE = RuntimeAdapter(
    name="claude", glyph="❯", idle_ready=CLAUDE_IDLE,
    gate_signatures=tuple(CLAUDE_GATE_SIGNATURES),
)
# Codex prints the same footer when launched with Sutando's flags; a selected
# picker row is its own glyph followed by a number, and is itself the affordance.
CODEX_PICKER_ROW = re.compile(r"›\s*\d+\.")
CODEX = RuntimeAdapter(
    name="codex", glyph="›", idle_ready=CLAUDE_IDLE,
    gate_signatures=tuple(CLAUDE_GATE_SIGNATURES) + (("selection", CODEX_PICKER_ROW),),
    await_hint=re.compile(f"{AWAIT_HINT.pattern}|{CODEX_PICKER_ROW.pattern}", re.I),
)
ADAPTERS = {CLAUDE.name: CLAUDE, CODEX.name: CODEX}


@dataclass(frozen=True)
class PromptLine:
    text: str
    placeholder: bool


@dataclass(frozen=True)
class Verdict:
    state: str
    reason: str
    pending: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Outcome:
    status: str
    code: int
    message: str


def prompt_line(capture: str, adapter: RuntimeAdapter, width: int = 0) -> Optional[PromptLine]:
    """The LAST line starting with the runtime's glyph (scrollback holds old ones):
    its input is what follows the glyph and one optional space/nbsp, with any
    styled hint (dim placeholder, grey ghost suggestion) dropped first -- typed
    text is unstyled on both runtimes, so a styled run after the glyph is never
    pending. None = no prompt seen."""
    lines_ = capture.splitlines()
    last_i, found = -1, None
    for i, raw in enumerate(lines_):
        plain = _SGR.sub("", raw).lstrip(" \t")
        if not plain.startswith(adapter.glyph):
            continue
        rest_raw = raw[raw.find(adapter.glyph) + len(adapter.glyph):]
        rest_ghostless, n = _GHOST.subn("", rest_raw)
        rest = _SGR.sub("", rest_ghostless)
        placeholder = n > 0
        if rest[:1] in (" ", "\u00a0"):
            rest = rest[1:]
        last_i, found, last_raw = i, PromptLine(rest.rstrip(), placeholder), rest_raw
    if found is None or width <= 0:
        return found
    raw_parts = [last_raw]
    prev_full = len(_SGR.sub("", lines_[last_i])) >= width
    for nxt in lines_[last_i + 1:]:
        if not prev_full:
            break
        plain = _SGR.sub("", nxt)
        if plain.lstrip(" \t").startswith(adapter.glyph):
            break
        raw_parts.append(nxt)
        prev_full = len(plain) >= width
    # A ghost run opened on the prompt row stays open across the wrap, and its continuation
    # rows carry no SGR of their own -- strip AFTER joining, or the tail reads as input.
    joined_ghostless, n = _GHOST.subn("", "".join(raw_parts))
    joined = _SGR.sub("", joined_ghostless)
    if joined[:1] in (" ", " "):
        joined = joined[1:]
    return PromptLine(joined.rstrip(), n > 0)


def pending_text(capture: str, adapter: RuntimeAdapter, width: int = 0) -> Optional[str]:
    """Text typed at the current prompt; "" when the composer is empty; None when no prompt is seen."""
    line = prompt_line(capture, adapter, width)
    return None if line is None else line.text


def after_prompt(capture: str, adapter: RuntimeAdapter, width: int = 0) -> str:
    """Everything BELOW the current prompt line, colour stripped -- a fingerprint of the
    rest of the pane. A prompt line matching a staged payload is not proof the composer
    is still live: it can be a stale line from before a gate appeared beneath it. Callers
    doing a delayed re-check (tmux-send-line.sh's Codex path) must also require this be
    unchanged, not just the prompt text. "" when no prompt line was found at all."""
    lines_ = capture.splitlines()
    last_i = -1
    for i, raw in enumerate(lines_):
        if _SGR.sub("", raw).lstrip(" \t").startswith(adapter.glyph):
            last_i = i
    if last_i < 0:
        return ""
    end = last_i
    if width > 0:
        # A wrapped composer continues onto rows with no glyph; they are input, not content
        # below it, so the fingerprint must start after the last continuation row.
        prev_full = len(_SGR.sub("", lines_[last_i])) >= width
        for j in range(last_i + 1, len(lines_)):
            if not prev_full:
                break
            plain = _SGR.sub("", lines_[j])
            if plain.lstrip(" \t").startswith(adapter.glyph):
                break
            end = j
            prev_full = len(plain) >= width
    return "\n".join(_SGR.sub("", x).strip() for x in lines_[end + 1:])


#: A pane-border/rule row (box-drawing chars only) -- never legitimate composer text.
BORDER_LINE = re.compile(r"^[\s─-╿]+$")
# The CLI's hint in an EMPTY composer (`❯ Try "refactor <filepath>"`); it vanishes
# on the first typed character, so it is never a draft. Plain capture loses its dimming.
COMPOSER_PLACEHOLDER = re.compile(
    r'^\s*❯\s*(?:Try "[^"\n]*"|Press up to edit queued messages)\s*$')
# A single hint line the CLI prints below its own idle footer; only the fixed
# "Tip:" lead-in is matched, since the tip text itself rotates release to release.
TIP_ROW = re.compile(r"Tip:\s")


def composer_text(capture: str, adapter: RuntimeAdapter = CLAUDE) -> Optional[str]:
    """The composer's full typed content, dewrapped, or None with no <glyph> line
    at all; "" for an empty composer. The ONE parser for task-notifier.sh's
    EXACT-equality staging checks -- core-input-watch.py's `_composer_text`
    aliases this rather than re-deriving it.

    From the bottommost prompt line to the end of the capture. Below an editable
    composer the CLI renders its own frame -- box rule(s), at most one idle-footer
    row, at most one hint/tip row -- so the strip removes exactly those, once each,
    from the back. It never re-classifies an interior row: an owner's own typed
    line that happens to read one of those rows' words survives, because only the
    LAST matching row of each kind is ever popped, and never a second time.
    """
    lines = [ln for ln in capture.splitlines() if ln.strip()]
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip(" \t").startswith(adapter.glyph):
            start = i
            break
    if start is None:
        return None
    block = lines[start:]

    def _pop_borders():
        while len(block) > 1 and BORDER_LINE.match(block[-1]):
            block.pop()

    _pop_borders()
    # A tip row renders below the idle-footer row, so it is popped first --
    # else the idle-footer check below never reaches its own trailing row.
    if len(block) > 1 and TIP_ROW.search(block[-1]):
        block.pop()
    _pop_borders()
    if len(block) > 1 and adapter.idle_ready.search(block[-1]):
        block.pop()
    _pop_borders()
    if not block or (len(block) == 1 and COMPOSER_PLACEHOLDER.match(block[0])):
        return ""
    block[0] = block[0].lstrip().lstrip(adapter.glyph).lstrip()
    return "".join(block)


def _tail_lines(capture: str) -> List[str]:
    """The last TAIL_LINES non-blank lines, attributes kept (the composer parse needs them)."""
    return [ln for ln in capture.splitlines() if _SGR.sub("", ln).strip()][-TAIL_LINES:]


def _gate(capture: str, tail: str, line: Optional[PromptLine], adapter: RuntimeAdapter) -> Optional[str]:
    """The gate kind holding the pane, or None. An empty composer at the bottom means no
    dialog owns the input -- UNLESS a gate has appeared BELOW that composer line since it
    was drawn. A stale empty composer sitting above a freshly-printed gate is not proof the
    gate is gone (keweichen/round-4, 2026-09-17): a login/approval dialog can render under
    an already-idle footer within the same capture. Check after_prompt() for exactly that
    case -- everything below the current prompt line -- before trusting the composer."""
    if line is not None and not line.text:
        following = after_prompt(capture, adapter)
        # A live pane draws its footer last, so text after the LAST footer line is
        # newer than the footer: a dialog there is live even under a trailing glyph.
        footer_hits = list(adapter.idle_ready.finditer(tail))
        beyond_footer = tail[footer_hits[-1].end():] if footer_hits else ""
        newer = "\n".join(part for part in (following, beyond_footer) if part.strip())
        if not newer or not adapter.await_hint.search(newer):
            return None
        for kind, rx in adapter.gate_signatures:
            if rx.search(newer):
                return kind
        # An await hint we cannot name is an UNRECOGNISED dialog, not the absence of one.
        # Falling through to "no gate" let a stale empty composer vouch for a live dialog.
        return "unlisted"
    if not adapter.await_hint.search(tail):
        return None
    for kind, rx in adapter.gate_signatures:
        if rx.search(tail):
            return kind
    return None


# A working turn queues typed input (the Claude notifier delivers like the Monitor
# tool); a dialog, a parked banner or an unreadable pane does not.
def accepts_input(verdict: Verdict) -> bool:
    """True when a line typed now reaches the composer: idle, a draft, or a running turn."""
    if verdict.state in ("idle-ready", "pending"):
        return True
    return verdict.state == "busy" and verdict.reason == "working"


def classify_pane(capture: Optional[str], adapter: RuntimeAdapter) -> Verdict:
    """One verdict for a captured pane. A failed or empty capture is UNKNOWN, never idle."""
    if capture is None:
        return Verdict("unknown", "no capture")
    if not capture.strip():
        return Verdict("unknown", "empty pane")
    lines = _tail_lines(capture)
    tail = "\n".join(_SGR.sub("", ln) for ln in lines)
    # cli_wedge ranks abnormal text above motion. A retry keeps that rank against
    # a dialog too; a parked line may be the dialog's own text, so the gate reads first.
    abn = frame_abnormal(tail)
    if abn and abn.retrying:
        return Verdict("abnormal", ",".join(abn.names))
    # FULL capture, not the TAIL_LINES tail: a wrapped draft past that window
    # loses its own glyph line to truncation and the footer reads as empty.
    line = prompt_line(capture, adapter)
    # _gate's after_prompt() must see the same capture line was found in, or
    # it computes "below" from a view that never contained that line at all.
    gate = _gate(capture, tail, line, adapter)
    if gate:
        return Verdict("busy", gate)
    if abn:
        return Verdict("abnormal", ",".join(abn.names))
    if frame_working(tail):
        return Verdict("busy", "working")
    if line is not None and line.text:
        return Verdict("pending", "text at the prompt", line.text)
    if adapter.idle_ready.search(tail) or (line is not None and line.placeholder):
        return Verdict("idle-ready", "idle footer or empty composer", "" if line else None)
    return Verdict("unknown", "no idle affordance")


def observe(socket_path: str, session: str, adapter: RuntimeAdapter, tmux_bin: str = "tmux",
            runner: Callable = subprocess.run, env: Optional[dict] = None) -> Verdict:
    """Capture the core window through cli_wedge's one capture path, then classify it."""
    target = core_target(socket_path, session, tmux_bin, runner, env)
    if target is None:
        return Verdict("unknown", "no core window")
    # Both runtimes need attributes: Codex draws a dim placeholder, Claude a
    # grey ghost suggestion, and prompt_line() strips either before deciding pending.
    text = capture_pane(socket_path, target, tmux_bin, runner, env, escapes=True)
    return classify_pane(text, adapter)


# tmux-send-line.sh's contract; 2 is its own argument rejection.
EXIT_STATUS = {0: "sent", 2: "rejected", 3: "no-session", 4: "no-tmux", 5: "pending", 6: "queued", 7: "unknown"}


def deliver(line: str, session: str, runtime: str, socket_path: Optional[str] = None,
            refuse_if_pending: bool = False, skip_if_queued: Optional[str] = None, dry_run: bool = False,
            runner: Callable = subprocess.run, script: Path = SEND_LINE) -> Outcome:
    """Type one line + Enter into the core pane via the ONE sender, scripts/tmux-send-line.sh.
    The prompt read, the queued-input policy and the lock are its; this only maps its exit code."""
    if runtime not in ADAPTERS:
        raise ValueError(f"unknown runtime {runtime!r} (expected one of {sorted(ADAPTERS)})")
    argv = ["bash", str(script), session, line, "--runtime", runtime]
    if socket_path:
        argv += ["--socket", socket_path]
    if refuse_if_pending:
        argv.append("--refuse-if-pending")
    if skip_if_queued:
        argv += ["--skip-if-queued", skip_if_queued]
    if dry_run:
        argv.append("--dry-run")
    proc = runner(argv, capture_output=True, text=True, timeout=60)
    code = int(getattr(proc, "returncode", 1))
    message = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or "")).strip()
    return Outcome(EXIT_STATUS.get(code, "failed"), code, message)


def _read_stdin() -> str:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return sys.stdin.read()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pane_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("safe-states")  # no --runtime: the state vocabulary is runtime-independent
    for name in ("classify", "pending", "after", "safe"):
        p = sub.add_parser(name)
        p.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
        p.add_argument("--width", type=int, default=0)
        if name == "classify":
            p.add_argument("--json", action="store_true")
    ct = sub.add_parser("composer-text")
    ct.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    hp = sub.add_parser("healthy")
    hp.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    d = sub.add_parser("deliver")
    d.add_argument("session")
    d.add_argument("line")
    d.add_argument("--runtime", required=True, choices=sorted(ADAPTERS))
    d.add_argument("--socket", default=os.environ.get("SUTANDO_TMUX_SOCKET"))
    d.add_argument("--refuse-if-pending", action="store_true")
    d.add_argument("--skip-if-queued", default=None)
    d.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "safe-states":
        # Emitting the SAFE set keeps a caller's membership test fail-closed: anything
        # absent -- a new state, a typo, an empty read -- refuses without being listed.
        print(" ".join(s for s in STATES if not state_is_unsafe(s)))
        return 0
    adapter = ADAPTERS[a.runtime]
    if a.cmd == "classify":
        v = classify_pane(_read_stdin(), adapter)
        print(json.dumps(v.as_dict()) if a.json else v.state)
        return 0
    if a.cmd == "pending":
        # None (no prompt line) and "" (empty composer) are opposite answers; printing
        # both as "" let a caller's `[ -n "$PENDING" ]` authorize a send. Unknown exits 3.
        text = pending_text(_read_stdin(), adapter, getattr(a, "width", 0))
        if text is None:
            print("pane_gate: no prompt line found — prompt unknown", file=sys.stderr)
            return EXIT_UNSAFE
        print(text)
        return 0
    if a.cmd == "healthy":
        # The notifier's pre-typing question, answered by the same verdict every
        # caller gets: exit 0 when typed input reaches the composer, else refuse.
        v = classify_pane(_read_stdin(), adapter)
        if not accepts_input(v):
            print(f"pane_gate: {v.state} ({v.reason}) — not accepting input", file=sys.stderr)
            return EXIT_UNSAFE
        print(v.state)
        return 0
    if a.cmd == "composer-text":
        # Same None/"" contract as "pending", via the frame-stripping parser
        # Claude's indented word-wrap needs instead of a terminal-wrap width.
        text = composer_text(_read_stdin(), adapter)
        if text is None:
            print("pane_gate: no prompt line found — prompt unknown", file=sys.stderr)
            return EXIT_UNSAFE
        print(text)
        return 0
    if a.cmd == "safe":
        v = classify_pane(_read_stdin(), adapter)
        if state_is_unsafe(v.state):
            print(f"pane_gate: {v.state} — not safe to type into", file=sys.stderr)
            return EXIT_UNSAFE
        print(v.state)
        return 0
    if a.cmd == "after":
        print(after_prompt(_read_stdin(), adapter, getattr(a, "width", 0)))
        return 0
    out = deliver(a.line, a.session, a.runtime, a.socket, a.refuse_if_pending, a.skip_if_queued, a.dry_run)
    print(f"{out.status}: {out.message}" if out.message else out.status, file=sys.stderr if out.code else sys.stdout)
    return out.code


if __name__ == "__main__":
    sys.exit(main())
