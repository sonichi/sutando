#!/usr/bin/env python3
"""The gateway's proactive drain posts a result only once it is ready (#3956).

`_post_proactive` claims `results/proactive-*.txt` by rename and then reads the
claim. A writer that creates the name and fills it afterwards leaves a prefix
there; the drain must hand it back, not post it. The post-claim read goes through
the shared readiness owner (`result_ready.read_ready_result`), like the slack,
discord and telegram proactive drains.

  a) a body still growing during the readiness hold -> nothing posted, handed back as .txt
  b) the same file once settled                      -> posted whole on the next pass
  c) an empty claim                                  -> nothing posted, handed back
  d) a settled body                                  -> posted unchanged (no regression)

Run: python3 tests/gateway-proactive-readiness.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="gw-ready-cc-")
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402
from ag2_sparrow import result_ready  # noqa: E402

ROOM = "!ReadyRoomAbCdEf:ag2.space"
HEAD = f"[channel: {ROOM}]\n" + "H" * 200 + "\n"
TAIL = "B" * 2575 + "\n"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def drain(tmp: Path, during_hold=None, claim_gate=None) -> list[str]:
    """One `_post_proactive` pass over `tmp`; `during_hold` runs inside the readiness hold."""
    posts: list[str] = []

    def _fake_req(method, path, payload=None, timeout=None):
        if method == "POST":
            posts.append(payload.get("body", ""))
        return {"ok": True, "event_id": "$evt"}

    def _hold(_sec):
        if during_hold is not None:
            during_hold()

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM, gb._req,
             gb.PROACTIVE_CLAIM_GATE, gb.PROACTIVE_ROOM_GATE, result_ready._sleep)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM, gb._req = "", _fake_req
    gb.PROACTIVE_CLAIM_GATE, gb.PROACTIVE_ROOM_GATE = claim_gate, None
    result_ready._sleep = _hold
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM, gb._req,
         gb.PROACTIVE_CLAIM_GATE, gb.PROACTIVE_ROOM_GATE, result_ready._sleep) = saved
    return posts


def fresh() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="gw-ready-"))
    (tmp / "archive").mkdir()
    return tmp


def main() -> int:
    # a) The writer lands its first chunk; the rest arrives while the drain holds.
    tmp = fresh()
    f = tmp / "proactive-1.txt"
    f.write_text(HEAD, encoding="utf-8")

    def _writer_appends():
        claim = next(tmp.glob("proactive-1.sending.*"), f)
        with open(claim, "a", encoding="utf-8") as fh:
            fh.write(TAIL)

    posts = drain(tmp, during_hold=_writer_appends)
    check(posts == [], f"a) a body still being written is not posted, got {[len(p) for p in posts]}")
    check(f.exists() and not list(tmp.glob("proactive-1.sending*")),
          "a) and it is handed back under its own name")

    # b) Settled now: the next pass delivers the whole body.
    if f.exists():
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime - 5))
    posts = drain(tmp)
    check(len(posts) == 1 and posts[0].endswith("B" * 2575), f"b) the settled file posts whole, got {[len(p) for p in posts]}")
    check(not f.exists(), "b) and is retired after delivery")

    # c) Addressed at the peek, truncated before the claim: hand back, never post.
    tmp = fresh()
    g = tmp / "proactive-2.txt"
    g.write_text(f"[channel: {ROOM}]\nlater\n", encoding="utf-8")
    posts = drain(tmp, claim_gate=lambda p: (p.write_text("", encoding="utf-8"), True)[1])
    check(posts == [] and g.exists() and g.stat().st_size == 0,
          f"c) an empty claim is handed back, nothing posted, got {posts}")

    # d) No regression for an ordinary settled body.
    tmp = fresh()
    h = tmp / "proactive-3.txt"
    h.write_text(f"[channel: {ROOM}]\nhello room\n", encoding="utf-8")
    os.utime(h, (h.stat().st_atime, h.stat().st_mtime - 5))
    posts = drain(tmp)
    check(posts == ["hello room"], f"d) a settled body posts unchanged, got {posts}")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — the gateway proactive drain posts only ready bodies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
