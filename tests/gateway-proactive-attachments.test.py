#!/usr/bin/env python3
"""A proactive result's `[file:]` attachment reaches the room, never silently dropped.

  a) text + [file:] to the owner DM  -> one message POST, then one media upload to the DM
  b) [channel: room] + [file:]       -> the upload goes to the named room
  c) a file outside the allowlist     -> the text is delivered, nothing uploaded, the refusal logged
  d) [file:] alone                    -> "(file attached)" is sent, not handed back as empty forever
  e) a second pass over a delivered item never uploads again
  f) the same name and prose re-published with a different attachment -> sent and uploaded again
  g) attachment-only, then a different attachment-only file under the same name -> both uploaded
  h) the same path whose bytes changed -> a new send and upload
  i) a redirect whose body the destination guard withholds -> the notice, no upload, file name only
  j) a clean redirect whose file is refused -> the refusal names only the file name
  k) an item with no attachments keeps its pre-change id
  l) a `~/` path whose bytes changed is a new send, fingerprinted as the uploader resolves it
  m) a FIFO outside the allowlist never stalls the drain: the text is sent, nothing uploaded
  n) a file outside the allowlist is never opened to fingerprint it
  o) an allowlisted file over the size cap is never read to fingerprint it
  p) the upload reader: a file that grows after its check is refused, never read past the cap
  q) the upload reader: an allowlisted file swapped for a symlink after its check is refused
  r) the canonical src/ copy gives the same identities and reads as the vendored one

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


def drain(body, passes: int = 1, name: str = "proactive-1.txt", between=None):
    """`passes` _post_proactive passes over one file name, each writing the next body of `body`
    (a list) or the same one; returns (message POSTs, media POSTs, logs)."""
    bodies = body if isinstance(body, list) else [body] * passes
    tmp = Path(tempfile.mkdtemp(dir=_ISO, prefix="drain-"))
    (tmp / "archive").mkdir()
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
        for i, text in enumerate(bodies):
            if i and between:
                between(i)
            (tmp / name).write_text(text, encoding="utf-8")
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

    import hashlib
    legacy = f"proactive-x~{hashlib.sha256(b'body').hexdigest()[:16]}"
    check(gb._proactive_item_id("proactive-x", "body") == legacy,
          "k) an item with no attachments keeps its existing id, so queued items are not re-sent")

    other = _ISO / "result" / "other.csv"
    other.write_text("c,d\n3,4\n", encoding="utf-8")
    messages, media, _ = drain([f"same prose\n[file: {CSV}]\n", f"same prose\n[file: {other}]\n"],
                               name="proactive-f.txt")
    check(len(messages) == 2, f"f) a changed attachment is a new send, got {len(messages)} messages")
    check([m[1].get("filename") for m in media] == ["report.csv", "other.csv"],
          f"f) both files uploaded in order, got {[m[1].get('filename') for m in media]}")

    messages, media, _ = drain([f"[file: {CSV}]\n", f"[file: {other}]\n"], name="proactive-g.txt")
    check([m[1].get("filename") for m in media] == ["report.csv", "other.csv"],
          f"g) attachment-only bodies no longer collide, got {[m[1].get('filename') for m in media]}")

    changing = _ISO / "result" / "changing.csv"
    changing.write_text("v1\n", encoding="utf-8")
    messages, media, _ = drain([f"export\n[file: {changing}]\n"] * 2, name="proactive-h.txt",
                               between=lambda _i: changing.write_text("v2 changed\n", encoding="utf-8"))
    check(len(media) == 2 and media[0][1]["content_b64"] != media[1][1]["content_b64"],
          f"h) changed bytes under the same path upload again, got {len(media)} uploads")

    messages, media, logs = drain(f"[channel: {ROOM}]\nsee /Users/someone/private/notes.txt\n[file: {CSV}]\n",
                                  name="proactive-i.txt")
    check(len(messages) == 1 and messages[0].get("body") == gb.team_result_guard.TEAM_LEAK_RESULT,
          f"i) the withheld notice is posted, got {messages}")
    check(media == [], f"i) no attachment reaches a room whose body was withheld, got {media}")
    withheld = [line for line in logs if "attachment(s) withheld" in line]
    check(len(withheld) == 1 and "report.csv" in withheld[0] and str(CSV.parent) not in withheld[0],
          f"i) the withhold names only the file name, got {withheld}")

    messages, media, logs = drain(f"[channel: {ROOM}]\nfor the room\n[file: {OUTSIDE}]\n",
                                  name="proactive-j.txt")
    refused = [line for line in logs if "attachment NOT sent" in line]
    check(len(messages) == 1 and media == [], f"j) text sent, nothing uploaded, got {messages} {media}")
    check(len(refused) == 1 and OUTSIDE.name in refused[0] and str(OUTSIDE.parent) not in refused[0],
          f"j) a guest room's refusal names only the file name, got {refused}")

    # l) HOME is the allowlisted dir, so `~/tilde.csv` is uploadable and lives there.
    tilde = _ISO / "result" / "tilde.csv"
    tilde.write_text("t1\n", encoding="utf-8")
    saved_home = os.environ["HOME"]
    os.environ["HOME"] = str(_ISO / "result")
    try:
        messages, media, _ = drain(["tilde export\n[file: ~/tilde.csv]\n"] * 2, name="proactive-l.txt",
                                   between=lambda _i: tilde.write_text("t2 changed\n", encoding="utf-8"))
    finally:
        os.environ["HOME"] = saved_home
    check(len(media) == 2 and media[0][1]["content_b64"] != media[1][1]["content_b64"],
          f"l) changed bytes under a ~/ path upload again, got {len(media)} uploads")

    import threading
    fifo = _ISO / "outside.fifo"
    os.mkfifo(fifo)
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(out=drain(f"fifo?\n[file: {fifo}]\n",
                                                                     name="proactive-m.txt")), daemon=True)
    worker.start()
    worker.join(10)
    stalled = worker.is_alive()
    if stalled:  # release the blocked reader so the run can finish
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(10)
    check(not stalled, "m) a FIFO outside the allowlist does not block the drain")
    messages, media, _ = box.get("out", ([], [], []))
    check(len(messages) == 1 and media == [], f"m) its text is sent and nothing uploaded, got {messages} {media}")

    from ag2_sparrow import proactive_routing as pr
    opened: list = []
    import builtins
    real_open, real_os_open = builtins.open, os.open

    def spy_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    def spy_os_open(path, *a, **kw):
        opened.append(str(path))
        return real_os_open(path, *a, **kw)

    pr.open, os.open = spy_open, spy_os_open
    try:
        gb._proactive_item_id("proactive-n", "x", [str(OUTSIDE)])
    finally:
        del pr.open
        os.open = real_os_open
    check(not any(OUTSIDE.name in p for p in opened), f"n) a refused file is not opened, opened {opened}")

    big = _ISO / "result" / "big.bin"
    big.write_bytes(b"z" * 64)
    saved_cap = gb.MAX_MEDIA_BYTES
    gb.MAX_MEDIA_BYTES = 16
    pr.open, os.open = spy_open, spy_os_open
    opened.clear()
    reads: list = []
    real_read = os.read
    os.read = lambda fd, n: reads.append(n) or real_read(fd, n)
    try:
        ident = pr.attachment_identity(str(big), gb.is_path_sendable, 16) \
            if "admit" in pr.attachment_identity.__code__.co_varnames else pr.attachment_identity(str(big))
    finally:
        del pr.open
        os.open, os.read = real_os_open, real_read
        gb.MAX_MEDIA_BYTES = saved_cap
    check(ident.endswith("over 16 bytes") and not reads, f"o) an oversize file is named, not read, got {ident!r} reads={reads}")

    import builtins
    grower = _ISO / "result" / "grower.bin"
    grower.write_bytes(b"g" * 10)
    real_b_open, real_os_open2 = builtins.open, os.open
    grown: list = []

    def grow_on_open(opener):
        def wrapped(path, *a, **kw):
            out = opener(path, *a, **kw)
            if str(path).endswith("grower.bin") and not grown:
                grown.append(1)
                with real_b_open(grower, "ab") as g:
                    g.write(b"G" * 1000)
            return out
        return wrapped
    saved_cap = gb.MAX_MEDIA_BYTES
    gb.MAX_MEDIA_BYTES = 16
    builtins.open, os.open = grow_on_open(real_b_open), grow_on_open(real_os_open2)
    try:
        name, b64, reason = gb._read_attachment(str(grower))
    finally:
        builtins.open, os.open = real_b_open, real_os_open2
        gb.MAX_MEDIA_BYTES = saved_cap
    sent = len(gb.base64.b64decode(b64)) if b64 else 0
    check(grown and not b64 and "exceeds" in reason,
          f"p) a file grown past the cap after its check is refused, sent {sent} bytes, reason {reason!r}")

    swapped = _ISO / "result" / "swapped.txt"
    swapped.write_text("allowed\n", encoding="utf-8")
    real_admit = gb.is_path_sendable
    swaps: list = []

    def admit_then_swap(path):
        ok = real_admit(path)
        if ok and str(path).endswith("swapped.txt") and not swaps:
            swaps.append(1)
            os.unlink(swapped)
            os.symlink(OUTSIDE, swapped)
        return ok
    gb.is_path_sendable = admit_then_swap
    try:
        name, b64, reason = gb._read_attachment(str(swapped))
    finally:
        gb.is_path_sendable = real_admit
    leaked = gb.base64.b64decode(b64) if b64 else b""
    check(swaps and not b64 and b"not for sending" not in leaked,
          f"q) a symlink swapped in after the check is refused, got {leaked!r} reason {reason!r}")

    import importlib.util

    def _src(name):  # the canonical src/ copy by file: the vendored dir can shadow a flat import
        spec = importlib.util.spec_from_file_location(name, REPO / "src" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
        return mod
    src_bfr = _src("bounded_file_read")
    src_pr = _src("proactive_routing")
    from ag2_sparrow import proactive_routing as pkg_pr  # noqa: E402
    fifo2 = _ISO / "result" / "inside.fifo"
    os.mkfifo(fifo2)
    once: list = []

    def admit_once(path):  # admits, then says no on the post-open recheck
        once.append(path)
        return len(once) == 1
    cases = [(str(CSV), real_admit, 1 << 20), (str(OUTSIDE), real_admit, 1 << 20), (str(big), real_admit, 16),
             (str(fifo2), lambda _p: True, 1 << 20), (str(_ISO / "result" / "missing.csv"), lambda _p: True, 99),
             (str(CSV), None, 99)]
    for path, admit, cap in cases:
        check(src_pr.attachment_identity(path, admit, cap) == pkg_pr.attachment_identity(path, admit, cap),
              f"r) src and vendored identities agree for {Path(path).name}")
    from ag2_sparrow import bounded_file_read as pkg_bfr  # noqa: E402
    for label, pr_mod, bfr in (("src", src_pr, src_bfr), ("vendored", pkg_pr, pkg_bfr)):
        check(pr_mod.read_attachment(str(CSV), real_admit, 99)[1] == CSV.read_bytes(),
              f"r) {label}: the reader returns the admitted file's bytes")
        once.clear()
        check(pr_mod.read_attachment(str(CSV), admit_once, 99)[2] == "changed after its check",
              f"r) {label}: a file the recheck no longer admits is refused")
        real_stat, gone_path = os.stat, os.path.realpath(CSV)  # resolved before the hook: resolve() calls os.stat

        def stat_gone(p, *a, real_stat=real_stat, gone_path=gone_path, **kw):  # vanishes after its open
            if os.fspath(p) == gone_path:
                raise FileNotFoundError(p)
            return real_stat(p, *a, **kw)
        os.stat = stat_gone
        try:
            gone = pr_mod.read_attachment(str(CSV), lambda _p: True, 99)
        finally:
            os.stat = real_stat
        check(gone[2] == "changed after its check", f"r) {label}: a file gone after its open is refused")
        rfd, wfd = os.pipe()
        os.write(wfd, b"x" * 40)
        os.close(wfd)
        tiny = os.stat(CSV)  # a regular file's fstat claiming 8 bytes, while 40 arrive
        fake = os.stat_result((tiny.st_mode, 0, 0, 0, 0, 0, 8, 0, 0, 0))
        check(bfr.read_fd_bytes(rfd, 16, fake) == (None, "over 16 bytes"),
              f"r) {label}: bytes past the cap after the fstat are refused")
        os.close(rfd)
    check(src_pr.resolve_attachment_path(" ~/x ") == os.path.realpath(os.path.expanduser("~/x")),
          "r) the src resolver expands ~ and resolves links")
    check(src_bfr.read_regular("/nonexistent-dir/x") == (None, ""), "r) read_regular still reports absence")
    utf = _ISO / "result" / "bad.txt"
    utf.write_bytes(b"\xff\xfe")
    check(src_bfr.read_regular(str(utf)) == (None, "not UTF-8"), "r) read_regular still refuses non-UTF-8")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
