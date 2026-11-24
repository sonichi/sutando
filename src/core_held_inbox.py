#!/usr/bin/env python3
"""The core's inbox while a provider limit keeps the core from launching at all.

start-cli.sh refuses to launch a Claude Code core under a limit hold (core_limit_hold gate, exit 75),
and then nothing reads tasks/: the owner's messages get no answer and no card. This loop runs in the
core's place until the core is up again, started by start-cli.sh on that refusal:

- the owner's bare "switch account / model / core" gets its card (switch_card_intercept);
- every other owner message gets the account card, once each, for a usage limit (core_quota_notice),
  and stays queued for the core to answer once it is up;
- "retry now" and "switch to <model>" are acted on (core_limit_hold.handle_owner_actions);
- once the hold is lifted (by those, by an account change, or by its own time), the core is launched
  through start-cli.sh, at most once a minute. A launch refused again keeps the loop running; a launch
  that starts the core ends it.

One loop per workspace (a lock under state/); it holds no task and writes no hold of its own.
CLI: core_held_inbox.py [--workspace W] [--repo R] [--interval S] [--once]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
sys.path.insert(0, str(_SRC))

HOLD_EXIT = 75
LAUNCH_EVERY_S = 60
LOCK = ("state", "core-held-inbox.lock")


def _log(*parts) -> None:
    print("core-held-inbox:", *parts, file=sys.stderr, flush=True)


def launch_core(repo: Path) -> int:
    """start-cli.sh's own answer: 0 the core is up, 75 still held, other a failure."""
    env = {k: v for k, v in os.environ.items() if k not in ("SUTANDO_INSTANCE_ID", "GR_RID")}
    try:
        return subprocess.run(["bash", str(repo / "src" / "agent" / "start-cli.sh")], cwd=str(repo), env=env,
                              stdin=subprocess.DEVNULL, timeout=600).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        _log("launch failed:", type(exc).__name__)
        return 1


class HeldInbox:
    def __init__(self, workspace: Path, repo: Path = _REPO, *, now: Callable[[], float] = time.time,
                 launch: Optional[Callable[[Path], int]] = None, quota=None, intercept=None, log=_log):
        import core_quota_notice
        self.ws, self.repo, self.now, self.log = Path(workspace), Path(repo), now, log
        self.launch = launch or launch_core
        self.seen: set = set()
        self.last_launch = 0.0
        script = self.repo / "skills" / "account-switch" / "scripts" / "account_card.py"
        self.quota = quota if quota is not None else core_quota_notice.QuotaNotice(
            self.ws, "Claude Code", core_quota_notice.card_sender([sys.executable, str(script)], self.ws, log),
            log, runtime="claude")
        if intercept is None:
            import switch_card_intercept
            intercept = lambda p: switch_card_intercept.intercept(p, self.ws, self.ws / "results", self.repo)  # noqa: E731
        self.intercept = intercept

    def pending(self) -> list:
        try:
            import task_queue
            return list(task_queue.pending_files(self.ws))
        except Exception:  # noqa: BLE001 - no queue read: nothing to do this tick
            return []

    def step(self) -> str:
        """One tick. 'launched' when the core came up (the loop ends), else what it did."""
        import core_limit_hold as clh
        import core_quota_notice
        at = self.now()
        lifted = clh.release_on_account_change(self.ws, at)
        hold = clh.active_hold(self.ws, at)
        if hold:
            for path in self.pending():
                if path.name in self.seen:
                    continue
                self.seen.add(path.name)
                try:
                    self.intercept(path)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{path.name}: switch card failed ({type(exc).__name__})")
            if hold.get("kind") == "usage-limit":
                reset_at = hold.get("reset_at")
                reset = core_quota_notice.reset_from_epoch(reset_at, at) if reset_at else ""
                done = self.quota.hit(reset, reset_at, tasks=core_quota_notice.owner_waiting(self.ws))
                if done:
                    self.log(f"quota: {done}")
            switch = self.repo / "scripts" / "switch-model.sh"
            clh.handle_owner_actions(self.ws, at, switch_cmd=str(switch) if switch.is_file() else None)
            hold = clh.active_hold(self.ws, at)
        if hold:
            return "held"
        if at - self.last_launch < LAUNCH_EVERY_S:
            return "waiting"
        self.last_launch = at
        self.log("the hold is lifted" + (" (another account)" if lifted else "") + "; launching the core")
        rc = self.launch(self.repo)
        if rc == 0:
            return "launched"
        self.log(f"the core did not launch (rc={rc}); still watching its inbox")
        return "held" if rc == HOLD_EXIT else "failed"


def _lock(workspace: Path):
    import fcntl
    path = Path(workspace).joinpath(*LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="")
    ap.add_argument("--repo", default=str(_REPO))
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    lock = _lock(ws)
    if lock is None:
        _log("another loop already watches this inbox")
        return 0
    inbox = HeldInbox(ws, Path(a.repo))
    _log(f"watching {ws / 'tasks'} while the core is held")
    while True:
        try:
            state = inbox.step()
        except Exception as exc:  # noqa: BLE001 - the loop outlives a bad tick
            _log("tick failed:", type(exc).__name__, exc)
            state = "failed"
        if state == "launched" or a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
