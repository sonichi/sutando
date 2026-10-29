"""The built distributions must ship every subpackage and start from a clean install.

0.4.0 listed packages by hand, so ag2_sparrow/delivery_core/ never reached the
wheel or sdist and the installed ag2-sparrow died at import. This builds both
distributions in a throwaway venv, checks every package directory is inside
them, installs the wheel and runs the console entry point.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = PKG_ROOT / "ag2_sparrow"
# Non-Python files the code reads at run time (package-data in pyproject.toml).
DATA_FILES = ("ag2_sparrow/tmux-pane-keys.sh",
              "ag2_sparrow/bounded-wait.sh",
              "ag2_sparrow/adapters/hatch/ag2-sparrow-hatch.sh",
              "ag2_sparrow/adapters/hatch/hook-definition.json",
              "ag2_sparrow/adapters/hatch/builtin-runtime-text.json",
              "ag2_sparrow/adapters/claude_routine/routine-prompt.md",
              "ag2_sparrow/adapters/claude_routine/README.md",
              "ag2_sparrow/adapters/claude_cloud_session/session-prompt.md",
              "ag2_sparrow/adapters/claude_cloud_session/README.md",
              # the plugin claude-local loads when no --plugin-dir is given
              "ag2_sparrow/adapters/claude_code/ag2space-inbox/.claude-plugin/plugin.json",
              "ag2_sparrow/adapters/claude_code/ag2space-inbox/hooks/hooks.json",
              "ag2_sparrow/adapters/claude_code/ag2space-inbox/hooks/register.ts")


def _expected_packages():
    return sorted(
        str(p.parent.relative_to(PKG_ROOT)).replace(os.sep, "/")
        for p in SRC.rglob("__init__.py")
        if "__pycache__" not in p.parts
    )


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise AssertionError(f"{cmd} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def main():
    expected = _expected_packages()
    assert "ag2_sparrow/delivery_core" in expected, expected
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        venv.EnvBuilder(with_pip=True).create(d / "venv")
        bindir = d / "venv" / ("Scripts" if os.name == "nt" else "bin")
        py = str(bindir / "python")
        _run([py, "-m", "pip", "install", "--quiet", "build"])
        out = d / "dist"
        _run([py, "-m", "build", "--sdist", "--wheel", "--outdir", str(out), str(PKG_ROOT)])

        whl = next(out.glob("*.whl"))
        sdist = next(out.glob("*.tar.gz"))
        wheel_names = set(zipfile.ZipFile(whl).namelist())
        sdist_names = {n.split("/", 1)[1] for n in tarfile.open(sdist).getnames() if "/" in n}
        for pkg in expected:
            init = f"{pkg}/__init__.py"
            assert init in wheel_names, f"wheel {whl.name} lacks {init}"
            assert init in sdist_names, f"sdist {sdist.name} lacks {init}"
        for src in SRC.rglob("*.py"):
            if "__pycache__" in src.parts:
                continue
            rel = str(src.relative_to(PKG_ROOT)).replace(os.sep, "/")
            assert rel in wheel_names, f"wheel {whl.name} lacks {rel}"
        for data in DATA_FILES:
            assert data in wheel_names, f"wheel {whl.name} lacks {data}"
            assert data in sdist_names, f"sdist {sdist.name} lacks {data}"
        print(f"PASS wheel+sdist ship all {len(expected)} packages: {', '.join(expected)}")

        _run([py, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", str(whl)])
        (d / "import-home").mkdir()
        env = hermetic_env(d / "import-home", AGENT_CONNECT_STATE_DIR=str(d / "state"), PYTHONPATH="")
        # Run from the temp dir so the source tree cannot shadow the installed copy.
        _run([py, "-c", "import ag2_sparrow.remote_gateway_bridge, ag2_sparrow.outbox_cli, ag2_sparrow.holder_lock"],
             cwd=d, env=env)
        r = subprocess.run([str(bindir / "ag2-sparrow")], capture_output=True, text=True,
                           cwd=d, env=env, timeout=60)
        assert r.returncode != 0, f"ag2-sparrow with no token exited 0:\n{r.stdout}\n{r.stderr}"
        assert "ModuleNotFoundError" not in r.stderr, r.stderr
        assert "REMOTE_TASK_TOKEN" in r.stderr, f"expected the missing-token message:\n{r.stderr}"
        print("PASS installed wheel imports and ag2-sparrow fails cleanly without a token")
        r = subprocess.run([str(bindir / "ag2-sparrow-hatch"), "--help"], capture_output=True,
                           text=True, cwd=d, env=env, timeout=60)
        assert r.returncode == 0 and "install" in r.stdout, f"{r.stdout}\n{r.stderr}"
        print("PASS installed ag2-sparrow-hatch runs the Hatch adapter")
        # install runs with a PATH that lacks the venv's bin, as a Hatch worker's may.
        home = d / "home"
        home.mkdir()
        ienv = dict(env, HOME=str(home), PATH="/usr/bin:/bin",
                    AGENT_CONNECT_TASK_DIR=str(d / "tasks"), AGENT_CONNECT_RESULT_DIR=str(d / "results"))
        # No relay here, so no Sparrow could connect: install's start-and-check step is skipped.
        r = subprocess.run([str(bindir / "ag2-sparrow-hatch"), "install", "--mxid", "@w:ag2.space",
                            "--no-start"],
                           capture_output=True, text=True, cwd=d, env=ienv, timeout=60)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        prompt = json.loads((home / ".config" / "ag2-sparrow-hatch" / "hook-definition.json")
                            .read_text())["prompt"]
        named = {span.split()[0] for span in re.findall(r"`([^`]+)`", prompt)}
        assert {str(pathlib.Path(n).resolve()) for n in named} == \
            {str((bindir / "ag2-sparrow-hatch").resolve())}, named
        r = subprocess.run([*named, "reply", "--help"],
                           capture_output=True, text=True, cwd=d, env=ienv, timeout=60)
        assert r.returncode == 0 and "result_file" in r.stdout, f"{r.stdout}\n{r.stderr}"
        r = subprocess.run([*named, "wake-context"],
                           capture_output=True, text=True, cwd=d, env=ienv, timeout=60)
        assert r.returncode == 0 and "Follow the worker_note exactly" in r.stdout, \
            f"{r.stdout}\n{r.stderr}"
        print(f"PASS installed hook prompt names the reply command by absolute path: {named.pop()}")

if __name__ == "__main__":
    main()
