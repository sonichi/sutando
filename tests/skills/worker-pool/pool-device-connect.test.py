#!/usr/bin/env python3
"""A worker's own agent through s.space's device-connect flow (`pool_device_connect.py`, `pool_identity.py
add|rotate --device-flow`), against an in-process fake of `/api/connect/start` and `/api/connect/poll` in
front of the provisioning fake's gateway.

- The owner gets the approval link in their DM; the device code and the bearer are never printed,
  returned, written to a file or raised.
- The token is saved only when the gateway confirms the agent the poll named, owned by the core's owner,
  and never when the server answers with the core's own agent.
- A link that expires is reported with its expiry and saves nothing; `rotate` keeps the same agent.

Run: python3 tests/skills/worker-pool/pool-device-connect.test.py
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("pool_provision_suite", Path(__file__).with_name("pool-provision.test.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)  # its hermetic environment, connection fakes and provisioning backend
C, H = S.C, S.H
import pool_device_connect as dc
import pool_identity
import pool_manage

W, CORE = S.W, S.CORE
NEW_AGENT = "@owner-desk-reviewer.agent:alpha-fixture.net"


class ConnectFake:
    """`/api/connect/*` on the backend's origin: start hands out a code, poll answers 202 until `approve`."""

    def __init__(self, backend, *, agent=NEW_AGENT, owner=None, expires_in=900, link_with_code=False,
                 link_host="https://alpha-fixture.net"):
        self.backend, self.agent, self.owner = backend, agent, owner
        self.expires_in, self.link_with_code, self.link_host = expires_in, link_with_code, link_host
        self.codes, self.approved, self.started = {}, False, []
        self.bearer = None
        self.start_status, self.interval, self.poll_script = 200, 3, []
        self.relay_url, self.agent_id_answer = backend.origin + "/relay", None

    def approve(self):
        self.approved = True

    def request(self, method, url, *, bearer="", payload=None, headers=None, timeout=None):
        origin, _, path = url.partition("://")[2].partition("/")
        if "/" + path == dc.START_PATH and method == "POST":
            self.started.append(dict(payload or {}))
            if self.start_status != 200:
                return self.start_status, {}, {"error": "rate_limited"}
            code = "dc-" + secrets.token_hex(16)
            self.codes[code] = self.backend.now + self.expires_in
            link = f"{self.link_host}/api/connect-page?code={code if self.link_with_code else 'U-' + code[-6:]}"
            return 200, {}, {"device_code": code, "verify_url": link, "interval": self.interval, "expires_in": self.expires_in}
        if "/" + path == dc.POLL_PATH and method == "POST":
            code = (payload or {}).get("device_code")
            if self.poll_script:
                step = self.poll_script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step if isinstance(step, tuple) else (step, {}, {"error": "forbidden"})
            if code not in self.codes or self.backend.now > self.codes[code] + 30:
                return 404, {}, {"error": "unknown_code"}
            if not self.approved:
                return 202, {}, {"status": "pending"}
            if self.bearer is None:
                self.bearer = "bearer-" + secrets.token_hex(16)
                self.backend.add_agent(self.agent, self.bearer, owner=self.owner)
            del self.codes[code]
            return 200, {}, {"relay_url": self.relay_url, "bearer": self.bearer,
                             "agent_id": self.agent_id_answer or self.agent}
        return self.backend.request(method, url, bearer=bearer, payload=payload, headers=headers, timeout=timeout)


class Base(S.Base):
    def setUp(self):
        super().setUp()
        self.sent = []

    def connect(self, fake, *, approve_after=2, **kw):
        polls = {"n": 0}

        def sleep(seconds):
            self.backend.sleep(seconds)
            polls["n"] += 1
            if approve_after is not None and polls["n"] >= approve_after:
                fake.approve()
        kw.setdefault("label", "reviewer")
        return dc.connect(W, workspace=self.ws, vault=self.vault, request=fake.request, preview=self.preview,
                          deliver=self.sent.append, sleep=sleep, clock=self.backend.clock, **kw)

    def assertNoSecret(self, fake, *texts):
        for text in texts:
            for value in list(fake.codes) + [fake.bearer]:
                if value:
                    self.assertNotIn(value, text)


