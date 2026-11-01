"""The rig fault launcher (scripts/witness/lifecycle-fault-launcher.py), driven against the real bridge
module: it fires once on the claimed effect it names, matched by the claim the ledger holds for the
call's operation id, passes every other call through unchanged, reports a second match or none, and
starts from `main()` through its first report line.

Run:
    python3 tests/witness-lifecycle-fault-launcher.test.py
"""
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gw, _READS = isolate_then_import(env={"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"})
assert gw._ATTEMPTS.take_ownership()

PATH = Path(__file__).resolve().parents[1] / "scripts" / "witness" / "lifecycle-fault-launcher.py"
spec = importlib.util.spec_from_file_location("lifecycle_fault_launcher", PATH)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def _task(tid, **extra):
    return {"id": tid, "task": "reply", "source": "ag2space", "user_id": "@o:x", "access_tier": "owner", **extra}


def _ack(tid, gen):
    return {"ok": True, "durable": True, "task_id": tid, "execution_generation": gen, "attempt": gen,
            "lease_expires_at": 1.0}


class Broker:
    """The real broker's stand-in: answers acks and results, and records each call's operation id."""

    def __init__(self, tid):
        self.tid, self.calls = tid, []

    def __call__(self, method, path, payload=None, timeout=None, **k):
        self.calls.append((method, path, k.get("operation_id")))
        return _ack(self.tid, 1) if path.endswith("/ack") else {"ok": True}


