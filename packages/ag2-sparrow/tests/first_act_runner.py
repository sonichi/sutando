#!/usr/bin/env python3
"""Runs one adapter's real main for the holder_lock FirstActContract (test helper, not shipped).

    first_act_runner.py routine|codex|cloud <work root> <lock home>

Prints `acquired` once the adapter is serving (after publishing its record and running one short
child, the caller's own after-publish child), or the refusing result's class name when main
returns non-zero.
Once serving, closing stdin stops the adapter with SIGTERM: its handler sets the flag, its loop
returns and its main releases the lock; that stop exits 0 here.
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import holder_lock  # noqa: E402


def main():
    adapter, root, home = sys.argv[1:4]
    from ag2_sparrow import task_io
    if not hasattr(task_io, "LockRefused"):
        print("this package's adapters do not take the shared lock", flush=True)
        return 2
    holder_lock._home = lambda: home
    seen = {}
    real_acquire, real_publish = holder_lock.acquire, holder_lock.publish

    def acquire(*a, **k):
        result = real_acquire(*a, **k)
        seen["result"] = type(result).__name__
        return result

    def publish(owner, name):
        out = real_publish(owner, name)
        subprocess.run(["true"], check=True)
        return out
    holder_lock.acquire, holder_lock.publish = acquire, publish
    from ag2_sparrow import relay_loop
    from ag2_sparrow.adapters.codex import codex_connector

    def serving(real):
        def serve(self, *a, **k):
            print("acquired", flush=True)
            threading.Thread(target=lambda: (sys.stdin.read(), os.kill(me, signal.SIGTERM)), daemon=True).start()
            return real(self, *a, **k)
        return serve
    relay_loop.RelayLoop.serve = serving(relay_loop.RelayLoop.serve)
    codex_connector.Connector.serve = serving(codex_connector.Connector.serve)
    me = os.getpid()
    if adapter == "routine":
        from ag2_sparrow.adapters.claude_routine import routine_adapter as ra
        rc = ra.main(["--work", root, "--trigger-id", "trig_01FIRSTACT", "--poll", "0.05"],
                     {"CLAUDE_ROUTINE_TOKEN": "t"})
    elif adapter == "codex":
        from ag2_sparrow.adapters.codex import codex_connector as cc
        rc = cc.main(["--server", "stub", "--work", root, "--poll", "0.05"])
    else:
        from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa
        rc = sa.main(["--work", root, "--session-id", "session_X", "--claude", sys.executable, "--poll", "0.05"],
                     {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()})
    if rc and seen.get("result") not in (None, "Acquired"):
        print(seen["result"], flush=True)
    return 0 if rc == 128 + signal.SIGTERM else rc


if __name__ == "__main__":
    sys.exit(main())