class Add(Base):
    def test_the_owner_approves_and_the_token_is_saved_for_the_new_agent(self):
        fake = ConnectFake(self.backend)
        out = self.connect(fake, display_name="Reviewer")
        self.assertEqual((out["agent_id"], out["status"]), (NEW_AGENT, "ok"))
        core = dc.pp.core_lane(self.ws, None)["agent_mxid"]
        self.assertEqual(fake.started, [{"label": "reviewer", "display_name": "Reviewer",
                                         "welcome": "worker", "welcome_core": core}])
        row = C.worker_identity(W, self.ws)
        self.assertEqual(row["agent_mxid"], NEW_AGENT)
        self.assertEqual(C.secret_of(self.vault.store[row["token_ref"]]), fake.bearer)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("https://alpha-fixture.net/api/connect-page?code=U-", self.sent[0])
        self.assertIn("It works until", self.sent[0])
        self.assertTrue(all(t.startswith("[dm-only]") for t in self.sent))
        self.assertNoSecret(fake, json.dumps({k: v for k, v in out.items() if k != "row"}), *self.sent)

    def test_once_saved_the_owner_is_told_which_agent_it_is_and_where_to_find_it(self):
        fake = ConnectFake(self.backend)
        self.connect(fake, worker_label="pool worker 1")
        done = self.sent[-1]
        self.assertIn(NEW_AGENT, done)
        self.assertIn("pool worker 1's own agent", done)
        self.assertIn("agents list", done)
        self.assertNoSecret(fake, done)

    def test_a_refused_save_sends_no_done_note(self):
        fake = ConnectFake(self.backend, agent=CORE)
        fake.bearer = self.backend.bearer_of(CORE)
        with self.assertRaises(dc.DeviceConnectError):
            self.connect(fake)
        self.assertEqual(len(self.sent), 1)

    def test_the_core_s_own_agent_is_refused(self):
        fake = ConnectFake(self.backend, agent=CORE)
        fake.bearer = self.backend.bearer_of(CORE)
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(fake)
        self.assertIn("the core's own agent", str(e.exception))
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNoSecret(fake, str(e.exception))

    def test_an_agent_of_another_owner_is_refused(self):
        fake = ConnectFake(self.backend, owner="@stranger:alpha-fixture.net")
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(fake)
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNoSecret(fake, str(e.exception))

    def test_a_link_that_expires_saves_nothing_and_says_when(self):
        fake = ConnectFake(self.backend, expires_in=60)
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(fake, approve_after=None)
        self.assertIn("expired at", str(e.exception))
        self.assertEqual(e.exception.status, "expired")
        self.assertEqual(len(self.sent), 2, "the link, then the note that it lapsed")
        self.assertTrue(self.sent[1].startswith("[dm-only]"))
        self.assertIn("expired at", self.sent[1])
        self.assertIn("Ask me for a new link", self.sent[1])
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNoSecret(fake, str(e.exception))

    def test_a_link_carrying_the_device_code_is_never_sent(self):
        fake = ConnectFake(self.backend, link_with_code=True)
        with self.assertRaises(dc.DeviceConnectError):
            self.connect(fake)
        self.assertEqual(self.sent, [])

    def test_a_link_on_another_host_is_never_sent(self):
        for host in ("https://alpha-fixture.net.evil.example", "http://alpha-fixture.net"):
            with self.assertRaises(dc.DeviceConnectError):
                self.connect(ConnectFake(self.backend, link_host=host))
        self.assertEqual(self.sent, [])
        self.connect(ConnectFake(self.backend, link_host=""))
        self.assertIn("https://chat.alpha-fixture.net/api/connect-page?code=U-", self.sent[0])

    def test_no_label_is_refused_before_anything_is_asked(self):
        fake = ConnectFake(self.backend)
        for label in ("", "Has Space", "x" * 40):
            with self.assertRaises(dc.DeviceConnectError):
                self.connect(fake, label=label)
        self.assertEqual(fake.started, [])

    def test_a_worker_with_an_agent_is_told_to_rotate(self):
        self.connect(ConnectFake(self.backend))
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(ConnectFake(self.backend))
        self.assertIn("rotate it instead", str(e.exception))


