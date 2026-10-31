#!/usr/bin/env python3
"""The tutorial's rollout fence, measured against the deployed writer: the getting_started.py and
tour_page.py of cf87f324, checked in verbatim under tests/fixtures/old_main/, run as their own
process against the stub room. Every remote write (create, append, replace, summon) is audited.

A sentinel marker value is not a fence: the deployed planner rewrites any recorded, listed page
without reading the recorded hash. A directory at the v1 marker path is: the deployed writer
records an entry before every remote write it can make, and that record cannot land."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
base_spec = importlib.util.spec_from_file_location("tutorial_base", HERE / "tutorial-getting-started.test.py")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)  # isolates the environment
REPO = base.REPO
DEPLOYED = HERE / "fixtures" / "old_main" / "tutorial_cf87f324"
DEPLOYED_SHA = "cf87f324"
PINNED = {"getting_started.py": "43eba3defddcb5592b571b092c0b5100cf9de082216551d9e0808466a30e1396",
          "tour_page.py": "062f55dc3dda8003c6d6c8fc162ff8d61db08c6a181853ef54107316a2befdc8"}
WRITES = ("create", "append", "replace", "summon")
ROOM, ME = "!dm:example.test", "@kai.agent:example.test"
OWNER = "@owner:example.test"
# Every run asks for a summon, so a fenced run that summoned would show in the audit.
SUMMON = ("--owner", OWNER, "--summon")

# Runs the deployed main() with only its two remote seams replaced: the create action and the
# credential lookup (neither is reachable from a test). Everything else is the deployed code.
RUNNER = textwrap.dedent('''
    import importlib.util, json, os, sys
    from pathlib import Path
    script, ws = sys.argv[1], Path(sys.argv[2])
    spec = importlib.util.spec_from_file_location("deployed_getting_started", script)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    state = Path(os.environ["STUB_STATE"])
    class Door:
        def execute(self, room, action, arguments, operation_id=None):
            db = json.loads(state.read_text())
            made = db.setdefault("ops", {})
            if operation_id not in made:
                k = "html-p%d" % db["next"]
                db["next"] += 1
                db["pages"][k] = arguments["body"]
                db.setdefault("by", {})[k] = os.environ["STUB_BY"]
                made[operation_id] = k
            db["calls"].append(["create", made[operation_id], operation_id])
            state.write_text(json.dumps(db))
            return {"kind": made[operation_id], "surface": "html", "operation_id": operation_id}
    m.connect_actions = lambda *a, **k: Door()
    m.commons_env = lambda script, environ=None: dict(os.environ)
    m._workspace = lambda: ws
    sys.exit(m.main(sys.argv[3:]))
''')


def verbatim(name: str) -> bytes:
    """The fixture less its one provenance line."""
    return (DEPLOYED / name).read_bytes().split(b"\n", 1)[1]


def fence(ws: Path) -> Path:
    """The fence the design specifies: the v1 marker moved aside, a directory in its place."""
    marker = ws / "state" / "tutorial" / "getting-started.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.is_file():
        os.link(marker, marker.with_name("getting-started.v1.bak"))
        marker.unlink()
    marker.mkdir()
    (marker / "FENCED").write_text("v2 owns this room's tutorial state; see getting-started.v2.json\n")
    return marker


def tree(path: Path) -> dict:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


class DeployedFenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-fence-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        scripts = self.tmp / "deployed" / "skills" / "tutorial" / "scripts"
        scripts.mkdir(parents=True)
        for name in PINNED:
            (scripts / name).write_bytes(verbatim(name))
        (scripts.parent / "assets").symlink_to(REPO / "skills" / "tutorial" / "assets")
        self.script = scripts / "getting_started.py"
        (self.tmp / "runner.py").write_text(RUNNER)
        (self.tmp / "room_commons.py").write_text(base.STUB)
        self.state = self.tmp / "room.json"
        self.state.write_text(json.dumps({"pages": {}, "calls": [], "next": 1}))

    def deployed(self, ws: Path, name: str = "Blue", by: str = ME):
        env = base.isolated_child_env(STUB_STATE=str(self.state), STUB_BY=by)
        done = subprocess.run(
            [sys.executable, str(self.tmp / "runner.py"), str(self.script), str(ws), "publish",
             "--room", ROOM, "--name", name, "--core", "codex", "--agent-mxid", by,
             "--room-commons", str(self.tmp / "room_commons.py"), *SUMMON],
            capture_output=True, text=True, timeout=120, env=env)
        return done.returncode, done.stdout, done.stderr

    def db(self):
        return json.loads(self.state.read_text())

    def writes(self):
        return [c for c in self.db()["calls"] if c[0] in WRITES]

    def seed(self, ws: Path, name: str = "Blue") -> str:
        """A page the deployed writer created and recorded, as every deployed DM has."""
        code, out, err = self.deployed(ws, name)
        self.assertEqual(code, 0, err + out)
        kind = json.loads(out)["kind"]
        self.assertIn(["summon", kind, OWNER], self.db()["calls"])
        self.reset()
        return kind

    def reset(self):
        db = self.db()
        db["calls"] = []
        self.state.write_text(json.dumps(db))

    def edit(self, kind: str) -> str:
        db = self.db()
        db["pages"][kind] += "<p>OWNER NOTES</p>"
        self.state.write_text(json.dumps(db))
        return db["pages"][kind]

    def test_the_fixture_is_the_deployed_writer_byte_for_byte(self):
        for name, digest in PINNED.items():
            self.assertEqual(hashlib.sha256(verbatim(name)).hexdigest(), digest, name)
        have = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", DEPLOYED_SHA + "^{commit}"],
                              capture_output=True)
        if have.returncode != 0:
            self.skipTest(f"{DEPLOYED_SHA} not in this clone; the pinned digests stand")
        for name in PINNED:
            shown = subprocess.run(["git", "-C", str(REPO), "show", f"{DEPLOYED_SHA}:skills/tutorial/scripts/{name}"],
                                   capture_output=True, check=True).stdout
            self.assertEqual(shown, verbatim(name), name)

    def test_a_sentinel_entry_does_not_fence_the_deployed_writer(self):
        ws = self.tmp / "ws"
        kind = self.seed(ws)
        marker = ws / "state" / "tutorial" / "getting-started.json"
        marker.write_text(json.dumps({"version": 1, "rooms": {ROOM: {
            "kind": kind, "hash": "fenced-by-v2", "state": "complete"}}}))
        self.edit(kind)
        code, out, _ = self.deployed(ws)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "updated")
        self.assertTrue(any(c[0] == "replace" for c in self.writes()), self.writes())
        self.assertNotIn("OWNER NOTES", self.db()["pages"][kind])

    def test_a_fenced_host_makes_no_remote_write_whatever_the_page(self):
        cases = {"owner-edited": lambda ws, k: self.edit(k),
                 "equal to its render": lambda ws, k: None,
                 "equal, then a changed target": lambda ws, k: None}
        for case, prepare in cases.items():
            with self.subTest(case):
                self.state.write_text(json.dumps({"pages": {}, "calls": [], "next": 1}))
                ws = self.tmp / ("ws-" + case.replace(" ", "-").replace(",", ""))
                kind = self.seed(ws)
                prepare(ws, kind)
                before = self.db()["pages"][kind]
                marker = fence(ws)
                fenced = tree(marker)
                runs = ["Blue", "Green"] if case.endswith("changed target") else ["Blue"]
                for name in runs:
                    code, out, _ = self.deployed(ws, name)
                    self.assertEqual(code, 4, out)
                    self.assertFalse(json.loads(out)["ok"])
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.db()["pages"][kind], before)
                self.assertEqual(tree(marker), fenced)

    def test_a_fenced_host_creates_nothing_in_a_room_it_never_recorded(self):
        ws = self.tmp / "ws"
        fence(ws)
        code, out, _ = self.deployed(ws)
        self.assertEqual((code, self.writes(), self.db()["pages"]), (4, [], {}), out)

    def test_a_rollback_leaves_nothing_for_a_re_upgrade_to_reconcile(self):
        ws = self.tmp / "ws"
        kind = self.seed(ws)
        backup = (ws / "state" / "tutorial" / "getting-started.json").read_bytes()
        marker = fence(ws)
        fenced = tree(marker)
        for name in ("Blue", "Green", "Blue"):
            self.assertEqual(self.deployed(ws, name)[0], 4)
        self.assertEqual(tree(marker), fenced)
        self.assertEqual(marker.with_name("getting-started.v1.bak").read_bytes(), backup)
        self.assertEqual(self.writes(), [])
        self.assertIn(kind, self.db()["pages"])

    def test_mixed_hosts_an_unfenced_host_never_writes_a_v2_page(self):
        """Host A has the fence; host B still runs the deployed writer on its own marker. Each host
        is audited on its own, in both orders: A writes nothing, B writes only its own recorded page."""
        for order in ("fenced first", "unfenced first"):
            with self.subTest(order):
                self.state.write_text(json.dumps({"pages": {}, "calls": [], "next": 1}))
                host_a, host_b = self.tmp / order / "host-a", self.tmp / order / "host-b"
                legacy = self.seed(host_b)
                fence(host_a)
                db = self.db()
                candidate = "html-p%d" % db["next"]
                db["next"] += 1
                db["pages"][candidate] = ('<!doctype html><html><head><meta name="sutando-tutorial" '
                                          f'content="{ME}" data-revision="tutorial.2026-10-23.1"></head></html>')
                db.setdefault("by", {})[candidate] = ME
                self.state.write_text(json.dumps(db))
                v2_page = db["pages"][candidate]
                for host in ((host_a, host_b) if order == "fenced first" else (host_b, host_a)):
                    self.reset()
                    code, out, err = self.deployed(host, "Green")
                    if host is host_a:
                        self.assertEqual((code, self.writes()), (4, []), out)
                    else:
                        self.assertEqual(code, 0, err + out)
                        self.assertTrue(self.writes(), "host B refreshes its own page")
                        self.assertEqual({c[1] for c in self.writes()}, {legacy}, self.writes())
                    self.assertEqual(self.db()["pages"][candidate], v2_page)
                # Host B with its marker lost: it adopts only a page carrying its exact deployed tag.
                shutil.rmtree(host_b / "state")
                self.reset()
                code, out, err = self.deployed(host_b, "Blue")
                self.assertEqual(code, 0, err + out)
                self.assertTrue(json.loads(out).get("adopted"), out)
                self.assertEqual({c[1] for c in self.writes()}, {legacy}, self.writes())
                self.assertEqual(self.db()["pages"][candidate], v2_page)

    def test_the_head_writer_is_fenced_too(self):
        ws = self.tmp / "ws"
        kind = self.seed(ws)
        self.edit(kind)
        before = self.db()["pages"][kind]
        fence(ws)
        env = base.isolated_child_env(STUB_STATE=str(self.state), STUB_BY=ME)
        done = subprocess.run(
            [sys.executable, str(self.tmp / "runner.py"), str(base.SCRIPT), str(ws), "publish",
             "--room", ROOM, "--name", "Green", "--core", "codex", "--agent-mxid", ME,
             "--room-commons", str(self.tmp / "room_commons.py"), *SUMMON],
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.db()["pages"][kind], before)


if __name__ == "__main__":
    unittest.main()
