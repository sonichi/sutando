#!/usr/bin/env python3
"""_recover_orphaned_task_routing must survive a torn task file.

The read is a decode. A UnicodeDecodeError is a ValueError, so it escapes the
`except OSError` and propagates out of the function, out of
_gather_pending_task_ids, out of main()'s tick loop, and out of a bare
`main()` — one torn file exits the bridge. And recovery runs immediately after
the crash that produced the tear, so it is the likeliest caller to meet one.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Isolate the channel root BEFORE exec_module: the bridge resolves ACCESS_FILE
# at module level, so unset it reads the operator's real per-user allowlist.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-torn-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": ["4242"]}')
# TOKEN is resolved at module level too, and the isolated dir has no .env — the
# bridge prints and exits at import without this, so isolation alone is not enough.
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"

_spec = importlib.util.spec_from_file_location("tg", REPO / "src" / "telegram-bridge.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def torn(text: str) -> bytes:
    """`text` truncated inside a multi-byte character — decode-fatal."""
    raw = text.encode()
    cut = next(i for i in range(len(raw) - 1, 0, -1) if (raw[i] & 0xC0) == 0x80)
    out = raw[:cut + 1] if (raw[cut - 1] & 0xC0) == 0xC0 else raw[:cut]
    try:
        out.decode()
    except UnicodeDecodeError:
        return out
    raise AssertionError("fixture is not torn — it still decodes")


HEAD = "id: {tid}\nsource: telegram\nchat_id: {chat}\ntask: réply ✅ with an emoji 🎉\n"


def test_torn_task_does_not_kill_recovery():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        tasks, results = ws / "tasks", ws / "results"
        tasks.mkdir(); results.mkdir()
        # one torn, one clean — the clean one must still be recovered
        (tasks / "task-TORN.txt").write_bytes(torn(HEAD.format(tid="TORN", chat="111")))
        (tasks / "task-OK.txt").write_text(HEAD.format(tid="OK", chat="222"))
        for t in ("TORN", "OK"):
            (results / f"task-{t}.txt").write_text("done\n")
        raw = (tasks / "task-TORN.txt").read_bytes()
        try:
            raw.decode()
            check(False, "fixture is not torn — it decodes, so this proves nothing")
            return
        except UnicodeDecodeError:
            check(True, f"fixture: task-TORN is undecodable ({len(raw)} bytes)")
        try:
            got = tg._recover_orphaned_task_routing(results, tasks, set())
        except UnicodeDecodeError:
            check(False, "recovery RAISED UnicodeDecodeError — one torn file exits the bridge")
            return
        check(got.get("task-OK") == 222,
              f"the clean task is still recovered alongside a torn one, got {got!r}")
        # The headers sit above the tear, so the replacement decode recovers
        # this file too; a mode that skipped it keeps the assert above green.
        check(got.get("task-TORN") == 111,
              f"the torn task's own routing survives the tear, got {got!r}")


test_torn_task_does_not_kill_recovery()
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
