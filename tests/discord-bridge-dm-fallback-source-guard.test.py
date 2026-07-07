#!/usr/bin/env python3
"""Regression guard: poll_dm_fallback forwards ONLY channel-less voice/phone
results to the owner's Discord DM — never results from sources that own their
own delivery path (api/chat, discord, telegram, slack).

## The bug

The web chat interface (agent-api.py) creates task files with `source: api`,
and the agent writes results to `results/task-<id>.txt`. poll_dm_fallback
scanned results/ and forwarded any untracked `task-*.txt` older than 90s to
the owner's Discord DM. Web-API tasks are never in `pending_replies`, so they
passed every guard and leaked local web-chat replies into the owner's DM.

## The fix (root cause, not a band-aid)

poll_dm_fallback's docstring already says it exists for "voice-originated or
cron-originated results" — but its *selection* was "any `task-*.txt`", and
`task-` is the universal result prefix every source uses. The fix aligns
selection with that documented purpose: an explicit POSITIVE allowlist,
``DM_FALLBACK_SOURCES = {"voice", "phone"}``. A `task-` result whose source is
anything else is skipped (left for its own consumer + the retention sweep).
The non-`task-` fallback prefixes (question-/briefing-/insight-/friction-) are
cron/proactive artifacts with no channel and stay eligible.

This is an allowlist, not the earlier rejected denylist of {api, chat}: a new
source is non-eligible by default and can never leak into DM unless it is
deliberately added to the allowlist.
"""

import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "discord-bridge.py").read_text()


def _extract(pattern: str) -> str:
    m = re.search(pattern, SRC, re.MULTILINE | re.DOTALL)
    assert m, f"Could not locate /{pattern}/ in discord-bridge.py"
    return m.group(0)


def _poll_dm_fallback_body() -> str:
    return _extract(r"async def poll_dm_fallback\(\):.*?(?=^(?:async )?def |\Z)")


def _build_helper_namespace(tmpdir: Path, task_files: dict):
    """exec DM_FALLBACK_SOURCES + _task_source in isolation with a stub
    find_task_file/TASKS_DIR, so the eligibility logic can be exercised
    without importing the whole discord-bridge module (heavy deps)."""
    for tid, body in task_files.items():
        (tmpdir / f"{tid}.txt").write_text(body)

    def stub_find_task_file(tasks_dir, task_id):
        p = Path(tasks_dir) / f"{task_id}.txt"
        return p if p.exists() else None

    const_src = _extract(r"DM_FALLBACK_SOURCES = \{[^}]*\}")
    func_src = _extract(r"def _task_source\(task_id: str\):.*?(?=^\n\n|\Z)")
    elig_src = _extract(r"def _dm_fallback_eligible\(task_id: str\).*?(?=^\n\n|\Z)")
    ns = {"find_task_file": stub_find_task_file, "TASKS_DIR": tmpdir, "Path": Path}
    exec(const_src + "\n\n" + func_src + "\n\n" + elig_src, ns)
    return ns


def _build_helper_namespace_with_archive(tmpdir: Path, active: dict, archived: dict):
    """Like _build_helper_namespace, but use the real helper's archive lookup
    shape: tasks/archive/YYYY-MM/<task_id>.txt."""
    for tid, body in active.items():
        (tmpdir / f"{tid}.txt").write_text(body)
    archive_month = tmpdir / "archive" / "2026-06"
    archive_month.mkdir(parents=True)
    for tid, body in archived.items():
        (archive_month / f"{tid}.txt").write_text(body)

    def stub_find_task_file(tasks_dir, task_id):
        p = Path(tasks_dir) / f"{task_id}.txt"
        return p if p.exists() else None

    const_src = _extract(r"DM_FALLBACK_SOURCES = \{[^}]*\}")
    func_src = _extract(r"def _task_source\(task_id: str\):.*?(?=^\n\n|\Z)")
    elig_src = _extract(r"def _dm_fallback_eligible\(task_id: str\).*?(?=^\n\n|\Z)")
    ns = {"find_task_file": stub_find_task_file, "TASKS_DIR": tmpdir, "Path": Path, "sorted": sorted}
    exec(const_src + "\n\n" + func_src + "\n\n" + elig_src, ns)
    return ns


