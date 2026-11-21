"""How a connector started `--held` under a launcher begins: inside the held root, once attached.

The launcher (connect_common's holder sequence) holds the work root and attaches its child as the
consumer whose liveness fences the root. The child takes no lock of its own: it fchdirs into the held
root's descriptor, so a renamed or replaced pathname never reaches it, and it starts only once the
holder's record names this very process (pid and start time) and verifies it alive. A hand start
with --held, a stale record naming a reused pid, or one whose lineage cannot be read serves nothing.

Package level so every connector shares it without importing another adapter. Stdlib only.
"""
from __future__ import annotations

import os
import sys
import time

from .task_io import holder_consumer, start_time, stop_requested

WAIT_S = 30.0
POLL_S = 0.05


def enter(root_fd, wait_s=WAIT_S, consumer_of=holder_consumer):
    """None once this process is inside the held root and attached; else 2, after one line on stderr.
    `consumer_of(".")` reads the holder's consumer record, passed in so a caller's own name for it is used."""
    try:
        os.fchdir(root_fd)
    except OSError as e:
        sys.stderr.write("refusing to start: --root-fd %d is not the held root's directory (%s)\n" % (root_fd, e))
        return 2
    deadline = time.monotonic() + wait_s
    while True:
        consumer = consumer_of(".")
        if consumer and consumer.get("pid") == os.getpid():
            if consumer.get("live") is True and consumer.get("start") == start_time(os.getpid()):
                return None
            why = ("a start time that is not this process's" if consumer.get("start") != start_time(os.getpid())
                   else "liveness unknown" if consumer.get("live") is None else "liveness false")
            sys.stderr.write("refusing to start: --held, but the holder of %s names this pid with %s, not verified\n"
                             % (os.getcwd(), why))
            return 2
        if stop_requested() or time.monotonic() >= deadline:
            sys.stderr.write("refusing to start: --held, but no holder of %s attached this process\n" % os.getcwd())
            return 2
        time.sleep(POLL_S)
