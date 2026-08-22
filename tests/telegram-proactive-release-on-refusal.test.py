#!/usr/bin/env python3
"""Telegram: a send that is REFUSED without raising must release, not consume.

`send_reply()` reports refusal by returning ``ok: False``; it does not raise. A
watcher gating cleanup on "did not raise" therefore consumes the claim on the
ordinary failure and the message is unrecoverable.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate the channel root BEFORE exec_module: the bridge resolves ACCESS_FILE
# at module level, and unset it falls back to the operator's real ~/.claude.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-refusal-")
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


sys.path.insert(0, str(REPO / "src"))
# Imported before the stub replaces sys.modules: it hands back the real gate.
from proactive_routing import body_claimable_by as _real_body_claimable_by  # noqa: E402


class _Stop(Exception):
    """Breaks main()'s poll loop after the drain has run."""


def _load(workspace: Path):
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_refusal_under_test", REPO / "src" / "telegram-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_one_drain(mod, results: Path, send_result: dict):
    """Drive main() until the proactive drain has processed one file."""
    mod.RESULTS_DIR = results
    mod.ACCESS_FILE = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram" / "access.json"
    seen = {"n": 0}

    def _send_reply(_chat, _text, task_id=None, message_thread_id=None):
        seen["n"] += 1
        return dict(send_result)

    def _api(method, **_kw):
        # No updates ever; raise once the drain has had a turn so main() exits.
        if seen["n"] > 0:
            raise _Stop()
        return {"ok": True, "result": []}

    mod.send_reply = _send_reply
    mod.api = _api
    mod.load_allowed = lambda: {"4242"}
    # main() imports should_claim_proactive INSIDE the loop, so the stub has to
    # live in sys.modules; rebinding it on `mod` would never be consulted.
    routing = types.ModuleType("proactive_routing")
    routing.should_claim_proactive = lambda *_a, **_k: True
    routing.should_claim_proactive_file = lambda *_a, **_k: True
    routing.proactive_destination = lambda *_a, **_k: None
    # Stubbed routing claims every file; body_claimable_by stays REAL.
    routing.body_claimable_by = _real_body_claimable_by
    sys.modules["proactive_routing"] = routing
    mod.presenter_mode_active = lambda *_a, **_k: False
    t = threading.Thread(target=lambda: _swallow(mod.main), daemon=True)
    t.start()
    deadline = time.time() + 6
    while time.time() < deadline and seen["n"] == 0:
        time.sleep(0.1)
    time.sleep(0.6)
    return seen


def _swallow(fn):
    try:
        fn()
    except BaseException:
        pass


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="tg-refusal-ws-"))
    results = ws / "results"
    results.mkdir(parents=True)
    (ws / "tasks").mkdir(exist_ok=True)
    mod = _load(ws)

    check("harness is sandboxed", str(results).startswith(tempfile.gettempdir())
          or "/var/folders/" in str(results), f"results={results}")

    # --- REFUSAL: ok=False, no exception ---
    name = "proactive-tg-refusal.txt"
    body = "a proactive message telegram refuses"
    (results / name).write_text(body)
    seen = _run_one_drain(mod, results, {"ok": False, "text_chunks": 1, "files_sent": 0})

    check("the refused send was attempted", seen["n"] > 0, "drain never called send_reply")
    txt = results / name
    sending = results / name.replace(".txt", ".sending")
    survived = txt.exists() or sending.exists()
    check("a refused proactive DM is NOT consumed", survived,
          "both the claim and the released file are gone — message lost")
    if survived:
        present = txt if txt.exists() else sending
        check("its body is intact", present.read_text() == body)
    check("it returns to the .txt stream pollers scan", txt.exists(),
          "left stranded as .sending")

    # --- POSITIVE CONTROL: ok=True must still consume, or the gate is unconditional ---
    ws2 = Path(tempfile.mkdtemp(prefix="tg-refusal-ws2-"))
    results2 = ws2 / "results"
    results2.mkdir(parents=True)
    (ws2 / "tasks").mkdir(exist_ok=True)
    mod2 = _load(ws2)
    name2 = "proactive-tg-delivered.txt"
    (results2 / name2).write_text("a proactive message telegram accepts")
    seen2 = _run_one_drain(mod2, results2, {"ok": True, "text_chunks": 1, "files_sent": 0})
    check("the accepted send was attempted", seen2["n"] > 0)
    check("a DELIVERED proactive DM IS consumed (release is not unconditional)",
          not (results2 / name2).exists()
          and not (results2 / name2.replace(".txt", ".sending")).exists(),
          "delivered file left behind — it would send again")

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: telegram releases a refused proactive DM and consumes a delivered one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