class Launcher(unittest.TestCase):
    def setUp(self):
        self.report = Path(tempfile.mkdtemp()) / "report.jsonl"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gw, "_log", lambda _m: None).start()

    def events(self):
        return [json.loads(line) for line in self.report.read_text().splitlines()]

    def install(self, tid, rule, fault="stall"):
        broker = Broker(tid)
        mock.patch.object(gw, "_req", broker).start()
        sentinel = Path(tempfile.mkdtemp()) / "held"
        launcher.install(gw, rule, fault, self.report, sentinel)
        mock.patch.object(launcher.time, "sleep", lambda _s: sentinel.unlink(missing_ok=True)).start()
        return broker

    def test_an_ack_on_a_served_task_carries_its_claim_and_fires_the_rule_once(self):
        tid = "task-launcher-ack"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        rule = launcher.Rule("ack", tid, "*", "none", None)
        broker = self.install(tid, rule)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        acks = [op for _m, path, op in broker.calls if path.endswith("/ack")]
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0] and acks[0].startswith("ack-"), "the ack carries its claim's operation id")
        matched = [e for e in self.events() if e["event"] == "matched"]
        self.assertEqual(len(matched), 1)
        self.assertEqual({k: matched[0]["claim"][k] for k in ("task", "op", "kind", "lease_id", "run")},
                         {"task": tid, "op": acks[0], "kind": "ack", "lease_id": "L1", "run": tid},
                         "the report names the matched claim's whole identity")
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "released, the ack commits as usual")

    def test_a_named_lease_or_generation_that_is_not_the_claims_never_fires(self):
        tid = "task-launcher-miss"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        for rule in (launcher.Rule("ack", tid, "L9", None, None), launcher.Rule("ack", tid, None, 3, None),
                     launcher.Rule("ack", tid, None, None, f"{tid}~~2"), launcher.Rule("ack", "task-other", "*", None, None)):
            broker = self.install(tid, rule)
            gw._post_task_ack(tid, durable=True, lease_id="L1")
            self.assertEqual(rule.matches, [], vars(rule))
            self.assertEqual(len([c for c in broker.calls if c[1].endswith("/ack")]), 1, "passed through unchanged")
            with gw._ATTEMPTS.transition(tid) as t:
                t.put(execution_generation=None, awaiting_ack=True)

    def test_a_result_rule_matches_the_open_result_claim_and_a_second_match_fails_the_run(self):
        tid = "task-launcher-result"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        with mock.patch.object(gw, "_req", Broker(tid)):
            self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        rule = launcher.Rule("result", tid, "L1", 1, None)
        broker = self.install(tid, rule)
        with gw._ATTEMPTS.transition(tid) as t:
            claim = t.claim("result", file=[1, 2])
        doc = {"id": tid, "body": "x", "execution_generation": 1}
        gw._req("POST", "/v1/results", {"id": "task-elsewhere", "execution_generation": 1})
        gw._req("POST", "/v1/results", {**doc, "execution_generation": 2})
        self.assertEqual(rule.matches, [], "another task, a generation never claimed")
        gw._req("POST", "/v1/results", doc)
        gw._req("POST", "/v1/results", doc)
        self.assertEqual([m["op"] for m in rule.matches], [claim["op"], claim["op"]])
        self.assertEqual([e["event"] for e in self.events()], ["matched", "released", "FAIL: matched more than once"])
        self.assertEqual(len([c for c in broker.calls if c[1] == "/v1/results"]), 4, "every call went through")

    def test_the_crash_fault_records_the_brokers_real_answer_then_exits_before_the_commit(self):
        tid = "task-launcher-crash"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        rule = launcher.Rule("ack", tid, "L1", None, None)
        self.install(tid, rule, fault="crash")
        with mock.patch.object(launcher.os, "_exit", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                gw._post_task_ack(tid, durable=True, lease_id="L1")
        crashed = self.events()[-1]
        self.assertEqual(crashed["answer"]["execution_generation"], 1, "the broker's own answer is on record")
        self.assertIn("killing the bridge before its commit", crashed["event"])
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("execution_generation"), "nothing was committed")
        self.assertEqual([c["kind"] for c in gw._ATTEMPTS.get(tid)["claims"].values()], ["ack"], "its claim is open")

    def test_a_call_whose_claim_is_not_on_the_ledger_or_is_ambiguous_passes_through(self):
        rule = launcher.Rule("result", "task-a", "*", 1, None)
        two = {"claims": {"r1": {"kind": "result", "execution_generation": 1},
                          "r2": {"kind": "result", "execution_generation": 1}}}
        self.assertIsNone(rule.match("POST", "/v1/results", {"id": "task-a", "execution_generation": 1}, None, two))
        self.assertIsNone(rule.match("GET", "/v1/results", {"id": "task-a"}, None, two))

    def test_the_loader_is_loaded_as_the_script_would_be_and_a_stall_needs_its_sentinel(self):
        loader = Path(tempfile.mkdtemp()) / "remote-gateway-bridge.py"
        loader.write_text("RUNTIME_IDENTITY = {'entrypoint': __file__}\nRAN_MAIN = __name__ == '__main__'\n")
        with mock.patch.object(launcher, "LOADER", loader):
            mod = launcher.load_bridge()
        self.assertEqual((mod.RUNTIME_IDENTITY["entrypoint"], mod.RAN_MAIN), (str(loader), False),
                         "its script tail is not run by the load")
        for bad in (["--fault", "stall", "--kind", "ack"], ["--fault", "reclaim", "--kind", "ack", "--sentinel", "s"],
                    ["--fault", "crash", "--kind", "serve"]):
            with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                launcher.main([*bad, "--task", "t", "--report", str(self.report)])

    def test_reclaim_withholds_the_tasks_beats_and_its_newer_serve_until_released(self):
        tid = "task-launcher-reclaim"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        sent = []

        def broker(method, path, payload=None, timeout=None, **k):
            sent.append((method, path, payload))
            if method == "GET":
                return {"tasks": [{"id": tid, "lease_id": broker.lease}, {"id": "task-other", "lease_id": "Lx"}]}
            return _ack(tid, 1) if path.endswith("/ack") else {"ok": True}
        broker.lease = "L1"
        mock.patch.object(gw, "_req", broker).start()
        with mock.patch.object(gw, "_req", Broker(tid)):
            self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        sentinel = Path(tempfile.mkdtemp()) / "held"
        sentinel.write_text("hold")
        rule = launcher.Rule("serve", tid, None, None, None)
        launcher.install(gw, rule, "reclaim", self.report, sentinel)
        gw._req("POST", launcher.BEATS, {"tasks": [{"task_id": tid}, {"task_id": "task-other"}]})
        self.assertEqual(sent[-1][2]["tasks"], [{"task_id": "task-other"}], "its beats are withheld")
        self.assertEqual([t["id"] for t in gw._req("GET", "/v1/tasks?wait=1")["tasks"]], [tid, "task-other"],
                         "the running generation's own lease passes")
        broker.lease = "L2"
        for _ in range(2):
            self.assertEqual([t["id"] for t in gw._req("GET", "/v1/tasks?wait=1")["tasks"]], ["task-other"],
                             "the newer serve is kept back, every time it is offered")
        sentinel.unlink()
        self.assertEqual([t["id"] for t in gw._req("GET", "/v1/tasks?wait=1")["tasks"]], [tid, "task-other"])
        gw._req("POST", launcher.BEATS, {"tasks": [{"task_id": tid}]})
        self.assertEqual(sent[-1][2]["tasks"], [{"task_id": tid}], "released: beats pass again")
        self.assertEqual([e["event"] for e in self.events()], ["beats withheld", "matched", "released"])
        self.assertEqual(rule.matches[0]["lease_id"], "L2")
        sentinel.write_text("hold")
        broker.lease = "L3"
        gw._req("GET", "/v1/tasks?wait=1")
        self.assertEqual(self.events()[-1]["event"], "FAIL: matched more than once", "a third lease")

    def test_main_starts_the_loader_and_writes_its_identity_first(self):
        impl = Path(gw.__file__)
        ran = []
        fake = types.SimpleNamespace(_IMPL=impl, RUNTIME_IDENTITY={"build_sha": "abc", "module_sha256": "d"},
                                     _req=lambda *a, **k: {}, _ATTEMPTS=gw._ATTEMPTS,
                                     VOICE_ROOM_VERIFIER=types.SimpleNamespace(start=lambda: ran.append("verifier")),
                                     main=lambda: ran.append("main"))
        sentinel = Path(tempfile.mkdtemp()) / "held"
        argv = ["--fault", "stall", "--kind", "result", "--task", "task-x", "--lease", "*", "--generation", "2",
                "--report", str(self.report), "--sentinel", str(sentinel)]
        with mock.patch.object(launcher, "load_bridge", return_value=fake), \
                mock.patch.object(launcher.atexit, "register") as at_exit, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            launcher.main(argv)
        first = self.events()[0]
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["event"], "loaded")
        self.assertEqual((first["event"], first["module_file"], first["runtime_identity"]["build_sha"]),
                         ("loaded", str(impl.resolve()), "abc"))
        self.assertEqual(first["module_sha256"], launcher._sha256(impl))
        self.assertEqual((first["rule"]["report"], first["rule"]["sentinel"], first["rule"]["generation"]),
                         (str(self.report), str(sentinel), 2))
        self.assertEqual(ran, ["verifier", "main"], "the bridge's own start follows")
        self.assertIsNot(fake._req, None)
        at_exit.call_args[0][0]()
        self.assertEqual(self.events()[-1]["event"], "FAIL: the rule never matched")


if __name__ == "__main__":
    unittest.main()
