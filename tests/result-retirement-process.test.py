#!/usr/bin/env python3
"""Production publication and generation retirement composed across fresh processes.

Run: python3 tests/result-retirement-process.test.py -v
"""
from __future__ import annotations

import atexit
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ
from _helpers.subprocess_coverage import covered_code

PROCESS_HOME = Path(tempfile.mkdtemp(prefix="result-retirement-home-")).resolve()
atexit.register(shutil.rmtree, PROCESS_HOME)
# This standalone process owns helper coverage scratch as well as its fixtures.
tempfile.tempdir = str(PROCESS_HOME)
isolate_os_environ(home=PROCESS_HOME, TMPDIR=str(PROCESS_HOME))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import result_publish
from delivery import disposal
from delivery.readiness import identity_of

WAIT_S = 8.0
BODY_A = "Generation A: the reply both observers read.\nUnicode: café.\n"
BODY_B = "Generation B: a newer reply published while disposal is locked.\nUnicode: 日本語.\n"
ARCHIVE_NAMES = ["task-shared-1.txt", "task-shared-2.txt"]
CHILD_CODE = r'''
import errno
import json
import os
import sys
import time
from pathlib import Path

src, workspace, ipc, name, mode, body = sys.argv[1:]
sys.path.insert(0, src)
import result_publish
from delivery import disposal
from delivery.readiness import identity_of

workspace, ipc = Path(workspace), Path(ipc)
results = workspace / "results"
target = results / "task-shared.txt"

def report(phase, data):
    path = ipc / (phase + "-" + name + ".json")
    staged = path.with_suffix(".part")
    staged.write_text(json.dumps(data), encoding="utf-8")
    staged.replace(path)

def wait_for(gate):
    deadline = time.monotonic() + 20.0
    while not (ipc / gate).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("gate did not open: " + gate)
        time.sleep(0.01)

ready = {"pid": os.getpid(), "home": str(Path.home()), "cwd": str(Path.cwd()),
         "primitive": disposal.undelivered_quarantine.RENAME_PRIMITIVE}
if mode == "observe":
    data, generation = identity_of(target)
    ready.update(body=data.decode("utf-8"), generation=list(generation))
report("ready", ready)
wait_for("retire" if mode == "observe" else "publish")
if mode == "observe":
    real_lock_fd = disposal.lock_fd
    def observed_lock_fd(*args, **kwargs):
        try:
            return real_lock_fd(*args, **kwargs)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
                report("contended", {"errno": exc.errno})
            raise
    disposal.lock_fd = observed_lock_fd
    lines = []
    done = disposal.retire_generation(
        results, target, generation, lines.append, results / "archive",
        ["task-shared-1.txt", "task-shared-2.txt"])
    report("done", {"outcome": done.outcome.value, "retired": done.retired,
                    "path": str(done.path) if done.path else None,
                    "cause": done.cause, "warning": done.warning, "logs": lines})
else:
    published = result_publish.publish_text(target, body)
    data, generation = identity_of(published)
    report("done", {"path": str(published), "body": data.decode("utf-8"),
                    "generation": list(generation)})
'''