class Edges(Base):
    def refused(self, fake, text, **kw):
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(fake, **kw)
        self.assertIn(text, str(e.exception))
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertNoSecret(fake, str(e.exception))
        return e.exception

    def test_a_start_the_server_refuses_names_its_status_and_reason(self):
        fake = ConnectFake(self.backend)
        fake.start_status = 429
        self.refused(fake, "did not start a device connect (HTTP 429, rate_limited)")
        self.assertEqual(self.sent, [])

    def test_a_dropped_poll_is_asked_again_and_a_refused_one_ends_the_wait(self):
        fake = ConnectFake(self.backend)
        fake.poll_script = [dc.pp.TransportError("URLError")]
        self.assertEqual(self.connect(fake)["status"], "ok")
        fake = ConnectFake(self.backend, agent="@owner-desk-two.agent:alpha-fixture.net")
        fake.poll_script = [403]
        C.retire_worker_identity(W, workspace=self.ws, vault=self.vault)
        self.refused(fake, "refused the device connect (HTTP 403, forbidden)")

    def test_an_unreadable_interval_falls_back_to_the_floor(self):
        fake = ConnectFake(self.backend)
        fake.interval = "soon"
        before = self.backend.now
        self.connect(fake, approve_after=1)
        self.assertEqual(self.backend.now - before, dc.MIN_INTERVAL_S)

    def test_a_base_that_is_not_an_origin_is_refused(self):
        for base in ("https://chat.alpha-fixture.net/x", "https://evil.example", "https://alpha-fixture.net"):
            self.refused(ConnectFake(self.backend), "the core's own gateway origin", base=base)
        self.assertEqual(self.sent, [], "no link is ever asked for on another origin")
        self.assertEqual(self.connect(ConnectFake(self.backend), base="https://chat.alpha-fixture.net")["status"], "ok")

    def test_pending_and_slow_down_answers_keep_polling_and_429_honours_retry_after(self):
        fake = ConnectFake(self.backend)
        fake.poll_script = [(400, {}, {"error": "authorization_pending"}),
                            (400, {}, {"error": "slow_down"}),
                            (429, {"retry-after": "20"}, {}),
                            (429, {"retry-after": "soon"}, {})]
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            self.backend.sleep(seconds)
            if not fake.poll_script:
                fake.approve()
        out = dc.connect(W, label="reviewer", workspace=self.ws, vault=self.vault, request=fake.request,
                         preview=self.preview, deliver=self.sent.append, sleep=sleep, clock=self.backend.clock)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(waits[:5], [3, 3, 8, 20, 8])

    def test_a_poll_answer_without_a_valid_agent_id_or_on_another_gateway_is_not_saved(self):
        fake = ConnectFake(self.backend)
        fake.agent_id_answer = "not-an-mxid"
        self.refused(fake, "names no agent id")
        fake = ConnectFake(self.backend)
        fake.relay_url = "https://other-fixture.net/relay"
        self.refused(fake, "a gateway that is not the core's")

    def test_a_token_the_gateway_does_not_confirm_is_not_saved(self):
        self.preview = lambda token: {"verified": False, "authenticated": None, "error": "no listing"}
        self.refused(ConnectFake(self.backend), "does not confirm the token is agent")

    def test_a_save_the_connection_rules_refuse_is_named_without_a_secret(self):
        def refuse(*a, **k):
            raise C.ConnectionsError("vault unavailable")
        with unittest.mock.patch.object(C, "add_worker_identity", refuse):
            self.refused(ConnectFake(self.backend), "vault unavailable; the approved agent")

    def test_a_pasted_identity_added_while_the_token_is_checked_is_not_overwritten_or_reported(self):
        # The pasted-token add takes no roster lock, so it can land after the fence's own check.
        other, real = "@owner-desk-pasted.agent:alpha-fixture.net", self.preview
        pasted = self.backend.add_agent(other, "bearer-pasted")

        def preview(token):
            if C.worker_identity(W, self.ws) is None:
                C.add_worker_identity(W, pasted, expected_server="alpha-fixture.net", preview=real(pasted),
                                      workspace=self.ws, vault=self.vault)
            return real(token)
        self.preview = preview
        starts = []
        fake = ConnectFake(self.backend)
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(fake, start_lane=lambda row: starts.append(row))
        self.assertIn("already had a connection", str(e.exception))
        self.assertEqual(starts, [])
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], other)
        self.assertNoSecret(fake, str(e.exception))

    def test_an_identity_that_changes_during_the_wait_is_refused(self):
        self.connect(ConnectFake(self.backend))
        fake = ConnectFake(self.backend)

        def sleep(seconds):
            self.backend.sleep(seconds)
            C.retire_worker_identity(W, workspace=self.ws, vault=self.vault)
            fake.approve()
        with self.assertRaises(dc.DeviceConnectError) as e:
            dc.connect(W, label="reviewer", rotate=True, workspace=self.ws, vault=self.vault, request=fake.request,
                       preview=self.preview, deliver=self.sent.append, sleep=sleep, clock=self.backend.clock)
        self.assertEqual(e.exception.status, "identity_changed")

    def test_by_default_the_link_is_queued_to_the_owner_s_dm(self):
        fake = ConnectFake(self.backend)
        fake.approve()
        dc.connect(W, label="reviewer", workspace=self.ws, vault=self.vault, request=fake.request,
                   preview=self.preview, sleep=self.backend.sleep, clock=self.backend.clock)
        queued = sorted((self.ws / "results").glob("proactive-*worker-connect*.txt"))
        self.assertEqual(len(queued), 2)
        texts = [q.read_text() for q in queued]
        self.assertTrue(all(t.startswith("[dm-only]") for t in texts))
        self.assertTrue(any("/api/connect-page?code=U-" in t for t in texts))
        self.assertTrue(any(t.startswith("[dm-only]\nDone:") for t in texts))
        self.assertNoSecret(fake, *texts)


