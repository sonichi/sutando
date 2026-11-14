"""Private startup receipts for externally managed core helpers; never manages processes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from watcher_identity import proc_argv_vector

SCRIPTS = {"monitor": "core-input-watch.py", "heartbeat": "core_heartbeat.py"}


def _process(pid):
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid helper pid")
    result = subprocess.run(
        ["ps", "-o", "uid=,lstart=,stat=", "-p", str(pid)],
        capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"},
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) != 7 or fields[-1].startswith("Z"):
        raise ValueError("helper process is not observable")
    if int(fields[0]) != os.getuid():
        raise ValueError("helper belongs to another user")
    return {"pid": pid, "uid": int(fields[0]), "lstart": " ".join(fields[1:6])}


def _directory(directory, workspace):
    directory = Path(directory).resolve(strict=True)
    state = Path(workspace).resolve() / "state"
    if state not in directory.parents:
        raise ValueError("helper receipts must be below workspace/state")
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("helper receipt directory must be owner-private")
    return directory


def publish(directory, role, script, workspace, socket, session, *, passive, output=None):
    """Called by the running helper with its resolved configuration, before its loop."""
    if role not in SCRIPTS or not passive:
        raise ValueError("external helpers must be continuous and passive")
    directory = _directory(directory, workspace)
    record = {
        "version": 1, "role": role, **_process(os.getpid()),
        "script": str(Path(script).resolve()), "workspace": str(Path(workspace).resolve()),
        "socket": str(Path(socket).resolve()), "session": session, "passive": True,
        "output": str(Path(output).resolve()) if output else None,
    }
    fd, temporary = tempfile.mkstemp(prefix="." + role + "-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, directory / (role + ".json"))
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise ValueError("helper receipt must be a bounded owner-private file")
        record = json.load(stream)
    if not isinstance(record, dict):
        raise ValueError("invalid helper receipt")
    return record


def _script_argv(pid, script):
    argv = proc_argv_vector(pid)
    if not argv or not re.fullmatch(r"python[0-9.]*", Path(argv[0]).name, re.IGNORECASE):
        raise ValueError("helper interpreter is not observable")
    args = argv[1:]
    if args[:1] == ["-B"]:
        args = args[1:]
    if not args or not Path(args[0]).is_absolute() or Path(args[0]).resolve() != script:
        raise ValueError("helper is not running the selected checkout script")
    return args[1:]


def _option(args, name):
    values = []
    for i, arg in enumerate(args):
        if arg == name:
            if i + 1 == len(args):
                raise ValueError("missing helper operand")
            values.append(args[i + 1])
        elif arg.startswith(name + "="):
            values.append(arg[len(name) + 1:])
    if len(values) != 1:
        raise ValueError("missing or repeated helper operand")
    return values[0]


def validate(directory, repo, workspace, socket, session):
    """Validate both receipts against live identity; unknown never permits a fallback."""
    directory = _directory(directory, workspace)
    repo, workspace, socket = Path(repo).resolve(), Path(workspace).resolve(), Path(socket).resolve()
    identities = {}
    for role, filename in SCRIPTS.items():
        record = _read(directory / (role + ".json"))
        script = repo / "src" / filename
        expected = {"version": 1, "role": role, "script": str(script),
                    "workspace": str(workspace), "socket": str(socket),
                    "session": session, "passive": True,
                    "output": str(workspace / "state/core-supervisor.json") if role == "monitor" else None}
        if (type(record.get("version")) is not int or record.get("passive") is not True
                or any(record.get(key) != value for key, value in expected.items())):
            raise ValueError("helper receipt configuration mismatch")
        identity = _process(record.get("pid"))
        if any(record.get(key) != value for key, value in identity.items()):
            raise ValueError("helper process identity changed")
        args = _script_argv(identity["pid"], script)
        if Path(_option(args, "--helper-receipt-dir")).resolve() != directory:
            raise ValueError("helper receipt invocation mismatch")
        if any(arg.split("=", 1)[0] in ("--once", "--stop", "--mark-stopped") for arg in args):
            raise ValueError("helper is not continuous")
        if role == "monitor":
            if "--no-auto-answer" not in args or "--no-chat-escalation" not in args:
                raise ValueError("monitor is not passive")
            if (Path(_option(args, "--socket")).resolve() != socket
                    or _option(args, "--session") != session
                    or Path(_option(args, "--out")).resolve() != Path(expected["output"])):
                raise ValueError("monitor invocation configuration mismatch")
        if _process(identity["pid"]) != identity:
            raise ValueError("helper changed during validation")
        identities[role] = identity
    if identities["monitor"]["pid"] == identities["heartbeat"]["pid"]:
        raise ValueError("helpers must be separate processes")
    return identities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--expected", help="retain the initially validated process identities")
    args = parser.parse_args()
    try:
        from workspace_default import resolve_workspace
        repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
        identities = validate(args.directory, repo,
                              resolve_workspace(), args.socket, args.session)
        fingerprint = hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
        if args.expected and args.expected != fingerprint:
            raise ValueError("external helpers were replaced during launch")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("external core helpers are missing, changed, or not passive", file=sys.stderr)
        return 1
    print(fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
