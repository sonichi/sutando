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
child's startup, before any audit hook. Children get PYTHONNOUSERSITE=1 and no PYTHONUSERBASE.
Under the coverage runner, PYTHONPATH gets a private directory linking only the coverage package
(and its unconditional dependencies), never the directory it is installed in: a sitecustomize.py
there would run at startup too.
"""
from __future__ import annotations

import importlib.metadata
import json
import importlib.util
import os
import re
import shutil
import tempfile
from pathlib import Path

from _helpers import ORIGINAL_ENVIRON
from _helpers.credential_scope import CREDENTIAL_NAME

# The only outer variables carried; none of them names a credential source.
HOST_ALLOWLIST = ("PATH", "LANG", "TMPDIR")
# The documented opt-in that lets node tests find jsdom: a path to modules, not a credential selector.
MODULE_PATH_ALLOWLIST = ("NODE_PATH",)
# All a runtime harness's environment.json may hand a test: where to find programs and modules.
HARNESS_KEYS = ("PATH", "PYTHONPATH", "TMPDIR", "LANG")
# Windows cannot start a process without these (SYSTEMROOT seeds its RNG); none names a credential.
WINDOWS_ALLOWLIST = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
# The suite runner's coverage controls, so a converted test and its children stay measured.
HARNESS_ALLOWLIST = ("SUTANDO_TEST_SUBPROCESS_COVERAGE", "PYTHONFAULTHANDLER")
HARNESS_PREFIX = "COVERAGE_"
# Test-control gates (run-under-CI, opt-in suites); a name the credential census matches is never one.
TEST_CONTROL_ALLOWLIST = ("CI", "GITHUB_ACTIONS")
TEST_CONTROL_PREFIX = "SUTANDO_TEST_"
HERMETIC_USER = "hermetic-test"
# Read when this helper is imported, so before any isolation: a runtime harness names its private root here.
_HARNESS_SOCKET = os.environ.get("SUTANDO_RUNTIME_SOCKET")
_process_home: "str | None" = None
_coverage_dirs: "dict[str, str]" = {}


def _package_location(name: str) -> "str | None":
    """The directory of package `name` (or its module file), resolved in this process."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return os.path.abspath(list(spec.submodule_search_locations)[0])
    return os.path.abspath(spec.origin) if spec.origin else None


def _link(src: str, d: str) -> None:
    dst = os.path.join(d, os.path.basename(src))
    try:
        os.symlink(src, dst)
    except OSError:  # Windows without symlink privilege
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)


def coverage_bootstrap(location: str) -> str:
    """A private directory holding a link to the coverage package at `location` and to each
    unconditional dependency it declares; nothing else from their install directory."""
    if location not in _coverage_dirs:
        d = tempfile.mkdtemp(prefix="hermetic-coverage-")
        _link(location, d)
        try:
            requires = importlib.metadata.requires("coverage") or []
        except importlib.metadata.PackageNotFoundError:
            requires = []
        for req in requires:
            if ";" in req:
                continue
            dep = _package_location(re.split(r"[^A-Za-z0-9_.-]", req.strip(), maxsplit=1)[0].replace("-", "_"))
            if dep is not None and not os.path.exists(os.path.join(d, os.path.basename(dep))):
                _link(dep, d)
        _coverage_dirs[location] = d
    return _coverage_dirs[location]


def _coverage_path() -> "str | None":
    """The coverage bootstrap directory, when the coverage runner is active."""
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") != "1" and not any(
            k.startswith(HARNESS_PREFIX) for k in os.environ):
        return None
    try:
        import coverage
    except ImportError:
        return None
    return coverage_bootstrap(os.path.dirname(os.path.abspath(coverage.__file__)))


def _test_control(name: str) -> bool:
    return name in TEST_CONTROL_ALLOWLIST or name.startswith(TEST_CONTROL_PREFIX)