class Rotate(Base):
    def test_rotate_replaces_the_secret_for_the_same_agent_only(self):
        first = ConnectFake(self.backend)
        self.connect(first)
        old = self.vault.store[C.worker_identity(W, self.ws)["token_ref"]]
        other = ConnectFake(self.backend, agent="@owner-desk-other.agent:alpha-fixture.net")
        with self.assertRaises(dc.DeviceConnectError) as e:
            self.connect(other, rotate=True)
        self.assertIn("must answer for", str(e.exception))
        self.assertEqual(self.vault.store[C.worker_identity(W, self.ws)["token_ref"]], old)

        again = ConnectFake(self.backend)
        self.backend.agents[NEW_AGENT]["bearer"] = again.bearer = "bearer-" + secrets.token_hex(16)
        updated = {}
        with unittest.mock.patch.object(C, "update_token", lambda instance, token, **kw: updated.update(
                instance=instance, secret=C.secret_of(token)) or {"instance": instance}):
            out = self.connect(again, rotate=True)
        self.assertEqual((out["agent_id"], out["status"]), (NEW_AGENT, "rotated"))
        self.assertNotIn("welcome", again.started[-1])
        self.assertEqual(updated, {"instance": C.worker_identity(W, self.ws)["instance"], "secret": again.bearer})

    def test_rotate_needs_an_agent(self):
        with self.assertRaises(dc.DeviceConnectError):
            self.connect(ConnectFake(self.backend), rotate=True)