class ResultRetirementProcesses(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="result-retirement-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.results = self.workspace / "results"
        self.results.mkdir(parents=True)
        self.target = self.results / "task-shared.txt"
        self.ipc = self.root / "ipc"
        self.ipc.mkdir()
        self.children = {}
        self.homes = {}
        self.sentinels = {}
        for target in (self.results / "task-neighbor.txt",
                       self.root / "other-workspace" / "results" / self.target.name):
            result_publish.publish_text(target, "Unrelated publication: " + target.parent.parent.name)
            self.sentinels[target] = identity_of(target)
        result_publish.publish_text(self.target, BODY_A)
        self.generation = identity_of(self.target)[1]

    def _stop_child(self, process):
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _start(self, name, mode):
        home = self.root / (name + "-home")
        home.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", covered_code(CHILD_CODE), str(REPO / "src"),
             str(self.workspace), str(self.ipc), name, mode, BODY_B],
            cwd=self.workspace, env=hermetic_env(home, TMPDIR=str(home)), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_child, process)
        self.children[name] = process
        self.homes[name] = home

    def _record(self, phase, name, deadline=None):
        deadline = time.monotonic() + WAIT_S if deadline is None else deadline
        path = self.ipc / (phase + "-" + name + ".json")
        process = self.children[name]
        while True:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=2)
                    self.fail(f"{name} exited {process.returncode} before {phase}: {stdout}\n{stderr}")
                if time.monotonic() >= deadline:
                    self.fail(f"{name} did not report {phase} within the deadline")
                time.sleep(0.01)

    def _observers(self, replacement):
        for name in ("observer-1", "observer-2"):
            self._start(name, "observe")
        if replacement:
            self._start("publisher", "publish")
        pids = set()
        deadline = time.monotonic() + WAIT_S
        for name, process in self.children.items():
            ready = self._record("ready", name, deadline)
            self.assertEqual(ready["pid"], process.pid)
            self.assertNotEqual(ready["pid"], os.getpid())
            pids.add(ready["pid"])
            self.assertEqual(Path(ready["home"]), self.homes[name])
            self.assertEqual(Path(ready["cwd"]), self.workspace)
            if name.startswith("observer"):
                self.assertEqual(ready["body"], BODY_A)
                self.assertEqual(ready["generation"], list(self.generation))
                if replacement:
                    self.assertNotEqual(ready["primitive"], "none")
        self.assertEqual(len(pids), len(self.children))

    def _assert_blocked(self):
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            for name in ("observer-1", "observer-2"):
                self.assertIsNone(self.children[name].poll(), name + " exited while the lock was held")
                self.assertFalse((self.ipc / ("done-" + name + ".json")).exists(),
                                 name + " retired while the coordinator held the lock")
            time.sleep(0.01)

    def _run_schedule(self, replacement):
        self._observers(replacement)
        # All children have exec'd before the coordinator takes the lock.
        with disposal.locked(self.results):
            deadline = time.monotonic() + WAIT_S
            (self.ipc / "retire").touch()
            for name in ("observer-1", "observer-2"):
                contended = self._record("contended", name, deadline)
                self.assertIn(contended["errno"], (errno.EAGAIN, errno.EACCES,
                                                   getattr(errno, "EWOULDBLOCK", errno.EAGAIN)))
            if replacement:
                (self.ipc / "publish").touch()
                published = self._record("done", "publisher", deadline)
                self.assertEqual((published["path"], published["body"]), (str(self.target), BODY_B))
                self.replacement = identity_of(self.target)
                self.assertEqual(self.replacement[0], BODY_B.encode("utf-8"))
                self.assertEqual(list(self.replacement[1]), published["generation"])
            self._assert_blocked()
        records = [self._record("done", name) for name in ("observer-1", "observer-2")]
        for name, process in self.children.items():
            stdout, stderr = process.communicate(timeout=WAIT_S)
            self.assertEqual(process.returncode, 0, name + ": " + stdout + stderr)
            self.assertEqual((stdout, stderr), ("", ""), name)
        for record in records:
            self.assertEqual((record["cause"], record["warning"]), ("", ""), record)
        return records

    def _assert_isolated_and_clean(self):
        for path, snapshot in self.sentinels.items():
            self.assertEqual(identity_of(path), snapshot, str(path.relative_to(self.root)))
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(list(self.root.rglob("*.disposing-*")), [])
        self.assertEqual(list(self.root.rglob("*" + result_publish.STAGED_SUFFIX)), [])
        self.assertEqual(list((self.results / "undelivered").glob("*")), [])

    def _require_native_noreplace(self):
        probe = self.root / "probe"
        probe.mkdir()
        source, destination = probe / "source", probe / "destination"
        source.write_bytes(b"probe")
        try:
            disposal.rename_noreplace(source, destination)
        except FileExistsError as exc:
            if exc.errno == errno.ENOTSUP:
                self.skipTest("replacement-live requires a native no-replace rename on this filesystem")
            raise
        source.write_bytes(b"collision")
        with self.assertRaises(FileExistsError) as refused:
            disposal.rename_noreplace(source, destination)
        self.assertEqual(refused.exception.errno, errno.EEXIST)
        self.assertEqual((source.read_bytes(), destination.read_bytes()), (b"collision", b"probe"))

    def test_two_processes_leave_a_new_publication_live(self):
        self._require_native_noreplace()
        records = self._run_schedule(replacement=True)
        self.assertEqual([record["outcome"] for record in records],
                         [disposal.Retirement.REPLACEMENT_LIVE.value] * 2)
        self.assertEqual([(record["retired"], record["path"]) for record in records],
                         [(False, None)] * 2)
        self.assertEqual(identity_of(self.target), self.replacement)
        self.assertNotEqual(identity_of(self.target)[1], self.generation)
        self.assertEqual(list((self.results / "archive").glob("*")), [])
        self.assertEqual(sorted(path.name for path in self.results.iterdir()),
                         [disposal.LOCK_NAME, "task-neighbor.txt", self.target.name])
        self._assert_isolated_and_clean()

    def test_two_processes_retire_one_publication_exactly_once(self):
        records = self._run_schedule(replacement=False)
        self.assertCountEqual([record["outcome"] for record in records],
                              [disposal.Retirement.PLACED.value, disposal.Retirement.SOURCE_GONE.value])
        self.assertTrue(all(record["retired"] for record in records))
        placed = next(record for record in records if record["outcome"] == disposal.Retirement.PLACED.value)
        gone = next(record for record in records if record["outcome"] == disposal.Retirement.SOURCE_GONE.value)
        archived = self.results / "archive" / ARCHIVE_NAMES[0]
        self.assertEqual(placed["path"], str(archived))
        self.assertIsNone(gone["path"])
        self.assertEqual(list(archived.parent.iterdir()), [archived])
        self.assertEqual(identity_of(archived), (BODY_A.encode("utf-8"), self.generation))
        self.assertFalse(self.target.exists())
        self.assertEqual(sorted(path.name for path in self.results.iterdir()),
                         [disposal.LOCK_NAME, "archive", "task-neighbor.txt"])
        self._assert_isolated_and_clean()


if __name__ == "__main__":
    unittest.main()