def hermetic_env(home: "Path | str | None" = None, **extra: str) -> "dict[str, str]":
    """The allowlisted host and test-control variables, a temp HOME, a fixed USER, telemetry off, then `extra`.

    Without `home`, an isolated process's own temp HOME is reused, so its children share it."""
    allowed = HOST_ALLOWLIST + MODULE_PATH_ALLOWLIST + HARNESS_ALLOWLIST + (WINDOWS_ALLOWLIST if os.name == "nt" else ())
    env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith(HARNESS_PREFIX)
           or (_test_control(k) and not CREDENTIAL_NAME.search(k))}
    env["HOME"] = str(home) if home is not None else (_process_home or tempfile.mkdtemp(prefix="hermetic-home-"))
    if os.name == "nt":
        env["USERPROFILE"] = env["HOME"]
    env["USER"] = HERMETIC_USER
    env["PYTHONNOUSERSITE"] = "1"
    # IMPORTANT: tests must never emit product telemetry; removing or weakening these lines makes CI test
    # runs count as real users on the admin DAU. Enforced by tests/telemetry-test-optout-guard.test.py.
    env["SUTANDO_TELEMETRY"] = "0"
    env["DO_NOT_TRACK"] = "1"
    env.update({k: str(v) for k, v in extra.items()})
    cov = _coverage_path()
    if cov is not None:
        parts = [x for x in env.get("PYTHONPATH", "").split(os.pathsep) if x]
        env["PYTHONPATH"] = os.pathsep.join(parts + ([] if cov in parts else [cov]))
    return env


def _environ_patch_active() -> bool:
    """A started mock.patch.dict on os.environ: its stop() puts back the environment it saved."""
    from unittest import mock
    try:
        active, patch_dict = list(mock._patch._active_patches), mock._patch_dict
    except AttributeError as e:
        raise RuntimeError("isolate_os_environ() cannot read mock's started patches to verify none "
                           "covers os.environ; refusing to isolate unverified") from e
    return any(isinstance(p, patch_dict) and (p.in_dict is os.environ or p.in_dict == "os.environ")
               for p in active)


def isolate_os_environ(home: "Path | str | None" = None, **extra: str) -> "os._Environ[str]":
    """Replace this process's environment with `hermetic_env(...)` for the rest of the run; no
    patcher state can restore the outer one.

    For a module that imports the bridge or room_ops at load time, before that import."""
    global _process_home
    if os.environ is not ORIGINAL_ENVIRON:
        raise RuntimeError("isolate_os_environ() with os.environ replaced (patch.object, patch('os.environ') or "
                           "reassignment): clearing the stand-in leaves the process environment; isolate first")
    # A patch started after an earlier isolation saved an isolated environment: only an earlier one leaks.
    if _process_home is None and _environ_patch_active():
        raise RuntimeError("isolate_os_environ() under a mock.patch.dict(os.environ) started before any "
                           "isolation: stopping that patch would restore the outer environment; "
                           "isolate before patching os.environ")
    env = hermetic_env(home, **extra)
    # In place, not a patcher: mock.patch.stopall() would restore the outer environment.
    os.environ.clear()
    os.environ.update(env)
    _process_home = env["HOME"]
    return os.environ


def runtime_harness() -> "tuple[Path | None, dict[str, str]]":
    """The environment.json a runtime harness writes beside SUTANDO_RUNTIME_SOCKET, and only its
    HARNESS_KEYS; (None, {}) without one. Stable across isolation, so it can be an isolator's argument.
    Nothing else in the file (a token, a telemetry switch) ever reaches a test's environment."""
    if not _HARNESS_SOCKET:
        return None, {}
    path = Path(_HARNESS_SOCKET).parent / "environment.json"
    if not path.is_file():
        return None, {}
    data = json.loads(path.read_text())
    return path, {k: str(data[k]) for k in HARNESS_KEYS if k in data}


def isolated_child_env(**extra: str) -> "dict[str, str]":
    """This process's environment plus `extra`, for a child; only once isolate_os_environ() ran,
    so what a test set on top of the allowlist carries over and nothing from outside does."""
    if _process_home is None:
        raise RuntimeError("isolated_child_env() before isolate_os_environ(): the environment is the host's")
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra.items()})
    return env
