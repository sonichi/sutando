#!/usr/bin/env python3
"""Run: python3 tests/core-limit-hold-lifecycle.test.py -v

Four bounded traces evaluate production hold commands against independent expected
records: replay, fresh-refusal backoff, reset/clear/reinstate, identity binding and
task retention. Every step checks readers and core/worker/workspace isolation.
Deadlines and strikes are specified by the traces, never learned from disk.
Temporary state, fixed clocks and explicit identities need no credentials or I/O
stubs. Concurrent writers, provider parsing and delivery are outside this proof.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import core_limit_hold as clh  # noqa: E402

EPOCH = 1_800_000_000.0
OPUS = "claude-opus-5-5"
ACCOUNT_A, ACCOUNT_B = "fixture-account-a", "fixture-account-b"
NORTH_CORE = ("north", None)
NORTH_WORKER = ("north", "fixture-worker")
SOUTH_CORE = ("south", None)
SOUTH_WORKER = ("south", "fixture-worker")
LANES = (NORTH_CORE, NORTH_WORKER, SOUTH_CORE, SOUTH_WORKER)
PROBES = {
    "same": (OPUS, ACCOUNT_A), "alias": ("opus", ACCOUNT_A),
    "other_model": ("sonnet", ACCOUNT_A), "other_account": ("opus", ACCOUNT_B),
    "unread_account": ("opus", None), "unread_model": (None, ACCOUNT_A),
    "unread_both": (None, None), "b_sonnet": ("sonnet", ACCOUNT_B),
    "b_unread_model": (None, ACCOUNT_B),
}
A_OPUS = frozenset(("same", "alias", "unread_account", "unread_model", "unread_both"))
B_OPUS = frozenset(("other_account", "unread_account", "unread_both", "b_unread_model"))
B_SONNET = frozenset(("unread_both", "b_sonnet", "b_unread_model"))
UNREAD_OPUS = frozenset(("unread_account", "unread_both"))
FIELDS = (
    "status", "kind", "model", "source", "reset_at", "evidence_at", "set_at",
    "until", "strikes", "account", "account_at", "account_unread", "launch_account",
    "started_at", "present_at_hold", "held_tasks", "seat", "seat_label",
    "released_at", "cleared_by", "reinstated_at",
)
UNCHANGED = object()


def at(offset):
    return EPOCH + offset


def refusal(offset, reset=None, model=OPUS, kind="rate-limit"):
    return clh.Limit(kind, at(offset), None if reset is None else at(reset), model, "lifecycle-fixture")


@dataclass(frozen=True)
class Expected:
    record: dict
    bindings: frozenset


def held(lane, evidence, opened, until, strikes=1, reset=None, model=OPUS,
         account=ACCOUNT_A, bindings=A_OPUS, started=None, present=(), tasks=(),
         account_at=None, kind="rate-limit"):
    record = {
        "status": "held", "kind": kind, "model": model, "source": "lifecycle-fixture",
        "reset_at": None if reset is None else at(reset), "evidence_at": at(evidence),
        "set_at": at(opened), "until": at(until), "strikes": strikes, "account": account,
        "account_at": None if account_at is None else at(account_at),
        "started_at": at(opened if started is None else started),
        "present_at_hold": list(present), "held_tasks": list(tasks),
    }
    if account is None:
        record["account_unread"] = True
    if lane[1] is not None:
        record.update(seat=lane[1], seat_label="Fixture worker")
    return Expected(record, bindings)


def changed(expected, remove=(), **updates):
    record = {key: value for key, value in expected.record.items() if key not in remove}
    record.update(updates)
    return Expected(record, expected.bindings)


def projection(record):
    return None if record is None else {key: record[key] for key in FIELDS if key in record}


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


class HoldLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hold-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspaces = {name: self.root / name for name in ("north", "south")}
        for workspace in self.workspaces.values():
            for directory in ("state", "tasks", "projects"):
                (workspace / directory).mkdir(parents=True)
        self.expected = {lane: None for lane in LANES}
        self.steps = 0

    def check_readers(self, offset):
        before = snapshot(self.root)
        for lane, expected in self.expected.items():
            workspace = self.workspaces[lane[0]]
            with self.subTest(step=self.steps, lane=lane, reader="read_hold"):
                self.assertEqual(projection(clh.read_hold(workspace, seat=lane[1])),
                                 None if expected is None else expected.record)
            active = (expected is not None and expected.record["status"] == "held"
                      and at(offset) < expected.record["until"])
            with self.subTest(step=self.steps, lane=lane, reader="active_hold"):
                self.assertEqual(projection(clh.active_hold(workspace, at(offset), seat=lane[1])),
                                 expected.record if active else None)
            if lane[1] is None:
                for name, identity in PROBES.items():
                    with self.subTest(step=self.steps, lane=lane, reader="binding_hold", identity=name):
                        self.assertEqual(projection(clh.binding_hold(workspace, at(offset), identity=identity)),
                                         expected.record if active and name in expected.bindings else None)
                with self.subTest(step=self.steps, lane=lane, reader="held_task_ids"):
                    tasks = clh.held_task_ids(workspace)
                    self.assertIsInstance(tasks, frozenset)
                    self.assertEqual(tasks, frozenset(expected.record["held_tasks"] if expected else ()))
        self.assertEqual(snapshot(self.root), before, "a read-only predicate changed fixture state")

    def step(self, command, lane, offset, expected=UNCHANGED, *, limit=None, launch=None,
             identity=(OPUS, ACCOUNT_A), by=None, task=None, returns_hold=True):
        self.steps += 1
        workspace = self.workspaces[lane[0]]
        before = {name: snapshot(path) for name, path in self.workspaces.items()}
        if command == "decide":
            result = clh.decide(workspace, at(offset), quota={}, projects_dir=workspace / "projects",
                                evidence=() if limit is None else (limit,), launch_model=identity[0],
                                launch_account=identity[1], seat=lane[1], seat_label="Fixture worker",
                                account_card=True)
            self.assertIs(result.launch, launch, (self.steps, lane, command))
            self.assertEqual(bool(result.line), not launch)
            returned = result.hold
        elif command in ("clear", "reinstate"):
            returned = getattr(clh, command)(workspace, at(offset), by=by, seat=lane[1])
        elif command == "account-change":
            returned = clh.release_on_account_change(workspace, at(offset), account=identity[1], seat=lane[1])
        elif command == "arrive":
            (workspace / "tasks" / (task + ".txt")).write_text("task: Fictional lifecycle input\n")
        elif command == "remove":
            (workspace / "tasks" / (task + ".txt")).unlink()
        elif command != "read":
            self.fail("unknown trace command: " + command)
        if expected is not UNCHANGED:
            self.expected[lane] = expected
        if command in ("decide", "clear", "reinstate", "account-change"):
            contract = self.expected[lane]
            self.assertEqual(projection(returned),
                             contract.record if returns_hold and contract is not None else None)
        after = {name: snapshot(path) for name, path in self.workspaces.items()}
        other_workspace = "south" if lane[0] == "north" else "north"
        self.assertEqual(after[other_workspace], before[other_workspace], "another workspace changed")
        for other_lane in LANES:
            if other_lane == lane or other_lane[0] != lane[0]:
                continue
            filename = "state/core-limit-hold" + ("." + other_lane[1] if other_lane[1] else "") + ".json"
            self.assertEqual(after[lane[0]].get(filename), before[lane[0]].get(filename),
                             "another seat's hold changed")
        if lane[1] is not None:
            self.assertEqual(after[lane[0]].get("state/core-status.json"),
                             before[lane[0]].get("state/core-status.json"), "a worker changed core status")
        self.check_readers(offset)

    def test_replay_clear_reinstate_and_expiry(self):
        core = held(NORTH_CORE, 0, 0, 300)
        self.step("decide", NORTH_CORE, 0, core, limit=refusal(0), launch=False)
        self.step("decide", SOUTH_WORKER, 1, held(SOUTH_WORKER, 1, 1, 1060, reset=1000),
                  limit=refusal(1, reset=1000), launch=False)
        south = held(SOUTH_CORE, 2, 2, 302)
        self.step("decide", SOUTH_CORE, 2, south, limit=refusal(2), launch=False)
        worker = held(NORTH_WORKER, 3, 3, 303)
        self.step("decide", NORTH_WORKER, 3, worker, limit=refusal(3), launch=False)
        self.step("arrive", NORTH_CORE, 4, task="task-arrival")
        core = changed(core, status="cleared", released_at=at(5), cleared_by="retry-one",
                       held_tasks=["task-arrival"])
        self.step("clear", NORTH_CORE, 5, core, by="retry-one")
        self.step("decide", NORTH_CORE, 6, limit=refusal(0), launch=True)
        self.step("reinstate", NORTH_CORE, 7, by="wrong-retry")
        core = changed(core, remove=("released_at", "cleared_by"), status="held",
                       until=at(308), reinstated_at=at(8))
        self.step("reinstate", NORTH_CORE, 8, core, by="retry-one")
        self.step("decide", NORTH_CORE, 9, limit=refusal(9), launch=False)
        self.step("clear", NORTH_WORKER, 10,
                  changed(worker, status="cleared", released_at=at(10), cleared_by="worker-retry",
                          held_tasks=["task-arrival"]), by="worker-retry")
        self.step("clear", SOUTH_CORE, 11,
                  changed(south, status="cleared", released_at=at(11), cleared_by="south-retry"), by="south-retry")
        self.step("read", NORTH_CORE, 307)
        self.step("read", NORTH_CORE, 308)
        core = changed(core, status="released", released_at=at(308))
        self.step("decide", NORTH_CORE, 308, core, limit=refusal(0), launch=True)
        self.step("decide", NORTH_CORE, 309, limit=refusal(0), launch=True)
        self.step("reinstate", NORTH_CORE, 310, by="retry-one")
        self.assertEqual(self.steps, 17)

    def test_fresh_refusals_renew_then_cap_backoff(self):
        core = held(NORTH_CORE, 0, 0, 300)
        self.step("decide", NORTH_CORE, 0, core, limit=refusal(0), launch=False)
        for offset, lane in ((1, SOUTH_WORKER), (2, NORTH_WORKER), (3, SOUTH_CORE)):
            self.step("decide", lane, offset, held(lane, offset, offset, 50060, reset=50000),
                      limit=refusal(offset, reset=50000), launch=False)
        self.step("decide", NORTH_CORE, 299, limit=refusal(100), launch=False)
        core = held(NORTH_CORE, 100, 300, 900, strikes=2, started=0)
        self.step("decide", NORTH_CORE, 300, core, limit=refusal(100), launch=False)
        self.step("decide", NORTH_CORE, 301, limit=refusal(100), launch=False)
        self.step("read", NORTH_CORE, 900)
        self.step("decide", NORTH_CORE, 900, changed(core, status="released", released_at=at(900)),
                  limit=refusal(100), launch=True)
        self.step("decide", NORTH_CORE, 901, limit=refusal(100), launch=True)
        core = held(NORTH_CORE, 902, 902, 2102, strikes=3)
        self.step("decide", NORTH_CORE, 902, core, limit=refusal(902), launch=False)
        for evidence, until, strikes in ((2102, 4502, 4), (4502, 8102, 5), (8102, 11702, 6)):
            core = held(NORTH_CORE, evidence, evidence, until, strikes=strikes, started=902)
            self.step("decide", NORTH_CORE, evidence, core, limit=refusal(evidence), launch=False)
        self.step("decide", NORTH_CORE, 11702, changed(core, status="released", released_at=at(11702)),
                  limit=refusal(8102), launch=True)
        core = held(NORTH_CORE, 12002, 12002, 12302)
        self.step("decide", NORTH_CORE, 12002, core, limit=refusal(12002), launch=False)
        self.step("decide", NORTH_CORE, 12302, changed(core, status="released", released_at=at(12302)),
                  limit=refusal(12002), launch=True)
        self.step("decide", NORTH_CORE, 12303, limit=refusal(0), launch=True)
        self.assertEqual(self.steps, 18)

    def test_reset_grace_and_task_carryover_across_a_retry(self):
        self.step("arrive", NORTH_CORE, 0, task="task-existing")
        core = held(NORTH_CORE, 1, 1, 1860, reset=1800, kind="usage-limit", present=("task-existing",))
        self.step("decide", NORTH_CORE, 1, core, limit=refusal(1, reset=1800, kind="usage-limit"), launch=False)
        self.step("decide", SOUTH_WORKER, 2, held(SOUTH_WORKER, 2, 2, 3660, reset=3600),
                  limit=refusal(2, reset=3600), launch=False)
        self.step("decide", SOUTH_CORE, 3, held(SOUTH_CORE, 3, 3, 303), limit=refusal(3), launch=False)
        self.step("decide", NORTH_WORKER, 4, held(NORTH_WORKER, 4, 4, 304, present=("task-existing",)),
                  limit=refusal(4), launch=False)
        self.step("arrive", NORTH_CORE, 5, task="task-kept")
        self.step("arrive", NORTH_CORE, 6, task="task-gone")
        core = changed(core, held_tasks=["task-gone", "task-kept"])
        self.step("decide", NORTH_CORE, 7, core, limit=refusal(1, reset=1800, kind="usage-limit"), launch=False)
        core = changed(core, status="cleared", released_at=at(8), cleared_by="first-retry")
        self.step("clear", NORTH_CORE, 8, core, by="first-retry")
        self.step("reinstate", NORTH_CORE, 9, by="other-retry")
        core = changed(core, remove=("released_at", "cleared_by"), status="held", reinstated_at=at(10))
        self.step("reinstate", NORTH_CORE, 10, core, by="first-retry")
        self.step("remove", NORTH_CORE, 11, task="task-gone")
        self.step("clear", NORTH_CORE, 12,
                  changed(core, status="cleared", released_at=at(12), cleared_by="second-retry"), by="second-retry")
        self.step("arrive", NORTH_CORE, 13, task="task-between")
        core = held(NORTH_CORE, 14, 14, 1860, strikes=2, reset=1800, kind="usage-limit",
                    present=("task-between", "task-existing"), tasks=("task-kept",))
        self.step("decide", NORTH_CORE, 14, core, limit=refusal(14, reset=1800, kind="usage-limit"), launch=False)
        self.step("decide", NORTH_CORE, 15, launch=False)
        for offset in (1799, 1800, 1859, 1860):
            self.step("read", NORTH_CORE, offset)
        self.step("decide", NORTH_CORE, 1860, changed(core, status="released", released_at=at(1860)),
                  limit=refusal(14, reset=1800, kind="usage-limit"), launch=True)
        self.assertEqual(self.steps, 21)

    def test_account_and_model_switches_do_not_cross_seats_or_workspaces(self):
        core = held(NORTH_CORE, 0, 0, 300)
        self.step("decide", NORTH_CORE, 0, core, limit=refusal(0), launch=False)
        worker = held(SOUTH_WORKER, 1, 1, 301, model="sonnet", account=ACCOUNT_B, bindings=B_SONNET)
        self.step("decide", SOUTH_WORKER, 1, worker, limit=refusal(1, model="sonnet"),
                  identity=("sonnet", ACCOUNT_B), launch=False)
        north_worker = held(NORTH_WORKER, 2, 2, 302, account=ACCOUNT_B, bindings=B_OPUS)
        self.step("decide", NORTH_WORKER, 2, north_worker, limit=refusal(2), identity=(OPUS, ACCOUNT_B), launch=False)
        south = held(SOUTH_CORE, 3, 3, 303, account=None, bindings=UNREAD_OPUS)
        self.step("decide", SOUTH_CORE, 3, south, limit=refusal(3), identity=(OPUS, None), launch=False)
        self.step("account-change", NORTH_CORE, 4, returns_hold=False)
        core = changed(core, status="released", released_at=at(5))
        self.step("decide", NORTH_CORE, 5, core, limit=refusal(0), identity=("sonnet", ACCOUNT_A), launch=True)
        self.step("decide", NORTH_CORE, 6, limit=refusal(0), launch=True)
        self.step("account-change", SOUTH_CORE, 7, identity=(OPUS, ACCOUNT_B), returns_hold=False)
        south = changed(south, status="released", released_at=at(8), launch_account=ACCOUNT_B)
        self.step("decide", SOUTH_CORE, 8, south, limit=refusal(3), identity=(OPUS, ACCOUNT_B), launch=True)
        south = changed(south, account=ACCOUNT_A, account_at=at(9))
        self.step("decide", SOUTH_CORE, 9, south, limit=refusal(3), launch=True)
        core = changed(core, account=ACCOUNT_B, account_at=at(10))
        self.step("decide", NORTH_CORE, 10, core, limit=refusal(0), identity=(OPUS, ACCOUNT_B), launch=True)
        self.step("decide", NORTH_CORE, 11, limit=refusal(10), identity=(OPUS, ACCOUNT_B), launch=True)
        core = held(NORTH_CORE, 12, 12, 612, strikes=2, account=ACCOUNT_B, account_at=10, bindings=B_OPUS)
        self.step("decide", NORTH_CORE, 12, core, limit=refusal(12), identity=(OPUS, ACCOUNT_B), launch=False)
        self.step("account-change", NORTH_CORE, 13, identity=(OPUS, ACCOUNT_B), returns_hold=False)
        core = changed(core, status="cleared", released_at=at(14), cleared_by="account-change")
        self.step("account-change", NORTH_CORE, 14, core, identity=(OPUS, ACCOUNT_A))
        core = changed(core, account=ACCOUNT_A, account_at=at(15))
        self.step("decide", NORTH_CORE, 15, core, limit=refusal(12), launch=True)
        core = held(NORTH_CORE, 16, 16, 1216, strikes=3, account_at=15)
        self.step("decide", NORTH_CORE, 16, core, limit=refusal(16), launch=False)
        self.step("account-change", NORTH_WORKER, 17,
                  changed(north_worker, status="cleared", released_at=at(17), cleared_by="account-change"))
        self.step("decide", SOUTH_WORKER, 18, changed(worker, status="released", released_at=at(18)),
                  limit=refusal(1, model="sonnet"), identity=(OPUS, ACCOUNT_B), launch=True)
        south = held(SOUTH_CORE, 19, 19, 619, strikes=2, account_at=9)
        self.step("decide", SOUTH_CORE, 19, south, limit=refusal(19), launch=False)
        self.assertEqual(self.steps, 20)


if __name__ == "__main__":
    unittest.main()
