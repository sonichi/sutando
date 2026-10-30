#!/usr/bin/env python3
"""src/launch_hooks.py directly: the runner starts each injected hook detached (a .py under the
given python, anything else under bash, in its own session, stdin empty, output in its log) and
returns at once; a hook that cannot start is reported and the rest still start; the edge starts
exactly the enabled, contained `on_launch` declarers and never fails; the POSIX dispatcher starts
them and hands off without waiting. Also the field's contract in the canonical schema and in
scripts/lint-skill.py."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

# The runner drops each child unwaited on purpose; Popen's "still running" warning is that, by design.
warnings.filterwarnings("ignore", "subprocess .* is still running", ResourceWarning)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import launch_hooks as lh, skill_roots, workspace_default  # noqa: E401,E402

_spec = importlib.util.spec_from_file_location("lint_skill_launch", REPO / "scripts" / "lint-skill.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

PY_HOOK = """import json, os, sys, time
gate = os.environ.get("HOOK_GATE")
while gate and not os.path.exists(gate):
    time.sleep(0.05)
out = os.path.join(os.environ["HOOK_OUT"], os.path.basename(__file__) + ".json")
sid = os.getsid(0) if hasattr(os, "getsid") else None
json.dump({"exe": sys.executable, "sid": sid, "stdin": sys.stdin.read()}, open(out, "w"))
print("py hook ran")
"""
SH_HOOK = """out="$HOOK_OUT/$(basename "$0").json"
printf '{"bash": "%s", "sid": %s, "stdin": "%s"}' "$BASH_VERSION" "$("$HOOK_PY" -c 'import os; print(os.getsid(0))')" "$(cat)" > "$out"
echo "sh hook ran"
"""


def wait_for(path: Path, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError):
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="launch-hooks-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out, self.logs = self.tmp / "out", self.tmp / "logs"
        self.out.mkdir()
        self.env = {**os.environ, "HOOK_OUT": str(self.out), "HOOK_PY": sys.executable}

    def hook(self, name: str, body: str) -> Path:
        path = self.tmp / "hooks" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
        return path


class RunnerTest(_Tmp):
    @unittest.skipIf(os.name == "nt", "a .sh hook, sessions and FIFOs are POSIX")
    def test_a_py_hook_runs_under_the_given_python_and_anything_else_under_bash_detached(self):
        py, sh = self.hook("one.py", PY_HOOK), self.hook("two.sh", SH_HOOK)
        # This process's stdin carries data; a hook must not inherit it.
        r, w = os.pipe()
        os.write(w, b"leaked")
        os.close(w)
        saved = os.dup(0)
        os.dup2(r, 0)
        try:
            started = lh.start([("one", py), ("two", sh)], self.logs, sys.executable, self.env)
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(r)
        self.assertEqual([n for n, _ in started], ["one", "two"])
        a, b = wait_for(self.out / "one.py.json"), wait_for(self.out / "two.sh.json")
        self.assertEqual(Path(a["exe"]).resolve(), Path(sys.executable).resolve())
        self.assertTrue(b["bash"], "the .sh hook ran under bash")
        self.assertEqual([a["sid"], b["sid"]], [pid for _, pid in started], "each hook leads its own session")
        self.assertNotIn(os.getsid(0), (a["sid"], b["sid"]))
        self.assertEqual((a["stdin"], b["stdin"]), ("", ""))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all(
                (self.logs / f"launch-hook-{n}.log").exists()
                and "ran" in (self.logs / f"launch-hook-{n}.log").read_text() for n in ("one", "two")):
            time.sleep(0.05)
        self.assertIn("py hook ran", (self.logs / "launch-hook-one.log").read_text())
        self.assertIn("sh hook ran", (self.logs / "launch-hook-two.log").read_text())

    @unittest.skipIf(os.name == "nt", "os.kill(pid, 0) is a probe only on POSIX")
    def test_start_returns_while_the_hook_still_runs(self):
        gate = self.tmp / "gate"
        env = {**self.env, "HOOK_GATE": str(gate)}
        self.addCleanup(gate.touch)  # whatever happens, the hook can finish
        got = []
        caller = threading.Thread(target=lambda: got.append(
            lh.start([("slow", self.hook("slow.py", PY_HOOK))], self.logs, sys.executable, env)), daemon=True)
        caller.start()
        caller.join(10)
        self.assertFalse(caller.is_alive(), "start() waited on the hook")
        (_, pid), = got[0]
        os.kill(pid, 0)  # still running: nobody waited for it
        self.assertFalse((self.out / "slow.py.json").exists())
        gate.touch()
        wait_for(self.out / "slow.py.json")

    @unittest.skipIf(os.name == "nt", "a .sh hook, sessions and FIFOs are POSIX")
    def test_a_hook_that_cannot_start_is_reported_and_the_others_still_start(self):
        bad, good = self.hook("bad.py", PY_HOOK), self.hook("good.sh", SH_HOOK)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = lh.start([("bad", bad), ("good", good)], self.logs, str(self.tmp / "no-such-python"), self.env)
        self.assertEqual([n for n, _ in started], ["good"])
        self.assertIn("launch-hooks: bad: not started (FileNotFoundError)", err.getvalue())
        wait_for(self.out / "good.sh.json")


    def in_time(self, call, seconds=10):
        """`call()` in a thread: its result, failing if it is still running after `seconds`."""
        got = []
        t = threading.Thread(target=lambda: got.append(call()), daemon=True)
        t.start()
        t.join(seconds)
        self.assertFalse(t.is_alive(), "start() blocked")
        return got[0]

    @unittest.skipIf(os.name == "nt", "a .sh hook, sessions and FIFOs are POSIX")
    def test_a_fifo_or_a_symlink_at_the_log_path_starts_nothing_and_blocks_nothing(self):
        self.logs.mkdir()
        os.mkfifo(self.logs / "launch-hook-pipe.log")
        outside = self.tmp / "outside.log"
        outside.write_text("untouched\n")
        (self.logs / "launch-hook-link.log").symlink_to(outside)
        hooks = [("pipe", self.hook("pipe.py", PY_HOOK)), ("link", self.hook("link.py", PY_HOOK)),
                 ("fine", self.hook("fine.sh", SH_HOOK))]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = self.in_time(lambda: lh.start(hooks, self.logs, sys.executable, self.env))
        self.assertEqual([n for n, _ in started], ["fine"])
        self.assertIn("launch-hooks: pipe: not started (log:", err.getvalue())
        self.assertIn("launch-hooks: link: not started (log:", err.getvalue())
        wait_for(self.out / "fine.sh.json")
        self.assertEqual(outside.read_text(), "untouched\n")
        self.assertFalse((self.out / "pipe.py.json").exists() or (self.out / "link.py.json").exists())

    def test_a_script_that_is_neither_py_nor_sh_is_never_run(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = lh.start([("txt", self.hook("run.txt", SH_HOOK))], self.logs, sys.executable, self.env)
        self.assertEqual(started, [])
        self.assertIn("launch-hooks: txt: not started (not a .py or .sh script)", err.getvalue())
        time.sleep(0.3)
        self.assertFalse((self.out / "run.txt.json").exists())


class EdgeTest(_Tmp):
    def skill(self, root: Path, name: str, manifest: dict, script: str = "", body: str = PY_HOOK) -> None:
        d = root / name
        (d / "scripts").mkdir(parents=True)
        if script:
            (d / script).write_text(body)
        (d / "manifest.json").write_text(json.dumps(manifest))

    @unittest.skipIf(os.name == "nt", "a .sh hook, sessions and FIFOs are POSIX")
    def test_main_starts_every_enabled_contained_declarer_and_nothing_else(self):
        roots = self.tmp / "skills"
        self.skill(roots, "alpha", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        self.skill(roots, "beta", {"on_launch": "scripts/b.sh"}, "scripts/b.sh", SH_HOOK)
        self.skill(roots, "off", {"on_launch": "scripts/c.py", "enabled": False}, "scripts/c.py")
        self.skill(roots, "escape", {"on_launch": "../alpha/scripts/a.py"})
        self.skill(roots, "missing", {"on_launch": "scripts/none.py"})
        self.skill(roots, "plain", {"name": "plain"})
        self.skill(roots, "aa-nul", {"on_launch": "scripts/\x00.py"})  # sorts first: must not stop the scan
        self.skill(roots, "ab-long", {"on_launch": "scripts/" + "a" * 5000 + ".py"})
        self.skill(roots, "txt", {"on_launch": "scripts/run.txt"}, "scripts/run.txt", SH_HOOK)
        err = io.StringIO()
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.tmp / "ws"), \
                mock.patch.object(skill_roots, "skill_roots", return_value=[roots]), \
                mock.patch.dict(os.environ, {"HOOK_OUT": str(self.out), "HOOK_PY": sys.executable}), \
                contextlib.redirect_stderr(err):
            self.assertEqual(lh.main(), 0)
        self.assertEqual(re.findall(r"started (\w+)", err.getvalue()), ["alpha", "beta"])
        wait_for(self.out / "a.py.json")
        wait_for(self.out / "b.sh.json")
        self.assertEqual(sorted(p.name for p in (self.tmp / "ws" / "logs").iterdir()),
                         ["launch-hook-alpha.log", "launch-hook-beta.log"])

    def run_main(self, roots: Path, seconds: float = 20) -> str:
        """lh.main() over `roots` in a thread; its stderr, failing if it is still running after `seconds`."""
        err, done = io.StringIO(), threading.Event()

        def call():
            with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.tmp / "ws"), \
                    mock.patch.object(skill_roots, "skill_roots", return_value=[roots]), \
                    mock.patch.dict(os.environ, {"HOOK_OUT": str(self.out), "HOOK_PY": sys.executable}), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(lh.main(), 0)
            done.set()
        threading.Thread(target=call, daemon=True).start()
        self.assertTrue(done.wait(seconds), "lh.main() blocked: the launch would never hand off")
        return err.getvalue()

    @staticmethod
    def release_fifo(fifo: Path) -> None:
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))

    @unittest.skipIf(os.name == "nt", "no FIFOs in a Windows tree")
    def test_a_fifo_manifest_neither_blocks_discovery_nor_hides_a_later_hook(self):
        roots = self.tmp / "skills"
        (roots / "aa-fifo").mkdir(parents=True)
        fifo = roots / "aa-fifo" / "manifest.json"
        os.mkfifo(fifo)
        # A blocked reader from a failing run is released, so the suite itself never hangs.
        self.addCleanup(self.release_fifo, fifo)
        self.skill(roots, "zz-good", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        self.assertEqual(re.findall(r"started (\S+)", self.run_main(roots, 5)), ["zz-good"])
        wait_for(self.out / "a.py.json")

    def test_an_oversized_or_non_regular_manifest_is_skipped_and_a_later_hook_still_starts(self):
        roots = self.tmp / "skills"
        self.skill(roots, "aa-big", {}, "scripts/a.py")
        (roots / "aa-big" / "manifest.json").write_text(
            json.dumps({"on_launch": "scripts/a.py", "pad": "x" * (skill_roots.MANIFEST_MAX_BYTES + 1)}))
        (roots / "ab-dir" / "manifest.json").mkdir(parents=True)
        self.skill(roots, "zz-good", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        self.assertEqual(re.findall(r"started (\S+)", self.run_main(roots)), ["zz-good"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_self_referential_hook_symlink_is_skipped_and_a_later_hook_still_starts(self):
        roots = self.tmp / "skills"
        self.skill(roots, "aa-loop", {"on_launch": "scripts/loop.py"})
        (roots / "aa-loop" / "scripts" / "loop.py").symlink_to("loop.py")
        self.skill(roots, "zz-good", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        self.assertEqual(re.findall(r"started (\S+)", self.run_main(roots)), ["zz-good"])

    def test_any_error_resolving_one_declaration_skips_only_that_one(self):
        # Python 3.9 to 3.12 raise RuntimeError for a symlink loop; pinned here on every interpreter.
        roots = self.tmp / "skills"
        self.skill(roots, "aa-bad", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        self.skill(roots, "zz-good", {"on_launch": "scripts/a.py"}, "scripts/a.py")
        real = Path.resolve

        def resolve(path, *a, **k):
            if "aa-bad" in str(path) and str(path).endswith(".py"):
                raise RuntimeError("Symlink loop from " + str(path))
            return real(path, *a, **k)
        with mock.patch.object(Path, "resolve", resolve):
            found = skill_roots.declared_scripts(lh.FIELD, roots, lh.SUFFIXES)
        self.assertEqual([n for n, _ in found], ["zz-good"])

    def test_main_hands_off_when_discovery_never_returns(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(lh, "DISCOVERY_SECONDS", 0.5), \
                mock.patch.object(skill_roots, "declared_scripts", lambda *a, **k: release.wait()):
            err = self.run_main(self.tmp / "skills", 5)
        self.assertIn("launch-hooks: none started (discovery timed out)", err)

    def test_main_exits_0_when_the_scan_fails(self):
        err = io.StringIO()
        with mock.patch.object(workspace_default, "resolve_workspace", side_effect=RuntimeError("no config")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(lh.main(), 0)
        self.assertIn("launch-hooks: none started (RuntimeError)", err.getvalue())


class ContractTest(_Tmp):
    schema = json.loads((REPO / "schemas" / "skill-manifest.schema.json").read_text())

    def test_the_schema_declares_the_field_as_a_contained_py_or_sh_path(self):
        props = self.schema["properties"]
        self.assertIs(self.schema["additionalProperties"], False)
        pattern = props[lh.FIELD]["pattern"]
        for good in ("scripts/on_launch.py", "scripts/x.sh", "hook.py"):
            self.assertTrue(re.match(pattern, good), good)
        for bad in ("../x.py", "/abs/x.py", "scripts/../x.py", "scripts/a b.py", "scripts/x.txt", "", "x.py/.."):
            self.assertFalse(re.match(pattern, bad), bad)

    @unittest.skipIf(os.name == "nt", "plants a symlink")
    def test_the_linter_validates_the_field_rather_than_only_allowing_it(self):
        def run(value, link=None):
            d = self.tmp / "sk"
            shutil.rmtree(d, ignore_errors=True)
            (d / "scripts").mkdir(parents=True)
            (d / "scripts" / "hook.py").write_text("# hook\n")
            (d / "scripts" / "hook.sh").write_text("# hook\n")
            if link:
                (d / "scripts" / "link.py").symlink_to(link)
            (d / "manifest.json").write_text(json.dumps({"name": "sk", "version": "1.0.0", "owner": "x",
                                                         "stability": "experimental", lh.FIELD: value}))
            errors, _ = lint._lint_manifest(d)
            return [e for e in errors if lh.FIELD in e]
        outside = self.tmp / "outside.py"
        outside.write_text("# not the skill's\n")
        self.assertEqual(run("scripts/hook.py"), [])
        self.assertEqual(run("scripts/hook.sh"), [])
        self.assertIn("does not exist", run("scripts/none.py")[0])
        self.assertIn("must stay inside the skill directory", run("/etc/hook.py")[0])
        self.assertIn("must stay inside the skill directory", run("../outside.py")[0])
        self.assertIn("must stay inside the skill directory", run("scripts/../../outside.py")[0])
        self.assertIn("must resolve inside the skill directory", run("scripts/link.py", outside)[0])
        self.assertIn("relative path to a .py or .sh script", run("scripts/hook.txt")[0])
        self.assertIn("relative path to a .py or .sh script", run(7)[0])
        self.assertIn("relative path to a .py or .sh script", run(None)[0])


@contextlib.contextmanager
def windows_os():
    """This process's os module without the POSIX-only names a Windows build lacks."""
    with mock.patch.dict(lh.os.__dict__):
        for missing in ("O_NOFOLLOW", "O_NONBLOCK", "set_blocking"):
            lh.os.__dict__.pop(missing, None)
        yield


