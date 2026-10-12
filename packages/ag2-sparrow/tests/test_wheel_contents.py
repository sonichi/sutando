"""The built distributions must ship every subpackage and start from a clean install.

0.4.0 listed packages by hand, so ag2_sparrow/delivery_core/ never reached the
wheel or sdist and the installed ag2-sparrow died at import. This builds both
distributions in a throwaway venv, checks every package directory is inside
them, installs the wheel and runs the console entry point.
"""
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = PKG_ROOT / "ag2_sparrow"
# Non-Python files the code reads at run time (package-data in pyproject.toml).
DATA_FILES = ("ag2_sparrow/adapters/hatch/ag2-sparrow-hatch.sh",
              "ag2_sparrow/adapters/hatch/hook-definition.json")


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
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("REMOTE_TASK_", "AG2_", "SUTANDO_"))
               and k != "CLAUDE_CONFIG_DIR"}  # names a channel .env holding a live token
        env["AGENT_CONNECT_STATE_DIR"] = str(d / "state")
        env["PYTHONPATH"] = ""
        # Run from the temp dir so the source tree cannot shadow the installed copy.
        _run([py, "-c", "import ag2_sparrow.remote_gateway_bridge, ag2_sparrow.outbox_cli"],
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


if __name__ == "__main__":
    main()
