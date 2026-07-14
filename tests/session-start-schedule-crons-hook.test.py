"""
Tests for scripts/install-session-start-hook.sh (Python-accessible logic).

The installer's core logic is a Python fragment. These tests exercise it
directly — no subprocess overhead — and are discovered by CI's Python test
runner alongside other *.test.py files.

Covers:
  - Fresh install (settings.json absent)
  - Install with existing other hooks (preserves PreCompact/Stop)
  - Idempotency (hook already present → no duplicate)
  - Adds alongside different existing SessionStart commands
"""

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HINT_CMD = f'bash "{REPO}/src/schedule-crons-session-hint.sh"'

_pass = 0
_fail = 0


def ok(label: str) -> None:
    global _pass
    print(f"  PASS: {label}")
    _pass += 1


def fail(label: str, detail: str = "") -> None:
    global _fail
    detail_str = f" — {detail}" if detail else ""
    print(f"  FAIL: {label}{detail_str}", file=sys.stderr)
    _fail += 1


def run_installer(settings_path: str, hook_cmd: str) -> str:
    """Mirror of the Python block in install-session-start-hook.sh."""
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    if not os.path.exists(settings_path):
        with open(settings_path, "w") as f:
            json.dump({"hooks": {}}, f)

    with open(settings_path) as f:
        settings = json.load(f)

    hooks = settings.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    for entry in session_start:
        for h in entry.get("hooks", []):
            if h.get("command", "") == hook_cmd:
                return "already installed"

    session_start.insert(0, {
        "matcher": "",
        "hooks": [{"type": "command", "command": hook_cmd}]
    })

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return "installed"


# ── Test 1: fresh install (settings.json absent) ──────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    settings_path = os.path.join(tmp, ".claude", "settings.json")
    result = run_installer(settings_path, HINT_CMD)
    with open(settings_path) as f:
        data = json.load(f)
    cmds = [h["command"] for e in data["hooks"].get("SessionStart", []) for h in e.get("hooks", [])]
    if result == "installed" and HINT_CMD in cmds:
        ok("fresh install creates settings.json with SessionStart hook")
    else:
        fail("fresh install", f"result={result!r} cmds={cmds!r}")

# ── Test 2: existing settings with other hooks, no SessionStart ───────────────
with tempfile.TemporaryDirectory() as tmp:
    settings_path = os.path.join(tmp, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path))
    base = {
        "hooks": {
            "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": "echo precompact"}]}],
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo stop"}]}],
        }
    }
    with open(settings_path, "w") as f:
        json.dump(base, f)

    run_installer(settings_path, HINT_CMD)

    with open(settings_path) as f:
        data = json.load(f)
    has_session_start = HINT_CMD in [
        h["command"] for e in data["hooks"].get("SessionStart", []) for h in e.get("hooks", [])
    ]
    has_precompact = "PreCompact" in data["hooks"]
    has_stop = "Stop" in data["hooks"]
    if has_session_start and has_precompact and has_stop:
        ok("installer adds SessionStart and preserves PreCompact + Stop")
    else:
        fail("preserve other hooks", f"session_start={has_session_start} precompact={has_precompact} stop={has_stop}")

# ── Test 3: idempotency ───────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    settings_path = os.path.join(tmp, ".claude", "settings.json")
    run_installer(settings_path, HINT_CMD)
    result2 = run_installer(settings_path, HINT_CMD)

    with open(settings_path) as f:
        data = json.load(f)
    count = len(data["hooks"].get("SessionStart", []))
    if result2 == "already installed" and count == 1:
        ok("installer is idempotent (no duplicate on second run)")
    else:
        fail("idempotency", f"result={result2!r} SessionStart entry count={count}")

# ── Test 4: adds alongside existing different-command SessionStart entry ───────
with tempfile.TemporaryDirectory() as tmp:
    settings_path = os.path.join(tmp, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path))
    base = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo other-hook"}]}]
        }
    }
    with open(settings_path, "w") as f:
        json.dump(base, f)

    result = run_installer(settings_path, HINT_CMD)

    with open(settings_path) as f:
        data = json.load(f)
    count = len(data["hooks"].get("SessionStart", []))
    if result == "installed" and count == 2:
        ok("installer adds alongside existing different-command SessionStart entry")
    else:
        fail("add alongside existing", f"result={result!r} count={count}")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\nResults: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
