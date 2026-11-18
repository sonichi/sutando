#!/usr/bin/env python3
"""A stub holder_lock caller for the first-act contract: acquire, publish, then act on the root.

    holder_lock_stub.py --module src|vendored --work ROOT [--spawn-before-publish] [--by-path]
                        [--release-in-handler] [--linger SECONDS]

Prints one line: `acquired`, or the refusal's class and reason; exits 0, 3 on a refusal, 2 on a
LockError. While holding it waits for stdin to close or SIGTERM's flag, then releases: one finally
covers everything after publish, so a signal at any point after it still releases.
"""
import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_STOP = []


def load(kind):
    if kind == "src":
        sys.path.insert(0, str(REPO / "src"))
        import holder_lock
        return holder_lock
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    from ag2_sparrow import holder_lock
    return holder_lock


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", choices=("src", "vendored"), required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--spawn-before-publish", action="store_true")
    ap.add_argument("--by-path", action="store_true", help="a faulty first act by pathname")
    ap.add_argument("--release-in-handler", action="store_true",
                    help="a faulty SIGTERM handler that calls release instead of setting a flag")
    ap.add_argument("--linger", type=float, default=0.0,
                    help="seconds to stay busy right after printing `acquired` (widens the SIGTERM window)")
    args = ap.parse_args(argv)
    hl = load(args.module)
    try:
        result = hl.acquire(args.work, install_id=hl.install_id())
    except hl.LockError as e:
        print("LockError %s" % e, flush=True)
        return 2
    if not isinstance(result, hl.Acquired):
        print("%s %s" % (type(result).__name__, getattr(result, "reason", getattr(result, "pid", ""))), flush=True)
        return 3
    if args.spawn_before_publish:
        subprocess.run(["true"], check=True)
    if args.release_in_handler:
        signal.signal(signal.SIGTERM, lambda *_: hl.release(result.owner))
    else:
        signal.signal(signal.SIGTERM, lambda signum, frame: _STOP.append(signum))
    try:
        hl.publish(result.owner, "stub")
        if args.by_path:
            os.makedirs(os.path.join(args.work, "state"), exist_ok=True)
        else:
            try:
                os.mkdir("state", 0o755, dir_fd=result.owner.root_fd)
            except FileExistsError:
                pass
        subprocess.run(["true"], check=True)
        print("acquired", flush=True)
        time.sleep(args.linger)
        _wait_for_stdin_eof_or_stop()
    finally:
        hl.release(result.owner)
    return 0


def _wait_for_stdin_eof_or_stop():
    """The main loop: one short select per step, ending on stdin EOF or the SIGTERM flag."""
    while not _STOP:
        if select.select([0], [], [], 0.05)[0] and not os.read(0, 4096):
            return


if __name__ == "__main__":
    sys.exit(main())