class WindowsTest(_Tmp):
    """The Windows path, where os has no O_NOFOLLOW, O_NONBLOCK or sessions; it runs for real on the
    Windows runtime job, and here with those names removed."""

    def test_the_log_opens_as_a_regular_file_without_the_posix_flags(self):
        self.logs.mkdir()
        with windows_os():
            with lh.open_log(self.logs / "launch-hook-a.log") as log:
                log.write(b"one\n")
            with lh.open_log(self.logs / "launch-hook-a.log") as log:
                log.write(b"two\n")
        self.assertEqual((self.logs / "launch-hook-a.log").read_bytes(), b"one\ntwo\n")

    @unittest.skipIf(os.name == "nt", "a FIFO and a symlink need POSIX to be planted")
    def test_without_the_posix_flags_a_symlink_or_fifo_log_is_refused_and_nothing_blocks(self):
        self.logs.mkdir()
        os.mkfifo(self.logs / "launch-hook-pipe.log")
        outside = self.tmp / "outside.log"
        outside.write_text("untouched\n")
        (self.logs / "launch-hook-link.log").symlink_to(outside)
        hooks = [("pipe", self.hook("pipe.py", PY_HOOK)), ("link", self.hook("link.py", PY_HOOK)),
                 ("fine", self.hook("fine.py", PY_HOOK))]
        err, got, done = io.StringIO(), [], threading.Event()

        def call():
            with windows_os(), contextlib.redirect_stderr(err):
                got.append(lh.start(hooks, self.logs, sys.executable, self.env))
            done.set()
        threading.Thread(target=call, daemon=True).start()
        self.addCleanup(EdgeTest.release_fifo, self.logs / "launch-hook-pipe.log")
        self.assertTrue(done.wait(10), "start() blocked on the log")
        self.assertEqual([n for n, _ in got[0]], ["fine"], err.getvalue())
        self.assertEqual(outside.read_text(), "untouched\n")

    def test_on_windows_a_hook_is_started_detached_in_its_own_process_group(self):
        seen = {}

        class Proc:
            pid = 4242

        def popen(argv, **kw):
            seen.update(kw, argv=argv)
            return Proc()
        with mock.patch.object(lh, "WINDOWS", True), mock.patch.object(lh.subprocess, "Popen", popen):
            started = lh.start([("a", self.hook("a.py", PY_HOOK))], self.logs, sys.executable, self.env)
        self.assertEqual(started, [("a", 4242)])
        self.assertNotIn("start_new_session", seen)
        self.assertEqual(seen["creationflags"], lh.DETACHED_PROCESS | lh.CREATE_NEW_PROCESS_GROUP)
        self.assertIs(seen["stdin"], subprocess.DEVNULL)

    @unittest.skipIf(os.name != "nt", "a real detached start on Windows")
    def test_a_py_hook_really_starts_and_writes_its_log_on_windows(self):
        started = lh.start([("one", self.hook("one.py", PY_HOOK))], self.logs, sys.executable, self.env)
        self.assertEqual([n for n, _ in started], ["one"])
        self.assertEqual(wait_for(self.out / "one.py.json")["stdin"], "")


