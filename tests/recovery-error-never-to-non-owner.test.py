#!/usr/bin/env python3
"""BEHAVIOURAL: an internal recovery error never reaches a non-owner conversation.

Incident shape: a task from the owner's DM was claimed and archived normally; a
raw error naming its local task path ("[Errno 2] No such file or directory:
'.../workspace/tasks/task-....txt'") was then posted as plain text in a team
member's Slack DM. Pins the three properties the fix owns:

  1. an archived or answered task resolves as settled, not missing
     (src/task_disposition.py);
  2. the Slack reply leg runs the shared egress guard, so a non-owner reply
     carrying that error is withheld (src/slack-bridge.py), and a proactive
     owner notice never resolves to an unmapped allowlist member
     (src/slack_owner.py); a route without a tier falls back through the one
     shared resolver, `resolve_route_tier` (src/policy/egress/result.py);
  3. no local path or raw OS error survives into guarded user-facing text
     (src/policy/egress/result.py).
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

_tmp_home = tempfile.mkdtemp(prefix="recovery-error-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _tmp_home
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
_cfg = Path(_tmp_home) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text(json.dumps({"allowFrom": ["UOWNER"]}))

for name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode",
             "slack_sdk", "slack_sdk.errors"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        if name == "slack_bolt":
            m.App = type("App", (), {"__init__": lambda self, **kw: None,
                                     "event": lambda self, *a, **k: (lambda fn: fn),
                                     "client": types.SimpleNamespace()})
        if name == "slack_bolt.adapter.socket_mode":
            m.SocketModeHandler = type("SocketModeHandler", (),
                                       {"__init__": lambda self, *a, **kw: None})
        if name == "slack_sdk.errors":
            m.SlackApiError = type("SlackApiError", (Exception,), {})
        sys.modules[name] = m


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("slack_bridge_recovery_error", REPO / "src" / "slack-bridge.py")
td_mod = _load("task_disposition", REPO / "src" / "task_disposition.py")
owner_mod = _load("slack_owner_recovery_error", REPO / "src" / "slack_owner.py")
from policy.egress import result as egress  # noqa: E402

TID = "task-recovery-error-1"
ERROR_BODY = ("Core recovery cannot read required workspace state: [Errno 2] No such file "
              "or directory: '/Users/someone/Library/Application Support/space.ag2.app/"
              "workspace/tasks/task-1792600000000.txt'")
LOCAL_MARKERS = ("/Users/", "[Errno", "workspace/tasks/", "task-1792600000000")


class _Stop(Exception):
    pass


class _Resp:
    status_code = 200
    data = {"ok": True, "ts": "1724.0001"}


class ArchivedTaskIsNotFatal(unittest.TestCase):
    def _ws(self, td):
        for d in ("tasks/archive/2026-10", "results/archive/2026-10"):
            (Path(td) / d).mkdir(parents=True)
        return Path(td)

    def test_month_archived_task_is_archived(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "tasks/archive/2026-10" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "archived")

    def test_epoch_suffixed_month_archived_result_is_answered(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "results/archive/2026-10" / f"{TID}-1792626046.txt").write_text("done")
            self.assertEqual(td_mod.task_disposition(ws, TID), "answered")
            self.assertIn(td_mod.task_disposition(ws, TID), td_mod.SETTLED)

    def test_empty_live_result_is_not_answered(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "results" / f"{TID}.txt").write_text("  \n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "missing")
            (ws / "tasks" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "pending")

    def test_empty_archived_result_is_not_answered(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "results/archive/2026-10" / f"{TID}-1792626046.txt").write_text("")
            self.assertEqual(td_mod.task_disposition(ws, TID), "missing")
            (ws / "tasks" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "pending")

    def test_ready_archived_result_behind_empty_live_one_is_answered(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "tasks" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            (ws / "results" / f"{TID}.txt").write_text("")
            (ws / "results/archive/2026-10" / f"{TID}-1792626046.txt").write_text("done")
            self.assertEqual(td_mod.task_disposition(ws, TID), "answered")

    def test_claimed_variant_is_claimed_and_absent_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            self.assertEqual(td_mod.task_disposition(ws, TID), "missing")
            claimed = ws / "tasks" / f"{TID}.txt.claimed-core"
            claimed.write_text(f"id: {TID}\ntask: hi\n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "claimed")
            claimed.unlink()
            (ws / "tasks" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            self.assertEqual(td_mod.task_disposition(ws, TID), "pending")

    def test_cli_accepts_the_notification_file_name(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            (ws / "tasks/archive" / f"{TID}.txt").write_text("id: x\ntask: hi\n")
            self.assertEqual(td_mod.main([f"{TID}.txt", "--workspace", str(ws)]), 0)
            self.assertEqual(td_mod.main(["..", "--workspace", str(ws)]), 2)
            fake = types.ModuleType("workspace_default")
            fake.resolve_workspace = lambda migrate=True: ws
            saved = sys.modules.get("workspace_default")
            sys.modules["workspace_default"] = fake
            try:
                self.assertEqual(td_mod.main([TID]), 0)
            finally:
                if saved is None:
                    sys.modules.pop("workspace_default", None)
                else:
                    sys.modules["workspace_default"] = saved


class ErrorNoticeNeverRoutedToNonOwner(unittest.TestCase):
    def _one_pass(self, td: str, tier: str, route_without_tier: bool = False, task_file: bool = True):
        results, tasks = Path(td) / "results", Path(td) / "tasks"
        (results / "archive").mkdir(parents=True)
        (tasks / "archive").mkdir(parents=True)
        (Path(td) / "state").mkdir()
        if task_file:
            (tasks / f"{TID}.txt").write_text(
                f"id: {TID}\nsource: slack\naccess_tier: {tier}\ntask: hi\n")
        (results / f"{TID}.txt").write_text(ERROR_BODY)
        calls: list[dict] = []

        def _post(**kw):
            calls.append(kw)
            return _Resp()

        sb.REPO = Path(td)
        sb.RESULTS_DIR, sb.TASKS_DIR, sb.STATE_DIR = results, tasks, Path(td) / "state"
        sb.ARCHIVE_RESULTS_DIR = results / "archive"
        sb.ARCHIVE_TASKS_DIR = tasks / "archive"
        sb.app = types.SimpleNamespace(client=types.SimpleNamespace(chat_postMessage=_post))
        sb._atomic_write_pending_replies = lambda *a, **k: None
        sb.pending_replies.clear()
        route = {"channel": "D0TEAM", "thread_ts": None, "submitted_at": sb.time.time()}
        if not route_without_tier:
            route["access_tier"] = tier
        sb.pending_replies[TID] = route

        def _sleep(_s):
            raise _Stop()

        orig_sleep = sb.time.sleep
        sb.time.sleep = _sleep
        try:
            with self.assertRaises(_Stop):
                sb.result_watcher()
        finally:
            sb.time.sleep = orig_sleep
        return calls

    def test_team_reply_carrying_internal_error_is_withheld(self):
        with tempfile.TemporaryDirectory() as td:
            calls = self._one_pass(td, "team")
            sent = " ".join(c.get("text", "") for c in calls)
            for marker in LOCAL_MARKERS:
                self.assertNotIn(marker, sent, f"{marker!r} reached a non-owner DM")

    def test_route_without_tier_rereads_the_task_file(self):
        with tempfile.TemporaryDirectory() as td:
            calls = self._one_pass(td, "team", route_without_tier=True)
            self.assertNotIn("[Errno", " ".join(c.get("text", "") for c in calls))

    def test_route_without_tier_or_task_file_guards_as_guest(self):
        with tempfile.TemporaryDirectory() as td:
            calls = self._one_pass(td, "owner", route_without_tier=True, task_file=False)
            self.assertNotIn("[Errno", " ".join(c.get("text", "") for c in calls))

    def test_owner_reply_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            calls = self._one_pass(td, "owner")
            self.assertEqual([c["text"] for c in calls], [ERROR_BODY])

    def test_proactive_owner_never_resolves_to_unmapped_member(self):
        # A tierMap exists, the team member is unmapped and listed first, no tofuOwner:
        # inbound resolves that member as "other", so proactive must not call them owner.
        access = {"allowFrom": ["UTEAM", "UOWNER"], "tierMap": {"UOWNER": "owner"}}
        self.assertEqual(owner_mod.resolve_proactive_owner_id(access), "UOWNER")
        self.assertIsNone(owner_mod.resolve_proactive_owner_id(
            {"allowFrom": ["UTEAM"], "tierMap": {}}))
        # A legacy file with no tierMap key keeps the grandfathered owner default.
        self.assertEqual(owner_mod.resolve_proactive_owner_id({"allowFrom": ["ULEGACY"]}), "ULEGACY")


class RouteTierFallbackContract(unittest.TestCase):
    """The one shared fallback for a route without a tier: task file (live, claimed,
    archived), else guest. Every bridge's reply leg delegates to it."""

    def _tasks(self, td):
        tasks = Path(td) / "tasks"
        (tasks / "archive" / "2026-10").mkdir(parents=True)
        return tasks

    def test_carried_tier_wins_without_reading_any_file(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(egress.resolve_route_tier("team", self._tasks(td), TID), "team")

    def test_unknown_tier_reads_live_claimed_and_archived_task_files(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = self._tasks(td)
            for where in (tasks / f"{TID}.txt", tasks / f"{TID}.txt.claimed-core",
                          tasks / "archive" / f"{TID}.txt",
                          tasks / "archive" / "2026-10" / f"{TID}.txt"):
                where.write_text(f"id: {TID}\naccess_tier: team\ntask: hi\n")
                for missing in ("unknown", None, ""):
                    self.assertEqual(egress.resolve_route_tier(missing, tasks, TID), "team", where)
                where.unlink()

    def test_no_task_file_anywhere_guards_as_guest(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(egress.resolve_route_tier("unknown", self._tasks(td), TID), "guest")

    def test_archived_owner_task_is_owner(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = self._tasks(td)
            (tasks / "archive" / "2026-10" / f"{TID}.txt").write_text(
                f"id: {TID}\naccess_tier: owner\ntask: hi\n")
            self.assertEqual(egress.resolve_route_tier("unknown", tasks, TID), "owner")


class SlackReplyLegDelegatesTheTierFallback(unittest.TestCase):
    def test_guard_reply_uses_the_shared_resolver(self):
        seen = []

        def _spy(tier, tasks_dir, task_id):
            seen.append((tier, tasks_dir, task_id))
            return "team"

        saved = sb.resolve_route_tier
        sb.resolve_route_tier = _spy
        try:
            with tempfile.TemporaryDirectory() as td:
                sb.REPO, sb.TASKS_DIR, sb.STATE_DIR = Path(td), Path(td) / "tasks", Path(td) / "state"
                safe = sb._guard_reply(TID, {"channel": "D0TEAM"}, ERROR_BODY)
        finally:
            sb.resolve_route_tier = saved
        self.assertEqual(seen, [(None, Path(td) / "tasks", TID)])
        self.assertNotIn("[Errno", safe, "the resolver's tier did not drive the guard")


class NoLocalPathInUserFacingText(unittest.TestCase):
    def _guard(self, body, tier="team"):
        return egress.guard_result_for_tier(body, tier, REPO, secret_filter=_no_secrets)

    def test_guarded_text_never_carries_a_local_path_or_errno(self):
        for body in (ERROR_BODY,
                     "Traceback (most recent call last):\n  File \"/home/u/x.py\"",
                     "see /private/tmp/claude-501/scratch/out.txt",
                     "cache at /var/folders/ab/T/x",
                     "[WinError 2] The system cannot find the file specified: "
                     "'C:\\Users\\bob\\ws\\tasks\\t.txt'",
                     "FileNotFoundError: No such file: /tmp/ws/tasks/task-1.txt",
                     "spooled to /var/tmp/x",
                     "open('/root/x') failed"):
            safe, reason = self._guard(body)
            self.assertEqual(reason, egress.INTERNAL_ERROR_DETAIL, body)
            for marker in ("/Users/", "/home/", "/private/", "/var/folders/", "[Errno", "Traceback",
                           "[WinError", "C:\\Users", "/tmp/", "/root/"):
                self.assertNotIn(marker, safe)

    def test_ordinary_text_and_urls_pass(self):
        for body in ("Here is the summary you asked for.",
                     "Docs: https://example.com/Users/guide and src/app/home/page.tsx",
                     "See https://example.com/tmp/notes and https://example.com/root/a"):
            self.assertEqual(self._guard(body), (body, None))

    def test_marker_value_is_not_prose(self):
        self.assertFalse(egress.carries_internal_error_detail("chart\n[file: /var/folders/ab/T/c.png]"))
        self.assertTrue(egress.carries_internal_error_detail("[file: /tmp/x.png] failed: [Errno 2]"))

    def test_owner_tier_is_exempt(self):
        self.assertEqual(self._guard(ERROR_BODY, tier="owner"), (ERROR_BODY, None))


def _no_secrets(text):
    return types.SimpleNamespace(detected=False, secret_types=[], text=text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
