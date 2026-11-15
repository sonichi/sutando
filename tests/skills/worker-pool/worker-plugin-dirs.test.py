#!/usr/bin/env python3
"""`pool_manage.py plugins`: the owner's per-worker opt-in to extra Claude Code plugin dirs.

A recorded dir is a real Claude plugin (refused otherwise), lives on that worker's roster row
only, survives re-registration, and reaches every launch of that worker as --plugin-dir.

Run: python3 tests/skills/worker-pool/worker-plugin-dirs.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load("pool_manage")
pr, wi, sw = pm.pr, pm.wi, pm.sw

WID, OTHER, CODEX = "a" * 32, "b" * 32, "c" * 32


def make_plugin(root: Path, name: str) -> Path:
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    return d


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(os.path.realpath(td.name))
        self.ws = self.tmp / "ws"
        (self.ws / "state").mkdir(parents=True)
        for wid, label, rt in ((WID, "w1", "claude"), (OTHER, "w2", "claude"), (CODEX, "cx", "codex")):
            wi.create_worker(self.ws, runtime=rt, tmux_socket=str(self.tmp / "s"), worker_id=wid,
                             cwd=str(REPO))
            pr.register_worker(self.ws, wid, label, runtime=rt)
        self.mods = self.tmp / "mods"
        self.plugin = make_plugin(self.mods, "pr-watch")

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pm.main(["--workspace", str(self.ws), *args])
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def row(self, wid=WID):
        return pr.load_roster(self.ws)["workers"][wid]


class Record(Base):
    def test_add_records_the_real_path_on_that_worker_only(self):
        link = self.tmp / "link"
        link.symlink_to(self.plugin)
        rc, out = self.main("plugins", "w1", "add", str(link))
        self.assertEqual((rc, out["result"]), (0, "added"))
        self.assertEqual(self.row()["plugin_dirs"], [str(self.plugin)])
        self.assertNotIn("plugin_dirs", self.row(OTHER))

    def test_add_twice_is_unchanged_and_list_shows_it(self):
        self.main("plugins", WID, "add", str(self.plugin))
        rc, out = self.main("plugins", WID, "add", str(self.plugin))
        self.assertEqual((rc, out["result"]), (0, "unchanged"))
        rc, out = self.main("plugins", WID, "list")
        self.assertEqual((rc, out["plugin_dirs"]), (0, [str(self.plugin)]))

    def test_remove_drops_it_and_clears_the_field(self):
        self.main("plugins", WID, "add", str(self.plugin))
        rc, out = self.main("plugins", WID, "remove", str(self.plugin))
        self.assertEqual((rc, out["result"], out["plugin_dirs"]), (0, "removed", []))
        self.assertNotIn("plugin_dirs", self.row())

    def test_remove_works_after_the_dir_is_gone(self):
        self.main("plugins", WID, "add", str(self.plugin))
        shutil.rmtree(self.plugin)
        rc, out = self.main("plugins", WID, "remove", str(self.plugin))
        self.assertEqual((rc, out["result"]), (0, "removed"))

    def test_refuses_what_is_not_a_claude_plugin(self):
        (self.tmp / "bare").mkdir()
        bad_json = make_plugin(self.tmp, "badjson")
        (bad_json / ".claude-plugin" / "plugin.json").write_text("not json")
        (self.tmp / "file").write_text("x")
        for path in (self.tmp / "bare", bad_json, self.tmp / "file", self.tmp / "missing"):
            rc, out = self.main("plugins", WID, "add", str(path))
            self.assertEqual((rc, out["result"]), (2, "refused"), path)
        self.assertNotIn("plugin_dirs", self.row())

    def test_refuses_core_codex_and_a_missing_dir_argument(self):
        for args in (("core", "add", str(self.plugin)), (CODEX, "add", str(self.plugin)),
                     (WID, "add"), ("nobody", "list")):
            rc, out = self.main("plugins", *args)
            self.assertEqual((rc, out["result"]), (2, "refused"), args)

    def test_reregistration_keeps_the_opt_in(self):
        self.main("plugins", WID, "add", str(self.plugin))
        pr.register_worker(self.ws, WID, "w1", runtime="claude")
        self.assertEqual(self.row()["plugin_dirs"], [str(self.plugin)])

    def test_a_malformed_row_is_refused_by_the_roster_boundary(self):
        with self.assertRaises(pr.RosterError):
            pr.validate_workers({WID: {"state": "live", "plugin_dirs": ["relative/dir"]}})


class Launch(Base):
    def test_plan_hands_this_workers_dirs_and_nobody_elses(self):
        self.main("plugins", WID, "add", str(self.plugin))
        self.assertEqual(sw.plan(self.ws, REPO, worker_id=WID)["env"]["SUTANDO_WORKER_PLUGIN_DIRS"],
                         str(self.plugin))
        self.assertEqual(sw.plan(self.ws, REPO, worker_id=OTHER)["env"]["SUTANDO_WORKER_PLUGIN_DIRS"], "")
        self.assertEqual(sw.plan(self.ws, REPO)["env"]["SUTANDO_WORKER_PLUGIN_DIRS"], "")

    def test_plan_skips_a_dir_that_stopped_being_a_plugin(self):
        second = make_plugin(self.mods, "other")
        self.main("plugins", WID, "add", str(self.plugin))
        self.main("plugins", WID, "add", str(second))
        shutil.rmtree(self.plugin)
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            env = sw.plan(self.ws, REPO, worker_id=WID)["env"]
        self.assertEqual(env["SUTANDO_WORKER_PLUGIN_DIRS"], str(second))
        self.assertIn("skipping recorded plugin dir", err.getvalue())

    def test_the_launcher_turns_each_line_into_one_plugin_dir(self):
        stub = self.tmp / "bin"
        stub.mkdir()
        for t in ("lsof", "launchctl", "pgrep", "tmux"):
            (stub / t).write_text("#!/bin/sh\nexit 1\n")
            (stub / t).chmod(0o755)
        spaced = make_plugin(self.mods, "has space")
        env = {"HOME": os.environ.get("HOME", str(self.tmp)),
               "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
               "SUTANDO_TMUX_SESSION": "sutando-worker-test", "SUTANDO_INSTANCE_ID": WID,
               "SUTANDO_TASK_WATCH_MOD_WORKER": "0",
               "SUTANDO_WORKER_PLUGIN_DIRS": f"{self.plugin}\n{spaced}\n"}
        r = subprocess.run(["bash", str(SCRIPTS / "launch-worker-session.sh"), "--print-surface-args"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        pairs = [lines[i + 1] for i, a in enumerate(lines) if a == "--plugin-dir"]
        self.assertEqual(pairs[-2:], [str(self.plugin), str(spaced)])
        env["SUTANDO_WORKER_PLUGIN_DIRS"] = ""
        r = subprocess.run(["bash", str(SCRIPTS / "launch-worker-session.sh"), "--print-surface-args"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(str(self.plugin), r.stdout)


if __name__ == "__main__":
    unittest.main()