class WindowsLauncherTest(unittest.TestCase):
    """scripts/start-cli.ps1, the Windows core launcher src/startup.ps1 runs, starts the hooks before
    either launch path; tests/windows-launch-hooks.test.ps1 runs it on the Windows job."""

    def test_the_hooks_run_after_the_already_running_exit_and_before_both_launch_paths(self):
        text = (REPO / "scripts" / "start-cli.ps1").read_text(encoding="utf-8")
        calls = [m.start() for m in re.finditer(r"Join-Path \$REPO 'src/launch_hooks\.py'", text)]
        self.assertEqual(len(calls), 1, "one launch_hooks.py call")
        already = text.index("sutando-core already running")
        foreground = text.index("& $claudeCmd.Source @claudeArgs")
        window = text.index("Start-Process -FilePath")
        self.assertLess(already, calls[0])
        self.assertLess(calls[0], min(foreground, window))
        self.assertIn("Resolve-SutandoPython", text[already:calls[0] + 80])


STALL_HOOK = """import os, time
while os.path.exists(os.environ["HOOK_STALL"]):
    time.sleep(0.05)
open(os.path.join(os.environ["HOOK_OUT"], "demo.done"), "w").write("ran")
"""
FAKE_LAUNCHER = "#!/bin/bash\necho \"$*\" > \"$LAUNCH_STAMP\"\n"


