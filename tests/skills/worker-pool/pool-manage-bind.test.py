#!/usr/bin/env python3
"""`pool_manage.py bind` / `unbind`: the owner's way to bind a room now the picker's pin and unpin are gone.

`bind <room> <worker>` binds through `pool_roster.bind_room` (id or label) and the router then delivers that
room's tasks to the worker; `unbind <room>` releases it to the core (absent is not an error). A worker on its
own task folders is refused (exit 2), since the router never delivers to one; an unknown worker is refused.

Run: python3 tests/skills/worker-pool/pool-manage-bind.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "skills/worker-pool/scripts"), str(REPO / "src"), str(REPO / "packages/ag2-sparrow")]

import connections as C, pool_manage as pm  # noqa: E401,E402

pr, wi = pm.pr, pm.wi
ROOM = "!bound:srv.example"


def make_worker(ws, label):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket="/recorded/app/run/tmux.sock", worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


class Bind(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.alpha = make_worker(self.ws, "alpha")

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pm.main(["--workspace", str(self.ws), *argv])
        return rc, json.loads(out.getvalue())

    def bindings(self):
        return (pr.load_roster(self.ws) or {}).get("bindings") or {}

    def test_bind_by_label_then_unbind(self):
        rc, out = self.cli("bind", ROOM, "alpha")
        self.assertEqual((rc, out["result"], out["worker"]), (0, "bound", self.alpha))
        self.assertEqual(self.bindings().get(ROOM), self.alpha)
        self.assertEqual(pr.targets_for(pr.load_roster(self.ws), ROOM), [self.alpha])
        rc, out = self.cli("unbind", ROOM)
        self.assertEqual((rc, out["result"]), (0, "unbound"))
        self.assertNotIn(ROOM, self.bindings())
        self.assertEqual(self.cli("unbind", ROOM)[0], 0)

    def test_an_unknown_worker_is_refused(self):
        rc, out = self.cli("bind", ROOM, "nobody")
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertNotIn(ROOM, self.bindings())

    def test_a_worker_on_its_own_folders_is_refused(self):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        base = {"server": "srv.example", "gateway_url": "https://chat.srv.example", "added_by": "owner",
                "added_at": "2026-11-16T00:00:00Z", "verified": True, "server_source": "gateway"}
        path.write_text(json.dumps({"schema_version": 1, "connections": [
            dict(base, instance="srv-example", token_ref="AG2_REMOTE_TOKEN_A", primary=True),
            dict(base, instance="srv-example-a", token_ref="AG2_REMOTE_TOKEN_B", primary=False,
                 worker=self.alpha, agent_mxid="@alpha-agent:srv.example", dirs="own")]}))
        rc, out = self.cli("bind", ROOM, "alpha")
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertIn("own folders", out["why"])
        self.assertNotIn(ROOM, self.bindings())

    def test_a_failed_unbind_reports_without_a_worker(self):
        from unittest import mock
        with mock.patch.object(pr, "unbind_room", side_effect=OSError("disk")):
            rc, out = self.cli("unbind", ROOM)
        self.assertEqual((rc, out["result"], out["worker"]), (1, "failed", None))


if __name__ == "__main__":
    unittest.main()
