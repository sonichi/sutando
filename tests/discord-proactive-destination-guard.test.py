#!/usr/bin/env python3
"""BEHAVIOURAL: a Discord proactive `[channel:]` redirect is guarded at the target's
audience tier through the shared `guard_proactive_for_destination` (#4971).

Drives one real `poll_proactive` pass with a recording provider and channels:
  a) guild channel + an internal error / local path -> only the standard notice
  b) the same body with no redirect                  -> the owner DM, unchanged
  c) a DM whose recipient is owner-tier              -> unchanged
  d) a DM whose recipient is team                    -> guarded
  e) clean prose + an attachment to a guild         -> only the marker notice, no path
  f) a sendable attachment to a guild                -> not uploaded; to an owner-tier DM -> uploaded
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import (  # noqa: E402
    body_claimable_by as _real_body_claimable_by,
    body_target_channel as _real_body_target_channel,
    fallback_claims_name as _real_fallback_claims,
    lane_claims_proactive as _real_lane_claims,
    redirect_target_is_foreign as _real_redirect_target_is_foreign,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


_CFG = tempfile.mkdtemp(prefix="proactive-destguard-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps(
    {"allowFrom": ["4242"], "tierMap": {"4242": "owner", "9001": "owner", "9002": "team"}}))

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


db = _load("dbridge_destguard", REPO / "src" / "discord-bridge.py")

_LIVE_RESULTS = Path(db.RESULTS_DIR)          # capture BEFORE rebinding
_live_before = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None


class _Sentinel(Exception):
    """Breaks the poll loop after exactly one pass."""


GUILD, OWNER_PEER_DM, TEAM_PEER_DM, OWNER_DM = 1530802402603700415, 1530802402603700777, 1530802402603700888, 4242


def _run_one_pass(results: Path, classify=None, coroutine="poll_proactive"):
    """One pass; returns [(channel_id, text)] for every text post and upload."""
    db.RESULTS_DIR = results
    db.ACCESS_FILE = Path(_CFG) / "channels" / "discord" / "access.json"
    db.presenter_mode_active = lambda *_a, **_k: False
    routing = types.ModuleType("proactive_routing")
    routing.redirect_target_is_foreign = _real_redirect_target_is_foreign
    routing.body_target_channel = _real_body_target_channel
    routing.body_claimable_by = _real_body_claimable_by
    routing.lane_claims_proactive = _real_lane_claims
    routing.should_claim_proactive_peeked = lambda *_a, **_k: True
    routing.fallback_claims_name = _real_fallback_claims
    sys.modules["proactive_routing"] = routing
    posts: list = []

    from ag2_sparrow.delivery_core.contract import (
        DeliveryOutcome, DeliveryReceipt, ProviderCapabilities)

    class _Provider:
        capabilities = ProviderCapabilities()

        def deliver(self, item_id, payload, key):
            p = json.loads(payload.decode("utf-8"))
            posts.append((int(p["channel_id"]), p["content"]))
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED, provider_ref="m1")

        def reconcile(self, attempt):
            return None

    db._PROACTIVE_PROVIDER = _Provider()

    class _Channel:
        def __init__(self, cid, recipient=None):
            self.id = cid
            self.name = f"c{cid}"
            if recipient is not None:
                self.recipient = types.SimpleNamespace(id=recipient)

        async def send(self, content=None, file=None, **_kw):
            posts.append((self.id, "<file>" if file is not None else content))

    channels = {GUILD: _Channel(GUILD), OWNER_PEER_DM: _Channel(OWNER_PEER_DM, 9001),
                TEAM_PEER_DM: _Channel(TEAM_PEER_DM, 9002), OWNER_DM: _Channel(OWNER_DM, 4242)}

    class _User:
        bot = False
        name = "owner"

        async def create_dm(self):
            return channels[OWNER_DM]

    class _Client:
        async def fetch_user(self, _uid):
            return _User()

        def get_channel(self, cid):
            return channels.get(cid)

    db.client = _Client()
    if classify is not None:
        db._classify_attachment = classify

    async def _sleep(_secs):
        raise _Sentinel()

    _orig_sleep = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    try:
        asyncio.run(getattr(db, coroutine)())
    except _Sentinel:
        pass
    finally:
        db.asyncio.sleep = _orig_sleep
    return posts


def _one(body, classify=None):
    box = Path(tempfile.mkdtemp(prefix="proactive-destguard-"))
    (box / "proactive-1.txt").write_text(body)
    return _run_one_pass(box, classify)


def _fallback(body, classify=None):
    """One poll_dm_fallback pass over an owner task's aged result naming a redirect."""
    box = Path(tempfile.mkdtemp(prefix="dm-fallback-destguard-"))
    tasks = box / "tasks"
    tasks.mkdir()
    db.TASKS_DIR = tasks
    (tasks / "question-1.txt").write_text("id: question-1\naccess_tier: owner\ntask: probe\n")
    result = box / "question-1.txt"
    result.write_text(body)
    aged = time.time() - 400
    os.utime(result, (aged, aged))
    return _run_one_pass(box, classify, coroutine="poll_dm_fallback")