class Cli(Base):
    def test_add_device_flow_prints_one_json_line_without_a_secret(self):
        fake = ConnectFake(self.backend)
        real = dc.connect

        def wired(worker, **kw):
            kw.update(request=fake.request, preview=self.preview, deliver=self.sent.append, vault=self.vault,
                      sleep=lambda s: (self.backend.sleep(s), fake.approve()), clock=self.backend.clock)
            return real(worker, **kw)
        out = []
        with unittest.mock.patch.object(dc, "connect", wired), \
                unittest.mock.patch.object(C, "start_added", lambda row, **kw: {"ok": True}), \
                unittest.mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = pool_identity.main(["--workspace", str(self.ws), "add", "Reviewer", "--device-flow",
                                     "--label", "reviewer"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out[-1]), {"agent_id": NEW_AGENT, "status": "ok", "seat": "skipped"})
        self.assertNoSecret(fake, *out)

    def test_a_new_identity_restarts_the_running_seat_and_a_rotate_does_not(self):
        calls = []
        with unittest.mock.patch.object(pool_manage, "reseat",
                                        lambda ws, repo, wid, **kw: calls.append(wid) or {"result": "reseated"}):
            rc, out, _ = self.run_cli(ConnectFake(self.backend))
            self.assertEqual((rc, json.loads(out[-1])["seat"]), (0, "reseated"))
            self.assertEqual(calls, [W])
            with unittest.mock.patch.object(C, "update_token", lambda instance, token, **kw: {"instance": instance}):
                rc, out, _ = self.run_cli(ConnectFake(self.backend), verb="rotate")
        self.assertNotIn("seat", json.loads(out[-1]))
        self.assertEqual(calls, [W])

    def test_a_seat_that_owes_work_is_left_and_the_owner_is_told_how_to_restart_it(self):
        with unittest.mock.patch.object(pool_manage, "reseat", lambda *a, **kw: {
                "result": "deferred", "why": "it owes 1 task(s)"}):
            rc, out, _ = self.run_cli(ConnectFake(self.backend))
        self.assertEqual((rc, json.loads(out[-1])["seat"]), (0, "deferred"))
        self.assertTrue(any(f"restart it with: pool_manage.py reseat {W}" in line for line in out), out)

    def run_cli(self, fake, during_wait=None, *, patch_start=True, extra=(), verb="add", approve=True):
        real, out, starts = dc.connect, [], []

        def wired(worker, **kw):
            def sleep(s):
                self.backend.sleep(s)
                if during_wait is not None and not fake.approved:
                    during_wait()
                if approve:
                    fake.approve()
            kw.update(request=fake.request, preview=self.preview, deliver=self.sent.append, vault=self.vault,
                      sleep=sleep, clock=self.backend.clock)
            return real(worker, **kw)
        recorder = (lambda row, **kw: starts.append(row) or {"ok": True}) if patch_start else C.start_added
        with unittest.mock.patch.object(dc, "connect", wired), \
                unittest.mock.patch.object(C, "start_added", recorder), \
                unittest.mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = pool_identity.main(["--workspace", str(self.ws), verb, "Reviewer", "--device-flow",
                                     "--label", "reviewer", *extra])
        return rc, out, starts

    def test_a_worker_retired_during_the_approval_wait_gets_no_identity_and_no_lane(self):
        import pool_roster as pr

        def retire():
            pr.set_worker_state(self.ws, W, "retired", keep_bindings=True)
            C.retire_worker_identity(W, workspace=self.ws, vault=self.vault)
        fake = ConnectFake(self.backend)
        rc, out, starts = self.run_cli(fake, during_wait=retire)
        self.assertEqual(rc, 1, out)
        self.assertEqual(json.loads(out[-1]), {"status": "worker_retired"})
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertEqual(starts, [])
        self.assertEqual(pr.load_roster(self.ws)["workers"][W]["state"], "retired")
        self.assertNoSecret(fake, *out)

    def test_control_a_live_worker_saves_and_starts_its_lane_once(self):
        rc, out, starts = self.run_cli(ConnectFake(self.backend))
        self.assertEqual((rc, json.loads(out[-1])["status"]), (0, "ok"))
        self.assertEqual([r["instance"] for r in starts], [C.worker_identity(W, self.ws)["instance"]])

    def test_a_lane_that_does_not_start_is_reported_and_no_start_skips_it(self):
        fake = ConnectFake(self.backend)
        out = []

        def broken(row, **kw):
            raise C.ConnectionsError("bridge refused")
        with unittest.mock.patch.object(C, "start_added", broken):
            rc, out, _ = self.run_cli(fake, patch_start=False)
        self.assertEqual((rc, json.loads(out[-1])["status"]), (1, "stored_lane_not_started"))
        self.assertTrue(any("bridge refused" in line for line in out))
        C.retire_worker_identity(W, workspace=self.ws, vault=self.vault)
        rc, out, starts = self.run_cli(ConnectFake(self.backend, agent="@owner-desk-two.agent:alpha-fixture.net"),
                                       extra=["--no-start"])
        self.assertEqual((rc, json.loads(out[-1])["status"], starts), (0, "ok", []), out)

    def test_rotate_through_the_cli_and_a_missing_label(self):
        self.run_cli(ConnectFake(self.backend))
        again = ConnectFake(self.backend)
        self.backend.agents[NEW_AGENT]["bearer"] = again.bearer = "bearer-" + secrets.token_hex(16)
        with unittest.mock.patch.object(C, "update_token", lambda instance, token, **kw: {"instance": instance}):
            rc, out, starts = self.run_cli(again, verb="rotate")
        self.assertEqual((rc, json.loads(out[-1]), starts), (0, {"agent_id": NEW_AGENT, "status": "rotated"}, []))
        err = []
        with unittest.mock.patch("builtins.print", lambda *a, **k: err.append(" ".join(map(str, a)))):
            rc = pool_identity.main(["--workspace", str(self.ws), "add", "Reviewer", "--device-flow"])
        self.assertEqual(rc, 2)
        self.assertIn("needs --label", err[-1])

    def test_an_expired_link_prints_its_own_status(self):
        rc, out, starts = self.run_cli(ConnectFake(self.backend, expires_in=60), during_wait=lambda: None,
                                       approve=False)
        self.assertEqual((rc, json.loads(out[-1]), starts), (1, {"status": "expired"}, []))
        self.assertIn("Ask me for a new link", self.sent[-1])

    def test_a_pasted_token_still_needs_repo_and_logs_dir(self):
        with self.assertRaises(SystemExit):
            with unittest.mock.patch("sys.stderr"):
                pool_identity.main(["--workspace", str(self.ws), "add", "Reviewer", "-"])


if __name__ == "__main__":
    unittest.main()
