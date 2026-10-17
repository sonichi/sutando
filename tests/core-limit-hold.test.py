#!/usr/bin/env python3
"""A core refused for a provider limit is held, not relaunched in a loop (#5633).

Pinned here:
  * the classifier: 429, rate_limit_error and the CLI's usage-limit banners are a
    limit; a plain crash or a 500 is not;
  * `decide`: a limit holds with a recorded reason, repeated revives stay held,
    and the core launches by itself once the reset time passes; with no reset
    time a bounded backoff applies and grows only on fresh evidence;
  * the real launcher (start-cli.sh, PATH-shimmed tmux): a limit leaves no core
    window and exits 75, a plain crash is healed as before, and the next revive
    after the hold ends launches the core;
  * the status surfaces: runtime-health reports `paused` with the reason, and the
    health-check recovery does not relaunch or DM while the hold is in force.

Run: python3 tests/core-limit-hold.test.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import core_limit_hold as clh  # noqa: E402

SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def limited_record(now: float, *, reset_in: float = 3600, rejected: bool = True,
                   model: str = "claude-opus-5-5", ago: float = 30) -> dict:
    """The proxy's record after the CLI's request was refused."""
    status = "rejected" if rejected else "allowed"
    headers = {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-7d-status": status,
        "anthropic-ratelimit-unified-7d-utilization": "1.0",
        "anthropic-ratelimit-unified-7d-reset": str(int(now + reset_in)),
    }
    return {
        "available": not rejected,
        "last_checked": iso(now - ago),
        "headers": headers,
        "recent_rejections": [{
            "ts": iso(now - ago), "status": 429, "path": "/v1/messages", "model": model,
            "snippet": '{"type":"error","error":{"type":"rate_limit_error","message":"usage limit"}}',
        }],
    }


def owner_task(tasks: Path, name: str, body: str, tier: str = "owner") -> Path:
    path = tasks / f"{name}.txt"
    path.write_text(f"id: {name}\nsource: ag2space\naccess_tier: {tier}\ntask: {body}\n")
    return path


class Classifier(unittest.TestCase):
    def test_limits(self):
        now = time.time()
        for text in ('API Error: 429 {"error":{"type":"rate_limit_error"}}',
                     "Claude usage limit reached. Your limit will reset at 3pm (America/Los_Angeles).",
                     "5-hour limit reached ∙ resets 3pm",
                     "Opus weekly limit reached ∙ resets Oct 20, 9am",
                     "You've hit your limit · resets 11am",
                     "Claude AI usage limit reached|1760040000"):
            self.assertIsNotNone(clh.classify_text(text, now), text)

    def test_not_limits(self):
        now = time.time()
        for text in ("", "API Error: 500 internal server error", "Segmentation fault",
                     "fetch failed", "The core exited with status 1"):
            self.assertIsNone(clh.classify_text(text, now), text)

    def test_reset_from_cli_text(self):
        at = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc).timestamp()
        lim = clh.classify_text("5-hour limit reached ∙ resets 3pm (UTC)", at)
        self.assertEqual(lim.reset_at, datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc).timestamp())
        lim = clh.classify_text("usage limit reached ∙ resets 9am (UTC)", at)
        self.assertEqual(lim.reset_at, datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc).timestamp())


class Decide(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_limit_holds_until_reset_then_resumes(self):
        now = 1_800_000_000.0
        rec = limited_record(now, reset_in=3600)
        d = clh.decide(self.ws, now, quota=rec)
        self.assertFalse(d.launch)
        hold = json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())
        self.assertEqual(hold["status"], "held")
        self.assertEqual(hold["kind"], "usage-limit")
        self.assertEqual(hold["model"], "claude-opus-5-5")
        self.assertEqual(hold["reset_at"], now + 3600)
        self.assertIn("core paused: usage limit on claude-opus-5-5, resets", hold["reason"])
        status = json.loads((self.ws / "state" / "core-status.json").read_text())
        self.assertEqual((status["status"], status["step"]), ("paused", hold["reason"]))
        # Revives every two minutes until the reset: none launches.
        for minute in range(2, 60, 2):
            self.assertFalse(clh.decide(self.ws, now + minute * 60, quota=rec).launch, minute)
        # The reset passed and the same record is all there is: the core comes back.
        after = now + 3600 + clh.RESET_GRACE_S + 1
        d = clh.decide(self.ws, after, quota=rec)
        self.assertTrue(d.launch)
        self.assertEqual(clh.read_hold(self.ws)["status"], "released")
        self.assertIsNone(clh.active_hold(self.ws, after))

    def test_no_reset_backs_off_and_grows_only_on_new_evidence(self):
        now = 1_800_000_000.0
        rec = limited_record(now, rejected=False, ago=1)  # a 429 with no rejected window
        rec["last_checked"] = iso(now - 1)
        d = clh.decide(self.ws, now, quota=rec)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["until"], now + clh.BACKOFF_BASE_S)
        self.assertIn("retrying", d.line)
        t = now + clh.BACKOFF_BASE_S + 1
        self.assertTrue(clh.decide(self.ws, t, quota=rec).launch)
        # The relaunched core is refused again: a fresh 429, a longer hold.
        rec2 = limited_record(t + 30, rejected=False, ago=1)
        d = clh.decide(self.ws, t + 30, quota=rec2)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["strikes"], 2)
        self.assertEqual(d.hold["until"], t + 30 + 2 * clh.BACKOFF_BASE_S)

    def test_backoff_is_bounded(self):
        lim = clh.Limit("rate-limit", 0, None, None, "provider-record")
        self.assertEqual(clh._hold_until(lim, 20, 0), clh.BACKOFF_CAP_S)

    def test_plain_crash_launches(self):
        now = 1_800_000_000.0
        self.assertTrue(clh.decide(self.ws, now, quota=None).launch)
        self.assertTrue(clh.decide(self.ws, now, quota={"headers": {}}).launch)
        self.assertIsNone(clh.read_hold(self.ws))

    def test_old_evidence_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, ago=clh.EVIDENCE_WINDOW_S + 60)
        self.assertTrue(clh.decide(self.ws, now, quota=rec).launch)

    def test_served_since_the_429_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, rejected=False, ago=120)
        rec["last_checked"] = iso(now - 10)
        self.assertTrue(clh.decide(self.ws, now, quota=rec).launch)

    def test_other_model_family_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, model="claude-opus-5-5")
        self.assertTrue(clh.decide(self.ws, now, quota=rec, launch_model="sonnet").launch)
        self.assertFalse(clh.decide(self.ws, now, quota=rec, launch_model="claude-opus-5-5").launch)

    def test_clear_lifts_and_same_evidence_does_not_rearm(self):
        now = 1_800_000_000.0
        rec = limited_record(now)
        self.assertFalse(clh.decide(self.ws, now, quota=rec).launch)
        clh.clear(self.ws, now + 1)
        self.assertTrue(clh.decide(self.ws, now + 2, quota=rec).launch)

    def test_cli_transcript_evidence(self):
        now = time.time()
        proj = self.ws / "projects"
        proj.mkdir()
        entry = {"isApiErrorMessage": True, "timestamp": iso(now - 20), "type": "assistant",
                 "message": {"model": "<synthetic>", "content": [
                     {"type": "text", "text": "Claude usage limit reached. Your limit will reset at 3pm (UTC)."}]}}
        (proj / "s.jsonl").write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(entry) + "\n")
        d = clh.decide(self.ws, now, quota=None, projects_dir=proj)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["source"], "cli-transcript")
        self.assertIsNotNone(d.hold["reset_at"])
        (proj / "s.jsonl").write_text(json.dumps(dict(entry, message={"content": "API Error: 500"})) + "\n")
        clh.clear(self.ws)
        self.assertTrue(clh.decide(self.ws, now + 1, quota=None, projects_dir=proj).launch)


# ---- the real launcher, PATH-shimmed (harness from start-cli-private-socket-foreign-core) ----
TMUX_STUB = r"""#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
echo "$sub $*" >> "$TMUX_LOG"
case "$sub" in
  has-session) case "$*" in *watcher*) exit 1 ;; esac; [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session) touch "$SESS_MARK"; exit 0 ;;
  new-window) echo 0; exit 0 ;;
  list-panes) [ -f "$SESS_MARK" ] || exit 1; exit 0 ;;
  *) exit 0 ;;
