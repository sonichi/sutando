#!/usr/bin/env python3
"""Unit tests for the dashboard Schedules card (PR #2008).

Covers the cron-schedule surface added to ``src/dashboard.py``:
  - ``_cron_field_match`` — one cron field vs a spec (``*``, ``*/N``, ``A-B``,
    ``A,B``, ``N``, and the malformed-token skip paths).
  - ``_cron_next_run`` — next matching datetime (a match, a wrong-arity expr,
    and an expr with no match inside the horizon).
  - ``get_schedules`` — reads ``<workspace>/hosts/<host>/crons.json`` and formats
    each job (all four ``next``-string buckets + the three ``desc`` sources +
    the missing-file / bad-json guards).
  - the Schedules block inside ``render_dashboard`` — driven with the heavy
    ``get_*`` helpers stubbed so it runs identically on any host/CI.

Standalone script (``python3 tests/dashboard-schedules.test.py``), matching the
repo's ``tests/**/*.test.py`` convention.
"""

import json
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import dashboard  # noqa: E402


# --------------------------------------------------------------------------- #
# _cron_field_match
# --------------------------------------------------------------------------- #
def test_cron_field_match_wildcard():
    assert dashboard._cron_field_match("*", 0) is True
    assert dashboard._cron_field_match("*", 59) is True


def test_cron_field_match_step():
    assert dashboard._cron_field_match("*/5", 10) is True   # 10 % 5 == 0
    assert dashboard._cron_field_match("*/5", 3) is False   # 3 % 5 != 0
    assert dashboard._cron_field_match("*/0", 4) is False   # step 0 → no match
    assert dashboard._cron_field_match("*/x", 4) is False   # non-int step → skip


def test_cron_field_match_range():
    assert dashboard._cron_field_match("1-5", 3) is True
    assert dashboard._cron_field_match("1-5", 9) is False
    assert dashboard._cron_field_match("a-b", 3) is False   # non-int range → skip


def test_cron_field_match_list_and_single():
    assert dashboard._cron_field_match("1,2,3", 2) is True
    assert dashboard._cron_field_match("7", 7) is True
    assert dashboard._cron_field_match("7", 8) is False
    assert dashboard._cron_field_match("9,10-12", 11) is True  # list + range mix


# --------------------------------------------------------------------------- #
# _cron_next_run
# --------------------------------------------------------------------------- #
def test_cron_next_run_matches():
    now = datetime(2026, 7, 7, 12, 0, 0)
    nxt = dashboard._cron_next_run("* * * * *", now)
    assert nxt == now + timedelta(minutes=1)


def test_cron_next_run_wrong_arity():
    assert dashboard._cron_next_run("* * * *", datetime.now()) is None
    assert dashboard._cron_next_run("", datetime.now()) is None


def test_cron_next_run_no_match_in_horizon():
    # Feb 30 never exists → scans the whole horizon and returns None.
    assert dashboard._cron_next_run("0 0 30 2 *", datetime(2026, 7, 7, 12, 0)) is None


# --------------------------------------------------------------------------- #
# get_schedules
# --------------------------------------------------------------------------- #
def _with_crons(tmp: Path, jobs):
    # get_schedules resolves the file via _crons_path() (canonical host-label,
    # not bare socket.gethostname) as of the editable-schedules change. Point
    # _crons_path at a temp file directly.
    cf = tmp / "crons.json"
    cf.write_text(json.dumps(jobs))
    dashboard._crons_path = lambda: cf
    return dashboard.get_schedules()


