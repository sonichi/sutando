#!/usr/bin/env python3
"""A consumer must not retire a claimed inode a producer is still writing.

The claim is a hard link, so a producer holding the original fd keeps appending
to THIS inode after the bridge's post-claim `read_ready_result`. The bridge then
sent the snapshot and unlinked, destroying every byte that arrived in between —
never guarded, never delivered, unrecoverable.

The append is injected at the SDK boundary, which the shipped code calls after
the read and before the retire, so it lands in the real window with no threads
and no sleeps deciding the outcome.

Drives the real `result_watcher()`; only the Slack SDK client is stubbed.
Case 2 is the mutation control: it re-runs the identical scenario with the fence
neutralised and requires the loss to reappear, so a green case 1 cannot be an
artifact of the harness.

Run: python3 tests/proactive-claim-retire-fence.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# MODULE level, before any exec_module: the bridge resolves ACCESS_FILE during
# import and falls back to the real ~/.claude when CLAUDE_CONFIG_DIR is unset.
_CCD = tempfile.mkdtemp(prefix="sutando-retire-fence-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CCD
_cfg_slack = Path(_CCD) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')
# The wiring checks name every bridge; each channel's allowlist is seeded so
# no import can fall back to the developer's real config.
_cfg_telegram = Path(_CCD) / "channels" / "telegram"
_cfg_telegram.mkdir(parents=True, exist_ok=True)
(_cfg_telegram / "access.json").write_text('{"allowFrom": []}')
_cfg_discord = Path(_CCD) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": []}')

OWNER_DM = "DOWNER1"
FIRST = "first half of the answer"
APPENDED = "SECOND-HALF-ARRIVED-LATE"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


class _RecordingClient:
    """Stubs ONLY the SDK boundary. Everything above it is the shipped code."""

    def __init__(self):
        self.posted: list[dict] = []
        self.append_target: Path | None = None
        self._appended = False

    def conversations_open(self, **kwargs):
        return {"channel": {"id": OWNER_DM}}

    def chat_postMessage(self, **kwargs):
        # The shipped code reaches here AFTER read_ready_result and BEFORE the
        # retire — exactly the window a still-writing producer appends in.
        if (not self._appended and self.append_target is not None
                and self.append_target.exists()):
            with open(self.append_target, "a") as fh:
                fh.write(f"\n{APPENDED}\n")
            self._appended = True
        self.posted.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _RecordingClient()

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


def _load_bridge(tag: str):
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket.SocketModeHandler = type("SocketModeHandler", (), {})
    sys.modules["slack_bolt.adapter.socket_mode"] = socket

    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix=f"sutando-retire-{tag}-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
    spec = importlib.util.spec_from_file_location(
        f"slackbridge_retire_fence_{tag}", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    access = Path(os.environ["SUTANDO_WORKSPACE"]) / "access.json"
    access.write_text(json.dumps({"allowFrom": ["owner-1"],
                                  "tierMap": {"owner-1": "owner"},
                                  "tofuOwner": "owner-1"}))
    mod.ACCESS_FILE = access
    return mod


def _settle(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _run_scenario(tag: str, neutralise_fence: bool):
    """Deliver one proactive file whose producer appends mid-send."""
    bridge = _load_bridge(tag)
    if neutralise_fence:
        # The mutation: retire unconditionally, which is the pre-fix behaviour.
        def _always_retire(claim, delivered):
            Path(claim).unlink(missing_ok=True)
            return True
        bridge.retire_claim_if_unchanged = _always_retire

    client = bridge.app.client
    results = Path(bridge.RESULTS_DIR)
    results.mkdir(parents=True, exist_ok=True)

    name = "proactive-late-append"
    client.append_target = results / f"{name}.sending"

    threading.Thread(target=bridge.result_watcher, daemon=True).start()
    (results / f"{name}.txt").write_text(FIRST)

    delivered_late = _settle(
        lambda: any(APPENDED in str(p.get("text", "")) for p in client.posted))
    settled_gone = _settle(
        lambda: not (results / f"{name}.txt").exists()
        and not (results / f"{name}.sending").exists(), timeout=2.0)
    return client, delivered_late, settled_gone


def main() -> int:
    print("case 1: with the fence, a mid-send append still reaches the owner")
    client, delivered_late, _ = _run_scenario("fenced", neutralise_fence=False)
    check("the first half was delivered",
          any(FIRST in str(p.get("text", "")) for p in client.posted),
          f"posted= {[str(p.get('text'))[:50] for p in client.posted]}")
    check("the late-appended half was NOT destroyed by the retire",
          delivered_late,
          f"posted= {[str(p.get('text'))[:60] for p in client.posted]}")

    print("case 2 (mutation control): fence disabled, the loss must reappear")
    m_client, m_delivered_late, m_gone = _run_scenario("mutated", neutralise_fence=True)
    check("control still delivers the first half (scenario really ran)",
          any(FIRST in str(p.get("text", "")) for p in m_client.posted),
          f"posted= {[str(p.get('text'))[:50] for p in m_client.posted]}")
    check("control retires the inode (the destructive step happened)",
          m_gone, "claim/result file still on disk, so nothing was destroyed")
    check("control LOSES the late-appended half (test detects the defect)",
          not m_delivered_late,
          "appended text was delivered even with the fence disabled — "
          "this test would pass without the fix and proves nothing")

    _retire_rows()

    _guard_refusal_row()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all pass")
    return 0


def _retire_rows() -> None:
    """The helper's OWN failure modes, driven directly.

    The end-to-end case above injects its append at the SDK boundary, which is
    BEFORE the helper's final read — so it can only ever exercise "already
    grown". These three rows are internal to `retire_claim_if_unchanged` and are
    unreachable from that fixture (keweichen, #3305).
    """
    print("retire_claim_if_unchanged rows (retired / claim remains):")
    spec = importlib.util.spec_from_file_location(
        "_rd", REPO / "src" / "delivery" / "readiness.py")
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    d = Path(tempfile.mkdtemp(prefix="retire-rows-"))

    c1 = d / "unchanged.txt"; c1.write_text("body\n")
    check("unchanged retires", rd.retire_claim_if_unchanged(c1, "body") is True
          and not c1.exists())

    c2 = d / "grown.txt"; c2.write_text("body\n")
    with open(c2, "a") as f:
        f.write("MORE\n")
    check("grown is kept", rd.retire_claim_if_unchanged(c2, "body") is False
          and c2.exists())

    # A multi-byte character cut mid-sequence, exactly as a partial write leaves
    # it. Bytes EXIST and are undelivered, so unlinking destroys them.
    c3 = d / "partial.txt"
    c3.write_bytes("body\n".encode() + b"\xe6\x97")
    check("partial UTF-8 is kept, not destroyed",
          rd.retire_claim_if_unchanged(c3, "body") is False and c3.exists(),
          "a mid-character partial write must not be read as 'nothing to retire'")

    # An append landing AFTER the helper's own final read, inside its window.
    c4 = d / "late.txt"; c4.write_text("body\n")
    real_read = Path.read_bytes
    fired = {"done": False}

    def _hooked(self, *a, **k):
        out = real_read(self, *a, **k)
        if str(self) == str(c4) and not fired["done"]:
            fired["done"] = True
            with open(c4, "a") as f:
                f.write("LATE\n")
        return out

    Path.read_bytes = _hooked
    try:
        kept = rd.retire_claim_if_unchanged(c4, "body") is False
    finally:
        Path.read_bytes = real_read
    check("append inside the read-to-unlink window is kept",
          kept and c4.exists(),
          "the late bytes were destroyed")

    # keweichen (#3305): the size re-check only NARROWED the window. An append
    # between the size check and the retire step must be kept AND recoverable.
    c9 = d / "after-stat.txt"; c9.write_text("body\n")
    real_stat = Path.stat
    fired9 = {"done": False}

    def _stat_hook(self, *a, **k):
        out = real_stat(self, *a, **k)
        if str(self) == str(c9) and not fired9["done"]:
            fired9["done"] = True
            with open(c9, "a") as f:
                f.write("LATE\n")
        return out

    Path.stat = _stat_hook
    try:
        kept9 = rd.retire_claim_if_unchanged(c9, "body") is False
    finally:
        Path.stat = real_stat
    check("append AFTER the size check is kept and the claim released",
          kept9 and c9.exists() and "LATE" in c9.read_text(),
          "the late bytes were destroyed or the claim was retired")

    # Retirement never unlinks: the moved inode keeps whatever a stale fd
    # appends after the re-verify, so the worst case is a recoverable file.
    c10 = d / "after-verify.txt"; c10.write_text("body\n")
    real_replace = Path.replace
    appended = {"done": False}

    def _replace_hook(self, target, *a, **k):
        out = real_replace(self, target, *a, **k)
        return out
    fd10 = open(c10, "a")
    try:
        assert rd.retire_claim_if_unchanged(c10, "body") is True
        fd10.write("LATE\n"); fd10.flush()
    finally:
        fd10.close()
        Path.replace = real_replace
    retired10 = rd._retired_path(c10)
    check("retired claim path is gone (consumer semantics unchanged)", not c10.exists())
    check("bytes appended after retirement are preserved, not destroyed",
          retired10.exists() and "LATE" in retired10.read_text(),
          f"retired inode missing or lost the late bytes: {retired10}")
    # Preservation is not recovery: the sweep republishes the bytes past the
    # delivered length as a new proactive file and drops the inode once quiescent.
    late_pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("a late append is republished as its own proactive file",
          len(late_pub) == 1 and late_pub[0].parent == d
          and late_pub[0].name.startswith("proactive-late-")
          and late_pub[0].read_text().strip() == "LATE",
          f"published={late_pub} bodies={[x.read_text() for x in late_pub]}")
    check("the republished remainder carries no already-delivered bytes",
          late_pub and "body" not in late_pub[0].read_text())
    check("the inode stays until quiescent (a second append is still possible)",
          retired10.exists())
    check("a quiescent inode with nothing new is dropped",
          rd.sweep_retired(d, quiesce_s=0, now=time.time() + 1) == []
          and not retired10.exists() and not rd._delivered_marker(retired10).exists())
    check("a sweep republishes nothing twice",
          not list(d.glob("proactive-late-*.txt")) or len(list(d.glob("proactive-late-*.txt"))) == 1)
    # A retired inode with no delivered-length marker predates this lifecycle:
    # its prefix may already have been sent, so it is aged out, never resent.
    legacy = d / "retired" / "legacy.txt"; legacy.write_text("body\nMAYBE-SENT\n")
    check("an unmarked retired inode is never republished",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and legacy.exists())
    check("an unmarked retired inode ages out once quiescent",
          rd.sweep_retired(d, quiesce_s=0, now=time.time() + 1) == [] and not legacy.exists())

    # Failure branches: an unwritable marker never fails the retirement, an
    # unreadable or unpublishable inode is skipped, a raising sweep is contained.
    import errno as _errno
    c11 = d / "marker-unwritable.txt"; c11.write_text("body\n")
    real_write_text = Path.write_text
    def _wt_hook(self, *a, **k):
        if self.name.endswith(".delivered"):
            raise OSError(_errno.EACCES, "marker unwritable")
        return real_write_text(self, *a, **k)
    Path.write_text = _wt_hook
    real_atomic = rd._write_atomic
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError(_errno.EACCES, "marker unwritable"))
    try:
        # No marker -> late bytes indistinguishable -> retirement undone.
        check("an unwritable delivered-marker undoes the retirement and keeps the claim",
              rd.retire_claim_if_unchanged(c11, "body") is False
              and c11.exists() and not rd._retired_path(c11).exists())
    finally:
        Path.write_text = real_write_text
        rd._write_atomic = real_atomic
    c11.unlink()

    c12 = d / "unreadable.txt"; c12.write_text("body\n")
    assert rd.retire_claim_if_unchanged(c12, "body") is True
    r12 = rd._retired_path(c12)
    with open(r12, "a") as fh: fh.write("LATE\n")
    real_read_bytes = Path.read_bytes
    def _rb_hook(self, *a, **k):
        if self == r12:
            raise OSError(_errno.EIO, "unreadable inode")
        return real_read_bytes(self, *a, **k)
    Path.read_bytes = _rb_hook
    try:
        check("an unreadable retired inode is skipped, not raised, and kept",
              rd.sweep_retired(d, quiesce_s=0, now=time.time() + 1) == [] and r12.exists())
    finally:
        Path.read_bytes = real_read_bytes
    real_mkstemp = rd.tempfile.mkstemp
    rd.tempfile.mkstemp = lambda *a, **k: (_ for _ in ()).throw(OSError(_errno.ENOSPC, "no space"))
    try:
        check("a failed republish keeps the inode and marker for the next pass",
              rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and r12.exists()
              and rd._delivered_marker(r12).read_text() == str(len(b"body\n")))
    finally:
        rd.tempfile.mkstemp = real_mkstemp
    # An unmarkable remainder is not published (it would republish every pass);
    # it is published exactly once when the marker is writable again.
    real_marker_write = rd._write_atomic
    late_before = set(d.glob("proactive-late-*"))
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError(_errno.EACCES, "marker unwritable"))
    try:
        pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
        check("no republish lands while the marker cannot advance",
              pub == [] and r12.exists()
              and rd._delivered_marker(r12).read_text() == str(len(b"body\n"))
              and set(d.glob("proactive-late-*")) == late_before)
    finally:
        rd._write_atomic = real_marker_write
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("the remainder is published once the marker is writable, exactly once",
          len(pub) == 1 and pub[0].read_text().strip() == "LATE"
          and rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [])

    # Wiring: every proactive poller runs the sweep each pass, so "eventual"
    # is bounded by one poll interval, not by a tool nobody runs.
    for bridge in ("slack-bridge.py", "telegram-bridge.py", "discord-bridge.py"):
        src = (REPO / "src" / bridge).read_text()
        check(f"{bridge} sweeps retired inodes every proactive pass",
              "sweep_retired(RESULTS_DIR)" in src and src.count("_sweep_retired_pass()") >= 1,
              "the sweep helper is not called from the poll loop")
    # The helper is the same shape in each bridge; drive the loaded Slack
    # module's copy with a raising sweep — the poll loop must not die.
    _b = _load_bridge("sweepref")
    _saved = _b.sweep_retired
    _b.sweep_retired = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sweep blew up"))
    try:
        _b._sweep_retired_pass()
        check("a raising sweep does not escape the bridge's per-pass helper", True)
    except Exception as e:
        check("a raising sweep does not escape the bridge's per-pass helper", False, repr(e))
    finally:
        _b.sweep_retired = _saved
    _b.sweep_retired = lambda *_a, **_k: [Path("/x/proactive-late-y.txt")]
    try:
        _b._sweep_retired_pass(); check("the helper reports a republished remainder", True)
    finally:
        _b.sweep_retired = _saved

    # The remaining branches. Each is a distinct decision, and each was a way
    # bytes got destroyed or kept before, so none is filler for a coverage gate.
    c5 = d / "missing.txt"
    check("a missing claim retires (nothing to lose)",
          rd.retire_claim_if_unchanged(c5, "body") is True and not c5.exists())

    c6 = d / "emptied.txt"; c6.write_text("   \n")
    check("an emptied claim retires (nothing to resend)",
          rd.retire_claim_if_unchanged(c6, "body") is True and not c6.exists())

    c7 = d / "unreadable.txt"; c7.write_text("body\n")
    real = Path.read_bytes

    def _boom(self, *a, **k):
        if str(self) == str(c7):
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    Path.read_bytes = _boom
    try:
        kept7 = rd.retire_claim_if_unchanged(c7, "body") is False
    finally:
        Path.read_bytes = real
    check("an unreadable claim is kept, never destroyed unverified",
          kept7 and c7.exists())

    # Another consumer unlinked the claim between the read and the size check:
    # stat() raises, and releasing beats unlinking a path we cannot verify.
    c8 = d / "vanished.txt"; c8.write_text("body\n")
    real_stat = Path.stat

    def _gone(self, *a, **k):
        if str(self) == str(c8):
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(self, *a, **k)

    Path.stat = _gone
    try:
        released = rd.retire_claim_if_unchanged(c8, "body") is False
    finally:
        Path.stat = real_stat
    check("a claim that vanishes before the size check is released, not unlinked",
          released)


def _guard_refusal_row() -> None:
    """The POST-claim guard: body turned foreign after the peek, claim released.

    The guard runs twice — a pre-claim peek that just skips, and a post-claim
    re-check that must RELEASE. Only a body that passes the peek and then turns
    foreign reaches the second one, which is the producer-rewrites-under-us race
    this PR is about; a plain foreign body is rejected at the peek and never
    claimed.
    """
    print("slack post-claim body-guard refusal (claim released, not deleted):")
    bridge = _load_bridge("guardref")
    results = Path(bridge.RESULTS_DIR)
    results.mkdir(parents=True, exist_ok=True)

    name = "proactive-turns-foreign"
    src = results / f"{name}.txt"
    real_claim = bridge.claim_for_delivery

    def _rewrite_then_claim(path, recipient):
        # Runs AFTER the peek and BEFORE the post-claim read: exactly the window.
        if path.name == src.name and path.exists():
            path.write_text("[channel: 1535008729106485288]\nturned foreign")
        return real_claim(path, recipient)

    bridge.claim_for_delivery = _rewrite_then_claim
    threading.Thread(target=bridge.result_watcher, daemon=True).start()
    src.write_text("benign body that passes the peek")

    released = _settle(lambda: src.exists()
                       and "turned foreign" in src.read_text()
                       and not (results / f"{name}.sending").exists())
    check("a post-claim guard refusal releases the claim, never deletes it",
          released,
          "the refused claim was consumed or left in .sending")


if __name__ == "__main__":
    sys.exit(main())
