#!/usr/bin/env python3
"""Test the Fable quota-tracking + open/close reminder in read-quota.py.

Two small features (no model switching — reminder only):
  1. read-quota tags each reading with the active core model (`core_model`/`on_fable`).
  2. `--fable-remind` prints a one-line reminder to switch to/from Fable based on
     the 5h utilization thresholds (or "" when neither condition holds / stale).

Isolation: all state lives in a throwaway temp workspace. `$SUTANDO_WORKSPACE`
is no longer honored (v0.8 / #1440), so the redirect is done by monkeypatching
`workspace_default.resolve_workspace` BEFORE the module import — read-quota
binds `status_read_path` at import and that helper looks up `resolve_workspace`
as a module global at call time, so every path (QUOTA_FILE and the burn-history
file, both resolved at import) lands in the temp dir. Neither the quota state
this test writes nor the burn history the script updates ever touches the
user's real workspace; case (j) asserts that. The module runs IN-PROCESS
(importlib + main() with patched argv), not via subprocess, so the coverage
gate actually measures the exercised lines.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
READ_QUOTA = _REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"

_WS = Path(tempfile.mkdtemp(prefix="quota-fable-test-ws-"))
(_WS / "state").mkdir()
QUOTA_FILE = _WS / "state" / "quota-state.json"  # status_read_path layout


def _write_state(util_5h: float, util_7d: float = 0.2) -> None:
    QUOTA_FILE.write_text(json.dumps({"headers": {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": str(util_5h),
        "anthropic-ratelimit-unified-7d-utilization": str(util_7d),
    }}))
    os.utime(QUOTA_FILE, None)  # fresh mtime so it isn't flagged stale


# Redirect the workspace BEFORE import — QUOTA_FILE and the burn-history path
# are resolved at import time (and import exits if the quota file is missing,
# so seed it first). read-quota imports workspace_default from <repo>/src; by
# importing it first and patching resolve_workspace, the module under test
# resolves every state path into the temp workspace.
sys.path.insert(0, str(_REPO / "src"))
import workspace_default  # noqa: E402

workspace_default.resolve_workspace = lambda *a, **k: _WS
_write_state(0.5)
_spec = importlib.util.spec_from_file_location("read_quota_under_test", READ_QUOTA)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _run(args: list, core_model: str = "") -> str:
    """Call main() in-process with patched argv/env; return captured stdout."""
    old_argv = sys.argv
    old_model = os.environ.get("SUTANDO_CORE_MODEL")
    sys.argv = ["read-quota.py"] + args
    os.environ["SUTANDO_CORE_MODEL"] = core_model
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            _mod.main()
    finally:
        sys.argv = old_argv
        if old_model is None:
            os.environ.pop("SUTANDO_CORE_MODEL", None)
        else:
            os.environ["SUTANDO_CORE_MODEL"] = old_model
    return out.getvalue()


def _remind(core_model: str = "") -> str:
    return _run(["--fable-remind"], core_model=core_model).strip()


def run():
    fails, passed = [], 0
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
        j = json.loads(_run(["--json"], core_model="claude-fable-5"))
        if j.get("core_model") == "claude-fable-5" and j.get("on_fable") is True:
            passed += 1
        else:
            fails.append("(e) --json should expose core_model + on_fable")

        # (f) no-flags human output, ON Fable at low util → Core model line
        # with the Fable attribution tag AND the close-Fable reminder line.
        _write_state(0.30)
        out = _run([], core_model="claude-fable-5")
        if ("Core model: claude-fable-5" in out
                and "— Fable" in out
                and "switch back off Fable" in out):
            passed += 1
        else:
            fails.append(f"(f) no-flags on-Fable output missing tag/reminder: {out!r}")

        # (g) no-flags human output, non-Fable at mid util → Core model line
        # WITHOUT the Fable tag and no reminder line.
        _write_state(0.55)
        out = _run([], core_model="claude-opus-4-8")
        if ("Core model: claude-opus-4-8" in out
                and "— Fable" not in out
                and "⚡" not in out
                and "switch back off Fable" not in out):
            passed += 1
        else:
            fails.append(f"(g) no-flags non-Fable output wrong: {out!r}")

        # (h) no-flags human output with no core model set → no Core model line.
        _write_state(0.55)
        out = _run([], core_model="")
        if "Core model:" not in out:
            passed += 1
        else:
            fails.append(f"(h) empty core model should print no Core model line: {out!r}")

        # (i) stale state → no reminder even at high util (never advise on
        # stale numbers).
        _write_state(0.85)
        os.utime(QUOTA_FILE, (1, 1))  # ancient mtime → stale
        if _remind(core_model="claude-opus-4-8") == "":
            passed += 1
        else:
            fails.append("(i) stale state should suppress the reminder")

        # (j) isolation: burn-history writes landed in the temp workspace.
        if (_WS / "state" / "quota-burn-history.json").exists():
            passed += 1
        else:
            fails.append("(j) burn history should land in the temp workspace")
    finally:
        shutil.rmtree(_WS, ignore_errors=True)

    if fails:
        print(f"FAIL ({passed} passed):")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print(f"OK — {passed} passed")


if __name__ == "__main__":
    run()