def _domain_rows(tmp: Path, jobs):
    """Call the DOMAIN function, not the dashboard adapter: `prompt_or_skill`
    exists only here, so an adapter-shaped test cannot observe it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dsched", REPO / "src" / "dashboard_schedules.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    cf = tmp / "crons.json"
    cf.write_text(json.dumps(jobs))
    return {r["name"]: r for r in m.list_schedules(cf)}


def test_domain_prompt_or_skill_carries_the_shell_command():
    """Regression pin. Reverting `prompt_or_skill` to the old `skill or prompt`
    fallback survived every adapter-level test; only this observes the field."""
    with tempfile.TemporaryDirectory() as td:
        rows = _domain_rows(Path(td), [
            {"name": "mixed", "cron": "0 9 * * *", "launchd": True,
             "shell_command": "echo hi", "prompt_skill": "fallback"},
            {"name": "blankshell", "cron": "0 9 * * *", "launchd": True,
             "shell_command": "   ", "prompt_skill": "fallback"},
            {"name": "intshell", "cron": "0 9 * * *", "launchd": True,
             "shell_command": 123, "prompt_skill": "fallback"},
        ])
    assert rows["mixed"]["prompt_or_skill"] == "echo hi", rows["mixed"]
    assert rows["mixed"]["kind"] == "shell"
    for nm in ("blankshell", "intshell"):
        assert rows[nm]["kind"] == "malformed", rows[nm]
        assert rows[nm]["prompt_or_skill"] == "", rows[nm]
        assert "fallback" not in rows[nm]["description"], rows[nm]


def test_shell_on_a_non_shelling_owner_is_terminal():
    """Only launchd shells out (cron-runner handles `launchd: true` only), so a
    session/codex entry naming a shell command must not advertise one."""
    with tempfile.TemporaryDirectory() as td:
        rows = _domain_rows(Path(td), [
            {"name": "sess", "cron": "0 9 * * *",
             "shell_command": "echo hi", "prompt_skill": "fallback"},
            {"name": "cdx", "cron": "0 9 * * *", "execution": "codex-task",
             "shell_command": "echo hi", "prompt_skill": "fallback"},
            {"name": "lnch", "cron": "0 9 * * *", "launchd": True,
             "shell_command": "echo hi", "prompt_skill": "fallback"},
        ])
    for nm in ("sess", "cdx"):
        assert rows[nm]["kind"] == "malformed", rows[nm]
        assert "fallback" not in rows[nm]["description"], rows[nm]
        assert rows[nm]["prompt_or_skill"] == "", rows[nm]
    assert rows["lnch"]["kind"] == "shell", rows["lnch"]
    assert rows["lnch"]["prompt_or_skill"] == "echo hi", rows["lnch"]


def test_get_schedules_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        dashboard._crons_path = lambda: Path(td) / "crons.json"  # missing
        assert dashboard.get_schedules() == []


def test_get_schedules_bad_json_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        cf = Path(td) / "crons.json"
        cf.write_text("{not valid json")
        dashboard._crons_path = lambda: cf
        assert dashboard.get_schedules() == []


def test_get_schedules_formats_all_branches():
    now = datetime.now()
    soon = now + timedelta(minutes=30)          # < 60m  → "in Xm"
    hours = now + timedelta(hours=3)            # < 1440m → "in HhMMm"
    days = now + timedelta(days=2)              # >= 1440m → "in DdHh"
    jobs = [
        # prompt_skill → kind "skill:...", desc "Runs the /... skill"
        {"name": "loop", "cron": "* * * * *", "prompt_skill": "proactive-loop"},
        # explicit description wins; hour-bucket next-run
        {"name": "brief", "cron": f"{hours.minute} {hours.hour} * * *",
         "description": "Daily briefing"},
        # day-bucket next-run
        {"name": "weekly", "cron": f"{days.minute} {days.hour} {days.day} * *",
         "prompt": "do weekly things"},
        # minute-bucket + prompt-derived desc with Run: prefix, HTML chars, truncation
        {"name": "sync", "cron": f"{soon.minute} {soon.hour} * * *",
         "prompt": "Run: sync & flush <everything> " + "x" * 120},
        # mechanical launchd job → shell kind and command-derived desc
        {"name": "poll", "cron": f"{soon.minute} {soon.hour} * * *",
         "launchd": True, "shell_command": "bash scripts/poll.sh"},
        # MIXED form: the runner executes shell_command, so every rendered field
        # must describe the shell, not the skill that never runs.
        {"name": "mixed", "cron": f"{soon.minute} {soon.hour} * * *",
         "launchd": True, "shell_command": "echo hi", "prompt_skill": "fallback"},
        # Shell key PRESENT but unusable: the runner skips the entry outright,
        # so no surface may advertise the fallback skill.
        {"name": "blankshell", "cron": f"{soon.minute} {soon.hour} * * *",
         "launchd": True, "shell_command": "   ", "prompt_skill": "fallback"},
        {"name": "intshell", "cron": f"{soon.minute} {soon.hour} * * *",
         "launchd": True, "shell_command": 123, "prompt_skill": "fallback"},
        # valid expr but no match in horizon → ">7d"
        {"name": "leap", "cron": "0 0 30 2 *"},
        # no cron → "invalid"; no name → "?"
        {"prompt": "no schedule"},
    ]
    with tempfile.TemporaryDirectory() as td:
        out = _with_crons(Path(td), jobs)

    by_name = {r["name"]: r for r in out}
    assert by_name["loop"]["kind"] == "skill:proactive-loop", f'got {by_name["loop"]["kind"]!r}'
    assert by_name["loop"]["desc"] == "Runs the /proactive-loop skill"
    assert by_name["brief"]["desc"] == "Daily briefing"
    assert by_name["brief"]["kind"] == "prompt"
    assert by_name["poll"]["kind"] == "shell", f'shell job rendered as {by_name["poll"]["kind"]!r}'
    assert by_name["poll"]["desc"] == "Runs shell command: bash scripts/poll.sh", \
        f'shell desc was {by_name["poll"]["desc"]!r}'

    # A mixed record must not advertise the skill launchd will never run.
    mixed = by_name["mixed"]
    assert mixed["kind"] == "shell", f'mixed rendered as {mixed["kind"]!r}'
    assert mixed["desc"] == "Runs shell command: echo hi", \
        f'mixed desc named the un-run skill: {mixed["desc"]!r}'
    assert "fallback" not in mixed["desc"], f'skill leaked into desc: {mixed["desc"]!r}'

    # A blank or non-string shell key must not fall through to the skill leg,
    # and a non-string must not raise on either listing surface.
    for nm in ("blankshell", "intshell"):
        r = by_name[nm]
        assert r["kind"].startswith("malformed"), f'{nm} rendered as {r["kind"]!r}'
        assert "fallback" not in r["desc"], f'{nm} advertises the un-run skill: {r["desc"]!r}'

    # next-string buckets
    assert by_name["loop"]["next"].endswith("(in 0m)") or "in " in by_name["loop"]["next"]
    assert "h" in by_name["brief"]["next"]        # HhMMm bucket
    assert "d" in by_name["weekly"]["next"]       # DdHh bucket
    assert by_name["leap"]["next"] == ">7d"
    assert by_name["?"]["next"] == "invalid"

    # prompt-derived desc: "Run:" stripped, &/< escaped, truncated with ellipsis
    sync_desc = by_name["sync"]["desc"]
    assert not sync_desc.startswith("Run:")
    assert "&amp;" in sync_desc and "&lt;" in sync_desc
    assert sync_desc.endswith("…")


# --------------------------------------------------------------------------- #
# render_dashboard — Schedules block
# --------------------------------------------------------------------------- #
def _stub_render_deps():
    """Stub the heavy get_* helpers so render_dashboard runs deterministically."""
    dashboard.get_health = lambda *a, **k: []
    dashboard.get_activity = lambda *a, **k: []
    dashboard.get_pending_count = lambda *a, **k: {"open": 0}
    dashboard.get_score = lambda *a, **k: 50
    dashboard.get_system_stats = lambda *a, **k: {
        "charging": False, "disk_free": "10G", "battery": "100%",
        "quota": {"available": True, "utilization_5h": 0, "utilization_7d": 0,
                  "reset_5h": "?", "reset_7d": "?", "headers": {}},
    }
    dashboard.get_use_case_matrix = lambda *a, **k: ""
    dashboard.get_outbox = lambda *a, **k: []
    # Avoid depending on a real `pgrep` binary in the render path.
    dashboard.subprocess = types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(returncode=1),
        PIPE=-1,
    )


def test_render_dashboard_includes_schedules_block():
    _stub_render_deps()
    dashboard.get_schedules = lambda: [
        {"name": "job1", "desc": "does a thing", "cron": "* * * * *",
         "kind": "skill:proactive-loop", "next": "Mon 09:00 (in 5m)"},
    ]
    html = dashboard.render_dashboard()
    assert "<h2>Schedules</h2>" in html
    assert "job1" in html and "does a thing" in html
    assert "1 active." in html


def test_render_dashboard_shows_add_form_when_none():
    # The Schedules card now renders even with 0 jobs so the Add row is
    # reachable (editable-schedules change). Assert the card + add form show,
    # and no job rows.
    _stub_render_deps()
    dashboard.get_schedules = lambda: []
    html = dashboard.render_dashboard()
    assert "<h2>Schedules</h2>" in html
    assert 'id="ns-name"' in html and 'onclick="addCron()"' in html
    assert 'data-name=' not in html  # no job rows


def main():
    tests = [
        test_cron_field_match_wildcard,
        test_cron_field_match_step,
        test_cron_field_match_range,
        test_cron_field_match_list_and_single,
        test_cron_next_run_matches,
        test_cron_next_run_wrong_arity,
        test_cron_next_run_no_match_in_horizon,
        test_domain_prompt_or_skill_carries_the_shell_command,
        test_shell_on_a_non_shelling_owner_is_terminal,
        test_get_schedules_missing_file_returns_empty,
        test_get_schedules_bad_json_returns_empty,
        test_get_schedules_formats_all_branches,
        test_render_dashboard_includes_schedules_block,
        test_render_dashboard_shows_add_form_when_none,
    ]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All dashboard-schedules tests passed.")


if __name__ == "__main__":
    main()
