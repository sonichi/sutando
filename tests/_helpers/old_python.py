"""Find a real Python older than 3.11 (no tomllib) for tests that must run on one."""
import functools
import os
import re
import shutil
import subprocess
import sys

_NAME = re.compile(r"python3(\.\d+)?")


def _runnable(path):
    # The macOS system python3 is a stub that opens a dialog unless the developer tools exist.
    if sys.platform == "darwin" and os.path.realpath(os.path.dirname(path)) == os.path.join("/usr", "bin"):
        return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0
    return os.access(path, os.X_OK)


def _candidates():
    yield os.environ.get("SUTANDO_TEST_OLD_PYTHON", "")
    uv = shutil.which("uv")
    if uv:
        found = subprocess.run([uv, "python", "find", "3.10"], capture_output=True, text=True)
        yield found.stdout.strip() if found.returncode == 0 else ""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if _NAME.fullmatch(name):
                yield os.path.join(directory, name)


@functools.lru_cache(maxsize=None)
def pre_311_python():
    """`$SUTANDO_TEST_OLD_PYTHON`, else the first runnable 3.9/3.10 on PATH or from uv; None if none."""
    for path in filter(None, _candidates()):
        if not os.path.isfile(path) or not _runnable(path):
            continue
        probe = subprocess.run([path, "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                               capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip() == "True":
            return path
    return None
