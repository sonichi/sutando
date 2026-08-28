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

        # Positive control: without it the torn case alone leaves the success
        # return unexercised, and "always pending" would pass.
        (arch / "task-2.txt").write_text(BODY)
        r = api.get_task_result("task-2")
        check(r and r.get("status") == "completed" and "emoji" in (r.get("result") or ""),
              "control: a complete ARCHIVED body still returns completed with its text")


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
        # Status and body must answer the SAME question. Existence-based `done`
        # plus readiness-based body reports done-with-an-empty-result.
        row = api.task_history.get("task-4", {})
        check(row.get("status") != "done",
              f"a torn result does not mark the row done (status={row.get('status')!r})")
        g = (api.get_task_result("task-4") or {}).get("status")
        check((row.get("status") == "done") == (g == "completed"),
              f"rows and /result agree on the same file (row={row.get('status')!r} /result={g!r})")


def test_task_envelope_census_survives_a_torn_body():
    """Census rglobs task-*.txt while a bridge writes them; `except OSError`
    alone does not cover the decode error."""
    census = _load("task_envelope_census", REPO / "src" / "task_envelope_census.py")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "tasks").mkdir(parents=True)
        (ws / "tasks" / "task-good.txt").write_text(
            "id: task-good\nsource: chat\ntask: hello\n")
        (ws / "tasks" / "task-torn.txt").write_bytes(
            torn("id: task-torn\nsource: chat\ntask: " + BODY))
        try:
            out = census.census(workspace=ws, days=3650.0)
            check(out.get("scanned", 0) >= 1,
                  f"census scans the clean task and skips the torn one (scanned={out.get('scanned')})")
        except UnicodeDecodeError as e:
            check(False, f"task-envelope census RAISED {type(e).__name__}")


def test_fully_archived_torn_result_is_pending_not_404():
    """Both task and result archived — normal post-delivery state — and the
    archived result torn. Returning None here becomes HTTP 404, which a client
    reads as terminal; `main` raised instead, which at least retries.

    The other archive test keeps a LIVE task file, so the `tasks/<id>.txt`
    fallback answers before this path is reached and masks it.
    """
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        res_arch = api.RESULT_DIR / "archive" / "2026-08"
        task_arch = api.TASK_DIR / "archive" / "2026-08"
        res_arch.mkdir(parents=True)
        task_arch.mkdir(parents=True)
        (task_arch / "task-9.txt").write_text("id: task-9\ntask: x\n")
        (res_arch / "task-9.txt").write_bytes(torn(BODY))
        check(not (api.TASK_DIR / "task-9.txt").exists(),
              "fixture: no LIVE task file, or the pending fallback masks this path")
        try:
            r = api.get_task_result("task-9")
            check(r is not None and r.get("status") == "pending",
                  f"fully-archived torn result is pending, not 404, got {r!r}")
        except UnicodeDecodeError as e:
            check(False, f"/result RAISED {type(e).__name__}")

        # Negative control: an id with nothing on disk must STILL be None (404).
        # Without this, a blanket `return pending` would pass the case above.
        check(api.get_task_result("task-does-not-exist") is None,
              "control: an unknown id still returns None so /result can 404")

        # Positive control: the same archived path returns the body once whole.
        (res_arch / "task-9.txt").write_text(BODY)
        r = api.get_task_result("task-9")
        check(r and r.get("status") == "completed" and "emoji" in (r.get("result") or ""),
              "control: the same fully-archived pair returns completed once readable")


def test_empty_and_whitespace_results_are_pending():
    """An empty or whitespace-only result is not-ready, never `completed` with
    an empty string — the wrong answer readiness.py exists to prevent.
    """
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        (api.TASK_DIR / "task-3.txt").write_text("id: task-3\ntask: x\n")
        for label, raw in (("empty", ""), ("whitespace", "   \n\t\n  ")):
            (api.RESULT_DIR / "task-3.txt").write_text(raw)
            r = api.get_task_result("task-3")
            check(r and r.get("status") == "pending",
                  f"a {label} result is pending, not a completed empty answer, got {r!r}")

        # Positive control: the same path completes once there is a real body.
        (api.RESULT_DIR / "task-3.txt").write_text(BODY)
        r = api.get_task_result("task-3")
        check(r and r.get("status") == "completed" and "emoji" in (r.get("result") or ""),
              "control: a real body on that same path returns completed")


