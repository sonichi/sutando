#!/usr/bin/env python3
"""Regression guard for restart-safety #3: result-delivery idempotency sentinel.

If the bridge crashes between channel.send() returning and archive_file()
finishing, on restart the result file is still on disk and gets re-sent —
duplicate delivery. The fix: touch a sentinel after send, check it before
send on restart, clear it after archive.

Run: python3 tests/discord-bridge-delivery-sentinel.test.py
Exit: 0 on pass, non-zero on fail.
"""

import importlib.util
import os
import re
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
    if bridge.DELIVERED_DIR.exists():
        for p in bridge.DELIVERED_DIR.iterdir():
            p.unlink()


def test_is_delivered_false_when_no_sentinel():
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
    import shutil
    if bridge.DELIVERED_DIR.exists():
        shutil.rmtree(bridge.DELIVERED_DIR)
    bridge._mark_delivered("task-fresh")
    assert bridge.DELIVERED_DIR.is_dir()
    assert bridge._is_delivered("task-fresh") is True


def test_clear_idempotent():
    _clear_sentinels()
    bridge._clear_delivered("task-never-existed")
    assert bridge._is_delivered("task-never-existed") is False


def test_separate_tasks_independent():
    _clear_sentinels()
    bridge._mark_delivered("task-A")
    assert bridge._is_delivered("task-A") is True
    assert bridge._is_delivered("task-B") is False
    bridge._mark_delivered("task-B")
    bridge._clear_delivered("task-A")
    assert bridge._is_delivered("task-A") is False
    assert bridge._is_delivered("task-B") is True


def test_poll_results_checks_sentinel_before_main_send():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    poll_block = re.search(
        r"async def poll_results\(\):(.*?)(?=^async def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert poll_block, "could not locate poll_results"
    body = poll_block.group(1)
    delivered_pos = body.find("_is_delivered(task_id)")
    skip_continue_pos = body.find("Skipped (already replied or deduped)")
    first_send_pos = body.find("await channel.send(", skip_continue_pos)
    assert delivered_pos > 0, "_is_delivered NOT called in poll_results"
    assert first_send_pos > 0, "could not locate post-skip channel.send"
    assert delivered_pos < first_send_pos, (
        "_is_delivered check must come BEFORE the first channel.send"
    )


def test_poll_results_marks_delivered_in_send_block():
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
        "_mark_delivered must be called AFTER the first channel.send"
    )


def test_poll_results_clears_sentinel_after_archive():
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
            print(f"  ✗ {fn.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s).")
        sys.exit(1)
    print("All delivery-sentinel tests passed.")


if __name__ == "__main__":
    main()
