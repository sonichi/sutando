#!/usr/bin/env python3
"""The primary gateway wrapper's lane keeper (`connections.keep_lanes_up`).

Pins: a launch-plan lane no bridge holds is started once, with one log line; a lane that is up,
disconnected or parked is left alone; a lane that does not start backs off (30 s, then doubling,
capped at 15 min however many failures) and is not retried every pass, nor stops the pass for others; a token update's stop and relaunch share one hold of the record
lock, so a keeper pass run in between neither starts the lane on the old code nor starts it twice;
the keeper's environment never carries an inherited lane token; a pass that hangs is given up at
its bound with one line, and the pass after it tends the lanes; the wrapper runs one pass at a time.

Run: python3 tests/connections-lane-keeper.test.py
"""
import importlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env
from _helpers.subprocess_coverage import covered_code

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
C = importlib.import_module("connections")
dotspace = importlib.import_module("dotspace")
bridge_lease = importlib.import_module("_helpers.bridge_lease")

REGISTRY = {"instances": [{"server": "alpha-fixture.net", "gateway_host": "chat.alpha-fixture.net"},
                          {"server": "beta-fixture.net", "gateway_host": "relay.beta-fixture.net"}]}
C.DEFAULT_RESOLVER = lambda host: dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)
C.DEFAULT_GETADDRINFO = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))]

TOKEN_A = "https://chat.alpha-fixture.net|secret-alpha"
TOKEN_B = "https://relay.beta-fixture.net:8443|secret-beta"
NEW_B = "https://relay.beta-fixture.net:8443/relay2|secret-beta-rotated"
BETA = "beta-fixture-net"
REPO = Path(__file__).resolve().parent.parent


class FakeVault:
    def __init__(self):
        self.store = {}

    def get(self, key):
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def agents(*ids):
    def fetch(url, token, address=None):
        return {"agents": [{"id": i, "owner": "@owner:" + i.split(":", 1)[1]} for i in ids]}
    return fetch


class KeeperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.vault = FakeVault()
        self.children = []
        self.spawned = []
        self.lock = threading.Lock()
        self.add(TOKEN_A, "@sutando:alpha-fixture.net")  # primary
        self.add(TOKEN_B, "@s:beta-fixture.net")

    def tearDown(self):
        for proc in self.children:
            bridge_lease.stop(proc)
        self.tmp.cleanup()

    def add(self, token, agent):
        preview = C.preview_connection(token, fetch=agents(agent))
        C.add_connection(token, expected_server=preview["server"], preview=preview, added_by="owner",
                         workspace=self.ws, vault=self.vault)

    def spawn(self, argv, env, log_path):
        with self.lock:
            self.spawned.append((env["GATEWAY_INSTANCE"], env["REMOTE_TASK_TOKEN"]))
        proc = bridge_lease.start(self.ws, env["GATEWAY_INSTANCE"], wait=False)
        self.children.append(proc)
        return proc

    def keep(self, **kw):
        kw.setdefault("spawn", self.spawn)
        return C.keep_lanes_up(repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                               vault=self.vault, log=lambda line: None, **kw)

    def launch(self, inst):
        return C.launch(inst, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                        vault=self.vault, spawn=self.spawn)


class KeepsLanesUp(KeeperBase):
    def test_a_down_lane_is_started_once_and_said_once(self):
        lines = self.keep()
        self.assertEqual(sorted(i for i, _ in self.spawned), ["alpha-fixture-net", BETA])
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("was down; started it" in line for line in lines), lines)
        self.assertEqual(self.keep(), [], "a lane that is up is left alone, silently")
        self.assertEqual(len(self.spawned), 2)

    def test_a_disconnected_lane_is_not_started(self):
        C.disconnect(BETA, workspace=self.ws, kill=lambda *a: None)
        self.keep()
        self.assertEqual([i for i, _ in self.spawned], ["alpha-fixture-net"])

    def test_a_lane_that_does_not_start_backs_off(self):
        self.launch("alpha-fixture-net")
        self.spawned.clear()

        def boom(argv, env, log_path):
            self.spawned.append((env["GATEWAY_INSTANCE"], env["REMOTE_TASK_TOKEN"]))
            raise OSError("no python")
        first = self.keep(spawn=boom, now=1000.0)
        self.assertEqual(len(first), 1, first)
        self.assertIn("did not start (launch_failed); next try in 30s", first[0])
        self.assertEqual(self.keep(spawn=boom, now=1010.0), [], "inside the wait: no try, no line")
        self.assertEqual(len(self.spawned), 1)
        second = self.keep(spawn=boom, now=1031.0)
        self.assertEqual(len(second), 1)
        self.assertIn("next try in 60s", second[0], "the wait doubles")
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.keep(spawn=boom, now=1080.0), [])
        ok = self.keep(now=1092.0)
        self.assertEqual(len(ok), 1)
        self.assertIn("started it", ok[0])
        self.assertEqual(C._read_json(C.lane_keeper_state_path(self.ws)), {}, "a started lane clears its wait")


