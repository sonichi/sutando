#!/usr/bin/env python3
"""src/task_transition.py: the one owner of a task's archive-then-report transition.

Every case drives the PRODUCTION operation on a real filesystem (#4399, rounds 5-6,
kewei-red / susans-MacBook-Pro): two transitions in one frozen second from separate
processes leave two notices; a name collision inside one process is retried, never
replaced; a task replaced between the generation check and the claim survives under
its own name; a retaken name leaves the claimed bytes quarantined; an archived record
under the same name is never overwritten and the notice names the `.N` it landed
under; the directories are pinned handles, so one swapped for a symlink after
pre-flight is refused; and a crash after each persisted stage (claim, placement,
reserved notice, publication) is recovered by the next pass exactly once.

Run: python3 tests/task-transition.test.py
"""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import task_transition as tt  # noqa: E402

# The transition's own entries in the archive dir: its lock and the retired names.
_OURS = (tt._LOCK_NAME, getattr(tt.task_archive, "RETIRED_DIR", ".retired"))

PREFIX = "proactive-orphan-recovery-"
FROZEN = 1800000000.0


class Crash(RuntimeError):
    """The process dies here."""


def _task(tasks: Path, name: str, ask: str) -> Path:
    p = tasks / name
    p.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    return p


def _replace(live: Path, ask: str) -> None:
    tmp = live.with_name(f".{live.name}.new")
    tmp.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    os.replace(tmp, live)


def _unref(text: str) -> str:
    """A published notice without its transaction line, which every notice ends with."""
    head, sep, tail = text.rstrip("\n").rpartition("\n\n(ref ")
    assert sep and tail.endswith(")") and len(tail) == 17, text
    return head + "\n"


def _notice(text: str | None):
    """A renderer that says `text` whatever landed (None: nothing to say)."""
    return lambda landed: text


def _naming(landed: dict) -> str | None:
    """A renderer that names what landed, the way the skill's does."""
    return ("archived: " + ", ".join(f"{k}->{v}" for k, v in sorted(landed.items())) + "\n") if landed else None


def _transition_in_child(root: str, name: str, body: str, out) -> None:
    ws = Path(root)
    gen = tt.generation_of(ws / "tasks" / name)
    out.put(tt.transition(ws / "tasks", ws / "results", ws / "tasks" / "archive",
                          moves={name: gen}, render=_notice(body), prefix=PREFIX, now=FROZEN))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        self.archive = self.tasks / "archive"
        self.tasks.mkdir(parents=True)
        self.results.mkdir()

    def run_transition(self, names: list[str], render=_notice("notice\n"), **kw) -> dict:
        moves = {n: tt.generation_of(self.tasks / n) for n in names}
        return tt.transition(self.tasks, self.results, self.archive, moves=moves, render=render,
                             prefix=PREFIX, **kw)

    def notices(self) -> list[Path]:
        return sorted(self.results.glob(f"{PREFIX}*.txt"))

    def journals(self) -> list[Path]:
        return sorted(self.archive.glob(f"{tt.JOURNAL_PREFIX}*.json")) if self.archive.is_dir() else []

    def live_names(self) -> list[str]:
        return sorted(x.name for x in self.tasks.iterdir())

    def archive_names(self) -> list[str]:
        return sorted(x.name for x in self.archive.iterdir() if not x.name.startswith("."))

    def names_outside_retired(self, path: Path) -> int:
        """Names of this inode in tasks/ and tasks/archive/; `.retired/` holds the set-aside ones."""
        ino = os.stat(path).st_ino
        return sum(1 for d in (self.tasks, self.archive) for x in d.iterdir()
                   if x.is_file() and os.stat(x).st_ino == ino)

    def inject(self, point: str, p: Path, action, module=None):
        """Run `action()` once at the named checkpoint for `p` — `before_claim` /
        `after_claim` in task_transition, `before_give_back` in task_archive —
        the explicit injection points the owner calls, not a call count."""
        module, fired = module or tt, []

        def at(where, name):
            if where == point and name == p.name and not fired:
                fired.append(1)
                action()

        return mock.patch.object(module, "_checkpoint", side_effect=at)

    def replace_before_claim(self, p: Path, ask: str):
        """Susan's and Kewei's round-6 repro: the live name is rewritten after the
        move's own generation check and before the claim rename."""
        return self.inject("before_claim", p, lambda: _replace(p, ask))


