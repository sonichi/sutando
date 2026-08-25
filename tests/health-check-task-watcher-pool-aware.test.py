#!/usr/bin/env python3
"""The task-watcher probe must not tell a pool to kill its own watchers, and
must not bless a stranger either.

A pool runs one watcher per core. The PID sentinel is single-valued and is held
by whichever core stamped last, so N-1 of N watchers always read as "not tracked
by the sentinel"; the pre-fix verdict ended "Keep the sentinel's (pid), stop the
rest", which on a 4-core host advises stopping 3 live watchers.

Fixing that by treating ANY live parent as ownership was the opposite error: a
`sleep 999` qualifies, so two duplicate watchers on a single-core host read as a
legitimate pool. Ownership now means ancestry reaching a VERIFIED local core
session; a live-but-unattributable parent is `unverified` and earns neither a
stop instruction nor a "legitimate" claim.

These drive the real `check_task_watcher()` against a real process table and
assert on the emitted GROUP STRUCTURE, because that is what the step-9 consumer
contract keys on — never on adjectives.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", _REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def _ps(rows: dict) -> str:
    """A ps table. `rows` maps pid -> ppid; a None ppid omits the row entirely,
    which is how an unreadable parent actually presents."""
    out = ["  PID  PPID ARGS"]
    for pid, ppid in rows.items():
        if ppid is not None:
            out.append(f"{pid} {ppid} bash src/watch-tasks-stream.sh")
    return "\n".join(out) + "\n"


def _groups(detail: str) -> dict:
    """Parse the three labelled groups back out of a verdict."""
    found = {}
    for label in ("session-owned", "unverified", "ownerless"):
        m = re.search(rf"{label} \((\d+)\): ([^;]*)", detail)
        if m is None:
            continue
        body = m.group(2).strip()
        pids = set() if body == "none" else {p.strip() for p in body.split(",")}
        assert len(pids) == int(m.group(1)), f"{label} count disagrees with its list: {detail!r}"
        found[label] = pids
    return found


def _probe(ps_rows, roots, core_pids, sentinel=None, sentinel_alive=False):
    """Run the real probe. `sentinel` is the pid written to the sentinel file."""
    hc = _load()
    ws = Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    (ws / "state" / "cores" / "main.alive").write_text("{}")
    if sentinel is not None:
        (ws / "state" / "watch-tasks-stream.pid").write_text(str(sentinel))
    hc.WORKSPACE_DIR = ws
    table = _ps(ps_rows)
    hc._ps_snapshot = lambda: table
    hc._watcher_trees = lambda ps_output=None: {r: [r] for r in roots}
    hc._proc_argv = (lambda pid: "bash src/watch-tasks-stream.sh") if sentinel_alive \
        else (lambda pid: None)
    hc._any_core_alive = lambda *a, **k: True
    hc._local_core_pids = lambda: core_pids
    return hc.check_task_watcher()


class OwnershipRequiresAVerifiedCoreSession(unittest.TestCase):
    """P1: `PPID != 1` is not evidence of pool membership."""

    def test_live_but_unrelated_parents_are_unverified_not_legitimate(self):
        """The reviewer's adjacent-input control. Under the old rule these two
        flipped the verdict to 'these are legitimate. Do NOT stop them'."""
        v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                   roots=["100", "200"], core_pids=set())
        d = v["detail"]
        self.assertEqual(_groups(d)["unverified"], {"100", "200"})
        self.assertEqual(_groups(d)["session-owned"], set())
        self.assertNotIn("legitimate", d)
        self.assertNotIn("Do NOT stop them", d)

    def test_ancestry_reaching_a_verified_core_is_owned(self):
        v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                   roots=["100", "200"], core_pids={"500", "600"})
        d = v["detail"]
        self.assertEqual(_groups(d)["session-owned"], {"100", "200"})
        self.assertIn("Do NOT stop them", d)

    def test_ownership_is_transitive_through_intermediate_shells(self):
        """A watcher is spawned via a wrapper, so the core is rarely the DIRECT
        parent — a one-hop check would call this unverified."""
        v = _probe({"100": "300", "300": "400", "400": "500", "500": "1",
                    "200": "310", "310": "500"},
                   roots=["100", "200"], core_pids={"500"})
        self.assertEqual(_groups(v["detail"])["session-owned"], {"100", "200"})

    def test_tmux_unavailable_makes_everything_unverified(self):
        """Fail closed: with no way to verify, nothing is blessed and nothing is
        named for stopping."""
        v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                   roots=["100", "200"], core_pids=None)
        d = v["detail"]
        self.assertEqual(_groups(d)["unverified"], {"100", "200"})
        self.assertEqual(_groups(d)["ownerless"], set())
        self.assertNotIn("legitimate", d)

    def test_reparented_to_init_is_ownerless(self):
        v = _probe({"100": "1", "200": "1"}, roots=["100", "200"], core_pids={"9"})
        self.assertEqual(_groups(v["detail"])["ownerless"], {"100", "200"})

    def test_unreadable_parentage_is_ownerless_not_owned(self):
        v = _probe({"100": None, "200": "1"}, roots=["100", "200"], core_pids={"9"})
        self.assertEqual(_groups(v["detail"])["ownerless"], {"100", "200"})


class EveryMultiRootVerdictNamesEveryGroup(unittest.TestCase):
    """P1: an omitted group leaves one undifferentiated list, which the step-9
    contract must read as 'change nothing' — so a real all-ownerless duplicate
    would go unacted-on."""

    CASES = {
        "absent sentinel": dict(sentinel=None, sentinel_alive=False),
        "dead sentinel": dict(sentinel=999, sentinel_alive=False),
        # sentinel=999 keeps all named roots in `extras`; the sentinel's own
        # tree has its own dedicated cases below.
        "live sentinel": dict(sentinel=999, sentinel_alive=True),
    }

    def test_all_three_labels_present_when_every_root_is_ownerless(self):
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "1", "200": "1"}, roots=["100", "200"],
                           core_pids={"9"}, **kw)
                g = _groups(v["detail"])
                self.assertEqual(set(g), {"session-owned", "unverified", "ownerless"}, nm)
                self.assertEqual(g["ownerless"], {"100", "200"}, nm)

    def test_all_three_labels_present_when_every_root_is_owned(self):
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "500", "200": "500", "500": "1"},
                           roots=["100", "200"], core_pids={"500"}, **kw)
                g = _groups(v["detail"])
                self.assertEqual(set(g), {"session-owned", "unverified", "ownerless"}, nm)
                self.assertEqual(g["session-owned"], {"100", "200"}, nm)

    def test_a_stop_instruction_never_names_an_owned_root(self):
        """The invariant the consumer keys on, across all three branches."""
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "500", "200": "1", "300": "500", "500": "1"},
                           roots=["100", "200", "300"], core_pids={"500"}, **kw)
                g = _groups(v["detail"])
                self.assertEqual(g["ownerless"], {"200"}, nm)
                self.assertEqual(g["session-owned"], {"100", "300"}, nm)


class TheSentinelsOwnTreeIsClassifiedToo(unittest.TestCase):
    """Splitting only `extras` left an orphaned sentinel tree unclassified
    beside a live replacement."""

    def test_orphan_sentinel_beside_a_verified_replacement(self):
        v = _probe({"100": "1", "200": "500", "500": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        d = v["detail"]
        g = _groups(d)
        self.assertEqual(g["ownerless"], {"100"}, d)
        self.assertEqual(g["session-owned"], {"200"}, d)
        # The sentinel is the thing to clean up, so it must not be protected.
        self.assertNotIn("keep the sentinel's", d)

    def test_owned_sentinel_is_still_protected(self):
        """Mirror control: without it, a verdict that never protects the
        sentinel would satisfy the test above."""
        v = _probe({"100": "500", "200": "1", "500": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        self.assertIn("keep the sentinel's (100)", v["detail"])


class RepairDataAccompaniesTheRepairOffer(unittest.TestCase):
    def test_all_owned_no_sentinel_branch_supplies_the_restamp_pid(self):
        """It advertises `--fix`; without a pid the fix dispatcher is a no-op."""
        v = _probe({"100": "500", "200": "500", "500": "1"},
                   roots=["100", "200"], core_pids={"500"}, sentinel=None)
        self.assertIn("Re-stamp the sentinel with --fix", v["detail"])
        self.assertIn(v.get("_sentinel_restamp_pid"), {"100", "200"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
