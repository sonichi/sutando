#!/usr/bin/env python3
"""Regression guard for restart-safety #1: orphan `.sending` file recovery.

## The bug

Both bridges atomic-claim `results/proactive-*.txt` files via
`rename(proactive-X.txt → proactive-X.sending)` before sending. The
rename prevents same-tick double-deliveries between concurrent poll
iterations.

If the bridge crashes BETWEEN the rename and the delivery, the
`.sending` file sits orphaned in `results/` forever — no poll
iteration ever looks at `.sending` suffixes. The proactive
notification is silently dropped until next manual intervention.

The fix adds a startup sweep:

  - discord-bridge.py:on_ready → `_recover_orphan_sending_files()`
  - telegram-bridge.py:main → `_recover_orphan_sending_files()`

Both rename `*.sending` back to `*.txt` so the normal claim-and-
deliver flow picks them up on the next poll iteration.

This test exercises BOTH bridges' recovery helpers directly.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Set workspace BEFORE importing the bridges — they capture
# `RESULTS_DIR = REPO / "results"` at module-load time.
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-orphan-recovery-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

_results = Path(_WORKSPACE_TMP) / "results"
_results.mkdir(parents=True, exist_ok=True)


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


discord_bridge = _load("discord_bridge", REPO / "src" / "discord-bridge.py")
telegram_bridge = _load("telegram_bridge", REPO / "src" / "telegram-bridge.py")


def _clear_results():
    """Delete any files in the test workspace's results/ between
    tests so each starts from a clean slate."""
    for p in _results.iterdir():
        if p.is_file():
            p.unlink()


def _make_orphan(name: str, content: str = "fake-proactive") -> Path:
    """Create an orphan `.sending` file in the test results/ dir."""
    p = _results / name
    p.write_text(content)
    return p


def _make_normal_txt(name: str, content: str = "fake-proactive") -> Path:
    """Create a regular `.txt` proactive file (not orphaned)."""
    p = _results / name
    p.write_text(content)
    return p


def test_discord_bridge_recovers_orphan_sending_file():
    """Headline case: a single orphan `proactive-X.sending` file is
    renamed back to `proactive-X.txt` on startup."""
    _clear_results()
    orphan = _make_orphan("proactive-test-1.sending", "stranded content")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 1, f"expected 1 file recovered, got {recovered}"
    assert not orphan.exists(), "orphan .sending file should have been renamed away"
    restored = _results / "proactive-test-1.txt"
    assert restored.exists(), f"expected {restored} to exist post-recovery"
    assert restored.read_text() == "stranded content", "content must be preserved"


def test_telegram_bridge_recovers_orphan_sending_file():
    """Telegram bridge has the same recovery helper. Pin parity."""
    _clear_results()
    orphan = _make_orphan("proactive-tg-1.sending", "tg content")
    recovered = telegram_bridge._recover_orphan_sending_files()
    assert recovered == 1
    assert not orphan.exists()
    assert (_results / "proactive-tg-1.txt").exists()


def test_no_op_when_no_orphans():
    """Idempotency: a fresh start with no `.sending` files is a no-op."""
    _clear_results()
    _make_normal_txt("proactive-normal-1.txt")  # exists; not an orphan
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0
    assert (_results / "proactive-normal-1.txt").exists()  # untouched


def test_multiple_orphans_all_recovered():
    """Edge case: multiple orphans from a multi-file crash. All
    recovered in a single startup pass."""
    _clear_results()
    for i in range(5):
        _make_orphan(f"proactive-multi-{i}.sending", f"content-{i}")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 5
    for i in range(5):
        restored = _results / f"proactive-multi-{i}.txt"
        assert restored.exists(), f"missing {restored}"
        assert restored.read_text() == f"content-{i}"


def test_non_proactive_sending_files_ignored():
    """Defensive: only `proactive-*.sending` files are recovered.
    Other `.sending` patterns (if any are introduced later) are
    untouched until they're explicitly opted in."""
    _clear_results()
    other = _make_orphan("task-other.sending", "not-a-proactive")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0, "non-proactive .sending file should NOT be recovered"
    assert other.exists(), "non-proactive .sending must remain untouched"


def test_recovery_skips_when_txt_already_exists():
    """Collision guard: if a `.txt` with the same name somehow exists
    when we try to rename a `.sending` back, the rename is skipped
    (logged but not failed). Prevents overwriting newer content."""
    _clear_results()
    _make_orphan("proactive-collide.sending", "old stranded")
    _make_normal_txt("proactive-collide.txt", "newer txt")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0, "collision case should NOT count as recovered"
    # Both files still exist; the operator can inspect manually.
    assert (_results / "proactive-collide.sending").exists()
    assert (_results / "proactive-collide.txt").read_text() == "newer txt"


def test_recovery_idempotent():
    """Calling the recovery a second time is a clean no-op."""
    _clear_results()
    _make_orphan("proactive-idem.sending", "x")
    first = discord_bridge._recover_orphan_sending_files()
    second = discord_bridge._recover_orphan_sending_files()
    assert first == 1
    assert second == 0


def test_recovery_handles_missing_results_dir():
    """Defensive: if the workspace doesn't have a results/ dir yet
    (first ever run), recovery is a clean no-op, not an error."""
    # Temporarily remove the dir for this test.
    import shutil
    backup = Path(_WORKSPACE_TMP) / "results-backup"
    if _results.exists():
        shutil.move(_results, backup)
    try:
        recovered = discord_bridge._recover_orphan_sending_files()
        assert recovered == 0
    finally:
        if backup.exists():
            if _results.exists():
                shutil.rmtree(_results)
            shutil.move(backup, _results)


def main():
    failures = []
    for fn in (
        test_discord_bridge_recovers_orphan_sending_file,
        test_telegram_bridge_recovers_orphan_sending_file,
        test_no_op_when_no_orphans,
        test_multiple_orphans_all_recovered,
        test_non_proactive_sending_files_ignored,
        test_recovery_skips_when_txt_already_exists,
        test_recovery_idempotent,
        test_recovery_handles_missing_results_dir,
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
    print("All orphan-recovery tests passed.")


if __name__ == "__main__":
    main()
