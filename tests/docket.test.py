"""The docket: what it refuses, when an item is ready, and that a
reader never sees a half-written file.

Run: python3 tests/docket.test.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import docket as todo  # noqa: E402

PASS = 0
FAIL = []


def check(name, fn):
    global PASS
    try:
        fn()
        PASS += 1
        print(f"  ok  {name}")
    except AssertionError as exc:
        FAIL.append((name, str(exc)))
        print(f"FAIL  {name}: {exc}")


def good(**over):
    record = {
        "room_id": "!room:ag2.space",
        "surface": "markdown",
        "note": "## TODO → the segmented control line",
        "how_to_open": "room_collab.py --url https://chat.ag2.space read '!room:ag2.space'",
        "brief": "swap the three-state view toggle for a segmented control",
        "importance": "should",
        "urgency": "soon",
        "size": "M",
        "status": "approved",
    }
    record.update(over)
    return record


def test_refuses_an_unusable_record():
    problems = todo.validate({"room_id": "!r:x"})
    for field in ("note", "how_to_open", "brief", "importance", "urgency", "size"):
        assert any(field in p for p in problems), f"{field} not reported: {problems}"
    # Every problem at once, not one per attempt.
    assert len(problems) >= 6, problems
    assert todo.validate(good()) == []


def test_refuses_bad_values_and_a_multiline_brief():
    assert any("importance" in p for p in todo.validate(good(importance="critical")))
    assert any("urgency" in p for p in todo.validate(good(urgency="whenever-ish")))
    assert any("size" in p for p in todo.validate(good(size="huge")))
    assert any("one line" in p for p in todo.validate(good(brief="first\nsecond")))
    assert any("when must be" in p for p in todo.validate(good(when=["when i feel like it"])))
    assert any("due_at" in p for p in todo.validate(good(due_at="tomorrow")))


def test_the_lifecycle_is_checked_and_a_blocked_item_must_say_what_it_waits_on():
    assert any("status" in p for p in todo.validate(good(status="nearly-done")))
    blocked = todo.validate(good(status="blocked"))
    assert any("blocked_note" in p for p in blocked), blocked
    assert todo.validate(good(status="blocked", blocked_note="waiting on #4550 to land")) == []


def test_only_an_approved_item_is_picked_up(tmp):
    """The owner's lifecycle gates pickup: an agent may propose anything, but
    nothing is worked on before she approves it."""
    proposed = todo.add(good(status="proposed", when=[]), workspace=tmp, now=1)
    assert proposed["status"] == "proposed", "filing defaults to proposed, never approved"
    assert not todo.is_ready(proposed, 100, {}), "a proposed item must not be picked up"
    for held in ("blocked", "in_progress", "completion_declared", "confirmed"):
        row = good(status=held, when=[], blocked_note="x")
        assert not todo.is_ready(row, 100, {}), f"{held} must not be offered for pickup"
    assert todo.is_ready(good(status="approved", when=[]), 100, {})


def test_a_proposed_item_is_surfaced_to_the_owner_not_silently_held(tmp):
    """If `proposed` gated pickup with nothing showing her the queue, filing a
    TODO would be the same as dropping it."""
    mine = todo.add(good(brief="needs her nod", status="proposed"), workspace=tmp, now=1)
    todo.add(good(brief="already approved", status="approved"), workspace=tmp, now=2)
    declared = todo.add(good(brief="says it is done", status="completion_declared"), workspace=tmp, now=3)
    briefs = {r["brief"] for r in todo.awaiting_owner(tmp)}
    assert briefs == {"needs her nod", "says it is done"}, briefs
    assert mine["id"] in {r["id"] for r in todo.awaiting_owner(tmp)}
    assert declared["id"] in {r["id"] for r in todo.awaiting_owner(tmp)}


def test_set_status_moves_it_and_confirming_closes_it(tmp):
    row = todo.add(good(), workspace=tmp)
    assert todo.set_status(row["id"], "in_progress", workspace=tmp) is True
    assert todo.load(tmp)[0]["status"] == "in_progress"
    assert todo.set_status("todo-nope", "approved", workspace=tmp) is False
    try:
        todo.set_status(row["id"], "blocked", workspace=tmp)
        raise AssertionError("blocked with no note was accepted")
    except ValueError as exc:
        assert "note" in str(exc), exc
    assert todo.set_status(row["id"], "blocked", "waiting on review", workspace=tmp)
    assert todo.load(tmp)[0]["blocked_note"] == "waiting on review"
    # Confirming closes it, so status and state can never disagree.
    assert todo.set_status(row["id"], "confirmed", workspace=tmp)
    assert todo.load(tmp) == [], "a confirmed item is no longer open"
    assert todo.load(tmp, state="done")[0]["status"] == "confirmed"


def test_how_to_open_is_required_so_a_later_agent_can_find_the_doc():
    problems = todo.validate(good(how_to_open="   "))
    assert any("how_to_open" in p for p in problems), problems


def test_add_stores_and_refuses(tmp):
    row = todo.add(good(), workspace=tmp, now=1000.0)
    assert row["id"].startswith("todo-")
    assert row["state"] == "open" and row["created_at"] == 1000.0
    assert todo.load(tmp)[0]["brief"] == row["brief"]
    try:
        todo.add(good(brief=""), workspace=tmp)
        raise AssertionError("a record with no brief was stored")
    except ValueError as exc:
        assert "brief" in str(exc), exc
    assert len(todo.load(tmp)) == 1, "the refused record must not be in the store"


def test_a_worker_is_named_only_when_the_filer_names_one(tmp):
    """The owner's point: a room may host several workers later, so an explicit
    addressee has to be possible — but an absent one must make no claim rather
    than guess a seat the bindings would contradict."""
    plain = todo.add(good(), workspace=tmp)
    assert plain["assignee"] is None, plain
    assert plain["room_id"] == "!room:ag2.space"
    addressed = todo.add(good(assignee="c8138e81"), workspace=tmp)
    assert addressed["assignee"] == "c8138e81"
    assert any("assignee" in p for p in todo.validate(good(assignee="  "))), "blank is worse than absent"


def test_ready_for_a_worker_includes_the_unaddressed(tmp):
    mine = todo.add(good(brief="mine", assignee="c8138e81", when=[]), workspace=tmp, now=1)
    theirs = todo.add(good(brief="theirs", assignee="39041ce6", when=[]), workspace=tmp, now=2)
    loose = todo.add(good(brief="loose", when=[]), workspace=tmp, now=3)
    rows = todo.ready(workspace=tmp, now=10, signals={})
    ids = {r["id"] for r in rows}
    assert {mine["id"], theirs["id"], loose["id"]} <= ids, "ready() does not filter; the caller does"
    for_me = [r["brief"] for r in rows if r.get("assignee") in (None, "c8138e81")]
    assert sorted(for_me) == ["loose", "mine"], for_me


def test_credit_for_size_defers_the_big_item_not_the_small_one(tmp):
    """The point of a size: LIGHT credit should start a small item and hold a
    large one, which is what makes her "enough credit" condition mean anything."""
    small = good(size="S", when=["credit_for_size"])
    large = good(size="XL", when=["credit_for_size"])
    assert todo.conditions_met(small, {"tier": "LIGHT"})
    assert not todo.conditions_met(large, {"tier": "LIGHT"})
    assert todo.conditions_met(large, {"tier": "FULL"})
    assert not todo.conditions_met(good(size="M", when=["credit_for_size"]), {"tier": "MINIMAL"})
    # A missing tier must not stop everything: a lost signal is not a reason to stall.
    assert todo.conditions_met(large, {})


def test_the_note_is_built_so_a_filer_need_not_remember_it(tmp):
    once = todo.canonical_how_to_open("!r:x", "markdown", "once")
    hold = todo.canonical_how_to_open("!r:x", "markdown", "hold")
    assert "room-collab skill" in once and "room_collab.py" in once and "!r:x" in once
    assert "hold the connection open" in hold, hold
    assert todo.validate(good(how_to_open=once)) == []


def test_conditions_stack(tmp):
    record = good(when=["idle", "owner_away"])
    assert todo.conditions_met(record, {"idle": True, "owner_away": True})
    assert not todo.conditions_met(record, {"idle": True})
    assert not todo.conditions_met(record, {"idle": True, "owner_away": False})
    assert todo.conditions_met(good(when=[]), {}), "no conditions means nothing to wait for"


def test_ready_respects_floor_deadline_and_state(tmp):
    now = 2_000.0
    waiting = todo.add(good(when=["idle"], not_before=now + 60), workspace=tmp)
    assert not todo.is_ready(waiting, now, {"idle": True}), "not_before must hold it back"
    assert todo.is_ready(waiting, now + 61, {"idle": True})

    overdue = good(due_at=now - 1, when=["idle"])
    assert todo.is_ready(overdue, now, {"idle": False}), "a passed deadline overrides conditions"

    assert not todo.is_ready(good(state="done", when=[]), now, {}), "a closed item is never ready"


def test_ready_orders_the_most_pressing_first(tmp):
    now = 5_000.0
    todo.add(good(brief="nice one", importance="nice", urgency="whenever"), workspace=tmp, now=1)
    todo.add(good(brief="must one", importance="must", urgency="soon"), workspace=tmp, now=2)
    todo.add(good(brief="overdue one", importance="nice", due_at=now - 5), workspace=tmp, now=3)
    order = [r["brief"] for r in todo.ready(workspace=tmp, now=now, signals={})]
    assert order[0] == "overdue one", order
    assert order[1] == "must one", order


def test_close_marks_and_reports(tmp):
    row = todo.add(good(), workspace=tmp)
    assert todo.close(row["id"], "done", workspace=tmp) is True
    assert todo.load(tmp) == [], "a done item is not open any more"
    assert todo.load(tmp, state="done")[0]["id"] == row["id"]
    assert todo.close("todo-nope", "done", workspace=tmp) is False


def test_a_reader_never_sees_a_partial_file(tmp):
    """The store is read by the loop while agents file items; a truncated read
    would drop every TODO at once, so the write has to be atomic."""
    todo.add(good(), workspace=tmp)
    stop = threading.Event()
    seen = []

    def reader():
        while not stop.is_set():
            try:
                raw = todo.store_path(tmp).read_text(encoding="utf-8")
            except OSError:
                continue
            seen.append(json.loads(raw) if raw else {})

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for i in range(40):
        todo.add(good(brief=f"item {i}"), workspace=tmp)
    stop.set()
    t.join(timeout=5)
    assert seen, "the reader never got a look"
    assert all(isinstance(d.get("todos"), list) for d in seen), "a partial read got through"


def test_cli_refuses_then_files_and_lists(tmp):
    # --workspace, not an env var: $SUTANDO_WORKSPACE is not honoured any more,
    # and an earlier version of this test wrote its rows into the real workspace.
    script = str(ROOT / "src" / "docket.py")
    base = [sys.executable, script, "--workspace", str(tmp)]
    env = dict(os.environ)
    # Missing the discipline fields: argparse itself refuses, nothing is stored.
    bad = subprocess.run(
        base + ["add", "--room", "!r:x", "--brief", "no other fields"],
        capture_output=True, text=True, env=env,
    )
    assert bad.returncode != 0, bad.stdout
    ok = subprocess.run(
        base + [
            "add",
            "--room", "!r:x", "--note", "## TODO", "--how-to-open", "room_collab.py read '!r:x'",
            "--brief", "one line", "--importance", "must", "--urgency", "now", "--size", "S",
            "--when", "idle", "--status", "approved",
        ],
        capture_output=True, text=True, env=env,
    )
    assert ok.returncode == 0, ok.stderr
    row = json.loads(ok.stdout)
    assert row["when"] == ["idle"] and row["due_at"] is None

    listed = subprocess.run(base + ["list"], capture_output=True, text=True, env=env)
    assert row["id"] in listed.stdout, listed.stdout

    idle_ready = subprocess.run(
        base + ["ready", "--idle"], capture_output=True, text=True, env=env
    )
    assert row["id"] in idle_ready.stdout, "an idle TODO should be ready when idle"
    busy = subprocess.run(base + ["ready"], capture_output=True, text=True, env=env)
    assert row["id"] not in busy.stdout, "it should wait while not idle"

    done = subprocess.run(base + ["done", row["id"]], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert row["id"] not in subprocess.run(
        base + ["list"], capture_output=True, text=True, env=env
    ).stdout
    # A proposed item waits for her instead of being picked up.
    prop = subprocess.run(
        base + [
            "add", "--room", "!r:x", "--note", "## TODO", "--how-to-open", "read '!r:x'",
            "--brief", "needs a nod", "--importance", "nice", "--urgency", "soon", "--size", "S",
        ],
        capture_output=True, text=True, env=env,
    )
    assert prop.returncode == 0, prop.stderr
    pid = json.loads(prop.stdout)["id"]
    assert pid not in subprocess.run(
        base + ["ready", "--idle"], capture_output=True, text=True, env=env
    ).stdout, "a proposed item must not be ready"
    assert pid in subprocess.run(
        base + ["awaiting"], capture_output=True, text=True, env=env
    ).stdout, "a proposed item must be shown to the owner"
    moved = subprocess.run(
        base + ["status", pid, "blocked"], capture_output=True, text=True, env=env
    )
    assert moved.returncode == 2 and "note" in moved.stderr, moved.stderr

    # Everything this test wrote went to its own workspace.
    assert (tmp / "state" / "docket.json").exists()



def _run(args, box):
    """The CLI in this process: coverage cannot see a subprocess, and the real
    entry point is covered separately by the subprocess case."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = todo._cli(["--workspace", str(box)] + args)
        except SystemExit as exc:  # argparse refuses before we see the args
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