@unittest.skipIf(os.name == "nt", "the POSIX dispatcher")
class DispatcherTest(_Tmp):
    """The real dispatcher with a stand-in runtime launcher and one demo skill's hook."""

    def engine(self, ws: Path) -> Path:
        repo = self.tmp / "repo"
        ignore = shutil.ignore_patterns("__pycache__", "node_modules")
        for tree in ("src", "shared", "scripts"):
            shutil.copytree(REPO / tree, repo / tree, ignore=ignore)
        demo = repo / "skills" / "demo" / "scripts"
        demo.mkdir(parents=True)
        (demo / "hook.py").write_text(STALL_HOOK)
        (demo.parent / "manifest.json").write_text(json.dumps({"name": "demo", lh.FIELD: "scripts/hook.py"}))
        (repo / "sutando.config.json").write_text(json.dumps({"workspace": {"path": str(ws)}}))
        launcher = repo / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
        launcher.write_text(FAKE_LAUNCHER)
        launcher.chmod(0o755)
        return repo

    def test_a_launch_starts_the_hook_detached_and_hands_off_while_it_stalls(self):
        ws, stall, stamp = self.tmp / "ws", self.tmp / "stall", self.tmp / "launched"
        (ws / "skills").mkdir(parents=True)
        repo = self.engine(ws)
        stall.touch()
        self.addCleanup(lambda: stall.unlink(missing_ok=True))
        env = {k: v for k, v in self.env.items() if k not in ("SUTANDO_PY", "SUTANDO_WORKSPACE")}
        env.update({"SUTANDO_CORE_RUNTIME": "claude", "HOOK_STALL": str(stall), "LAUNCH_STAMP": str(stamp),
                    "SUTANDO_TMUX_SOCKET": str(self.tmp / "tmux.sock")})
        try:
            run = subprocess.run(["bash", str(repo / "src" / "agent" / "start-cli.sh")], env=env,
                                 capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("the dispatcher waited on the launch hook instead of handing off")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(stamp.exists(), "the runtime launcher was never reached\n" + run.stderr)
        self.assertIn("launch-hooks: started demo", run.stderr)
        self.assertFalse((self.out / "demo.done").exists(), "the hook should still be stalled")
        stall.unlink()
        deadline = time.monotonic() + 30
        while not (self.out / "demo.done").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.out / "demo.done").exists(), "the hook never finished")

    def test_a_fifo_manifest_in_the_workspace_never_holds_the_launch(self):
        ws, stamp = self.tmp / "ws", self.tmp / "launched"
        (ws / "skills" / "aa-fifo").mkdir(parents=True)
        fifo = ws / "skills" / "aa-fifo" / "manifest.json"
        os.mkfifo(fifo)
        self.addCleanup(EdgeTest.release_fifo, fifo)
        repo = self.engine(ws)
        env = {k: v for k, v in self.env.items() if k not in ("SUTANDO_PY", "SUTANDO_WORKSPACE")}
        env.update({"SUTANDO_CORE_RUNTIME": "claude", "HOOK_STALL": str(self.tmp / "no-stall"),
                    "LAUNCH_STAMP": str(stamp), "SUTANDO_TMUX_SOCKET": str(self.tmp / "tmux.sock")})
        try:
            run = subprocess.run(["bash", str(repo / "src" / "agent" / "start-cli.sh")], env=env,
                                 capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("a FIFO manifest held the launch")
        self.assertTrue(stamp.exists(), run.stderr)
        self.assertIn("launch-hooks: started demo", run.stderr)

    def test_the_dispatcher_and_the_runner_name_no_skill(self):
        dispatcher = (REPO / "src" / "agent" / "start-cli.sh").read_text()
        self.assertNotIn("skills/", dispatcher.replace('"$REPO/scripts/', ""))
        self.assertNotIn("tutorial", dispatcher + (REPO / "src" / "launch_hooks.py").read_text())


if __name__ == "__main__":
    unittest.main(warnings="ignore")
