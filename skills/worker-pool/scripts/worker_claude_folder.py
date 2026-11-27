#!/usr/bin/env python3
"""A worker's own Claude config folder, so its account and model are its own.

Claude Code keeps its sign-in per config folder (its Keychain item is named by the folder's path) and
writes `/model` into that folder's settings.json as the default for new sessions. A worker started in
the core's folder therefore shares both: switching either for one worker switches the core and every
other worker.

A worker that was given a folder of its own (`<core folder>-workers/<worker id>`) is started in it.
Only what makes an account or a model its own lives there:

  .claude.json    the account Claude Code names, and this folder's onboarding/trust answers
  settings.json   the core's settings with this worker's own `model` kept

Only the entries named in SHARED (skills, plugins, projects with their memory and transcripts, ...)
are links to the core's entry of the same name, so nothing is copied and nothing forks. It is an
allowlist on purpose: anything else in the core's folder -- its file-backed sign-in
(`.credentials.json`, `.credentials.source.json`), its locks, backups and per-session state -- never
reaches a worker. Links are re-made on every launch; a link into the core's folder under any name not
in SHARED (a credential link an older version made) is removed then. A real file the worker made, such
as its own `.credentials.json` after it signed in, is never touched.

The worker's `.claude.json` and `settings.json` are its own and private (0600). One that is there but
is not a JSON object is refused, not replaced: it may hold the worker's sign-in, and putting the
core's answers in its place would lose that silently. Fix it, or move it aside and the next launch
makes a fresh one (the worker then signs in again).

Opt-in: a worker with no folder keeps the core's. `ensure` makes one; nothing here signs in or out.

  worker_claude_folder.py path   <core folder> <worker id>   # prints the folder, made or not
  worker_claude_folder.py ensure <core folder> <worker id>   # makes/refreshes it, prints it
  worker_claude_folder.py launch <core folder> <worker id>   # refreshes it if it exists; prints the
                                                             # folder to start in (the core's if none);
                                                             # exits 1 when it has one that failed
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

OWN = (".claude.json", "settings.json")      # kept per worker, never linked

# The only core entries a worker follows: what it works with, never who it is signed in as
# (`channels` is the core's channel configs its progress posts go through, not a Claude sign-in).
SHARED = ("CLAUDE.md", "agents", "channels", "commands", "hooks", "keybindings.json",
          "output-styles", "plugins", "projects", "skills")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def folder(core: "str | os.PathLike", worker_id: str) -> Path:
    """Where `worker_id`'s own folder is, made or not. A sibling of the core's, never inside it."""
    if not _ID.match(str(worker_id or "")) or ".." in str(worker_id):
        raise ValueError("not a worker id")
    core = Path(core)
    return core.with_name(core.name + "-workers") / str(worker_id)


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_own(path: Path) -> dict:
    """The worker's own file: {} when it has none yet; refused when it is a link or not a JSON object."""
    if path.is_symlink():
        raise ValueError("%s is a link, not this worker's own file; remove it and launch again" % path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object; fix it, or move it aside so the next launch makes a"
                         " fresh one (the worker then signs in again)" % path)
    return data


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _into(link: Path, core: Path) -> bool:
    """Whether `link` points at or into the core's folder."""
    roots = {str(core), os.path.realpath(core)}
    targets = {os.path.join(str(link.parent), os.readlink(link)), os.path.realpath(link)}
    return any(t == r or t.startswith(r + os.sep) for t in targets for r in roots)


def settings(core_settings: dict, own_settings: dict) -> dict:
    """The core's settings (its hooks and permissions stay current) with this worker's own model; a
    worker that never chose one keeps the core's."""
    merged = dict(core_settings)
    if "model" in own_settings:
        merged["model"] = own_settings["model"]
    return merged


def account_file(core_file: dict, own_file: dict) -> dict:
    """The worker's .claude.json: its own, plus the core's answers to first-run and folder-trust
    questions it has not answered itself. The account is never taken from the core: whoever signs
    this folder in writes it."""
    merged = {k: v for k, v in core_file.items() if k not in ("oauthAccount", "projects")}
    merged.update(own_file)
    projects = dict(core_file.get("projects") or {}) if isinstance(core_file.get("projects"), dict) else {}
    if isinstance(own_file.get("projects"), dict):
        projects.update(own_file["projects"])
    if projects:
        merged["projects"] = projects
    return merged


def ensure(core: "str | os.PathLike", worker_id: str) -> Path:
    """Make or refresh the worker's folder; returns it. Safe to call on every launch."""
    core, own = Path(core), folder(core, worker_id)
    if not core.is_dir():
        raise FileNotFoundError("the core's Claude folder is not there")
    own.mkdir(parents=True, exist_ok=True)
    os.chmod(own, 0o700)
    for link in own.iterdir():           # a link a worker must not follow: the core's sign-in, ...
        if link.is_symlink() and link.name not in SHARED and _into(link, core):
            link.unlink()
    for name in SHARED:
        entry = core / name
        if not entry.exists():
            continue
        link = own / name
        if link.is_symlink():
            if os.readlink(link) == str(entry):
                continue
            link.unlink()
        elif link.exists():
            continue                     # the worker's own real entry is never replaced
        os.symlink(entry, link)
    for link in own.iterdir():           # a link whose core entry is gone points nowhere
        if link.is_symlink() and not link.exists():
            link.unlink()
    mine = {name: _load_own(own / name) for name in OWN}   # all checked before anything is written
    for name, merge in ((".claude.json", account_file), ("settings.json", settings)):
        merged = merge(_load(core / name), mine[name])
        if not (own / name).exists() or merged != mine[name]:
            _write(own / name, merged)
        os.chmod(own / name, 0o600)
    return own


def launch(core: "str | os.PathLike", worker_id: str) -> Path:
    """The folder to start `worker_id` in: its own, refreshed, when it has one; else the core's. A
    folder that is there but cannot be refreshed raises: the worker must not fall back to the core's
    account."""
    try:
        own = folder(core, worker_id)
    except ValueError:
        return Path(core)
    if not own.exists() and not own.is_symlink():
        return Path(core)
    return ensure(core, worker_id)


def main(argv) -> int:
    if len(argv) != 4 or argv[1] not in ("path", "ensure", "launch"):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: worker_claude_folder.py {path|ensure|launch} <core folder> <worker id>", file=sys.stderr)
        return 2
    try:
        out = {"path": folder, "ensure": ensure, "launch": launch}[argv[1]](argv[2], argv[3])
    except (ValueError, OSError) as e:
        print("worker_claude_folder: %s" % e, file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