def main() -> int:
    from policy.egress.result import TEAM_LEAK_RESULT as NOTICE
    from policy.egress.result import TEAM_LEAK_RESULT_MARKER as MARKER_NOTICE
    leak = "copy failed: [Errno 2] No such file: /Users/someone/private/plan.md"

    posts = _one(f"[channel: {GUILD}]\n{leak}\n")
    check("a) a guild channel gets only the standard notice", posts == [(GUILD, NOTICE)], repr(posts))
    posts = _one(f"{leak}\n")
    check("b) the same body to the owner DM is unchanged", posts == [(OWNER_DM, leak)], repr(posts))
    posts = _one(f"[channel: {OWNER_PEER_DM}]\n{leak}\n")
    check("c) a DM with an owner-tier recipient is unchanged", posts == [(OWNER_PEER_DM, leak)], repr(posts))
    posts = _one(f"[channel: {TEAM_PEER_DM}]\n{leak}\n")
    check("d) a DM with a team recipient gets the notice", posts == [(TEAM_PEER_DM, NOTICE)], repr(posts))

    refused = lambda p: (db._ATTACH_REFUSED, p)  # noqa: E731
    posts = _one(f"[channel: {GUILD}]\nweekly summary\n[attach: /Users/someone/private/report.png]\n",
                 classify=refused)
    check("e) a guild gets only the marker notice for a body carrying [file:]",
          posts == [(GUILD, MARKER_NOTICE)], repr(posts))

    sendable = lambda p: (db._ATTACH_SEND, p)  # noqa: E731
    chart = Path(tempfile.mkdtemp(prefix="proactive-destguard-file-")) / "chart.png"
    chart.write_bytes(b"png")
    posts = _one(f"[channel: {GUILD}]\nchart\n[attach: {chart}]\n", classify=sendable)
    check("f) an allowlisted attachment is not uploaded to a guild", posts == [(GUILD, MARKER_NOTICE)],
          repr(posts))
    posts = _one(f"[channel: {OWNER_PEER_DM}]\nchart\n[attach: {chart}]\n", classify=sendable)
    check("f) an allowlisted attachment is still uploaded to an owner-tier DM",
          (OWNER_PEER_DM, "<file>") in posts, repr(posts))

    posts = _fallback(f"[channel: {GUILD}]\n{leak}\n")
    check("g) dm-fallback: a guild channel gets only the standard notice",
          posts == [(GUILD, NOTICE)], repr(posts))
    posts = _fallback(f"[channel: {OWNER_PEER_DM}]\n{leak}\n")
    check("g) dm-fallback: a DM with an owner-tier recipient is unchanged",
          posts == [(OWNER_PEER_DM, leak)], repr(posts))
    posts = _fallback(f"[channel: {GUILD}]\nweekly summary\n[attach: /Users/someone/private/report.png]\n",
                      classify=refused)
    check("g) dm-fallback: a guild gets only the marker notice for a body carrying [file:]",
          posts == [(GUILD, MARKER_NOTICE)], repr(posts))

    src = (REPO / "src" / "discord-bridge.py").read_text()
    check("the bridge delegates to the shared guard and notice policy",
          "guard_proactive_for_destination(" in src and "attachment_notice_name(" in src)
    check("no redirect target's refusal notice interpolates the raw path",
          'target_ch.send(f"(file not allowed: {fpath})")' not in src
          and 'target_channel.send(f"(file not allowed: {fpath})")' not in src)
    tier = db._redirect_destination_tier
    dm = types.SimpleNamespace
    check("a redirect target's tier: owner DM, DM recipient's tierMap tier, else guest",
          tier(dm(id=OWNER_DM), OWNER_DM) == "owner"
          and tier(dm(id=1, recipient=dm(id=9001)), None) == "owner"
          and tier(dm(id=1, recipient=dm(id=9002)), None) == "team"
          and tier(dm(id=1, recipient=dm(id=5)), None) == "guest"
          and tier(dm(id=GUILD), OWNER_DM) == "guest")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PASS: Discord proactive redirects are guarded at the target's tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