esac
"""
PY_STUB = r"""#!/bin/bash
for a in "$@"; do
  case "$a" in
    *core_heartbeat.py|*core-input-watch.py|*core-supervisor-relay.py|*notifier*) exit 0 ;;
  esac
done
exec "$REAL_PY" "$@"
"""


def transcript(proj: Path, *entries) -> None:
    """A session transcript, oldest first: ("error", age, text) or ("served", age, model)."""
    now, lines = time.time(), []
    for kind, age, value in entries:
        if kind == "error":
            lines.append({"type": "assistant", "isApiErrorMessage": True, "timestamp": iso(now - age),
                          "message": {"model": "<synthetic>", "content": [{"type": "text", "text": value}]}})
        else:
            lines.append({"type": "assistant", "timestamp": iso(now - age),
                          "message": {"model": value, "content": [{"type": "text", "text": "done"}]}})
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines))


class ServedSince(unittest.TestCase):
    """The transcript twin of the record's served-since check (Susan's review at 849cdcad)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        self.proj = self.ws / "projects"

    def tearDown(self):
        self.td.cleanup()

    def test_a_turn_served_after_the_error_does_not_hold(self):
        transcript(self.proj, ("served", 900, "claude-opus-5-5"),
                   ("error", 600, "Claude usage limit reached. Your limit will reset at 3pm (UTC)."),
                   ("served", 300, "claude-opus-5-5"))
        d = clh.decide(self.ws, time.time(), quota=None, projects_dir=self.proj)
        self.assertTrue(d.launch, "a core that answered after the error was held")

    def test_sonnet_served_after_an_opus_limit_does_not_hold_sonnet(self):
        transcript(self.proj, ("served", 900, "claude-opus-5-5"),
                   ("error", 600, "Opus weekly limit reached ∙ resets 3pm (UTC)"),
                   ("served", 300, "claude-sonnet-5"))
        d = clh.decide(self.ws, time.time(), quota=None, projects_dir=self.proj, launch_model="sonnet")
        self.assertTrue(d.launch, "the documented /model sonnet recovery was held until the Opus reset")

    def test_the_limited_model_comes_from_the_last_real_turn(self):
        transcript(self.proj, ("served", 900, "claude-opus-5-5"),
                   ("error", 600, "Claude usage limit reached. Your limit will reset at 3pm (UTC)."))
        lim = clh.transcript_evidence(self.proj, time.time())
        self.assertEqual(lim.model, "claude-opus-5-5")
        self.assertTrue(clh.decide(self.ws, time.time(), quota=None, projects_dir=self.proj,
                                   launch_model="sonnet").launch)
        self.assertFalse(clh.decide(self.ws, time.time(), quota=None, projects_dir=self.proj,
                                    launch_model="claude-opus-5-5").launch)

    def test_clear_mirrors_starting(self):
        now = 1_800_000_000.0
        clh.decide(self.ws, now, quota=limited_record(now))
        clh.clear(self.ws, now + 1)
        status = json.loads((self.ws / "state" / "core-status.json").read_text())
        self.assertEqual(status["status"], "starting")


