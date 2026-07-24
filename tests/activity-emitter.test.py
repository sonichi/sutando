#!/usr/bin/env python3
"""Tests for hooks/activity-emitter.py — the Activity-outbox journal source.
Covers: hook→type mapping, JSONL journaling, binding attribution, secret
hygiene (no raw command payloads), fail-open on garbage, unknown-hook
forward-compat. Runs the hook as a subprocess exactly as Claude Code would
(async command hook). Exit 0/1."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "activity-emitter.py"

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _run(payload, journal_dir, argv=None, raw=None):
    env = {**os.environ, "SUTANDO_ACTIVITY_DIR": str(journal_dir)}
    p = subprocess.run(
        [sys.executable, str(HOOK)] + (argv or []),
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=15)
    return p


def _journal(journal_dir):
    files = sorted(Path(journal_dir).glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(ln) for ln in files[-1].read_text().splitlines() if ln]


def test_mapping_and_journal():
    d = tempfile.mkdtemp()
    cases = [
        ({"hook_event_name": "SessionStart", "session_id": "s1"},
         "agent.session.started"),
        ({"hook_event_name": "PreToolUse", "session_id": "s1",
          "tool_name": "Bash", "tool_input": {"description": "Run tests"}},
         "agent.tool.started"),
        ({"hook_event_name": "PostToolUseFailure", "session_id": "s1",
          "tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}},
         "agent.tool.failed"),
        ({"hook_event_name": "Stop", "session_id": "s1"},
         "task.turn.completed"),
    ]
    for payload, _ in cases:
        p = _run(payload, d)
        check(p.returncode == 0, f"emitter exits 0 for {payload['hook_event_name']}")
    rows = _journal(d)
    check([r["type"] for r in rows] == [c[1] for c in cases],
          "hook→activity type mapping journals in order")
    check(all(r["activity_id"].startswith("act_") and r["occurred_at"].endswith("Z")
              for r in rows), "every activity carries id + UTC timestamp")
    tool_row = rows[1]
    check(tool_row["tool"] == {"kind": "Bash", "display": "Run tests"},
          "tool activities carry kind + display hint")


def test_no_raw_command_leaks():
    d = tempfile.mkdtemp()
    _run({"hook_event_name": "PreToolUse", "session_id": "s1",
          "tool_name": "Bash",
          "tool_input": {"command": "curl -H 'Authorization: Bearer sk-SECRET' x"}}, d)
    rows = _journal(d)
    dumped = json.dumps(rows)
    check("SECRET" not in dumped and "curl" not in dumped,
          "SECRET HYGIENE — raw command payloads never reach the journal")
    check(rows[0]["tool"] == {"kind": "Bash"},
          "command-only Bash input reduces to kind alone (no display)")


def test_url_hints_strip_query_and_fragment():
    # Codex P1: URLs routinely carry bearer tokens / presigned sigs / OAuth
    # codes in query or fragment — only scheme+host+path may reach the journal.
    d = tempfile.mkdtemp()
    _run({"hook_event_name": "PreToolUse", "session_id": "s",
          "tool_name": "WebFetch",
          "tool_input": {"url": "https://files.example/download?token=sk-SECRET&sig=abc#frag"}}, d)
    rows = _journal(d)
    dumped = json.dumps(rows)
    check("sk-SECRET" not in dumped and "sig=abc" not in dumped and "#frag" not in dumped,
          "URL SECRET HYGIENE — query + fragment never reach the journal")
    check(rows[0]["tool"]["display"] == "https://files.example/download",
          "URL hint keeps scheme+host+path (still useful display)")


def test_url_hints_strip_userinfo_credentials():
    # Second-review P1: netloc INCLUDES userinfo, so scheme+netloc+path still
    # leaked `user:password@`. The authority must be rebuilt from
    # hostname[:port] — username, password, query, fragment ALL absent.
    d = tempfile.mkdtemp()
    _run({"hook_event_name": "PreToolUse", "session_id": "s",
          "tool_name": "WebFetch",
          "tool_input": {"url":
              "https://alice:sk-SECRET@files.example:8443/download?tok=q#f"}}, d)
    rows = _journal(d)
    dumped = json.dumps(rows)
    check("sk-SECRET" not in dumped and "alice" not in dumped
          and "tok=q" not in dumped and "#f" not in dumped,
          "URL SECRET HYGIENE — userinfo (user:password@) never reaches the journal")
    check(rows[0]["tool"]["display"] == "https://files.example:8443/download",
          "URL hint authority rebuilt as hostname:port only")
    # an unparseable-port URL degrades to no hint, never a crash or a leak
    d2 = tempfile.mkdtemp()
    _run({"hook_event_name": "PreToolUse", "session_id": "s",
          "tool_name": "WebFetch",
          "tool_input": {"url": "https://bob:pw@files.example:not-a-port/x"}}, d2)
    dumped2 = json.dumps(_journal(d2))
    check("pw" not in dumped2 and "bob" not in dumped2,
          "URL SECRET HYGIENE — invalid-port URL drops the hint, no leak")


def test_binding_attribution():
    ws = Path(tempfile.mkdtemp())
    (ws / "state" / "bindings").mkdir(parents=True)
    (ws / "state" / "bindings" / "active-execution.json").write_text(json.dumps(
        {"task_id": "task-123", "room_id": "!r:hs", "generation": 7, "extra": "x"}))
    d = ws / "state" / "activity-journal"
    # point CLAUDE_CONFIG_DIR at <ws>/.claude-sutando so _workspace() → ws,
    # exercising the real binding-read path (no SUTANDO_ACTIVITY_DIR override).
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(ws / ".claude-sutando")}
    env.pop("SUTANDO_ACTIVITY_DIR", None)
    subprocess.run([sys.executable, str(HOOK)],
                   input=json.dumps({"hook_event_name": "Stop", "session_id": "s"}),
                   capture_output=True, text=True, env=env, timeout=15)
    rows = _journal(d)
    check(rows and rows[0]["task_id"] == "task-123" and rows[0]["room_id"] == "!r:hs"
          and rows[0]["generation"] == 7,
          "binding registry attribution rides on activities (task/room/generation)")
    check("extra" not in rows[0], "only the attribution keys are copied")


def test_unknown_hook_and_argv_fallback():
    d = tempfile.mkdtemp()
    _run({"hook_event_name": "SomeFutureHook", "session_id": "s"}, d)
    _run({"session_id": "s"}, d, argv=["SessionEnd"])
    rows = _journal(d)
    check(rows[0]["type"] == "agent.activity" and rows[0]["hook"] == "SomeFutureHook",
          "unknown hooks journal as generic agent.activity (forward-compatible)")
    check(rows[1]["type"] == "agent.session.ended",
          "argv hook-name fallback works when stdin lacks hook_event_name")


def test_fail_open():
    d = tempfile.mkdtemp()
    p = _run(None, d, raw="{not json")
    check(p.returncode == 0, "garbage stdin → exit 0 (NEVER wedge the core)")
    p2 = _run(None, d, raw="")
    check(p2.returncode == 0, "empty stdin → exit 0")
    rows = _journal(d)
    check(len(rows) == 1 and rows[0]["hook"] == "unknown",
          "empty input still journals a generic activity; garbage journals nothing")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — activity-emitter (journal source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
