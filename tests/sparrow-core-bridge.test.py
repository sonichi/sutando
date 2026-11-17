#!/usr/bin/env python3
"""sparrowd's core gateway bridge (`src/sparrow_core_bridge.py`, `src/sparrowd.py --core`).

- Which bridge: the legacy one while its row is unparked and it has a token; else the named primary;
  else none, with the reason. No token, or a parked legacy row with no named primary, is no worker.
- Its environment is the wrapper's: the launchd wrapper's child and the launcher's exec get the same
  variables, legacy alone and with a named row holding the primary (proactive room emptied), and the
  loader run under each installs the same hooks and names the same status and lease files, with no
  worker router. A named primary runs under exactly its lane's `launch_env`.
- The revision changes on a token rotation and on a primary move, not otherwise; exit 75 is held.
- The launcher stands down (75) when the decision moved, evicts its own stray before the exec, alerts
  on a restart except a poll stall, and a named lane logs to its own file with a launch record.

Run: python3 tests/sparrow-core-bridge.test.py
"""
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
_spec = importlib.util.spec_from_file_location("connections_contract_core", ROOT / "tests" / "connections.test.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
C = H.C

import sparrow_core_bridge as core
from ag2_sparrow import send_allowlist

_sd_spec = importlib.util.spec_from_file_location("sparrowd_launcher_core", ROOT / "src" / "sparrowd.py")
SD = importlib.util.module_from_spec(_sd_spec)
_sd_spec.loader.exec_module(SD)

# Set by the shell each process runs under, not by either launcher.
SHELL_NOISE = {"_", "SHLVL", "PWD", "OLDPWD", "__CF_USER_TEXT_ENCODING"}
HOOKS = ("TASK_INTAKE_GATE", "PROACTIVE_ROUTE_GATE", "PROACTIVE_DISPATCH_GATE", "PROACTIVE_ACK_GATE", "LANE_SERVER",
         "PROACTIVE_ROOM_GATE", "OWNER_DM_UPGRADED_NOTIFY", "TASK_STATUS", "STOP_INTERRUPT", "OWNS_UNADDRESSED",
         "PROACTIVE_ROOM_KEEP", "STAND_DOWN_GATE", "PROACTIVE_CLAIM_GATE", "PROACTIVE_OWN_FILE", "TASK_ADDRESSEE",
         "TASK_PRE_PUBLISH", "_LANE_WORKER", "GATEWAY_INSTANCE", "PROACTIVE_ROOM", "VOICE_ROOM_VERIFIER",
         "_LOCK_ROLE", "GATEWAY_STATUS_FILE", "INFLIGHT_FILE")


def _shape(value):
    if value is None or isinstance(value, (str, bool, int, Path)):
        return value
    return getattr(value, "__qualname__", type(value).__qualname__)


def quiet(env):
    return {k: v for k, v in env.items() if k not in SHELL_NOISE}


class Checkout:
    """A scratch checkout the real wrapper runs from: stub config, connections and bridge, the real lib."""

    def __init__(self, root: Path):
        self.d = root
        for sub in ("src/launchd", "scripts", "ws/state", "ws/results", "shims", "home"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.env_file = root / "relay.env"
        (root / "scripts" / "sutando-config.sh").write_text(
            f'#!/bin/bash\ncase "${{1:-}}" in workspace) echo "{root}/ws" ;; *) echo "{self.env_file}" ;; esac\n')
        (root / "scripts" / "connections.py").write_text(
            "import os, sys\n"
            f"p = {str(root / 'primary')!r}\n"
            "if sys.argv[1:] == ['primary'] and os.path.exists(p):\n"
            "    print(open(p).read().strip()); sys.exit(0)\n"
            "sys.exit(2 if sys.argv[1:] == ['primary'] else 1 if sys.argv[1:] == ['legacy-parked'] else 0)\n")
        (root / "src" / "remote-gateway-bridge.py").write_text(
            "import json, os, time\n"
            f"out = {str(root / 'child-env.json')!r}\n"
            "open(out + '.tmp', 'w').write(json.dumps(dict(os.environ)))\n"
            "os.replace(out + '.tmp', out)\n"
            "time.sleep(30)\n")
        (root / "src" / "sparrowd.py").write_text("import time\nwhile True: time.sleep(0.2)\n")
        for rel in ("src/launchd/gateway-bridge-wrapper.sh", "src/launchd/gateway-bridge-lib.sh", "src/channel_token.py"):
            shutil.copy(ROOT / rel, root / rel)
        (root / "shims" / "python3").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        (root / "shims" / "python3").chmod(0o755)

    def base_env(self, **extra):
        return hermetic_env(home=self.d / "home", PATH=f"{self.d}/shims:/usr/bin:/bin", **extra)

    def wrapper_child_env(self, env) -> dict:
        out = self.d / "child-env.json"
        out.unlink(missing_ok=True)
        proc = subprocess.Popen(["bash", "src/launchd/gateway-bridge-wrapper.sh"], cwd=str(self.d), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            proc.terminate()
            proc.wait(timeout=30)
        return json.loads(out.read_text())


class TheWrappersEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.co = Checkout(Path(self.tmp.name).resolve())
        self.co.env_file.write_text("AG2_REMOTE_TOKEN=legacy-fixture-token\nREMOTE_PROACTIVE_ROOM='!room:alpha-fixture.net'\n")
        self.env = self.co.base_env(AG2_DEVICE_ENV=str(self.co.d / "device.env"), REMOTE_TASK_TOKEN="",
                                    AG2_REMOTE_TOKEN="")

    def launched(self, primary: str):
        seen = {}

        def execve(path, argv, env):
            seen.update(path=path, argv=argv, env=env)
        with mock.patch.object(C, "legacy_parked", return_value=False), \
                mock.patch.object(C, "primary_instance", return_value=primary), contextlib.redirect_stderr(io.StringIO()):
            rc = core.main(["--expect="], workspace=self.co.d / "ws", environ={**self.env, core.LAST_RC_ENV: "1"},
                           execve=execve, evict=lambda inst, ws: 0, alert=lambda rc, ws: seen.update(alert=rc),
                           legacy=lambda e: core.legacy_env(e, repo=self.co.d))
        self.assertEqual(rc, 0)
        self.assertEqual((seen["path"], seen["argv"]), (sys.executable, [sys.executable, str(core.LOADER)]))
        self.assertEqual(seen["alert"], "1")
        self.assertNotIn(core.LAST_RC_ENV, seen["env"], "the restart code never reaches the bridge")
        return seen["env"]

    def assert_same(self, primary: str):
        if primary:
            (self.co.d / "primary").write_text(primary)
        child = self.co.wrapper_child_env(self.env)
        mine = self.launched(primary)
        self.assertEqual(quiet(mine), quiet(child))
        self.assertEqual(mine["REMOTE_TASK_TOKEN"], "legacy-fixture-token")
        self.assertEqual((mine["REMOTE_TASK_TIER"], mine["REMOTE_MEDIA_MARKER"]), ("owner", "ag2space-media"))
        self.assertEqual(mine["REMOTE_TASK_TOKEN_FILE"], str(self.co.env_file))
        self.assertEqual(mine["AG2_DEVICE_ENV"], str(self.co.d / "device.env"), "the device env is kept")
        self.assertNotIn("REPO", mine)
        return mine, child

    def test_legacy_primary_matches_the_wrappers_child(self):
        mine, _ = self.assert_same("")
        self.assertEqual(mine["REMOTE_PROACTIVE_ROOM"], "!room:alpha-fixture.net", "the proactive room is kept")

    def test_a_named_primary_empties_the_legacy_bridges_proactive_room_as_the_wrapper_does(self):
        mine, _ = self.assert_same("alpha-fixture-net")
        self.assertEqual(mine["REMOTE_PROACTIVE_ROOM"], "")

    def test_the_loader_installs_the_same_hooks_under_either_launch(self):
        child = self.co.wrapper_child_env(self.env)
        mine = self.launched("")
        ws = self.co.d / "ws"
        loads = [self.load(child, ws), self.load(mine, ws)]
        self.assertEqual(*[{k: _shape(ns[k]) for k in HOOKS} for ns in loads])
        for ns in loads:
            self.assertEqual((ns["GATEWAY_INSTANCE"], ns["_LANE_WORKER"], ns["TASK_PRE_PUBLISH"]), ("", "", None),
                             "the core installs no worker router")
            self.assertIsNotNone(ns["PROACTIVE_ROOM_KEEP"])
            self.assertIsNotNone(ns["STAND_DOWN_GATE"])
            self.assertIsNotNone(ns["TASK_INTAKE_GATE"])
            self.assertEqual((ns["_LOCK_ROLE"], ns["GATEWAY_STATUS_FILE"].name), ("gateway-bridge", "gateway-status.json"))
            self.assertEqual(ns["PROACTIVE_ROOM"], "!room:alpha-fixture.net")

    def load(self, env, ws):
        import runpy
        import workspace_default
        self.addCleanup(setattr, send_allowlist, "_EXTRA_ROOTS", list(send_allowlist._EXTRA_ROOTS))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=ws), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runpy.run_path(str(ROOT / "src" / "remote-gateway-bridge.py"), run_name="core_parity_probe")

    def test_no_token_is_no_bridge(self):
        self.co.env_file.write_text("")
        with mock.patch.object(C, "legacy_parked", return_value=False), \
                mock.patch.object(C, "primary_instance", return_value=None):
            decision, why = core.decide(self.co.d / "ws", self.env, legacy=lambda e: core.legacy_env(e, repo=self.co.d))
        self.assertEqual((decision, why), (None, "no REMOTE_TASK_TOKEN configured and no named connection is primary"))


class Named(H.Base):
    def setUp(self):
        super().setUp()
        self.row, _ = self.add(H.TOKEN_A)
        self.inst = self.row["instance"]
        C.set_primary(self.inst, workspace=self.ws)
        self.environ = {"REMOTE_PROACTIVE_ROOM": "!room:alpha-fixture.net", "AG2_DEVICE_ENV": "/fixture/device.env"}

    def decide(self, parked=True, legacy=None):
        with mock.patch.object(C, "legacy_parked", return_value=parked):
            return core.decide(self.ws, self.environ, legacy=legacy or (lambda e: {}))

    def test_a_named_primary_runs_under_exactly_its_lanes_launch_env(self):
        decision, why = self.decide()
        self.assertEqual((decision["instance"], why), (self.inst, ""))
        lane = C.launch_env(C.plan_entry(self.inst, C.keeper_environ(self.environ), workspace=self.ws),
                            C.keeper_environ(self.environ), vault=self.vault)
        self.assertEqual(core.bridge_env(decision, vault=self.vault), lane)
        self.assertEqual(lane["REMOTE_PROACTIVE_ROOM"], "!room:alpha-fixture.net", "the primary keeps the room")

    def test_an_unparked_legacy_bridge_without_a_token_yields_to_the_named_primary(self):
        decision, _ = self.decide(parked=False, legacy=lambda e: {"REMOTE_TASK_TOKEN": ""})
        self.assertEqual(decision["instance"], self.inst)

    def test_a_parked_legacy_row_with_no_named_primary_is_no_bridge(self):
        with mock.patch.object(C, "primary_instance", return_value=None):
            self.assertEqual(self.decide(), (None, "the legacy connection is parked and no named connection is primary"))
            with mock.patch.object(SD, "resolve_workspace", return_value=self.ws), \
                    mock.patch.object(C, "legacy_parked", return_value=True):
                self.assertEqual(SD.core_spec(), (None, "the legacy connection is parked and no named connection is primary"))

    def test_an_unlaunchable_primary_is_unavailable_not_absent(self):
        with mock.patch.object(C, "plan_entry", side_effect=C.UnknownConnection("gone")):
            with self.assertRaises(core.CoreUnavailable):
                self.decide()

    def test_the_spec_is_held_on_75_and_its_revision_follows_the_token_file_and_the_primary(self):
        token = self.ws / "relay.env"
        token.write_text("REMOTE_TASK_TOKEN=one\n")
        legacy = lambda e: {"REMOTE_TASK_TOKEN": "t", "REMOTE_TASK_TOKEN_FILE": str(token)}  # noqa: E731

        def spec():
            with mock.patch.object(C, "legacy_parked", return_value=False), \
                    mock.patch.object(core, "legacy_env", legacy):
                return SD.core_spec(self.ws, self.environ)[0]
        first = spec()
        self.assertEqual((first.name, first.hold_rc, first.argv[1:]),
                         (SD.CORE_WORKER, frozenset({75}), [str(SD.CORE_LAUNCHER), "--expect="]))
        self.assertEqual(spec().revision, first.revision, "nothing changed")
        token.write_text("REMOTE_TASK_TOKEN=two\n")
        rotated = spec().revision
        self.assertNotEqual(rotated, first.revision, "a rotated token restarts the core")
        with mock.patch.object(C, "primary_instance", return_value=None):
            self.assertNotEqual(spec().revision, rotated, "a primary move restarts it")
        token.unlink()
        self.assertNotEqual(spec().revision, rotated, "an unreadable token file is a change too")

    def test_a_named_revision_follows_its_rows_token_file(self):
        decision, _ = self.decide()
        before = core.revision(decision, self.ws)
        with mock.patch.object(C, "lane_token_file", return_value=str(self.ws / "missing.env")):
            self.assertNotEqual(core.revision(decision, self.ws), before)

    def run_main(self, argv=("--expect=x",), **kw):
        seen = {}
        kw.setdefault("evict", lambda inst, ws: 0)
        kw.setdefault("alert", lambda rc, ws: None)
        err = io.StringIO()
        with mock.patch.object(core, "_lane_log", lambda inst, ws: seen.update(log=inst)), contextlib.redirect_stderr(err):
            rc = core.main(list(argv), workspace=self.ws, environ=self.environ, vault=self.vault,
                           execve=lambda p, a, e: seen.update(env=e), **kw)
        return rc, seen, err.getvalue()

    def test_main_runs_the_named_primary_with_a_launch_record_and_its_own_log(self):
        with mock.patch.object(C, "legacy_parked", return_value=True):
            rc, seen, _ = self.run_main([f"--expect={self.inst}"])
        self.assertEqual((rc, seen["log"], seen["env"]["GATEWAY_INSTANCE"]), (0, self.inst, self.inst))
        rec = json.loads(C.launch_record_path(self.inst, self.ws).read_text())
        self.assertEqual((rec["ok"], rec["lease"], rec["pid"]), (True, "supervised", os.getpid()))

    def test_main_stands_down_when_the_decision_moved_or_there_is_nothing(self):
        with mock.patch.object(C, "legacy_parked", return_value=True):
            rc, seen, err = self.run_main(["--expect="])
        self.assertEqual((rc, seen), (75, {}))
        self.assertIn("standing down for sparrowd's next scan", err)
        with mock.patch.object(C, "legacy_parked", return_value=True), \
                mock.patch.object(C, "primary_instance", return_value=None):
            rc, _, err = self.run_main()
        self.assertEqual(rc, 75)
        self.assertIn("nothing to run", err)

    def test_main_backs_off_when_the_core_cannot_be_read_or_built(self):
        with mock.patch.object(C, "legacy_parked", side_effect=C.SchemaError("bad record")):
            self.assertEqual(self.run_main()[0], 1)
        with mock.patch.object(C, "legacy_parked", return_value=True), \
                mock.patch.object(C, "launch_env", side_effect=C.NoToken("no token")):
            rc, seen, err = self.run_main([f"--expect={self.inst}"])
        self.assertEqual((rc, seen), (1, {}))
        self.assertIn("no token", err)

    def test_main_stands_down_to_an_alias_and_never_launches_past_a_failed_eviction(self):
        with mock.patch.object(C, "legacy_parked", return_value=True), \
                mock.patch.object(C, "alias_instances", return_value=["alias-x"]), \
                mock.patch.object(C, "bridge_pid", return_value=4242):
            rc, seen, err = self.run_main([f"--expect={self.inst}"])
        self.assertEqual((rc, seen), (75, {}))
        self.assertIn("already runs under an alias (pid 4242)", err)
        with mock.patch.object(C, "legacy_parked", return_value=True):
            rc, seen, err = self.run_main([f"--expect={self.inst}"], evict=lambda inst, ws: 4)
        self.assertEqual((rc, seen), (4, {}))

    def test_alias_holder_is_zero_when_no_alias_serves(self):
        with mock.patch.object(C, "alias_instances", return_value=["alias-x"]), \
                mock.patch.object(C, "bridge_pid", return_value=0):
            self.assertEqual(core.alias_holder(self.inst, self.ws), 0)


class Edges(unittest.TestCase):
    def test_legacy_env_failures_are_unavailable(self):
        def broken(*a, **k):
            raise OSError("no bash")
        for run in (broken, lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
                    lambda *a, **k: subprocess.CompletedProcess(a, 0, "not json\n", "")):
            with self.assertRaises(core.CoreUnavailable):
                core.legacy_env({}, run=run)

    def test_evict_runs_the_shared_helper_with_the_proven_holder(self):
        calls = []

        def run(argv, env):
            calls.append((argv, env["EVICT_PROVEN_PID"], env["EVICT_WAIT_S"]))
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(C, "bridge_pid", return_value=31):
            self.assertEqual(core.evict_stray("lane-a", Path("/ws"), repo=Path("/repo"), run=run, environ={}), 0)
        with mock.patch.object(C, "bridge_pid", side_effect=C.BridgeLeaseIndeterminate("", "unreadable")):
            core.evict_stray("", Path("/ws"), repo=Path("/repo"), run=run, environ={})
        self.assertEqual(calls[0][0], ["bash", "/repo/src/launchd/evict-own-bridge.sh", "remote-gateway", "/repo",
                                       "GATEWAY_INSTANCE", "lane-a"])
        self.assertEqual([c[1:] for c in calls], [("31", "15"), ("", "15")])

    def test_the_real_eviction_finds_no_stray_of_a_scratch_checkout(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(C, "bridge_pid", return_value=0):
            (Path(d) / "src" / "launchd").mkdir(parents=True)
            shutil.copy(core.EVICT, Path(d) / "src" / "launchd" / core.EVICT.name)
            self.assertEqual(core.evict_stray("no-such-lane-fixture", Path(d), repo=Path(d), environ=hermetic_env()), 0)

    def test_restart_alert_skips_a_first_start_and_a_poll_stall(self):
        calls = []
        run = lambda argv, env: calls.append((argv, env["WORKSPACE"], env["GW_LOG_TAG"]))  # noqa: E731
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            core.restart_alert(None, Path("/ws"), run=run)
            core.restart_alert("69", Path("/ws"), run=run)
            core.restart_alert("-15", Path("/ws"), repo=Path("/repo"), run=run)
        self.assertIn("poll stall (rc 69); relaunching without an owner alert", err.getvalue())
        self.assertEqual(calls, [(["bash", "/repo/src/launchd/gateway-bridge-lib.sh", "alert"], "/ws", core.LOG_TAG)])

    def test_a_named_lane_logs_to_its_own_file(self):
        with tempfile.TemporaryDirectory() as d:
            saved = os.dup(1), os.dup(2)
            try:
                core._lane_log("lane-a", Path(d))
                os.write(1, b"out\n")
                os.write(2, b"err\n")
            finally:
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
            self.assertEqual((Path(d) / "logs" / "remote-gateway-bridge.lane-a.log").read_text(), "out\nerr\n")


class TheCoreScan(unittest.TestCase):
    def test_it_keeps_the_last_core_while_unreadable_and_says_each_reason_once(self):
        spec = SD.WorkerSpec(name=SD.CORE_WORKER, argv=["py"])
        answers = [(spec, ""), RuntimeError("record unreadable"), RuntimeError("record unreadable"), (None, "parked")]

        def core_spec(ws):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        scan = SD.CoreScan()
        err = io.StringIO()
        with mock.patch.object(SD, "core_spec", core_spec), contextlib.redirect_stderr(err):
            got = [scan() for _ in range(4)]
        self.assertEqual(got, [[spec], [spec], [spec], []])
        self.assertEqual(err.getvalue().count("not rescanned (RuntimeError: record unreadable)"), 1)
        self.assertIn("core gateway bridge — not supervised: parked", err.getvalue())

    def test_main_adds_the_core_only_with_the_flag(self):
        spec = SD.WorkerSpec(name=SD.CORE_WORKER, argv=["py"])
        for argv, want in ((["--agent-bridges"], []), (["--agent-bridges", "--core"], [SD.CORE_WORKER])):
            with self.subTest(argv=argv), mock.patch.object(SD, "core_spec", return_value=(spec, "")), \
                    mock.patch.object(SD, "AgentBridgeScan", return_value=lambda: []), \
                    mock.patch.object(SD, "resolve_workspace", return_value=Path("/ws")), \
                    mock.patch.object(SD, "run", return_value=0) as run:
                self.assertEqual(SD.main(argv), 0)
                self.assertEqual([s.name for s in run.call_args[0][0]], want)
                self.assertEqual([s.name for s in run.call_args[1]["rescan"]()], want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
