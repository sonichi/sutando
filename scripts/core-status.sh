#!/usr/bin/env bash
# Bash wrapper around workspace_default.write_status.
#
# Shell callers use this instead of `echo '{...}' > core-status.json`. The
# redirect truncates before it writes, so a reader polling in that window sees
# a zero-length file; graceful-restart's busy() gate read that as "idle" and
# authorised a kill (sonichi/sutando#3156). This wrapper writes via temp-file +
# os.replace, so the swap is atomic and no reader can observe a partial record.
#
# It also stamps `ts` itself, so a caller cannot forget it or format it wrong,
# and resolves the workspace through the same loader every reader uses — the
# split-brain class scripts/sutando-config.sh exists to prevent.
#
# Every write also carries `pending` (the queue, from src/task_queue.py) and
# refreshes state/task-queue.json, so a status transition is also a fresh
# queue for anyone who asks "how much is waiting".
#
# Usage:
#   bash scripts/core-status.sh running "reviewing PR #123"
#   bash scripts/core-status.sh idle
set -euo pipefail

STATUS="${1:?usage: core-status.sh <status> [step]}"
STEP="${2-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Bare `python3` is Apple's stub on a Mac without Command Line Tools: executing
# it raises the install modal, and this wrapper runs on EVERY status transition.
. "$REPO_ROOT/scripts/python-binary.sh"
PY_BIN="$(resolve_python "$REPO_ROOT")"
if [ -z "$PY_BIN" ]; then
	echo "core-status: no runnable python3 — status NOT written" >&2
	exit 1
fi

# Quoted heredoc + argv: REPO_ROOT is data, never spliced into Python source.
exec "$PY_BIN" - "$REPO_ROOT" "$STATUS" "$STEP" <<'PY'
import importlib.util, sys, time
from pathlib import Path

repo_root, status, step = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location(
    "wd", str(Path(repo_root) / "src" / "workspace_default.py"))
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)
ws = wd.resolve_workspace()

payload = {"status": status, "ts": int(time.time())}
if step:
    payload["step"] = step
# Additive: readers of status/ts/step are unaffected; a queue that cannot be
# counted must never stop the status write, which is a liveness signal.
try:
    tq_spec = importlib.util.spec_from_file_location(
        "tq", str(Path(repo_root) / "src" / "task_queue.py"))
    tq = importlib.util.module_from_spec(tq_spec)
    tq_spec.loader.exec_module(tq)
    payload["pending"] = tq.pending(ws)
    tq.write_snapshot(ws)
except Exception as exc:
    print(f"core-status: pending queue not counted: {exc}", file=sys.stderr)
print(wd.write_status("core-status.json", payload, workspace=ws))

# `idle` closes a proactive-loop pass, so stamp the liveness marker
# check_cron_schedule reads. core-status.json is NOT a substitute: owner turns
# refresh it, so it stays fresh through a dead schedule.
if status == "idle":
    try:
        m = wd.resolve_workspace() / "state" / "last-loop-ok"
        m.parent.mkdir(parents=True, exist_ok=True)
        m.touch()
    except Exception as exc:
        print(f"core-status: last-loop-ok not stamped: {exc}", file=sys.stderr)
PY