FILE_ARGS = [
    "add", "--room", "!r:x", "--note", "## TODO", "--how-to-open", "read '!r:x'",
    "--brief", "one line", "--importance", "must", "--urgency", "now", "--size", "S",
]


def test_the_cli_files_moves_and_closes_in_process(tmp):
    rc, out, _ = _run(FILE_ARGS + ["--when", "idle", "--status", "approved"], tmp)
    assert rc == 0, out
    row = json.loads(out)

    rc, out, _ = _run(["list"], tmp)
    assert rc == 0 and row["id"] in out
    rc, out, _ = _run(["list", "--status", "approved"], tmp)
    assert row["id"] in out
    rc, out, _ = _run(["list", "--status", "blocked"], tmp)
    assert row["id"] not in out, "the filter must actually filter"

    rc, out, _ = _run(["ready", "--idle"], tmp)
    assert row["id"] in out, "an approved idle item is ready when idle"
    rc, out, _ = _run(["ready"], tmp)
    assert row["id"] not in out, "it waits while not idle"
    rc, out, _ = _run(["ready", "--idle", "--owner-away", "--tier", "LIGHT", "--for", "c8138e81"], tmp)
    assert row["id"] in out, "unaddressed items belong to whoever asks"

    rc, _, err = _run(["status", row["id"], "blocked"], tmp)
    assert rc == 2 and "note" in err, err
    rc, _, err = _run(["status", row["id"], "blocked", "--note", "waiting on review"], tmp)
    assert rc == 0 and "moved" in err
    assert todo.load(tmp)[0]["blocked_note"] == "waiting on review"
    rc, _, err = _run(["status", "todo-nope", "approved"], tmp)
    assert rc == 1 and "no such id" in err

    rc, out, _ = _run(["awaiting"], tmp)
    assert row["id"] not in out, "a blocked item is not waiting on her"
    _run(["status", row["id"], "completion_declared"], tmp)
    rc, out, _ = _run(["awaiting"], tmp)
    assert row["id"] in out, "a declared-complete item needs her confirmation"

    rc, _, err = _run(["done", row["id"]], tmp)
    assert rc == 0 and "closed" in err
    rc, _, err = _run(["done", row["id"]], tmp)
    assert rc == 1, "already closed is not open to close again"


