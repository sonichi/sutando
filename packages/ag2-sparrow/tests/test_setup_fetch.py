"""Sparrow fetches its served setup (`GET /v1/setup`) when a heartbeat announces a revision it
does not hold, and keeps the last good copy on any failure. No network. Run:
    python3 packages/ag2-sparrow/tests/test_setup_fetch.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import setup_fetch  # noqa: E402
from ag2_sparrow.adapters.hatch import runtime_text  # noqa: E402

AGENT = "@watch-agent:ag2.space"


def result(revision="acp.muse.2026-10-17.3", **over):
    out = {"schema_version": 1, "reason": "steps", "agent_mxid": AGENT, "revision": revision,
           "kind": "acp", "vendor": "muse", "min_package": "0.5.4", "steps": [],
           "text_schema_version": 1,
           "runtime_text": {"prompt": "Served prompt; run `<REPLY_COMMAND> reply <f>`.",
                            "worker_note": "Served note.",
                            "restart_hint": "Served hint: `<REPLY_COMMAND> keepalive keepawake`.",
                            "tier_addenda": {"owner": "", "team": "", "guest": ""}},
           "tunables": {"keepawake_interval_s": 900, "stuck_task_age_s": 900,
                        "hook_silent_age_s": 300, "heartbeat_cadence_s": 60},
           "package": {"min_version": "0.5.4", "target_version": None, "target_sha256": None},
           "message": None}
    out.update(over)
    return out


def write(path, text):
    Path(path).write_text(text, encoding="utf-8")
    return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Refresher(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        self.calls, self.answers, self.logs = [], [], []
        self.clock = Clock()

    def get(self, path, timeout_s):
        self.calls.append((path, timeout_s))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def refresher(self, write_fn=write):
        return setup_fetch.Refresher(self.get, write_fn, self.state, log=self.logs.append,
                                     clock=self.clock)

    def served(self):
        return setup_fetch.load_served(self.state)

    def test_a_new_announcement_is_fetched_and_stored_whole(self):
        self.answers = [result()]
        self.assertEqual(self.refresher().maybe_refresh("acp.muse.2026-10-17.3", AGENT), "fetched")
        self.assertEqual(self.calls, [("/v1/setup", setup_fetch.FETCH_TIMEOUT_S)])
        self.assertEqual(self.served(), result())
        # The adapter reads it through fetch_setup and caches it for the woken worker.
        (self.state / "setup-hint.json").write_text(json.dumps({"setup_revision": "acp.muse.2026-10-17.3"}))
        cache = runtime_text.refresh_cache(self.state)
        self.assertEqual(cache["revision"], "acp.muse.2026-10-17.3")
        self.assertEqual(runtime_text.wake_context(cache, {})[1], "served revision acp.muse.2026-10-17.3")

    def test_a_changed_revision_is_refetched(self):
        r = self.refresher()
        self.answers = [result("r1"), result("r2")]
        self.assertEqual(r.maybe_refresh("r1", AGENT), "fetched")
        self.assertEqual(r.maybe_refresh("r2", AGENT), "fetched")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.served()["revision"], "r2")

    def test_an_unchanged_revision_is_not_refetched_even_after_a_restart(self):
        self.answers = [result("r1")]
        r = self.refresher()
        r.maybe_refresh("r1", AGENT)
        self.assertEqual(r.maybe_refresh("r1", AGENT), "unchanged")
        self.assertEqual(self.refresher().maybe_refresh("r1", AGENT), "unchanged")
        self.assertEqual(len(self.calls), 1)
        for nothing in (None, ""):
            self.assertEqual(r.maybe_refresh(nothing, AGENT), "none")
        self.assertEqual(len(self.calls), 1)

    def test_a_failed_fetch_keeps_the_last_good_copy_and_backs_off(self):
        self.answers = [result("r1")]
        r = self.refresher()
        r.maybe_refresh("r1", AGENT)
        good = self.served()
        token = "bearer-secret-0123456789"
        self.answers = [urllib.error.HTTPError("https://gw/v1/setup", 503, token, {}, None),
                        TimeoutError(token), result("r2")]
        self.assertEqual(r.maybe_refresh("r2", AGENT), "failed")
        self.assertEqual(self.served(), good)
        self.assertEqual(r.maybe_refresh("r2", AGENT), "backoff", "no retry before the backoff")
        self.assertEqual(len(self.calls), 2)
        self.clock.now += setup_fetch.BACKOFF_MIN_S
        self.assertEqual(r.maybe_refresh("r2", AGENT), "failed")
        self.assertEqual(self.served(), good)
        self.clock.now += setup_fetch.BACKOFF_MIN_S
        self.assertEqual(r.maybe_refresh("r2", AGENT), "backoff", "the backoff doubles")
        self.clock.now += setup_fetch.BACKOFF_MIN_S
        self.assertEqual(r.maybe_refresh("r2", AGENT), "fetched")
        self.assertEqual(self.served()["revision"], "r2")
        self.assertIn("HTTP 503", self.logs[1])
        self.assertNotIn(token, "\n".join(self.logs))

    def test_a_newly_announced_revision_is_tried_at_once_and_backs_off_from_the_minimum(self):
        r = self.refresher()
        self.answers = [OSError("down")] * 3
        r.maybe_refresh("r2", AGENT)
        self.clock.now += setup_fetch.BACKOFF_MIN_S
        r.maybe_refresh("r2", AGENT)
        self.assertIn("retry in 120s", self.logs[-1])
        self.assertEqual(r.maybe_refresh("r3", AGENT), "failed", "not held back by r2's backoff")
        self.assertIn("retry in 60s", self.logs[-1])

    def test_an_uncommitted_write_is_a_failure_and_retried(self):
        self.answers = [result("r1"), result("r1")]
        r = self.refresher(write_fn=lambda path, text: False)
        self.assertEqual(r.maybe_refresh("r1", AGENT), "failed")
        self.assertIsNone(self.served())
        self.clock.now += setup_fetch.BACKOFF_MIN_S
        self.assertEqual(r.maybe_refresh("r1", AGENT), "failed", "not counted as held")

    def test_a_writer_that_raises_never_reaches_the_heartbeat_and_keeps_the_last_good_copy(self):
        self.answers = [result("r1")]
        self.refresher().maybe_refresh("r1", AGENT)
        good = self.served()
        token = "bearer-secret-0123456789"

        def boom(path, text):
            raise OSError(28, "disk full " + token)
        r = self.refresher(write_fn=boom)
        self.answers = [result("r2")]
        self.assertEqual(r.maybe_refresh("r2", AGENT), "failed")
        self.assertEqual(self.served(), good)
        self.assertIn("retry in 60s", self.logs[-1])
        self.assertEqual(r.maybe_refresh("r2", AGENT), "backoff")
        self.assertNotIn(token, "\n".join(self.logs))

    def test_the_backoff_is_capped(self):
        r = self.refresher()
        self.answers = [OSError("down")] * 20
        for _ in range(20):
            self.clock.now += setup_fetch.BACKOFF_MAX_S
            r.maybe_refresh("r1", AGENT)
        self.assertLessEqual(r._retry_at - self.clock.now, setup_fetch.BACKOFF_MAX_S)

    def test_a_malformed_or_unknown_version_result_is_refused(self):
        self.answers = [result("r1")]
        r = self.refresher()
        r.maybe_refresh("r1", AGENT)
        good = self.served()
        partial = result("r2")
        partial["tunables"] = None
        cases = {
            "not an object": ["x"],
            "unknown schema_version": result("r2", schema_version=2),
            "unknown schema_version ": result("r2", schema_version=True),
            "unknown text_schema_version": result("r2", text_schema_version=2),
            "revision is not the announced one": result("r9"),
            "agent_mxid is not this agent": result("r2", agent_mxid="@other:ag2.space"),
            "listener fields are partly set": partial,
            "runtime_text, tunables or package is not an object": result("r2", runtime_text="x"),
            "no kind or vendor": result("r2", vendor=None),
        }
        for why, bad in cases.items():
            with self.subTest(why=why):
                self.clock.now += setup_fetch.BACKOFF_MAX_S
                self.answers = [bad]
                self.assertEqual(r.maybe_refresh("r2", AGENT), "refused")
                self.assertIn(why.strip(), self.logs[-1])
                self.assertEqual(self.served(), good, "the last good copy stays")
        self.clock.now += setup_fetch.BACKOFF_MAX_S
        self.answers = [result("r2", text=("x" * setup_fetch.MAX_BYTES))]
        self.assertEqual(r.maybe_refresh("r2", AGENT), "refused")
        self.assertEqual(self.served(), good)

    def test_a_result_with_no_listener_text_is_kept_and_the_adapter_falls_back(self):
        empty = result("dot.1", **{k: None for k in setup_fetch.LISTENER_KEYS})
        self.answers = [empty]
        self.assertEqual(self.refresher().maybe_refresh("dot.1", AGENT), "fetched")
        (self.state / "setup-hint.json").write_text(json.dumps({"setup_revision": "dot.1"}))
        self.assertIsNone(runtime_text.refresh_cache(self.state))

    def test_fetch_setup_returns_only_the_requested_kind_and_vendor(self):
        self.assertIsNone(setup_fetch.fetch_setup("acp", "muse", self.state))
        self.answers = [result("r1")]
        self.refresher().maybe_refresh("r1", AGENT)
        self.assertEqual(setup_fetch.fetch_setup("acp", "muse", self.state)["revision"], "r1")
        self.assertIsNone(setup_fetch.fetch_setup("acp", "dot", self.state))
        self.assertIsNone(setup_fetch.fetch_setup("acp", "muse", self.state, instance="b"))


class Bridge(unittest.TestCase):
    """The running bridge wires the heartbeat reply to the fetch through its own _req."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.state = self.home / "state"
        self.state.mkdir()

    def run_bridge(self, script):
        env = hermetic_env(self.home, PYTHONPATH=str(PKG_ROOT), AGENT_MXID=AGENT,
                           AGENT_CONNECT_TASK_DIR=str(self.home / "t"),
                           AGENT_CONNECT_RESULT_DIR=str(self.home / "r"),
                           AGENT_CONNECT_STATE_DIR=str(self.state),
                           REMOTE_TASK_URL="https://gw.example/relay",
                           REMOTE_TASK_TOKEN="fake-relay-token-0123456789abcdef")
        r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                           text=True, cwd=str(self.home), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stdout + r.stderr

    def test_heartbeats_fetch_on_a_new_revision_only_and_survive_a_refused_fetch(self):
        script = """
import json
from unittest import mock
from ag2_sparrow import remote_gateway_bridge as m
calls, served = [], %s
def req(method, path, payload=None, timeout=0, deadline=None):
    calls.append((method, path, timeout, deadline is not None))
    if path == "/v1/heartbeat":
        return {"ok": True, "setup_revision": revision}
    if path == "/v1/setup":
        return answer
    raise AssertionError(path)
with mock.patch.object(m, "_req", req):
    revision, answer = "r1", served
    m._post_heartbeat(set(), force=True)
    m._post_heartbeat(set(), force=True)
    revision, answer = "r2", "not json"
    m._post_heartbeat(set(), force=True)
print(json.dumps({"calls": calls, "served": json.loads(m.setup_fetch.served_path(m._STATE).read_text())}))
""" % repr(result("r1"))
        out, logs = self.run_bridge(script)
        setup_calls = [c for c in out["calls"] if c[1] == "/v1/setup"]
        self.assertEqual(setup_calls, [["GET", "/v1/setup", setup_fetch.FETCH_TIMEOUT_S, True]] * 2,
                         "fetched for r1 once, then for r2; bounded by a deadline")
        self.assertEqual(out["served"], result("r1"), "the refused r2 left r1 in place")
        self.assertIn("refused (not an object)", logs)
        self.assertNotIn("fake-relay-token", logs)


    def test_an_unreadable_identity_still_fetches_and_never_breaks_the_heartbeat(self):
        script = """
import json
from unittest import mock
from ag2_sparrow import remote_gateway_bridge as m
def req(method, path, payload=None, timeout=0, deadline=None):
    return {"ok": True, "setup_revision": "r1"} if path == "/v1/heartbeat" else served
def unread():
    raise RuntimeError("identity file holds " + m.TOKEN)
served = %s
with mock.patch.object(m, "_req", req), mock.patch.object(m, "_reenroll_identity", unread):
    beat = m._post_heartbeat(set(), force=True)
print(json.dumps({"beat": beat, "served": json.loads(m.setup_fetch.served_path(m._STATE).read_text())}))
""" % repr(result("r1", agent_mxid="@someone-else:ag2.space"))
        out, logs = self.run_bridge(script)
        self.assertIs(out["beat"], True, "the heartbeat still reports success")
        self.assertEqual(out["served"]["revision"], "r1", "fetched with the agent check skipped")
        self.assertIn("served setup: identity unread", logs)
        self.assertNotIn("fake-relay-token", logs)


if __name__ == "__main__":
    unittest.main()