# ---------------------------------------------------------------------------
# Static structure: the allowlist exists and is positive (voice/phone in,
# api/chat out), and the gate is wired into poll_dm_fallback.
# ---------------------------------------------------------------------------

def test_allowlist_is_positive_voice_phone_only():
    const_src = _extract(r"DM_FALLBACK_SOURCES = \{[^}]*\}")
    assert "voice" in const_src and "phone" in const_src, const_src
    # Must NOT be the old denylist of local sources.
    assert "api" not in const_src and "chat" not in const_src, (
        "DM_FALLBACK_SOURCES must be a positive allowlist of channel-less "
        f"sources, not a denylist: {const_src}"
    )


def test_gate_is_wired_into_poll_dm_fallback():
    body = _poll_dm_fallback_body()
    assert "_dm_fallback_eligible" in body, (
        "fallback must consult the shared eligibility decision"
    )
    assert 'task_id.startswith("task-")' in body, (
        "gate must scope to task- results so question-/briefing-/insight-/"
        "friction- cron artifacts stay eligible"
    )


def test_gate_precedes_grace_window():
    body = _poll_dm_fallback_body()
    gate = body.index("DM_FALLBACK_SOURCES")
    grace = body.index("age < GRACE_SECONDS")  # the grace *check*, not the const def
    assert gate < grace, "source gate must run before the grace/stat work"


# ---------------------------------------------------------------------------
# Functional: the eligibility decision behaves correctly per source.
# ---------------------------------------------------------------------------

def _eligible(ns, task_id) -> bool:
    """Execute the REAL production decision (_dm_fallback_eligible, extracted
    and exec'd from discord-bridge.py in the namespace) — not a replica, so a
    regression in the production function fails this test."""
    return ns["_dm_fallback_eligible"](task_id)


def test_voice_result_is_eligible():
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace(Path(d), {
            "task-1": "id: task-1\nsource: voice\ntask: hi\n",
        })
        assert ns["_task_source"]("task-1") == "voice"
        assert _eligible(ns, "task-1") is True


def test_api_and_chat_results_are_not_eligible():
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace(Path(d), {
            "task-api": "id: task-api\nsource: api\nfrom: web\n",
            "task-chat": "id: task-chat\nsource: chat\n",
        })
        assert _eligible(ns, "task-api") is False
        assert _eligible(ns, "task-chat") is False


def test_archived_chat_task_source_is_not_eligible():
    """Live failure caught during PR test: chat task files can be archived
    before dm-fallback sees the result. The archived source must still block
    Discord DM fallback."""
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace_with_archive(
            Path(d),
            active={},
            archived={"task-chat-archived": "id: task-chat-archived\nsource: chat\n"},
        )
        assert ns["_task_source"]("task-chat-archived") == "chat"
        assert _eligible(ns, "task-chat-archived") is False


def test_discord_and_telegram_results_are_not_eligible():
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace(Path(d), {
            "task-d": "id: task-d\nsource: discord\n",
            "task-t": "id: task-t\nsource: telegram\n",
        })
        assert _eligible(ns, "task-d") is False
        assert _eligible(ns, "task-t") is False


def test_missing_source_fails_closed_to_not_eligible():
    """FAIL-CLOSED (#1854 follow-up): no source field, or no task file at
    all → NOT eligible. The earlier fail-open posture let a result whose
    task file was swept/unreadable DM regardless of its true origin —
    that's the residual leak path the post-merge audit flagged."""
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace(Path(d), {
            "task-nosrc": "id: task-nosrc\ntask: legacy\n",
        })
        assert ns["_task_source"]("task-nosrc") is None
        assert _eligible(ns, "task-nosrc") is False
        # missing file entirely
        assert ns["_task_source"]("task-ghost") is None
        assert _eligible(ns, "task-ghost") is False


def test_source_match_is_case_insensitive():
    with tempfile.TemporaryDirectory() as d:
        ns = _build_helper_namespace(Path(d), {
            "task-v": "id: task-v\nsource: Voice\n",
        })
        assert ns["_task_source"]("task-v") == "voice"
        assert _eligible(ns, "task-v") is True


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
