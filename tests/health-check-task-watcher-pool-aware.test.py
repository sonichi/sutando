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
import types
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


LABELS = ("session-owned", "unverified", "ownerless", "same-core duplicates")


def _groups(detail: str) -> dict:
    """Parse the four labelled groups back out of a verdict."""
    found = {}
    for label in LABELS:
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


class LocalCorePidsComeFromTmuxNotTheSyncedHeartbeats(unittest.TestCase):
    """`_local_core_pids()` is the ownership oracle, so its tri-state matters:
    a set means tmux answered, None means it could not be asked at all."""

    def _mod(self, default_rc, default_out, sock=None, sock_rc=0, sock_out="",
             panes_argv=None):
        hc = _load()
        hc._resolve_tmux_bin = lambda: "/usr/bin/tmux"
        hc._resolve_launch_env = lambda: {}

        class R:
            def __init__(self, rc, out):
                self.returncode, self.stdout = rc, out

        if default_rc is None:
            hc.subprocess = types.SimpleNamespace(
                run=lambda *a, **k: (_ for _ in ()).throw(OSError("no tmux")))
        else:
            hc.subprocess = types.SimpleNamespace(
                run=lambda *a, **k: R(default_rc, default_out))
        hc._local_core_socket = lambda: sock
        hc._run_tmux = (lambda s, *a: R(sock_rc, sock_out)) if sock else (lambda s, *a: None)
        # A pane is a core only if its process IS a core runtime, so the
        # fixture must state that; `panes_argv` overrides a listed pane.
        argv = {}
        for text in (default_out or "", sock_out or ""):
            for line in text.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    # A launcher names the session it started; a fixture that
                    # hardcodes one session cannot represent a renamed core.
                    argv[parts[1]] = f"claude --name {parts[0]} --dangerously-skip-permissions"
        argv.update(panes_argv or {})
        hc._ps_snapshot = lambda: "\n".join(f"{k} 1 {v}" for k, v in argv.items()) + "\n"
        return hc

    def test_pane_pids_from_the_default_socket(self):
        hc = self._mod(0, "core-1 3951\ncore-2 3952\ncore-3 38058\n")
        self.assertEqual(hc._local_core_pids(), {"3951", "3952", "38058"})

    def test_non_numeric_rows_are_dropped(self):
        """An old tmux can emit the format string itself instead of expanding it."""
        hc = self._mod(0, "core-1 3951\n#{session_name} #{pane_pid}\n\n")
        self.assertEqual(hc._local_core_pids(), {"3951"})

    def test_the_runtime_socket_is_unioned_in(self):
        hc = self._mod(0, "core-1 100\n", sock="/tmp/s.sock", sock_out="sutando-core 200\n")
        self.assertEqual(hc._local_core_pids(), {"100", "200"})

    def test_the_runtime_socket_is_queried_even_without_a_heartbeat(self):
        """The pool lives on tmux's DEFAULT socket; the main core lives on the
        runtime one, and `_local_core_socket()` is None without a fresh local
        heartbeat. Measured live: the main core's own watcher read as
        `unverified` because that socket was never asked."""
        # This case swaps `_run_tmux` out from under `_mod`, so the runtime
        # socket's pane must declare its process here.
        hc = self._mod(0, "core-1 3951\n",
                       panes_argv={"31930": "claude --name sutando-core --chrome"})
        seen = []

        class R:
            returncode, stdout = 0, "sutando-core 31930\n"  # runtime: main core

        hc._local_core_socket = lambda: None
        hc._run_tmux = lambda s, *a: (seen.append(s), R())[1]
        self.assertEqual(hc._local_core_pids(), {"3951", "31930"})
        self.assertIn("/tmp/sutando-tmux.sock", seen)

    def test_tmux_unavailable_is_None_not_an_empty_set(self):
        """The distinction the whole fail-closed path rests on: None means
        'could not verify' (everything unverified), a set means 'asked, and
        these are the cores' (anything absent is genuinely not a core)."""
        self.assertIsNone(self._mod(None, "")._local_core_pids())

    def test_a_nonzero_exit_is_also_unverifiable(self):
        self.assertIsNone(self._mod(1, "")._local_core_pids())

    def test_a_non_core_session_pane_confers_no_ownership(self):
        """The blocker this scoping exists for. An unscoped census admitted
        every pane on the host, so an ordinary shell parenting a stray watcher
        made it read `session-owned` — i.e. legitimate, do not stop."""
        hc = self._mod(0, "core-2 3952\nmy-editor 4100\nirc 4200\n")
        self.assertEqual(hc._local_core_pids(), {"3952"})

    def test_the_main_core_session_name_follows_the_launcher_env(self):
        """`SUTANDO_TMUX_SESSION` renames the main core's session, and the
        census has to follow it or that core's own watcher goes unverified."""
        hc = self._mod(0, "sutando-core 10\nalt-core 20\n")
        self.assertEqual(hc._local_core_pids(), {"10"})
        hc.os.environ["SUTANDO_TMUX_SESSION"] = "alt-core"
        try:
            self.assertEqual(hc._local_core_pids(), {"20"})
        finally:
            del hc.os.environ["SUTANDO_TMUX_SESSION"]

    def test_tmux_running_with_no_panes_is_an_empty_set(self):
        """Positive control against the case above: tmux ANSWERED, so absence
        is real. Returning None here would silently re-bless every stranger."""
        self.assertEqual(self._mod(0, "")._local_core_pids(), set())


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
                    "200": "310", "310": "600", "600": "1"},
                   roots=["100", "200"], core_pids={"500", "600"})
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

    def test_all_labels_present_when_every_root_is_ownerless(self):
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "1", "200": "1"}, roots=["100", "200"],
                           core_pids={"9"}, **kw)
                g = _groups(v["detail"])
                self.assertEqual(set(g), set(LABELS), nm)
                self.assertEqual(g["ownerless"], {"100", "200"}, nm)

    def test_all_labels_present_when_every_root_is_owned_by_a_distinct_core(self):
        """Two roots under ONE core are not the all-owned case (see
        OneWatcherPerCore); the all-owned fixture needs one core per root."""
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                           roots=["100", "200"], core_pids={"500", "600"}, **kw)
                g = _groups(v["detail"])
                self.assertEqual(set(g), set(LABELS), nm)
                self.assertEqual(g["session-owned"], {"100", "200"}, nm)

    def test_a_stop_instruction_never_names_an_owned_root(self):
        """The invariant the consumer keys on, across all three branches."""
        for nm, kw in self.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "500", "200": "1", "300": "600", "500": "1", "600": "1"},
                           roots=["100", "200", "300"], core_pids={"500", "600"}, **kw)
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

    def test_sentinel_under_a_live_shell_is_classified_not_dropped(self):
        """The blocker. Partitioning only `extras` and deciding the sentinel by
        a bespoke PPID test left its root in NO group whenever its parent was a
        live shell — not owned, not unverified, not ownerless — so the pid lists
        step 9 acts on simply omitted it."""
        v = _probe({"100": "700", "200": "500", "500": "1", "700": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        d = v["detail"]
        g = _groups(d)
        self.assertEqual(g["unverified"], {"100"}, d)
        self.assertEqual(g["session-owned"], {"200"}, d)
        # Classified, but still not something to stop.
        self.assertIn("keep the sentinel's (100)", d)

    def test_owned_sentinel_is_named_in_its_own_group(self):
        v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                   roots=["100", "200"], core_pids={"500", "600"},
                   sentinel=100, sentinel_alive=True)
        self.assertEqual(_groups(v["detail"])["session-owned"], {"100", "200"})

    def test_unreadable_sentinel_parentage_is_not_an_orphan_claim(self):
        """Mirror of the two above. For a stranger, unreadable parentage is
        ownerless; for the tracked live watcher it is absence of evidence, so
        the verdict must not offer its root for stopping."""
        v = _probe({"100": None, "200": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        d = v["detail"]
        # 200 is genuinely reparented, so this IS the stop branch — which is
        # what makes the sentinel's exemption observable rather than vacuous.
        self.assertEqual(_groups(d)["ownerless"], {"200"}, d)
        self.assertIn("keep the sentinel's (100)", d)

    def test_owned_sentinel_is_still_protected(self):
        """Mirror control: without it, a verdict that never protects the
        sentinel would satisfy the test above."""
        v = _probe({"100": "500", "200": "1", "500": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        self.assertIn("keep the sentinel's (100)", v["detail"])

    def test_unreadable_sentinel_beside_an_OWNED_extra_is_not_called_legitimate(self):
        """The reported blocker, and the case the sibling above cannot reach.

        There the extra was reparented, so the stop branch ran and the dropped
        sentinel stayed invisible. Give the extra a verified core parent and
        every group empties, which takes the `not orphaned and not unverified`
        branch and blesses a real duplicate as a legitimate pool.
        """
        v = _probe({"100": None, "200": "500", "500": "1"},
                   roots=["100", "200"], core_pids={"500"},
                   sentinel=100, sentinel_alive=True)
        d = v["detail"]
        self.assertNotIn("legitimate", d)
        self.assertEqual(_groups(d)["unverified"], {"100"}, d)
        self.assertEqual(_groups(d)["session-owned"], {"200"}, d)
        self.assertIn("keep the sentinel's (100)", d)

    def test_every_root_appears_in_exactly_one_group(self):
        """Totality is what step 9 acts on: a root in no group is a watcher the
        operator never sees. Asserted over every shape this class exercises."""
        shapes = [
            ({"100": None, "200": "500", "500": "1"}, {"500"}),
            ({"100": None, "200": "1"}, {"500"}),
            ({"100": "1", "200": "500", "500": "1"}, {"500"}),
            ({"100": "700", "200": "500", "500": "1", "700": "1"}, {"500"}),
            ({"100": "500", "200": "500", "500": "1"}, {"500"}),
            ({"100": "500", "200": "1", "500": "1"}, {"500"}),
        ]
        for rows, cores in shapes:
            with self.subTest(rows=rows):
                d = _probe(rows, roots=["100", "200"], core_pids=cores,
                           sentinel=100, sentinel_alive=True)["detail"]
                g = _groups(d)
                union = set().union(*(g[k] for k in LABELS))
                self.assertEqual(union, {"100", "200"}, d)
                self.assertEqual(sum(len(g[k]) for k in LABELS), 2,
                                 f"a root landed in two groups: {d!r}")


class RepairDataAccompaniesTheRepairOffer(unittest.TestCase):
    def test_all_owned_no_sentinel_branch_supplies_the_restamp_pid(self):
        """It advertises `--fix`; without a pid the fix dispatcher is a no-op."""
        v = _probe({"100": "500", "200": "600", "500": "1", "600": "1"},
                   roots=["100", "200"], core_pids={"500", "600"}, sentinel=None)
        self.assertIn("Re-stamp the sentinel with --fix", v["detail"])
        self.assertIn(v.get("_sentinel_restamp_pid"), {"100", "200"})

    def test_same_core_duplicates_get_no_restamp_offer(self):
        """Mirror control: only the owner mapping differs, and the repair goes away."""
        v = _probe({"100": "500", "200": "500", "500": "1"},
                   roots=["100", "200"], core_pids={"500"}, sentinel=None)
        self.assertNotIn("Re-stamp", v["detail"])
        self.assertNotIn("_sentinel_restamp_pid", v)


class OneWatcherPerCore(unittest.TestCase):
    """Two roots tracing to the SAME core are a duplicate, not a pool; recording only
    "reaches some core" made that pair look like two roots under two distinct cores."""

    SAME = {"100": "300", "300": "500", "200": "310", "310": "500", "500": "1"}
    DISTINCT = {"100": "300", "300": "500", "200": "310", "310": "600",
                "500": "1", "600": "1"}

    def test_same_core_roots_are_duplicates_in_every_sentinel_state(self):
        for nm, kw in EveryMultiRootVerdictNamesEveryGroup.CASES.items():
            with self.subTest(nm):
                v = _probe(self.SAME, roots=["100", "200"], core_pids={"500"}, **kw)
                d = v["detail"]
                g = _groups(d)
                self.assertNotIn("legitimate", d, d)
                self.assertNotIn("Do NOT stop them", d, d)
                self.assertNotIn("_sentinel_restamp_pid", v, d)
                # One survivor per core; the other is named for stopping.
                self.assertEqual(g["session-owned"], {"100"}, d)
                self.assertEqual(g["same-core duplicates"], {"200"}, d)
                self.assertEqual(g["ownerless"] | g["unverified"], set(), d)
                self.assertIn("Stop ONLY the ownerless roots and the same-core duplicates", d)

    def test_distinct_owners_are_legitimate_in_every_sentinel_state(self):
        """The control: only the owner mapping changes, and the verdict flips."""
        for nm, kw in EveryMultiRootVerdictNamesEveryGroup.CASES.items():
            with self.subTest(nm):
                v = _probe(self.DISTINCT, roots=["100", "200"],
                           core_pids={"500", "600"}, **kw)
                d = v["detail"]
                g = _groups(d)
                self.assertIn("Do NOT stop them", d, d)
                self.assertEqual(g["session-owned"], {"100", "200"}, d)
                self.assertEqual(g["same-core duplicates"], set(), d)

    def test_the_sentinels_root_is_the_survivor(self):
        """Which duplicate to keep is not arbitrary when one holds the sentinel."""
        v = _probe(self.SAME, roots=["100", "200"], core_pids={"500"},
                   sentinel=200, sentinel_alive=True)
        d = v["detail"]
        g = _groups(d)
        self.assertEqual(g["session-owned"], {"200"}, d)
        self.assertEqual(g["same-core duplicates"], {"100"}, d)
        self.assertIn("keep the sentinel's (200)", d)

    def test_three_roots_two_cores_stops_exactly_one(self):
        v = _probe({"100": "500", "200": "500", "300": "600", "500": "1", "600": "1"},
                   roots=["100", "200", "300"], core_pids={"500", "600"})
        g = _groups(v["detail"])
        self.assertEqual(g["session-owned"], {"100", "300"}, v["detail"])
        self.assertEqual(g["same-core duplicates"], {"200"}, v["detail"])


class APaneIsACoreOnlyIfItRunsACoreRuntime(unittest.TestCase):
    """The session NAME is not ownership. Both launchers preserve sibling
    windows inside the core session, so an ordinary shell pane legitimately
    sits in the canonical session — and before this it conferred core ownership
    on any watcher it parented, blessing a hand-started duplicate."""

    _mod = LocalCorePidsComeFromTmuxNotTheSyncedHeartbeats._mod

    def test_a_sibling_shell_pane_in_the_core_session_is_not_a_core(self):
        hc = self._mod(0, "sutando-core 500\nsutando-core 600\n",
                       panes_argv={"500": "claude --name sutando-core --chrome",
                                   "600": "-zsh"})
        self.assertEqual(hc._local_core_pids(), {"500"})

    def test_a_pool_follower_has_no_launcher_identity_and_is_unverified(self):
        """A pool pane carries no launcher-authored identity ON THIS BASE, so it
        is unverified — NOT owned. The predicate this replaces accepted it on the
        `claude` basename alone, which also accepted an ordinary `--resume`
        sibling (keweichen, #3328).

        This is safe, and the earlier claim that it "restores stop the rest" is
        wrong: unverified roots are named in neither the orphaned group nor the
        stop instruction (see the mirror control below)."""
        hc = self._mod(0, "core-1 74927\n", panes_argv={
            "74927": "/opt/homebrew/bin/claude --dangerously-skip-permissions "
                     "--add-dir /w -- /proactive-loop-pool"})
        self.assertEqual(hc._local_core_pids(), set())

    def test_a_codex_core_pane_is_unverified_not_owned(self):
        """A Codex core is a node script with no `--name`, so it cannot assert
        launcher identity either. Unverified is the honest classification: no
        launcher that could stamp one exists on this base."""
        hc = self._mod(0, "core-4 80059\n", panes_argv={
            "80059": "node /Users/x/.local/bin/codex -C /repo --sandbox danger-full-access"})
        self.assertEqual(hc._local_core_pids(), set())

    def test_a_pane_with_no_ps_row_is_not_a_core(self):
        hc = self._mod(0, "core-1 3951\n", panes_argv={"3951": ""})
        self.assertEqual(hc._local_core_pids(), set())

    def test_ps_unavailable_is_None_not_an_empty_set(self):
        """Same tri-state as tmux: an unreadable process table verifies nothing,
        and an empty set here would send every watcher to `unverified`."""
        hc = self._mod(0, "core-1 3951\n")
        hc._ps_snapshot = lambda: None
        self.assertIsNone(hc._local_core_pids())


class TheSiblingPaneMirrorControl(unittest.TestCase):
    """The reviewer's control, end to end through the real probe: one real core
    pane and one ordinary sibling pane in the SAME canonical session, a watcher
    under each. Only the core descendant may be session-owned, and the sibling's
    must not be handed a restamp repair."""

    def _run(self, sibling_argv):
        hc = _load()
        ws = Path(tempfile.mkdtemp())
        (ws / "state" / "cores").mkdir(parents=True)
        hc.WORKSPACE_DIR = ws
        table = ("100 500 bash src/watch-tasks-stream.sh\n"
                 "200 600 bash src/watch-tasks-stream.sh\n"
                 "500 1 claude --name sutando-core --chrome\n"
                 f"600 1 {sibling_argv}\n")
        hc._ps_snapshot = lambda: table
        hc._watcher_trees = lambda ps_output=None: {"100": ["100"], "200": ["200"]}
        hc._proc_argv = lambda pid: None
        hc._any_core_alive = lambda *a, **k: True
        hc._resolve_tmux_bin = lambda: "/usr/bin/tmux"
        hc._resolve_launch_env = lambda: {}
        hc._local_core_socket = lambda: None

        class R:
            returncode, stdout = 0, "sutando-core 500\nsutando-core 600\n"

        hc.subprocess = types.SimpleNamespace(run=lambda *a, **k: R())
        hc._run_tmux = lambda s, *a: R()
        return hc.check_task_watcher()

    def test_only_the_core_descendant_is_session_owned(self):
        v = self._run("-zsh")
        g = _groups(v["detail"])
        self.assertEqual(g["session-owned"], {"100"})
        self.assertEqual(g["unverified"], {"200"})

    def test_a_non_runtime_exec_carrying_name_is_unverified(self):
        """keweichen, #3328: `python3 worker.py --name sutando-core` is an
        ordinary program. A --name on it is not launcher-authored identity."""
        v = self._run("python3 worker.py --name sutando-core")
        g = _groups(v["detail"])
        self.assertEqual(g["session-owned"], {"100"}, v["detail"])
        self.assertEqual(g["unverified"], {"200"}, v["detail"])
        self.assertIsNone(v.get("_sentinel_restamp_pid"), v["detail"])

    def test_a_name_after_the_option_terminator_is_unverified(self):
        """keweichen, #3328: the launchers run `claude ... -- "/startup"`, so
        every token after `--` is PROMPT TEXT. This is the discriminating form —
        the executable IS a core runtime and the session name IS present."""
        v = self._run("claude -- --name sutando-core")
        g = _groups(v["detail"])
        self.assertEqual(g["session-owned"], {"100"}, v["detail"])
        self.assertEqual(g["unverified"], {"200"}, v["detail"])
        self.assertIsNone(v.get("_sentinel_restamp_pid"), v["detail"])
        self.assertNotIn("_sentinel_restamp_pid", v)

    def test_a_runtime_sibling_without_launcher_identity_is_unverified(self):
        """THE REPORTED BLOCKER (keweichen, #3328 02:30Z). The sibling is a real
        `claude` process, so a runtime-BASENAME predicate calls it a core and
        blesses the watcher it parents. Only launcher-authored identity separates
        them: this pane never passed `--name <session>`.

        The consequence is not cosmetic — Sutando.app runs health-check `--fix`
        at startup and every 30 min, so a blessed duplicate also gets the
        sentinel re-stamped onto its pid. Hence the `_sentinel_restamp_pid`
        assertion: unverified must earn no repair."""
        v = self._run("claude --resume user-work")
        g = _groups(v["detail"])
        self.assertEqual(g["session-owned"], {"100"}, v["detail"])
        self.assertEqual(g["unverified"], {"200"}, v["detail"])
        self.assertNotIn("_sentinel_restamp_pid", v)

    def test_a_prefixed_session_name_does_not_satisfy_identity(self):
        """`--name sutando-core-watcher` must not satisfy session `sutando-core`.
        Substring matching was a live false-healthy path once already (#2488),
        which is why the exact-match policy has one owner and this delegates."""
        g = _groups(self._run("claude --name sutando-core-watcher")["detail"])
        self.assertEqual(g["session-owned"], {"100"})
        self.assertEqual(g["unverified"], {"200"})

    def test_the_control_discriminates(self):
        """Change ONLY the sibling pane's process into a real core runtime and
        the verdict flips — so the assertion above is measuring the runtime
        test, not something incidental to the fixture."""
        g = _groups(self._run("claude --name sutando-core --chrome")["detail"])
        self.assertEqual(g["session-owned"], {"100", "200"})
        self.assertEqual(g["unverified"], set())


class AllOwnerlessCleanupRestartsExactlyOne(unittest.TestCase):
    """The post-cleanup contract is one sentence in every sentinel branch: stop
    the ownerless roots, rerun the probe, start exactly ONE only if none remains.
    Before, only the no-sentinel branch said so; following step 9 in the dead-
    or live-sentinel state stopped every watcher and started none."""

    RESTART = "start exactly ONE"

    def test_every_root_ownerless_restarts_in_all_three_branches(self):
        for nm, kw in EveryMultiRootVerdictNamesEveryGroup.CASES.items():
            for roots in (["100"], ["100", "200"]):
                with self.subTest(f"{nm} / {len(roots)} root(s)"):
                    ps = {r: "1" for r in roots}
                    v = _probe(ps, roots=roots, core_pids={"9"}, **kw)
                    self.assertIn(self.RESTART, v["detail"], v["detail"])
                    self.assertEqual(_groups(v["detail"])["ownerless"], set(roots))

    def test_mixed_ownership_stops_ownerless_and_starts_none(self):
        for nm, kw in EveryMultiRootVerdictNamesEveryGroup.CASES.items():
            with self.subTest(nm):
                v = _probe({"100": "1", "200": "500", "500": "1"},
                           roots=["100", "200"], core_pids={"500"}, **kw)
                d = v["detail"]
                self.assertNotIn(self.RESTART, d, d)
                self.assertIn("rerun this probe", d, d)
                self.assertEqual(_groups(d)["ownerless"], {"100"}, d)
                self.assertEqual(_groups(d)["session-owned"], {"200"}, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