class Launcher(unittest.TestCase):
    """The desktop's revive is `start-cli.sh` with the session up and the core gone."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        self.ws = td / "workspace"
        (self.ws / "state").mkdir(parents=True)
        self.bin = td / "bin"
        self.bin.mkdir()
        self.sess = td / "sess"
        self.sess.write_text("")
        self.log = td / "tmux.log"
        for name, body in (("tmux", TMUX_STUB), ("pgrep", "#!/bin/bash\nexit 1\n"),
                           ("ps", "#!/bin/bash\nexit 0\n"), ("claude", "#!/bin/bash\nexit 0\n"),
                           ("py", PY_STUB)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(td),
            "SESS_MARK": str(self.sess), "TMUX_LOG": str(self.log),
            "SUTANDO_PY": str(self.bin / "py"), "REAL_PY": sys.executable,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_RESTART_GRACE_S": "1",
            "SUTANDO_TMUX_SOCKET": str(td / "run" / "tmux.sock"),
        }

    def tearDown(self):
        self.td.cleanup()

    def revive(self):
        self.log.write_text("")
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=self.env, capture_output=True,
                           text=True, timeout=120)
        healed = any(c.startswith("new-window") for c in self.log.read_text().splitlines())
        return r.returncode, r.stdout + r.stderr, healed

    def write_quota(self, rec):
        (self.ws / "state" / "quota-state.json").write_text(json.dumps(rec))

    def test_plain_crash_is_revived(self):
        rc, out, healed = self.revive()
        self.assertTrue(healed, out[-600:])
        self.assertNotIn("not launched", out)

    def test_limit_holds_then_resumes_after_reset(self):
        self.write_quota(limited_record(time.time()))
        for _ in range(3):
            rc, out, healed = self.revive()
            self.assertEqual(rc, 75, out[-600:])
            self.assertFalse(healed, "a held core was relaunched")
            self.assertIn("core paused: usage limit on claude-opus-5-5, resets", out)
        hold = json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())
        # The reset passes: move the hold's end into the past, nothing else.
        hold["until"] = time.time() - 1
        (self.ws / "state" / clh.HOLD_FILE).write_text(json.dumps(hold))
        rc, out, healed = self.revive()
        self.assertTrue(healed, out[-600:])
        self.assertEqual(json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())["status"], "released")

    def test_owner_reply_switches_model_and_the_core_starts(self):
        self.write_quota(limited_record(time.time()))
        rc, out, healed = self.revive()
        self.assertEqual(rc, 75, out[-600:])
        tasks = self.ws / "tasks"
        tasks.mkdir(exist_ok=True)
        owner_task(tasks, "task-100", "switch to sonnet")
        owner_task(tasks, "task-101", "what is on my calendar today?")
        owner_task(tasks, "task-102", "switch to sonnet", tier="guest")
        rc, out, healed = self.revive()
        self.assertTrue(healed, out[-900:])
        models = [json.loads(p.read_text()).get("model") for p in self.ws.rglob("settings.json")]
        self.assertIn("sonnet", models, out[-900:])
        self.assertIn("sonnet", (self.ws / "results" / "task-100.txt").read_text())
        # The ordinary owner task waits for the core; a guest's reply is not an action.
        self.assertFalse((self.ws / "results" / "task-101.txt").exists())
        self.assertFalse((self.ws / "results" / "task-102.txt").exists())
        import task_queue
        self.assertEqual({p.name for p in task_queue.pending_files(self.ws)}, {"task-101.txt", "task-102.txt"})
        # The handled reply leaves the live queue; the ordinary owner task stays for the core.
        self.assertFalse((tasks / "task-100.txt").exists(), "a returning core would re-run the switch")
        self.assertEqual([p.name for p in (tasks / "archive").rglob("task-100.txt*")], ["task-100.txt"])
        self.assertTrue((tasks / "task-101.txt").exists())


class StatusSurfaces(unittest.TestCase):
    def _load(self, name, rel):
        spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_runtime_health_reports_paused(self):
        rh = self._load("rh_limit", "src/runtime-health.py")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            clh.decide(ws, time.time(), quota=limited_record(time.time()))
            rh._core_running = lambda: True
            rh._pane_text = lambda: ""
            rh._core_status = lambda w: (None, None)
            rh._gateway_running = lambda: True
            rh._resolve_workspace = lambda repo: str(ws)
            d = rh.derive()
        self.assertEqual(d["health"], "paused")
        self.assertEqual(rh.severity_of("paused"), "warn")
        self.assertEqual(rh.severity_gate({"health": "paused", "confirm": 9}), "report")
        self.assertIn("core paused: usage limit on claude-opus-5-5", d["detail"])

    def test_health_check_recovery_holds(self):
        hc = self._load("hc_limit", "src/health-check.py")
        with tempfile.TemporaryDirectory() as td:
            calls = []
            r = hc.recover_core_if_wedged(
                state_file=Path(td) / "core-recovery.json", now=1_800_000_000.0,
                alive_fn=lambda: False, oldest_task_fn=lambda: None, status_ts_fn=lambda: None,
                just_booted_fn=lambda: False, stopped_fn=lambda: False,
                restart_fn=lambda: calls.append("restart") or True,
                sender=lambda m: calls.append("dm") or True,
                limit_hold_fn=lambda: {"until": 1_800_003_600.0, "reason": "core paused: x"})
        self.assertEqual(r["action"], "limit-hold")
        self.assertEqual(calls, [])


class EdgePaths(unittest.TestCase):
    """The non-happy branches: odd reset text, unreadable transcripts, the CLI itself."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_reset_text_forms(self):
        at = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clh.parse_reset("usage limit, resets in 2h", at), at + 7200)
        self.assertEqual(clh.parse_reset("usage limit, resets in 30 minutes", at), at + 1800)
        self.assertEqual(clh.parse_reset("429 retry-after: 40", at), at + 40)
        self.assertIsNone(clh.parse_reset("usage limit reached", at))
        self.assertIsNone(clh.parse_reset("limit reached, resets Foo 3, 9am", at))
        past = clh.parse_reset("resets Jan 2, 9am (UTC)", at)
        self.assertEqual(past, datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNotNone(clh.parse_reset("resets 9am (Not/AZone)", at))

    def test_record_shapes(self):
        now = 1_800_000_000.0
        self.assertIsNone(clh.record_evidence("not a dict", now))
        rec = limited_record(now, rejected=False, ago=1)
        rec["recent_rejections"].insert(0, {"ts": iso(now - 2), "status": 500})
        rec["recent_rejections"][1]["snippet"] = "rate limited, retry-after: 90"
        lim = clh.record_evidence(rec, now)
        self.assertEqual((lim.kind, lim.reset_at), ("rate-limit", now - 1 + 90))

    def test_transcript_unreadable_and_odd_lines(self):
        now = time.time()
        proj = self.ws / "projects"
        proj.mkdir()
        self.assertIsNone(clh.transcript_evidence(None, now))
        from unittest import mock
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertIsNone(clh.transcript_evidence(proj, now))
        old = proj / "old.jsonl"
        old.write_text("{}\n")
        os_t = now - clh.EVIDENCE_WINDOW_S - 60
        import os
        os.utime(old, (os_t, os_t))
        self.assertIsNone(clh.transcript_evidence(proj, now))
        cur = proj / "cur.jsonl"
        cur.write_text("not json\n" + json.dumps({"type": "user"}) + "\n")
        self.assertIsNone(clh.transcript_evidence(proj, now))
        stale = {"isApiErrorMessage": True, "timestamp": iso(now - clh.EVIDENCE_WINDOW_S - 60),
                 "message": {"content": "usage limit reached"}}
        cur.write_text(json.dumps(stale) + "\n")
        self.assertIsNone(clh.transcript_evidence(proj, now))
        with mock.patch("builtins.open", side_effect=OSError("denied")):
            self.assertIsNone(clh.transcript_evidence(proj, now))

    def test_mirror_failure_does_not_stop_the_hold(self):
        from unittest import mock
        real = clh.write_status

        def fail_status(name, payload, workspace=None):
            if name == "core-status.json":
                raise OSError("read-only")
            return real(name, payload, workspace=workspace)
        with mock.patch.object(clh, "write_status", side_effect=fail_status):
            d = clh.decide(self.ws, 1_800_000_000.0, quota=limited_record(1_800_000_000.0))
        self.assertFalse(d.launch)
        self.assertIsNone(clh.clear(self.ws / "nowhere"))

    def test_unqueued_notice_does_not_stop_the_hold(self):
        (self.ws / "results").write_text("a file where the directory should be")
        d = clh.decide(self.ws, 1_800_000_000.0, quota=limited_record(1_800_000_000.0))
        self.assertFalse(d.launch)
        self.assertFalse(d.hold["notice_queued"])

    def test_cli_status_clear_gate(self):
        import contextlib
        import io
        from unittest import mock
        now = time.time()
        (self.ws / "state" / "quota-state.json").write_text(json.dumps(limited_record(now)))
        out = io.StringIO()
        with mock.patch.object(clh, "resolve_workspace", return_value=self.ws), \
                contextlib.redirect_stdout(out):
            self.assertEqual(clh.main(["clear"]), 0)
            self.assertEqual(clh.main(["gate", "--cwd", str(self.ws)]), clh.HOLD_EXIT)
            self.assertEqual(clh.main(["status"]), 0)
            self.assertEqual(clh.main(["clear"]), 0)
            self.assertEqual(clh.main(["gate"]), 0)
        text = out.getvalue()
        self.assertIn("core paused: usage limit", text)
        self.assertIn("hold lifted", text)
        self.assertIn("no hold in force", text)


class ReviewFixes(unittest.TestCase):
    """Mini's review on d2c7cbe7: each case failed before its fix."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def notices(self):
        return sorted((self.ws / "results").glob("proactive-*.txt")) if (self.ws / "results").is_dir() else []

    def test_model_switch_during_hold_launches(self):
        now = 1_800_000_000.0
        rec = limited_record(now, model="claude-opus-5-5")
        self.assertFalse(clh.decide(self.ws, now, quota=rec, launch_model="claude-opus-5-5").launch)
        d = clh.decide(self.ws, now + 60, quota=rec, launch_model="sonnet")
        self.assertTrue(d.launch, "an Opus hold kept a Sonnet core down")
        self.assertIsNone(clh.active_hold(self.ws, now + 61))

    def test_one_owner_notice_per_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, reset_in=3600)
        for minute in range(0, 30, 2):
            clh.decide(self.ws, now + minute * 60, quota=rec)
        files = self.notices()
        self.assertEqual(len(files), 1, files)
        body = files[0].read_text()
        self.assertTrue(body.startswith("[dm-only]\n"))
        self.assertIn("usage limit on claude-opus-5-5", body)
        self.assertIn("starts again by itself", body)
        self.assertIn("switch to sonnet", body)
        self.assertEqual(clh.read_hold(self.ws)["notice"], files[0].name)
        # The retry after the hold is refused again: a longer hold, no second notice.
        after = now + 3600 + clh.RESET_GRACE_S + 1
        self.assertTrue(clh.decide(self.ws, after, quota=rec).launch)
        self.assertFalse(clh.decide(self.ws, after + 30, quota=limited_record(after + 30, ago=1)).launch)
        self.assertEqual(len(self.notices()), 1)

    def test_far_reset_is_rechecked(self):
        now = 1_800_000_000.0
        far = clh.Limit("usage-limit", now, now + 5 * 86400, None, "provider-record")
        self.assertEqual(clh._hold_until(far, 1, now), now + 6 * 3600)
        near = clh.Limit("usage-limit", now, now + 7200, None, "provider-record")
        self.assertEqual(clh._hold_until(near, 1, now), now + 7200 + clh.RESET_GRACE_S)

    def test_429_must_be_a_status(self):
        now = time.time()
        for text in ("see https://github.com/org/repo/pull/429 for details",
                     "fixed in issue 429", "request id 429 failed"):
            self.assertIsNone(clh.classify_text(text, now), text)
        for text in ("API Error: 429 overloaded", "HTTP 429", '{"status": 429}', "status_code=429",
                     "429 Too Many Requests"):
            self.assertIsNotNone(clh.classify_text(text, now), text)


class OwnerRecovery(unittest.TestCase):
    """The owner's way out while the core is down."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()
        self.calls = self.ws / "switch-calls"
        self.switch = self.ws / "switch.sh"
        self.switch.write_text(f'#!/bin/bash\necho "$@" >> {self.calls}\nexit 0\n')
        self.switch.chmod(0o755)
        self.now = 1_800_000_000.0
        clh.decide(self.ws, self.now, quota=limited_record(self.now))

    def tearDown(self):
        self.td.cleanup()

    def notices(self):
        return sorted((self.ws / "results").glob("proactive-*.txt"))

    def test_notice_offers_product_actions(self):
        body = self.notices()[0].read_text()
        self.assertIn('"switch to sonnet"', body)
        self.assertIn('"retry now"', body)
        self.assertIn("Agent Settings", body)
        self.assertNotIn("python3", body)
        self.assertNotIn("settings.json", body)

    def test_switch_reply_runs_the_switch_once_and_lifts_the_hold(self):
        owner_task(self.ws / "tasks", "task-1", "Switch to Sonnet")
        done = clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=str(self.switch))
        self.assertEqual(done, ["task-1"])
        self.assertEqual(self.calls.read_text().split(), ["sonnet", "--offline"])
        self.assertIsNone(clh.active_hold(self.ws, self.now + 61))
        self.assertIn("sonnet", (self.ws / "results" / "task-1.txt").read_text())
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 120, switch_cmd=str(self.switch)), [])
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def test_delivered_answer_is_not_sent_again(self):
        for body in ("switch to sonnet", "retry now"):
            with self.subTest(body=body):
                self.tearDown()
                self.setUp()
                owner_task(self.ws / "tasks", "task-11", body)
                self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=str(self.switch)), ["task-11"])
                live = self.ws / "results" / "task-11.txt"
                month = self.ws / "results" / "archive" / "2026-10"
                month.mkdir(parents=True, exist_ok=True)
                live.rename(month / "task-11.txt")  # the bridge delivered it and archived it
                for minute in (2, 4):
                    clh.handle_owner_actions(self.ws, self.now + minute * 60, switch_cmd=str(self.switch))
                self.assertFalse(live.exists(), "a delivered answer was queued again")
                entry = clh.read_hold(self.ws)["actions"][-1]
                self.assertEqual((entry["answered"], entry["lifted"], entry["archived"]), (True, True, True))

    def test_answered_but_unarchived_reply_is_archived_not_redone(self):
        owner_task(self.ws / "tasks", "task-8", "retry now")
        (self.ws / "results").mkdir(exist_ok=True)
        (self.ws / "results" / "task-8.txt").write_text("answered before a crash\n")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=None), [])
        self.assertFalse((self.ws / "tasks" / "task-8.txt").exists())
        self.assertEqual((self.ws / "results" / "task-8.txt").read_text(), "answered before a crash\n")
        self.assertIsNotNone(clh.active_hold(self.ws, self.now + 61))

    def test_retry_reply_lifts_the_hold(self):
        owner_task(self.ws / "tasks", "task-2", "retry now")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=None), ["task-2"])
        self.assertIsNone(clh.active_hold(self.ws, self.now + 61))
        self.assertTrue(clh.decide(self.ws, self.now + 62, quota=limited_record(self.now)).launch)

    def test_non_owner_and_other_text_are_not_actions(self):
        owner_task(self.ws / "tasks", "task-3", "switch to sonnet", tier="team")
        owner_task(self.ws / "tasks", "task-4", "please switch to sonnet when you can and also email Bob")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=str(self.switch)), [])
        self.assertFalse(self.calls.exists())
        self.assertIsNotNone(clh.active_hold(self.ws, self.now + 61))

    def test_failed_switch_keeps_the_hold_and_says_so(self):
        self.switch.write_text("#!/bin/bash\necho refused >&2\nexit 9\n")
        owner_task(self.ws / "tasks", "task-5", "switch to sonnet")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=str(self.switch)), ["task-5"])
        self.assertIsNotNone(clh.active_hold(self.ws, self.now + 61))
        self.assertIn("could not", (self.ws / "results" / "task-5.txt").read_text())

    def test_switch_without_a_writer_says_so(self):
        owner_task(self.ws / "tasks", "task-7", "switch to haiku")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=None), ["task-7"])
        self.assertIsNotNone(clh.active_hold(self.ws, self.now + 61))
        self.assertIn("could not", (self.ws / "results" / "task-7.txt").read_text())

    def test_no_hold_no_action(self):
        clh.clear(self.ws, self.now + 1)
        owner_task(self.ws / "tasks", "task-6", "retry now")
        self.assertEqual(clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=None), [])