OLD = "OLD ANSWER — superseded"
NEW = "NEW ANSWER — the one the client is waiting for"


def test_live_torn_never_falls_back_to_a_readable_archive():
    """@keweichen's repro: the newest body is mid-write and an OLDER archived
    body is readable. Answering `completed` with the archive is TERMINAL — the
    client stops polling and the new answer is stranded. `pending` is retryable.
    """
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        arch = api.RESULT_DIR / "archive" / "2026-08"
        arch.mkdir(parents=True)
        (arch / "task-9.txt").write_text(OLD)
        (api.RESULT_DIR / "task-9.txt").write_bytes(torn(NEW))
        r = api.get_task_result("task-9")
        check(r is not None and r.get("status") == "pending",
              f"live torn + readable archive -> pending, got {r!r}")
        check(OLD not in str(r), "the superseded archive body is NOT returned")
        # Positive control: once the live body lands whole, it wins over the archive.
        (api.RESULT_DIR / "task-9.txt").write_text(NEW)
        r = api.get_task_result("task-9")
        check(r and r.get("status") == "completed" and r.get("result") == NEW,
              f"the same path returns the NEW body once readable, got {r!r}")


def test_newest_archive_torn_never_falls_back_to_an_older_archive():
    """Same rule one tier down: archives are consulted newest-first, so a torn
    newest archive must not surface an older month's body as completed."""
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        old_m = api.RESULT_DIR / "archive" / "2026-07"
        new_m = api.RESULT_DIR / "archive" / "2026-08"
        old_m.mkdir(parents=True); new_m.mkdir(parents=True)
        (old_m / "task-8.txt").write_text(OLD)
        (new_m / "task-8.txt").write_bytes(torn(NEW))
        r = api.get_task_result("task-8")
        check(r is not None and r.get("status") == "pending",
              f"newest archive torn + older readable -> pending, got {r!r}")
        check(OLD not in str(r), "the older month's body is NOT returned")
        # Negative control: an id with nothing on disk must still be None (404),
        # so this is not a blanket `pending`.
        check(api.get_task_result("task-absent") is None,
              "control: an unknown id still returns None so /result can 404")


def test_active_rows_live_torn_does_not_report_a_cached_body():
    """The row and /result must answer the same question. A torn live body with
    a cached `done` row previously reported done-with-the-OLD-body while
    /result said pending — the inconsistency this PR exists to remove."""
    with tempfile.TemporaryDirectory() as td:
        _bind(Path(td))
        (api.RESULT_DIR / "archive").mkdir(parents=True, exist_ok=True)
        api.task_history.clear()
        (api.TASK_DIR / "task-7.txt").write_text("id: task-7\ntask: x\n")
        (api.RESULT_DIR / "task-7.txt").write_text(OLD)
        api._active_task_rows()
        check(api.task_history.get("task-7", {}).get("status") == "done",
              "fixture: the readable body first caches a done row")
        (api.RESULT_DIR / "task-7.txt").write_bytes(torn(NEW))
        api._active_task_rows()
        row = api.task_history.get("task-7", {})
        check(row.get("status") == "working",
              f"torn live body -> working, not the cached done (got {row.get('status')!r})")
        check(OLD not in str(row.get("result", "")),
              "the cached superseded body is NOT reported")
        api.task_history.clear()


test_result_poll_degrades_to_pending()
test_archive_poll_degrades_to_pending()
test_fully_archived_torn_result_is_pending_not_404()
test_empty_and_whitespace_results_are_pending()
test_display_fields_narrow_guard_covers_decode_error()
test_active_task_rows_survives_torn_bodies()
test_task_envelope_census_survives_a_torn_body()
test_live_torn_never_falls_back_to_a_readable_archive()
test_newest_archive_torn_never_falls_back_to_an_older_archive()
test_active_rows_live_torn_does_not_report_a_cached_body()
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
