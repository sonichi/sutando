#!/usr/bin/env python3
"""Unit tests for `_select_sibling_attachments` in `src/discord-bridge.py`.

Regression guard for the cross-instance media gap (2026-07-16): when someone
pings the bot with text but no media of their own and references ANOTHER user
("@bot make the video @Alice sent"), the media lives on that user's own earlier,
un-mentioned messages. Those messages neither invoke the bot nor are replied-to,
so the primary attachment loop and the reply-context loop both miss them.

`_select_sibling_attachments(history, referenced_ids, cutoff, cap)` is the pure
selection logic that decides which sibling attachments to pull. It is kept out of
the async download I/O so it can be tested without a live discord channel. These
tests pin: referenced-user filtering, the time-window cutoff (ordered early-stop),
the cap, oldest-first ordering, and defensive handling of empty/attachmentless
messages.
"""

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Same module-load harness as the sibling attachment-filename test: stub the
# `discord` module + a fake token so discord-bridge.py imports cleanly in CI.
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-discord-sibling-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ["SUTANDO_TEST_MODE"] = "1"
_HOME_TMP = tempfile.mkdtemp(prefix="sutando-discord-sibling-test-home-")
os.environ["HOME"] = _HOME_TMP
_token_dir = Path(_HOME_TMP) / ".claude" / "channels" / "discord"
_token_dir.mkdir(parents=True, exist_ok=True)
(_token_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")


# Isolate BEFORE the bridge is imported: it resolves channel config at module
# level, so an unset CLAUDE_CONFIG_DIR reads the developer's real allowlist.
_CFG = tempfile.mkdtemp(prefix="ccd-sibling-attachments-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
# Seeding access.json is the load-bearing half: a temp dir alone still falls
# back to the operator's file.
_cfg_discord = Path(_CFG) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text(json.dumps({"allowFrom": []}))


def _load(name: str, path: Path):
    if "discord" not in sys.modules:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("discord_bridge", REPO / "src" / "discord-bridge.py")
select = bridge._select_sibling_attachments


# --- fakes -----------------------------------------------------------------
class _Author:
    def __init__(self, uid, name):
        self.id = uid
        self._name = name

    def __str__(self):
        return self._name


class _Att:
    def __init__(self, filename):
        self.filename = filename


class _Msg:
    def __init__(self, uid, name, created_at, filenames):
        self.author = _Author(uid, name)
        self.created_at = created_at
        self.attachments = [_Att(f) for f in filenames]


_NOW = datetime(2026, 7, 16, 2, 15, 0)
_CUTOFF = _NOW - timedelta(minutes=15)


def _hist(*msgs):
    """History is newest-first (as discord channel.history(before=...) yields)."""
    return list(msgs)


# --- tests -----------------------------------------------------------------
def test_picks_referenced_users_media():
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["garden.mp4"]),
    )
    picked = select(history, {999}, _CUTOFF)
    assert len(picked) == 1, picked
    author, att = picked[0]
    assert author == "Alice"
    assert att.filename == "garden.mp4"


def test_skips_non_referenced_users():
    history = _hist(
        _Msg(111, "Bob", _NOW - timedelta(minutes=1), ["random.png"]),
        _Msg(999, "Alice", _NOW - timedelta(minutes=2), ["wanted.mp4"]),
    )
    picked = select(history, {999}, _CUTOFF)
    assert len(picked) == 1
    assert picked[0][1].filename == "wanted.mp4"


def test_respects_cutoff_early_stop():
    # Newest-first: the scan stops at the first older-than-cutoff message, so an
    # in-window attachment behind an out-of-window one is intentionally missed.
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=20), ["too-old.mp4"]),
        _Msg(999, "Alice", _NOW - timedelta(minutes=25), ["also-old.mp4"]),
    )
    picked = select(history, {999}, _CUTOFF)
    assert picked == [], picked


def test_cap_limits_total():
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), [f"f{i}.png" for i in range(10)]),
    )
    picked = select(history, {999}, _CUTOFF, cap=3)
    assert len(picked) == 3, picked


