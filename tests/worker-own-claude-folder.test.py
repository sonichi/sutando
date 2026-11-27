#!/usr/bin/env python3
"""A worker given a Claude folder of its own keeps its account and model there, and nothing else.

  a) no folder made -> the worker starts in the core's, as before
  b) a made folder links the core's entries and keeps .claude.json and settings.json as real files
  c) the core's account is never copied; the worker's own account and model survive a refresh
  d) the core's settings and newly trusted folders reach the worker on the next launch
  e) an entry the core gained is linked, one it lost is unlinked, a worker's real entry is kept
  f) a worker id that could leave the folder is refused
  g) the core's file-backed sign-in never reaches a worker: only SHARED entries are linked, a stale
     link to the core's .credentials.json is removed, a worker's own .credentials.json is kept
  h) the worker's .claude.json is private (0600); a damaged one is refused, not replaced
  i) the launcher keeps the core's folder only for a worker with no folder of its own; one whose
     folder cannot be made ready is refused (bash-level, with the helper stubbed to fail)

Run: python3 tests/worker-own-claude-folder.test.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
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
    file_auth_isolation()
    private_account_file()
    launcher_refuses_a_failed_folder()
    launcher_refuses_an_undeterminable_folder()
    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


def file_auth_isolation() -> None:
    core = core_folder()
    dummy = '{"claudeAiOauth": {"accessToken": "dummy-not-a-token"}}'
    (core / ".credentials.json").write_text(dummy)
    (core / ".credentials.source.json").write_text("{}")
    (core / "statsig").mkdir()
    own = F.ensure(core, WORKER)
    names = {p.name for p in own.iterdir()}
    check(".credentials.json" not in names and ".credentials.source.json" not in names,
          f"g) the core's file-backed sign-in is neither linked nor copied, got {sorted(names)}")
    check(not any(p.is_symlink() and p.name not in F.SHARED for p in own.iterdir()),
          "g) only allowlisted entries are links")
    check(not any(p.is_file() and not p.is_symlink() and p.read_text() == dummy for p in own.rglob("*")
                  if not p.is_symlink()), "g) no file in the worker's folder carries the core's credential")
    check("statsig" not in names and (own / "skills").is_symlink(), "g) an entry not in SHARED stays the core's alone")

    os.symlink(core / ".credentials.json", own / ".credentials.json")      # an older version's link
    os.symlink(core / ".credentials.source.json", own / "creds-alias")
    F.launch(core, WORKER)
    check(not os.path.lexists(own / ".credentials.json") and not os.path.lexists(own / "creds-alias"),
          "g) a refresh removes a stale link to the core's credential file, under any name")
    check((core / ".credentials.json").read_text() == dummy, "g) the core's own credential file is untouched")

    (own / ".credentials.json").write_text('{"worker": "own"}')
    F.launch(core, WORKER)
    check(not (own / ".credentials.json").is_symlink()
          and (own / ".credentials.json").read_text() == '{"worker": "own"}',
          "g) the worker's own regular .credentials.json is kept across a refresh")


def private_account_file() -> None:
    core = core_folder()
    own = F.ensure(core, WORKER)
    mode = stat.S_IMODE((own / ".claude.json").stat().st_mode)
    check(mode == 0o600, f"h) a new .claude.json is 0600, got {oct(mode)}")
    os.chmod(own / ".claude.json", 0o644)
    F.ensure(core, WORKER)
    check(stat.S_IMODE((own / ".claude.json").stat().st_mode) == 0o600, "h) a refresh makes a 0644 one private again")
    (own / ".claude.json").write_text("{not json")
    try:
        F.launch(core, WORKER)
        check(False, "h) a damaged .claude.json is refused")
    except ValueError as e:
        check("move it aside" in str(e), f"h) a damaged .claude.json is refused with a fix, got {e}")
    check((own / ".claude.json").read_text() == "{not json", "h) the damaged file is left as it was")
    check(F.main(["x", "launch", str(core), WORKER]) == 1, "h) the command line answers 1 for it")
    (own / ".claude.json").write_text("[]")
    check(F.main(["x", "launch", str(core), WORKER]) == 1, "h) a .claude.json that is not an object is refused")
    os.unlink(own / ".claude.json")
    os.symlink(core / ".claude.json", own / ".claude.json")
    F.launch(core, WORKER)
    check(not (own / ".claude.json").is_symlink() and "oauthAccount" not in read(own / ".claude.json"),
          "h) a .claude.json linked to the core's is replaced by the worker's own, without the core's account")
    other = core.with_name(".claude-core-workers") / "w2"
    other.parent.mkdir(exist_ok=True)
    other.write_text("not a folder")
    check(F.main(["x", "launch", str(core), "w2"]) == 1, "h) a worker path that is not a folder is a failure, not 'none'")


def _resolve(core: Path, worker: str, py: str) -> "subprocess.CompletedProcess[str]":
    lib = REPO / "skills" / "worker-pool" / "scripts" / "worker-claude-folder.sh"
    return subprocess.run(["bash", "-c", '. "$1"; worker_claude_folder "$2" "$3" "$4" "$5"', "x",
                           str(lib), str(REPO), py, str(core), worker], capture_output=True, text=True)


def launcher_refuses_a_failed_folder() -> None:
    core = core_folder()
    stub = core.parent / "failing-python"
    stub.write_text("#!/bin/sh\necho 'stub: refresh failed' >&2\nexit 1\n")
    stub.chmod(0o755)
    r = _resolve(core, WORKER, str(stub))
    check(r.returncode == 0 and r.stdout.strip() == str(core),
          f"i) no folder of its own + failing helper: the core's folder, got {r.returncode} {r.stdout!r}")
    r = _resolve(core, WORKER, "")
    check(r.returncode == 0 and r.stdout.strip() == str(core), "i) no folder of its own + no Python: the core's folder")
    own = F.ensure(core, WORKER)
    r = _resolve(core, WORKER, str(stub))
    check(r.returncode == 1 and r.stdout == "", f"i) its own folder + failing helper: refused, got {r.returncode} {r.stdout!r}")
    check("stub: refresh failed" in r.stderr and str(own) in r.stderr and "core's account" in r.stderr,
          "i) the refusal says what failed and how to fix it")
    r = _resolve(core, WORKER, "")
    check(r.returncode == 1 and r.stdout == "", "i) its own folder + no Python to refresh it: refused")
    r = _resolve(core, WORKER, sys.executable)
    check(r.returncode == 0 and r.stdout.strip() == str(own), "i) its own folder + working helper: its own folder")
    (own / ".claude.json").write_text("{damaged")
    r = _resolve(core, WORKER, sys.executable)
    check(r.returncode == 1 and "move it aside" in r.stderr, "i) a damaged .claude.json refuses the launch with the fix")
    launcher = (REPO / "skills" / "worker-pool" / "scripts" / "launch-worker-session.sh").read_text()
    block = launcher.split("worker_claude_folder \"$REPO\"", 1)[1].split("resolve_claude_settings_args", 1)[0]
    check("worker-claude-folder.sh" in launcher and "exit 1" in block.split("fi", 1)[0],
          "i) the launcher exits before launching when the folder is refused")


def launcher_refuses_an_undeterminable_folder() -> None:
    """A failed path test is not proof of absence: if the workers' folder can't be searched, refuse."""
    if os.geteuid() == 0:
        print("SKIP j) running as root: permissions are not enforced")
        return
    core = core_folder()
    stub = core.parent / "failing-python-j"
    stub.write_text("#!/bin/sh\necho 'stub: refresh failed' >&2\nexit 1\n")
    stub.chmod(0o755)
    own = F.ensure(core, WORKER)
    workers = own.parent
    workers.chmod(0o000)
    try:
        r = _resolve(core, WORKER, str(stub))
        check(r.returncode == 1 and r.stdout == "",
              f"j) its own folder unsearchable + failing helper: refused, got {r.returncode} {r.stdout!r}")
        r = _resolve(core, WORKER, "")
        check(r.returncode == 1 and r.stdout == "", "j) its own folder unsearchable + no Python: refused")
    finally:
        workers.chmod(0o755)
    workers.chmod(0o000)
    try:
        r = _resolve(core, WORKER, sys.executable)
        check(r.returncode == 1 and r.stdout == "",
              f"j) its own folder unsearchable + the real helper: refused, got {r.returncode} {r.stdout!r}")
        # Python 3.14's Path.exists()/is_symlink() return False on an inaccessible path; act it out here.
        from unittest import mock
        with mock.patch.object(Path, "exists", lambda self, **k: False), \
                mock.patch.object(Path, "is_symlink", lambda self: False):
            try:
                got = F.launch(core, WORKER)
                check(False, f"j) launch() on an unsearchable workers' folder raises, not {got}")
            except PermissionError:
                check(True, "j) launch() on an unsearchable workers' folder raises, not the core's folder")
    finally:
        workers.chmod(0o755)
    gone = core.with_name(".claude-gone")
    gone.mkdir()
    r = _resolve(gone, WORKER, str(stub))
    check(r.returncode == 0 and r.stdout.strip() == str(gone),
          "j) no workers' folder at all, its parent searchable: confirmed missing, the core's folder")
    r = _resolve(gone, WORKER, sys.executable)
    check(r.returncode == 0 and r.stdout.strip() == str(gone),
          "j) no workers' folder at all + the real helper: confirmed missing, the core's folder")
    top = core.parent / "locked-top"
    (top / ".claude").mkdir(parents=True)
    top.chmod(0o000)
    try:
        r = _resolve(top / ".claude", WORKER, str(stub))
        check(r.returncode == 1 and r.stdout == "", "j) nothing above it can be searched: refused, not the core's folder")
    finally:
        top.chmod(0o755)


if __name__ == "__main__":
    sys.exit(main())
