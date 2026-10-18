"""The one environment builder for tests that reach gateway credentials.

The remote gateway bridge, Hatch, room_ops' gateway client and the credential
owner all discover a token from whatever the environment names (a token file,
a device .env, a channel dir, a config dir). A test that overlays its values
onto the outer environment therefore inherits a developer's live selectors.
Every such test builds its environment here instead, from nothing:

    env = hermetic_env(REMOTE_TASK_TOKEN="t")      # a child process's env
    isolate_os_environ(REMOTE_TASK_TOKEN="t")      # this process, before importing
    env = isolated_child_env(X="1")                # an isolated process's env, for a child

`tests/hermetic-env-structural.test.py` refuses inheritance anywhere else.

The host's user site-packages is an execution path, not a selector: its .pth files run at a
child's startup, before any audit hook. Children get PYTHONNOUSERSITE=1 and no PYTHONUSERBASE;
under the coverage runner, coverage's own install directory is put on PYTHONPATH instead.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

# The only outer variables carried; none of them names a credential source.
HOST_ALLOWLIST = ("PATH", "LANG", "TMPDIR")
# Windows cannot start a process without these (SYSTEMROOT seeds its RNG); none names a credential.
WINDOWS_ALLOWLIST = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
# The suite runner's coverage controls, so a converted test and its children stay measured.
HARNESS_ALLOWLIST = ("SUTANDO_TEST_SUBPROCESS_COVERAGE", "PYTHONFAULTHANDLER")
HARNESS_PREFIX = "COVERAGE_"
HERMETIC_USER = "hermetic-test"
_process_home: "str | None" = None


def _coverage_path() -> "str | None":
    """coverage's install directory, resolved in this process, when the coverage runner is active.

    A directory on PYTHONPATH runs no .pth file, unlike a re-enabled user site."""
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") != "1" and not any(
            k.startswith(HARNESS_PREFIX) for k in os.environ):
        return None
    try:
        import coverage
    except ImportError:
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(coverage.__file__)))


def hermetic_env(home: "Path | str | None" = None, **extra: str) -> "dict[str, str]":
    """The allowlisted host variables, a temp HOME, a fixed USER, then `extra`.

    Without `home`, an isolated process's own temp HOME is reused, so its children share it."""
    allowed = HOST_ALLOWLIST + HARNESS_ALLOWLIST + (WINDOWS_ALLOWLIST if os.name == "nt" else ())
    env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith(HARNESS_PREFIX)}
    env["HOME"] = str(home) if home is not None else (_process_home or tempfile.mkdtemp(prefix="hermetic-home-"))
    if os.name == "nt":
        env["USERPROFILE"] = env["HOME"]
    env["USER"] = HERMETIC_USER
    env["PYTHONNOUSERSITE"] = "1"
    env.update({k: str(v) for k, v in extra.items()})
    cov = _coverage_path()
    if cov is not None:
        parts = [x for x in env.get("PYTHONPATH", "").split(os.pathsep) if x]
        env["PYTHONPATH"] = os.pathsep.join(parts + ([] if cov in parts else [cov]))
    return env


def isolate_os_environ(home: "Path | str | None" = None, **extra: str) -> "os._Environ[str]":
    """Replace this process's environment with `hermetic_env(...)` for the rest of the run.

    For a module that imports the bridge or room_ops at load time, before that import."""
    global _process_home
    env = hermetic_env(home, **extra)
    mock.patch.dict(os.environ, env, clear=True).start()
    _process_home = env["HOME"]
    return os.environ


def isolated_child_env(**extra: str) -> "dict[str, str]":
    """This process's environment plus `extra`, for a child; only once isolate_os_environ() ran,
    so what a test set on top of the allowlist carries over and nothing from outside does."""
    if _process_home is None:
        raise RuntimeError("isolated_child_env() before isolate_os_environ(): the environment is the host's")
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra.items()})
    return env