def test_oldest_first_ordering():
    # newest-first input; each message has one attachment
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["newest.mp4"]),
        _Msg(999, "Alice", _NOW - timedelta(minutes=3), ["middle.mp4"]),
        _Msg(999, "Alice", _NOW - timedelta(minutes=5), ["oldest.mp4"]),
    )
    picked = select(history, {999}, _CUTOFF)
    names = [a.filename for _, a in picked]
    assert names == ["oldest.mp4", "middle.mp4", "newest.mp4"], names


def test_messages_without_attachments_ignored():
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), []),
        _Msg(999, "Alice", _NOW - timedelta(minutes=2), ["real.mp4"]),
    )
    picked = select(history, {999}, _CUTOFF)
    assert len(picked) == 1
    assert picked[0][1].filename == "real.mp4"


def test_empty_history_returns_empty():
    assert select([], {999}, _CUTOFF) == []


def test_empty_referenced_ids_returns_empty():
    history = _hist(_Msg(999, "Alice", _NOW - timedelta(minutes=1), ["x.mp4"]))
    assert select(history, set(), _CUTOFF) == []


def test_multiple_referenced_users():
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["a.mp4"]),
        _Msg(888, "Cara", _NOW - timedelta(minutes=2), ["c.mp4"]),
        _Msg(111, "Bob", _NOW - timedelta(minutes=3), ["ignored.png"]),
    )
    picked = select(history, {999, 888}, _CUTOFF)
    names = sorted(a.filename for _, a in picked)
    assert names == ["a.mp4", "c.mp4"], names


def test_sibling_history_fetch_is_time_bounded():
    # A fixed message count truncates a busy channel before the in-window
    # attachment; the scan lives in an un-coverable handler, so pin it here.
    src = (REPO / "src" / "discord-bridge.py").read_text()
    assert "after=cutoff" in src, "sibling history scan must be time-bounded (after=cutoff)"
    assert "limit=15, before=message" not in src, "bare 15-message count fetch must be gone"
    # CR #2126 (round 4): any message-count cap re-introduces truncation — the
    # scan must use limit=None (pure time bound), not limit=15/200/etc.
    import re as _re
    m = _re.search(r"channel\.history\(\s*limit=(\w+),\s*after=cutoff", src)
    assert m is not None, "sibling history call not found in expected shape"
    assert m.group(1) == "None", f"sibling history must use limit=None, got limit={m.group(1)}"


def test_single_message_preserves_attachment_order():
    # CR #2126: one message with several attachments must keep UPLOAD order —
    # the prior flat-list reverse inverted attachments within each message.
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["first.png", "second.png", "third.png"]),
    )
    picked = select(history, {999}, _CUTOFF)
    names = [a.filename for _, a in picked]
    assert names == ["first.png", "second.png", "third.png"], names


def test_multi_message_multi_attachment_ordering():
    # Oldest MESSAGE first, but each message's own attachments in upload order.
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["new1.mp4", "new2.mp4"]),  # newest
        _Msg(999, "Alice", _NOW - timedelta(minutes=3), ["old1.mp4", "old2.mp4"]),  # older
    )
    picked = select(history, {999}, _CUTOFF)
    names = [a.filename for _, a in picked]
    assert names == ["old1.mp4", "old2.mp4", "new1.mp4", "new2.mp4"], names


def test_cap_across_messages_keeps_nearest_in_order():
    # cap spans two messages: keep all of the nearest (newest) message + the
    # first of the older, still emitted oldest-message-first, upload order intact.
    history = _hist(
        _Msg(999, "Alice", _NOW - timedelta(minutes=1), ["n1.png", "n2.png", "n3.png"]),  # newest
        _Msg(999, "Alice", _NOW - timedelta(minutes=3), ["o1.png", "o2.png"]),            # older
    )
    picked = select(history, {999}, _CUTOFF, cap=4)
    names = [a.filename for _, a in picked]
    assert names == ["o1.png", "n1.png", "n2.png", "n3.png"], names


# --- _process_sibling_attachment: the download + OFFLOADED transcription -------
# (CR #2126 — the transcription subprocess must run off the gateway event loop.)
import asyncio
import threading


class _FakeAtt:
    def __init__(self, filename, content_type="", data=b"x"):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self._data = data

    async def save(self, path):
        Path(path).write_bytes(self._data)