class RetryState(KeeperBase):
    def boom(self, argv, env, log_path):
        self.spawned.append((env["GATEWAY_INSTANCE"], env["REMOTE_TASK_TOKEN"]))
        raise OSError("no python")

    def write_state(self, state):
        path = C.lane_keeper_state_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def test_a_malformed_wait_is_no_wait_and_counts_as_a_first_failure(self):
        self.write_state({"alpha-fixture-net": "soon", BETA: {"next_try": "later", "failures": "many"}})
        lines = self.keep(spawn=self.boom, now=1000.0)
        self.assertEqual(sorted(i for i, _ in self.spawned), ["alpha-fixture-net", BETA], "both were tried")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("next try in 30s" in line for line in lines), lines)
        self.assertEqual(C._read_json(C.lane_keeper_state_path(self.ws)),
                         {i: {"failures": 1, "next_try": 1030.0} for i in ("alpha-fixture-net", BETA)})

    def test_a_wait_kept_for_a_lane_no_longer_in_the_plan_is_dropped(self):
        self.write_state({"gone-fixture-net": {"failures": 4, "next_try": 9e9}})
        self.keep()
        self.assertEqual(C._read_json(C.lane_keeper_state_path(self.ws)), {})

    def test_a_lane_disconnected_after_the_plan_was_read_is_not_started(self):
        stale = C.launch_plan({}, workspace=self.ws)
        C.disconnect(BETA, workspace=self.ws, kill=lambda *a: None)
        self.write_state({BETA: {"failures": 2, "next_try": 0}})
        real, calls = C.launch_plan, []

        def launch_plan(*a, **kw):
            calls.append(1)
            return stale if len(calls) == 1 else real(*a, **kw)
        with unittest.mock.patch.object(C, "launch_plan", launch_plan):
            lines = self.keep(spawn=self.boom)
        self.assertEqual([i for i, _ in self.spawned], ["alpha-fixture-net"])
        self.assertFalse([line for line in lines if BETA in line], lines)
        self.assertNotIn(BETA, C._read_json(C.lane_keeper_state_path(self.ws)))


class SustainedFailure(KeeperBase):
    def spawn_failing_alpha(self, argv, env, log_path):
        if env["GATEWAY_INSTANCE"] == "alpha-fixture-net":
            raise OSError("no python")
        return self.spawn(argv, env, log_path)

    def test_thousands_of_failures_stay_capped_and_the_next_lane_still_starts(self):
        now, waits = 1000.0, set()
        for n in range(3000):
            lines = self.keep(spawn=self.spawn_failing_alpha, now=now)
            alpha = [line for line in lines if "alpha-fixture-net" in line]
            self.assertEqual(len(alpha), 1, (n, lines))
            waits.add(alpha[0].rsplit("next try in ", 1)[1])
            if n == 0:
                self.assertIn(f"lane {BETA} was down; started it", " ".join(lines), "same pass as the failure")
            now += C.LANE_RETRY_MAX_S + 1
        self.assertEqual(C._read_json(C.lane_keeper_state_path(self.ws))["alpha-fixture-net"]["failures"], 3000)
        self.assertTrue(waits <= {"30s", "60s", "120s", "240s", "480s", "900s"}, waits)
        self.assertEqual(C.lane_retry_wait(1025), C.LANE_RETRY_MAX_S)
        self.assertEqual(C.lane_retry_wait(10 ** 9), C.LANE_RETRY_MAX_S)

    def test_a_lane_whose_check_raises_does_not_stop_the_pass(self):
        real = C._row_named

        def row_named(inst, workspace):
            if inst == "alpha-fixture-net":
                raise RuntimeError("bad row")
            return real(inst, workspace)
        with unittest.mock.patch.object(C, "_row_named", row_named):
            lines = self.keep(now=1000.0)
        self.assertEqual([i for i, _ in self.spawned], [BETA])
        self.assertTrue(any("alpha-fixture-net: keeper check failed (RuntimeError); next try in 30s" in line
                            for line in lines), lines)