def test_the_cli_refuses_a_bad_file_and_cancels(tmp):
    rc, _, err = _run(["add", "--room", "!r:x", "--brief", "no other fields"], tmp)
    assert rc != 0, "argparse must refuse a file missing its discipline fields"

    rc, out, _ = _run(FILE_ARGS + ["--context", "see the thread", "--assignee", "c8138e81",
                                   "--due-at", "1789999999", "--not-before", "1"], tmp)
    assert rc == 0
    row = json.loads(out)
    assert row["assignee"] == "c8138e81" and row["context"] == "see the thread"
    assert row["due_at"] == 1789999999.0 and row["status"] == "proposed"

    rc, _, err = _run(["cancel", row["id"]], tmp)
    assert rc == 0 and "closed" in err
    assert todo.load(tmp, state="cancelled")[0]["id"] == row["id"]
    rc, _, err = _run(["done", row["id"]], tmp)
    assert rc == 1, "done must not quietly overwrite a cancellation"
    assert todo.load(tmp, state="cancelled")[0]["id"] == row["id"]

    rc, out, _ = _run(FILE_ARGS + ["--status", "blocked"], tmp)
    assert rc == 2, "a blocked file with nothing to wait on is refused"

    # Filing something already blocked is legitimate when the reason is given.
    rc, out, _ = _run(FILE_ARGS + ["--status", "blocked", "--blocked-note", "needs her API key"], tmp)
    assert rc == 0, out
    filed = json.loads(out)
    assert filed["blocked_note"] == "needs her API key"
    assert not todo.is_ready(filed, 10**10, {"idle": True}), "blocked is never picked up"


