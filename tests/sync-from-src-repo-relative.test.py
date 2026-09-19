#!/usr/bin/env python3
"""sync_from_src.py's MAP keys are repo-relative paths, not names under a fixed
src/ directory — this covers a skill-path source, drift on one, and MISSING.

Run: python3 tests/sync-from-src-repo-relative.test.py
"""
import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "packages" / "ag2-sparrow" / "tools" / "sync_from_src.py"

# A real, already-tracked file outside src/ — exactly the kind of source this
# capability exists to make expressible (skills/worker-pool owns pool_delivery.py,
# not src/).
SKILL_SOURCE = "skills/worker-pool/scripts/pool_delivery.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_from_src_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    old_argv = sys.argv
    sys.argv = ["sync_from_src.py", *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main()
    finally:
        sys.argv = old_argv
    return rc, out.getvalue(), err.getvalue()


def test_skill_path_source_is_vendored():
    assert (REPO / SKILL_SOURCE).exists(), f"fixture source missing: {SKILL_SOURCE}"
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        mod.PKG_DIR = Path(tmp)
        mod.MAP = {SKILL_SOURCE: "pool_delivery_vendored.py"}

        rc, out, err = _run(mod, [])
        assert rc == 0, err
        vendored = Path(tmp) / "pool_delivery_vendored.py"
        assert vendored.exists()
        assert vendored.read_text(encoding="utf-8") == (REPO / SKILL_SOURCE).read_text(encoding="utf-8")

        # regenerated copy must itself report in sync
        rc, out, err = _run(mod, ["--check"])
        assert rc == 0, err
        assert "in sync" in out


def test_drift_detected_on_a_repo_relative_source():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        mod.PKG_DIR = Path(tmp)
        mod.MAP = {SKILL_SOURCE: "pool_delivery_vendored.py"}
        _run(mod, [])

        vendored = Path(tmp) / "pool_delivery_vendored.py"
        vendored.write_text(vendored.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

        rc, out, err = _run(mod, ["--check"])
        assert rc == 1
        assert "DRIFT" in err
        assert "pool_delivery_vendored.py" in err


def test_missing_canonical_source_fails_with_repo_relative_path():
    mod = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        mod.PKG_DIR = Path(tmp)
        missing_rel = "skills/worker-pool/scripts/does-not-exist.py"
        mod.MAP = {missing_rel: "does-not-exist.py"}

        rc, out, err = _run(mod, ["--check"])
        assert rc == 1
        assert "MISSING canonical source" in err
        assert str(mod.REPO_ROOT / missing_rel) in err


if __name__ == "__main__":
    test_skill_path_source_is_vendored()
    test_drift_detected_on_a_repo_relative_source()
    test_missing_canonical_source_fails_with_repo_relative_path()
    print("PASS — sync_from_src.py vendors repo-relative sources outside src/")
