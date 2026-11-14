#!/usr/bin/env python3
"""Copy stdin to stdout line by line, never letting a slow stdout stall stdin.

The watcher puts this between fswatch and its event FIFO. fswatch 1.18 on macOS
segfaults when a burst backs up behind a full pipe, and its batched writes can
exceed PIPE_BUF and interleave with the sweep's lines on the same FIFO. Here stdin
is drained at once into memory, and each line leaves in its own write(), which a
pipe keeps whole up to PIPE_BUF (512 bytes on macOS).
"""
from __future__ import annotations

import os
import sys
import threading
from collections import deque


def relay(src, dst_fd: int) -> None:
    lines: deque = deque()
    ready = threading.Condition()
    done = [False]

    def drain() -> None:
        for line in iter(src.readline, b""):
            with ready:
                lines.append(line if line.endswith(b"\n") else line + b"\n")
                ready.notify()
        with ready:
            done[0] = True
            ready.notify()

    threading.Thread(target=drain, daemon=True).start()
    while True:
        with ready:
            while not lines and not done[0]:
                ready.wait()
            if not lines:
                return
            line = lines.popleft()
        view = memoryview(line)
        while view:
            view = view[os.write(dst_fd, view):]


def main() -> int:
    try:
        relay(sys.stdin.buffer, sys.stdout.fileno())
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
