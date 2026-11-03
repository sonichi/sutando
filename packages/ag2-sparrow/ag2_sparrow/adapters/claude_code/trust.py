"""Claude Code's folder trust for the held inbox, recorded before the session starts.

An interactive `claude` asks "Is this a project you created or one you trust?" in a directory its
global config has not trusted, and an unattended pane would wait there forever. The CLI's own
record is `projects[<dir>].hasTrustDialogAccepted: true` in its global config file; this module sets
exactly that key for exactly the inbox (never a parent, which would trust every child), under the
CLI's own lock directory, and changes nothing else in the file. It also names the startup dialogs a
pane capture can show, so the launcher and `verify` report a pane waiting at one. Stdlib only.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import time
import unicodedata

# The CLI's own lock is a directory beside the file; another holder's is waited for, never broken.
LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.05
DIALOGS = (("folder-trust", re.compile(r"^\s*(?:❯\s*)?Yes, I trust this folder\s*$", re.M)),
           ("bypass-permissions", re.compile(r"^\s*(?:❯\s*)?Yes, I accept\s*$", re.M)))
EXIT_LINE = re.compile(r"^\s*(?:❯\s*)?No, exit\s*$", re.M)


def config_file(claude_config_dir: str = "", home: str = "") -> str:
    """The CLI's global config: a non-empty legacy `.config.json` in its config dir wins, else
    `.claude.json` in CLAUDE_CONFIG_DIR, or in HOME when that is unset."""
    home = home or os.path.expanduser("~")
    legacy = os.path.join(claude_config_dir or os.path.join(home, ".claude"), ".config.json")
    primary = os.path.join(claude_config_dir or home, ".claude.json")
    try:
        st = os.stat(legacy)
        if os.path.isfile(legacy) and (st.st_size > 0 or not os.path.exists(primary)):
            return legacy
    except OSError:
        pass
    return primary


def project_key(directory: str) -> str:
    """The key the CLI looks the session's cwd up by: the resolved path, NFC."""
    return unicodedata.normalize("NFC", os.path.realpath(directory))


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("it is not a JSON object")
    projects = data.get("projects")
    if projects is not None and not isinstance(projects, dict):
        raise ValueError("its projects entry is not an object")
    return data


def trusted(path: str, key: str) -> bool:
    entry = (_read(path).get("projects") or {}).get(key)
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


def _lock(lock: str, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            os.mkdir(lock)
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(errno.EBUSY, "the CLI's config lock %s stayed held" % lock)
            time.sleep(LOCK_POLL_S)


def record(claude_config_dir: str, directory: str, home: str = "", wait_s: float = LOCK_WAIT_S) -> str:
    """'' once `directory` is trusted in the CLI's global config (written only when it was not), else
    the one refusal line. Read-modify-replace under the CLI's lock directory, the file's mode kept."""
    path = config_file(claude_config_dir, home)
    key = project_key(directory)
    try:
        if trusted(path, key):
            return ""
        target = os.path.realpath(path)
        lock = path + ".lock"
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        _lock(lock, wait_s)
        try:
            data = _read(target)
            projects = data.setdefault("projects", {})
            entry = projects.get(key)
            projects[key] = dict(entry if isinstance(entry, dict) else {}, hasTrustDialogAccepted=True)
            try:
                mode = os.stat(target).st_mode & 0o777
            except FileNotFoundError:
                mode = 0o600
            tmp = "%s.%d.%s.tmp" % (target, os.getpid(), os.urandom(4).hex())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), mode)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        finally:
            os.rmdir(lock)
        if not trusted(path, key):
            raise ValueError("the record did not read back")
    except (OSError, ValueError) as e:
        return ("cannot record Claude Code's folder trust for %s in %s (%s); the session would stop at the "
                "trust dialog" % (key, path, getattr(e, "strerror", None) or e))
    return ""


def dialog(capture: "str | None") -> "str | None":
    """The Claude Code startup dialog a pane capture shows (folder-trust, bypass-permissions), else None."""
    if not capture or not EXIT_LINE.search(capture):
        return None
    for name, rx in DIALOGS:
        if rx.search(capture):
            return name
    return None
