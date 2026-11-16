#!/usr/bin/env python3
"""A proactive result's `[file:]` attachment reaches the room, never silently dropped.

  a) text + [file:] to the owner DM  -> one message POST, then one media upload to the DM
  b) [channel: room] + [file:]       -> the upload goes to the named room
  c) a file outside the allowlist     -> the text is delivered, nothing uploaded, the refusal logged
  d) [file:] alone                    -> "(file attached)" is sent, not handed back as empty forever
  e) a second pass over a delivered item never uploads again

Imports the vendored module with an isolated env; never execs the wrapper.
Run: python3 tests/gateway-proactive-attachments.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
_ISO = Path(tempfile.mkdtemp(prefix="gw-proactive-attach-"))
for _k in ("task", "result", "state"):
    (_ISO / _k).mkdir()
os.environ.update({
    "HOME": str(_ISO / "home"),
    "CLAUDE_CONFIG_DIR": str(_ISO / "claude"),
    "REMOTE_TASK_TOKEN": "https://gw.example/relay|test-token",
    "REMOTE_PROACTIVE_ROOM": "",
    "AGENT_CONNECT_TASK_DIR": str(_ISO / "task"),
    "AGENT_CONNECT_RESULT_DIR": str(_ISO / "result"),
    "AGENT_CONNECT_STATE_DIR": str(_ISO / "state"),
})

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

ROOM = "!TargetRoomAbCdEf:ag2.space"
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
# The package's default allowlist root is the result dir.
CSV = _ISO / "result" / "report.csv"
CSV.write_text("a,b\n1,2\n", encoding="utf-8")
OUTSIDE = _ISO / "home-secret.txt"
OUTSIDE.write_text("not for sending\n", encoding="utf-8")
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def drain(body: str, passes: int = 1, name: str = "proactive-1.txt"):
    """`passes` _post_proactive passes over one file; returns (message POSTs, media POSTs, logs)."""
    tmp = Path(tempfile.mkdtemp(dir=_ISO, prefix="drain-"))
    (tmp / "archive").mkdir()
    (tmp / name).write_text(body, encoding="utf-8")
    messages: list[dict] = []
    media: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "POST" and path.endswith("/media"):
            media.append((path, payload))
        elif method == "POST":
            messages.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = OWNER_DM, _fake_req, None
    gb._log = logs.append
    gb._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=gb.time.time() + 3600)
    try:
        for i in range(passes):
            if i:  # the same body comes back, as a hand-back or a re-written file would
                (tmp / name).write_text(body, encoding="utf-8")
            gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log) = saved
        gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
    return messages, media, logs


def _room_of(path: str) -> str:
    return gb.urllib.parse.unquote(path.split("/v1/rooms/", 1)[1].rsplit("/media", 1)[0])


def main() -> int:
    messages, media, _ = drain(f"Here is the export.\n[file: {CSV}]\n")
    check(len(messages) == 1, f"a) one message, got {len(messages)}")
    check((messages[0] if messages else {}).get("body") == "Here is the export.",
          f"a) marker stripped from the text, got {messages}")
    check(len(media) == 1, f"a) one upload, got {media}")
    if media:
        check(_room_of(media[0][0]) == OWNER_DM, f"a) uploaded to the owner DM, got {media[0][0]}")
        check(media[0][1].get("filename") == "report.csv", f"a) filename sent, got {media[0][1].get('filename')}")

    messages, media, _ = drain(f"[channel: {ROOM}]\nfor the room\n[file: {CSV}]\n")
    check(len(media) == 1 and _room_of(media[0][0]) == ROOM, f"b) uploaded to the named room, got {media}")

    messages, media, logs = drain(f"private?\n[file: {OUTSIDE}]\n")
    check(len(messages) == 1, f"c) the text still delivered, got {messages}")
    check(media == [], f"c) nothing uploaded from outside the allowlist, got {media}")
    check(any("attachment NOT sent" in line and "not allowlisted" in line for line in logs),
          f"c) the refusal is logged, got {logs}")

    messages, media, _ = drain(f"[file: {CSV}]\n")
    check([m.get("body") for m in messages] == ["(file attached)"],
          f"d) an attachment-only body sends a placeholder, got {messages}")
    check(len(media) == 1, f"d) and uploads the file, got {media}")

    messages, media, logs = drain(f"once\n[file: {CSV}]\n", passes=2)
    check(len(messages) == 1, f"e) one message across two passes, got {len(messages)}")
    check(len(media) == 1, f"e) one upload across two passes, got {media}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
