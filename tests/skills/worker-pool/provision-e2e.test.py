#!/usr/bin/env python3
"""`provision_e2e.py`: the end-to-end check of worker provisioning, run against an in-process fake of
the hosted backend's contract.

- A full run passes every check, retires what it created and prints no secret.
- Each rule the fake can switch off makes exactly the check for it fail, so a pass means something.
- The leak scan finds a secret planted in a log, a transcript or a tmux pane, finds nothing in a clean
  tree, and says so when part of what it should cover could not be read.

Run: python3 tests/skills/worker-pool/provision-e2e.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

ROOT = Path(__file__).resolve().parents[3]

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
import unittest.mock

_spec = importlib.util.spec_from_file_location("connections_contract", ROOT / "tests" / "connections.test.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)  # fakes the resolver, as that suite does
C = H.C
sys.path.insert(0, str(ROOT / "skills" / "worker-pool" / "scripts"))
import pool_provision as pp
import provision_e2e as e2e
from _helpers import provision_fake

W = "7c700e11" + "0" * 24
CORE = "@owner-desk.agent:alpha-fixture.net"
WORKER_AGENT = "@owner-desk-reviewer.agent:alpha-fixture.net"
SECOND_AGENT = "@owner-desk-reviewer-x.agent:alpha-fixture.net"
BASE_URL = "https://chat.alpha-fixture.net"
ROOM = "!room:alpha-fixture.net"
CORE_SECRET = "c0de" * 12
CLAIM_KEY = "AG2_PROVISION_CLAIM_" + W.upper()
WORKER_REF = "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET_7C700E11"
TMUX = [("core", "sock", "sutando-core"), ("worker", "sock", "sutando-worker-x")]


class Base(H.Base):
    mode = "approval"

    def setUp(self):
        super().setUp()
        self.vault = provision_fake.recording(H.FakeVault)()
        self.backend = provision_fake.FakeBackend(mode=self.mode)
        self.backend.auto_approve = True
        self.backend.add_agent(CORE, CORE_SECRET)
        self.backend.rooms[ROOM] = []
        self.backend.members[ROOM] = {CORE, WORKER_AGENT}
        self.backend.responders.add(WORKER_AGENT)
        self.add(f"{BASE_URL}/relay|{CORE_SECRET}", fetch=H.agents(CORE))
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "Reviewer"}}, "bindings": {}}))
        self.transcripts = self.ws / ".claude-sutando" / "projects"
        for folder in (self.ws / "logs", self.ws / "results", self.ws / "tasks", self.transcripts / "slug"):
            folder.mkdir(parents=True, exist_ok=True)
        (self.ws / "logs" / "bridge.log").write_text("poll ok\n")
        (self.transcripts / "slug" / "session.jsonl").write_text('{"role": "assistant", "text": "done"}\n')
        self.preview = provision_fake.preview_through(self.backend, C)
        self.panes = {"core": b"$ pool_identity provision-begin\n", "worker": b"ready\n"}
        self.home = self.ws.parent / (self.ws.name + "-home")
        self.home.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home, True)
        for patch in (unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0),
                      unittest.mock.patch.object(e2e.Path, "home", lambda: self.home),
                      unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.ws / ".claude-sutando")})):
            patch.start()
            self.addCleanup(patch.stop)
        self.said = []

    def tmux(self, socket, session):
        return self.panes["core" if session == "sutando-core" else "worker"]

    def go(self, *flags, request=None, preview=None, tmux=None, targets=TMUX, start=False):
        """Run the check; ({check: (result, detail)}, exit code, everything printed)."""
        args = e2e.build_parser().parse_args(["--base-url", BASE_URL, "--worker", "Reviewer", "--workspace",
                                              str(self.ws), "--room", ROOM, *([] if start else ["--no-start"]),
                                              *flags])
        printed = []
        rc = e2e.run(args, request=request or self.backend.request, vault=self.vault, preview=preview or self.preview,
                     sleep=self.backend.sleep, clock=self.backend.clock, tmux=tmux or self.tmux, tmux_targets=targets,
                     out=printed.append, say=self.said.append)
        text = "\n".join(printed)
        self.assertClean(text + "\n".join(self.said))
        rows = {}
        for line in text.splitlines():
            for name in e2e.CHECKS:
                if line.startswith(name + " "):
                    status, _, detail = line[len(name):].strip().partition(" ")
                    rows[name] = (status, detail.strip())
        self.assertEqual(sorted(rows), sorted(e2e.CHECKS), "every check has a row")
        return rows, rc, text

    def assertClean(self, text):
        for value in self.vault.ever + [a["bearer"] for a in self.backend.agents.values() if a["bearer"]] \
                + list(self.backend.access):
            self.assertNotIn(C.secret_of(value) or value, text)

    def results(self, rows):
        return {name: status for name, (status, _) in rows.items()}


class FullRun(Base):
    def test_every_check_passes_and_nothing_is_left_behind(self):
        rows, rc, text = self.go()
        self.assertEqual(self.results(rows), {name: e2e.PASS for name in e2e.CHECKS}, text)
        self.assertEqual(rc, 0)
        self.assertIn("18 PASS, 0 FAIL, 0 SKIP, 0 INCOMPLETE", text)
        self.assertEqual(rows[e2e.CREATE][1], "held for the owner's approval")
        self.assertEqual(rows[e2e.NAMING][1], WORKER_AGENT)
        self.assertIn("authority is approval", rows[e2e.PROVENANCE][1])
        self.assertEqual(rows[e2e.WRONG_SECRET][1], "refused (http 403 wrong_secret)")
        self.assertEqual(rows[e2e.NO_CREDENTIAL][1], "refused (http 401 invalid_bearer)")
        self.assertEqual(rows[e2e.SECOND][1], "refused (http 409 claim_redeemed)")
        self.assertEqual(rows[e2e.OTHER_CALLER][1], "refused (http 404 claim_not_found)")
        self.assertEqual(rows[e2e.EXPIRED][1], "refused (http 410 claim_expired)")
        self.assertEqual(rows[e2e.RETIRED][1], "whoami answered http 401")
        self.assertEqual(rows[e2e.LEAKS][1], "0 hits for 4 secrets in 5 files, 2 tmux sessions")
        self.assertTrue(self.backend.agents[WORKER_AGENT]["deleted"] and self.backend.agents[SECOND_AGENT]["deleted"])
        self.assertEqual(sorted(self.vault.store), ["AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"], "only the core's secret")
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNotIn("left on the server", text)
        self.assertTrue(any("for the owner to approve agent.create" in line for line in self.said))
        self.assertTrue(any("for the second claim to expire" in line for line in self.said))

    def test_one_agent_only_skips_the_checks_that_need_a_second_claim(self):
        rows, rc, text = self.go("--single-claim")
        got = self.results(rows)
        self.assertEqual({got[e2e.OTHER_CALLER], got[e2e.EXPIRED], got[e2e.CLEANUP]}, {e2e.SKIP})
        self.assertEqual([n for n, s in got.items() if s not in (e2e.PASS, e2e.SKIP)], [], text)
        self.assertEqual(rc, 3, "a run with skipped checks is not a clean pass")
        self.assertNotIn(SECOND_AGENT, self.backend.agents)
        self.assertEqual(rows[e2e.SECOND][1], "refused (http 409 claim_redeemed)", "asked again after the window")

    def test_a_server_that_answers_in_text_and_names_no_expiry_still_passes(self):
        self.backend.bugs |= {"text_only", "no_expires_at"}
        rows, rc, text = self.go()
        self.assertEqual((rc, set(self.results(rows).values())), (0, {e2e.PASS}), text)

    def test_the_lane_is_started_and_a_lane_that_does_not_start_is_noted(self):
        started = []
        with unittest.mock.patch.object(C, "start_added", lambda row, **kw: started.append(kw) or {"ok": True}):
            _, rc, text = self.go("--repo", str(self.ws), "--logs-dir", str(self.ws / "logs"), start=True)
        self.assertEqual((rc, started[0]["repo"], started[0]["workspace"]), (0, self.ws, self.ws))
        self.assertNotIn("lane did not start", text)
        self.setUp()
        with unittest.mock.patch.object(C, "start_added", lambda row, **kw: started.append(kw) or
                                        {"ok": False, "reason": "no python"}):
            _, rc, text = self.go("--single-claim", start=True)
        self.assertIn("the worker's lane did not start: no python", text)
        self.assertEqual((started[1]["repo"], started[1]["logs_dir"]), (ROOT, self.ws / "logs"))


class Gate(Base):
    def test_without_the_approval_path_create_fails_closed_and_the_rest_is_skipped(self):
        self.backend.mode = "none"
        rows, rc, text = self.go()
        got = self.results(rows)
        self.assertEqual(got[e2e.CREATE], e2e.PASS)
        self.assertIn("fails closed with approval_required: this server cannot ask the owner yet", rows[e2e.CREATE][1])
        skipped = [n for n in e2e.CHECKS if n not in (e2e.CREATE, e2e.CONTROL, e2e.LEAKS)]
        self.assertEqual({got[n] for n in skipped}, {e2e.SKIP})
        self.assertEqual((rc, got[e2e.CONTROL], got[e2e.LEAKS]), (3, e2e.PASS, e2e.PASS))
        self.assertNotIn(CLAIM_KEY, self.vault.store, "the pending secret is forgotten")
        self.assertEqual([a for a in self.backend.agents], [CORE], "nothing was created")

    def test_under_a_grant_create_runs_at_once(self):
        self.backend.mode = "grant"
        rows, rc, text = self.go()
        self.assertEqual((rc, rows[e2e.CREATE]), (0, (e2e.PASS, "ran at once under a delegation grant")), text)
        self.assertIn("authority is delegation", rows[e2e.PROVENANCE][1])

    def test_a_held_call_answered_as_an_error_and_a_retire_that_must_be_called_again_still_pass(self):
        self.backend.bugs |= {"held_as_error", "soft_delete_incomplete_once"}
        rows, rc, text = self.go()
        self.assertEqual((rc, set(self.results(rows).values())), (0, {e2e.PASS}), text)

    def test_a_retire_that_does_not_say_the_bearer_was_revoked_is_not_a_retirement(self):
        real = self.backend._run

        def unrevoked(op, source, under):
            real(op, source, under)
            if op["action"] == "agent.retire":
                op["result"]["bearer_revoked"] = False
        self.backend._run = unrevoked
        rows, rc, _ = self.go("--single-claim")
        self.assertEqual((rc, rows[e2e.RETIRED]), (1, (e2e.FAIL, "agent.retire did not complete (done without "
                                                                 "bearer_revoked)")))

    def test_a_create_that_needed_neither_or_was_refused_fails(self):
        self.backend.mode = "grant"
        self.backend.bugs.add("no_authority")
        rows, rc, _ = self.go()
        self.assertEqual(rows[e2e.CREATE], (e2e.FAIL, "the agent was created with neither an approval nor a grant"))
        self.assertEqual((rc, rows[e2e.PROVENANCE][0]), (1, e2e.INCOMPLETE))
        self.assertIn("names no authority source", rows[e2e.PROVENANCE][1])

    def test_an_agent_that_may_not_create_agents_is_refused(self):
        self.backend.agents[CORE]["kind"] = "acp"
        rows, rc, _ = self.go()
        self.assertEqual((rc, rows[e2e.CREATE]), (1, (e2e.FAIL, "agent.create was refused (not_a_core)")))
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_a_denied_or_unanswered_approval_ends_the_run(self):
        self.backend.auto_approve = False
        naps = []

        def deny(seconds):
            naps.append(seconds)
            self.backend.now += seconds
            self.backend.approve("denied")
        args = e2e.build_parser().parse_args(["--base-url", BASE_URL, "--worker", "Reviewer", "--workspace", str(self.ws)])
        out = []
        rc = e2e.run(args, request=self.backend.request, vault=self.vault, preview=self.preview, sleep=deny,
                     clock=self.backend.clock, tmux=self.tmux, tmux_targets=TMUX, out=out.append, say=self.said.append)
        self.assertEqual(rc, 1)
        self.assertIn("agent.create did not complete (denied); no agent to check", out[0])
        self.assertIn("a second agent.create was requested and may still be approved: deny its card", out[0])
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        rows, rc, _ = self.go("--approval-timeout", "10", "--single-claim")
        self.assertEqual((rc, rows[e2e.PROVENANCE][1]), (1, "agent.create did not complete (timeout); no agent to check"))


class EachRuleHasACheckThatFails(Base):
    def broken(self, bug, *flags):
        self.backend.bugs.add(bug)
        rows, rc, text = self.go(*flags)
        self.assertEqual(rc, 1, text)
        return rows, [n for n, (s, _) in rows.items() if s == e2e.FAIL]

    def test_a_wrong_secret_that_is_accepted(self):
        rows, failed = self.broken("accept_wrong_secret")
        self.assertIn(e2e.WRONG_SECRET, failed)
        self.assertEqual(rows[e2e.WRONG_SECRET][1], "the server delivered a token")

    def test_another_caller_that_is_accepted(self):
        _, failed = self.broken("accept_other_caller")
        self.assertEqual(failed, [e2e.OTHER_CALLER])

    def test_an_expired_claim_that_is_accepted(self):
        _, failed = self.broken("accept_expired")
        self.assertEqual(failed, [e2e.EXPIRED])

    def test_a_second_redeem_that_mints_a_second_token(self):
        rows, failed = self.broken("second_bearer")
        self.assertIn(e2e.SECOND, failed)
        self.assertEqual(rows[e2e.SECOND][1], "a second, different token was minted")

    def test_a_redeem_answer_that_may_be_cached(self):
        rows, failed = self.broken("no_store_missing")
        self.assertEqual((failed, rows[e2e.NO_STORE][1]), ([e2e.NO_STORE], "Cache-Control lacks no-store"))

    def test_a_token_that_outlives_the_retirement(self):
        rows, failed = self.broken("bearer_survives_retire")
        self.assertEqual(failed, [e2e.RETIRED, e2e.LOCAL_GONE])
        self.assertEqual(rows[e2e.RETIRED][1], "whoami answered http 200")
        self.assertIn("still accepts its token", rows[e2e.LOCAL_GONE][1])
        self.assertIn(WORKER_REF, self.vault.store, "nothing local is removed while the token still works")

    def test_an_agent_named_or_recorded_wrongly(self):
        rows, failed = self.broken("flat_name", "--single-claim", "--room", "")
        self.assertEqual((failed, rows[e2e.NAMING][1]),
                         ([e2e.NAMING], f"got @reviewer.agent:alpha-fixture.net, expected {WORKER_AGENT}"))

    def test_a_name_the_server_had_to_number_and_a_default_core_are_still_the_expected_id(self):
        self.backend.add_agent(WORKER_AGENT, None)
        rows, _, text = self.go("--single-claim", "--room", "")
        self.assertEqual(rows[e2e.NAMING], (e2e.PASS, "@owner-desk-reviewer-2.agent:alpha-fixture.net"), text)
        self.assertEqual(e2e.core_label({"owner_mxid": "@owner:x.net", "agent_mxid": "@owner.agent:x.net"}), "sutando")
        self.assertEqual(e2e.core_label({"owner_mxid": "@owner:x.net", "agent_mxid": "@sutando-owner:x.net"}), "sutando")

    def test_an_agent_recorded_as_someone_elses_work(self):
        rows, failed = self.broken("wrong_creator", "--single-claim")
        self.assertEqual((failed, rows[e2e.PROVENANCE][1]), ([e2e.PROVENANCE], "created_by is not the core"))

    def test_a_retire_the_server_refuses_leaves_the_agent_named(self):
        real = self.backend._execute

        def not_yours(actor, args):
            if args.get("action") == "agent.retire":
                return self.backend._error(403, "not_created_by_actor")
            return real(actor, args)
        self.backend._execute = not_yours
        rows, rc, text = self.go("--single-claim")
        self.assertEqual((rc, rows[e2e.RETIRED]), (1, (e2e.FAIL, "agent.retire did not complete (refused)")))
        self.assertIn(f"left on the server: {WORKER_AGENT} (retire it with agent.retire", text)

    def test_an_agent_missing_from_the_owners_listing_or_held_by_another_owner(self):
        rows, failed = self.broken("unlisted", "--single-claim")
        self.assertIn(e2e.PROVENANCE, failed)
        self.assertIn("is not in the owner's agent listing (http 200)", rows[e2e.PROVENANCE][1])

    def test_an_agent_listed_under_another_owner_or_an_authority_nobody_granted(self):
        rows, failed = self.broken("wrong_owner", "--single-claim")
        self.assertIn(e2e.PROVENANCE, failed)
        self.assertEqual(rows[e2e.PROVENANCE][1], "owner is not the core's owner")
        self.setUp()
        rows, failed = self.broken("odd_authority", "--single-claim")
        self.assertEqual((failed, rows[e2e.PROVENANCE][1]), ([e2e.PROVENANCE], "authority source is own"))

    def test_a_server_with_no_recollect_window_refuses_the_second_redeem_at_once(self):
        self.backend.RECOLLECT_S = -1
        naps = []
        real = self.backend.sleep
        self.backend.sleep = lambda seconds: naps.append(seconds) or real(seconds)
        rows, rc, _ = self.go("--single-claim", "--room", "")
        self.assertEqual(rows[e2e.SECOND], (e2e.PASS, "refused (http 409 claim_redeemed)"))
        self.assertLess(max(naps), 100, "nothing waited for a window")

    def test_a_server_error_or_a_missing_route_is_not_a_refusal(self):
        for status, code in ((503, "UNAVAILABLE"), (404, "NOT_FOUND"), (302, "")):
            self.setUp()
            answered = []

            def broken(method, url, status=status, code=code, answered=answered, **kw):
                real = self.backend.request(method, url, **kw)
                if url.endswith("/redeem") and real[0] != 200:
                    answered.append(real[0])
                    return status, {}, {"code": code} if code else {}
                return real
            rows, rc, text = self.go(request=broken)
            negative = (e2e.WRONG_SECRET, e2e.NO_CREDENTIAL, e2e.SECOND, e2e.OTHER_CALLER, e2e.EXPIRED)
            self.assertEqual({rows[n][0] for n in negative}, {e2e.INCOMPLETE}, text)
            self.assertEqual(rows[e2e.WRONG_SECRET][1], f"not this refusal: the server answered http {status} {code}"
                             .rstrip() + ", not http 403 wrong_secret")
            self.assertEqual((rc, len(answered)), (3, 5), "five refusals were asked for and none is certified")

    def test_a_missing_credential_must_be_refused_as_unauthenticated(self):
        def conflict(method, url, **kw):
            real = self.backend.request(method, url, **kw)
            return (409, {}, {"code": "claim_redeemed"}) if url.endswith("/redeem") and not kw.get("bearer") else real
        rows, _, _ = self.go("--single-claim", request=conflict)
        self.assertEqual(rows[e2e.NO_CREDENTIAL], (e2e.INCOMPLETE, "not this refusal: the server answered http 409 "
                                                                   "claim_redeemed, not http 401 invalid_bearer"))

    def test_an_authentication_failure_or_another_rules_refusal_does_not_certify_a_claim_rule(self):
        def unauthenticated(method, url, **kw):
            real = self.backend.request(method, url, **kw)
            refused = url.endswith("/redeem") and real[0] != 200
            return (401, {}, {"code": "UNAUTHORIZED", "details": {"reason": "invalid_bearer"}}) if refused else real
        rows, rc, text = self.go(request=unauthenticated)
        for name in (e2e.WRONG_SECRET, e2e.SECOND, e2e.OTHER_CALLER, e2e.EXPIRED):
            self.assertEqual(rows[name], (e2e.INCOMPLETE, "not this refusal: the server answered http 401 "
                                          "invalid_bearer, not http %d %s" % e2e.REFUSALS[name]), text)
        self.assertEqual((rc, rows[e2e.NO_CREDENTIAL][0]), (3, e2e.PASS), "only the missing credential is that")
        self.setUp()

        def always_expired(method, url, **kw):
            real = self.backend.request(method, url, **kw)
            refused = url.endswith("/redeem") and real[0] != 200
            return (410, {}, {"details": {"reason": "claim_expired"}}) if refused else real
        rows, rc, _ = self.go(request=always_expired)
        self.assertEqual([n for n in e2e.REFUSALS if rows[n][0] == e2e.PASS], [e2e.EXPIRED], "one reason, one rule")

    def test_a_refusal_while_the_callers_credential_is_not_accepted_says_nothing_about_the_claim(self):
        def credential_lapsed(method, url, **kw):
            if url.endswith("/v1/whoami") and kw.get("bearer") == CORE_SECRET and self.backend.now > 1_800_000_200:
                return 401, {}, {"code": "AGENT_CREDENTIAL_STALE"}
            return self.backend.request(method, url, **kw)
        rows, rc, _ = self.go(request=credential_lapsed)
        self.assertEqual(rows[e2e.EXPIRED], (e2e.INCOMPLETE, "refused (http 410 claim_expired), but the caller's own "
                                             "credential was not accepted just after, so this says nothing about "
                                             "the claim"))
        self.assertEqual((rc, rows[e2e.WRONG_SECRET][0], rows[e2e.OTHER_CALLER][0]), (3, e2e.PASS, e2e.PASS))

    def test_provenance_the_server_does_not_expose_is_incomplete_not_a_pass(self):
        self.backend.bugs.add("hide_provenance")
        rows, rc, _ = self.go("--single-claim")
        self.assertEqual(rows[e2e.PROVENANCE], (e2e.INCOMPLETE, "the Action's answer names no created_by"))
        self.assertEqual(rc, 3)


class Edges(Base):
    def test_a_redeem_that_fails_stops_the_run_and_names_the_agent_left_behind(self):
        rows, rc, text = self.go(preview=lambda token: {"verified": False, "error": "auth_rejected"})
        self.assertEqual((rc, rows[e2e.REDEEM][0], rows[e2e.WHOAMI][0]), (1, e2e.FAIL, e2e.SKIP))
        self.assertEqual(rows[e2e.WRONG_SECRET], (e2e.INCOMPLETE, "refused (http 403 wrong_secret), but the "
                                                  "claim was never redeemed, so it was not shown to be valid"))
        self.assertEqual(rows[e2e.NO_CREDENTIAL][0], e2e.INCOMPLETE)
        self.assertIn("now has a token nobody holds", rows[e2e.REDEEM][1])
        self.assertIn(f"left on the server: {WORKER_AGENT}", text)
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_a_second_create_that_fails_or_expires_early_leaves_its_checks_incomplete(self):
        self.backend.CREATES_PER_HOUR = 1
        rows, rc, _ = self.go()
        self.assertEqual({rows[n] for n in (e2e.OTHER_CALLER, e2e.EXPIRED, e2e.CLEANUP)},
                         {(e2e.INCOMPLETE, "the second agent.create did not complete (refused)")})
        self.assertEqual(rc, 3)

    def test_a_second_claim_that_expired_before_it_was_tried(self):
        self.backend.CLAIM_TTL_S = 2
        rows, rc, _ = self.go()
        self.assertEqual(rows[e2e.OTHER_CALLER], (e2e.INCOMPLETE, "the second claim expired before it could be tried"))
        self.assertEqual((rc, rows[e2e.EXPIRED][0], rows[e2e.CLEANUP][0]), (3, e2e.PASS, e2e.PASS))

    def test_the_mention_check_needs_a_room_a_member_a_sent_message_and_a_reply(self):
        self.backend.responders.clear()
        rows, rc, _ = self.go("--mention-timeout", "9", "--single-claim")
        self.assertEqual((rc, rows[e2e.MENTION]), (1, (e2e.FAIL, f"no reply from {WORKER_AGENT} within 9s")))
        self.setUp()
        self.backend.members[ROOM] = {CORE}
        rows, _, _ = self.go("--mention-timeout", "9", "--single-claim")
        self.assertEqual(rows[e2e.MENTION], (e2e.FAIL, f"{WORKER_AGENT} was not a member of the room within 9s"))
        self.assertEqual(self.backend.rooms[ROOM], [], "nothing is posted to a room the agent is not in")
        self.assertTrue(any(f"invite {WORKER_AGENT} to {ROOM} now" in line for line in self.said))
        self.setUp()
        rows, _, _ = self.go("--room", "!unknown:alpha-fixture.net", "--single-claim", "--mention-timeout", "3")
        self.assertEqual(rows[e2e.MENTION][0], e2e.FAIL)
        self.setUp()
        self.backend.bugs.add("no_members_action")
        self.backend.rooms.pop(ROOM)
        rows, _, _ = self.go("--single-claim")
        self.assertEqual(rows[e2e.MENTION], (e2e.FAIL, "the mention was not sent (ROOM_NOT_FOUND)"))
        self.setUp()
        rows, _, _ = self.go("--room", "", "--single-claim")
        self.assertEqual(rows[e2e.MENTION], (e2e.SKIP, "no --room given"))

    def test_an_agent_invited_while_the_run_waits_is_mentioned_once_it_joins(self):
        self.backend.members[ROOM] = {CORE}
        real = self.backend.sleep

        def invite_later(seconds):
            real(seconds)
            if WORKER_AGENT in self.backend.agents and self.backend.agents[WORKER_AGENT]["bearer"]:
                self.backend.members[ROOM].add(WORKER_AGENT)
        self.backend.sleep = invite_later
        rows, _, text = self.go("--single-claim")
        self.assertEqual(rows[e2e.MENTION], (e2e.PASS, f"{WORKER_AGENT} replied in the room"), text)

    def test_a_server_without_a_member_listing_is_mentioned_anyway(self):
        self.backend.bugs.add("no_members_action")
        rows, _, text = self.go("--single-claim")
        self.assertEqual(rows[e2e.MENTION][0], e2e.PASS, text)

    def test_a_run_that_cannot_start_says_why_and_sends_no_credential(self):
        for flags, said in ((["--worker", "Nobody"], "stopped: no live pool worker 'Nobody'"),
                            (["--base-url", "https://chat.beta-fixture.net"], "stopped: no single core connection"),
                            (["--lane", "alpha-fixture-net", "--base-url", "https://elsewhere.example.com"],
                             "stopped: the named lane's gateway is not at --base-url; no credential was sent")):
            before = len(self.backend.paths)
            rows, rc, text = self.go(*flags)
            self.assertIn(said, text)
            self.assertEqual((rc, len(self.backend.paths), rows[e2e.LEAKS]),
                             (3, before, (e2e.SKIP, "the run produced no secret to look for")))
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "bindings": {}, "workers": {W: {"state": "live", "label": "Reviewer"},
                                                        "b" * 32: {"state": "live", "label": "Reviewer"}}}))
        self.assertIn("stopped: 'Reviewer' names more than one recipient", self.go()[2])

    def test_a_worker_that_already_has_an_agent_is_not_provisioned_again(self):
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", pp.begin(
            W, role="Reviewer", workspace=self.ws, vault=self.vault)["claim_key_sha256"])
        pp.finish(W, claim, WORKER_AGENT, workspace=self.ws, vault=self.vault, request=self.backend.request,
                  preview=self.preview)
        rows, rc, text = self.go()
        self.assertIn("stopped: worker", text)
        self.assertIn("already has an agent of its own", text)
        self.assertIn(WORKER_REF, self.vault.store, "the existing identity is untouched")

    def test_an_answer_nobody_expected_stops_the_run_without_echoing_it_and_the_scan_still_runs(self):
        def odd(method, url, **kw):
            if url.endswith("/v1/agents"):
                raise KeyError("carries " + self.vault.store[CLAIM_KEY])
            return self.backend.request(method, url, **kw)
        rows, rc, text = self.go(request=odd)
        self.assertIn("stopped: unexpected KeyError", text)
        self.assertEqual((rows[e2e.CONTROL][0], rows[e2e.LEAKS][0]), (e2e.PASS, e2e.PASS))
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_an_interrupted_run_still_forgets_the_pending_secret(self):
        def interrupted(method, url, **kw):
            if url.endswith("/mcp"):
                raise KeyboardInterrupt()
            return self.backend.request(method, url, **kw)
        with self.assertRaises(KeyboardInterrupt):
            self.go(request=interrupted)
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_a_cleanup_that_cannot_delete_the_secret_is_noted(self):
        self.backend.mode = "none"
        real = self.vault.delete
        with unittest.mock.patch.object(self.vault, "delete", side_effect=RuntimeError("locked")):
            _, _, text = self.go()
        self.assertIn("cleanup: the claim secret could not be deleted from the vault (RuntimeError)", text)
        real(CLAIM_KEY)


class McpClient(Base):
    def wrapped(self, rule):
        def request(method, url, **kw):
            answer = rule(method, url.replace(BASE_URL, ""), kw)
            return answer if answer is not None else self.backend.request(method, url, **kw)
        return request

    def test_discovery_or_the_exchange_failing_stops_the_run(self):
        _, rc, text = self.go(request=self.wrapped(lambda m, path, kw: (500, {}, {}) if path == "/relay" + pp.DISCOVERY_PATH else None))
        self.assertIn("stopped: MCP discovery answered http 500", text)
        elsewhere = {"mint_url": "https://evil.example.com/mint", "mcp_url": BASE_URL + "/mcp"}
        _, rc, text = self.go(request=self.wrapped(lambda m, path, kw: (200, {}, elsewhere) if path == "/relay" + pp.DISCOVERY_PATH else None))
        self.assertIn("with no mint endpoint on the gateway's own origin", text)
        self.assertNotIn("POST /mint", self.backend.paths)
        refused = (401, {}, {"code": "AGENT_CREDENTIAL_REVOKED"})
        _, rc, text = self.go(request=self.wrapped(lambda m, path, kw: refused if path == provision_fake.MINT_PATH else None))
        self.assertIn("stopped: the access-token exchange answered http 401 AGENT_CREDENTIAL_REVOKED", text)
        self.assertEqual(rc, 3)

    def test_a_rejected_delegation_is_minted_once_more(self):
        for first in ((401, {}, {}), (200, {}, {"result": {"isError": True, "content": [
                {"type": "text", "text": json.dumps({"code": pp.MCP_AUTH_CODE})}]}})):
            self.setUp()
            seen = []

            def once(method, path, kw, first=first, seen=seen):
                if path == "/mcp":
                    seen.append(path)
                    return first if len(seen) == 1 else None
            rows, rc, text = self.go("--single-claim", request=self.wrapped(once))
            self.assertEqual(rows[e2e.CREATE][0], e2e.PASS, text)
            self.assertEqual(self.backend.paths.count(f"POST {provision_fake.MINT_PATH}"), 2)

    def test_an_endpoint_that_does_not_answer_the_call_stops_the_run(self):
        _, _, text = self.go(request=self.wrapped(lambda m, path, kw: (200, {}, {}) if path == "/mcp" else None))
        self.assertIn("stopped: the MCP endpoint answered http 200 to room.action.execute without a result", text)


class LeakScan(Base):
    def test_a_token_logged_during_the_run_is_found_and_never_printed(self):
        def logging_bug(method, url, **kw):
            status, headers, body = self.backend.request(method, url, **kw)
            if status == 200 and url.endswith("/redeem") and not (self.ws / "logs" / "redeem.log").exists():
                (self.ws / "logs" / "redeem.log").write_text(f"redeem answered {json.dumps(body)}\n")
            return status, headers, body
        rows, rc, text = self.go(request=logging_bug)
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (1, e2e.FAIL))
        self.assertEqual(rows[e2e.LEAKS][1], f"1 places hold a secret: worker token x1 in {self.ws / 'logs' / 'redeem.log'}")
        self.assertEqual([n for n, (s, _) in rows.items() if s == e2e.FAIL], [e2e.LEAKS])

    def test_the_cores_own_credential_is_a_leak_anywhere_but_its_own_configured_env_file(self):
        channel = self.ws / ".claude-sutando" / "channels" / "alpha-fixture-net-ag2space"
        channel.mkdir(parents=True)
        (channel / ".env").write_text(f"REMOTE_TASK_TOKEN='{BASE_URL}|{CORE_SECRET}'\n")
        os.symlink(self.tmp.name, self.ws / "state" / "link-out")
        rows, rc, text = self.go("--single-claim")
        self.assertEqual(rows[e2e.LEAKS], (e2e.PASS, "0 hits for 3 secrets in 6 files, 2 tmux sessions "
                                                     "(1 links not followed)"), text)
        for place in ("results/leaked.env", "logs/bridge.log", ".claude-sutando/channels/another-lane/.env"):
            self.setUp()
            elsewhere = self.ws / place
            elsewhere.parent.mkdir(parents=True, exist_ok=True)
            elsewhere.write_text(f"REMOTE_TASK_TOKEN='{BASE_URL}|{CORE_SECRET}'\n")
            rows, rc, _ = self.go("--single-claim")
            self.assertEqual((rc, rows[e2e.LEAKS]), (1, (e2e.FAIL, "1 places hold a secret: core credential x1 in "
                                                                   f"{elsewhere}")), "an env suffix exempts nothing")

    def test_credential_files_that_cannot_be_resolved_exempt_nothing(self):
        with unittest.mock.patch.object(C, "lane_channel_dir", side_effect=RuntimeError("no layout")):
            self.assertEqual(e2e.credential_files({"instance": "alpha-fixture-net"}), set())
        named = self.ws / "device.env"
        named.write_text(f"REMOTE_TASK_TOKEN='{BASE_URL}|{CORE_SECRET}'\n")
        with unittest.mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": str(named)}):
            self.assertEqual(e2e.credential_files({"instance": ""}), {str(named.resolve())})

    def test_a_directory_that_cannot_be_listed_leaves_the_scan_incomplete(self):
        real = os.scandir

        def locked(path):
            if str(path).endswith("slug"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        with unittest.mock.patch.object(os, "scandir", locked):
            rows, rc, _ = self.go("--single-claim")
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (3, e2e.INCOMPLETE))
        self.assertIn(f"unreadable {self.transcripts / 'slug'}", rows[e2e.LEAKS][1])

    def test_a_core_credential_too_short_to_search_for_leaves_the_scan_incomplete(self):
        self.backend.agents[CORE]["bearer"] = "tiny-cred"
        record = json.loads(C.record_path(self.ws).read_text())
        self.vault.store[record["connections"][0]["token_ref"]] = f"{BASE_URL}|tiny-cred"
        rows, rc, _ = self.go("--single-claim")
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (3, e2e.INCOMPLETE))
        self.assertIn("core credential is too short to search for", rows[e2e.LEAKS][1])

    def test_a_claim_secret_in_a_transcript_or_a_tmux_pane_is_found(self):
        def leaky_tmux(socket, session):
            secret = next(v for v in self.vault.ever if re.fullmatch(r"[0-9a-f]{64}", v))
            (self.transcripts / "slug" / "session.jsonl").write_text(json.dumps({"text": f"S={secret} S={secret}"}))
            return b"prompt> echo " + secret.encode() if session == "sutando-core" else b"clean"
        rows, rc, _ = self.go(tmux=leaky_tmux)
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (1, e2e.FAIL))
        self.assertIn(f"claim secret x2 in {self.transcripts / 'slug' / 'session.jsonl'}", rows[e2e.LEAKS][1])
        self.assertIn("claim secret x1 in tmux:core", rows[e2e.LEAKS][1])
        self.assertNotIn("tmux:worker", rows[e2e.LEAKS][1])

    def test_what_could_not_be_scanned_makes_the_result_incomplete(self):
        (self.ws / "results").rmdir()

        def no_session(socket, session):
            raise OSError(f"no tmux session {session}")
        rows, rc, _ = self.go(tmux=no_session)
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (3, e2e.INCOMPLETE))
        for gap in (f"missing {self.ws / 'results'}", "tmux:core (no tmux session sutando-core)", "tmux:worker"):
            self.assertIn(gap, rows[e2e.LEAKS][1])
        self.setUp()
        rows, rc, _ = self.go("--no-tmux")
        self.assertEqual((rc, rows[e2e.LEAKS][0]), (3, e2e.INCOMPLETE))
        self.assertTrue(rows[e2e.LEAKS][1].endswith("0 tmux sessions; tmux scrollback not scanned (--no-tmux)"))
        self.setUp()
        real = e2e._chunks

        def unreadable(path):
            if path.name == "bridge.log":
                raise PermissionError(path)
            return real(path)
        with unittest.mock.patch.object(e2e, "_chunks", unreadable):
            rows, rc, _ = self.go("--scan-root", str(self.ws / "extra"))
        self.assertEqual(rows[e2e.LEAKS][0], e2e.INCOMPLETE)
        self.assertIn(f"unreadable {self.ws / 'logs' / 'bridge.log'}", rows[e2e.LEAKS][1])
        self.assertIn(f"missing {self.ws / 'extra'}", rows[e2e.LEAKS][1])

    def test_a_scan_that_cannot_find_a_planted_value_is_not_trusted(self):
        with unittest.mock.patch.object(e2e, "count_in_stream", lambda chunks, needles: {k: 0 for k in needles}):
            rows, rc, _ = self.go("--single-claim")
        self.assertEqual(rows[e2e.CONTROL], (e2e.FAIL, f"the scan did not find a value planted in {self.ws / 'logs'} "
                                                       "and in a text"))
        self.assertEqual((rc, rows[e2e.LEAKS]), (1, (e2e.INCOMPLETE, "not run: the scan cannot be trusted")))
        self.assertEqual(sorted(p.name for p in (self.ws / "logs").iterdir()), ["bridge.log"], "the control is removed")

    def test_the_control_is_planted_in_the_workspaces_own_logs_so_a_scan_that_skips_them_fails(self):
        rows, _, _ = self.go("--single-claim")
        self.assertEqual(rows[e2e.CONTROL], (e2e.PASS, f"found once in {self.ws / 'logs'} and once in a planted text"))
        self.setUp()
        real = e2e.scan_roots
        with unittest.mock.patch.object(e2e, "scan_roots", lambda ws, extra=(): (real(ws, extra)[0], [])):
            rows, rc, _ = self.go("--single-claim")
        self.assertEqual((rc, rows[e2e.CONTROL][0], rows[e2e.LEAKS][0]), (1, e2e.FAIL, e2e.INCOMPLETE),
                         "a scan that reads none of the real places cannot pass")

    def test_logs_that_cannot_take_the_control_file_fail_the_control(self):
        if os.geteuid() == 0:
            self.skipTest("root writes to a read-only directory")
        os.chmod(self.ws / "logs", 0o500)
        try:
            rows, rc, _ = self.go("--single-claim")
        finally:
            os.chmod(self.ws / "logs", 0o700)
        self.assertEqual((rc, rows[e2e.CONTROL][0], rows[e2e.LEAKS][0]), (1, e2e.FAIL, e2e.INCOMPLETE))

    def test_logs_kept_behind_a_link_and_the_shell_history_are_scanned_too(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.ws / "logs" / "bridge.log").unlink()
        (self.ws / "logs").rmdir()
        os.symlink(outside.name, self.ws / "logs")

        def leaky_tmux(socket, session):
            secret = next(v for v in self.vault.ever if re.fullmatch(r"[0-9a-f]{64}", v))
            (Path(outside.name) / "bridge.log").write_text(f"claim {secret}\n")
            (self.home / ".zsh_history").write_text(f": 1:0;echo {secret}\n")
            return b"clean"
        rows, rc, _ = self.go("--single-claim", tmux=leaky_tmux)
        self.assertEqual((rows[e2e.CONTROL][0], rows[e2e.LEAKS][0]), (e2e.PASS, e2e.FAIL))
        self.assertIn(f"claim secret x1 in {Path(outside.name).resolve() / 'bridge.log'}", rows[e2e.LEAKS][1])
        self.assertIn(f"claim secret x1 in {self.home / '.zsh_history'}", rows[e2e.LEAKS][1])
        self.assertEqual(list(Path(outside.name).glob(".provision-e2e-control-*")), [])


class Scanner(unittest.TestCase):
    SECRET = "5ec2e7" * 8

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "logs" / "deep").mkdir(parents=True)
        (self.root / "logs" / "a.log").write_text("nothing to see\n")
        (self.root / "logs" / "deep" / "b.jsonl").write_text('{"text": "still nothing"}\n')

    def test_a_planted_secret_is_found_with_its_place_and_count(self):
        (self.root / "logs" / "deep" / "b.jsonl").write_text(f'{{"text": "Bearer {self.SECRET}"}}\n{self.SECRET}\n')
        out = e2e.scan({"token": self.SECRET}, [self.root], {"pane": f"x{self.SECRET}y".encode()})
        self.assertCountEqual(out["hits"], [("token", "pane", 1),
                                            ("token", str(self.root / "logs" / "deep" / "b.jsonl"), 2)])
        self.assertEqual((out["files"], out["unreadable"]), (2, []))

    def test_a_clean_tree_has_no_hit(self):
        out = e2e.scan({"token": self.SECRET, "absent": ""}, [self.root, self.root / "no-such-dir"], {"pane": b"clean"})
        self.assertEqual(out, {"hits": [], "files": 2, "unreadable": [str(self.root / "no-such-dir")], "links": []},
                         "a root that cannot be listed is not a clean one")

    def test_a_secret_split_across_read_chunks_is_still_found(self):
        (self.root / "logs" / "a.log").write_bytes(b"x" * 37 + self.SECRET.encode() + b"y" * 50 + self.SECRET.encode())
        with unittest.mock.patch.object(e2e, "CHUNK", 16):
            out = e2e.scan({"token": self.SECRET}, [self.root], {})
        self.assertEqual(out["hits"], [("token", str(self.root / "logs" / "a.log"), 2)])
        self.assertEqual(e2e.count_in_stream([b"ab", b"cab", b"c"], {"n": b"abc"}), {"n": 2})
        self.assertEqual(e2e.count_in_stream([b"aaa", b"a"], {"n": b"aa"}), {"n": 2}, "never counted twice")

    def test_a_needle_too_short_to_mean_anything_is_refused(self):
        for needles in ({"token": "short"}, {}, {"token": ""}):
            with self.assertRaises(ValueError):
                e2e.scan(needles, [self.root], {})

    def test_links_are_not_followed_and_what_cannot_be_read_is_listed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "elsewhere.log").write_text(self.SECRET)
        os.symlink(outside.name, self.root / "logs" / "linked-dir")
        os.symlink(self.root / "gone", self.root / "logs" / "dangling")
        real = e2e._chunks

        def locked(path):
            if path.name == "a.log":
                raise PermissionError(path)
            return real(path)
        with unittest.mock.patch.object(e2e, "_chunks", locked):
            out = e2e.scan({"token": self.SECRET}, [self.root], {})
        self.assertEqual((out["hits"], out["files"], out["unreadable"]), ([], 1, [str(self.root / "logs" / "a.log")]))
        self.assertCountEqual(out["links"], [str(self.root / "logs" / "linked-dir"), str(self.root / "logs" / "dangling")])
        os.symlink(Path(outside.name) / "elsewhere.log", self.root / "logs" / "linked-file")
        self.assertEqual(e2e.scan({"token": self.SECRET}, [self.root], {})["hits"], [], "a linked file is not read")


class Tmux(unittest.TestCase):
    def runner(self, panes, capture_rc=0):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "list-panes" in argv:
                return types.SimpleNamespace(returncode=0 if panes else 1, stdout=" ".join(panes).encode())
            return types.SimpleNamespace(returncode=capture_rc, stdout=f"history of {argv[-1]}".encode())
        return run, calls

    def test_every_pane_of_the_session_is_captured_whole_and_joined(self):
        run, calls = self.runner(["%1", "%2"])
        self.assertEqual(e2e.tmux_scrollback("sock", "sutando-core", run), b"history of %1\nhistory of %2\n")
        self.assertEqual(calls[0][1:], ["-S", "sock", "list-panes", "-s", "-t", "=sutando-core", "-F", "#{pane_id}"])
        self.assertEqual(calls[1][1:], ["-S", "sock", "capture-pane", "-p", "-J", "-S", "-", "-E", "-", "-t", "%1"])

    def test_a_missing_session_or_a_failed_capture_is_an_error_not_an_empty_pane(self):
        with self.assertRaises(OSError):
            e2e.tmux_scrollback("sock", "gone", self.runner([])[0])
        with self.assertRaises(OSError):
            e2e.tmux_scrollback("sock", "sutando-core", self.runner(["%1"], capture_rc=1)[0])


class Wiring(Base):
    def test_the_default_tmux_targets_are_the_core_session_and_the_workers(self):
        import pool_sessions
        rows = [{"worker_id": W, "tmux_socket": "worker.sock", "session_name": f"sutando-worker-{W}"},
                {"worker_id": "b" * 32, "tmux_socket": "", "session_name": "other"}]
        with unittest.mock.patch.object(pool_sessions, "sessions", lambda ws: rows), \
                unittest.mock.patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": "core.sock"}):
            self.assertEqual(e2e.default_tmux_targets(self.ws, "Reviewer"),
                             [("core", "core.sock", "sutando-core"), ("worker", "worker.sock", f"sutando-worker-{W}")])
            self.assertEqual(e2e.default_tmux_targets(self.ws, "Nobody", "other.sock=main"),
                             [("core", "other.sock", "main")])

    def test_with_no_vault_handed_in_the_run_uses_the_real_adapters_split(self):
        own = provision_fake.KeychainShaped(self.vault)
        args = e2e.build_parser().parse_args(["--base-url", BASE_URL, "--worker", "Reviewer", "--workspace",
                                              str(self.ws), "--room", ROOM, "--no-start"])
        out = []
        with unittest.mock.patch.object(pp, "_vault", lambda v=None: own), \
                unittest.mock.patch.object(C, "_vault", lambda v=None: v or self.vault), \
                unittest.mock.patch.object(pp, "http_json", self.backend.request), \
                unittest.mock.patch.object(C, "preview_connection", self.preview):
            rc = e2e.run(args, sleep=self.backend.sleep, clock=self.backend.clock, tmux=self.tmux, tmux_targets=TMUX,
                         out=out.append, say=self.said.append)
        self.assertEqual(rc, 0, out[0])
        self.assertIn("18 PASS", out[0])

    def test_main_resolves_the_workspace_and_the_targets_then_runs(self):
        seen = {}

        def fake_run(args, **kw):
            seen.update(args=args, **kw)
            return 7
        with unittest.mock.patch.object(e2e, "run", fake_run), \
                unittest.mock.patch.object(e2e, "default_tmux_targets", lambda ws, worker, core: [("core", core, "s")]), \
                unittest.mock.patch.object(e2e, "resolve_workspace", lambda: self.ws):
            rc = e2e.main(["--base-url", BASE_URL, "--worker", "Reviewer", "--core-tmux", "a.sock=s"])
            self.assertEqual((rc, seen["args"].workspace, seen["tmux_targets"]), (7, self.ws, [("core", "a.sock=s", "s")]))
            e2e.main(["--base-url", BASE_URL, "--worker", "Reviewer", "--workspace", str(self.ws), "--no-tmux"])
            self.assertEqual(seen["tmux_targets"], [])

    def test_the_whole_workspace_is_scanned_but_the_owners_projects_and_links_out_of_it(self):
        (self.ws / "projects" / "app").mkdir(parents=True)
        (self.ws / "conversation.log").write_text("hello\n")
        os.symlink(self.tmp.name, self.ws / "link-out")
        required, scanned = e2e.scan_roots(self.ws)
        mine = [self.ws / "logs", self.ws / "results", self.ws / "tasks", self.transcripts]
        self.assertEqual(required, mine)
        self.assertEqual(scanned, sorted([self.ws / ".claude-sutando", self.ws / "conversation.log", self.ws / "logs",
                                          self.ws / "results", self.ws / "state", self.ws / "tasks"])
                         + [r.resolve() for r in mine] + [self.home / ".zsh_history", self.home / ".bash_history"])
        with tempfile.TemporaryDirectory() as home, unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            required, scanned = e2e.scan_roots(self.ws, [Path("more")])
            self.assertEqual((required[-1], scanned[-3]), (Path("more"), Path(home) / "projects"))
            self.assertIn(Path("more").resolve(), scanned)
        self.assertEqual(e2e.scan_roots(self.ws / "absent")[1][0], (self.ws / "absent" / "logs").resolve())

    def test_a_secret_anywhere_in_the_workspace_is_found_once_even_when_a_root_is_named_twice(self):
        secret = "5ec2e7" * 8
        (self.ws / "conversation.log").write_text(f"owner: {secret}\n")
        (self.ws / "state" / "note.json").write_text(json.dumps({"k": secret}))
        _, scanned = e2e.scan_roots(self.ws, [self.ws / "state"])
        out = e2e.scan({"token": secret}, scanned, {})
        self.assertCountEqual(out["hits"], [("token", str(self.ws / "conversation.log"), 1),
                                            ("token", str(self.ws / "state" / "note.json"), 1)])


class Small(unittest.TestCase):
    def test_expiry_reads_a_number_or_an_iso_time(self):
        self.assertEqual(e2e.expiry_of(1800000300), 1800000300.0)
        self.assertEqual(e2e.expiry_of("2027-01-15T08:05:00Z"), e2e.expiry_of("2027-01-15T08:05:00+00:00"))
        self.assertEqual(e2e.expiry_of("2027-01-15T08:05:00"), e2e.expiry_of("2027-01-15T08:05:00Z"), "naive is UTC")
        for unreadable in (None, "soon", True):
            self.assertIsNone(e2e.expiry_of(unreadable))

    def test_the_report_redacts_what_the_run_holds_and_never_passes_an_unset_check(self):
        report = e2e.Report(["hunter2-hunter2-hunter2"])
        self.assertEqual(report.exit_code(), 3, "nothing set is nothing passed")
        for name in e2e.CHECKS:
            report.set(name, e2e.PASS, "saw hunter2-hunter2-hunter2")
        report.note("note with hunter2-hunter2-hunter2")
        self.assertEqual((report.exit_code(), "hunter2" in report.render()), (0, False))
        report.set(e2e.LEAKS, e2e.INCOMPLETE)
        self.assertEqual(report.exit_code(), 3)
        report.set(e2e.CREATE, e2e.FAIL)
        self.assertEqual(report.exit_code(), 1)

    def test_progress_goes_to_stderr(self):
        with unittest.mock.patch.object(sys, "stderr", new=unittest.mock.Mock()) as err:
            e2e._say("waiting")
        err.write.assert_any_call("waiting")


class Collected(unittest.TestCase):
    def test_every_test_in_this_file_is_above_the_runner(self):
        source = Path(__file__).read_text()
        self.assertEqual(len(re.findall(r"^    def test_", source, re.M)), 70)
        self.assertTrue(source.rstrip().endswith("unittest.main()"))
        self.assertIs(subprocess.SubprocessError, e2e.subprocess.SubprocessError)


if __name__ == "__main__":
    unittest.main()
