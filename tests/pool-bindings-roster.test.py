#!/usr/bin/env python3
"""Bindings are owner-authored; the roster is compiled; the router only reads.

The rules under test are the ones whose violation routes work somewhere the
owner did not ask for:

  * an absent or unreadable roster REFUSES the pass — never defaults to core,
    because a silent default aims every task at one recipient the moment the
    file is unwritable;
  * a target not in the roster fails the task by name — never substituted;
  * a target that is not `live` holds the work — never re-aimed;
  * a binding naming a nonexistent worker is refused at COMPILE time, where one
    error is visible, rather than at routing time once per task.

Run: python3 tests/pool-bindings-roster.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pool_roster as pr  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"


def live(*ids):
    return {i: {"label": i[:6], "state": "live"} for i in ids}


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)


class TestCompile(Base):
    def test_version_increments_so_an_assignment_can_cite_one(self):
        a = pr.compile_roster(self.ws, live(W1))
        b = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(b["version"], a["version"] + 1)

    def test_a_binding_to_a_nonexistent_worker_is_refused_at_compile(self):
        """One visible error, instead of every task from that source failing."""
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W2})
        self.assertIn(W2, str(e.exception))

    def test_an_empty_binding_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": []})

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, {W1: {"state": "alive"}})

    def test_a_malformed_worker_id_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, {"../escape": {"state": "live"}})

    def test_binding_to_the_core_is_allowed(self):
        got = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": "core"})
        self.assertEqual(got["bindings"]["room:!x:ag2.space"], "core")

    def test_a_refused_compile_leaves_the_previous_roster_intact(self):
        first = pr.compile_roster(self.ws, live(W1))
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": "nope"})
        self.assertEqual(pr.load_roster(self.ws)["version"], first["version"])

    def test_the_written_roster_is_valid_json(self):
        pr.compile_roster(self.ws, live(W1))
        json.loads(pr.roster_path(self.ws).read_text(encoding="utf-8"))


class TestLoad(Base):
    def test_absent_roster_is_None_not_a_default(self):
        self.assertIsNone(pr.load_roster(self.ws))

    def test_unreadable_roster_is_None(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(pr.load_roster(self.ws))

    def test_a_roster_without_workers_is_not_a_roster(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"version": 1}', encoding="utf-8")
        self.assertIsNone(pr.load_roster(self.ws))


class TestTargets(Base):
    def test_no_binding_resolves_to_the_core(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(r, "room:!unbound:ag2.space"), ["core"])

    def test_a_binding_resolves_to_its_worker(self):
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])

    def test_a_set_is_refused_until_members_have_their_own_result(self):
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1, W2), {"room:!x:ag2.space": [W1, W2]})
        self.assertIn("fan-out", str(e.exception))

    def test_a_one_member_list_is_still_one_target(self):
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": [W1]})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])
    def test_requested_worker_outranks_the_binding(self):
        r = pr.compile_roster(self.ws, live(W1, W2), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space", requested_worker=W2), [W2])

    def test_the_binding_key_is_the_SOURCE_not_a_task_property(self):
        """The owner declares it before any task exists, so it cannot key on one."""
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])
        self.assertEqual(pr.targets_for(r, "discord:1234"), ["core"])


class TestValidation(Base):
    def test_an_unknown_target_is_named_not_substituted(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.unknown_targets(r, [W1, "ghost"]), ["ghost"])

    def test_the_core_is_always_known(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.unknown_targets(r, ["core"]), [])

class TestRouterInputIsClosed(Base):
    def test_a_snapshot_routes_after_the_file_is_gone(self):
        """The router's whole input is the roster it was handed, so a pass is
        replayable: delete the file and the same snapshot still resolves."""
        r = pr.compile_roster(self.ws, {W1: {"state": "live"}}, {"!x:ag2.space": W1})
        snapshot = json.loads(json.dumps(r))
        pr.roster_path(self.ws).unlink()          # the file is gone
        self.assertIsNone(pr.load_roster(self.ws))
        self.assertEqual(pr.targets_for(snapshot, "!x:ag2.space", None), [W1])
        self.assertEqual(pr.unknown_targets(snapshot, [W1]), [])


class TestLabelResolution(unittest.TestCase):
    """An envelope names a worker the way a person does — by label."""

    ROSTER = {"workers": {"a" * 32: {"state": "live", "label": "worker-1"},
                          "b" * 32: {"state": "live", "label": "worker-2"}},
              "bindings": {}}

    def test_a_label_resolves_to_its_id(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "worker-1"), ["a" * 32])

    def test_an_id_passes_through_unchanged(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "b" * 32), ["b" * 32])

    def test_an_unknown_label_is_left_to_fail_by_name(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "worker-9"), ["worker-9"])
        self.assertEqual(pr.unknown_targets(self.ROSTER, ["worker-9"]), ["worker-9"])

    def test_an_ambiguous_label_never_picks_one(self):
        """Two workers sharing a label must fail the task, not silently choose."""
        r = {"workers": {"a" * 32: {"state": "live", "label": "dup"},
                         "b" * 32: {"state": "live", "label": "dup"}},
             "bindings": {}}
        self.assertEqual(pr.targets_for(r, "", "dup"), ["dup"])
        self.assertEqual(pr.unknown_targets(r, ["dup"]), ["dup"])

    def test_a_requested_worker_overrides_the_binding(self):
        r = dict(self.ROSTER, bindings={"!room:x": "b" * 32})
        self.assertEqual(pr.targets_for(r, "!room:x", "worker-1"), ["a" * 32])



class TestCorruptDeclarations(Base):
    def declare(self, text):
        p = pr.bindings_path(self.ws); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)

    def test_a_missing_file_is_a_valid_empty_declaration(self):
        self.assertEqual(pr.load_bindings(self.ws), {})
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(r, "!x:ag2.space"), [pr.CORE])

    def test_a_valid_empty_declaration_compiles(self):
        self.declare(json.dumps({"bindings": {}}))
        self.assertEqual(pr.compile_roster(self.ws, live(W1))["bindings"], {})

    def test_a_corrupt_file_is_refused_and_the_last_roster_survives(self):
        """Read as empty, a corrupt declaration compiled into a roster that sent
        every bound task to the core, one version up, with no error anywhere."""
        self.declare(json.dumps({"bindings": {"!x:ag2.space": W1}}))
        good = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(good, "!x:ag2.space"), [W1])
        self.declare("{broken")
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1))
        kept = pr.load_roster(self.ws)
        self.assertEqual(kept["version"], good["version"])
        self.assertEqual(pr.targets_for(kept, "!x:ag2.space"), [W1])

    def test_a_mis_shaped_declaration_is_refused(self):
        for bad in ("[]", '{"bindings": []}', '"text"'):
            self.declare(bad)
            with self.assertRaises(pr.RosterError, msg=bad):
                pr.load_bindings(self.ws)

    def test_a_bare_map_without_the_bindings_key_is_refused(self):
        """Measured 2026-09-11: a hand-written {source: worker} map loaded as {}
        and the next compile emitted 1 binding where the owner had declared 6."""
        self.declare(json.dumps({"!x:ag2.space": W1, "!y:ag2.space": W1}))
        with self.assertRaises(pr.RosterError):
            pr.load_bindings(self.ws)
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1))

    def test_the_bindings_key_is_what_is_missing_not_the_file(self):
        """The refusal must not swallow the still-valid empty declarations."""
        self.assertEqual(pr.load_bindings(self.ws), {})
        self.declare(json.dumps({"bindings": {}}))
        self.assertEqual(pr.load_bindings(self.ws), {})


class TestBindingLoss(Base):
    """A source whose binding vanishes is re-aimed at the core with no error,
    which is the same silent misroute a corrupt declaration produced."""

    def test_a_shrink_without_allow_unbind_is_refused_by_name(self):
        pr.compile_roster(self.ws, live(W1), {"!x:ag2.space": W1, "!y:ag2.space": W1})
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1), {"!x:ag2.space": W1})
        self.assertIn("!y:ag2.space", str(e.exception))
        self.assertNotIn("!x:ag2.space", str(e.exception))

    def test_a_refused_shrink_leaves_the_previous_roster_intact(self):
        first = pr.compile_roster(self.ws, live(W1), {"!y:ag2.space": W1})
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1), {})
        kept = pr.load_roster(self.ws)
        self.assertEqual(kept["version"], first["version"])
        self.assertEqual(pr.targets_for(kept, "!y:ag2.space"), [W1])

    def test_a_shrink_the_caller_names_compiles(self):
        pr.compile_roster(self.ws, live(W1), {"!x:ag2.space": W1, "!y:ag2.space": W1})
        got = pr.compile_roster(self.ws, live(W1), {"!x:ag2.space": W1},
                                allow_unbind=["!y:ag2.space"])
        self.assertEqual(got["bindings"], {"!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(got, "!y:ag2.space"), [pr.CORE])

    def test_growth_needs_no_permission(self):
        pr.compile_roster(self.ws, live(W1), {"!x:ag2.space": W1})
        got = pr.compile_roster(self.ws, live(W1, W2),
                                {"!x:ag2.space": W1, "!y:ag2.space": W2})
        self.assertEqual(pr.targets_for(got, "!y:ag2.space"), [W2])

    def test_retargeting_a_source_is_not_a_shrink(self):
        pr.compile_roster(self.ws, live(W1, W2), {"!x:ag2.space": W1})
        got = pr.compile_roster(self.ws, live(W1, W2), {"!x:ag2.space": W2})
        self.assertEqual(pr.targets_for(got, "!x:ag2.space"), [W2])


# Each child loads the roster inside the production compile and waits at a
# barrier for its peer, so both read one predecessor and publish in a fixed order.
CONCURRENT_WRITER = """
import json, os, sys, time
sys.path.insert(0, {src!r})
import pool_roster as pr

