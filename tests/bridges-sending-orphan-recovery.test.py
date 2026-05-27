#!/usr/bin/env python3
"""Regression guard for restart-safety #1: orphan `.sending` file recovery.

Both bridges atomic-claim `results/proactive-*.txt` files via
`rename(proactive-X.txt → proactive-X.sending)` before sending. If the
bridge crashes BETWEEN the rename and the delivery, the `.sending` file
sits orphaned in `results/` — no poll iteration ever looks at `.sending`
suffixes. The owner notification is silently dropped.

The fix adds a startup sweep in both bridges.

Run: python3 tests/bridges-sending-orphan-recovery.test.py
Exit: 0 on pass, non-zero on fail.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

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
    for p in _results.iterdir():
        if p.is_file():
            p.unlink()


def _make_orphan(name: str, content: str = "fake-proactive") -> Path:
    p = _results / name
    p.write_text(content)
    return p


def _make_normal_txt(name: str, content: str = "fake-proactive") -> Path:
    p = _results / name
    p.write_text(content)
    return p


def test_discord_bridge_recovers_orphan_sending_file():
    _clear_results()
    orphan = _make_orphan("proactive-test-1.sending", "stranded content")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 1, f"expected 1 file recovered, got {recovered}"
    assert not orphan.exists(), "orphan .sending file should have been renamed away"
    restored = _results / "proactive-test-1.txt"
    assert restored.exists(), f"expected {restored} to exist post-recovery"
    assert restored.read_text() == "stranded content", "content must be preserved"


def test_telegram_bridge_recovers_orphan_sending_file():
    _clear_results()
    orphan = _make_orphan("proactive-tg-1.sending", "tg content")
    recovered = telegram_bridge._recover_orphan_sending_files()
    assert recovered == 1
    assert not orphan.exists()
    assert (_results / "proactive-tg-1.txt").exists()


def test_no_op_when_no_orphans():
    _clear_results()
    _make_normal_txt("proactive-normal-1.txt")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0
    assert (_results / "proactive-normal-1.txt").exists()


def test_multiple_orphans_all_recovered():
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
    _clear_results()
    other = _make_orphan("task-other.sending", "not-a-proactive")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0, "non-proactive .sending file should NOT be recovered"
    assert other.exists(), "non-proactive .sending must remain untouched"


def test_recovery_skips_when_txt_already_exists():
    _clear_results()
    _make_orphan("proactive-collide.sending", "old stranded")
    _make_normal_txt("proactive-collide.txt", "newer txt")
    recovered = discord_bridge._recover_orphan_sending_files()
    assert recovered == 0, "collision case should NOT count as recovered"
    assert (_results / "proactive-collide.sending").exists()
    assert (_results / "proactive-collide.txt").read_text() == "newer txt"


def test_recovery_idempotent():
    _clear_results()
    _make_orphan("proactive-idem.sending", "x")
    first = discord_bridge._recover_orphan_sending_files()
    second = discord_bridge._recover_orphan_sending_files()
    assert first == 1
    assert second == 0


def test_recovery_handles_missing_results_dir():
    import shutil
    backup = Path(_WORKSPACE_TMP) / "results-backup"
    if _results.exists():
        shutil.move(str(_results), str(backup))
    try:
        recovered = discord_bridge._recover_orphan_sending_files()
        assert recovered == 0
    finally:
        if backup.exists():
            if _results.exists():
                shutil.rmtree(str(_results))
            shutil.move(str(backup), str(_results))


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
            print(f"  ✗ {fn.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s).")
        sys.exit(1)
    print("All orphan-recovery tests passed.")


if __name__ == "__main__":
    main()
