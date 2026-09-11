#!/usr/bin/env python3
"""BEHAVIOURAL: `poll_approved` must find the marker AND never destroy it.

TWO DEFECTS, one function (sonichi#2629, reported by @Sutando-Mini).

1. The producer and the consumer disagreed on the path. The official plugin's
   `access` skill hardcodes the vanilla home — its SKILL.md step 7 is
   `mkdir -p ~/.claude/channels/discord/approved` — while the bridge resolved
   `ACCESS_FILE.parent / "approved"`, i.e. through `$CLAUDE_CONFIG_DIR`. They
   coincide on a default install and diverge on every Sutando install that
   relocates the config dir, so the confirmation was never sent. The user IS
   granted access; they are simply never told.

2. `f.unlink(missing_ok=True)` sat OUTSIDE the try guarding the send, so a
   failed send destroyed the marker. Worse here than in `poll_proactive`: there
   the file carries a message, here the file IS the obligation.

WHY THE ASSERTIONS ARE ABOUT THE FILE AND THE SEND, NOT THE LOG. A bridge that
logs "sent" while the marker is gone and nothing arrived is the failure being
fixed. Each case pins the observable: did a send happen, to which channel, and
where is the file afterwards.

HERMETIC: `CLAUDE_CONFIG_DIR` and `SOURCE_CLAUDE_CONFIG_DIR` are rebound to
tmpdirs BEFORE import, and the module's own `ACCESS_FILE` after it. The last
case asserts the operator's two REAL approved dirs are untouched rather than
trusting the redirect — this test exists because those two paths differ, so
assuming either one is redirected is exactly the mistake under test.

The loop is `while True: ... ; await asyncio.sleep(3)` with the sleep OUTSIDE
the per-directory work, so patching `asyncio.sleep` to raise is a clean
single-iteration driver that the handler under test cannot swallow.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


# Capture the operator's REAL locations BEFORE overriding anything. The first
# version of this file read them back through the module AFTER the overrides
# were in place, so `_LIVE[1]` was the tmpdir, and the hermeticity assertion
# compared the fixture against itself. It failed loudly (None -> []) rather than
# passing vacuously, which is the only reason it was caught — a redirect that
# also redirects the check is not a check.
_REAL_CCD = os.environ.get("CLAUDE_CONFIG_DIR")
_REAL_SRC = os.environ.get("SOURCE_CLAUDE_CONFIG_DIR")

_CFG = tempfile.mkdtemp(prefix="approved-poll-ccd-")
_SRC = tempfile.mkdtemp(prefix="approved-poll-vanilla-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["SOURCE_CLAUDE_CONFIG_DIR"] = _SRC
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))

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


db = _load("dbridge_approved", REPO / "src" / "discord-bridge.py")

# Captured BEFORE any rebinding, and BOTH of them: the whole point of #2629 is
# that these two are different paths on this host.
_rel = Path("channels") / "discord" / "approved"
_LIVE = [Path(os.path.expanduser(_REAL_CCD)) / _rel if _REAL_CCD else None,
         Path(os.path.expanduser(_REAL_SRC or "~/.claude")) / _rel]
_LIVE = [p for p in _LIVE if p is not None]
_live_before = [sorted(p.iterdir()) if p.exists() else None for p in _LIVE]


class _Sentinel(Exception):
    """Breaks the poll loop after exactly one pass."""


def _run_one_pass(canonical: Path, sends: list, fail: bool = False,
                  log: "list | None" = None, corrupt: bool = False):
    """Drive one full delivery pass — poll_approved then poll_pending_notify.

    `log` collects what the loop PRINTED. Needed for one property that has no
    filesystem trace: see the `is_file()` control below."""
    db.ACCESS_FILE = canonical.parent / "access.json"
    if corrupt:
        # mutate_access_file refuses to write over a corrupt file and returns
        # None SILENTLY — the adopt-failure lever, no monkeypatching.
        db.ACCESS_FILE.write_text("{not valid json")

    class _Chan:
        def __init__(self, cid): self.cid = cid

        async def send(self, text):
            if fail:
                raise RuntimeError("404 Not Found (error code: 10003): Unknown Channel")
            sends.append((self.cid, text))

    class _Client:
        async def fetch_channel(self, cid):
            return _Chan(cid)

    db.client = _Client()

    async def _sleep(_secs):
        raise _Sentinel()

    _orig = db.asyncio.sleep
    db.asyncio.sleep = _sleep
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_buf):
            # Delivery is two loops now: poll_approved ADOPTS a marker into
            # pendingNotify, poll_pending_notify is the sole sender (#3318).
            for _loop in (db.poll_approved, db.poll_pending_notify):
                try:
                    asyncio.run(_loop())
                except _Sentinel:
                    pass
    except _Sentinel:
        pass
    finally:
        db.asyncio.sleep = _orig
        if log is not None:
            log.append(_buf.getvalue())
        print(_buf.getvalue(), end="")


def _dirs(tag: str):
    """A fresh canonical dir + the legacy dir the plugin actually writes."""
    box = Path(tempfile.mkdtemp(prefix=f"approved-{tag}-"))
    canonical = box / "channels" / "discord" / "approved"
    canonical.mkdir(parents=True)
    legacy = Path(_SRC) / "channels" / "discord" / "approved"
    legacy.mkdir(parents=True, exist_ok=True)
    for stale in legacy.iterdir():
        if stale.is_file():
            stale.unlink()
    return canonical, legacy


def main() -> int:
    print("approved-poll: finds the marker, and never destroys it:")

    # --- THE BUG: the plugin writes to the vanilla home ---------------------
    canonical, legacy = _dirs("legacy")
    (legacy / "9001").write_text("555000111")
    sends: list = []
    _run_one_pass(canonical, sends)
    check("a marker in the LEGACY plugin path is delivered",
          any(c == 555000111 for c, _ in sends),
          f"no send for the legacy marker — sends={sends}")
    check("  ...and the marker is consumed afterwards",
          not (legacy / "9001").exists(), "left behind, would re-send every 3s")

    # --- POSITIVE CONTROL: the canonical path must STILL work --------------
    # Without this, a fix that read ONLY the legacy dir passes every assertion
    # above while breaking the default install, where the two coincide.
    canonical, legacy = _dirs("canonical")
    (canonical / "9002").write_text("555000222")
    sends = []
    _run_one_pass(canonical, sends)
    check("a marker in the CANONICAL path is still delivered",
          any(c == 555000222 for c, _ in sends), f"sends={sends}")
    check("  ...and is consumed", not (canonical / "9002").exists(), "left behind")

    # --- ORDERING: canonical is read FIRST ---------------------------------
    # A stale marker in the vanilla home must not shadow a fresh canonical one.
    canonical, legacy = _dirs("order")
    (canonical / "9003").write_text("555000333")
    (legacy / "9003").write_text("999999999")
    sends = []
    _run_one_pass(canonical, sends)
    check("canonical is read BEFORE legacy",
          bool(sends) and sends[0][0] == 555000333,
          f"first send was {sends[0] if sends else None} — a stale legacy "
          f"marker shadowed the canonical one")
    # Found by this case, not designed for: reading both dirs sent "You're in!"
    # TWICE for one sender, the second time to the stale legacy chat_id. The
    # marker's NAME is the sender id, so the same id in both dirs is one
    # obligation recorded twice.
    check("  ...and the shadowed duplicate is NOT sent again",
          len(sends) == 1, f"{len(sends)} sends for one sender: {sends}")
    check("  ...but IS consumed, so it cannot resurface next pass",
          not (legacy / "9003").exists(),
          "the stale legacy marker survives and re-sends every 3s")

    # --- A FAILED SEND MUST NOT DESTROY THE OBLIGATION ---------------------
    # Relocated with the send (#3318): the obligation is now a RECORD in
    # access.json, not a marker file — same guarantee, different owner.
    canonical, legacy = _dirs("fail")
    (canonical / "9004").write_text("555000444")
    sends = []
    _run_one_pass(canonical, sends, fail=True)
    doc = json.loads((canonical.parent / "access.json").read_text())
    owed = dict(doc.get("pendingNotify", {}))
    owed.update(doc.get("notifyFailed", {}))
    check("a failed send LEAVES the obligation recorded", "9004" in owed,
          f"obligation lost — pendingNotify={doc.get('pendingNotify')} "
          f"notifyFailed={doc.get('notifyFailed')}")
    check("  ...with the chat_id preserved", owed.get("9004") == "555000444",
          f"chat_id changed: {owed.get('9004')!r}")
    check("  ...and no send was recorded", not sends, f"sends={sends}")
    check("  ...and the marker was consumed, not re-polled every 3s",
          not (canonical / "9004").exists(),
          "the marker survives adoption and would be adopted again forever")

    # --- undelivered/ IS STILL NEVER TREATED AS A MARKER -------------------
    # Only the LOG shows this breaking, and only once the dir already exists.
    canonical, legacy = _dirs("quardir")
    (canonical / "undelivered").mkdir()
    (canonical / "9009").write_text("555000999")
    sends, log = [], []
    _run_one_pass(canonical, sends, log=log)
    # Match the token the FAILURE path prints. One only the success path emits
    # is absent either way, which is a control that cannot fail.
    check("undelivered/ is never TREATED as a marker",
          "approval to undelivered" not in "".join(log)
          and "for undelivered" not in "".join(log),
          "the quarantine dir entered the poll: " +
          next((ln for ln in "".join(log).splitlines() if "undelivered" in ln), "?"))
    check("  ...and the real marker beside it still delivers",
          any(c == 555000999 for c, _ in sends), f"sends={sends}")

    # --- one bad entry must not abort the rest of the scan -----------------
    # The read used to sit in the OUTER try, so an unreadable entry ended the
    # whole directory scan and the loop just slept.
    canonical, legacy = _dirs("mixed")
    (canonical / "9005").mkdir()                      # a directory, not a marker
    (canonical / "9006").write_text("555000666")
    sends = []
    _run_one_pass(canonical, sends)
    check("a non-file entry does not abort the scan",
          any(c == 555000666 for c, _ in sends),
          f"the good marker after it was never processed — sends={sends}")

    # --- CI diff-coverage gaps, both named by @bassilkhilo-ag2 on #2630 ----
    # They pulled the coverage-summary artifact rather than assuming, and the
    # two uncovered regions are both real branches of THIS change, not incidental
    # lines: the not-exists skip is what makes the legacy fallback safe on a host
    # that has never run stock Claude, and the quarantine-failure handler is the
    # last thing standing between a failed send and a lost obligation.

    # (a) A candidate directory that does not exist is SKIPPED, not an error.
    #     The legacy dir is absent on any host where stock Claude never ran, so
    #     this is the common case there, not an edge one.
    canonical, legacy = _dirs("nolegacy")
    import shutil
    shutil.rmtree(legacy)
    assert not legacy.exists()
    (canonical / "9007").write_text("555000777")
    sends = []
    _run_one_pass(canonical, sends)
    check("an absent candidate dir is skipped, and the present one still delivers",
          any(c == 555000777 for c, _ in sends), f"sends={sends}")

    # (b) Adopt fails AND quarantine fails: a corrupt access.json makes
    #     mutate_access_file a silent no-op, and `undelivered` is a FILE.
    canonical, legacy = _dirs("noquar")
    (canonical / "9008").write_text("555000888")
    (canonical / "undelivered").write_text("not a directory")
    sends, log = [], []
    _run_one_pass(canonical, sends, log=log, corrupt=True)
    check("adopt fails + quarantine fails -> the marker is still left in place",
          (canonical / "9008").is_file(),
          "deleted despite the whole point being not to delete")
    check("  ...body intact on the last-resort path",
          (canonical / "9008").is_file()
          and (canonical / "9008").read_text() == "555000888", "content lost")
    check("  ...and it says it did not lose the file",
          "leaving it in place rather than losing it" in "".join(log),
          "the log does not distinguish kept-vs-dropped")
    check("  ...and nothing was sent for it", not sends, f"sends={sends}")

    # --- hermeticity, asserted rather than assumed -------------------------
    live_after = [sorted(p.iterdir()) if p.exists() else None for p in _LIVE]
    check("HERMETIC: operator's BOTH real approved dirs untouched",
          live_after == _live_before, f"{_live_before} -> {live_after}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All approved-poll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
