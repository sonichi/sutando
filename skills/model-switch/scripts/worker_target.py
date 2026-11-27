#!/usr/bin/env python3
"""Whose session a model switch targets, and whether a worker's may be switched.

`/model` saves the pick as the default of the Claude folder the session RUNS in. A pool worker
normally runs in the core's folder, so switching it there would change the core's default and every
other worker's. A worker is switched only when its running session is in a folder of its own
(`skills/worker-pool/scripts/worker_claude_folder.py`); everything else is refused, never sent to the
core instead.

  worker_target.py resolve --session S --session-source flag|env|descriptor [--target core|worker]
                           [--caller-worker ID]
      prints `core` or `worker <id>`; exit 14 when the target cannot be told.
  worker_target.py folder --core-folder DIR --worker ID --session S --pids "PID ..."
      prints the worker's own Claude folder when every running process of the session uses it;
      exit 15 otherwise (the core's folder, another one, or an environment that cannot be read).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
POOL = REPO / "skills" / "worker-pool" / "scripts"
UNIDENTIFIED_RC, NOT_OWN_FOLDER_RC = 14, 15
PREFIX = "sutando-worker-"   # a pool worker's tmux session; checked against the pool's own naming


class Refused(Exception):
    pass


def _pool(name: str):
    if not (POOL / f"{name}.py").is_file():
        raise Refused("the worker-pool skill is not installed, so a worker's session cannot be checked")
    for path in (str(REPO / "src"), str(POOL)):
        if path not in sys.path:
            sys.path.insert(0, path)
    return __import__(name)


def resolve(session: str, source: str, target: str = "", caller_worker: str = "") -> tuple:
    """("core", "") or ("worker", id). Raises Refused when the session and what the caller says
    disagree, or name no single worker."""
    named = session[len(PREFIX):] if session.startswith(PREFIX) else None
    if named is None:
        if target == "worker":
            raise Refused(f"--target worker, but session '{session}' is not a worker's")
        if caller_worker and target != "core":
            raise Refused(f"run from worker {caller_worker}'s session, but the target resolved to "
                          f"'{session}', not that worker's session; not switching the core in its place "
                          "(--target core switches the core on purpose)")
        return "core", ""
    if target == "core":
        raise Refused(f"--target core, but session '{session}' is a worker's")
    identity = _pool("worker_identity")
    if not identity.WORKER_ID_RE.match(named) or identity.tmux_session_name(named) != session:
        raise Refused(f"session '{session}' names no worker id")
    if source != "flag" and caller_worker and caller_worker != named:
        raise Refused(f"the session names worker {named} but this is worker {caller_worker}'s session; "
                      "pass --session to name the one to switch")
    return "worker", named


def _session_env(pid: str, ps_runner=None) -> dict:
    quota = _core("quota_availability")
    try:
        proc = (ps_runner or quota._run_ps)(pid)
    except Exception as e:  # noqa: BLE001 -- a probe failure is unknown, never a pass
        raise Refused(f"process {pid} could not be read ({type(e).__name__})")
    if proc is None or getattr(proc, "returncode", 1) != 0:
        raise Refused(f"process {pid} could not be read")
    return quota._env_pairs((proc.stdout or "").strip())


def _core(name: str):
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    return __import__(name)


def own_folder(core_folder: str, worker: str, session: str, pids: list, ps_runner=None) -> Path:
    """The worker's own Claude folder, only when every running process of `session` runs in it."""
    restart = (f"give worker {worker} a Claude folder of its own "
               f"(skills/worker-pool/scripts/worker_claude_folder.py ensure <core folder> {worker}) "
               "and restart it so its session runs there")
    try:
        own = _pool("worker_claude_folder").folder(core_folder, worker)
    except ValueError as e:
        raise Refused(f"worker {worker} can have no folder of its own ({e})")
    if not pids:
        raise Refused(f"no running Claude process was found for session '{session}', so the folder it "
                      f"uses could not be read; {restart}")
    live = set()
    for pid in pids:
        env = _session_env(pid, ps_runner)
        # The launcher stamps the session on its process: without it this is not that session's
        # environment (or it could not be read), and nothing in it is trusted.
        if env.get("SUTANDO_TMUX_SESSION") != session:
            raise Refused(f"the environment of process {pid} could not be read as session '{session}''s; {restart}")
        folder = env.get("CLAUDE_CONFIG_DIR") or ""
        if not folder or not Path(folder).is_dir():
            raise Refused(f"the Claude folder process {pid} runs in could not be read; {restart}")
        live.add(os.path.realpath(folder))
    if len(live) != 1:
        raise Refused(f"session '{session}' has processes in more than one Claude folder; {restart}")
    running = live.pop()
    if running == os.path.realpath(core_folder):
        raise Refused(f"worker {worker}'s running session uses the core's Claude folder, so /model there "
                      f"would change the core's default and every other worker's; {restart}")
    if running != os.path.realpath(own):
        raise Refused(f"worker {worker}'s running session uses a Claude folder that is not its own; {restart}")
    settings = own / "settings.json"
    if settings.is_symlink() or not settings.is_file():
        raise Refused(f"worker {worker}'s folder has no settings file of its own; {restart}")
    return own


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worker_target.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--session", required=True)
    r.add_argument("--session-source", choices=("flag", "env", "descriptor"), required=True)
    r.add_argument("--target", default="")
    r.add_argument("--caller-worker", default="")
    f = sub.add_parser("folder")
    f.add_argument("--core-folder", required=True)
    f.add_argument("--worker", required=True)
    f.add_argument("--session", required=True)
    f.add_argument("--pids", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "resolve":
            if a.target not in ("", "core", "worker"):
                raise Refused(f"--target is core or worker, not '{a.target}'")
            print(" ".join(resolve(a.session, a.session_source, a.target, a.caller_worker)).strip())
            return 0
        print(own_folder(a.core_folder, a.worker, a.session, a.pids.split()))
        return 0
    except Refused as e:
        print(str(e), file=sys.stderr)
        return UNIDENTIFIED_RC if a.cmd == "resolve" else NOT_OWN_FOLDER_RC
    except Exception as e:  # noqa: BLE001 -- anything unexpected is a refusal, never a pass
        print(f"the target could not be checked ({type(e).__name__}: {e})", file=sys.stderr)
        return UNIDENTIFIED_RC if a.cmd == "resolve" else NOT_OWN_FOLDER_RC


if __name__ == "__main__":
    sys.exit(main())
