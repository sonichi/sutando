#!/usr/bin/env python3
"""Tests for hooks/human-action-bridge.py — the remote-ask upgrade of the
AskUserQuestion hard-deny (human-action bridge v1 step 1).

Covers the safety invariants from notes/tasks-events/human_action_bridge_design.md:
timeout NEVER approves; only an explicit resolved decision produces an allow;
fail-open for the session; terminal states immutable; card is best-effort.
Runs the hook as a subprocess exactly as Claude Code would. Exit 0/1."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "human-action-bridge.py"

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _hook_input(tool="AskUserQuestion"):
    return {
        "tool_name": tool,
        "session_id": "sess_test",
        "hook_event_name": "PreToolUse",
        "tool_input": {
            "questions": [{
                "question": "Ship v1 or wait?",
                "header": "Scope",
                "multiSelect": False,
                "options": [
                    {"label": "Ship v1", "description": "smallest useful slice"},
                    {"label": "Wait", "description": "bundle with v2"},
                ],
            }],
        },
    }


def _run(payload, env_extra, timeout=30):
    env = {**os.environ, **env_extra}
    p = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    out = None
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out = json.loads(line)
            break
    return p, out


def _dirs():
    base = Path(tempfile.mkdtemp())
    store, cards = base / "actions", base / "cards"
    return {
        "SUTANDO_HA_DIR": str(store),
        "SUTANDO_HA_CARD_DIR": str(cards),
        "SUTANDO_HA_POLL": "0.1",
    }, store, cards


def test_other_tools_pass_through():
    env, store, _ = _dirs()
    p, out = _run(_hook_input(tool="Bash"), {**env, "SUTANDO_HA_TIMEOUT": "1"})
    check(p.returncode == 0 and out is None,
          "non-AskUserQuestion tools pass through untouched (no decision JSON)")
    check(not store.exists() or not list(store.glob("*.json")),
          "no action file is created for other tools")


def test_timeout_denies_and_expires():
    env, store, cards = _dirs()
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "1"})
    d = out["hookSpecificOutput"]
    check(d["permissionDecision"] == "deny",
          "TIMEOUT NEVER APPROVES — no response ⇒ deny")
    check("decide autonomously" in d["permissionDecisionReason"],
          "timeout deny carries the decide-autonomously guidance (today's behavior)")
    actions = list(store.glob("ha_*.json"))
    check(len(actions) == 1, "one durable action file created")
    rec = json.loads(actions[0].read_text())
    check(rec["status"] == "expired", "unanswered action is stamped expired")
    check(rec["audit"][-1]["event"] == "expired", "expiry is audited")
    card = list(cards.glob("proactive-ha-*.txt"))
    check(len(card) == 1 and "Ship v1" in card[0].read_text(),
          "owner card written with the options rendered")


def test_decision_allows_with_answers():
    env, store, _ = _dirs()

    def resolve_soon():
        # act like the DecisionHandler: wait for the pending file, write decision
        deadline = time.time() + 5
        while time.time() < deadline:
            files = list(store.glob("ha_*.json"))
            if files:
                rec = json.loads(files[0].read_text())
                if rec["status"] == "pending":
                    rec["status"] = "resolved"
                    rec["decision"] = {"answers": {"Ship v1 or wait?": "Ship v1"}}
                    rec["resolved_by"] = "@qingyun:ag2.space"
                    files[0].write_text(json.dumps(rec))
                    return
            time.sleep(0.05)

    t = threading.Thread(target=resolve_soon)
    t.start()
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "6"})
    t.join()
    d = out["hookSpecificOutput"]
    check(d["permissionDecision"] == "allow",
          "an explicit resolved decision produces allow")
    check(d["updatedInput"]["answers"] == {"Ship v1 or wait?": "Ship v1"},
          "owner's answers ride back via updatedInput")
    check(d["updatedInput"]["questions"],
          "updatedInput preserves the original questions (answers-injection contract)")


def test_cancelled_action_denies():
    env, store, _ = _dirs()

    def cancel_soon():
        deadline = time.time() + 5
        while time.time() < deadline:
            files = list(store.glob("ha_*.json"))
            if files:
                rec = json.loads(files[0].read_text())
                rec["status"] = "cancelled"
                files[0].write_text(json.dumps(rec))
                return
            time.sleep(0.05)

    t = threading.Thread(target=cancel_soon)
    t.start()
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "6"})
    t.join()
    check(out["hookSpecificOutput"]["permissionDecision"] == "deny",
          "a cancelled action denies — fail-closed for the decision")


def test_garbage_stdin_fails_open():
    env, _, _ = _dirs()
    p = subprocess.run([sys.executable, str(HOOK)], input="{not json",
                       capture_output=True, text=True,
                       env={**os.environ, **env}, timeout=10)
    check(p.returncode == 0 and not p.stdout.strip().startswith("{"),
          "garbage stdin fails OPEN (exit 0, no decision) — never wedge the core")


def test_card_failure_still_waits_then_denies():
    env, store, _ = _dirs()
    # point the card dir at an uncreatable path (child of a FILE)
    blocker = Path(tempfile.mkdtemp()) / "file"
    blocker.write_text("x")
    env["SUTANDO_HA_CARD_DIR"] = str(blocker / "sub")
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "1"})
    check(out["hookSpecificOutput"]["permissionDecision"] == "deny",
          "card-write failure is non-fatal: hook still waits then denies (best-effort card)")
    check(list(store.glob("ha_*.json")),
          "action file exists even when the card could not be written")


def test_timeout_cannot_overwrite_a_racing_resolution():
    # Review blocker (CAS): a resolution landing between the hook's last poll
    # and its expiry write must WIN. Deterministic version of the race: hold
    # the shared transition lock (the same flock the resolver uses), let the
    # hook hit its timeout and block on the lock, resolve while holding, then
    # release — the expiry path must re-read, see `resolved`, and HONOR it
    # (allow + answers), never stamp `expired` over the decision.
    import fcntl
    env, store, _ = _dirs()
    store.mkdir(parents=True, exist_ok=True)

    holder = {}

    def hold_lock_then_resolve():
        deadline = time.time() + 5
        while time.time() < deadline and not list(store.glob("ha_*.json")):
            time.sleep(0.05)
        files = list(store.glob("ha_*.json"))
        if not files:
            return
        lock = open(store / (files[0].stem + ".lock"), "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        holder["locked"] = True
        time.sleep(1.5)  # hook's 1s timeout passes while we hold the lock
        rec = json.loads(files[0].read_text())
        rec["status"] = "resolved"
        rec["decision"] = {"answers": {"Ship v1 or wait?": "Ship v1"}}
        rec["resolved_by"] = "@qingyun:ag2.space"
        files[0].write_text(json.dumps(rec))
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()

    t = threading.Thread(target=hold_lock_then_resolve)
    t.start()
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "1"}, timeout=30)
    t.join()
    d = out["hookSpecificOutput"]
    check(holder.get("locked") and d["permissionDecision"] == "allow",
          "CAS: a resolution racing the expiry write WINS (allow, not expired)")
    rec = json.loads(list(store.glob("ha_*.json"))[0].read_text())
    check(rec["status"] == "resolved",
          "CAS: the action file keeps the decision — expiry never overwrote it")


def test_action_writes_are_durable_and_uniquely_named():
    # Review blocker: pending-action state must be fsync-durable (file + dir)
    # with per-writer-unique temp names. Verify via the module's own writer.
    import importlib.util
    spec = importlib.util.spec_from_file_location("hab", str(HOOK))
    hab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hab)
    d = Path(tempfile.mkdtemp())
    fsynced, replaced = [], []
    orig_fsync, orig_replace = os.fsync, os.replace
    os.fsync = lambda fd: fsynced.append(fd)
    os.replace = lambda a, b: (replaced.append(str(a)), orig_replace(a, b))[1]
    try:
        hab._atomic_write(d / "ha_x.json", {"status": "pending"})
    finally:
        os.fsync, os.replace = orig_fsync, orig_replace
    check(len(fsynced) >= 2, "durable write: fsyncs the file AND the directory")
    check(str(os.getpid()) in replaced[0] and not replaced[0].endswith("json.tmp"),
          "durable write: per-writer-unique temp name (no fixed .tmp collision)")


def test_card_is_written_atomically():
    # Review P2: the card lands on a bridge-consumed path — a poller must never
    # see a half-written file, so it must arrive via temp + os.replace.
    env, _, cards = _dirs()
    replaced = []
    # run the hook and observe the card path appears fully-formed with no
    # lingering temp (the rename target is the final name).
    p, out = _run(_hook_input(), {**env, "SUTANDO_HA_TIMEOUT": "1"})
    card_files = list(cards.glob("proactive-ha-*.txt"))
    tmp_files = list(cards.glob("*.tmp"))
    check(len(card_files) == 1 and not tmp_files,
          "card: final file present, no temp residue (temp+rename path)")
    check("Reply:" in card_files[0].read_text(),
          "card: content complete (rendered before the rename)")
    _ = replaced


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — human-action-bridge hook (v1 step 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
