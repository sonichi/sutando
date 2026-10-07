#!/usr/bin/env python3
"""Behavioral test for skill-path resolution in the bridge task-file injection
(PR #1845: use claude_home_path() instead of inline os.environ.get).

Replaces the deleted source-grep guard (tests/bridge-skill-hints-injection.test.py)
with BEHAVIORAL coverage of the actual contract Susan flagged: the
===SKILL INSTRUCTIONS=== block lands in owner task files with notify/transcribe
commands whose paths are resolved via claude_home_path — i.e. honouring
$CLAUDE_CONFIG_DIR, which the old inline `os.environ.get(...)/~/.claude` pattern
missed.

Coverage:
1. claude_home_path() honours $CLAUDE_CONFIG_DIR (the shared helper all three
   bridges call identically) — the exact resolution the inline pattern got wrong.
2. slack-bridge._write_task end-to-end: an owner task with the task-progress
   skill installed under $CLAUDE_CONFIG_DIR gets a NOTIFY command whose path is
   under that dir (not ~/.claude) — proving the fix at the real call site.
3. skill-existence gate: owner + no skill installed → no NOTIFY line.
4. owner-only gate: a non-owner task gets no SKILL INSTRUCTIONS block.
5. cross-bridge convention: discord + telegram call the same
   claude_home_path("skills", "task-progress", ...) helper (single-line, so a
   structural assert is the right tool — behavioral import of discord.py /
   python-telegram-bot at test time is not worth the harness cost; the shared
   helper is behaviorally covered in (1)).

Run: python3 tests/bridge-skill-path-resolution.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── temp CLAUDE_CONFIG_DIR with the task-progress skill installed ────────────
_ccd = tempfile.mkdtemp(prefix="sutando-ccd-")
_notify_rel = ("skills", "task-progress", "scripts", "notify.py")
_notify_abs = Path(_ccd, *_notify_rel)
_notify_abs.parent.mkdir(parents=True, exist_ok=True)
_notify_marker = _notify_abs.with_name("seen-claude-config-dir.txt")
_notify_abs.write_text(
    "import os\n"
    "from pathlib import Path\n"
    f"Path({str(_notify_marker)!r}).write_text(os.environ.get('CLAUDE_CONFIG_DIR', ''))\n"
)

_ws = tempfile.mkdtemp(prefix="sutando-ws-")
os.environ["CLAUDE_CONFIG_DIR"] = _ccd
os.environ["SUTANDO_WORKSPACE"] = _ws
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"

# ── (1) shared helper: claude_home_path honours $CLAUDE_CONFIG_DIR ───────────
sys.path.insert(0, str(REPO / "src"))
from util_paths import claude_home_path  # noqa: E402

resolved = claude_home_path(*_notify_rel)
check(
    "claude_home_path resolves under $CLAUDE_CONFIG_DIR (the step the inline pattern missed)",
    str(resolved) == str(_notify_abs),
    f"{resolved} != {_notify_abs}",
)

# ── slack-bridge harness (stub slack_bolt, mirrors slack-bridge-chunking) ────
class _FakeApp:
    def __init__(self, token=None):
        self.client = types.SimpleNamespace(
            chat_postMessage=lambda **k: {"ok": True},
            conversations_replies=lambda **k: {"ok": True, "messages": []},
        )

    def _decorator(self, *a, **k):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


_bolt = types.ModuleType("slack_bolt")
_bolt.App = _FakeApp
sys.modules["slack_bolt"] = _bolt
_adapter = types.ModuleType("slack_bolt.adapter")
_socket = types.ModuleType("slack_bolt.adapter.socket_mode")
_socket.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter"] = _adapter
sys.modules["slack_bolt.adapter.socket_mode"] = _socket

spec = importlib.util.spec_from_file_location("slackbridge_spr", REPO / "src" / "slack-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TASKS_DIR = Path(_ws) / "tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
mod.TASKS_DIR = TASKS_DIR


_TS_SEQ = itertools.count(1)


def write_task(text: str, user_id: str = "U_OWNER", tier_map: dict | None = None) -> Path | None:
    """Call _write_task with access control mocked; return the written file path.

    ts is unique per call: the ingress replay-dedup (already_admitted) drops a
    second admission of the same provider ts as a replay.
    """
    event = {"user": user_id, "channel": "CFAKE", "channel_type": "im",
             "ts": f"1000.{next(_TS_SEQ):03d}"}
    effective_tier_map = tier_map if tier_map is not None else {user_id: "owner"}
    with patch.object(mod, "load_allowed", lambda: {user_id}), \
         patch.object(mod, "_ensure_tier_map_seeded", lambda: True), \
         patch.object(mod, "load_tier_map", lambda: effective_tier_map):
        task_id = mod._write_task(event, "DM", text, "testowner")
    if not task_id:
        return None
    p = TASKS_DIR / f"{task_id}.txt"
    return p if p.exists() else None


# ── (2) owner + skill installed → NOTIFY path is under $CLAUDE_CONFIG_DIR ─────
p_owner = write_task("please check the Zacks report")
check("owner task: file written", p_owner is not None)
if p_owner:
    body = p_owner.read_text()
    check("owner task: SKILL INSTRUCTIONS block present", "===SKILL INSTRUCTIONS" in body)
    check(
        "owner task: NOTIFY command path resolved under $CLAUDE_CONFIG_DIR (not ~/.claude)",
        str(_notify_abs) in body,
        "notify.py path not resolved via claude_home_path($CLAUDE_CONFIG_DIR)",
    )
    check(
        "owner task: notify path is NOT the ~/.claude default",
        str(Path.home() / ".claude" / "skills" / "task-progress") not in body,
    )
    notify_line = next(
        line for line in body.splitlines() if "NOTIFY FIRST:" in line
    )
    notify_command = notify_line.split("NOTIFY FIRST:", 1)[1].strip()
    clean_env = os.environ.copy()
    clean_env.pop("CLAUDE_CONFIG_DIR", None)
    clean_env.pop("CLAUDE_HOME", None)
    ran = subprocess.run(
        notify_command,
        shell=True,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    check(
        "owner task: injected NOTIFY command runs without inherited CLAUDE_CONFIG_DIR",
        ran.returncode == 0,
        ran.stderr,
    )
    check(
        "owner task: injected NOTIFY command restores resolved CLAUDE_CONFIG_DIR",
        _notify_marker.exists() and _notify_marker.read_text() == _ccd,
        "generated command did not propagate the bridge-resolved config dir",
    )

# ── (3) skill-existence gate: owner, but skill absent → no NOTIFY line ────────
_ccd_empty = tempfile.mkdtemp(prefix="sutando-ccd-empty-")
with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": _ccd_empty}):
    p_no_skill = write_task("another task, no skills installed")
check("owner + no skill installed: task still written", p_no_skill is not None)
if p_no_skill:
    body2 = p_no_skill.read_text()
    check(
        "owner + no skill installed: no NOTIFY line (existence gate honoured)",
        "NOTIFY FIRST:" not in body2,
    )

# ── (4) owner-only gate: non-owner tier → no SKILL INSTRUCTIONS block ─────────
p_other = write_task("hello from a stranger", user_id="U_OTHER", tier_map={"U_OWNER": "owner"})
check("non-owner task: written (not silently dropped)", p_other is not None)
if p_other:
    body3 = p_other.read_text()
    check("non-owner task: access_tier: other", "access_tier: other" in body3)
    check("non-owner task: no SKILL INSTRUCTIONS block (owner-gated)", "===SKILL INSTRUCTIONS" not in body3)

# ── (5) cross-bridge convention: discord + telegram use the same helper ───────
disc = (REPO / "src" / "discord-bridge.py").read_text()
tg = (REPO / "src" / "telegram-bridge.py").read_text()
_call = 'claude_home_path("skills", "task-progress", "scripts", "notify.py")'
check("discord bridge builds the notify path via claude_home_path", _call in disc)
check("telegram bridge builds the notify path via claude_home_path", _call in tg)

if failures:
    print(f"\nFAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nPASS — bridge skill-path resolution behavioral tests")
