#!/usr/bin/env python3
"""A Team-tier suppression is journaled and closed silently, not posted as prose.

The guard withholds `[no-send]` on a guarded tier so a sender cannot close its
own delivery lease silently -- the record requirement is the policy. It used to
discharge that by posting TEAM_SUPPRESS_RESULT, which on a human channel is
marker mechanics addressed to someone who has never heard of a marker.

An adapter that can journal now binds `suppress_journal=(state_dir, task_id)`:
the record lands on disk and the body becomes `[no-send]`, which the bridge
already routes to its silent-archive path. Adapters that omit it are unchanged.

  a) journal bound     -> body is [no-send], record written, original body kept
  b) journal omitted   -> the notice still stands (ag2space/gateway unchanged)
  c) owner tier        -> never touched
  d) journal UNWRITABLE -> notice stands (fail-closed; record never dropped)
  e) redirect/attach   -> still LEAK, never silently closed
  f) already-silent suppress -> not re-journaled, body unchanged

Run: python3 tests/team-guard-suppress-journaled-silent-close.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import policy.egress.result as guard  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    body = "[no-send]\nAddressed to another agent; nothing owed here."

    # a) THE FIX.
    state = pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-"))
    got, reason = guard.guard_result_for_tier(
        body, "team", REPO, suppress_journal=(state, "task-42"))
    check(got == "[no-send]", f"a) body is [no-send], got {got!r}")
    check(guard.TEAM_SUPPRESS_RESULT not in got, "a) the notice is NOT in the body")
    rec = guard.suppressed_record_path(state, "task-42")
    check(rec.is_file(), f"a) journal record written at {rec.name}")
    if rec.is_file():
        payload = json.loads(rec.read_text())
        check(payload["suppressed_body"] == body, "a) the ORIGINAL body is preserved in the record")
        check(payload["task_id"] == "task-42", "a) record carries the task id")
        check(payload["status"] == "suppressed_silent_close", "a) record states the disposition")
        check(oct(rec.parent.stat().st_mode)[-3:] == "700", "a) journal dir is owner-only")
    check(reason and "journaled silent close" in reason, f"a) reason names it, got {reason!r}")

    # b) An adapter that does not bind the journal is UNCHANGED.
    got_b, _ = guard.guard_result_for_tier(body, "team", REPO)
    check(got_b == guard.TEAM_SUPPRESS_RESULT,
          "b) without a journal the notice still stands (gateway unchanged)")

    # c) Owner is exempt end to end.
    got_c, reason_c = guard.guard_result_for_tier(
        body, "owner", REPO, suppress_journal=(state, "task-owner"))
    check(got_c == body and reason_c is None, "c) owner body delivered verbatim")
    check(not guard.suppressed_record_path(state, "task-owner").exists(),
          "c) and nothing is journaled for owner")

    # d) FAIL-CLOSED. A journal that cannot be written must not lose the record.
    blocked = pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-ro-"))
    (blocked / guard.SUPPRESSED_RESULT_DIR).write_text("not a directory", encoding="utf-8")
    got_d, _ = guard.guard_result_for_tier(
        body, "team", REPO, suppress_journal=(blocked, "task-99"))
    check(got_d == guard.TEAM_SUPPRESS_RESULT,
          f"d) unwritable journal -> notice STANDS, got {got_d[:40]!r}")

    # e) The capability markers are a different class and must not be softened.
    for marker in ("[channel: 1530802402603700415]\nhi", "[file: /etc/passwd]\nhi"):
        got_e, _ = guard.guard_result_for_tier(
            marker, "team", REPO, suppress_journal=(state, "task-leak"))
        check(got_e == guard.TEAM_LEAK_RESULT,
              f"e) {marker.split(chr(10))[0]} still LEAK, got {got_e[:34]!r}")
    check(not guard.suppressed_record_path(state, "task-leak").exists(),
          "e) and a leak is never journaled as a silent close")

    # f) An already-silent suppress is returned untouched.
    silent = guard.TeamResultVerdict(guard.VERDICT_SUPPRESS, "[no-send]", "prior")
    out = guard.materialize_suppressed_verdict(
        silent, body, state, "task-77", stub="[no-send]")
    check(out == silent, "f) an already-silent suppress is not re-journaled")

    # g) The WRITE raises after the directory exists. Case (d) fails at mkdir and
    #    takes the earlier OSError guard, so this branch was untested.
    ok_dir = pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-raise-"))
    saved_writer = guard._write_artifact
    guard._write_artifact = lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone"))
    try:
        got_g, _ = guard.guard_result_for_tier(
            body, "team", REPO, suppress_journal=(ok_dir, "task-raise"))
    finally:
        guard._write_artifact = saved_writer
    check(got_g == guard.TEAM_SUPPRESS_RESULT,
          f"g) a RAISING journal write -> notice stands, got {got_g[:40]!r}")

    # h) The write reports failure without raising -- same requirement, different path.
    guard._write_artifact = lambda *a, **k: False
    try:
        got_h, _ = guard.guard_result_for_tier(
            body, "team", REPO, suppress_journal=(ok_dir, "task-false"))
    finally:
        guard._write_artifact = saved_writer
    check(got_h == guard.TEAM_SUPPRESS_RESULT,
          f"h) an UNSUCCESSFUL journal write -> notice stands, got {got_h[:40]!r}")

    # i) A flat "[no-send]" would drop the dedup target discord-bridge needs to
    #    validate the holder's channel. The body must equal the stub function's.
    for src_body, want in (("[no-send]\nx", "[no-send]"),
                           ("[REPLIED]\nx", "[REPLIED]"),
                           ("[deduped: task-abc123]\nx", "[deduped: task-abc123]")):
        st = pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-stub-"))
        got_i, _ = guard.guard_result_for_tier(
            src_body, "team", REPO, suppress_journal=(st, "task-stub"))
        check(got_i == want, f"i) {src_body.splitlines()[0]} -> {want!r}, got {got_i!r}")
        check(got_i == guard.suppression_stub_for_tier(src_body, "team"),
              "i) and it equals suppression_stub_for_tier (one policy, not two)")

    # j) A default stub would let a future caller silently reinstate the
    #    flattening. The signature, not the docstring, has to refuse.
    try:
        guard.materialize_suppressed_verdict(
            guard.TeamResultVerdict(guard.VERDICT_SUPPRESS,
                                    guard.TEAM_SUPPRESS_RESULT, "r"),
            "[REPLIED]\nx", pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-j-")),
            "task-j")
    except TypeError:
        check(True, "j) omitting stub is a TypeError, not a silent [no-send]")
    else:
        check(False, "j) omitting stub was ACCEPTED -- the default is back")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS -- suppression is journaled and closed silently on a bound adapter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