def test_the_guards_that_raise_rather_than_report(tmp):
    assert any("state" in p for p in todo.validate(good(state="halfway")))
    assert any("blocked_note is free text" in p for p in todo.validate(good(blocked_note=7)))
    for call, bad in ((todo.close, "state"), (todo.set_status, "status")):
        try:
            call("todo-x", "nonsense", workspace=tmp)
            raise AssertionError(f"{bad} was not checked")
        except ValueError as exc:
            assert bad in str(exc), exc


def main():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "state").mkdir()
        check("an unusable record is refused, with every problem at once", test_refuses_an_unusable_record)
        check("bad values and a multi-line brief are refused", test_refuses_bad_values_and_a_multiline_brief)
        check("how_to_open is required", test_how_to_open_is_required_so_a_later_agent_can_find_the_doc)
        check("the lifecycle is checked and a blocked item says what it waits on",
              test_the_lifecycle_is_checked_and_a_blocked_item_must_say_what_it_waits_on)
        for name, fn in (
            ("add stores a good record and refuses a sloppy one", test_add_stores_and_refuses),
            ("a worker is named only when the filer names one", test_a_worker_is_named_only_when_the_filer_names_one),
            ("ready for a worker includes the unaddressed ones", test_ready_for_a_worker_includes_the_unaddressed),
            ("only an approved item is picked up", test_only_an_approved_item_is_picked_up),
            ("a proposed item is surfaced to the owner, not silently held",
             test_a_proposed_item_is_surfaced_to_the_owner_not_silently_held),
            ("set_status moves it, and confirming closes it", test_set_status_moves_it_and_confirming_closes_it),
            ("conditions stack (AND), not any-of", test_conditions_stack),
            ("credit_for_size defers a big item on light credit", test_credit_for_size_defers_the_big_item_not_the_small_one),
            ("the canonical how-to-open note is built for the filer", test_the_note_is_built_so_a_filer_need_not_remember_it),
            ("ready respects not_before, due_at and state", test_ready_respects_floor_deadline_and_state),
            ("ready puts the most pressing first", test_ready_orders_the_most_pressing_first),
            ("close marks it and says whether it was there", test_close_marks_and_reports),
            ("a concurrent reader never sees a partial file", test_a_reader_never_sees_a_partial_file),
            ("the CLI refuses an incomplete file, then files, lists and closes", test_cli_refuses_then_files_and_lists),
            ("the CLI files, moves and closes (in process)", test_the_cli_files_moves_and_closes_in_process),
            ("the CLI refuses a bad file and cancels", test_the_cli_refuses_a_bad_file_and_cancels),
            ("the guards that raise rather than report", test_the_guards_that_raise_rather_than_report),
        ):
            with tempfile.TemporaryDirectory() as fresh:
                box = Path(fresh)
                (box / "state").mkdir()
                check(name, lambda fn=fn, box=box: fn(box))
    print(f"\ndocket: {PASS} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
