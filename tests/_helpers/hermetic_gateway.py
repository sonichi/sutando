"""Import the gateway bridge without reaching any host config, token or vault.

The bridge resolves its token, directories and channel config at import time,
so isolation must precede the first import:

    from _helpers.hermetic_gateway import isolate_then_import
    gw, IMPORT_READS = isolate_then_import()

`IMPORT_READS` holds every file opened and every subprocess started during the
import; `assert_hermetic(test, IMPORT_READS)` fails on any host path or vault use.
"""
from __future__ import annotations

import builtins
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(tempfile.mkdtemp(prefix="hermetic-gateway-"))

_ENV = {
    "CLAUDE_CONFIG_DIR": str(ROOT / "claude-config"),
    "AGENT_CONNECT_TASK_DIR": str(ROOT / "tasks"),
    "AGENT_CONNECT_RESULT_DIR": str(ROOT / "results"),
    "AGENT_CONNECT_STATE_DIR": str(ROOT / "state"),
    "REMOTE_TASK_TOKEN": "hermetic-test-token",
    "REMOTE_TASK_URL": "http://127.0.0.1:9",
}
_UNSET = ("AG2_DEVICE_ENV", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")


def _forbidden_roots() -> "list[Path]":
    home = Path.home().resolve()
    return [home, (REPO / "workspace").resolve()]


def host_reads(reads: "list[tuple[str, str]]") -> "list[str]":
    """The recorded opens and subprocesses that left the isolated root."""
    bad = []
    repo = REPO.resolve()
    for kind, what in reads:
        if kind == "proc":
            if any(w in what for w in ("security", "secret-vault", "keychain")):
                bad.append(f"vault subprocess: {what}")
            continue
        try:
            p = Path(what).resolve()
        except (OSError, ValueError):
            continue
        if p == ROOT or ROOT.resolve() in p.parents:
            continue
        home, workspace = _forbidden_roots()
        if p == workspace or workspace in p.parents:
            bad.append(f"workspace read: {p}")
        elif (p == home or home in p.parents) and not (p == repo or repo in p.parents):
            bad.append(f"host read: {p}")
    return bad


def isolate_then_import():
    """Isolate the environment, then import the bridge while recording reads."""
    for d in ("claude-config", "tasks", "results", "state"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    os.environ.update(_ENV)
    for name in _UNSET:
        os.environ.pop(name, None)
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    reads: "list[tuple[str, str]]" = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    real_popen_init = subprocess.Popen.__init__

    def spy_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            reads.append(("file", os.fsdecode(file)))
        return real_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        reads.append(("file", os.fsdecode(path)))
        return real_os_open(path, *a, **k)

    def spy_popen(self, args, *a, **k):
        reads.append(("proc", " ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args)))
        return real_popen_init(self, args, *a, **k)

    builtins.open = io.open = spy_open
    os.open = spy_os_open
    subprocess.Popen.__init__ = spy_popen
    try:
        from ag2_sparrow import remote_gateway_bridge as gw
    finally:
        builtins.open, io.open, os.open = real_open, real_io_open, real_os_open
        subprocess.Popen.__init__ = real_popen_init
    return gw, reads


def assert_hermetic(test, reads) -> None:
    test.assertTrue(reads, "the import recorded nothing: the spy never ran")
    test.assertEqual(host_reads(reads), [], "the bridge import reached host config, a token file or the vault")