class _FailAtt:
    filename = "bad.mp3"
    content_type = "audio/mpeg"
    size = 1

    async def save(self, path):
        raise RuntimeError("network error")


def _reset_patches():
    bridge._push_vision_image = lambda *a, **k: None


def test_sibling_attachment_offloads_transcription():
    # The load-bearing assertion for CR #2126: _transcribe_via_skill (blocking,
    # long timeout) must run OFF the event-loop thread via asyncio.to_thread.
    _reset_patches()
    main_thread = threading.current_thread()
    seen = {}

    def fake_transcribe(p):
        seen["thread"] = threading.current_thread()
        seen["path"] = p
        return "hello world"

    orig = bridge._transcribe_via_skill
    bridge._transcribe_via_skill = fake_transcribe
    try:
        att = _FakeAtt("note.ogg", "audio/ogg")
        ref, note = asyncio.run(bridge._process_sibling_attachment(att, "Alice"))
    finally:
        bridge._transcribe_via_skill = orig
    assert ref is not None, "an attachment ref must be returned on success"
    assert "Voice transcript (from Alice's recent message): hello world" in note, note
    assert seen["thread"] is not main_thread, "transcription must run off the event-loop thread"


def test_sibling_attachment_file_note_when_no_transcript():
    _reset_patches()
    orig = bridge._transcribe_via_skill
    bridge._transcribe_via_skill = lambda p: None
    try:
        ref, note = asyncio.run(bridge._process_sibling_attachment(_FakeAtt("doc.pdf", "application/pdf"), "Bob"))
    finally:
        bridge._transcribe_via_skill = orig
    assert ref is not None and "File attached (from Bob's recent message)" in note, note


def test_sibling_attachment_download_failure_returns_none():
    _reset_patches()
    ref, note = asyncio.run(bridge._process_sibling_attachment(_FailAtt(), "Cara"))
    assert ref is None and note == "", (ref, note)


def test_sibling_attachment_image_pushed_to_vision():
    orig = bridge._transcribe_via_skill
    bridge._transcribe_via_skill = lambda p: None
    pushed = []
    bridge._push_vision_image = lambda path, source="discord": pushed.append((path, source))
    try:
        ref, note = asyncio.run(bridge._process_sibling_attachment(_FakeAtt("pic.png", "image/png"), "Dan"))
    finally:
        bridge._transcribe_via_skill = orig
        _reset_patches()
    assert ref is not None, "image attachment must still produce a ref"
    assert len(pushed) == 1 and pushed[0][1] == "discord", pushed


def test_sibling_attachment_vision_error_swallowed():
    # A vision-push failure must not break note-building or the return value.
    orig = bridge._transcribe_via_skill
    bridge._transcribe_via_skill = lambda p: None

    def boom(*a, **k):
        raise RuntimeError("vision service down")

    bridge._push_vision_image = boom
    try:
        ref, note = asyncio.run(bridge._process_sibling_attachment(_FakeAtt("pic.png", "image/png"), "Ed"))
    finally:
        bridge._transcribe_via_skill = orig
        _reset_patches()
    assert ref is not None and "File attached (from Ed's recent message)" in note, note


def main():
    failures = []
    for fn in (
        test_picks_referenced_users_media,
        test_skips_non_referenced_users,
        test_respects_cutoff_early_stop,
        test_cap_limits_total,
        test_oldest_first_ordering,
        test_messages_without_attachments_ignored,
        test_empty_history_returns_empty,
        test_empty_referenced_ids_returns_empty,
        test_multiple_referenced_users,
        test_sibling_history_fetch_is_time_bounded,
        test_single_message_preserves_attachment_order,
        test_multi_message_multi_attachment_ordering,
        test_cap_across_messages_keeps_nearest_in_order,
        test_sibling_attachment_offloads_transcription,
        test_sibling_attachment_file_note_when_no_transcript,
        test_sibling_attachment_download_failure_returns_none,
        test_sibling_attachment_image_pushed_to_vision,
        test_sibling_attachment_vision_error_swallowed,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All 18 sibling-attachment tests passed (selection + offloaded processing).")


if __name__ == "__main__":
    main()