class Crash(BaseException):
    """A process death in the middle of an owner action."""


class OwnerActionCrash(unittest.TestCase):
    """A crash between the action's checkpoints: the next revive still answers once, acts
    once and launches. A repeated switch to the same model would be harmless (same write)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()
        self.calls = self.ws / "switch-calls"
        self.switch = self.ws / "switch.sh"
        self.switch.write_text(f'#!/bin/bash\necho "$@" >> {self.calls}\nexit 0\n')
        self.switch.chmod(0o755)
        self.now = 1_800_000_000.0
        self.rec = limited_record(self.now)
        clh.decide(self.ws, self.now, quota=self.rec)

    def tearDown(self):
        self.td.cleanup()

    def _crash_then_revive(self, body: str, point: str):
        from unittest import mock
        import local_record
        owner_task(self.ws / "tasks", "task-9", body)
        real_write, real_clear = local_record.write_text_whole, clh.clear

        def write_crashing(path, text):
            if Path(path).name == "task-9.txt":
                raise Crash()
            return real_write(path, text)

        def clear_crashing(*a, **k):
            raise Crash()
        real_record = clh._write_hold

        def record_crashing(ws, hold):
            if hold.get("actions"):
                raise Crash()
            return real_record(ws, hold)
        patch = {"answer": mock.patch.object(local_record, "write_text_whole", side_effect=write_crashing),
                 "clear": mock.patch.object(clh, "clear", side_effect=clear_crashing),
                 "record": mock.patch.object(clh, "_write_hold", side_effect=record_crashing)}[point]
        with patch, self.assertRaises(Crash):
            clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=str(self.switch))
        self.assertIs(clh.clear, real_clear)
        for minute in (2, 4):
            clh.handle_owner_actions(self.ws, self.now + minute * 60, switch_cmd=str(self.switch))
        answer = self.ws / "results" / "task-9.txt"
        self.assertTrue(answer.exists(), f"{point}: the owner never got an answer")
        self.assertFalse((self.ws / "tasks" / "task-9.txt").exists(), f"{point}: the core would act on it again")
        self.assertIsNone(clh.active_hold(self.ws, self.now + 300), f"{point}: the core stays held")
        self.assertTrue(clh.decide(self.ws, self.now + 301, quota=self.rec, launch_model="sonnet").launch)
        return answer.read_text()

    def test_crash_before_the_answer(self):
        for body, point in (("switch to sonnet", "answer"), ("retry now", "answer")):
            with self.subTest(body=body):
                self.td.cleanup()
                self.setUp()
                text = self._crash_then_revive(body, point)
                self.assertIn("sonnet" if "switch" in body else "Retrying", text)
                if "switch" in body:
                    self.assertEqual(len(self.calls.read_text().splitlines()), 1, "the switch ran twice")

    def test_crash_before_the_hold_is_lifted(self):
        for body in ("switch to sonnet", "retry now"):
            with self.subTest(body=body):
                self.td.cleanup()
                self.setUp()
                self._crash_then_revive(body, "clear")
                if "switch" in body:
                    self.assertEqual(len(self.calls.read_text().splitlines()), 1, "the switch ran twice")


    def test_crash_after_the_answer_before_it_is_marked(self):
        # The answer landed and the bridge delivered it, but the record never said so: the
        # replay finds the delivered copy in the archive and does not write a second one.
        from unittest import mock
        owner_task(self.ws / "tasks", "task-9", "retry now")
        real_mark = clh._mark

        def mark_crashing(ws, task, step):
            if step == "answered":
                raise Crash()
            return real_mark(ws, task, step)
        with mock.patch.object(clh, "_mark", side_effect=mark_crashing), self.assertRaises(Crash):
            clh.handle_owner_actions(self.ws, self.now + 60, switch_cmd=None)
        month = self.ws / "results" / "archive" / "2026-10"
        month.mkdir(parents=True)
        (self.ws / "results" / "task-9.txt").rename(month / "task-9.txt")
        for minute in (2, 4):
            clh.handle_owner_actions(self.ws, self.now + minute * 60, switch_cmd=None)
        self.assertFalse((self.ws / "results" / "task-9.txt").exists(), "the delivered answer was written again")
        self.assertIsNone(clh.active_hold(self.ws, self.now + 300))
        self.assertFalse((self.ws / "tasks" / "task-9.txt").exists())

    def test_crash_after_the_switch_before_the_record(self):
        # Nothing durable says the switch ran, so the next pass runs it again: the same model,
        # written to the same settings key, which is harmless. The owner still gets one answer.
        text = self._crash_then_revive("switch to sonnet", "record")
        self.assertIn("sonnet", text)
        self.assertEqual(self.calls.read_text().split(), ["sonnet", "--offline"] * 2)


class NoticeRetry(unittest.TestCase):
    """A notice whose first enqueue failed is still owed, and is sent exactly once."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def notices(self):
        return sorted((self.ws / "results").glob("proactive-*.txt"))

    def test_failed_enqueue_is_retried_once_storage_recovers(self):
        now = 1_800_000_000.0
        rec = limited_record(now)
        (self.ws / "results").write_text("storage is broken")
        self.assertFalse(clh.decide(self.ws, now, quota=rec).launch)
        (self.ws / "results").unlink()
        for minute in (2, 4, 6):
            self.assertFalse(clh.decide(self.ws, now + minute * 60, quota=rec).launch)
        self.assertEqual(len(self.notices()), 1)
        self.assertTrue(clh.read_hold(self.ws)["notice_queued"])

    def test_no_duplicates_after_success_even_once_delivered(self):
        now = 1_800_000_000.0
        rec = limited_record(now)
        clh.decide(self.ws, now, quota=rec)
        first = self.notices()
        self.assertEqual(len(first), 1)
        first[0].unlink()  # the bridge delivered it and removed the file
        for minute in (2, 4, 6):
            clh.decide(self.ws, now + minute * 60, quota=rec)
        self.assertEqual(self.notices(), [])