class TestExclusivePublication(Base):
    def test_two_processes_in_one_frozen_second_leave_two_notices(self):
        _task(self.tasks, "task-a.txt", "A")
        _task(self.tasks, "task-b.txt", "B")
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        procs = [ctx.Process(target=_transition_in_child, args=(str(self.ws), n, f"notice {n}\n", q))
                 for n in ("task-a.txt", "task-b.txt")]
        for p in procs:
            p.start()
        outs = [q.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(30)
        self.assertEqual([o["errors"] for o in outs], [[], []])
        self.assertEqual([o["recovered"] for o in outs], [[], []], "a live pass's journal is never taken")
        self.assertEqual(len({o["result"] for o in outs}), 2)
        self.assertEqual(sorted(_unref(p.read_text()) for p in self.notices()),
                         ["notice task-a.txt\n", "notice task-b.txt\n"])
        self.assertEqual(self.archive_names(), ["task-a.txt", "task-b.txt"])
        self.assertEqual(sorted(x.name for x in self.archive.iterdir()
                                if x.name.startswith(".") and x.name not in _OURS), [])

    def test_a_name_collision_in_one_process_is_retried_never_replaced(self):
        _task(self.tasks, "task-a.txt", "A")
        _task(self.tasks, "task-b.txt", "B")
        real_hex, names = tt.secrets.token_hex, iter(["aaaa", "aaaa", "bbbb"])
        # Only the 4-byte result-name token is scripted; staging, claim (5) and journal (6) stay real.
        with mock.patch.object(tt.secrets, "token_hex",
                               side_effect=lambda n=32: next(names) if n == 4 else real_hex(n)):
            first = self.run_transition(["task-a.txt"], _notice("first\n"), now=FROZEN)
            second = self.run_transition(["task-b.txt"], _notice("second\n"), now=FROZEN)
        self.assertEqual((first["errors"], second["errors"]), ([], []))
        self.assertEqual(Path(first["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-aaaa.txt")
        self.assertEqual(Path(second["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-bbbb.txt")
        self.assertEqual(_unref(Path(first["result"]).read_text()), "first\n")
        self.assertEqual([p.name for p in self.results.iterdir() if p.name.startswith(".")], [],
                         "no staging left behind by the refused attempt")
        self.assertEqual(self.journals(), [])

    def test_the_commit_is_result_publishs_exclusive_mode(self):
        with mock.patch.object(tt.result_publish, "publish_text",
                               wraps=tt.result_publish.publish_text) as m:
            self.run_transition([], _notice("notice\n"))
        self.assertEqual(m.call_count, 1)
        self.assertTrue(m.call_args.kwargs.get("exclusive"))

    def test_an_undeliverable_body_is_refused_and_the_archive_stays_journaled(self):
        _task(self.tasks, "task-a.txt", "A")
        out = self.run_transition(["task-a.txt"], _notice("x" * (tt.PROACTIVE_MAX_BODY_B + 1)))
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("undeliverable", out["errors"][0])
        self.assertIn("recorded in tasks/archive/.notice-", out["errors"][0])
        self.assertIsNone(out["result"])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertEqual(len(self.journals()), 1)
        with self.assertRaises(tt.TransitionRefused):
            tt.publish_exclusive(self.results, PREFIX, "x" * (tt.PROACTIVE_MAX_BODY_B + 1))

    def test_a_body_at_the_ceiling_is_published(self):
        out = self.run_transition([], _notice("x" * tt.PROACTIVE_MAX_BODY_B))
        self.assertEqual(out["errors"], [])
        self.assertEqual(Path(out["result"]).stat().st_size, tt.PROACTIVE_MAX_BODY_B)


class TestGenerationGuard(Base):
    def test_generation_is_the_bytes_not_the_name(self):
        p = _task(self.tasks, "task-a.txt", "A")
        before = tt.generation_of(p)
        _replace(p, "A rewritten")
        self.assertNotEqual(before, tt.generation_of(p))
        self.assertIsNone(tt.generation_of(self.tasks / "absent.txt"))

    def test_replaced_after_planning_refuses_everything(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        _replace(p, "new")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                            render=_notice("notice\n"), prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("refused to move 'task-a.txt': changed since it was planned", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], out["archived"], self.notices()), (None, {}, []))
        self.assertIn("new", p.read_text())
        self.assertEqual(self.journals(), [], "no journal is written for a refused plan")

    def test_replaced_between_the_generation_check_and_the_claim_puts_the_new_task_back(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        with self.replace_before_claim(p, "new"):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("refused to move 'task-a.txt': changed since it was planned; it is back under "
                      "its name, live", out["errors"][0])
        self.assertEqual((out["archived"], out["quarantined"], out["result"]), ({}, {}, None))
        self.assertIn("new", p.read_text(), "the new task survives under its own name")
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"], "no claim left behind")
        self.assertEqual(sorted(x for x in self.archive.iterdir() if x.name not in _OURS), [],
                         "nothing archived, no journal kept")

    def test_a_name_retaken_during_the_give_back_quarantines_the_claim(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        retake = self.inject("before_give_back", p, lambda: _task(self.tasks, "task-a.txt", "newer still"),
                             module=tt.task_archive)  # a third writer wins the name during the give-back
        with self.replace_before_claim(p, "new"), retake:
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("quarantined as tasks/task-a.txt.claim-", out["errors"][0])
        self.assertIn("rename it back to requeue", out["errors"][0])
        kept = [x for x in self.tasks.iterdir() if x.name.startswith("task-a.txt.claim-")
                and not x.name.endswith(tt.QUARANTINE_SUFFIX)]
        self.assertEqual(len(kept), 1)
        self.assertEqual(out["quarantined"], {"task-a.txt": kept[0].name})
        self.assertIn("new", kept[0].read_text())
        self.assertIn("newer still", p.read_text())
        self.assertEqual(self.archive_names(), [])
        self.assertEqual(self.journals(), [], "nothing landed: nothing to publish, journal closed")
        self.assertTrue((self.tasks / (kept[0].name + tt.QUARANTINE_SUFFIX)).exists(), "marked as kept on purpose")
        swept = self.recover_sweep()
        self.assertEqual(swept["swept"], [], "a quarantine is never swept as a stray")
        self.assertTrue((self.tasks / kept[0].name).exists(), "the quarantined bytes are still there")

    def recover_sweep(self) -> dict:
        return tt.transition(self.tasks, self.results, self.archive, moves={}, render=_notice(None), prefix=PREFIX)

    def test_a_writer_recreating_the_live_name_during_the_move_is_never_touched(self):
        """After the claim rename the live name is free; a task written there is
        a new task the move never sees, and the notice describes the old bytes."""
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        real_move = tt.task_archive.move_without_clobbering

        def move_then_recreate(src, dest, **kw):
            _task(self.tasks, "task-a.txt", "new")
            return real_move(src, dest, **kw)

        with mock.patch.object(tt.task_archive, "move_without_clobbering", side_effect=move_then_recreate):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertIn("old", (self.archive / "task-a.txt").read_text())
        self.assertIn("new", p.read_text())
        self.assertEqual(_unref(Path(out["result"]).read_text()), "archived: task-a.txt->task-a.txt\n")

    def test_a_failed_placing_move_puts_the_claim_back(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with mock.patch.object(tt.task_archive, "move_without_clobbering",
                               side_effect=PermissionError(1, "EPERM")):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertIn("the archive move failed", out["errors"][0])
        self.assertIn("back under its name, live", out["errors"][0])
        self.assertIn("task: A", p.read_text())
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"])
        self.assertIsNone(out["result"], "nothing landed, nothing to say")

    def test_the_move_is_task_archives_contract_through_the_directory_handles(self):
        p = _task(self.tasks, "task-a.txt", "A")
        with mock.patch.object(tt.task_archive, "claim_generation",
                               wraps=tt.task_archive.claim_generation) as c, \
                mock.patch.object(tt.task_archive, "place_claim", wraps=tt.task_archive.place_claim) as m:
            landed = tt.archive_generation(self.tasks, "task-a.txt", tt.generation_of(p), self.archive)
        self.assertEqual(landed, self.archive / "task-a.txt")
        self.assertEqual((c.call_count, m.call_count), (1, 1))
        self.assertTrue(m.call_args.args[0].startswith("task-a.txt.claim-"))
        self.assertIsInstance(m.call_args.kwargs["src_dir_fd"], int)
        self.assertIsInstance(m.call_args.kwargs["dst_dir_fd"], int)

    def test_a_missing_generation_is_refused(self):
        _task(self.tasks, "task-a.txt", "A")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": None},
                            render=_notice(None), prefix=PREFIX)
        self.assertIn("no generation was captured", out["errors"][0])
        self.assertTrue((self.tasks / "task-a.txt").exists())


class TestNoClobberAndSymlinks(Base):
    def test_an_archived_record_is_never_overwritten_and_the_notice_names_the_landing(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("earlier\n")
        (self.archive / "task-a.txt.1").write_text("earlier still\n")
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt.2"})
        self.assertEqual((self.archive / "task-a.txt").read_text(), "earlier\n")
        self.assertIn("task: A", (self.archive / "task-a.txt.2").read_text())
        self.assertFalse((self.tasks / "task-a.txt").exists())
        self.assertEqual(_unref(Path(out["result"]).read_text()), "archived: task-a.txt->task-a.txt.2\n")

    def test_names_that_are_not_bare_are_refused_before_anything_moves(self):
        (self.ws / "secret.txt").write_text("stay")
        absolute = str(self.ws / "secret.txt")
        dotted = _task(self.tasks, "task-x.txt", "X")
        g = tt.generation_of  # real generations: only the bare-name guard stands in the way
        out = tt.transition(self.tasks, self.results, self.archive,
                            moves={"../secret.txt": g(self.ws / "secret.txt"), absolute: g(Path(absolute)),
                                   "": g(self.tasks), "..": g(self.ws), "./task-x.txt": g(dotted)},
                            render=_notice("notice\n"), prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 5, out)
        for err in out["errors"]:
            self.assertIn("refused to move", err)
            self.assertIn("not a bare filename", err)
        self.assertEqual((out["result"], self.notices()), (None, []))
        self.assertEqual((self.ws / "secret.txt").read_text(), "stay")
        self.assertFalse((self.tasks / "secret.txt").exists())
        self.assertTrue(dotted.exists())

    def test_a_symlinked_task_is_refused_and_its_target_untouched(self):
        real = Path(self.tmp.name) / "real.txt"
        real.write_text("task: via link\n")
        (self.tasks / "task-link.txt").symlink_to(real)
        out = self.run_transition(["task-link.txt"])
        self.assertIn("refused to move 'task-link.txt': a symlink, not a task file", out["errors"][0])
        self.assertEqual((out["result"], out["archived"]), (None, {}))
        self.assertTrue(real.exists() and (self.tasks / "task-link.txt").is_symlink())

    def test_a_symlinked_archive_dir_is_refused_at_preflight(self):
        _task(self.tasks, "task-a.txt", "A")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.archive.symlink_to(outside, target_is_directory=True)
        out = self.run_transition(["task-a.txt"])
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("is a symlink; nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], sorted(outside.iterdir())), (None, []))
        self.assertTrue((self.tasks / "task-a.txt").exists())

    def test_an_archive_dir_swapped_for_a_symlink_after_preflight_is_refused(self):
        """Kewei's round-6 TOCTOU: the directory is checked, then replaced by
        `archive -> outside` before the move. The move works through the handle
        pinned at pre-flight, sees the name no longer leads to it, and refuses;
        nothing reaches `outside`."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        real = tt._Journal.create

        def swap_after_journal(*a, **kw):
            j = real(*a, **kw)
            for x in self.archive.iterdir():  # empty the pinned dir so the name can be re-pointed
                x.unlink()
            self.archive.rmdir()
            self.archive.symlink_to(outside, target_is_directory=True)
            return j

        with mock.patch.object(tt._Journal, "create", side_effect=swap_after_journal):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("tasks/archive/ was replaced since pre-flight", out["errors"][0])
        self.assertEqual((out["result"], out["archived"]), (None, {}))
        self.assertEqual(sorted(outside.iterdir()), [], "nothing followed the symlink")
        self.assertIn("task: A", p.read_text())

    def test_an_uncreatable_archive_dir_publishes_nothing(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.write_text("a file where the dir should be")
        out = self.run_transition(["task-a.txt"])
        self.assertIn("could not create", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], self.notices()), (None, []))

    def test_nothing_to_move_publishes_without_a_journal(self):
        out = self.run_transition([], _notice("notice\n"))
        self.assertEqual(out["errors"], [])
        self.assertIsNotNone(out["result"])
        self.assertEqual(sorted(x for x in self.archive.iterdir() if x.name not in _OURS), [],
                         "created empty, no journal")


class TestJournalRecovery(Base):
    """Kewei's round-6 contract: a crash after each persisted stage is recovered
    by the next pass exactly once — one copy of every generation, live or
    archived; one notice iff publication committed; nothing done twice."""

    def crash_after(self, stage: str):
        """A context in which the owner dies right after persisting `stage`
        (`claimed`, `placed`, `publishing`) or after publishing (`published`)."""
        real_set, real_publish = tt._Journal.set, tt._publish_at

        def set_then_crash(self_, **fields):
            real_set(self_, **fields)
            if fields.get("stage") == stage:
                raise Crash(stage)

        def publish_then_crash(*a, **kw):
            path = real_publish(*a, **kw)
            raise Crash("published")

        if stage == "published":
            return mock.patch.object(tt, "_publish_at", side_effect=publish_then_crash)
        return mock.patch.object(tt._Journal, "set", set_then_crash)

    def invariants(self, generation: str, ask: str, notice_committed: bool):
        copies = [x for x in list(self.tasks.iterdir()) + list(self.archive.iterdir())
                  if x.is_file() and not x.name.startswith(".") and tt.generation_of(x) == generation]
        self.assertEqual(len(copies), 1, f"exactly one copy of the generation: {copies}")
        self.assertIn(ask, copies[0].read_text())
        self.assertEqual(len(self.notices()), 1 if notice_committed else 0)
        self.assertEqual(self.journals(), [])
        self.assertEqual([x.name for x in self.archive.iterdir()
                          if x.name.startswith(".") and x.name not in _OURS], [])

    def recover(self) -> dict:
        return self.run_transition([], _notice(None))

    def test_crash_after_the_claim_is_placed_and_published_by_the_next_pass(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("claimed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(self.notices(), [], "no notice before publication committed")
        self.assertTrue(any(x.name.startswith("task-a.txt.claim-") for x in self.tasks.iterdir()))
        self.assertEqual(json.loads(self.journals()[0].read_text())["stage"], "claimed")
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.assertIn("archived: task-a.txt->task-a.txt", Path(out["recovered"][0]["result"]).read_text())
        self.assertIn("Published by a later pass", Path(out["recovered"][0]["result"]).read_text())
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [], "a second recovery pass finds nothing")

    def test_crash_after_the_placement_is_found_by_generation_and_published(self):
        """The claim was placed but the journal still says `claimed`: recovery
        finds the archived copy by its generation, under its `.N` landing."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("earlier\n")
        real_place = tt.task_archive.place_claim

        def place_then_crash(*a, **kw):
            real_place(*a, **kw)
            raise Crash("after placement, before the journal")

        with mock.patch.object(tt.task_archive, "place_claim", side_effect=place_then_crash), \
                self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt.1"})
        body = Path(out["recovered"][0]["result"]).read_text()
        self.assertIn("task-a.txt is archived as tasks/archive/task-a.txt.1", body)
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual((self.archive / "task-a.txt").read_text(), "earlier\n")
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_the_placed_stage_publishes_once(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("placed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(self.notices(), [])
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_the_notice_name_was_reserved_publishes_at_that_name_once(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("publishing"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        reserved = json.loads(self.journals()[0].read_text())["result"]
        self.assertEqual(self.notices(), [])
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(Path(out["recovered"][0]["result"]).name, reserved)
        self.assertEqual(_unref(Path(out["recovered"][0]["result"]).read_text()), "archived: task-a.txt->task-a.txt\n")
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_publication_sends_no_second_notice(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("published"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(self.journals()), 1, "the journal outlived the commit")
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["result"], str(self.notices()[0]))
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_between_the_claim_record_and_the_rename_leaves_the_task_live(self):
        """Mini's race (round 6, dev room), first half: the journal names the claim
        but the rename never happened. Recovery must read the task as still
        live under its own name — planned afresh — never as gone."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)

        def die():
            raise Crash("before rename")

        with self.inject("before_claim", p, die), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        journal = json.loads(self.journals()[0].read_text())
        self.assertEqual(journal["stage"], "claiming")
        self.assertTrue(journal["claims"]["task-a.txt"].startswith("task-a.txt.claim-"))
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"], "nothing was renamed")
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["recovered"][0]["landed"], {})
        self.assertEqual(self.notices(), [], "nothing landed, nothing to say")
        self.assertEqual(self.journals(), [])
        self.assertEqual(tt.generation_of(p), gen, "still live, untouched")
        again = self.run_transition(["task-a.txt"], _naming)  # the fresh plan archives it once
        self.assertEqual(again["archived"], {"task-a.txt": "task-a.txt"})
        self.invariants(gen, "task: A", notice_committed=True)

    def test_crash_between_the_rename_and_the_claimed_record_is_attributed_by_the_journal(self):
        """Mini's race, second half: the rename happened to the recorded name but
        the `claimed` stage was never written. The live name is absent; the
        recorded claim is present; recovery places it, never reports it gone."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)

        def die():
            raise Crash("after rename, before the record")

        with self.inject("after_claim", p, die), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        journal = json.loads(self.journals()[0].read_text())
        self.assertEqual(journal["stage"], "claiming")
        claim = journal["claims"]["task-a.txt"]
        self.assertEqual(self.live_names(), ["archive", claim], "renamed to exactly the recorded name")
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.assertIn("archived: task-a.txt->task-a.txt", Path(out["recovered"][0]["result"]).read_text())
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_a_recorded_claim_that_no_longer_matches_is_given_back_by_recovery(self):
        """The recorded claim is present but a writer rewrote it meanwhile (it is
        not the planned generation): recovery gives it back live, archives nothing."""
        p = _task(self.tasks, "task-a.txt", "old")
        real = tt.task_archive.claim_generation

        def rename_then_crash(*a, **kw):
            real(*a, **kw)
            raise Crash("after rename")

        with mock.patch.object(tt.task_archive, "claim_generation", side_effect=rename_then_crash), \
                self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        claim = self.tasks / json.loads(self.journals()[0].read_text())["claims"]["task-a.txt"]
        _replace(claim, "rewritten while claimed")
        out = self.recover()
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("changed since it was planned; it is back under its name, live", out["errors"][0])
        self.assertIn("rewritten while claimed", p.read_text())
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"])
        self.assertEqual((self.notices(), self.journals(), self.archive_names()), ([], [], []))

    def test_the_claim_is_recorded_before_the_rename(self):
        """The order is the guarantee: at the instant of the rename, the journal on
        disk already names the destination."""
        _task(self.tasks, "task-a.txt", "A")
        seen = []
        real = tt.task_archive.claim_generation

        def peek(name, *, src_dir_fd, claim=None):
            seen.append((claim, json.loads(self.journals()[0].read_text())))
            return real(name, src_dir_fd=src_dir_fd, claim=claim)

        with mock.patch.object(tt.task_archive, "claim_generation", side_effect=peek):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        (claim, journal), = seen
        self.assertEqual(journal["stage"], "claiming")
        self.assertEqual(journal["claims"], {"task-a.txt": claim})

    def test_a_finished_pass_leaves_nothing_for_recovery(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_a_failed_publish_is_finished_by_the_next_pass_without_moving_again(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.results.rmdir()
        self.results.write_text("not a directory")
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertIn("result write failed", out["errors"][0])
        self.assertIn("published by the next pass", out["errors"][0])
        self.assertEqual(json.loads(self.journals()[0].read_text())["stage"], "publishing")
        self.results.unlink()
        self.results.mkdir()
        with mock.patch.object(tt.task_archive, "place_claim", side_effect=AssertionError("moved again")):
            again = self.recover()
        self.assertEqual(again["errors"], [])
        self.assertEqual(again["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.invariants(gen, "task: A", notice_committed=True)

    def test_a_live_passs_journal_is_never_taken(self):
        _task(self.tasks, "task-a.txt", "A")
        real = tt._Journal.create
        seen = []

        def create_then_peek(*a, **kw):
            j = real(*a, **kw)
            seen.append(self.recover()["recovered"])  # another pass runs while this one holds the lock
            return j

        with mock.patch.object(tt._Journal, "create", side_effect=create_then_peek), \
                mock.patch.object(tt, "_LOCK_WAIT_S", 0):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.assertEqual(seen, [[]])
        self.assertEqual(len(self.notices()), 1)

    def test_the_handles_stay_open_across_recovery(self):
        """Recovery places through the handles opened at this pass's pre-flight:
        an archive dir swapped for a symlink in between is refused, not followed."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("claimed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        real = tt._Journal.take

        def take_then_swap(*a, **kw):
            j = real(*a, **kw)
            kept = {x.name: x.read_bytes() for x in self.archive.iterdir()}
            for x in self.archive.iterdir():
                x.unlink()
            self.archive.rmdir()
            self.archive.symlink_to(outside, target_is_directory=True)
            for n, b in kept.items():
                (outside / n).write_bytes(b)
            return j

        with mock.patch.object(tt._Journal, "take", side_effect=take_then_swap):
            out = self.recover()
        self.assertTrue(any("was replaced since pre-flight" in e for e in out["errors"]), out)
        self.assertEqual([x.name for x in outside.iterdir() if not x.name.startswith(".")], [])
        self.assertTrue(any(x.name.startswith("task-a.txt.claim-") for x in self.tasks.iterdir()),
                        "the claim is still there for the next recovery")

    def test_a_dead_passs_unjournaled_claim_is_given_back_and_reported(self):
        """A claim whose pid is gone and that no journal names (its pass died
        between the claim and the record, or the journal is gone): given back to
        its live name for the next plan to judge, never deleted."""
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"  # no such pid
        stray.write_text("task: Z\n")
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["swept"], [{"claim": stray.name, "live": "task-z.txt"}])
        self.assertEqual(self.live_names(), ["archive", "task-z.txt"])
        self.assertEqual((self.tasks / "task-z.txt").read_text(), "task: Z\n")

    def test_a_dead_passs_claim_whose_name_is_taken_is_kept_and_reported(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("task: Z old\n")
        _task(self.tasks, "task-z.txt", "Z new")
        out = self.recover()
        self.assertEqual(out["swept"], [])
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("orphaned claim tasks/task-z.txt.claim-", out["errors"][0])
        self.assertIn("rename it back to requeue", out["errors"][0])
        self.assertTrue(stray.exists())
        self.assertIn("Z new", (self.tasks / "task-z.txt").read_text())

    def test_a_live_passs_claim_is_never_swept(self):
        mine = self.tasks / f"task-z.txt.claim-{os.getpid()}-abcdef0123"
        mine.write_text("task: Z\n")
        out = self.recover()
        self.assertEqual((out["swept"], out["errors"]), ([], []))
        self.assertTrue(mine.exists())

    def test_the_directories_are_fsynced_after_the_journal_and_the_claim(self):
        _task(self.tasks, "task-a.txt", "A")
        synced = []
        real = os.fsync

        def spy(fd):
            synced.append(os.fstat(fd).st_ino)
            return real(fd)

        with mock.patch("os.fsync", side_effect=spy):  # task_archive imports os per function
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        inodes = {os.stat(self.tasks).st_ino: "tasks", os.stat(self.archive).st_ino: "archive"}
        names = [inodes.get(i, "other") for i in synced]
        self.assertGreaterEqual(names.count("archive"), 3, "journal persists: planned, claiming, claimed, …")
        self.assertGreaterEqual(names.count("tasks"), 2, "the claim rename and the placing move")

    def test_pending_notices_lists_the_journals_read_only(self):
        self.archive.mkdir()
        (self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json").write_text("{}")
        (self.archive / "task-old.txt").write_text("x")
        self.assertEqual(tt.pending_notices(self.archive), [f"{tt.JOURNAL_PREFIX}1-2-abc.json"])
        self.assertEqual(tt.pending_notices(self.ws / "nowhere"), [])

    def test_an_unreadable_journal_is_reported_and_left(self):
        self.archive.mkdir()
        bad = self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json"
        bad.write_text("not json")
        out = self.recover()
        self.assertIn("is unreadable; left for the operator", out["errors"][0])
        self.assertTrue(bad.exists())


class TestRefusalPaths(Base):
    """Each refusal leaves the live task where it was and names why."""

    def rename_then_crash(self):
        real = tt.task_archive.claim_generation

        def go(*a, **kw):
            real(*a, **kw)
            raise Crash("after rename")

        return mock.patch.object(tt.task_archive, "claim_generation", side_effect=go)

    def test_preflight_names_a_non_regular_source_and_promises_nothing_for_no_moves(self):
        (self.tasks / "task-d.txt").mkdir()
        self.assertEqual(tt.preflight(self.tasks, self.archive, {}), [])
        self.assertFalse(self.archive.exists(), "no moves: nothing is created")
        gen = tt.generation_of(self.tasks / "task-d.txt")
        self.assertEqual(tt.preflight(self.tasks, self.archive, {"task-d.txt": gen}),
                         ["refused to move 'task-d.txt': not a regular file"])
        self.assertTrue((self.tasks / "task-d.txt").is_dir())

    def test_preflight_refuses_an_archive_dir_not_directly_under_tasks(self):
        p = _task(self.tasks, "task-a.txt", "A")
        elsewhere = self.ws / "archive"
        out = tt.preflight(self.tasks, elsewhere, {"task-a.txt": tt.generation_of(p)})
        self.assertEqual(len(out), 1)
        self.assertIn("is not directly under", out[0])
        self.assertFalse(elsewhere.exists())

    def test_a_missing_tasks_dir_refuses_the_whole_transition(self):
        gone = self.ws / "no-tasks"
        out = tt.transition(gone, self.results, gone / "archive", moves={}, render=_notice("n\n"),
                            prefix=PREFIX)
        self.assertIn(f"could not open {gone}", out["errors"][0])
        self.assertEqual(self.notices(), [])

    @unittest.skipIf(os.geteuid() == 0, "root opens a mode-000 directory")
    def test_an_unopenable_archive_dir_is_refused_with_its_reason(self):
        p = _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir(mode=0)
        self.addCleanup(self.archive.chmod, 0o755)
        out = tt.preflight(self.tasks, self.archive, {"task-a.txt": tt.generation_of(p)})
        self.assertEqual(len(out), 1)
        self.assertIn(f"could not open {self.archive}", out[0])
        self.assertTrue(p.exists())

    def test_an_archive_dir_removed_after_preflight_is_reported_gone(self):
        dirs, problem = tt._open_dirs(self.tasks, self.archive)
        self.assertIsNone(problem)
        try:
            self.assertIsNone(dirs.problem())
            self.archive.rmdir()
            self.assertIn("tasks/archive/ is gone since pre-flight", dirs.problem())
        finally:
            dirs.close()

    def test_archive_generation_refuses_a_symlinked_archive_dir(self):
        p = _task(self.tasks, "task-a.txt", "A")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.archive.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(tt.TransitionRefused, "is a symlink"):
            tt.archive_generation(self.tasks, "task-a.txt", tt.generation_of(p), self.archive)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(p.exists())

    def test_archive_generation_refuses_a_task_replaced_after_planning(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        _replace(p, "new")
        with mock.patch.object(tt.task_archive, "claim_generation",
                               side_effect=AssertionError("the live name was renamed")):
            with self.assertRaisesRegex(tt.TransitionRefused, "changed since it was planned"):
                tt.archive_generation(self.tasks, "task-a.txt", gen, self.archive)
        self.assertIn("task: new", p.read_text())
        self.assertEqual(self.archive_names(), [])

    def test_a_task_replaced_after_preflight_is_refused_before_its_name_is_taken(self):
        """The pre-claim check, not place_claim's give-back, refuses it: the new
        task's live name is never renamed away, not even briefly."""
        p = _task(self.tasks, "task-a.txt", "A")
        q = _task(self.tasks, "task-b.txt", "old B")
        real, claimed = tt.task_archive.claim_generation, []

        def spy(name, **kw):
            claimed.append(name)
            return real(name, **kw)

        with self.inject("after_claim", p, lambda: _replace(q, "new B")), \
                mock.patch.object(tt.task_archive, "claim_generation", side_effect=spy):
            out = self.run_transition(["task-a.txt", "task-b.txt"], _naming)
        self.assertEqual(claimed, ["task-a.txt"])
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("'task-b.txt': changed since it was planned (a new generation", out["errors"][0])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertIn("task: new B", q.read_text())
        self.assertEqual(self.journals(), [])

    def test_a_refused_claim_whose_journal_rewrite_fails_still_finishes_the_pass(self):
        """Dropping a refused name from the journal is best-effort: recovery reads
        an unrenamed recorded claim as still live, so the pass goes on."""
        p = _task(self.tasks, "task-a.txt", "A")
        q = _task(self.tasks, "task-b.txt", "old B")
        real_set, seen_b = tt._Journal.set, []

        def set_failing_after_b_is_dropped(self_, **fields):
            if "task-b.txt" in self_.data["claims"]:
                seen_b.append(1)
            elif seen_b and fields.get("stage") == "claiming":
                raise OSError(28, "No space left on device")
            real_set(self_, **fields)

        with self.inject("after_claim", p, lambda: _replace(q, "new B")), \
                mock.patch.object(tt._Journal, "set", set_failing_after_b_is_dropped):
            out = self.run_transition(["task-a.txt", "task-b.txt"], _naming)
        self.assertTrue(seen_b)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("changed since it was planned", out["errors"][0])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertEqual(_unref(self.notices()[0].read_text()), "archived: task-a.txt->task-a.txt\n")
        self.assertIn("task: new B", q.read_text())
        self.assertEqual(self.journals(), [])

    def test_archive_generation_gives_back_a_claim_rewritten_after_the_rename(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)

        def rewrite_claim():
            claim = next(x for x in self.tasks.iterdir() if ".claim-" in x.name)
            with claim.open("a") as fh:
                fh.write("appended while claimed\n")

        with self.inject("after_claim", p, rewrite_claim):
            with self.assertRaisesRegex(tt.TransitionRefused, "back under its name, live"):
                tt.archive_generation(self.tasks, "task-a.txt", gen, self.archive)
        self.assertIn("appended while claimed", p.read_text())
        self.assertEqual((self.live_names(), self.archive_names()), (["archive", "task-a.txt"], []))

    def test_a_name_gone_at_the_claim_is_refused_as_already_archived(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.inject("before_claim", p, p.unlink):
            with self.assertRaisesRegex(tt.TransitionRefused, "another pass archived it"):
                tt.archive_generation(self.tasks, "task-a.txt", gen, self.archive)

    def test_a_name_gone_at_the_claim_in_a_transition_is_dropped_from_the_journal(self):
        p = _task(self.tasks, "task-a.txt", "A")
        q = _task(self.tasks, "task-b.txt", "B")
        moves = {n: tt.generation_of(self.tasks / n) for n in ("task-a.txt", "task-b.txt")}
        with self.inject("before_claim", p, p.unlink):
            out = tt.transition(self.tasks, self.results, self.archive, moves=moves,
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("another pass archived it", out["errors"][0])
        self.assertEqual(out["archived"], {"task-b.txt": "task-b.txt"})
        self.assertEqual(_unref(self.notices()[0].read_text()), "archived: task-b.txt->task-b.txt\n")
        self.assertEqual(self.journals(), [])
        self.assertFalse(q.exists())

    def test_a_journal_that_cannot_be_written_moves_nothing_and_leaves_no_debris(self):
        p = _task(self.tasks, "task-a.txt", "A")
        with mock.patch.object(tt.json, "dump", side_effect=OSError(28, "No space left on device")):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("could not write the journal", out["errors"][0])
        self.assertIn("nothing moved", out["errors"][0])
        self.assertTrue(p.exists())
        self.assertEqual([x.name for x in self.archive.iterdir() if x.name not in _OURS], [],
                         "no journal lock, tmp or journal left")
        self.assertEqual(self.notices(), [])

    def test_a_publish_with_nothing_to_move_that_fails_is_reported(self):
        self.results.rmdir()
        self.results.write_text("not a directory")
        out = self.run_transition([], _notice("notice\n"))
        self.assertIsNone(out["result"])
        self.assertIn("result write failed", out["errors"][0])
        self.assertIn("nothing moved", out["errors"][0])

    def test_publish_exclusive_never_replaces_a_taken_name(self):
        taken = self.results / f"{PREFIX}1-1-aaaa.txt"
        taken.write_text("earlier notice\n")
        names = iter([taken.name, f"{PREFIX}1-1-bbbb.txt"])
        with mock.patch.object(tt, "_mint_name", side_effect=lambda *a: next(names)):
            got = tt.publish_exclusive(self.results, PREFIX, "later\n")
        self.assertEqual(got.name, f"{PREFIX}1-1-bbbb.txt")
        self.assertEqual(taken.read_text(), "earlier notice\n")

    def test_every_name_taken_keeps_the_journal_for_the_next_pass(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        taken = self.results / f"{PREFIX}1-1-aaaa.txt"
        taken.write_text("earlier notice\n")
        with mock.patch.object(tt, "_mint_name", return_value=taken.name):
            out = self.run_transition(["task-a.txt"], _naming)
            with self.assertRaisesRegex(OSError, "could not reserve a result name"):
                tt.publish_exclusive(self.results, PREFIX, "x\n")
        self.assertIn("could not reserve a result name", out["errors"][0])
        self.assertEqual(taken.read_text(), "earlier notice\n")
        self.assertEqual(len(self.journals()), 1)
        again = TestJournalRecovery.recover(self)
        self.assertEqual(again["errors"], [])
        self.assertEqual(len(self.notices()), 2, "the earlier notice is not taken for this one")
        self.assertEqual(self.journals(), [])
        self.assertEqual(tt.generation_of(self.archive / "task-a.txt"), gen)


class TestRecoveryEdges(Base):
    """Recovery's own failure paths: each reports and keeps what it cannot finish."""

    crash_after, invariants = TestJournalRecovery.crash_after, TestJournalRecovery.invariants
    recover, rename_then_crash = TestJournalRecovery.recover, TestRefusalPaths.rename_then_crash

    def recorded_claim(self) -> Path:
        return self.tasks / json.loads(self.journals()[0].read_text())["claims"]["task-a.txt"]

    def test_a_claim_rewritten_while_its_name_was_retaken_is_quarantined_and_marked(self):
        _task(self.tasks, "task-a.txt", "old")
        with self.rename_then_crash(), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        claim = self.recorded_claim()
        _replace(claim, "rewritten while claimed")
        _task(self.tasks, "task-a.txt", "a new ask")
        out = self.recover()
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn(f"quarantined as tasks/{claim.name}", out["errors"][0])
        self.assertIn("rewritten while claimed", claim.read_text())
        self.assertIn("a new ask", (self.tasks / "task-a.txt").read_text())
        self.assertTrue((self.tasks / (claim.name + tt.QUARANTINE_SUFFIX)).exists())
        self.assertEqual((self.notices(), self.archive_names()), ([], []))

    def test_a_claim_lost_with_no_archived_copy_is_reported_not_invented(self):
        _task(self.tasks, "task-a.txt", "A")
        with self.rename_then_crash(), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.recorded_claim().unlink()
        out = self.recover()
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("lost track of 'task-a.txt'", out["errors"][0])
        self.assertEqual(out["recovered"][0]["landed"], {})
        self.assertEqual(self.notices(), [])
        self.assertEqual(len(self.journals()), 1, "an unresolved journal is kept for the operator")

    def test_a_recovery_whose_publish_fails_keeps_the_journal_and_finishes_later(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("placed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.results.rmdir()
        self.results.write_text("not a directory")
        out = self.recover()
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("could not publish tasks/archive/.notice-", out["errors"][0])
        self.assertEqual(len(self.journals()), 1)
        self.results.unlink()
        self.results.mkdir()
        self.assertEqual(self.recover()["errors"], [])
        self.invariants(gen, "task: A", notice_committed=True)

    def test_a_journal_that_is_not_an_object_is_reported_and_left(self):
        self.archive.mkdir()
        bad = self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json"
        bad.write_text("[]")
        out = self.recover()
        self.assertIn("is unreadable; left for the operator", out["errors"][0])
        self.assertTrue(bad.exists())

    def test_a_held_journal_is_skipped_even_when_unreadable(self):
        import fcntl
        self.archive.mkdir()
        held = self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json"
        held.write_text("not json")
        fd = os.open(self.archive / f"{held.name}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = self.recover()
        self.assertEqual((out["recovered"], out["errors"]), ([], []))
        self.assertTrue(held.exists())

    def test_a_quarantined_dead_claim_is_never_swept(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("task: Z\n")
        (self.tasks / (stray.name + tt.QUARANTINE_SUFFIX)).write_text("")
        out = self.recover()
        self.assertEqual((out["swept"], out["errors"]), ([], []))
        self.assertTrue(stray.exists())
        self.assertFalse((self.tasks / "task-z.txt").exists())

    def test_a_claim_whose_pid_belongs_to_another_user_is_treated_as_live(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("task: Z\n")
        with mock.patch.object(tt.os, "kill", side_effect=PermissionError):
            out = self.recover()
        self.assertEqual((out["swept"], out["errors"]), ([], []))
        self.assertTrue(stray.exists())


class TestCrashWindows(Base):
    """Crash windows a later pass must finish without claiming more than
    durably landed, adopting another notice, or keeping two archive names."""

    crash_after, invariants = TestJournalRecovery.crash_after, TestJournalRecovery.invariants
    recover = TestJournalRecovery.recover

    def two_tasks(self):
        return _task(self.tasks, "task-a.txt", "A"), _task(self.tasks, "task-b.txt", "B")

    def all_notice_text(self) -> str:
        return "".join(p.read_text() for p in self.notices())

    def test_a_crash_after_the_first_of_two_placements_notices_only_what_landed(self):
        _, b = self.two_tasks()
        with self.crash_after("placed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt", "task-b.txt"], _naming)
        self.assertTrue(b.exists())
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(Path(out["recovered"][0]["result"]).read_text().splitlines()[0],
                         "archived: task-a.txt->task-a.txt")
        self.assertNotIn("task-b.txt", self.all_notice_text(), "B is still live")
        again = self.run_transition(["task-b.txt"], _naming)
        self.assertEqual(again["errors"], [])
        self.assertEqual(self.all_notice_text().count("task-b.txt->"), 1, "B is reported once")

    def test_a_crash_after_the_first_claim_of_two_notices_only_the_claimed_one(self):
        p, b = self.two_tasks()
        with self.inject("after_claim", p, lambda: (_ for _ in ()).throw(Crash("killed"))), \
                self.assertRaises(Crash):
            self.run_transition(["task-a.txt", "task-b.txt"], _naming)
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.assertNotIn("task-b.txt", self.all_notice_text())
        self.assertTrue(b.exists())

    def test_a_crash_after_a_name_collision_never_adopts_the_other_notice(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        foreign = self.results / f"{PREFIX}1-1-foreign.txt"
        foreign.write_text("FOREIGN NOTICE\n")
        real_mint, real_publish = tt._mint_name, tt._publish_at
        names = iter([foreign.name])

        def collide_then_die(*a, **kw):
            try:
                return real_publish(*a, **kw)
            except FileExistsError:
                raise Crash("killed after the collision") from None

        with mock.patch.object(tt, "_mint_name", side_effect=lambda *a: next(names, None) or real_mint(*a)), \
                mock.patch.object(tt, "_publish_at", side_effect=collide_then_die), \
                self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(json.loads(self.journals()[0].read_text())["result"], foreign.name)
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        mine = Path(out["recovered"][0]["result"])
        self.assertNotEqual(mine.name, foreign.name)
        self.assertIn("archived: task-a.txt->task-a.txt", mine.read_text())
        self.assertEqual(foreign.read_text(), "FOREIGN NOTICE\n")
        self.assertEqual(tt.generation_of(self.archive / "task-a.txt"), gen)
        self.assertEqual((len(self.notices()), self.journals()), (2, []))

    def test_a_failed_fallback_rewrite_after_every_name_collided_is_still_published(self):
        _task(self.tasks, "task-a.txt", "A")
        foreign = self.results / f"{PREFIX}1-1-foreign.txt"
        foreign.write_text("FOREIGN NOTICE\n")
        real_set = tt._Journal.set

        def set_failing_the_fallback(self_, **fields):
            if fields == {"stage": "placed", "result": None}:
                raise OSError(5, "I/O error")
            real_set(self_, **fields)

        with mock.patch.object(tt, "_mint_name", return_value=foreign.name), \
                mock.patch.object(tt._Journal, "set", set_failing_the_fallback):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertIn("could not reserve a result name", out["errors"][0])
        self.assertEqual(json.loads(self.journals()[0].read_text())["result"], foreign.name)
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn("archived: task-a.txt->task-a.txt", Path(again["recovered"][0]["result"]).read_text())
        self.assertEqual(foreign.read_text(), "FOREIGN NOTICE\n")
        self.assertEqual(self.journals(), [])

    def test_a_crash_between_the_archive_link_and_the_claim_unlink_keeps_one_name(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        fired = []

        def at(where, name):
            if where == "after_link" and not fired:
                fired.append(name)
                raise Crash("killed between link and unlink")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertEqual(os.stat(self.archive / "task-a.txt").st_nlink, 2, "claim and landing share it")
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(self.archive_names(), ["task-a.txt"])
        self.assertEqual(self.names_outside_retired(self.archive / "task-a.txt"), 1)
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.invariants(gen, "task: A", notice_committed=True)

    def test_the_landing_is_fsynced_before_the_claim_is_unlinked(self):
        _task(self.tasks, "task-a.txt", "A")
        archive_ino, events = None, []
        real_sync = tt.task_archive._fsync_dir

        def sync(fd):
            events.append(("fsync", os.fstat(fd).st_ino))
            real_sync(fd)

        def at(where, name):
            if where == "after_link":
                events.append(("unlink-next", name))

        self.archive.mkdir()
        archive_ino = os.stat(self.archive).st_ino
        with mock.patch.object(tt.task_archive, "_fsync_dir", side_effect=sync), \
                mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        i = next(k for k, e in enumerate(events) if e[0] == "unlink-next")
        self.assertEqual(events[i - 1], ("fsync", archive_ino), events)

    def test_a_placement_past_a_gap_in_the_n_sequence_is_found(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("older\n")
        (self.archive / "task-a.txt.1").write_text("older still\n")
        real_place = tt._place

        def place_then_die(*a, **kw):
            real_place(*a, **kw)
            raise Crash("killed before the landing was recorded")

        with mock.patch.object(tt, "_place", side_effect=place_then_die), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        (self.archive / "task-a.txt.1").unlink()  # the older record was requeued meanwhile
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt.2"})
        self.assertIn("task-a.txt is archived as tasks/archive/task-a.txt.2",
                      Path(out["recovered"][0]["result"]).read_text())
        self.assertEqual(tt.generation_of(self.archive / "task-a.txt.2"), gen)
        self.assertEqual(self.journals(), [])

    def crash_at_publishing(self, foreign_name: str | None = None) -> Path:
        """One task archived, the pass killed with its notice reserved and unpublished."""
        _task(self.tasks, "task-a.txt", "A")
        with self.crash_after("publishing"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        return self.results / json.loads(self.journals()[0].read_text())["result"]

    def test_a_recovered_pass_with_nothing_to_say_publishes_nothing(self):
        _task(self.tasks, "task-a.txt", "A")
        with self.crash_after("placed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _notice(None))
        out = self.recover()
        self.assertEqual((out["errors"], out["recovered"][0]["result"]), ([], None))
        self.assertEqual((self.notices(), self.journals()), ([], []))
        self.assertEqual(self.archive_names(), ["task-a.txt"])

    def test_a_name_taken_during_the_recovery_publish_is_judged_by_its_bytes(self):
        reserved = self.crash_at_publishing()
        real = tt._publish_at
        calls = []

        def taken_first(results_dir, name, body, *rest):
            calls.append(name)
            if len(calls) == 1:
                (results_dir / name).write_text("FOREIGN NOTICE\n")
                raise FileExistsError(name)
            return real(results_dir, name, body, *rest)

        with mock.patch.object(tt, "_publish_at", side_effect=taken_first):
            out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(reserved.read_text(), "FOREIGN NOTICE\n")
        mine = Path(out["recovered"][0]["result"])
        self.assertNotEqual(mine, reserved)
        self.assertEqual(_unref(mine.read_text()), "archived: task-a.txt->task-a.txt\n")
        self.assertEqual(self.journals(), [])

    def test_a_failed_recovery_publish_keeps_the_journal(self):
        self.crash_at_publishing()
        with mock.patch.object(tt, "_publish_at", side_effect=OSError(28, "No space left on device")):
            out = self.recover()
        self.assertIn("could not publish tasks/archive/.notice-", out["errors"][0])
        self.assertEqual((self.notices(), len(self.journals())), ([], 1))
        self.assertEqual(self.recover()["errors"], [])
        self.assertEqual(len(self.notices()), 1)

    def test_recovery_that_finds_every_name_foreign_keeps_the_journal(self):
        reserved = self.crash_at_publishing()
        reserved.write_text("FOREIGN NOTICE\n")
        with mock.patch.object(tt, "_mint_name", return_value=reserved.name):
            out = self.recover()
        self.assertIn("could not reserve a result name", out["errors"][0])
        self.assertEqual(reserved.read_text(), "FOREIGN NOTICE\n")
        self.assertEqual(len(self.journals()), 1)
        again = self.recover()
        self.assertEqual(again["errors"], [])
        self.assertEqual(_unref(Path(again["recovered"][0]["result"]).read_text()),
                         "archived: task-a.txt->task-a.txt\n")

    def test_a_foreign_notice_with_an_identical_body_is_not_adopted(self):
        """Two transactions can render the same text; only the transaction id
        the notice ends with says whose it is."""
        reserved = self.crash_at_publishing()
        reserved.write_text("archived: task-a.txt->task-a.txt\n")  # same text, not this txn's
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        mine = Path(out["recovered"][0]["result"])
        self.assertNotEqual(mine, reserved)
        txn = None if not self.notices() else mine.read_text().rstrip().rsplit("(ref ", 1)[-1][:-1]
        self.assertTrue(txn and txn not in reserved.read_text())
        self.assertEqual(_unref(mine.read_text()), "archived: task-a.txt->task-a.txt\n")
        self.assertEqual(reserved.read_text(), "archived: task-a.txt->task-a.txt\n")
        self.assertEqual(self.journals(), [])

    def crash_with_b_linked_at_2(self):
        """A landed; B's claim linked into the archive at `.2` (`task-b.txt` and
        `.1` held older records) and the pass killed before the claim unlink."""
        _task(self.tasks, "task-a.txt", "A")
        b = _task(self.tasks, "task-b.txt", "B")
        gen_b = tt.generation_of(b)
        self.archive.mkdir()
        (self.archive / "task-b.txt").write_text("older\n")
        (self.archive / "task-b.txt.1").write_text("older still\n")

        def at(where, name):
            if where == "after_link" and name.startswith("task-b.txt.claim-"):
                raise Crash("killed between B's link and its claim unlink")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), self.assertRaises(Crash):
            self.run_transition(["task-a.txt", "task-b.txt"], _naming)
        return gen_b

    def test_one_recovery_resolves_a_gap_a_duplicate_claim_and_a_same_text_foreign_notice(self):
        gen_b = self.crash_with_b_linked_at_2()
        claim = next(x for x in self.tasks.iterdir() if x.name.startswith("task-b.txt.claim-"))
        self.assertEqual(os.stat(claim).st_ino, os.stat(self.archive / "task-b.txt.2").st_ino)
        (self.archive / "task-b.txt.1").unlink()  # an older record requeued meanwhile
        expected = ("archived: task-a.txt->task-a.txt, task-b.txt->task-b.txt\n\n"
                    "(Published by a later pass after an interrupted one.)\n"
                    "task-b.txt is archived as tasks/archive/task-b.txt.2.\n")
        foreign = self.results / f"{PREFIX}1-1-foreign.txt"
        foreign.write_text(expected)
        real_mint, names = tt._mint_name, iter([foreign.name])
        with mock.patch.object(tt, "_mint_name", side_effect=lambda *a: next(names, None) or real_mint(*a)):
            out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(out["recovered"][0]["landed"],
                         {"task-a.txt": "task-a.txt", "task-b.txt": "task-b.txt.2"})
        self.assertEqual(self.archive_names(), ["task-a.txt", "task-b.txt", "task-b.txt.2"], "no .3")
        self.assertFalse(claim.exists())
        self.assertEqual(self.names_outside_retired(self.archive / "task-b.txt.2"), 1)
        self.assertEqual(tt.generation_of(self.archive / "task-b.txt.2"), gen_b)
        mine = Path(out["recovered"][0]["result"])
        self.assertNotEqual(mine, foreign)
        self.assertEqual(_unref(mine.read_text()), expected)
        self.assertEqual(foreign.read_text(), expected)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.recover()["recovered"], [])
        self.assertEqual(len(self.notices()), 2)

    def test_a_notice_waits_while_a_claimed_task_is_unresolved_then_names_it(self):
        self.crash_with_b_linked_at_2()
        claim = next(x for x in self.tasks.iterdir() if x.name.startswith("task-b.txt.claim-"))
        hidden = Path(self.tmp.name) / "hidden"
        os.rename(self.archive / "task-b.txt.2", hidden)
        claim.unlink()  # B is nowhere this pass can see
        first = self.recover()
        self.assertIn("lost track of 'task-b.txt'", first["errors"][0])
        self.assertEqual((self.notices(), len(self.journals())), ([], 1), "no A-only notice")
        os.rename(hidden, self.archive / "task-b.txt.2")
        second = self.recover()
        self.assertEqual(second["errors"], [], second)
        text = _unref(Path(second["recovered"][0]["result"]).read_text())
        self.assertIn("task-a.txt->task-a.txt", text)
        self.assertIn("task-b.txt is archived as tasks/archive/task-b.txt.2.", text)
        self.assertEqual((len(self.notices()), self.journals()), (1, []))

    def test_a_notice_retired_by_its_consumer_is_not_published_again(self):
        """The bridge claims a proactive result by renaming it to `.sending.<pid>` and,
        once delivered, renames that into results/archive/ (remote_gateway_bridge's
        drain). A publisher killed before its journal went must not send it twice."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("published"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        (live,) = self.notices()
        claim = live.with_suffix(f".sending.{os.getpid()}")
        live.rename(claim)
        (self.results / "archive").mkdir()
        delivered = self.results / "archive" / f"{live.stem}-1800000000.txt"
        claim.rename(delivered)
        out = self.recover()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(Path(out["recovered"][0]["result"]).name, live.name)
        self.assertEqual(self.notices(), [], "no live result: nothing to deliver again")
        self.assertEqual([x.name for x in (self.results / "archive").iterdir()], [delivered.name])
        self.assertEqual([x.name for x in self.results.iterdir() if x.name.startswith(".")], [])
        self.assertEqual(self.journals(), [])
        self.assertEqual(tt.generation_of(self.archive / "task-a.txt"), gen)
        self.assertEqual(self.recover()["recovered"], [])

    def test_an_archive_fsync_failure_after_the_link_keeps_one_name_and_the_journal(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.archive.mkdir()
        archive_ino, fired = os.stat(self.archive).st_ino, []
        real_sync = tt.task_archive._fsync_dir

        def sync(fd):
            if (not fired and os.fstat(fd).st_ino == archive_ino
                    and (self.archive / "task-a.txt").exists()):
                fired.append(1)
                raise OSError(5, "Input/output error")
            real_sync(fd)

        with mock.patch.object(tt.task_archive, "_fsync_dir", side_effect=sync):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertEqual((out["archived"], out["result"]), ({}, None))
        self.assertIn("is linked as tasks/archive/task-a.txt but not yet durable", out["errors"][0])
        self.assertFalse(p.exists(), "a landed file is not given back under its live name")
        (journal,) = self.journals()
        self.assertEqual(json.loads(journal.read_text())["unsynced"], {"task-a.txt": "task-a.txt"})
        retry = self.recover()
        self.assertEqual(retry["errors"], [], retry)
        self.assertEqual(self.archive_names(), ["task-a.txt"], "one generation, one archive name")
        self.assertEqual(self.names_outside_retired(self.archive / "task-a.txt"), 1)
        self.invariants(gen, "task: A", notice_committed=True)

    def test_a_replaced_claim_entry_is_kept_and_the_journal_retained(self):
        p = _task(self.tasks, "task-a.txt", "A")
        fired = []

        def at(where, name):
            if where == "after_link" and not fired:
                fired.append(name)
                raise Crash("killed between link and unlink")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        claim = self.tasks / fired[0]
        _replace(claim, "not the claimed bytes")
        self.assertNotEqual(os.stat(claim).st_ino, os.stat(self.archive / "task-a.txt").st_ino)
        out = self.recover()
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn(f"tasks/{claim.name} now holds a different file", out["errors"][0])
        self.assertIn("not the claimed bytes", claim.read_text())
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(self.notices(), [])

    def test_a_witness_left_by_a_crash_after_the_journal_went_is_swept(self):
        _task(self.tasks, "task-a.txt", "A")
        real_drop = tt._drop_witness
        with mock.patch.object(tt, "_drop_witness", side_effect=Crash("killed")), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(self.journals(), [])
        self.assertEqual(len([x for x in self.results.iterdir() if x.name.endswith(".witness")]), 1)
        import fcntl
        live = self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json"  # a live pass's, mid-publish
        live.write_text(json.dumps({"txn": "keepme", "moves": {}, "body": None}))
        fd = os.open(self.archive / f"{live.name}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        keep = self.results / ".proactive-x.txt.keepme.witness"
        keep.write_text("x")
        self.recover()
        self.assertEqual([x.name for x in self.results.iterdir() if x.name.endswith(".witness")],
                         [keep.name], "only a journal's own witness survives")
        self.assertTrue(real_drop)
        self.assertEqual(len(self.notices()), 1)

    def test_a_refusal_after_the_claim_with_the_name_retaken_quarantines_the_claim(self):
        p = _task(self.tasks, "task-a.txt", "old")
        seen = []

        def safe(name, read):
            seen.append(read)
            return "held by worker w1" if read != name else None

        with self.inject("after_claim", p, lambda: _task(self.tasks, "task-a.txt", "new")):
            out = self.run_transition(["task-a.txt"], _naming, still_safe=safe)
        self.assertEqual(seen[0], "task-a.txt")
        claim = seen[1]
        self.assertEqual(out["quarantined"], {"task-a.txt": claim})
        self.assertIn(f"quarantined as tasks/{claim}", out["errors"][0])
        self.assertIn("task: old", (self.tasks / claim).read_text())
        self.assertIn("task: new", p.read_text())
        self.assertTrue((self.tasks / (claim + tt.QUARANTINE_SUFFIX)).exists())
        self.assertEqual((out["archived"], self.notices(), self.journals()), ({}, [], []))

    def test_still_safe_refuses_a_file_right_before_its_claim(self):
        p, b = self.two_tasks()
        out = self.run_transition(["task-a.txt", "task-b.txt"], _naming,
                                  still_safe=lambda n, _f: "held by worker w1" if n == "task-b.txt" else None)
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertEqual(out["errors"], ["refused to move 'task-b.txt': held by worker w1"])
        self.assertTrue(b.exists())
        self.assertNotIn("task-b.txt", self.all_notice_text())


class TestRound9(Base):
    """Ninth-round blockers (#4399): each drives the production transition on a
    real filesystem with the fault injected at the named seam."""

    crash_after, invariants = TestJournalRecovery.crash_after, TestJournalRecovery.invariants
    recover = TestJournalRecovery.recover

    def fsyncs(self):
        """Record os.fsync by the inode it synced, and os.unlink by name, in order."""
        events, real_fsync, real_unlink = [], os.fsync, os.unlink

        def fsync(fd):
            events.append(("fsync", os.fstat(fd).st_ino))
            return real_fsync(fd)

        def unlink(path, *a, **kw):
            events.append(("unlink", os.path.basename(str(path))))
            return real_unlink(path, *a, **kw)

        return events, mock.patch.object(os, "fsync", side_effect=fsync), \
            mock.patch.object(os, "unlink", side_effect=unlink)

    # 3 — a failed give-back never reverses the refusal
    def refused_after_claim(self, failing_give_back):
        p = _task(self.tasks, "task-a.txt", "A")
        with mock.patch.object(tt.task_archive, "give_back", side_effect=failing_give_back):
            try:
                out = self.run_transition(["task-a.txt"], _naming,
                                          still_safe=lambda n, read: "now done" if read != n else None)
            except OSError as exc:  # a pass that does not journal its refusal dies here
                out = {"errors": [str(exc)], "archived": {}}
        return p, out

    def test_a_give_back_whose_link_fails_is_returned_by_recovery_not_archived(self):
        def link_fails(claim, name, why, dir_fd, **kw):
            raise OSError(5, "Input/output error")

        p, out = self.refused_after_claim(link_fails)
        self.assertEqual(out["archived"], {})
        self.assertFalse(p.exists())
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn("task: A", p.read_text())
        self.assertEqual((self.archive_names(), self.notices(), self.journals()), ([], [], []))
        self.assertEqual([x.name for x in self.tasks.iterdir() if ".claim-" in x.name], [])

    def test_a_give_back_whose_unlink_fails_keeps_one_live_copy_and_no_notice(self):
        def unlink_fails(claim, name, why, dir_fd, **kw):
            os.link(claim, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            raise OSError(5, "Input/output error")

        p, out = self.refused_after_claim(unlink_fails)
        self.assertTrue(p.exists())
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn("task: A", p.read_text())
        self.assertEqual((self.archive_names(), self.notices(), self.journals()), ([], [], []))
        self.assertEqual([x.name for x in self.tasks.iterdir() if ".claim-" in x.name], [])

    # 4 — placement moves only the inode it judged
    def test_a_claim_replaced_between_judgement_and_move_is_never_archived(self):
        p = _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        real = tt.task_archive.move_without_clobbering

        def replace_then_move(src, dest, **kw):
            _replace(self.tasks / str(src), "REPLACEMENT BYTES")
            return real(src, dest, **kw)

        with mock.patch.object(tt.task_archive, "move_without_clobbering", side_effect=replace_then_move):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["archived"], {})
        self.assertIn("was replaced after it was judged", out["errors"][0])
        self.assertEqual(self.archive_names(), [])
        kept = [x for x in self.tasks.iterdir() if x.name.endswith(".displaced")]
        self.assertEqual(len(kept), 1)
        self.assertIn("ORIGINAL ASK", kept[0].read_text())
        self.assertEqual((self.notices(), len(self.journals())), ([], 1))

    def test_a_replaced_claim_is_never_given_back_or_cleared_by_later_passes(self):
        p = _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        real = tt.task_archive.move_without_clobbering
        held = []

        def replace_then_move(src, dest, **kw):
            held.append(str(src))
            _replace(self.tasks / str(src), "REPLACEMENT BYTES")
            return real(src, dest, **kw)

        with mock.patch.object(tt.task_archive, "move_without_clobbering", side_effect=replace_then_move):
            self.run_transition(["task-a.txt"], _naming)
        claim = self.tasks / held[0]
        for _ in range(2):
            out = self.recover()
            self.assertEqual(len(out["errors"]), 1, out)
            self.assertIn(f"kept as tasks/{claim.name}.displaced", out["errors"][0])
            self.assertIn("waits for the operator", out["errors"][0])
            self.assertFalse(p.exists(), "the replacement is never promoted to a live task")
            self.assertIn("REPLACEMENT BYTES", claim.read_text())
            self.assertIn("ORIGINAL ASK", (self.tasks / f"{claim.name}.displaced").read_text())
            self.assertEqual((len(self.journals()), self.notices(), self.archive_names()), (1, [], []))
        self.assertTrue((self.tasks / (claim.name + tt.QUARANTINE_SUFFIX)).exists())

    # 5 — an unreadable witness keeps the journal
    def retired_notice(self):
        p = _task(self.tasks, "task-a.txt", "A")
        with self.crash_after("published"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        (live,) = self.notices()
        claim = live.with_suffix(f".sending.{os.getpid()}")
        live.rename(claim)
        (self.results / "archive").mkdir()
        claim.rename(self.results / "archive" / f"{live.stem}-1800000000.txt")
        return p, live

    def test_an_unreadable_witness_is_not_read_as_absent(self):
        _, live = self.retired_notice()
        real_lstat = os.lstat

        def lstat(path, *a, **kw):
            if str(path).endswith(".witness"):
                raise OSError(5, "Input/output error")
            return real_lstat(path, *a, **kw)

        with mock.patch.object(os, "lstat", side_effect=lstat):
            out = self.recover()
        self.assertTrue(out["errors"], out)
        self.assertEqual(self.notices(), [], "nothing recreated")
        self.assertEqual(len(self.journals()), 1, "the journal waits for a readable witness")
        clean = self.recover()
        self.assertEqual(clean["errors"], [], clean)
        self.assertEqual((self.notices(), self.journals()), ([], []))

    # 6 — journal removal is durable before the witness goes
    def test_the_journal_removal_is_fsynced_before_the_witness_is_unlinked(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir()
        archive_ino, results_ino = os.stat(self.archive).st_ino, os.stat(self.results).st_ino
        events, p_fsync, p_unlink = self.fsyncs()
        with p_fsync, p_unlink:
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        j = next(i for i, e in enumerate(events) if e[0] == "unlink" and e[1].startswith(tt.JOURNAL_PREFIX)
                 and e[1].endswith(".json"))
        w = next(i for i, e in enumerate(events) if e[0] == "unlink" and e[1].endswith(".witness"))
        self.assertIn(("fsync", archive_ino), events[j:w], events)
        self.assertIn(("fsync", results_ino), events[w:], events)

    # 7 — recovery puts a barrier under a commit whose directory fsync failed
    def test_a_commit_whose_results_fsync_failed_is_synced_before_recovery_accepts_it(self):
        _task(self.tasks, "task-a.txt", "A")
        results_ino, fired, real_fsync = os.stat(self.results).st_ino, [], os.fsync

        def fsync(fd):
            if not fired and os.fstat(fd).st_ino == results_ino:
                fired.append(1)
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        with mock.patch.object(os, "fsync", side_effect=fsync):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertIn("result write failed", out["errors"][0])
        self.assertEqual(len(self.journals()), 1)
        events, p_fsync, p_unlink = self.fsyncs()
        with p_fsync, p_unlink:
            again = self.recover()
        self.assertEqual(again["errors"], [], again)
        j = next(i for i, e in enumerate(events) if e[0] == "unlink" and e[1].endswith(".json"))
        self.assertIn(("fsync", results_ino), events[:j], "a barrier before the journal goes")
        self.assertEqual((len(self.notices()), self.journals()), (1, []))

    # 8 — recovery honours an unsynced placement
    def test_an_unsynced_claim_removal_is_synced_before_recovery_publishes(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        tasks_ino, fired, real = os.stat(self.tasks).st_ino, [], tt.task_archive._fsync_dir

        def sync(fd):
            if (not fired and os.fstat(fd).st_ino == tasks_ino and (self.archive / "task-a.txt").exists()
                    and not any(".claim-" in x.name for x in self.tasks.iterdir())):
                fired.append(1)
                raise OSError(5, "Input/output error")
            real(fd)

        with mock.patch.object(tt.task_archive, "_fsync_dir", side_effect=sync):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertEqual((out["result"], len(self.journals())), (None, 1))
        events, p_fsync, p_unlink = self.fsyncs()
        with p_fsync, p_unlink:
            again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn(("fsync", tasks_ino), events, "the claim's removal is made durable")
        self.invariants(gen, "task: A", notice_committed=True)

    # coverage of the same contracts' failure branches
    def test_a_journal_removal_whose_barrier_fails_keeps_the_witness_and_sends_nothing_twice(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir()
        archive_ino, real = os.stat(self.archive).st_ino, tt.task_archive._fsync_dir
        fired = []

        def sync(fd):
            if (not fired and os.fstat(fd).st_ino == archive_ino and any(
                    p.name.endswith(".witness") for p in self.results.iterdir()) and not self.journals()):
                fired.append(1)
                raise OSError(5, "Input/output error")
            real(fd)

        with mock.patch.object(tt.task_archive, "_fsync_dir", side_effect=sync):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertIn("could not be removed durably", out["errors"][0])
        self.assertEqual(len([p for p in self.results.iterdir() if p.name.endswith(".witness")]), 1)
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertEqual(len(self.notices()), 1, "no second notice")
        self.assertEqual([p for p in self.results.iterdir() if p.name.endswith(".witness")], [])

    def failed_give_back(self):
        def link_fails(claim, name, why, dir_fd, **kw):
            raise OSError(5, "Input/output error")

        p, _ = self.refused_after_claim(link_fails)
        return p, next(x for x in self.tasks.iterdir() if ".claim-" in x.name)

    def test_a_journaled_return_whose_name_was_retaken_quarantines_the_claim(self):
        p, claim = self.failed_give_back()
        _task(self.tasks, "task-a.txt", "a new ask")
        out = self.recover()
        self.assertIn(f"quarantined as tasks/{claim.name}", out["errors"][0])
        self.assertIn("task: A", claim.read_text())
        self.assertIn("a new ask", p.read_text())
        self.assertTrue((self.tasks / (claim.name + tt.QUARANTINE_SUFFIX)).exists())
        self.assertEqual((self.archive_names(), self.notices(), self.journals()), ([], [], []))

    def test_a_journaled_return_with_both_names_gone_keeps_the_journal(self):
        _, claim = self.failed_give_back()
        claim.unlink()
        out = self.recover()
        self.assertIn("lost track of 'task-a.txt': it was refused", out["errors"][0])
        self.assertEqual((self.notices(), len(self.journals())), ([], 1))

    def test_recovery_effects_reads_unreadable_journals_and_missing_dirs(self):
        self.assertEqual(tt.recovery_effects(self.ws / "nowhere", self.ws / "nowhere" / "archive"),
                         {"journals": {}, "sweep": {}})
        self.assertEqual(tt.recovery_effects(self.tasks, self.archive), {"journals": {}, "sweep": {}})
        self.archive.mkdir()
        (self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json").write_text("not json")
        self.assertEqual(tt.recovery_effects(self.tasks, self.archive),
                         {"journals": {f"{tt.JOURNAL_PREFIX}1-2-abc.json": "unreadable"}, "sweep": {}})


class TestRound10(Base):
    """Tenth-round blockers (#4399), each on the production transition."""

    crash_after, invariants = TestJournalRecovery.crash_after, TestJournalRecovery.invariants
    recover = TestJournalRecovery.recover

    # 2 — the claim path is never unlinked once it holds a replacement
    def test_a_claim_replaced_after_the_archive_link_is_kept_not_deleted(self):
        p = _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        fired = []

        def at(where, name):
            if where == "after_link" and not fired:
                fired.append(name)
                _replace(self.tasks / name, "REPLACEMENT BYTES")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(fired)
        self.assertEqual((out["archived"], out["result"]), ({}, None))
        self.assertIn("its claim name was replaced meanwhile", out["errors"][0])
        self.assertIn("ORIGINAL ASK", (self.archive / "task-a.txt").read_text())
        kept = [x for x in self.tasks.rglob("*") if x.is_file() and "REPLACEMENT BYTES" in x.read_text()]
        self.assertEqual(len(kept), 1, "the replacement bytes still exist")
        self.assertFalse(p.exists())
        again = self.recover()
        self.assertIn("waits for the operator", again["errors"][0])
        self.assertEqual((len(self.journals()), self.notices()), (1, []))
        self.assertEqual(len([x for x in self.tasks.rglob("*") if x.is_file()
                              and "REPLACEMENT BYTES" in x.read_text()]), 1)

    # 3 — a refusal whose journal write fails is still never placed
    def refused_with_failed_persist(self, give_back=None):
        p = _task(self.tasks, "task-a.txt", "A")
        real_set = tt._Journal.set

        def set_failing_refusal(self_, **fields):
            if fields.get("stage") == "refusing":
                raise OSError(5, "Input/output error")
            real_set(self_, **fields)

        patches = [mock.patch.object(tt._Journal, "set", set_failing_refusal)]
        if give_back:
            patches.append(mock.patch.object(tt.task_archive, "give_back", side_effect=give_back))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                self.run_transition(["task-a.txt"], _naming,
                                    still_safe=lambda n, read: "now done" if read != n else None)
            except OSError:
                pass  # a pass that lets the failed write escape dies here
        return p

    def test_a_refusal_whose_journal_write_fails_is_returned_not_archived(self):
        p = self.refused_with_failed_persist()
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn("task: A", p.read_text())
        self.assertEqual((self.archive_names(), self.notices(), self.journals()), ([], [], []))

    def test_a_refusal_whose_journal_write_and_give_back_both_fail_is_returned_by_recovery(self):
        def link_fails(claim, name, why, dir_fd, **kw):
            raise OSError(5, "Input/output error")

        p = self.refused_with_failed_persist(link_fails)
        self.assertFalse(p.exists())
        again = self.recover()
        self.assertEqual(again["errors"], [], again)
        self.assertIn("task: A", p.read_text())
        self.assertEqual((self.archive_names(), self.notices(), self.journals()), ([], [], []))
        self.assertEqual([x.name for x in self.tasks.iterdir() if ".claim-" in x.name], [])

    # 1 — an approved sweep moves only the generation it approved
    def test_an_approved_dead_claim_replaced_before_the_sweep_is_refused(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("APPROVED BYTES\n")
        self.archive.mkdir()
        approved = tt.recovery_effects(self.tasks, self.archive)
        _replace(stray, "UNAPPROVED REPLACEMENT")
        out = tt.transition(self.tasks, self.results, self.archive, moves={}, render=_notice(None),
                            prefix=PREFIX, authorized=approved)
        self.assertIn("recovery state changed", out["errors"][0])
        self.assertEqual(out["swept"], [])
        self.assertFalse((self.tasks / "task-z.txt").exists())
        self.assertIn("UNAPPROVED REPLACEMENT", stray.read_text())


class TestRound11(Base):
    """Eleventh-round blockers (#4399): one retirement contract for every name this
    code removes, and an approved sweep bound to the full generation."""

    crash_after, invariants = TestJournalRecovery.crash_after, TestJournalRecovery.invariants
    recover = TestJournalRecovery.recover

    def replacement_survives(self) -> bool:
        return any(x.is_file() and "REPLACEMENT" in x.read_text() for x in self.tasks.rglob("*"))

    def claim_of(self, name="task-a.txt") -> str:
        return next(x.name for x in self.tasks.iterdir() if x.name.startswith(f"{name}.claim-"))

    # 1 — an in-place rewrite of an approved dead claim is never made live
    def test_an_approved_claim_rewritten_in_place_before_the_sweep_link_is_not_made_live(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("task: APPROVED BYTES\n")
        self.archive.mkdir()
        approved = tt.recovery_effects(self.tasks, self.archive)
        ino, real_link = os.stat(stray).st_ino, os.link

        def rewrite_then_link(src, dst, *a, **kw):
            if str(src) == stray.name:
                with open(stray, "r+") as fh:  # same inode, new size and mtime
                    fh.write("task: UNAPPROVED IN-PLACE REWRITE\n")
            return real_link(src, dst, *a, **kw)

        with mock.patch.object(os, "link", side_effect=rewrite_then_link):
            out = tt.transition(self.tasks, self.results, self.archive, moves={}, render=_notice(None),
                                prefix=PREFIX, authorized=approved)
        self.assertEqual(os.stat(stray).st_ino, ino)
        self.assertEqual(out["swept"], [])
        self.assertIn("changed since it was approved", out["errors"][0])
        self.assertFalse((self.tasks / "task-z.txt").exists(), "the rewrite is not made live")
        self.assertIn("UNAPPROVED IN-PLACE REWRITE", stray.read_text())

    # 2 — crash right after the claim name is retired, with a replacement in it
    def test_a_crash_after_retiring_a_replaced_claim_keeps_the_replacement_and_the_journal(self):
        _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        fired, real_rename = [], os.rename

        def at(where, name):
            if where == "after_link" and not fired:
                fired.append(name)
                _replace(self.tasks / name, "REPLACEMENT BYTES")
            elif where == "after_retire" and fired and name == fired[0]:
                raise Crash("killed right after the claim name was retired")

        def rename_then_die(src, dst, *a, **kw):
            real_rename(src, dst, *a, **kw)
            if fired and str(src) == fired[0]:
                raise Crash("killed right after the claim name was retired")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), \
                mock.patch.object(os, "rename", side_effect=rename_then_die), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        out = self.recover()
        self.assertTrue(out["errors"], out)
        self.assertTrue(self.replacement_survives())
        self.assertEqual((self.notices(), len(self.journals())), ([], 1))
        self.assertIn("ORIGINAL ASK", (self.archive / "task-a.txt").read_text())

    def test_retiring_the_same_name_twice_keeps_both(self):
        self.archive.mkdir()
        dfd = os.open(self.tasks, os.O_RDONLY)
        afd = os.open(self.archive, os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        self.addCleanup(os.close, afd)
        for body in ("first\n", "second\n"):
            (self.tasks / "x.claim-1-ab").write_text(body)
            with tt.task_archive.retired_dir(afd) as rfd:
                self.assertIsNotNone(tt.task_archive.retire("x.claim-1-ab", src_dir_fd=dfd, retired_fd=rfd,
                                                            expect=None))
        retired = self.archive / tt.task_archive.RETIRED_DIR
        self.assertEqual(sorted((x.name, x.read_text()) for x in retired.iterdir()),
                         [("x.claim-1-ab", "first\n"), ("x.claim-1-ab.1", "second\n")])

    # 2 — the live refusal's give-back never deletes a raced replacement
    def test_a_claim_replaced_during_a_refusals_give_back_is_kept(self):
        p = _task(self.tasks, "task-a.txt", "A")
        real_link, done = os.link, []

        def link_then_replace(src, dst, *a, **kw):
            real_link(src, dst, *a, **kw)
            if str(dst) == "task-a.txt" and ".claim-" in str(src) and not done:
                done.append(1)
                _replace(self.tasks / str(src), "REPLACEMENT BYTES")

        with mock.patch.object(os, "link", side_effect=link_then_replace):
            out = self.run_transition(["task-a.txt"], _naming,
                                      still_safe=lambda n, read: "now done" if read != n else None)
        self.assertTrue(done)
        self.assertTrue(self.replacement_survives(), out)
        self.assertIn("task: A", p.read_text())
        self.assertEqual(self.archive_names(), [])

    # 2 — the journaled return never deletes a raced replacement
    def test_a_claim_replaced_during_a_journaled_return_is_kept(self):
        p = _task(self.tasks, "task-a.txt", "A")

        def link_fails(claim, name, why, dir_fd, **kw):
            raise OSError(5, "Input/output error")

        with mock.patch.object(tt.task_archive, "give_back", side_effect=link_fails):
            self.run_transition(["task-a.txt"], _naming,
                                still_safe=lambda n, read: "now done" if read != n else None)
        claim = self.claim_of()
        real_link, done = os.link, []

        def link_then_replace(src, dst, *a, **kw):
            real_link(src, dst, *a, **kw)
            if str(src) == claim and not done:
                done.append(1)
                _replace(self.tasks / claim, "REPLACEMENT BYTES")

        with mock.patch.object(os, "link", side_effect=link_then_replace):
            out = self.recover()
        self.assertTrue(done)
        self.assertTrue(out["errors"], out)
        self.assertTrue(self.replacement_survives())
        self.assertIn("task: A", p.read_text())
        self.assertEqual((self.archive_names(), self.notices()), ([], []))

    # 2 — the landed recovery never deletes a raced replacement
    def test_a_claim_replaced_during_a_landed_recovery_is_kept(self):
        _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        fired = []

        def at(where, name):
            if where == "after_link" and not fired:
                fired.append(name)
                raise Crash("killed between the link and the claim's retirement")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        claim, archive_ino = fired[0], os.stat(self.archive).st_ino
        real_sync, done = tt.task_archive._fsync_dir, []

        def sync_then_replace(fd):
            real_sync(fd)
            if not done and os.fstat(fd).st_ino == archive_ino and (self.tasks / claim).exists():
                done.append(1)
                _replace(self.tasks / claim, "REPLACEMENT BYTES")

        with mock.patch.object(tt.task_archive, "_fsync_dir", side_effect=sync_then_replace):
            out = self.recover()
        self.assertTrue(done)
        self.assertTrue(out["errors"], out)
        self.assertTrue(self.replacement_survives())
        self.assertEqual((self.notices(), len(self.journals())), ([], 1))

    # 2 — the placement's own source retirement never deletes a raced replacement
    def test_a_claim_replaced_between_the_retire_check_and_removal_is_kept(self):
        _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        real_lstat, done = os.lstat, []

        def lstat_then_replace(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if not done and ".claim-" in str(path) and kw.get("dir_fd") is not None and any(
                    (self.archive / "task-a.txt").exists() for _ in [0]):
                held = [x for x in self.tasks.rglob(f"{path}") if x.exists()]
                if held:
                    done.append(1)
                    _replace(held[0], "SECOND REPLACEMENT")
            return st

        with mock.patch.object(os, "lstat", side_effect=lstat_then_replace):
            self.run_transition(["task-a.txt"], _naming)
        self.assertTrue(any(x.is_file() and "SECOND REPLACEMENT" in x.read_text()
                            for x in self.tasks.rglob("*")), "the replacement is kept somewhere")


class TestRound12(Base):
    """Twelfth round (#4399): the late windows after each check."""

    recover = TestJournalRecovery.recover

    def test_a_dead_claim_rewritten_in_place_at_its_retirement_is_not_left_live(self):
        stray = self.tasks / "task-z.txt.claim-4000000000-abcdef0123"
        stray.write_text("task: APPROVED BYTES\n")
        self.archive.mkdir()
        approved = tt.recovery_effects(self.tasks, self.archive)
        done = []

        def at(where, name):
            if where == "before_retire" and name == stray.name and not done:
                done.append(1)
                with open(self.tasks / name, "r+") as fh:  # after the live check, same inode
                    fh.write("task: UNAPPROVED AFTER VALIDATION\n")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at):
            out = tt.transition(self.tasks, self.results, self.archive, moves={}, render=_notice(None),
                                prefix=PREFIX, authorized=approved)
        self.assertTrue(done)
        self.assertEqual(out["swept"], [])
        self.assertTrue(out["errors"], out)
        live = self.tasks / "task-z.txt"
        self.assertFalse(live.exists() and "UNAPPROVED" in live.read_text(), "the rewrite is not live")

    def test_a_retired_name_taken_between_choice_and_placement_is_never_overwritten(self):
        self.archive.mkdir()
        dfd, afd = os.open(self.tasks, os.O_RDONLY), os.open(self.archive, os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        self.addCleanup(os.close, afd)
        (self.tasks / "x.claim-1-ab").write_text("NEWLY RETIRED\n")
        retired = self.archive / getattr(tt.task_archive, "RETIRED_DIR", ".retired")
        done = []

        def at(where, name):
            if where == "before_retire" and not done:
                done.append(1)
                retired.mkdir(exist_ok=True)
                (retired / "x.claim-1-ab").write_text("EARLIER EVIDENCE\n")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at), \
                tt.task_archive.retired_dir(afd) as rfd:
            tt.task_archive.retire("x.claim-1-ab", src_dir_fd=dfd, retired_fd=rfd, expect=None)
        self.assertTrue(done)
        self.assertEqual(sorted(x.read_text() for x in retired.iterdir()),
                         ["EARLIER EVIDENCE\n", "NEWLY RETIRED\n"])


class TestRound13(Base):
    """Thirteenth round (#4399): the late landing swap, the FIFO landing, and a replaced
    refusal claim retired by a pass that died."""

    recover = TestJournalRecovery.recover

    def landing(self, body="landed\n"):
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text(body)
        dirs = tt._Dirs(self.tasks, self.archive)
        self.addCleanup(dirs.close)
        return dirs

    def test_a_landing_replaced_while_it_is_read_is_not_returned(self):
        dirs = self.landing("ORIGINAL\n")
        real_fstat, done = os.fstat, []

        def swap_after_the_first_fstat(fd):
            st = real_fstat(fd)
            if not done and stat.S_ISREG(st.st_mode) and fd not in (dirs.tasks_fd, dirs.archive_fd):
                done.append(1)
                _replace(self.archive / "task-a.txt", "REPLACEMENT")
            return st

        with mock.patch.object(os, "fstat", side_effect=swap_after_the_first_fstat):
            self.assertIsNone(tt._read_landing(dirs, "task-a.txt"))
        self.assertTrue(done)

    def test_a_landing_rewritten_in_place_while_it_is_read_is_not_returned(self):
        dirs = self.landing("ORIGINAL\n")
        real_fstat, done = os.fstat, []

        def rewrite_after_the_first_fstat(fd):
            st = real_fstat(fd)
            if not done and stat.S_ISREG(st.st_mode) and fd not in (dirs.tasks_fd, dirs.archive_fd):
                done.append(1)
                with open(self.archive / "task-a.txt", "a") as fh:
                    fh.write("more\n")
            return st

        with mock.patch.object(os, "fstat", side_effect=rewrite_after_the_first_fstat):
            self.assertIsNone(tt._read_landing(dirs, "task-a.txt"))

    def test_an_untouched_landing_reads_back(self):
        dirs = self.landing("fine\n")
        got = tt._read_landing(dirs, "task-a.txt")
        self.assertEqual(got[1], b"fine\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_landing_is_refused_without_blocking(self):
        self.archive.mkdir()
        os.mkfifo(self.archive / "task-a.txt")
        dirs = tt._Dirs(self.tasks, self.archive)
        self.addCleanup(dirs.close)
        out = []
        t = threading.Thread(target=lambda: out.append(tt._read_landing(dirs, "task-a.txt")), daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():  # unblock the stuck open so the process can exit
            fd = os.open(self.archive / "task-a.txt", os.O_RDWR)
            os.close(fd)
            t.join(2)
            self.fail("_read_landing blocked opening a FIFO")
        self.assertEqual(out, [None])

    def test_without_a_no_follow_primitive_a_landing_is_not_read(self):
        dirs = self.landing("fine\n")
        with mock.patch.object(tt, "_NOFOLLOW", 0):
            self.assertIsNone(tt._read_landing(dirs, "task-a.txt"))

    def test_a_replaced_refusal_claim_retired_by_a_dead_pass_keeps_the_journal_and_reports(self):
        _task(self.tasks, "task-a.txt", "ORIGINAL ASK")
        refuse = lambda n, read: "now done" if read != n else None  # noqa: E731
        with mock.patch.object(tt.task_archive, "give_back", side_effect=OSError(5, "give-back failed")):
            self.run_transition(["task-a.txt"], _naming, still_safe=refuse)
        self.assertEqual(len(self.journals()), 1)
        fired = []

        def at(where, name):
            if where == "before_retire" and ".claim-" in name and not fired:
                fired.append(name)
                _replace(self.tasks / name, "REPLACEMENT B")
            elif where == "after_retire" and fired and name == fired[0]:
                raise OSError(5, "the pass dies right after the claim name was retired")

        with mock.patch.object(tt.task_archive, "_checkpoint", side_effect=at):
            interrupted = self.recover()
        self.assertTrue(any("interrupted" in e for e in interrupted["errors"]), interrupted)
        self.assertTrue(fired)
        out = self.recover()
        self.assertTrue(any("replaced its claim" in e for e in out["errors"]), out)
        self.assertEqual(len(self.journals()), 1, "the journal waits for the operator")
        self.assertTrue(self.replacement_survives())

    def replacement_survives(self) -> bool:
        return any(x.is_file() and "REPLACEMENT" in x.read_text() for x in self.tasks.rglob("*"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
