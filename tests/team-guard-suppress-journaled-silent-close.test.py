#!/usr/bin/env python3
"""A guarded-tier suppression is honoured and journaled, never refused.

`[channel:]` and `[file:]` move data somewhere the sender should not reach.
`[no-send]`, `[REPLIED]` and `[deduped:]` move nothing -- the party left without
an answer is the same non-owner who asked. What they can hide is that a task was
handled, which is an accountability property, and the answer to it is a durable
record rather than a refusal. So the guard stops classifying them at all: the
body passes through BYTE-IDENTICAL and the bridge honours it as it does for the
owner.

That also deletes the stub minter, which was the only place the guard parsed and
validated a dedup target -- a second copy of a rule the consumer already owns.

  a) journal bound       -> body passes through VERBATIM, record written
  b) journal omitted     -> still honoured, still verbatim (no notice)
  c) owner tier          -> untouched and never journaled
  d) journal UNWRITABLE  -> notice stands (fail-closed; a close is never both
                            silent and unrecorded)
  e) redirect/attach     -> still LEAK, never honoured
  f) malformed dedup id  -> HONOURED, not converted into channel prose
  g) secret + skip       -> honoured; the scan never runs on an undelivered body
  h) the stub minter is GONE, not merely unused
  i) a non-DELIVER verdict handed to the journal is returned untouched
  j) a record write that REPORTS failure (not just mkdir) also keeps the notice

Run: python3 tests/team-guard-suppress-journaled-silent-close.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
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


def _leaky(_text):
    class R:
        detected = True
        secret_types = ["api key"]
    return R()


def _clean(_text):
    class R:
        detected = False
        secret_types: list = []
    return R()


def _raise_write(*_args, **_kwargs):
    raise OSError("disk gone")


def main() -> int:
    body = "[no-send]\nAddressed to another agent; nothing owed here."

    # a) THE CHANGE. The body is handed back unchanged -- not a minted stub.
    state = pathlib.Path(tempfile.mkdtemp(prefix="tg-suppress-"))
    got, reason = guard.guard_result_for_tier(
        body, "team", REPO, secret_filter=_clean, suppress_journal=(state, "task-42"))
    check(got == body, f"a) body passes through VERBATIM, got {got!r}")
    check(reason is None, f"a) nothing is withheld, got reason={reason!r}")
    rec = guard.suppressed_record_path(state, "task-42")
    check(rec.is_file(), f"a) journal record written at {rec.name}")
    if rec.is_file():
        payload = json.loads(rec.read_text())
        check(payload["suppressed_body"] == body, "a) the record carries the body")
        check(payload["task_id"] == "task-42", "a) record carries the task id")
        check(payload["status"] == "suppressed_silent_close",
              "a) record states the disposition")

    # b) An adapter that cannot journal still honours the marker. Before this
    #    change it got the notice instead, which is what reached the channel.
    got_b, reason_b = guard.guard_result_for_tier(body, "team", REPO, secret_filter=_clean)
    check(got_b == body and reason_b is None,
          f"b) honoured with no journal bound, got {got_b!r}")
    check(guard.TEAM_SUPPRESS_RESULT not in got_b, "b) and no notice in its place")

    # c) Owner is unguarded and must not acquire a record.
    owner_state = pathlib.Path(tempfile.mkdtemp(prefix="tg-owner-"))
    got_c, reason_c = guard.guard_result_for_tier(
        body, "owner", REPO, secret_filter=_clean,
        suppress_journal=(owner_state, "task-owner"))
    check(got_c == body and reason_c is None, "c) owner passes through untouched")
    check(not guard.suppressed_record_path(owner_state, "task-owner").is_file(),
          "c) and an unguarded result is not journaled")

    # d) FAIL-CLOSED. A close that cannot be recorded must not also be silent.
    blocked = pathlib.Path(tempfile.mkdtemp(prefix="tg-blocked-"))
    (blocked / guard.SUPPRESSED_RESULT_DIR).write_text("not a directory", encoding="utf-8")
    got_d, reason_d = guard.guard_result_for_tier(
        body, "team", REPO, secret_filter=_clean, suppress_journal=(blocked, "task-99"))
    check(got_d == guard.TEAM_SUPPRESS_RESULT,
          f"d) unwritable journal -> the notice STANDS, got {got_d[:40]!r}")
    check(reason_d == "suppression record unwritable",
          f"d) and it says why, got {reason_d!r}")

    # e) The markers that DO move data are untouched by this change.
    leak_state = pathlib.Path(tempfile.mkdtemp(prefix="tg-leak-"))
    for n, marker in enumerate(("[channel: 1530802402603700415]\nrouted",
                                "[file: /etc/passwd]",
                                "[channel: 123]\n[no-send]\nboth")):
        got_e, reason_e = guard.guard_result_for_tier(
            marker, "team", REPO, secret_filter=_clean,
            suppress_journal=(leak_state, f"task-leak-{n}"))
        check(got_e == guard.TEAM_LEAK_RESULT and reason_e is not None,
              f"e) {marker.splitlines()[0]} still LEAK")
        check(not guard.suppressed_record_path(leak_state, f"task-leak-{n}").is_file(),
              f"e) and {marker.splitlines()[0]} is not journaled as a silent close")

    # e2) A leading skip cannot smuggle a redirect: the parser only executes a
    #     redirect that LEADS the body, so the marker on line 2 is inert text.
    smuggle = "[no-send]\n[channel: 123]\nboth"
    check(guard.is_suppression_only(smuggle),
          "e2) a leading skip with a trailing [channel:] is suppression-only")
    check(not any(a.kind == "redirect" for a in guard.parse_markers(smuggle).actions),
          "e2) and the trailing [channel:] is never an executable action")

    # f) THE EDGE THIS DELETES. A dedup target the guard could not validate used
    #    to become a notice; the consumer that dereferences one rejects it.
    malformed = pathlib.Path(tempfile.mkdtemp(prefix="tg-malformed-"))
    for target in ("[deduped: EVIL bytes]", "[deduped: ../../../etc/passwd]"):
        got_f, reason_f = guard.guard_result_for_tier(
            target, "team", REPO, secret_filter=_clean,
            suppress_journal=(malformed, "task-mal"))
        check(got_f == target and reason_f is None,
              f"f) {target} is honoured, not turned into channel prose")
    check(guard.suppressed_record_path(malformed, "task-mal").is_file(),
          "f) and it is journaled like any other close")

    # g) Suppression short-circuits the scan: an undelivered body has nothing to
    #    leak, and a LEAK verdict here would put a notice back in the channel.
    got_g, reason_g = guard.guard_result_for_tier(
        "[no-send]\nAKIAIOSFODNN7EXAMPLE", "team", REPO, secret_filter=_leaky)
    check(got_g == "[no-send]\nAKIAIOSFODNN7EXAMPLE" and reason_g is None,
          f"g) a secret-carrying skip body is still honoured, got {got_g[:40]!r}")

    # i) The two fail-closed branches of the journal, driven DIRECTLY: the
    #    wrapper only ever hands it a DELIVER verdict, so nothing else reaches them.
    leak_in = guard.TeamResultVerdict(guard.VERDICT_LEAK, guard.TEAM_LEAK_RESULT, "secret")
    out_i = guard.journal_suppressed_result(
        leak_in, body, pathlib.Path(tempfile.mkdtemp(prefix="tg-i-")), "task-i")
    check(out_i == leak_in, f"i) a non-DELIVER verdict is returned untouched, got {out_i}")

    # j) The record write REPORTS failure (returns False) rather than raising —
    #    distinct from d), which fails one step earlier at mkdir.
    for label, artifact in (("returns False", lambda *_a, **_k: False),
                            ("raises", _raise_write)):
        state_j = pathlib.Path(tempfile.mkdtemp(prefix="tg-j-"))
        saved_write = guard._write_artifact
        guard._write_artifact = artifact
        try:
            got_j, reason_j = guard.guard_result_for_tier(
                body, "team", REPO, secret_filter=_clean,
                suppress_journal=(state_j, "task-j"))
        finally:
            guard._write_artifact = saved_write
        check(got_j == guard.TEAM_SUPPRESS_RESULT and
              reason_j == "suppression record unwritable",
              f"j) write {label} -> notice stands, got {got_j[:32]!r}/{reason_j!r}")

    # h) Removed, not orphaned: an unused minter is one import from returning.
    check(not hasattr(guard, "suppression_stub_for_tier"),
          "h) suppression_stub_for_tier is gone from the module")
    check(not hasattr(guard, "materialize_suppressed_verdict"),
          "h) and so is the verdict-rewriting form it fed")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — suppression is honoured on every tier and journaled, not refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
