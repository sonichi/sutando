"""The single-file ag2-sparrow.pyz ships every module and starts from a bare interpreter.

Builds the zipapp with packages/ag2-sparrow/tools/build_zipapp.py, checks the
archive against the package tree, then runs it from a temp dir with no token.
"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import zipfile

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG_ROOT = REPO / "packages" / "ag2-sparrow"
SRC = PKG_ROOT / "ag2_sparrow"
sys.path.insert(0, str(PKG_ROOT / "tools"))
import build_zipapp, smoke_entry  # noqa: E401,E402


def _sources():
    return sorted(str(p.relative_to(PKG_ROOT)).replace(os.sep, "/")
                  for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_archive_contents(pyz):
    names = set(zipfile.ZipFile(pyz).namelist())
    pkgs = sorted({s.rsplit("/", 1)[0] for s in _sources() if s.endswith("/__init__.py")})
    assert "ag2_sparrow/delivery_core" in pkgs, pkgs
    for pkg in pkgs:
        assert f"{pkg}/__init__.py" in names, f"pyz lacks package {pkg}"
    missing = [s for s in _sources() if s not in names]
    assert not missing, f"pyz lacks modules: {missing}"
    stray = sorted(n for n in names
                   if n != "__main__.py" and not n.startswith("ag2_sparrow/"))
    assert not stray, f"pyz ships files outside the package: {stray}"
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")], names
    assert "ag2_sparrow.remote_gateway_bridge" in zipfile.ZipFile(pyz).read("__main__.py").decode()
    assert pyz.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    print(f"PASS pyz ships all {len(pkgs)} packages and {len(_sources())} modules, nothing else")


def test_runs_without_install(pyz, tmp):
    smoke_entry.check_fails_without_token([sys.executable, str(pyz)], str(tmp))
    env = smoke_entry.minimal_env(str(tmp))
    env["PYTHONPATH"] = str(pyz)
    r = subprocess.run([sys.executable, "-c",
                        "import ag2_sparrow.remote_gateway_bridge as b, ag2_sparrow.outbox_cli;"
                        "print(b.__file__)"],
                       capture_output=True, text=True, cwd=tmp, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith(str(pyz)), f"imported from outside the pyz: {r.stdout}"
    r = subprocess.run([sys.executable, "-m", "ag2_sparrow.outbox_cli", "--help"],
                       capture_output=True, text=True, cwd=tmp, env=env, timeout=60)
    assert r.returncode == 0 and "ag2-sparrow-outbox" in r.stdout, r.stderr
    print("PASS pyz fails cleanly without a token; bridge and outbox CLI import from it")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d).resolve()
        pyz = tmp / "out" / "ag2-sparrow.pyz"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert build_zipapp.main(["--output", str(pyz)]) == 0
        assert out.getvalue().strip() == str(pyz), out.getvalue()
        test_archive_contents(pyz)
        test_runs_without_install(pyz, tmp)
        test_smoke_cli(pyz)


def test_smoke_cli(pyz):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert smoke_entry.main([sys.executable, str(pyz)]) == 0
    assert out.getvalue().startswith("PASS "), out.getvalue()
    try:
        smoke_entry.main([])
    except SystemExit as e:
        assert "Usage" in str(e.code), e.code
    else:
        raise AssertionError("smoke_entry with no command did not exit")
    print("PASS smoke_entry CLI passes the pyz and refuses an empty command")


if __name__ == "__main__":
    main()
