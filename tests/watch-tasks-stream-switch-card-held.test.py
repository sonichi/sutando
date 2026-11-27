#!/usr/bin/env python3
"""A non-owner's account/model ask whose one line cannot be written is held, never announced to the core,
and answered by the held retry of the same running watcher once the line can be written (review on #6877).

Run: python3 tests/watch-tasks-stream-switch-card-held.test.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from readiness_window_helpers import REPO, pump, start, stop, wait_ready, watcher_env, workspace  # noqa: E402

sys.path.insert(0, str(REPO / "src"))
import card_words  # noqa: E402
import task_envelope  # noqa: E402

if os.name == "nt" or os.geteuid() == 0:
    print("ok - skipped: needs a POSIX non-root user (an unwritable results dir)")
    raise SystemExit(0)

TASK = ("id: task-team\nsource: ag2space\nchannel_id: !room:s.space\ntask_layout: mid\n"
        "task: which account are you on?\nroom_name: R\nchannel_kind: room\nsource_message_id: $evt\n"
        "user_id: @teammate:s.space\ninteraction_type: message\naccess_tier: team\n")


def main() -> int:
    tmp, ws, b = workspace("switch-card-held-")
    task_envelope.load_or_create_key(ws)
    results = ws / "results"
    env = watcher_env(tmp, ws, b, {"SUTANDO_HELD_RETRY_INTERVAL": "2"})
    p = start(ws, env)
    out: list = []
    failures = []
    try:
        wait_ready(ws)
        results.chmod(0o555)            # the line cannot be written
        final = ws / "tasks" / "task-team.txt"
        staged = final.with_name(".task-team.tmp")
        staged.write_text(task_envelope.stamp_text(TASK, ws))
        staged.replace(final)
        pump(p, out, lambda: False, timeout=3, settle=0)
        if (results / "task-team.txt").exists():
            failures.append("a result was written while results/ was read-only")
        results.chmod(0o755)            # writable again: the held retry answers it, same watcher
        answered = pump(p, out, lambda: (results / "task-team.txt").exists(), timeout=12, settle=1.0)
        if not answered:
            failures.append("the held task was not answered by the running watcher's retry")
        elif (results / "task-team.txt").read_text() != card_words.ACCOUNT_OWNER_ONLY + "\n":
            failures.append("wrong line: %r" % (results / "task-team.txt").read_text())
        if any("task-team" in line for line in out):
            failures.append("announced to the core: %r" % out)
    finally:
        try:
            results.chmod(0o755)
        except OSError:
            pass
        stop(p)
    for f in failures:
        print("FAIL", f)
    if not failures:
        print("ok - held while unwritable, never announced, answered by the same watcher's retry")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
