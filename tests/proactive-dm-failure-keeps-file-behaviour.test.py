#!/usr/bin/env python3
"""BEHAVIOURAL: a proactive DM whose send raises must leave the body recoverable.

This drives one real iteration of `poll_proactive` with `dm.send` raising the
error Discord actually returned on this host, and asserts the file is still on
disk afterwards.

WHY IT EXISTS. The sibling structural test asserted only that the `unlink` is
unreachable from the failure path, and its docstring claimed a behavioural test
was infeasible because importing `src/discord-bridge.py` pulls discord.py and
resolves the operator's config dir. **That claim was wrong** — several tests
already exec this module (`bridge-audit-wiring`, `bridge-not-allowlisted-ack`,
`bridge-timeout-guards`, …) using the stub-and-redirect pattern copied below. A
stated limitation nobody re-checks becomes a permanent excuse, so this is the
re-check.

HERMETIC: `CLAUDE_CONFIG_DIR` and every workspace-derived constant on the module
are rebound to tmpdirs BEFORE the coroutine runs. `discord` is stubbed, so no
network. The last case asserts the operator's real results/ was untouched rather
than trusting the redirect.

The loop is `while True: ... ; await asyncio.sleep(3)` with the sleep OUTSIDE the
per-file try, so patching `asyncio.sleep` to raise is a clean single-iteration
driver — it cannot be swallowed by the handler under test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Imported before the stub replaces sys.modules: it hands back the real gate.
from proactive_routing import redirect_target_is_foreign as _real_redirect_target_is_foreign  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


_CFG = tempfile.mkdtemp(prefix="proactive-undeliv-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": ["4242"]}')

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                      "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    stub.Message = type("Message", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = stub


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


db = _load("dbridge_undeliv", REPO / "src" / "discord-bridge.py")

_LIVE_RESULTS = Path(db.RESULTS_DIR)          # capture BEFORE rebinding
_live_before = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None


class _Sentinel(Exception):
    """Breaks the poll loop after exactly one pass."""


class _Boom(Exception):
    """The error Discord actually returned: 413 Payload Too Large (40005)."""


def _run_one_pass(results: Path, send):
    db.RESULTS_DIR = results
    db.ACCESS_FILE = Path(_CFG) / "channels" / "discord" / "access.json"
    db.presenter_mode_active = lambda *_a, **_k: False

    routing = types.ModuleType("proactive_routing")
    routing.should_claim_proactive = lambda *_a, **_k: True
    routing.should_claim_proactive_file = lambda *_a, **_k: True
    routing.proactive_destination = lambda *_a, **_k: None
    # Stubbed routing claims every file; redirect_target_is_foreign stays REAL.
    routing.redirect_target_is_foreign = _real_redirect_target_is_foreign
    sys.modules["proactive_routing"] = routing

    class _DM:
        id = 4242

        async def send(self, *a, **kw):
            return send(*a, **kw)

    # 5b: text rides the provider seam, not dm.send; `send` stays the
    # switch — a raise becomes NOT_DELIVERED (parks), a return confirms.
    from ag2_sparrow.delivery_core.contract import (
        DeliveryOutcome, DeliveryReceipt, ProviderCapabilities)

    class _Provider:
        capabilities = ProviderCapabilities()

        def deliver(self, item_id, payload, key):
            body = json.loads(payload.decode("utf-8"))["content"]
            try:
                send(body)
            except Exception as e:  # noqa: BLE001 — the injected failure
                return DeliveryReceipt(outcome=DeliveryOutcome.NOT_DELIVERED,
                                       detail=str(e))
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                                   provider_ref="m1")

        def reconcile(self, attempt):
            return None

    db._PROACTIVE_PROVIDER = _Provider()

    class _User:
        bot = False
        name = "owner"

        async def create_dm(self):
            return _DM()

    class _Client:
        async def fetch_user(self, _uid):
            return _User()

    db.client = _Client()

    async def _sleep(_secs):
        raise _Sentinel()

    _orig_sleep = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(db.poll_proactive())
    except _Sentinel:
        pass
    finally:
        db.asyncio.sleep = _orig_sleep


def main() -> int:
    print("proactive DM failure keeps the file (behavioural):")

    # --- the failure path: send raises, body must survive ------------------
    box = Path(tempfile.mkdtemp(prefix="proactive-fail-"))
    msg = box / "proactive-pending-q-boom.txt"
    BODY = "⚠️ a body Discord refuses to accept"
    msg.write_text(BODY)

    def _raise(*_a, **_k):
        raise _Boom("413 Payload Too Large (error code: 40005): Request entity too large")

    _run_one_pass(box, _raise)

    kept = list(box.rglob("proactive-pending-q-boom.txt"))
    check("the message still exists after a failed send", bool(kept),
          "file was DELETED — the body is unrecoverable")
    if kept:
        check("  ...quarantined under undelivered/, not left to re-poll forever",
              kept[0].parent.name == "undelivered", f"found at {kept[0]}")
        check("  ...body preserved byte-for-byte", kept[0].read_text() == BODY,
              "content changed in transit")

    # --- the success path still cleans up ----------------------------------
    box2 = Path(tempfile.mkdtemp(prefix="proactive-ok-"))
    (box2 / "proactive-pending-q-fine.txt").write_text("a body that sends fine")
    sent: list = []

    def _ok(*a, **_k):
        sent.append(a)

    _run_one_pass(box2, _ok)
    # Glob EVERYTHING, not `*.txt`. The claim renames to `.sending` before the
    # send, so a `*.txt` glob cannot tell "deleted" from "claimed and left" —
    # it reported success against a mutation that removed the unlink entirely.
    # (@john-the-dev's over-broad mutation on #2628 is what sent me looking.)
    _left = [q.name for q in box2.rglob("proactive-*") if q.is_file()]
    check("a SUCCESSFUL send still removes the file", not _left,
          f"success path stopped cleaning up — left {_left}; every message would re-send forever")
    check("  ...and it really was sent", bool(sent), "no send recorded")

    # --- LAST RESORT: even the quarantine fails ----------------------------
    # The whole guarantee is "noise is recoverable, deletion is not", and this
    # is the branch that has to hold it up. `undelivered` is created as a FILE,
    # so `mkdir(exist_ok=True)` raises FileExistsError — no monkeypatching, and
    # a plausible real state (a stray file, a name collision).
    # CI found these two lines uncovered (diff coverage 77.8%, missing 5034 and
    # 5038): the successful-quarantine case does not reach the handler.
    box3 = Path(tempfile.mkdtemp(prefix="proactive-noquar-"))
    (box3 / "proactive-pending-q-stuck.txt").write_text("must not vanish")
    (box3 / "undelivered").write_text("not a directory")

    _run_one_pass(box3, _raise)

    survivors = [p for p in box3.iterdir()
                 if p.is_file() and p.name.startswith("proactive-pending-q-stuck")]
    check("quarantine ALSO fails -> the file is still left in place",
          bool(survivors), "deleted despite the whole point being not to delete")
    if survivors:
        check("  ...body intact even on the last-resort path",
              survivors[0].read_text() == "must not vanish", "content lost")

    # --- hermeticity, asserted rather than assumed -------------------------
    live_after = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None
    check("HERMETIC: operator's real results/ untouched", live_after == _live_before,
          f"{_live_before} -> {live_after}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All behavioural proactive-failure checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
