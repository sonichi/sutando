#!/usr/bin/env python3
"""A linked worktree never replaces a live task-event handler.

A worktree run against the live workspace rewrote the handler to the worktree's
router; removing the worktree then left the core holding every task. The publisher
keeps a still-executable handler when it runs from a linked worktree, and still
writes when nothing live is declared or when it runs from a main checkout.

Run: python3 tests/skills/worker-pool/publish-handler-worktree-guard.test.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
SRC = REPO / "skills/worker-pool/scripts/pool_roster.py"

checks: dict[str, bool] = {}


spec = importlib.util.spec_from_file_location("pool_roster_guard", SRC)
pr = importlib.util.module_from_spec(spec)
sys.modules["pool_roster_guard"] = pr
spec.loader.exec_module(pr)


class Checkout:
    """A fake checkout the real publisher believes it runs from (via its `__file__`)."""

    def __init__(self, root: pathlib.Path, git_is_file: bool):
        scripts = root / "skills/worker-pool/scripts"
        scripts.mkdir(parents=True)
        self.file = scripts / "pool_roster.py"
        self.router = scripts / "pool_route_handler.py"
        self.router.write_text("#!/bin/sh\n")
        self.router.chmod(0o755)
        if git_is_file:
            (root / ".git").write_text("gitdir: elsewhere\n")
        else:
            (root / ".git").mkdir()

    def publish(self, ws):
        real = pr.__file__
        pr.__file__ = str(self.file)
        try:
            return pr.publish_task_event_handler(ws)
        finally:
            pr.__file__ = real


def declared(ws: pathlib.Path) -> str:
    return json.loads((ws / "state/task-event-handler.json").read_text())["handler"]


tmp = pathlib.Path(tempfile.mkdtemp(prefix="publish-guard-"))
try:
    ws = tmp / "ws"
    (ws / "state").mkdir(parents=True)
    main, wt = Checkout(tmp / "main", False), Checkout(tmp / "wt", True)
    main_router, wt_router = main.router, wt.router

    main.publish(ws)
    checks["a main checkout declares its router"] = declared(ws) == str(main_router.resolve())

    wt.publish(ws)
    checks["a worktree keeps the live main router"] = declared(ws) == str(main_router.resolve())

    main_router.unlink()
    wt.publish(ws)
    checks["a worktree may declare when the declared router is gone"] = (
        declared(ws) == str(wt_router.resolve()))

    main_router.write_text("#!/bin/sh\n")
    main_router.chmod(0o755)
    main.publish(ws)
    checks["a main checkout replaces a worktree's router"] = declared(ws) == str(main_router.resolve())

    fresh = tmp / "fresh"
    (fresh / "state").mkdir(parents=True)
    wt.publish(fresh)
    checks["a worktree declares when nothing is declared yet"] = declared(fresh) == str(wt_router.resolve())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if all(checks.values()) else 1)