class TokenUpdateRace(KeeperBase):
    def test_a_keeper_pass_between_stop_and_relaunch_starts_nothing(self):
        self.launch("alpha-fixture-net")
        self.launch(BETA)
        self.spawned.clear()
        keeper = {}

        def run_keeper():
            keeper["lines"] = self.keep()

        def kill(pid, sig):
            os.kill(pid, sig)
            t = threading.Thread(target=run_keeper, name="keeper")
            keeper["thread"] = t
            t.start()
            time.sleep(0.5)  # the pass is now waiting on the record lock

        real_launch = C.launch

        def slow_launch(*a, **kw):
            if threading.current_thread() is threading.main_thread():
                time.sleep(0.5)  # widen the window between the update's stop and its relaunch
            return real_launch(*a, **kw)
        with unittest.mock.patch.object(C, "launch", slow_launch):
            C.update_token(BETA, NEW_B, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                           workspace=self.ws, vault=self.vault, spawn=self.spawn, kill=kill,
                           fetch=agents("@s:beta-fixture.net"))
        keeper["thread"].join(30)
        self.assertEqual(self.spawned, [(BETA, NEW_B)], "the lane came back once, on the new code")
        self.assertEqual(keeper["lines"], [], "the keeper found it up and started nothing")
        self.assertTrue(C._proven_holder(BETA, self.ws))


class ReconnectRace(KeeperBase):
    def test_a_keeper_pass_between_stop_and_relaunch_starts_nothing(self):
        self.launch("alpha-fixture-net")
        self.launch(BETA)
        self.spawned.clear()
        keeper = {}

        def run_keeper():
            keeper["lines"] = self.keep()

        def kill(pid, sig):
            os.kill(pid, sig)
            if "thread" not in keeper:
                keeper["thread"] = threading.Thread(target=run_keeper, name="keeper")
                keeper["thread"].start()
                time.sleep(0.5)  # the pass is now waiting on the record lock

        real_launch = C.launch

        def slow_launch(*a, **kw):
            if threading.current_thread() is threading.main_thread():
                time.sleep(0.5)  # widen the window between the reconnect's stop and its relaunch
            return real_launch(*a, **kw)
        with unittest.mock.patch.object(C, "launch", slow_launch), \
                unittest.mock.patch.object(C, "_launch_preflight", lambda *a, **kw: None):
            C.reconnect(BETA, repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                        vault=self.vault, spawn=self.spawn, kill=kill, fetch=agents("@s:beta-fixture.net"))
        keeper["thread"].join(30)
        self.assertEqual(self.spawned, [(BETA, TOKEN_B)], "the lane came back once")
        self.assertEqual(keeper["lines"], [], "the keeper found it up and started nothing")


# The keep-lanes verb as the wrapper runs it, with a vault that holds nothing.
KEEP_LANES_CLI = r"""
import runpy, sys
sys.path.insert(0, sys.argv[1])
import connections
class NoVault:
    def get(self, key):
        raise KeyError(key)
connections._vault = lambda vault: NoVault()
script = sys.argv[2]
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
"""


