#!/usr/bin/env python3
"""Test the Fable quota-tracking + open/close reminder in read-quota.py.

Two small features (no model switching — reminder only):
  1. read-quota tags each reading with the active core model (`core_model`/`on_fable`).
  2. `--fable-remind` prints a one-line reminder to switch to/from Fable based on
     the 5h utilization thresholds (or "" when neither condition holds / stale).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
from workspace_default import status_read_path  # noqa: E402

READ_QUOTA = _REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"
QUOTA_FILE = status_read_path("quota-state.json")


def _write_state(util_5h: float, util_7d: float = 0.2) -> None:
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps({"headers": {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": str(util_5h),
        "anthropic-ratelimit-unified-7d-utilization": str(util_7d),
    }}))
    os.utime(QUOTA_FILE, None)  # fresh mtime so it isn't flagged stale


def _remind(core_model: str = "") -> str:
    env = dict(os.environ, SUTANDO_CORE_MODEL=core_model)
    out = subprocess.run([sys.executable, str(READ_QUOTA), "--fable-remind"],
                         capture_output=True, text=True, env=env)
    return out.stdout.strip()


def run():
    fails, passed = [], 0
    backup = QUOTA_FILE.read_text() if QUOTA_FILE.exists() else None
    try:
        # (a) high util on a non-Fable model → suggest OPENING Fable
        _write_state(0.85)
        if "Fable 5" in _remind(core_model="claude-opus-4-8"):
            passed += 1
        else:
            fails.append("(a) high-util non-Fable should suggest opening Fable")

        # (b) low util while ON Fable → suggest CLOSING Fable
        _write_state(0.30)
        if "switch back off Fable" in _remind(core_model="claude-fable-5"):
            passed += 1
        else:
            fails.append("(b) low-util on-Fable should suggest closing Fable")

        # (c) mid util, not on Fable → no reminder
        _write_state(0.55)
        if _remind(core_model="claude-opus-4-8") == "":
            passed += 1
        else:
            fails.append("(c) mid-util should produce no reminder")

        # (d) on Fable but still high util → no "close" reminder yet
        _write_state(0.85)
        if _remind(core_model="claude-fable-5") == "":
            passed += 1
        else:
            fails.append("(d) on-Fable high-util should not suggest closing yet")

        # (e) --json exposes core_model / on_fable
        _write_state(0.5)
        env = dict(os.environ, SUTANDO_CORE_MODEL="claude-fable-5")
        j = json.loads(subprocess.run(
            [sys.executable, str(READ_QUOTA), "--json"],
            capture_output=True, text=True, env=env).stdout)
        if j.get("core_model") == "claude-fable-5" and j.get("on_fable") is True:
            passed += 1
        else:
            fails.append("(e) --json should expose core_model + on_fable")
    finally:
        if backup is not None:
            QUOTA_FILE.write_text(backup)
        elif QUOTA_FILE.exists():
            QUOTA_FILE.unlink()

    if fails:
        print(f"FAIL ({passed} passed):")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print(f"OK — {passed} passed")


if __name__ == "__main__":
    run()
