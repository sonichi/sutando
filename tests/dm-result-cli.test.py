#!/usr/bin/env python3
"""CLI boundary tests for src/dm-result.py."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "dm-result.py"

spec = importlib.util.spec_from_file_location("dm_result_cli", SCRIPT)
dm = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(dm)


def run_help(flag: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = td
        env["DISCORD_BOT_TOKEN"] = ""
        env["SUTANDO_DM_OWNER_ID"] = ""
        return subprocess.run(
            [sys.executable, str(SCRIPT), flag],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )


def main() -> int:
    for flag in ("-h", "--help"):
        result = run_help(flag)
        assert result.returncode == 0, (flag, result.stderr)
        assert result.stdout.startswith("Usage: python3 src/dm-result.py")
        assert "sending to Discord DM" not in result.stdout
        assert "sent to DM" not in result.stdout
        print(f"ok: {flag} prints help without entering the DM delivery path")

    original_argv = dm.sys.argv
    original_voice_connected = dm.voice_connected
    try:
        dm.voice_connected = lambda: (_ for _ in ()).throw(
            AssertionError("help must return before the voice/network path")
        )
        for flag in ("-h", "--help"):
            dm.sys.argv = [str(SCRIPT), flag]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                dm.main()
            assert stdout.getvalue().startswith("Usage: python3 src/dm-result.py")
            print(f"ok: direct main({flag}) returns before voice/network work")

        dm.sys.argv = [str(SCRIPT)]
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr):
                dm.main()
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("missing arguments should exit 1")
        assert stderr.getvalue().startswith("Usage: python3 src/dm-result.py")
        print("ok: missing arguments print usage and exit 1")
    finally:
        dm.sys.argv = original_argv
        dm.voice_connected = original_voice_connected
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
