"""What the owner must read from a blocked terminal pane: the prompt minus the chrome around it.

Two surfaces quote the pane — the escalation card (`core-input-watch`) and the one-line relay
notice (`core-supervisor-relay`) — so the filter lives here once. It drops box-drawing rules,
URL fragments (an OAuth link wraps across lines), the tmux status bar and the idle footer; when
nothing readable is left it falls back to the raw lines, because these messages are the only
thing that reaches the owner and must fail noisy, never silent.
"""
from __future__ import annotations

import re

# Footer tokens are anchored to their glyphs so a real prompt that merely contains the words
# ("Bypass permissions for this tool? (y/n)") is not mistaken for the idle footer.
_NOISE_LINE = re.compile(
    r"https?://|%3A|code_challenge|[?&]state=|^\[[^\]\n]{0,40}\s\d+:\w+|⏵⏵ bypass permissions|← for agents",
    re.I)
_RULE_CHARS = set("─│┌┐└┘├┤┬┴┼╭╮╯╰═║ ")
_RULE_START = "─═╭╮╰╯┌┐└┘"  # a horizontal rule or a box corner begins chrome, never a prompt


def readable_lines(prompt: str | None) -> list[str]:
    """Every readable line of the pane, in order; empty when the pane is all chrome."""
    lines = [ln.strip() for ln in (prompt or "").splitlines() if ln.strip()]
    out = []
    for core in lines:
        if set(core) <= _RULE_CHARS or core[0] in _RULE_START:
            continue  # a rule, with or without a label in it ("──── sutando-core ─")
        if _NOISE_LINE.search(core):
            continue
        if len(core) > 80 and " " not in core:
            continue
        out.append(core.strip("─│ "))
    return out


def prompt_excerpt(prompt: str | None, limit: int = 6) -> list[str]:
    """The pane's tail for the card: the last `limit` readable lines, or the raw tail when the
    filter leaves nothing."""
    lines = [ln.strip() for ln in (prompt or "").splitlines() if ln.strip()]
    return (readable_lines(prompt) or lines)[-limit:]


def first_readable_line(prompt: str | None) -> str:
    """The single most informative line for a one-line notice: the first readable one, else the
    first non-empty raw line, else ''."""
    readable = readable_lines(prompt)
    if readable:
        return readable[0]
    return next((ln.strip() for ln in (prompt or "").splitlines() if ln.strip()), "")
