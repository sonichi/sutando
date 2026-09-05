#!/usr/bin/env python3
"""Telegram adapter path: a .to-telegram FILENAME outranks a Discord body.

The claim helper says filename > body > activity, but the bridge's delivery
guard used to re-check the BODY alone — so a destined file whose body carried
a foreign redirect was claimed by nobody: every other bridge refused the
filename tag while telegram's own peek guard refused the body (kewei P1).

This drives the REAL telegram main loop (no routing stubs): the conflict file
must be sent; an undestined discord-bodied control must stay untouched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate the channel root BEFORE exec_module: the bridge resolves ACCESS_FILE
# at module level, and unset it falls back to the operator's real ~/.claude.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-conflict-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": ["4242"]}')

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _Stop(Exception):
    """Breaks main()'s poll loop after the drain has run."""


def _load(workspace: Path):
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_conflict_under_test", REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _swallow(fn):
    try:
        fn()
    except BaseException:
        pass


def split_write_drain():
    """Own drain, own workspace, ONE undestined file.

    An undestined proactive file falls through to activity routing
    (`should_claim_proactive`), so telegram only claims it when
    `state/last-owner-activity.json` names telegram — without that the file is
    never claimed and any assertion about it is vacuous.
    """
    ws = Path(tempfile.mkdtemp(prefix="tg-splitwrite-ws-"))
    results = ws / "results"
    results.mkdir(parents=True)
    (ws / "tasks").mkdir(exist_ok=True)
    (ws / "state").mkdir(exist_ok=True)
    (ws / "state" / "last-owner-activity.json").write_text(
        json.dumps({"channel": "telegram", "ts": time.time()}))
    mod = _load(ws)
    mod.RESULTS_DIR = results
    mod.ACCESS_FILE = _cfg / "access.json"
    mod.presenter_mode_active = lambda *_a, **_k: False
    mod.load_allowed = lambda: {"4242"}

    split = results / "proactive-split.txt"
    fd = open(split, "w")
    fd.write("**[core: 1]**\n")
    fd.flush()

    sent, claimed = [], []

    def _send_reply(_chat, text, task_id=None, message_thread_id=None):
        sent.append(text)
        return {"ok": True, "text_chunks": 1, "files_sent": 0}

    _orig_claim = mod.claim_for_delivery

    def _claim_then_producer_appends(path, recipient):
        claim = _orig_claim(path, recipient)
        if claim is not None and "proactive-split" in claim.name:
            claimed.append(claim.name)
            # The claim hard-links then unlinks; the producer still holds the
            # ORIGINAL descriptor and keeps writing THIS inode.
            fd.write("[channel: 1535008729106485288]\n"
                     "private discord-directed body\n")
            fd.flush()
        return claim

    def _api(method, **_kw):
        if claimed:
            raise _Stop()
        return {"ok": True, "result": []}

    mod.send_reply = _send_reply
    mod.claim_for_delivery = _claim_then_producer_appends
    mod.api = _api

    t = threading.Thread(target=lambda: _swallow(mod.main), daemon=True)
    t.start()
    deadline = time.time() + 12
    while time.time() < deadline and not claimed:
        time.sleep(0.1)
    time.sleep(1.0)
    fd.close()
    recoverable = split.exists() or bool(list(results.glob("proactive-split*.txt")))
    return claimed, sent, recoverable


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="tg-conflict-ws-"))
    results = ws / "results"
    results.mkdir(parents=True)
    (ws / "tasks").mkdir(exist_ok=True)
    mod = _load(ws)

    mod.RESULTS_DIR = results
    mod.ACCESS_FILE = _cfg / "access.json"
    mod.presenter_mode_active = lambda *_a, **_k: False
    mod.load_allowed = lambda: {"4242"}

    # The conflict: filename destines telegram, body redirects to a Discord
    # channel. The old body-only delivery guard skipped this file forever.
    conflict = results / "proactive-1.to-telegram.txt"
    conflict.write_text("[channel: 1535008729106485288]\nconflict body")
    # Control: same body WITHOUT the filename tag — body rules still apply,
    # telegram must not claim another bridge's file.
    control = results / "proactive-2.txt"
    control.write_text("[channel: 1535008729106485288]\nforeign body")

    sent: list[str] = []

    def _send_reply(_chat, text, task_id=None, message_thread_id=None):
        sent.append(text)
        return {"ok": True, "text_chunks": 1, "files_sent": 0}

    def _api(method, **_kw):
        # No updates ever; raise once the drain has had a turn so main() exits.
        if sent:
            raise _Stop()
        return {"ok": True, "result": []}

    mod.send_reply = _send_reply
    mod.api = _api

    t = threading.Thread(target=lambda: _swallow(mod.main), daemon=True)
    t.start()
    deadline = time.time() + 8
    while time.time() < deadline and not sent:
        time.sleep(0.1)
    time.sleep(0.6)

    check("conflict file was DELIVERED on telegram (filename outranks body)",
          any("conflict body" in s for s in sent), f"sent={sent!r}")
    check("delivered file is consumed", not conflict.exists())
    check("no .sending claim left behind",
          not list(results.glob("proactive-1*.sending*")))
    check("undestined discord-bodied control is NOT claimed by telegram",
          control.exists() and not any("foreign body" in s for s in sent))

    # Split write, in its OWN drain: the shared drain above exits on the first
    # send, so a third file there is never reached and asserts nothing.
    _claimed, _sw_sent, _recoverable = split_write_drain()
    check("split-write file was ACTUALLY CLAIMED (else the checks below are vacuous)",
          bool(_claimed), f"claimed={_claimed!r}")
    check("post-claim re-check refused a completed body directed elsewhere",
          not any("private discord-directed body" in x for x in _sw_sent),
          f"sent={_sw_sent!r}")
    check("split-write claim RELEASED so the owning bridge can recover it",
          _recoverable)

    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}", file=sys.stderr)
        return 1
    print("\nPASS: the filename decision carries through telegram's delivery guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