class HungPass(KeeperBase):
    def test_a_pass_waiting_on_a_hung_lock_holder_is_given_up_and_the_next_tends_the_lanes(self):
        argv = [sys.executable, "-c", covered_code(KEEP_LANES_CLI), str(REPO / "src"),
                str(REPO / "scripts" / "connections.py"), "--workspace", str(self.ws), "keep-lanes",
                "--repo", str(self.ws / "repo"), "--logs-dir", str(self.ws / "logs"), "--max-s", "1"]
        with C._records_lock(self.ws):  # a holder that never lets go while the pass runs
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=isolated_child_env())
            try:
                out, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("the pass outlived its bound: its supervisor would never start another")
        self.assertEqual(proc.returncode, C.LANE_PASS_TIMEOUT_RC, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, (out, err))
        self.assertIn("lane keeper pass still running after 1s", lines[0])
        self.assertEqual(self.spawned, [])
        started = self.keep()
        self.assertEqual(sorted(i for i, _ in self.spawned), ["alpha-fixture-net", BETA])
        self.assertEqual(len(started), 2, started)

    def test_a_pass_stuck_in_a_lane_start_is_given_up_with_one_line(self):
        stuck, gave, said = threading.Event(), [], []

        def never_returns(argv, env, log_path):
            stuck.wait(20)
            raise OSError("gave up")

        def give_up(rc):
            gave.append(rc)
            stuck.set()  # the process would end here; the thread is let go in its place
        C.keep_lanes_bounded(max_s=0.3, give_up=give_up, log=said.append, spawn=never_returns,
                             repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={}, workspace=self.ws,
                             vault=self.vault, now=1000.0)
        self.assertEqual(gave, [C.LANE_PASS_TIMEOUT_RC])
        self.assertEqual(len([line for line in said if "gave it up" in line]), 1, said)

    def test_a_pass_that_finishes_in_time_is_left_alone(self):
        gave = []
        lines = C.keep_lanes_bounded(max_s=30, give_up=gave.append, log=lambda line: None, spawn=self.spawn,
                                     repo=self.ws / "repo", logs_dir=self.ws / "logs", environ={},
                                     workspace=self.ws, vault=self.vault)
        self.assertEqual(len(lines), 2, lines)
        time.sleep(0.1)
        self.assertEqual(gave, [])
        self.assertFalse([t for t in threading.enumerate() if isinstance(t, threading.Timer)], "timer cancelled")

    def test_the_verb_runs_a_bounded_pass_and_exits_zero(self):
        script = str(REPO / "scripts" / "connections.py")
        argv = [script, "--workspace", str(self.ws), "keep-lanes", "--repo", str(self.ws / "repo"),
                "--logs-dir", str(self.ws / "logs")]
        seen = {}
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(C, "keep_lanes_bounded", lambda **kw: seen.update(kw)), \
                self.assertRaises(SystemExit) as done:
            runpy.run_path(script, run_name="__main__")
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(seen, {"max_s": C.LANE_PASS_MAX_S, "repo": self.ws / "repo",
                                "logs_dir": self.ws / "logs", "workspace": self.ws})


# tend_lanes as the wrapper defines it, with the keeper and the clock replaced.
TEND_LANES = r"""
set -euo pipefail
REPO="$1"; WORKSPACE="$2"; CALLS="$3"; NOW=1000
date() { echo "$NOW"; }
python3() { echo "$*" >> "$CALLS"; sleep 1; return 3; }
calls() { wc -l < "$CALLS" | tr -d ' '; }
%s
tend_lanes; first="$LANES_PID"
NOW=1005; tend_lanes
[ "$LANES_PID" = "$first" ] && [ "$(calls)" = 1 ] || { echo "a second pass started beside a live one"; exit 1; }
sleep 1.5
NOW=1029; tend_lanes
[ -z "$LANES_PID" ] && [ "$(calls)" = 1 ] || { echo "a pass started before it was due"; exit 1; }
NOW=1030; tend_lanes
[ -n "$LANES_PID" ] && [ "$(calls)" = 2 ] || { echo "no pass after a failed one"; exit 1; }
wait "$LANES_PID" || true
echo tended
"""


class WrapperTendsLanes(unittest.TestCase):
    def test_one_pass_at_a_time_and_a_failed_pass_does_not_end_the_tending(self):
        wrapper = (REPO / "src" / "launchd" / "gateway-bridge-wrapper.sh").read_text()
        block = re.search(r"^LANES_EVERY_S=.*?^tend_lanes\(\) \{.*?^\}$", wrapper, re.M | re.S)
        self.assertIsNotNone(block, "the wrapper defines tend_lanes")
        self.assertGreaterEqual(len(re.findall(r"^\s+tend_lanes$", wrapper, re.M)), 2, "parked and running loops")
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            calls.write_text("")
            run = subprocess.run(["bash", "-c", TEND_LANES % block.group(0), "tend", "/repo", tmp, str(calls)],
                                 capture_output=True, text=True, timeout=30, env=isolated_child_env())
            self.assertEqual((run.returncode, run.stdout.strip()), (0, "tended"), run.stderr)
            self.assertEqual(calls.read_text().splitlines(),
                             [f"/repo/scripts/connections.py keep-lanes --repo /repo --logs-dir {tmp}/logs"] * 2)


class KeeperEnviron(unittest.TestCase):
    def test_an_inherited_lane_token_is_dropped(self):
        env = C.keeper_environ({"AG2_REMOTE_TOKEN_BETA": "stale", "OTHER": "1"})
        self.assertEqual(env, {"OTHER": "1"})


if __name__ == "__main__":
    unittest.main()
