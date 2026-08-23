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

    # Split write (kewei P1): the claim hard-links + unlinks, so a producer
    # holding the ORIGINAL fd keeps writing this inode after the claim.
    split = results / "proactive-3.txt"
    split_fd = open(split, "w")
    split_fd.write("**[core: 1]**\n")
    split_fd.flush()

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

    _orig_claim = mod.claim_for_delivery

    def _claim_then_producer_appends(path, recipient):
        claim = _orig_claim(path, recipient)
        if claim is not None and "proactive-3" in claim.name:
            split_fd.write("[channel: 1535008729106485288]\n"
                           "private discord-directed body\n")
            split_fd.flush()
        return claim

    mod.claim_for_delivery = _claim_then_producer_appends

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
    # The regression: telegram must re-check routing on the body it SENDS, not
    # only the one it peeked before claiming.
    check("split-write body completed as discord-directed is NOT sent on telegram",
          not any("private discord-directed body" in s for s in sent),
          f"sent={sent!r}")
    check("split-write claim is RELEASED so the real bridge can recover it",
          split.exists() or bool(list(results.glob("proactive-3*.txt"))))

    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}", file=sys.stderr)
        return 1
    print("\nPASS: the filename decision carries through telegram's delivery guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
