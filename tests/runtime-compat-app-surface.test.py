#!/usr/bin/env python3
"""runtime_compat as the desktop app drives it: the in-app card is the consent surface.

Covers the engine side of the app's repair card: machine-readable repair
progress, consent recorded from the app, a launch that never waits or DMs when
the app is the surface, a stable `doctor --json`, the logged reason for the
chosen binary, the app's one delayed owner DM, and the time bounds that keep a
broken CLI inside the app's launch window.

Run: python3 tests/runtime-compat-app-surface.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rc_suite", REPO / "tests" / "runtime-compat.test.py")
suite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(suite)
rc = suite.rc
fake_cli, fake_npm, write_exe = suite.fake_cli, suite.fake_npm, suite.write_exe
CLAUDE_FLAGS, PLENTY = suite.CLAUDE_FLAGS, suite.PLENTY
APP = {"SUTANDO_RUNTIME_CONSENT_SURFACE": "app"}
MXID = "@owner:example.org"


class AppBase(suite.OwnerReplyBase):
    def setUp(self):
        super().setUp()
        self._ws_fn, self._du = rc._workspace, rc.shutil.disk_usage
        rc._workspace = lambda: self.ws
        rc.shutil.disk_usage = lambda _p: PLENTY

    def tearDown(self):
        rc._workspace, rc.shutil.disk_usage = self._ws_fn, self._du
        super().tearDown()

    def main(self, *argv, **env):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(argv), env=dict(self.env, **env), consent=lambda _q: False)
        return code, out.getvalue(), err.getvalue()

    def json_lines(self, out):
        return [json.loads(ln) for ln in out.splitlines() if ln.strip()]

    def doctor(self, runtime="claude", **env):
        code, out, err = self.main("doctor", "--runtime", runtime, "--json", **env)
        return code, json.loads(out)[0]

    def broken_then_npm(self, version="2.3.0"):
        fake_cli(self.bin1 / "claude", version_rc=1)
        suite.AssistedRepair.good_npm(self, version)


class RepairJson(AppBase):
    """E1-a: `repair --json` streams one JSON line per phase, then one result line."""

    def test_repair_json_streams_phases_then_one_result_line(self):
        self.broken_then_npm()
        code, out, err = self.main("repair", "--runtime", "claude", "--yes", "--json")
        self.assertEqual(code, 0, err)
        lines = self.json_lines(out)
        phases = [ln["phase"] for ln in lines if ln["type"] == "progress"]
        self.assertEqual(phases, ["checking-disk", "downloading", "verifying", "switching", "done"])
        down = next(ln for ln in lines if ln.get("phase") == "downloading")
        self.assertEqual((down["attempt"], down["attempts"]), (1, 3))
        result = lines[-1]
        self.assertEqual(result["type"], "result")
        self.assertEqual({k: result[k] for k in ("ok", "reason", "attempts", "version")},
                         {"ok": True, "reason": "installed", "attempts": 1, "version": "2.3.0"})
        self.assertEqual(result["path"], str(self.root / "bin" / "claude"))
        self.assertEqual(sorted(result), sorted(["type", "ok", "reason", "detail", "user_action",
                                                 "attempts", "path", "version"]))
        self.assertIn("runtime-compat: installed", err)

    def test_repair_json_failure_counts_every_attempt(self):
        fake_npm(self.bin1 / "npm", 'echo "npm ERR! code ENOTFOUND" >&2; exit 1\n')
        code, out, _ = self.main("repair", "--runtime", "claude", "--yes", "--json")
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        lines = self.json_lines(out)
        self.assertEqual([ln["attempt"] for ln in lines if ln.get("phase") == "downloading"], [1, 2, 3])
        self.assertEqual([ln.get("phase") for ln in lines[-2:]], ["done", None])
        self.assertEqual((lines[-1]["ok"], lines[-1]["reason"], lines[-1]["attempts"]),
                         (False, "blocked-download", 3))

    def test_repair_without_json_keeps_stdout_empty(self):
        self.broken_then_npm()
        code, out, _ = self.main("repair", "--runtime", "claude", "--yes")
        self.assertEqual((code, out), (0, ""))


class RepairPhases(AppBase):
    """One declared phase set: what `repair --json` emits, in order, with `done` last.
    `done` is a stdout progress line only; no on-disk record treats it as finished."""

    def phases(self, *extra):
        code, out, _ = self.main("repair", "--runtime", "claude", "--yes", "--json", *extra)
        lines = self.json_lines(out)
        self.assertEqual(lines[-1]["type"], "result")
        return code, [ln["phase"] for ln in lines[:-1]]

    def assert_declared_order(self, phases):
        self.assertTrue(set(phases) <= set(rc.REPAIR_PHASES), phases)
        order = [rc.REPAIR_PHASES.index(p) for p in phases]
        self.assertEqual(order, sorted(order), phases)
        self.assertEqual((phases.count("done"), phases[-1]), (1, "done"))

    def test_the_phase_set_is_declared_once(self):
        self.assertEqual(rc.REPAIR_PHASES,
                         ("checking-disk", "downloading", "verifying", "switching", "done"))

    def test_success_and_failure_runs_emit_only_declared_phases_in_order(self):
        self.broken_then_npm()
        code, phases = self.phases()
        self.assertEqual(code, 0)
        self.assert_declared_order(phases)
        fake_npm(self.bin1 / "npm", 'echo "npm ERR! code ENOTFOUND" >&2; exit 1\n')
        code, phases = self.phases()
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assert_declared_order(phases)
        self.assertNotIn("switching", phases)

    def test_an_undeclared_phase_is_refused(self):
        self.broken_then_npm()
        real = rc.assisted_install

        def rogue(*a, progress=None, **k):
            progress("resuming")
        rc.assisted_install = rogue
        try:
            with self.assertRaises(ValueError):
                self.main("repair", "--runtime", "claude", "--yes", "--json")
        finally:
            rc.assisted_install = real

    def test_done_is_never_written_to_a_record(self):
        self.broken_then_npm()
        self.main("repair", "--runtime", "claude", "--yes", "--json", "--consent-source", "app",
                  "--consent-by", MXID)
        records = [p for p in list(self.root.rglob("*.json")) + list(self.ws.rglob("*.json"))]
        for path in records:
            self.assertNotIn('"done"', path.read_text(), path)

    def test_a_swap_journal_marked_done_is_rolled_back_not_finished(self):
        old = self.managed_version("2.1.0")
        new = suite.fake_cli(self.root / "claude" / "versions" / "2.3.0" / "bin" / "claude", "2.3.0")
        rc._swap_link(self.root / "bin" / "claude", Path(new))
        journal = self.root / "claude" / ".swap-999999-1.json"
        journal.write_text(json.dumps({"phase": "done", "previous": str(old), "final": None,
                                       "aside": None, "placed": False, "new_ino": None}))
        rc.recover_swaps(self.root, "claude", pid_alive=lambda _p: False)
        self.assertEqual(os.readlink(self.root / "bin" / "claude"), str(old))
        self.assertFalse(journal.exists())


class AppConsent(AppBase):
    """E1-b: consent given in the app is recorded before the repair acts, and closes the incident."""

    def app_repair(self, *extra):
        return self.main("repair", "--runtime", "claude", "--yes", "--json",
                         "--consent-source", "app", "--consent-by", MXID, *extra)

    def test_app_consent_is_recorded_before_the_repair_runs(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        seen = self.tmp / "seen.json"
        record = self.ws / "state" / "runtime-compat-claude.json"
        fake_npm(self.bin1 / "npm", f'cat "{record}" > "{seen}"; exit 1\n')
        _, plan = self.doctor()
        self.app_repair("--plan-hash", plan["plan_hash"])
        consent = json.loads(seen.read_text())["consent"]
        self.assertEqual({k: consent[k] for k in ("source", "by", "runtime", "plan_hash")},
                         {"source": "app", "by": MXID, "runtime": "claude",
                          "plan_hash": plan["plan_hash"]})
        self.assertIsInstance(consent["at"], int)
        self.assertEqual(self.incident()["status"], "open")

    def test_app_consent_closes_the_incident_and_answers_the_open_dm(self):
        self.broken_then_npm()
        self.ensure()
        self.assertEqual(len(self.notices()), 1)
        pending = self.reply("install compatible claude", name="task-200")
        _, plan = self.doctor()
        self.assertEqual(self.app_repair("--plan-hash", plan["plan_hash"])[0], 0)
        inc = self.incident()
        self.assertEqual((inc["status"], inc["resolved_by"]), ("resolved", "app"))
        fixed = [p for p in self.notices() if p.name != inc["notice"]]
        self.assertEqual(len(fixed), 1)
        self.assertIn("fixed from the app", fixed[0].read_text().lower())
        self.assertIn("already fixed from the app", (self.ws / "results" / pending.name).read_text().lower())
        self.assertFalse(pending.exists())

    def test_dm_reply_after_app_repair_reads_as_already_fixed(self):
        self.broken_then_npm()
        self.ensure()
        _, plan = self.doctor()
        self.assertEqual(self.app_repair("--plan-hash", plan["plan_hash"])[0], 0)
        late = self.reply("Install compatible claude.", name="task-300")
        code, path = self.ensure(repair_fn=self.counting_repair)
        self.assertEqual((code, path, self.repairs), (0, str(self.root / "bin" / "claude"), 0))
        self.assertIn("already fixed from the app", (self.ws / "results" / late.name).read_text().lower())
        self.assertFalse(late.exists())

    def test_changed_plan_refuses_the_consent(self):
        self.broken_then_npm()
        code, out, err = self.app_repair("--plan-hash", "0" * 64)
        self.assertEqual(code, rc.EXIT_USAGE)
        self.assertEqual(self.json_lines(out)[-1]["reason"], "plan-changed")
        self.assertFalse((self.root / "bin" / "claude").exists())

    def test_app_consent_without_a_plan_hash_is_refused(self):
        self.broken_then_npm()
        code, out, err = self.app_repair()
        self.assertEqual(code, rc.EXIT_USAGE)
        self.assertEqual(self.json_lines(out)[-1]["reason"], "plan-hash-required")
        self.assertFalse((self.root / "bin" / "claude").exists())

    def test_missing_workspace_refuses_before_installing(self):
        self.broken_then_npm()
        _, plan = self.doctor()
        real_ws = rc._workspace
        rc._workspace = lambda: None
        try:
            code, out, err = self.app_repair("--plan-hash", plan["plan_hash"])
        finally:
            rc._workspace = real_ws
        self.assertEqual(code, rc.EXIT_USAGE)
        self.assertEqual(self.json_lines(out)[-1]["reason"], "workspace-unavailable")
        self.assertFalse((self.root / "bin" / "claude").exists())


class ConsentSurfaceApp(AppBase):
    """E1-c: an app-launched core never waits and never DMs; the app shows the card."""

    def ensure_in_child(self, *argv, limit=20):
        """`main` in a child process, so a launch that still waits fails at `limit` instead of hanging."""
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                "import runtime_compat as rc; rc._workspace = lambda: Path(sys.argv[2]); "
                "sys.exit(rc.main(sys.argv[3:]))")
        start = time.monotonic()
        try:
            r = subprocess.run([sys.executable, "-c", code, str(REPO / "src"), str(self.ws), *argv],
                               env=dict(self.env, SUTANDO_RUNTIME_SYSTEM_DIRS="", **APP), capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=limit)
        except subprocess.TimeoutExpired:
            self.fail(f"the launch was still waiting after {limit}s")
        return r.returncode, r.stderr, time.monotonic() - start

    def test_app_surface_never_waits_or_dms(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        ran = self.tmp / "npm-ran"
        fake_npm(self.bin1 / "npm", f'touch "{ran}"; exit 1\n')
        code, err, _ = self.ensure_in_child("ensure", "--runtime", "claude", "--wait", "600",
                                            "--repair", "--notify-owner")
        self.assertEqual(code, rc.EXIT_UNUSABLE, err)
        self.assertEqual(self.notices(), [])
        self.assertNotIn("waiting up to", err)
        self.assertFalse(ran.exists(), "no consent was given, so no installer may run")
        self.assertFalse(os.path.lexists(self.root / "bin" / "claude"))

    def test_app_surface_logged_out_returns_4_at_once(self):
        fake_cli(self.bin1 / "codex", "codex-cli 0.160.1", suite.CODEX_FLAGS, login_rc=1, label="")
        code, err, _ = self.ensure_in_child("ensure", "--runtime", "codex", "--wait", "600",
                                            "--enforce-auth", "--notify-owner")
        self.assertEqual((code, self.notices()), (rc.EXIT_LOGIN, []), err)


class DoctorJsonSchema(AppBase):
    """E1-d: the document the desktop parses. A change to its shape breaks the app."""

    def test_doctor_json_schema_1_golden(self):
        p = fake_cli(self.bin1 / "claude", "2.1.0")
        self.env["CLAUDE_CONFIG_DIR"] = ""
        code, doc = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(doc), ["auth", "candidates", "interrupted_updates", "plan",
                                       "plan_hash", "runtime", "schema", "selected",
                                       "selected_reason", "status"])
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(sorted(doc["plan"]), ["assisted", "assisted_touches", "command",
                                               "consent_required", "reason", "status", "touches",
                                               "verify"])
        self.assertEqual(sorted(doc["selected"]), ["detail", "missing_flags", "path", "source",
                                                   "status", "version"])
        for cand in doc["candidates"]:
            self.assertEqual(sorted(cand), ["detail", "missing_flags", "path", "source", "status",
                                            "version"])
        self.assertEqual(sorted(doc["auth"]), ["detail", "login", "remedy"])
        self.assertEqual(doc["auth"]["login"], "unknown")
        self.assertEqual(doc["selected_reason"], f"chose {p} (PATH) 2.1.0; it passes doctor")
        self.assertRegex(doc["plan_hash"], r"^[0-9a-f]{64}$")

    def test_plan_hash_is_stable_and_follows_the_plan(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        h1 = self.doctor()[1]["plan_hash"]
        self.assertEqual(self.doctor()[1]["plan_hash"], h1)
        fake_cli(self.bin1 / "claude", "1.0.0")
        self.assertNotEqual(self.doctor()[1]["plan_hash"], h1)

    def test_codex_auth_block(self):
        fake_cli(self.bin1 / "codex", "codex-cli 0.160.1", suite.CODEX_FLAGS, login_rc=1, label="")
        _, doc = self.doctor("codex")
        self.assertEqual(doc["auth"]["login"], "logged-out")
        self.assertIn("codex", doc["auth"]["remedy"])
        fake_cli(self.bin1 / "codex", "codex-cli 0.160.1", suite.CODEX_FLAGS, label="")
        self.assertEqual(self.doctor("codex")[1]["auth"], {"login": "ok", "remedy": "",
                                                          "detail": "logged in"})

    def test_every_candidate_entry_has_the_same_keys(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        fake_cli(self.bin2 / "claude", "1.0.0")
        self.managed_version("2.1.0")
        _, doc = self.doctor()
        self.assertEqual(len(doc["candidates"]), 3)
        for cand in doc["candidates"]:
            self.assertEqual(sorted(cand), ["detail", "missing_flags", "path", "source", "status",
                                            "version"])
        self.assertEqual([c["status"] for c in doc["candidates"]], ["broken", "too-old", "ok"])
        self.assertIn(doc["auth"]["login"], rc.AUTH_LOGIN_VALUES)
        self.assertEqual(rc.AUTH_LOGIN_VALUES, ("ok", "logged-out", "unknown"))

    def test_nothing_selected_has_no_auth(self):
        _, doc = self.doctor()
        self.assertEqual(doc["auth"], {"login": "unknown", "remedy": "", "detail": "no usable binary"})


class PreferManaged(AppBase):
    """D-2: the managed binary wins only over a PATH binary that fails doctor, is known-bad,
    or is absent; every launch says which binary it chose and why."""

    def test_managed_preferred_when_path_binary_is_broken(self):
        bad = fake_cli(self.bin1 / "claude", version_rc=1)
        self.managed_version("2.1.0")
        r = self.discover()
        self.assertEqual(r.selected.source, "managed")
        self.assertEqual(r.selected_reason,
                         f"chose {self.root / 'bin' / 'claude'} (managed) 2.1.0 because {bad} "
                         f"(PATH) is broken: --version exited 1: 2.1.0 (Claude Code)")

    def test_managed_preferred_when_path_binary_is_known_bad(self):
        spec = rc.RuntimeSpec(**dict(rc.RUNTIMES["claude"].__dict__, known_bad=((2, 1, 5),)))
        bad = fake_cli(self.bin1 / "claude", "2.1.5")
        self.managed_version("2.1.0")
        r = self.discover(spec=spec)
        self.assertEqual(r.selected.source, "managed")
        self.assertIn(f"because {bad} (PATH) is known-bad", r.selected_reason)

    def test_managed_preferred_when_path_binary_is_too_old(self):
        bad = fake_cli(self.bin1 / "claude", "1.0.99")
        self.managed_version("2.1.0")
        r = self.discover()
        self.assertEqual(r.selected.source, "managed")
        self.assertIn(f"because {bad} (PATH) is too-old: 1.0.99 is older than the minimum 2.0.0",
                      r.selected_reason)

    def test_managed_preferred_when_path_binary_is_incompatible(self):
        bad = fake_cli(self.bin1 / "claude", "2.1.0", flags="--name")
        self.managed_version("2.1.0")
        r = self.discover()
        self.assertEqual(r.selected.source, "managed")
        self.assertIn(f"because {bad} (PATH) is incompatible: 2.1.0 lacks ", r.selected_reason)

    def test_managed_preferred_when_path_binary_is_absent(self):
        self.managed_version("2.1.0")
        r = self.discover()
        self.assertEqual(r.selected_reason,
                         f"chose {self.root / 'bin' / 'claude'} (managed) 2.1.0 because "
                         "no claude is on PATH")

    def test_older_path_binary_that_passes_doctor_is_kept_over_a_newer_managed(self):
        p = fake_cli(self.bin1 / "claude", "2.0.1")
        self.managed_version("2.5.0")
        r = self.discover()
        self.assertEqual(r.selected.path, p)
        self.assertEqual(r.selected_reason, f"chose {p} (PATH) 2.0.1; it passes doctor")

    def test_newer_untested_path_binary_is_kept(self):
        p = fake_cli(self.bin1 / "claude", "3.0.0")
        self.managed_version("2.1.0")
        r = self.discover()
        self.assertEqual((r.selected.path, r.status), (p, rc.NEWER_UNTESTED))
        self.assertTrue(r.selected_reason.startswith(f"chose {p} (PATH) 3.0.0; it passes doctor"))

    def test_nothing_usable_says_so(self):
        bad = fake_cli(self.bin1 / "claude", "1.0.0")
        self.assertEqual(self.discover().selected_reason,
                         f"no usable claude: {bad} (PATH) is too-old: 1.0.0 is older than the "
                         "minimum 2.0.0")

    def test_every_launch_logs_the_chosen_binary_and_reason(self):
        bad = fake_cli(self.bin1 / "claude", version_rc=1)
        self.managed_version("2.1.0")
        managed = self.root / "bin" / "claude"
        want = f"runtime-compat: chose {managed} (managed) 2.1.0 because {bad} (PATH) is broken"
        for argv in (("ensure", "--runtime", "claude"), ("which", "--runtime", "claude")):
            code, out, err = self.main(*argv)
            self.assertEqual((code, out.strip()), (0, str(managed)))
            self.assertIn(want, err, argv)


class NotifyOwner(AppBase):
    """D-3: the app's delayed DM goes through the incident's one-notice rule."""

    def notify(self, **env):
        code, out, _ = self.main("notify-owner", "--runtime", "claude", "--json", **env)
        return code, json.loads(out)

    def test_notify_owner_queues_once_per_incident(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.assertEqual(self.notify(**APP), (0, {"queued": True, "reason": "queued"}))
        self.assertEqual(self.notify(**APP), (0, {"queued": False, "reason": "already-sent"}))
        self.assertEqual(len(self.notices()), 1)
        self.assertIn('Reply "install compatible claude"', self.notices()[0].read_text())

    def test_notify_owner_after_resolution_sends_nothing(self):
        self.broken_then_npm()
        _, plan = self.doctor()
        self.main("repair", "--runtime", "claude", "--yes", "--consent-source", "app",
                  "--consent-by", MXID, "--plan-hash", plan["plan_hash"])
        before = self.notices()
        self.assertEqual(self.notify(**APP), (0, {"queued": False, "reason": "usable"}))
        self.assertEqual(self.notices(), before)

    def test_a_new_incident_can_dm_again(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        self.notify(**APP)
        rc.Incident(self.ws, "claude").close()
        time.sleep(1.1)  # a notice is named by the second its incident opened
        self.assertTrue(self.notify(**APP)[1]["queued"])
        self.assertEqual(len(self.notices()), 2)


class Timeouts(AppBase):
    """R-2: under the app surface a broken CLI is classified inside the app's launch window."""

    def clocked_run(self, hang_paths, clock, timeouts):
        def run(argv, timeout=rc.PROBE_TIMEOUT_S, **_kw):
            timeouts.append(timeout)
            if argv[0] in hang_paths:
                clock[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            return rc._run(argv, timeout=timeout, **_kw)
        return run

    def test_app_surface_probe_timeout_is_10s_per_binary(self):
        fake_cli(self.bin1 / "claude")
        timeouts, clock = [], [0.0]
        self.env.update(APP)
        self.discover(run=self.clocked_run((), clock, timeouts), now=lambda: clock[0])
        self.assertTrue(timeouts and max(timeouts) <= rc.APP_PROBE_TIMEOUT_S, timeouts)
        self.assertEqual(rc.APP_PROBE_TIMEOUT_S, 10)

    def test_discovery_deadline_reports_unprobed_candidates_as_unusable(self):
        dirs = [self.tmp / f"d{i}" for i in range(8)]
        hung = {fake_cli(d / "claude") for d in dirs[:7]}
        good = fake_cli(dirs[7] / "claude")
        self.env["PATH"] = ":".join(str(d) for d in dirs)
        self.env.update(APP)
        timeouts, clock = [], [0.0]
        r = self.discover(run=self.clocked_run(hung, clock, timeouts), now=lambda: clock[0])
        self.assertIsNone(r.selected)
        self.assertLessEqual(clock[0], rc.APP_DISCOVERY_CAP_S)
        unprobed = [c for c in r.candidates if c.status == rc.UNPROBED]
        self.assertIn(good, [c.path for c in unprobed])
        self.assertTrue(all(c.detail == "not probed in time" for c in unprobed))
        self.assertEqual(rc.APP_DISCOVERY_CAP_S, 60)

    def test_without_the_app_surface_probes_keep_the_30s_timeout(self):
        fake_cli(self.bin1 / "claude")
        timeouts, clock = [], [0.0]
        self.discover(run=self.clocked_run((), clock, timeouts), now=lambda: clock[0])
        self.assertEqual(set(timeouts), {rc.PROBE_TIMEOUT_S})

    def test_a_hanging_cli_is_unusable_within_the_cap_for_real(self):
        write_exe(self.bin1 / "claude", "exec sleep 40\n")
        start = time.monotonic()
        code, _, err = self.main("which", "--runtime", "claude", **APP)
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertLess(time.monotonic() - start, rc.APP_PROBE_TIMEOUT_S + 8, err)


class AppRepairBudget(AppBase):
    """R-3: from the click, an app-surface repair gives up a blocked download within its budget."""

    def test_app_surface_blocked_download_gives_up_within_budget(self):
        fake_npm(self.bin1 / "npm", 'echo "npm ERR! code ENOTFOUND" >&2; exit 1\n')
        clock, events = [0.0], []
        real = rc._run

        def slow_run(argv, **kw):
            clock[0] += 20
            return real(argv, **kw)

        def sleep(s):
            self.sleeps.append(s)
            clock[0] += s
        r = rc.assisted_install("claude", env=dict(self.env, **APP), run=slow_run, sleep=sleep,
                                disk_usage=lambda _p: PLENTY, monotonic=lambda: clock[0],
                                progress=lambda phase, **kw: events.append((phase, kw)))
        self.assertEqual(r.reason, "blocked-download")
        self.assertLessEqual(sum(self.sleeps), rc.APP_RETRY_BUDGET_S)
        self.assertLess(r.attempts, 3)
        self.assertEqual([kw["attempt"] for p, kw in events if p == "downloading"],
                         list(range(1, r.attempts + 1)))
        self.assertEqual(rc.APP_RETRY_BUDGET_S, 30)

    def budget_run(self, fail_at, sleep_by, backoff_s):
        """A fake installer that fails at clock `fail_at`; each backoff sleeps `sleep_by(s)`."""
        fake_npm(self.bin1 / "npm", "exit 1\n")
        clock, starts, events = [0.0], [], []

        def run(argv, **_kw):
            starts.append(clock[0])
            clock[0] = max(clock[0], fail_at)
            return subprocess.CompletedProcess(argv, 1, "", "npm ERR! code ENOTFOUND")

        def sleep(s):
            self.sleeps.append(s)
            clock[0] += sleep_by(s)
        r = rc.assisted_install("claude", env=dict(self.env, **APP), run=run, sleep=sleep,
                                disk_usage=lambda _p: PLENTY, monotonic=lambda: clock[0],
                                backoff_s=backoff_s,
                                progress=lambda phase, **kw: events.append((phase, kw)))
        return r, starts, events

    def assert_nothing_started_after_the_budget(self, r, starts, events):
        self.assertTrue(all(t < rc.APP_RETRY_BUDGET_S for t in starts), starts)
        self.assertEqual((r.ok, r.reason, r.attempts), (False, "blocked-download", len(starts)))
        self.assertEqual([kw["attempt"] for p, kw in events if p == "downloading"],
                         list(range(1, len(starts) + 1)))

    def test_no_attempt_starts_at_the_exact_deadline(self):
        r, starts, events = self.budget_run(fail_at=29, sleep_by=lambda s: s, backoff_s=5)
        self.assertEqual(self.sleeps, [1])
        self.assertEqual(starts, [0.0])
        self.assert_nothing_started_after_the_budget(r, starts, events)

    def test_an_overslept_backoff_does_not_start_another_attempt(self):
        for fail_at, backoff in ((29, 5), (1, 40)):
            self.sleeps.clear()
            r, starts, events = self.budget_run(fail_at=fail_at, sleep_by=lambda s: s + 0.01,
                                                backoff_s=backoff)
            self.assertEqual(starts, [0.0], (fail_at, backoff))
            self.assert_nothing_started_after_the_budget(r, starts, events)

    def test_app_surface_turns_off_npm_own_fetch_retries(self):
        seen = self.tmp / "env.txt"
        fake_npm(self.bin1 / "npm", f'echo "$npm_config_fetch_retries" > "{seen}"; exit 1\n')
        rc.assisted_install("claude", env=dict(self.env, **APP), sleep=self.sleeps.append,
                            disk_usage=lambda _p: PLENTY, attempts=1)
        self.assertEqual(seen.read_text().strip(), "0")

    def test_missing_cli_plans_npm_install_via_bundled_node(self):
        node_bin = self.tmp / "engine" / "runtime" / "node" / "bin"
        fake_npm(node_bin / "npm", "exit 0\n")
        self.env["PATH"] = f"{node_bin}:/usr/bin:/bin"
        code, doc = self.doctor()
        self.assertEqual((code, doc["status"], doc["plan"]["status"]), (rc.EXIT_UNUSABLE, "missing", "missing"))
        self.assertIsNotNone(doc["plan"]["assisted"])
        self.assertIn(str(self.root), doc["plan"]["assisted_touches"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
