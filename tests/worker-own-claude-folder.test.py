#!/usr/bin/env python3
"""A worker given a Claude folder of its own keeps its account and model there, and nothing else.

  a) no folder made -> the worker starts in the core's, as before
  b) a made folder links the core's entries and keeps .claude.json and settings.json as real files
  c) the core's account is never copied; the worker's own account and model survive a refresh
  d) the core's settings and newly trusted folders reach the worker on the next launch
  e) an entry the core gained is linked, one it lost is unlinked, a worker's real entry is kept
  f) a worker id that could leave the folder is refused

Run: python3 tests/worker-own-claude-folder.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
import worker_claude_folder as F  # noqa: E402

FAILS: list[str] = []
WORKER = "0123456789abcdef0123456789abcdef"


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def core_folder() -> Path:
    core = Path(tempfile.mkdtemp(prefix="wcf-")) / ".claude-core"
    (core / "skills").mkdir(parents=True)
    (core / "projects").mkdir()
    (core / "settings.json.bak-1").write_text("{}")
    (core / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "oauthAccount": {"emailAddress": "core@example.com"},
        "projects": {"/work": {"hasTrustDialogAccepted": True}}}))
    (core / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": []}}))
    return core


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def main() -> int:
    core = core_folder()
    check(F.launch(core, WORKER) == core, "a) a worker with no folder of its own starts in the core's")
    check(not F.folder(core, WORKER).exists(), "a) asking where to start makes no folder")

    own = F.ensure(core, WORKER)
    check(own == core.with_name(".claude-core-workers") / WORKER, f"b) the folder is a sibling of the core's, got {own}")
    check((own / "skills").is_symlink() and (own / "projects").is_symlink()
          and (own / "projects").resolve() == (core / "projects").resolve(), "b) skills and projects are the core's own")
    check(not (own / ".claude.json").is_symlink() and not (own / "settings.json").is_symlink(),
          "b) the account file and the settings are this worker's own files")
    check(not (own / "settings.json.bak-1").exists(), "b) the core's backups are not followed")
    check("oauthAccount" not in read(own / ".claude.json") and read(own / ".claude.json")["hasCompletedOnboarding"] is True,
          "c) a new folder has the core's first-run answers and no account")
    check(read(own / "settings.json")["model"] == "opus", "c) a worker that never chose a model has the core's")

    acct = read(own / ".claude.json")
    acct["oauthAccount"] = {"emailAddress": "worker@example.com"}
    (own / ".claude.json").write_text(json.dumps(acct))
    (own / "settings.json").write_text(json.dumps({"model": "sonnet"}))
    (core / "settings.json").write_text(json.dumps({"model": "haiku", "hooks": {"Stop": ["new"]}}))
    cf = read(core / ".claude.json")
    cf["projects"]["/later"] = {"hasTrustDialogAccepted": True}
    (core / ".claude.json").write_text(json.dumps(cf))
    (core / "plugins").mkdir()
    (core / "skills").rmdir()
    (own / "notes").mkdir()
    check(F.launch(core, WORKER) == own, "d) a worker with a folder of its own starts in it")
    check(read(own / ".claude.json")["oauthAccount"] == {"emailAddress": "worker@example.com"},
          "c) the worker's own account survives a refresh")
    st = read(own / "settings.json")
    check(st["model"] == "sonnet" and st["hooks"] == {"Stop": ["new"]},
          f"d) the worker keeps its model and gets the core's current hooks, got {st}")
    check("/later" in read(own / ".claude.json")["projects"], "d) a folder the core trusted since is trusted here")
    check((own / "plugins").is_symlink(), "e) an entry the core gained is linked")
    check(not (own / "skills").exists() and not (own / "skills").is_symlink(), "e) a link to an entry the core lost is removed")
    check((own / "notes").is_dir() and not (own / "notes").is_symlink(), "e) the worker's own real entry is kept")
    check(read(core / ".claude.json")["oauthAccount"] == {"emailAddress": "core@example.com"}
          and read(core / "settings.json")["model"] == "haiku", "c) nothing of the core's is changed")

    for bad in ("", "../x", "a/b", ".."):
        try:
            F.folder(core, bad)
            check(False, f"f) {bad!r} is refused")
        except ValueError:
            check(True, f"f) {bad!r} is refused")
        check(F.launch(core, bad) == core, f"f) {bad!r} starts in the core's folder")
    check(F.main(["x", "launch", str(core), WORKER]) == 0 and F.main(["x", "nope"]) == 2
          and F.main(["x", "ensure", str(core / "absent"), WORKER]) == 1, "f) the command line answers 0, 2 and 1")
    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
