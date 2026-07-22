#!/usr/bin/env python3
"""CLI boundary tests for src/dm-result.py."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "dm-result.py"


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
