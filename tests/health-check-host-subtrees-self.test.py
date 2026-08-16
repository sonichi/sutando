#!/usr/bin/env python3
"""This host's own subtree is not evidence that sync ran.

`check_host_subtrees` ages each `hosts/<label>/` by its newest file mtime and
reports the result as sync freshness. That reading holds for a PEER — its files
arrive here only by sync — and fails for the local host, whose core rewrites
`current-track.md`, `pending-questions.md` and `crons.json` continuously. The
local subtree is therefore always fresh, including where sync has never run.

The load-bearing case is `test_only_own_subtree_does_not_claim_sync`: it FAILS on
the parent commit, which answers "1 host subtree(s), all synced <7d" for a
single-machine workspace with `vault.enabled=false`. The peer cases would pass
against an implementation that changed nothing, so they prove nothing alone —
they are here to pin that peer behaviour is untouched.
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_host_subtrees", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_host_subtrees"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load()
SELF = "This-Very-Host"


class HostSubtreeSelfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "hosts").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        # WORKSPACE_DIR is module state, and the label probe must be stated by
        # each case rather than inherited — left real it reads the developer's
        # own hostname and the fixtures stop meaning anything.
        ws = mock.patch.object(hc, "WORKSPACE_DIR", self.ws)
        ws.start(); self.addCleanup(ws.stop)

    def _subtree(self, label, age_days):
        d = self.ws / "hosts" / label
        d.mkdir(parents=True, exist_ok=True)
        f = d / "current-track.md"
        f.write_text("x")
        when = time.time() - age_days * 86400
        os.utime(f, (when, when))
        return d

    def _run(self, labels=(SELF,), stale_days=None):
        env = {"SUTANDO_STALE_HOST_DAYS": str(stale_days)} if stale_days else {}
        with mock.patch.object(hc, "_local_host_labels", return_value=set(labels)), \
             mock.patch.dict(os.environ, env, clear=False):
            return hc.check_host_subtrees()

    # ---- THE regression pin: fails on the parent commit ---------------------

    def test_only_own_subtree_does_not_claim_sync(self):
        """A single-machine workspace. The subtree is fresh because the core
        writes it, and the parent commit reports that as 'all synced <7d'."""
        self._subtree(SELF, age_days=0)
        out = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("all synced", out["detail"])
        self.assertIn("local writes", out["detail"])
        self.assertIn(SELF, out["detail"])

    def test_own_subtree_is_not_counted_among_fresh_peers(self):
        """With one real peer, the count must be 1 — not 2."""
        self._subtree(SELF, age_days=0)
        self._subtree("Peer-Laptop", age_days=1)
        out = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertIn("1 peer subtree(s)", out["detail"])
        self.assertIn("all synced", out["detail"])

    def test_a_stale_own_subtree_is_never_reported_as_stopped_syncing(self):
        """'host stopped syncing?' is the wrong sentence about the machine
        reading it — a core that stopped writing is other probes' business."""
        self._subtree(SELF, age_days=99)
        out = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("stopped syncing", out["detail"])

    # ---- peer behaviour must be untouched (passes on the parent too) --------

    def test_a_stale_peer_still_warns(self):
        self._subtree(SELF, age_days=0)
        self._subtree("Dead-Peer", age_days=30)
        out = self._run()
        self.assertEqual(out["status"], "warn")
        self.assertIn("Dead-Peer", out["detail"])
        self.assertIn("stopped syncing", out["detail"])

    def test_stale_threshold_still_honours_the_env_override(self):
        self._subtree("Peer-Laptop", age_days=3)
        self.assertEqual(self._run(stale_days=2)["status"], "warn")
        self.assertEqual(self._run(stale_days=10)["status"], "ok")

    # ---- degraded paths -----------------------------------------------------

    def test_unknown_local_label_falls_back_instead_of_excluding_a_peer(self):
        """If the label cannot be resolved, excluding a subtree would discard a
        real peer's staleness. Restore the old counting and SAY the label is
        unknown, rather than silently guessing which subtree is ours."""
        self._subtree("Some-Host", age_days=0)
        with mock.patch.object(hc, "_local_host_labels", side_effect=OSError("no hostname")):
            out = hc.check_host_subtrees()
        self.assertEqual(out["status"], "ok")
        self.assertIn("could not identify", out["detail"])

    def test_an_empty_subtree_is_not_aged(self):
        (self.ws / "hosts" / "Empty-Host").mkdir()
        self._subtree(SELF, age_days=0)
        out = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("Empty-Host", out["detail"])

    def test_only_empty_peer_subtrees_says_so_rather_than_naming_a_host(self):
        """`fresh`, `stale` and `own` all empty. Reachable on a fresh clone that
        has a hosts/ dir before any host has written into it, so the sentence
        must not claim a subtree that is not there."""
        (self.ws / "hosts" / "Nobody-Yet").mkdir()
        out = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertIn("no datable subtree", out["detail"])
        self.assertNotIn("local writes", out["detail"])

    def test_no_hosts_dir_is_unchanged(self):
        with mock.patch.object(hc, "WORKSPACE_DIR", self.ws / "nope"):
            self.assertEqual(hc.check_host_subtrees()["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
