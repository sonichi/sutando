"""A read-only live view of a connector that has no terminal of its own.

A Claude Code agent runs in a tmux pane a person can look at. A Codex app-server or an ACP agent is
a background process speaking JSON over pipes: there is nothing to attach to. Its connector writes
what a turn is doing, as plain lines, to `<work>/state/live-view.log` (`Writer`), and the adapter's
`view` command follows that file (`follow`) for whoever opens the agent's terminal panel.

What is written is what the person would see in a terminal: who the turn is for, the agent's words
as they arrive, each command, file change and tool call with its outcome, and how the turn ended.
No header, token or environment value is written; text is cut to LINE_MAX a line. Writing is
best-effort: a view that cannot be written never fails a turn. Stdlib only.
"""
from __future__ import annotations

import os
import sys
import time

NAME = "live-view.log"
KEEP_BYTES = 1_000_000      # past this the log starts over; the last KEPT_TAIL bytes are carried
KEPT_TAIL = 100_000
LINE_MAX = 2000
SHOWN_FIRST = 60            # lines of the log's end a new viewer starts with
POLL_S = 0.25
WAITING = "Waiting for this agent's next turn. Nothing is typed here: this view is read-only."


def path_of(work) -> str:
    return os.path.join(str(work), "state", NAME)


def _one_line(text, limit=LINE_MAX) -> str:
    flat = " ".join(str(text if text is not None else "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


class Writer:
    """Appends a turn's lines to the log. Every method swallows OSError."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock, self.pending = str(path), clock, ""

    def _write(self, text: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._trim()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, text.encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except OSError:
            pass

    def _trim(self) -> None:
        """Past KEEP_BYTES, replace the log with its own tail (a new file: a follower sees it shrink)."""
        try:
            if os.path.getsize(self.path) <= KEEP_BYTES:
                return
            with open(self.path, "rb") as f:
                f.seek(-KEPT_TAIL, os.SEEK_END)
                tail = f.read()
        except OSError:
            return
        tail = tail[tail.find(b"\n") + 1:]
        fd = os.open(self.path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, tail)
        finally:
            os.close(fd)
        os.replace(self.path + ".new", self.path)

    def line(self, text: str) -> None:
        self.flush()
        self._write(_one_line(text) + "\n")

    def turn(self, who: str, message: str) -> None:
        """A turn begins: who it is for and how their message starts."""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        self.line("")
        self.line("── %s  %s ──" % (stamp, _one_line(who, 80)))
        self.line("> " + _one_line(message, 300))

    def say(self, delta: str) -> None:
        """The agent's words as they arrive: whole lines are written, the rest waits for its end."""
        self.pending += str(delta or "")
        whole, sep, rest = self.pending.rpartition("\n")
        if sep:
            self.pending = rest
            self._write("".join(_one_line(ln) + "\n" for ln in whole.split("\n")))

    def flush(self) -> None:
        if self.pending:
            rest, self.pending = self.pending, ""
            self._write(_one_line(rest) + "\n")

    def step(self, what: str, detail="", outcome="") -> None:
        """One thing the agent did: `what` (ran, edited, searched ...), on `detail`, and how it ended."""
        self.line("• %s %s%s" % (what, _one_line(detail, 400), "  [%s]" % _one_line(outcome, 60) if outcome else ""))

    def done(self, status: str) -> None:
        self.line("── turn %s ──" % _one_line(status or "ended", 60))


def follow(path, out=None, stop=lambda: False, sleep=time.sleep, first=SHOWN_FIRST) -> None:
    """Print the end of the log, then every line added to it, until `stop()`. A log that is not there
    yet is waited for; one that starts over (it shrank, or was replaced) is read again from its start."""
    out = out or sys.stdout
    path, shown, at, ident = str(path), False, 0, None
    while not stop():
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st is None:
            if not shown:
                out.write(WAITING + "\n")
                out.flush()
                shown = True
            sleep(POLL_S)
            continue
        now = (st.st_dev, st.st_ino)
        if ident != now or st.st_size < at:
            at = 0 if ident is not None else _start_of_last(path, st.st_size, first)
            ident = now
        if st.st_size > at:
            try:
                with open(path, "rb") as f:
                    f.seek(at)
                    data = f.read()
            except OSError:
                data = b""
            keep = data.rfind(b"\n") + 1        # a half-written line is left for the next look
            if keep:
                out.write(data[:keep].decode("utf-8", "replace"))
                out.flush()
                at += keep
                shown = True
                continue
        if not shown:
            out.write(WAITING + "\n")
            out.flush()
            shown = True
        sleep(POLL_S)


def _start_of_last(path, size, lines) -> int:
    """The offset where the last `lines` lines of the log begin."""
    try:
        with open(path, "rb") as f:
            back = min(size, lines * (LINE_MAX // 8 + 1))
            f.seek(size - back)
            data = f.read()
    except OSError:
        return 0
    cut = len(data)
    for _ in range(lines + 1):
        cut = data.rfind(b"\n", 0, cut)
        if cut < 0:
            return size - back if back == size else size - back + data.find(b"\n") + 1
    return size - back + cut + 1