class AdapterEdges(unittest.TestCase):
    def _load(self, name, rel):
        spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_core_input_watch_maps_paused(self):
        ciw = self._load("ciw_limit", "src/core-input-watch.py")
        state, _detail, prompt, kind = ciw.compose_state("", "paused", gateway_alive=False)
        self.assertEqual((state, prompt, kind), ("blocked-known", None, None))

    def test_runtime_health_probe_degrades(self):
        from unittest import mock
        rh = self._load("rh_limit2", "src/runtime-health.py")
        with mock.patch.object(rh.core_limit_hold, "active_hold", side_effect=RuntimeError("x")):
            self.assertIsNone(rh._limit_hold("/nowhere"))

    def test_health_check_reads_the_hold_and_resets_observation(self):
        from unittest import mock
        hc = self._load("hc_limit2", "src/health-check.py")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            clh.decide(ws, time.time(), quota=limited_record(time.time()))
            with mock.patch.object(hc, "WORKSPACE_DIR", ws):
                self.assertEqual(hc._core_limit_hold(time.time())["status"], "held")
            with mock.patch.object(hc, "WORKSPACE_DIR", ws / "missing"):
                self.assertIsNone(hc._core_limit_hold(time.time()))
            state = Path(td) / "core-recovery.json"
            state.write_text(json.dumps({"wedge_first_seen": 1.0, "wedge_task": "t|1", "wedge_mode": "dead"}))
            r = hc.recover_core_if_wedged(
                state_file=state, now=1_800_000_000.0, alive_fn=lambda: False,
                oldest_task_fn=lambda: None, status_ts_fn=lambda: None, just_booted_fn=lambda: False,
                stopped_fn=lambda: False, restart_fn=lambda: True, sender=lambda m: True,
                limit_hold_fn=lambda: {"until": 1.0, "reason": "r"})
            self.assertEqual(r["action"], "limit-hold")
            self.assertEqual(json.loads(state.read_text())["wedge_first_seen"], 0)
        with mock.patch.dict(sys.modules, {"core_limit_hold": None}):
            self.assertIsNone(hc._core_limit_hold(time.time()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