ws, room, mine, peer, wrote_first, order = sys.argv[1:7]
real_load = pr.load_roster


def wait_for(path, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline and not os.path.exists(path):
        time.sleep(0.01)


def barriered(workspace):
    got = real_load(workspace)
    open(mine, "w").close()
    # Bounded: under a lock the peer cannot arrive, so this must time out
    # rather than deadlock the serialised head.
    wait_for(peer, 3.0)
    if order == "second":
        wait_for(wrote_first, 5.0)
    return got


pr.load_roster = barriered
if order == "second":
    wait_for(peer, 5.0)   # the first writer is inside compile before this starts
try:
    got = pr.compile_roster(ws, {workers!r}, {{"!base:ag2.space": {w1!r}, room: {w1!r}}})
    print(json.dumps(["OK", got["version"], None]))
except pr.RosterError as e:
    print(json.dumps(["RosterError", None, str(e)]))
if order == "first":
    open(wrote_first, "w").close()
"""


class TestConcurrentCompiles(Base):
    """A shared mutable record has ONE writer contract, and a concurrency test
    calls the production writer. `os.replace` is atomic for the READER; it does
    not stop two writers validating a shrink against the same predecessor and
    both publishing, which re-aims the first writer's room at the core — the
    exact loss this module refuses sequentially. Found in review of this PR."""

    def _run_pair(self):
        """kewei's repro: both compiles read version 1, then publish left-first."""
        pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        prog = CONCURRENT_WRITER.format(src=str(REPO / "src"), workers=live(W1), w1=W1)
        script = self.ws / "writer.py"
        script.write_text(prog, encoding="utf-8")
        first_wrote = self.ws / "wrote-first"
        procs = []
        for room, name, peer, order in (
                ("!left:ag2.space", "ready-l", "ready-r", "first"),
                ("!right:ag2.space", "ready-r", "ready-l", "second")):
            procs.append(subprocess.Popen(
                [sys.executable, str(script), str(self.ws), room,
                 str(self.ws / name), str(self.ws / peer), str(first_wrote), order],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        return [json.loads(p.communicate()[0].strip() or '["CRASH",null,null]') for p in procs]

    def test_a_stale_concurrent_compile_is_refused_not_silently_applied(self):
        left, right = self._run_pair()
        outcomes = sorted(o[0] for o in (left, right))
        self.assertEqual(outcomes, ["OK", "RosterError"],
                         f"both writers succeeded: {left} {right}")
        final = pr.load_roster(self.ws)
        self.assertEqual(pr.targets_for(final, "!left:ag2.space"), [W1],
                         "the published room was silently re-aimed at the core")
        self.assertEqual(final["version"], 2)
        loser = left if left[0] == "RosterError" else right
        self.assertIn("!left:ag2.space", loser[2])
        self.assertEqual(pr.targets_for(final, "!right:ag2.space"), [pr.CORE])

    def test_concurrent_compiles_allocate_distinct_versions(self):
        """Two writers reading one predecessor otherwise allocate one version."""
        pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        seen, errors = [], []

        def compile_once():
            try:
                seen.append(pr.compile_roster(self.ws, live(W1),
                                              {"!base:ag2.space": W1})["version"])
            except Exception as exc:  # noqa: BLE001 — a lost race surfaces here
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=compile_once) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(seen), list(range(2, 14)))
        self.assertEqual(pr.load_roster(self.ws)["version"], 13)

    def test_a_compile_from_a_superseded_version_is_refused(self):
        """The caller reads, decides, then compiles; the roster moved between."""
        read = pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        pr.compile_roster(self.ws, live(W1, W2),
                          {"!base:ag2.space": W1, "!other:ag2.space": W2})
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1),
                              {"!base:ag2.space": W1, "!mine:ag2.space": W1},
                              expect_version=read["version"])
        self.assertIn("version", str(e.exception))
        kept = pr.load_roster(self.ws)
        self.assertEqual(kept["version"], 2)
        self.assertEqual(pr.targets_for(kept, "!other:ag2.space"), [W2])
        self.assertEqual(pr.targets_for(kept, "!mine:ag2.space"), [pr.CORE])

    def test_a_compile_on_the_version_it_read_is_accepted(self):
        read = pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        got = pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1},
                                expect_version=read["version"])
        self.assertEqual(got["version"], read["version"] + 1)

    def test_a_publication_cannot_keep_the_version_a_stale_compile_will_cite(self):
        """kewei's repro on this PR: an intervening publication that reused
        version 1 let a compile citing expect_version=1 erase it. Only the
        writer allocates the token now, so no publication leaves it in place."""
        read = pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        with self.assertRaises(TypeError):
            pr.compile_roster(self.ws, live(W1, W2), {"!base:ag2.space": W2},
                              version=read["version"])
        between = pr.compile_roster(self.ws, live(W1, W2), {"!base:ag2.space": W2})
        self.assertEqual(between["version"], read["version"] + 1)
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1},
                              expect_version=read["version"])
        self.assertIn("version", str(e.exception))
        kept = pr.load_roster(self.ws)
        self.assertEqual(kept["version"], between["version"])
        self.assertEqual(pr.targets_for(kept, "!base:ag2.space"), [W2])
        self.assertEqual(sorted(kept["workers"]), sorted([W1, W2]))

    def test_every_publication_advances_the_version_strictly(self):
        """Whatever shape a compile takes, the stored version moves past the
        one it read; two publications never share a token."""
        seen = []
        for i in range(8):
            before = (pr.load_roster(self.ws) or {}).get("version", 0)
            if i % 3 == 0:
                got = pr.compile_roster(self.ws, live(W1, W2), {"!base:ag2.space": W1})
            elif i % 3 == 1:
                got = pr.compile_roster(self.ws, live(W1, W2), {"!base:ag2.space": W2},
                                        expect_version=before)
            else:
                got = pr.compile_roster(self.ws, live(W1, W2), {},
                                        allow_unbind=["!base:ag2.space"])
            self.assertGreater(got["version"], before)
            self.assertEqual(pr.load_roster(self.ws)["version"], got["version"])
            seen.append(got["version"])
        self.assertEqual(seen, list(range(1, 9)))

    def test_the_lock_sidecar_is_not_a_roster_record(self):
        """The guard lives beside the record it guards, so it must not read as
        one to anything listing state, and must survive the publication."""
        pr.compile_roster(self.ws, live(W1), {"!base:ag2.space": W1})
        state = self.ws / "state"
        self.assertEqual(sorted(p.name for p in state.glob("*.json")), ["roster.json"])
        self.assertTrue((state / "roster.json.lock").exists())
        self.assertEqual(pr.load_roster(self.ws)["version"], 1)
        self.assertEqual(list(state.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=0)
