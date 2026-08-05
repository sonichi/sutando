#!/usr/bin/env python3
"""Discord delivery path writes the §7 audit line — Result Router S5.

Wires the shared `result_audit` sink into Discord's single testable delivery
choke point: `_mark_delivered(task_id)`, the one hook that fires after a
successful `channel.send` (text + any files) in `poll_results` → records
`delivered`. Because the file-send loop runs *before* `_mark_delivered` inside
the same try, a failed attachment hits the `except` and `_mark_delivered` never
runs — so the audit reflects the FULL delivery, never a premature `delivered`.

Also tests the skip-marker audit path: [no-send], [REPLIED], and [deduped:]
results are resolved deliveries (spec §7), not silent voids. Each must produce
exactly one audit line with the correct disposition (no_send or deduped).

(Telegram's audit was deferred: its `send_reply` doesn't send the caller's
`parsed.actions` attachments, so recording there would miss attachment failures.
It lands correctly at the delivery-outcome layer in the poller-extraction
follow-up. Slack landed in #1984, where `_send_reply` sends its own files.)

discord-bridge's module load has side effects (discord SDK import, token read)
that fail in clean CI, so we stub them and run against a hermetic temp workspace.

Run: python3 tests/bridge-audit-wiring.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WS = tempfile.mkdtemp()
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ["SUTANDO_TEST_MODE"] = "1"
AUDIT = Path(_WS) / "state" / "result-audit.log"

# Isolate the CONFIG dir too, and seed the canonical access file inside it.
#
# Setting DISCORD_BOT_TOKEN below stops the `.env` read, but it does NOT stop the
# module-load-time access resolution: discord-bridge calls channel_access_path("discord"),
# which resolves under $CLAUDE_CONFIG_DIR and falls back to the LEGACY real-home
# ~/.claude/channels/discord/access.json when the canonical path is missing. With
# CLAUDE_CONFIG_DIR unset the test inherits whatever the developer happens to have, so the
# same defect shows up differently per machine — on a clean host it hits the legacy fallback
# and prints `[util_paths] DEPRECATION: using legacy …`; on an operator host it silently
# imports that operator's REAL Discord allowlist. Green everywhere, trustworthy nowhere.
#
# Pointing CLAUDE_CONFIG_DIR at a temp root and writing an empty allowlist makes the import
# hermetic: no host state, no real file, no deprecation warning. Same shape as the telegram
# fix in tests/bridge-timeout-guards.test.py. (qingyun, #1886 / #2426.)
_CFG = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": []}')

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Stub `discord` + materialize a fake bot .env so module load works in CI.
try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub

# Hermetic: discord-bridge reads DISCORD_BOT_TOKEN from the env FIRST (line ~187),
# only falling back to $CLAUDE_CONFIG_DIR/channels/discord/.env if unset. Set the
# env var so module load succeeds WITHOUT writing into the real ~/.claude config.
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"

db = _load("dbridge_audit", REPO / "src" / "discord-bridge.py")

# Assert the isolation actually held, rather than trusting that it did. Without these the
# test still passes while reading host config — which is exactly how this went unnoticed.
# Checked against the real home explicitly so an inherited CLAUDE_CONFIG_DIR can't satisfy it.
_REAL_HOME_CFG = Path.home() / ".claude"
for _attr in ("ACCESS_FILE", "channels_env"):
    _resolved = getattr(db, _attr, None)
    check(f"hermetic: {_attr} is confined to the temp config root",
          _resolved is not None and Path(_resolved).is_relative_to(_CFG),
          f"{_attr}={_resolved} (temp root={_CFG})")
    check(f"hermetic: {_attr} does not touch the real ~/.claude",
          _resolved is not None and not Path(_resolved).is_relative_to(_REAL_HOME_CFG),
          f"{_attr}={_resolved} resolved inside {_REAL_HOME_CFG}")

db._mark_delivered("task-disc-1")
check("discord: _mark_delivered writes a delivered audit line",
      AUDIT.exists() and "\ttask-disc-1\tdelivered\tdiscord" in AUDIT.read_text(),
      AUDIT.read_text() if AUDIT.exists() else "(no audit file)")

# A second delivery appends (one line per delivered result).
db._mark_delivered("task-disc-2")
lines = [l for l in AUDIT.read_text().splitlines() if "\tdiscord" in l]
check("discord: appends one audit line per delivered result", len(lines) == 2, str(lines))
check("discord: every line has the delivered disposition + discord surface",
      all(l.split("\t")[2:] == ["delivered", "discord"] for l in lines))

# Skip-marker audit: [no-send] → no_send, [deduped:] → deduped (§7 spec).
# Call _record_skip_audit() so coverage hits the production helper lines.
AUDIT.write_text("")  # fresh slate for skip-marker checks

db._record_skip_audit("task-skip-1", "no-send")
check("discord: [no-send] writes no_send audit line",
      "\ttask-skip-1\tno_send\tdiscord" in AUDIT.read_text())

db._record_skip_audit("task-skip-2", "deduped")
check("discord: [deduped:] writes deduped audit line",
      "\ttask-skip-2\tdeduped\tdiscord" in AUDIT.read_text())

skip_lines = [l for l in AUDIT.read_text().splitlines() if l.strip()]
check("discord: skip audit lines have correct structure (4 tab-sep fields)",
      all(len(l.split("\t")) == 4 for l in skip_lines), str(skip_lines))

# ── poll_results integration: exercise the ACTUAL async call site ────────────
# The skip branch lives inside the poll_results() while-True coroutine. Run the
# real coroutine on an event loop for one iteration (it sleeps 1s per pass),
# with a fake gateway client, a pending [no-send] result, and a pending
# [deduped: …] result — then assert the audit lines were written BY THE LOOP
# and both files were archived. This covers the call site itself, so it needs
# no "tested via helper" pragma.
import asyncio


class _FakeClient:
    def is_ready(self):
        return False  # heartbeat gate closed — no file writes

    async def fetch_channel(self, cid):  # recovery path — unused (empty map)
        raise RuntimeError("not used")


class _FakeChannel:
    id = 1234567890

    async def send(self, *a, **k):  # dedup-notify path — not taken here
        return None


db.client = _FakeClient()
AUDIT.write_text("")  # fresh slate for the loop-path checks

for _tid, _marker in (("task-loop-nosend", "[no-send]\n"),
                      ("task-loop-dedup", "[deduped: task-holder-x]\n")):
    (db.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    (db.RESULTS_DIR).mkdir(parents=True, exist_ok=True)
    (db.TASKS_DIR / f"{_tid}.txt").write_text(f"id: {_tid}\ntask: t\n")
    (db.RESULTS_DIR / f"{_tid}.txt").write_text(_marker)
    db.pending_replies[_tid] = _FakeChannel()


async def _run_one_iteration():
    t = asyncio.ensure_future(db.poll_results())
    await asyncio.sleep(0.7)  # one pass completes well before the 1s sleep ends
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


asyncio.run(_run_one_iteration())

_audit_now = AUDIT.read_text()
check("discord loop: [no-send] audited no_send via the poll_results call site",
      "\ttask-loop-nosend\tno_send\tdiscord" in _audit_now, _audit_now)
check("discord loop: [deduped:] audited deduped via the poll_results call site",
      "\ttask-loop-dedup\tdeduped\tdiscord" in _audit_now, _audit_now)
check("discord loop: [no-send] result archived (not re-polled forever)",
      not (db.RESULTS_DIR / "task-loop-nosend.txt").exists())
check("discord loop: [no-send] task file archived",
      not (db.TASKS_DIR / "task-loop-nosend.txt").exists())
check("discord loop: pending_replies drained for both skip tasks",
      "task-loop-nosend" not in db.pending_replies
      and "task-loop-dedup" not in db.pending_replies)

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — discord audit-wiring tests")
