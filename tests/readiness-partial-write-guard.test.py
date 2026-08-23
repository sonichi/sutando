#!/usr/bin/env python3
"""A body read mid-write must not raise (issue #3307).

`read_text()` decodes strictly, so a read landing between the bytes of a
multi-byte character raises `UnicodeDecodeError` — a `ValueError`, not an
`OSError`. Several sites read bodies while iterating the freshest files in
`results/`/`tasks/`, i.e. exactly the ones another process may still be
writing. Each must degrade (pending / empty / skip), never propagate.

Covers both shapes: unguarded reads, and a `try/except OSError` that does not
cover the decode error at all.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def torn(text: str) -> bytes:
    """`text` truncated inside a multi-byte character — a real partial write.

    Cutting on a continuation byte is what makes this decode-fatal; truncating
    on an ASCII boundary yields a short but valid string and proves nothing.
    """
    raw = text.encode()
    cut = next(i for i in range(len(raw) - 1, 0, -1) if (raw[i] & 0xC0) == 0x80)
    out = raw[:cut + 1] if (raw[cut - 1] & 0xC0) == 0xC0 else raw[:cut]
    try:
        out.decode()
    except UnicodeDecodeError:
        return out
    raise AssertionError("fixture is not torn — it still decodes")


BODY = "réply with an emoji ✅ and more téxt so the tail is multi-byte 🎉"

sys.path.insert(0, str(REPO / "src"))
api = _load("agent_api", REPO / "src" / "agent-api.py")

_t = torn(BODY)
check(len(_t) > 0, f"fixture is a non-empty torn body ({len(_t)} of {len(BODY.encode())} bytes)")


def _bind(root: Path):
    api.TASK_DIR = root / "tasks"
    api.RESULT_DIR = root / "results"
    api.TASK_DIR.mkdir(parents=True, exist_ok=True)
    api.RESULT_DIR.mkdir(parents=True, exist_ok=True)


def test_result_poll_degrades_to_pending():
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        (api.TASK_DIR / "task-1.txt").write_text("id: task-1\ntask: x\n")
        (api.RESULT_DIR / "task-1.txt").write_bytes(torn(BODY))
        try:
            r = api.get_task_result("task-1")
            check(r and r.get("status") == "pending",
                  f"/result on a torn body returns pending, got {r and r.get('status')!r}")
        except UnicodeDecodeError as e:
            check(False, f"/result on a torn body RAISED {type(e).__name__}")
        (api.RESULT_DIR / "task-1.txt").write_text(BODY)
        r = api.get_task_result("task-1")
        check(r and r.get("status") == "completed" and "emoji" in (r.get("result") or ""),
              "control: a complete body still returns completed with its text")


def test_archive_poll_degrades_to_pending():
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        arch = api.RESULT_DIR / "archive" / "2026-08"
        arch.mkdir(parents=True)
        (api.TASK_DIR / "task-2.txt").write_text("id: task-2\ntask: x\n")
        (arch / "task-2.txt").write_bytes(torn(BODY))
        try:
            r = api.get_task_result("task-2")
            check(r and r.get("status") == "pending",
                  f"/result archive leg on a torn body returns pending, got {r and r.get('status')!r}")
        except UnicodeDecodeError as e:
            check(False, f"/result archive leg RAISED {type(e).__name__}")


def test_display_fields_narrow_guard_covers_decode_error():
    """`except OSError` alone does not catch it — UnicodeDecodeError is a ValueError."""
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        (api.TASK_DIR / "task-3.txt").write_bytes(torn("task: " + BODY))
        try:
            got = api._task_display_fields_for_id("task-3")
            check(got == ("", ""), f"torn task file -> empty display fields, got {got!r}")
        except UnicodeDecodeError as e:
            check(False, f"_task_display_fields_for_id RAISED {type(e).__name__}")


def test_active_task_rows_survives_torn_bodies():
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        (api.RESULT_DIR / "archive").mkdir(parents=True, exist_ok=True)
        (api.TASK_DIR / "task-4.txt").write_bytes(torn("task: " + BODY))
        (api.RESULT_DIR / "task-4.txt").write_bytes(torn(BODY))
        try:
            rows = api._active_task_rows()
            check(isinstance(rows, list),
                  f"_active_task_rows survives torn task AND result bodies ({len(rows)} row(s))")
        except UnicodeDecodeError as e:
            check(False, f"_active_task_rows RAISED {type(e).__name__}")


def test_daily_insight_analysis_survives_a_torn_body():
    """The 50-freshest scan over results/ must count, not crash."""
    insight = _load("daily_insight", REPO / "src" / "daily-insight.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "results"
        root.mkdir(parents=True)
        insight.RESULTS_DIR = root
        (root / "task-a.txt").write_bytes(torn("delivered via discord " + BODY))
        (root / "task-b.txt").write_text("delivered via telegram")
        try:
            counts = insight.analyze_task_patterns()
            check(sum(counts.values()) == 2,
                  f"analysis counts both a torn and a clean body, got {dict(counts)}")
            check(counts.get("Telegram") == 1,
                  "control: the clean telegram body is still classified correctly")
        except UnicodeDecodeError as e:
            check(False, f"analyze_task_patterns RAISED {type(e).__name__}")


test_result_poll_degrades_to_pending()
test_archive_poll_degrades_to_pending()
test_daily_insight_analysis_survives_a_torn_body()
test_display_fields_narrow_guard_covers_decode_error()
test_active_task_rows_survives_torn_bodies()
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
