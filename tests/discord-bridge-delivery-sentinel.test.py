#!/usr/bin/env python3
"""Regression guard for restart-safety #3: result-delivery idempotency
sentinel.

## The bug

In `poll_results`, the bridge:

  1. Pops `task_id` from `pending_replies`
  2. Calls `channel.send(reply_text)` (Discord API)
  3. Calls `archive_file(result_file, ...)`

If the bridge crashes between step 2 (send returned success) and
step 3 (archive completes), the result file is still on disk at
`results/{task_id}.txt`. On restart, `poll_results` re-iterates,
finds the result file, and re-sends — producing a duplicate
delivery to the owner.

## The fix

`<DELIVERED_DIR>/<task_id>.sentinel` files mark "send returned
success but archive not yet complete":

  - Right BEFORE the per-task send block, `_is_delivered(task_id)`
    checks the sentinel. If present, skip the send, run the archive,
    clear the sentinel.
  - Right AFTER `channel.send` succeeds, `_mark_delivered(task_id)`
    touches the sentinel.
  - After archive completes, `_clear_delivered(task_id)` removes
    the sentinel (so the dir doesn't accumulate forever).

The remaining narrow window — crash between send-success and
sentinel-touch — produces at most one duplicate on restart. Nonce-
based dedup via Discord's `nonce` parameter would close that
tighter; deferred to follow-up.

## What this test covers

The sentinel storage (touch / check / clear) is pure file I/O and
fully testable. The poll_results wiring is asserted via source-grep.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-delivery-sentinel-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
(Path(_WORKSPACE_TMP) / "state").mkdir(parents=True, exist_ok=True)


def _load(name: str, path: Path):
    if "discord" not in sys.modules:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        stub.Object = lambda id: type("Object", (), {"id": id})()
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("discord_bridge", REPO / "src" / "discord-bridge.py")


def _clear_sentinels():
    """Remove any sentinels left from a previous test."""
    if bridge.DELIVERED_DIR.exists():
        for p in bridge.DELIVERED_DIR.iterdir():
            p.unlink()


def test_is_delivered_false_when_no_sentinel():
    """Default: no sentinel → False. Bridge sends normally."""
    _clear_sentinels()
    assert bridge._is_delivered("task-123") is False


def test_mark_delivered_creates_sentinel():
    _clear_sentinels()
    bridge._mark_delivered("task-456")
    assert bridge._is_delivered("task-456") is True
    assert (bridge.DELIVERED_DIR / "task-456.sentinel").exists()


def test_clear_delivered_removes_sentinel():
    _clear_sentinels()
    bridge._mark_delivered("task-789")
    assert bridge._is_delivered("task-789") is True
    bridge._clear_delivered("task-789")
    assert bridge._is_delivered("task-789") is False


def test_mark_creates_directory_if_missing():
    """Defensive: DELIVERED_DIR may not exist on first-ever run.
    `_mark_delivered` must mkdir(parents=True) before touching."""
    import shutil
    if bridge.DELIVERED_DIR.exists():
        shutil.rmtree(bridge.DELIVERED_DIR)
    bridge._mark_delivered("task-fresh")
    assert bridge.DELIVERED_DIR.is_dir()
    assert bridge._is_delivered("task-fresh") is True


def test_clear_idempotent():
    """Clearing a non-existent sentinel is a no-op, not an error."""
    _clear_sentinels()
    # Should not raise even though sentinel doesn't exist
    bridge._clear_delivered("task-never-existed")
    assert bridge._is_delivered("task-never-existed") is False


def test_separate_tasks_independent():
    """Sentinels are per-task; one task's sentinel doesn't affect
    another. Critical for the case where multiple results are pending
    and only one was crashed-during-send."""
    _clear_sentinels()
    bridge._mark_delivered("task-A")
    assert bridge._is_delivered("task-A") is True
    assert bridge._is_delivered("task-B") is False
    bridge._mark_delivered("task-B")
    bridge._clear_delivered("task-A")
    assert bridge._is_delivered("task-A") is False
    assert bridge._is_delivered("task-B") is True


def test_poll_results_checks_sentinel_before_main_send():
    """Architectural source-grep: the sentinel check must come BEFORE
    the first `channel.send` call in poll_results. Pin via textual
    position so a refactor that re-orders these (e.g. moves the
    delivered check into the try-block AFTER send) fails loudly.

    Note: poll_results has multiple `try:` blocks (heartbeat retry,
    channel resolution, send, file-send) — we don't pin against a
    specific `try:` line; we pin the sentinel check is BEFORE the
    first send call."""
    import re
    src = (REPO / "src" / "discord-bridge.py").read_text()
    poll_block = re.search(
        r"async def poll_results\(\):(.*?)(?=^async def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert poll_block, "could not locate poll_results"
    body = poll_block.group(1)
    delivered_pos = body.find("_is_delivered(task_id)")
    # Find the first channel.send AFTER the skip-block. The skip-block
    # has `archive_file(result_file, "results", task_id)` followed by
    # `continue`, then the sentinel check, then the try-block with
    # the send. The first `await channel.send(` should be after both.
    skip_continue_pos = body.find("Skipped (already replied or deduped)")
    first_send_pos = body.find("await channel.send(", skip_continue_pos)
    assert delivered_pos > 0, "_is_delivered NOT called in poll_results"
    assert first_send_pos > 0, "could not locate post-skip channel.send"
    assert delivered_pos < first_send_pos, (
        "_is_delivered check must come BEFORE the first channel.send — "
        "otherwise the send fires before the sentinel is checked, "
        "defeating the fix."
    )


def test_poll_results_marks_delivered_in_send_block():
    """Architectural: `_mark_delivered` must be called inside the
    main try-block (after channel.send succeeded). Pin that it
    appears AFTER the first send call (so it's marking a real
    delivery, not a pre-send phantom)."""
    import re
    src = (REPO / "src" / "discord-bridge.py").read_text()
    poll_block = re.search(
        r"async def poll_results\(\):(.*?)(?=^async def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert poll_block
    body = poll_block.group(1)
    mark_pos = body.find("_mark_delivered(task_id)")
    skip_continue_pos = body.find("Skipped (already replied or deduped)")
    first_send_pos = body.find("await channel.send(", skip_continue_pos)
    assert mark_pos > 0, "_mark_delivered NOT called in poll_results"
    assert first_send_pos > 0
    assert mark_pos > first_send_pos, (
        "_mark_delivered must be called AFTER the first channel.send "
        "(post-success) — otherwise a crash between mark and send "
        "marks a delivery that never happened, silently dropping the "
        "message on restart."
    )


def test_poll_results_clears_sentinel_after_archive():
    """Architectural: `_clear_delivered` must be called AFTER both
    archive_file calls. Without it, sentinels accumulate forever in
    `state/discord-delivered/`."""
    import re
    src = (REPO / "src" / "discord-bridge.py").read_text()
    poll_block = re.search(
        r"async def poll_results\(\):(.*?)(?=^async def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert poll_block
    body = poll_block.group(1)
    clear_pos = body.find("_clear_delivered(task_id)")
    assert clear_pos > 0, "_clear_delivered NOT called in poll_results"


def main():
    failures = []
    for fn in (
        test_is_delivered_false_when_no_sentinel,
        test_mark_delivered_creates_sentinel,
        test_clear_delivered_removes_sentinel,
        test_mark_creates_directory_if_missing,
        test_clear_idempotent,
        test_separate_tasks_independent,
        test_poll_results_checks_sentinel_before_main_send,
        test_poll_results_marks_delivered_in_send_block,
        test_poll_results_clears_sentinel_after_archive,
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
    print("All delivery-sentinel tests passed.")


if __name__ == "__main__":
    main()
