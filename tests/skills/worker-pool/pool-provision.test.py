#!/usr/bin/env python3
"""Agent-native provisioning of a worker's own agent: the local half (`pool_provision.py`) and its
`pool_identity.py` verbs, against an in-process fake of the hosted backend's contract.

- `begin` keeps the one-time secret in the vault and gives back only its SHA-256 and a label.
- `finish` redeems as the core's own agent and saves the bearer as the worker's connection only when
  the answer and the gateway both name the agent `agent.create` made. It deletes the secret however
  it ends, an interrupt included, and names a failure without a secret in it.
- `retire` removes the identity only once the gateway rejects its token.
- The transport refuses cleartext and never follows a redirect.

The vault and the gateway probes are fakes; nothing here reaches a network beyond this machine.

Run: python3 tests/skills/worker-pool/pool-provision.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

ROOT = Path(__file__).resolve().parents[3]

import contextlib
import importlib.util
import io
import json
import re
import unittest
import unittest.mock

_spec = importlib.util.spec_from_file_location("connections_contract", ROOT / "tests" / "connections.test.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)  # fakes the resolver, as that suite does
C = H.C
sys.path.insert(0, str(ROOT / "skills" / "worker-pool" / "scripts"))
import pool_identity
import pool_provision as pp
from _helpers import provision_fake

W = "7c700e11" + "0" * 24
CORE = "@owner-desk.agent:alpha-fixture.net"
WORKER_AGENT = "@owner-desk-reviewer.agent:alpha-fixture.net"
CLAIM_KEY = "AG2_PROVISION_CLAIM_" + W.upper()
WORKER_REF = "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET_7C700E11"


class Base(H.Base):
    def setUp(self):
        super().setUp()
        self.vault = provision_fake.recording(H.FakeVault)()
        self.backend = provision_fake.FakeBackend()
        self.backend.add_agent(CORE, "secret-alpha")
        self.add(H.TOKEN_A, fetch=H.agents(CORE))
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "Reviewer"}}, "bindings": {}}))
        self.preview = provision_fake.preview_through(self.backend, C)
        stop = unittest.mock.patch.object(C, "_stop_row", lambda row, ws, kill: 0)
        stop.start()
        self.addCleanup(stop.stop)

    def begin(self, **kw):
        return pp.begin(W, role="Reviewer", workspace=self.ws, vault=self.vault, **kw)

    def claim(self, name="reviewer"):
        """Begin, then have the backend create the agent: (agent id, claim id, the secret)."""
        begun = self.begin()
        agent, claim = provision_fake.created(self.backend, CORE, name, begun["claim_key_sha256"])
        return agent, claim, self.vault.store[CLAIM_KEY]

    def finish(self, claim_id, **kw):
        kw.setdefault("request", self.backend.request)
        kw.setdefault("preview", self.preview)
        kw.setdefault("sleep", self.backend.sleep)
        return pp.finish(W, claim_id, kw.pop("agent", WORKER_AGENT), workspace=self.ws, vault=self.vault, **kw)

    def failure(self, claim_id, **kw):
        """The message of a `finish` that must fail; the pending secret must be gone afterwards."""
        with self.assertRaises(pp.ProvisionError) as e:
            self.finish(claim_id, **kw)
        self.assertNotIn(CLAIM_KEY, self.vault.store, "the claim secret is deleted on failure")
        self.assertClean(str(e.exception))
        return str(e.exception)

    def assertClean(self, text):
        for value in self.vault.ever + [a["bearer"] for a in self.backend.agents.values() if a["bearer"]]:
            self.assertNotIn(C.secret_of(value) or value, text)


class Begin(Base):
    def test_the_secret_stays_in_the_vault_and_only_its_hash_and_a_name_come_back(self):
        out = self.begin()
        self.assertEqual(sorted(out), ["claim_key_sha256", "name"])
        secret = self.vault.store[CLAIM_KEY]
        self.assertRegex(secret, r"^[0-9a-f]{64}$")
        self.assertEqual(out["claim_key_sha256"], pp.claim_hash(secret))
        self.assertEqual(out["name"], "reviewer", "the server puts <owner>-<core>- in front of it")
        self.assertNotIn(secret, json.dumps(out))

    def test_a_second_begin_replaces_the_pending_secret(self):
        first = self.begin()
        self.assertNotEqual(self.begin()["claim_key_sha256"], first["claim_key_sha256"])
        self.assertEqual(len([k for k in self.vault.store if k.startswith(pp.CLAIM_KEY_PREFIX)]), 1)

    def test_refused_for_a_worker_that_already_has_an_agent_and_without_a_core_lane(self):
        with self.assertRaises(pp.ProvisionError) as e:
            self.begin(lane="no-such-lane")
        self.assertIn("no single core connection", str(e.exception))
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        _, claim, _ = self.claim()
        self.finish(claim)
        with self.assertRaises(pp.ProvisionError) as e:
            self.begin()
        self.assertIn("already has an agent of its own", str(e.exception))
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_a_vault_that_cannot_store_is_named_by_type_only(self):
        self.vault.fail_set = True
        with self.assertRaises(pp.ProvisionError) as e:
            self.begin()
        self.assertEqual(str(e.exception), "the claim secret could not be stored in the vault (RuntimeError)")

    def test_abort_forgets_the_pending_secret(self):
        self.begin()
        pp.abort(W, vault=self.vault)
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        self.vault.fail_delete = True
        with self.assertRaises(pp.ProvisionError) as e:
            pp.abort(W, vault=self.vault)
        self.assertIn("could not be deleted", str(e.exception))


class CoreLane(Base):
    def add_second(self):
        return self.add(H.TOKEN_B, fetch=H.agents("@sutando:beta-fixture.net"))[0]

    def test_the_lane_is_the_named_one_then_the_origin_then_the_signed_in_one_then_the_primary(self):
        second = self.add_second()
        self.assertEqual(pp.core_lane(self.ws)["instance"], "alpha-fixture-net", "the primary")
        self.assertEqual(pp.core_lane(self.ws, lane=second["instance"])["instance"], second["instance"])
        self.assertEqual(pp.core_lane(self.ws, origin="https://relay.beta-fixture.net:8443/x")["instance"],
                         second["instance"])
        with unittest.mock.patch.object(pp.gateway_serving, "active_lane", lambda: second["instance"]):
            self.assertEqual(pp.core_lane(self.ws)["instance"], second["instance"])
        with unittest.mock.patch.object(pp.gateway_serving, "active_lane", lambda: "not-a-row"):
            self.assertEqual(pp.core_lane(self.ws)["instance"], "alpha-fixture-net")

    def test_a_worker_row_a_disabled_row_and_an_unnamed_agent_are_never_it(self):
        _, claim, _ = self.claim()
        self.finish(claim)
        self.assertEqual(pp.core_lane(self.ws)["instance"], "alpha-fixture-net", "the worker's row is not a core lane")
        with self.assertRaises(pp.ProvisionError):
            pp.core_lane(self.ws, lane=C.worker_identity(W, self.ws)["instance"])
        record = json.loads(C.record_path(self.ws).read_text())
        record["connections"][0]["agent_mxid"] = None
        C.record_path(self.ws).write_text(json.dumps(record))
        with self.assertRaises(pp.ProvisionError) as e:
            pp.core_lane(self.ws)
        self.assertIn("has not learned its agent", str(e.exception))
        record["connections"][0].update(agent_mxid=CORE, disabled=True, primary=False)
        C.record_path(self.ws).write_text(json.dumps(record))
        with self.assertRaises(pp.ProvisionError) as e:
            pp.core_lane(self.ws)
        self.assertIn("connected core lanes: none", str(e.exception))


class Finish(Base):
    def test_redeems_as_the_core_saves_the_worker_connection_and_deletes_the_secret(self):
        agent, claim, secret = self.claim()
        out = self.finish(claim)
        self.assertEqual((out["agent_id"], out["status"]), (WORKER_AGENT, "ok"))
        self.assertEqual(agent, WORKER_AGENT)
        row = C.worker_identity(W, self.ws)
        self.assertEqual((row["worker"], row["agent_mxid"], row["token_ref"], row["primary"], row["added_by"]),
                         (W, WORKER_AGENT, WORKER_REF, False, "agent_link"))
        self.assertEqual(self.vault.store[WORKER_REF],
                         f"https://chat.alpha-fixture.net/relay|{self.backend.bearer_of(agent)}")
        self.assertNotIn(CLAIM_KEY, self.vault.store, "the claim secret is deleted on success")
        self.assertClean(json.dumps({k: v for k, v in out.items() if k != "row"}) + json.dumps(row))
        self.assertIn(f"POST /api/v1/agents/provision-claims/{claim}/redeem", self.backend.paths)

    def test_without_a_pending_secret_or_with_a_claim_id_no_server_issues(self):
        self.assertIn("no pending claim secret", self.failure("apc_X"))
        self.begin()
        self.assertIn("not one a server issues", self.failure("apc_X/../../v1/whoami"))
        self.assertNotIn("POST /v1/whoami", self.backend.paths)

    def test_a_refused_redeem_names_the_status_and_code_and_is_not_asked_again(self):
        self.begin()
        self.assertEqual(self.failure("apc_UNKNOWN"), "the claim was not redeemed: http 404 claim_not_found")
        self.assertEqual(len([p for p in self.backend.paths if p.endswith("/redeem")]), 1)
        _, claim, _ = self.claim()
        self.backend.sleep(self.backend.CLAIM_TTL_S + 1)
        self.assertEqual(self.failure(claim), "the claim was not redeemed: http 410 claim_expired (the claim is "
                                              "dead: retire the agent and create it again)")
        _, claim, _ = self.claim("second")
        self.vault.set(CLAIM_KEY, "too short")
        self.assertEqual(self.failure(claim, agent="@owner-desk-second.agent:alpha-fixture.net"),
                         "the claim was not redeemed: http 400 invalid_secret")

    def test_a_wrong_secret_is_refused_and_five_burn_the_claim(self):
        _, claim, _ = self.claim()
        for _ in range(4):
            self.vault.set(CLAIM_KEY, "0" * 64)
            self.assertIn("http 403 wrong_secret", self.failure(claim))
        self.vault.set(CLAIM_KEY, "0" * 64)
        self.failure(claim)
        self.vault.set(CLAIM_KEY, "0" * 64)
        self.assertIn("http 410 claim_burned", self.failure(claim))

    def test_a_server_that_gives_no_answer_or_a_5xx_is_asked_again_then_named(self):
        naps = []
        self.begin()
        self.assertEqual(self.failure("apc_X", request=lambda *a, **k: (502, {}, {}), sleep=naps.append),
                         "the claim was not redeemed: http 502")
        self.assertEqual(naps, [2, 4])
        self.begin()

        def down(*a, **k):
            raise pp.TransportError("ConnectionRefusedError")
        self.assertEqual(self.failure("apc_X", request=down, sleep=naps.append),
                         "the claim was not redeemed: no answer (ConnectionRefusedError)")
        _, claim, _ = self.claim()
        calls, naps = [], []

        def flaky(method, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                raise pp.TransportError("TimeoutError")
            if len(calls) == 2:
                return 503, {}, {"error": {"code": "UNAVAILABLE"}}
            return self.backend.request(method, url, **kw)
        self.assertEqual(self.finish(claim, request=flaky, sleep=naps.append)["agent_id"], WORKER_AGENT)
        self.assertEqual((len(calls), naps), (3, [2, 4]))

    def test_a_server_that_echoes_the_secret_or_the_bearer_cannot_put_it_in_the_message(self):
        _, claim, secret = self.claim()
        text = self.failure(claim, request=lambda *a, **k: (403, {}, {"code": k["payload"]["secret"]}))
        self.assertEqual(text, "the claim was not redeemed: http 403 [redacted]")
        self.begin()
        text = self.failure("apc_X", request=lambda *a, **k: (403, {}, {"code": k["bearer"]}))
        self.assertEqual(text, "the claim was not redeemed: http 403 [redacted]")
        self.begin()
        answer = {"relay_url": "https://chat.alpha-fixture.net/relay", "bearer": "b" * 48, "agent_id": "b" * 48}
        text = self.failure("apc_X", request=lambda *a, **k: (200, {}, answer))
        self.assertIn(f"answered for another agent than {WORKER_AGENT}", text)
        self.assertNotIn("b" * 48, text)

    def test_an_answer_that_is_incomplete_or_names_another_gateway_stores_nothing(self):
        for answer, said in (({"bearer": "b" * 48, "agent_id": WORKER_AGENT}, "lacks relay_url, bearer or agent_id"),
                             ({"relay_url": "https://evil.example.com", "bearer": "b" * 48,
                               "agent_id": WORKER_AGENT}, "a gateway that is not the core's")):
            self.begin()
            text = self.failure("apc_X", request=lambda *a, **k: (200, {}, answer))
            self.assertIn(said, text)
            self.assertIsNone(C.worker_identity(W, self.ws))
            self.assertNotIn(WORKER_REF, self.vault.store)

    def test_only_the_agent_that_agent_create_made_is_ever_saved(self):
        other = "@owner-elsewhere.agent:alpha-fixture.net"
        self.backend.add_agent(other, "c" * 48)
        for answered in ("agent-123", other, WORKER_AGENT):
            self.begin()
            answer = {"relay_url": "https://chat.alpha-fixture.net", "bearer": "c" * 48, "agent_id": answered}
            text = self.failure("apc_X", request=lambda *a, **k: (200, {}, answer))
            said = f"it names {other}" if answered == WORKER_AGENT else f"answered for another agent than {WORKER_AGENT}"
            self.assertIn(said, text, "another agent of the same owner is never adopted as this worker")
            self.assertIsNone(C.worker_identity(W, self.ws))
            self.assertNotIn(WORKER_REF, self.vault.store)
        for not_an_mxid in ("agent-123", "", "@no-server", "@bad id:alpha-fixture.net"):
            self.begin()
            before = len(self.backend.paths)
            self.assertIn("must be the one agent.create returned", self.failure("apc_X", agent=not_an_mxid))
            self.assertEqual(len(self.backend.paths), before, "nothing was asked of the server")

    def test_a_token_the_gateway_does_not_confirm_is_not_stored_and_the_orphan_is_named(self):
        agent, claim, _ = self.claim()
        text = self.failure(claim, preview=lambda token: {"verified": False, "error": "unreachable (resolution)"})
        self.assertIn(f"does not confirm the redeemed token is agent {agent}'s (unreachable (resolution))", text)
        self.assertIn(f"The claim's agent now has a token nobody holds: retire {agent} and provision again", text)
        self.assertNotIn(WORKER_REF, self.vault.store)
        second, claim, _ = self.claim("second")
        other = dict(self.preview(H.TOKEN_A))
        text = self.failure(claim, agent=second, preview=lambda token: other)
        self.assertIn(f"it names {CORE}", text)

    def test_an_agent_held_by_another_owner_is_refused_by_the_connection_rules(self):
        _, claim, _ = self.claim()
        self.backend.agents[WORKER_AGENT]["owner"] = "@mallory:alpha-fixture.net"
        text = self.failure(claim)
        self.assertIn("refused (worker_owner_mismatch)", text)
        self.assertNotIn(WORKER_REF, self.vault.store)

    def test_a_worker_that_already_has_an_agent_is_refused_before_anything_is_redeemed(self):
        _, claim, _ = self.claim()
        self.finish(claim)
        kept = self.vault.store[WORKER_REF]
        self.vault.set(CLAIM_KEY, "1" * 64)
        before = len(self.backend.paths)
        self.assertIn("already has an agent of its own; nothing was redeemed", self.failure(claim))
        self.assertEqual((len(self.backend.paths), self.vault.store[WORKER_REF]), (before, kept))

    def test_a_connection_that_appeared_during_the_redeem_keeps_its_own_token(self):
        _, claim, _ = self.claim()
        row = {"instance": "alpha-fixture-net-7c700e11"}
        with unittest.mock.patch.object(C, "add_worker_identity", lambda *a, **k: (row, False)):
            self.assertIn("already had a connection (alpha-fixture-net-7c700e11)", self.failure(claim))

    def test_an_interrupt_during_the_redeem_still_deletes_the_secret(self):
        _, claim, _ = self.claim()

        def interrupted(*a, **k):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.finish(claim, request=interrupted)
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        self.vault.fail_delete = True
        self.begin()
        with self.assertRaises(KeyboardInterrupt):
            self.finish(claim, request=interrupted)

    def test_a_secret_a_later_begin_wrote_is_not_the_one_deleted(self):
        _, claim, first = self.claim()

        def begun_again(method, url, **kw):
            self.begin()
            return self.backend.request(method, url, **kw)
        self.finish(claim, request=begun_again)
        self.assertNotIn(self.vault.store[CLAIM_KEY], (first, None), "the newer pending secret is kept")
        with unittest.mock.patch.object(self.vault, "lookup", side_effect=[first, RuntimeError("locked")]):
            with self.assertRaises(pp.ProvisionError):
                self.finish("apc_X", request=lambda *a, **k: (404, {}, {}))
        self.assertNotIn(CLAIM_KEY, self.vault.store, "a vault that cannot say is asked to delete all the same")

    def test_an_unexpected_error_and_a_secret_that_cannot_be_deleted_are_both_reported(self):
        self.begin()

        def boom(*a, **k):
            raise ValueError("carries " + self.vault.store.get(CLAIM_KEY, ""))
        self.vault.fail_delete = True
        with self.assertRaises(pp.ProvisionError) as e:
            self.finish("apc_X", request=boom)
        self.assertEqual(str(e.exception), "unexpected ValueError; the claim secret could not be deleted from "
                                           "the vault (RuntimeError)")
        self.vault.fail_delete = False
        _, claim, _ = self.claim()
        self.vault.fail_delete = True
        with self.assertRaises(pp.ProvisionError) as e:
            self.finish(claim)
        self.assertEqual(str(e.exception), "the worker's connection was saved, but the claim secret could not be "
                                           "deleted from the vault (RuntimeError): run provision-abort")
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], WORKER_AGENT)


class CreateAndWait(Base):
    """`provision`: the create call, the wait for the owner's approval, and the redeem, in one go."""

    def setUp(self):
        super().setUp()
        self.said, self.naps = [], []

    def nap(self, approve_after=None, decision="approved"):
        def sleep(seconds):
            self.naps.append(seconds)
            self.backend.now += seconds
            if approve_after is not None and len(self.naps) == approve_after:
                self.backend.approve(decision)
        return sleep

    def provision(self, operation_id="op-1", name="reviewer", **kw):
        kw.setdefault("request", self.backend.request)
        kw.setdefault("sleep", self.nap())
        return pp.provision(W, operation_id, name, workspace=self.ws, vault=self.vault, preview=self.preview,
                            clock=self.backend.clock, say=self.said.append, **kw)

    def refused(self, **kw):
        with self.assertRaises(pp.ProvisionError) as e:
            self.provision(**kw)
        self.assertClean(str(e.exception))
        return str(e.exception)

    def test_it_asks_again_until_the_owner_approves_then_redeems_at_once(self):
        self.begin()
        out = self.provision(sleep=self.nap(approve_after=2), display_name="Reviewer", role="reviews PRs")
        self.assertEqual((out["agent_id"], out["status"], self.naps), (WORKER_AGENT, "ok", [10, 10]))
        self.assertEqual(self.said, ["waiting up to 900s for the owner to approve agent.create"])
        (op,) = self.backend.operations.values()
        self.assertEqual(op["arguments"], {"name": "reviewer", "display_name": "Reviewer", "role": "reviews PRs",
                                           "claim_key_sha256": op["arguments"]["claim_key_sha256"]})
        (claim,) = self.backend.claims.values()
        self.assertEqual(claim["redeemed_at"], self.backend.now, "redeemed in the same breath as the approval")
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], WORKER_AGENT)
        self.assertEqual(self.backend.paths.count("POST /mcp"), 3, "the first call and two more, no other tool")

    def test_the_lane_that_made_the_call_redeems_even_if_the_signed_in_lane_changes_meanwhile(self):
        other = self.add(H.TOKEN_B, fetch=H.agents("@owner-desk.agent:beta-fixture.net"))[0]
        signed_in = ["alpha-fixture-net"]

        def switch_then_approve(seconds):
            signed_in[0] = other["instance"]
            self.backend.now += seconds
            self.backend.approve()
        self.begin()
        with unittest.mock.patch.object(pp.gateway_serving, "active_lane", lambda: signed_in[0]):
            out = self.provision(sleep=switch_then_approve)
            self.assertEqual(pp.core_lane(self.ws)["instance"], other["instance"], "the default did move")
        self.assertEqual((out["agent_id"], signed_in[0]), (WORKER_AGENT, other["instance"]))
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], WORKER_AGENT)
        self.assertNotIn("claim_not_found", " ".join(self.backend.paths))

    def test_a_wait_that_times_out_keeps_the_secret_and_the_same_call_picks_it_up(self):
        self.begin()
        text = self.refused(timeout=12)
        self.assertEqual(text, "the owner has not approved agent.create within 12s. The pending secret is kept: run "
                               "provision-wait again with the same operation id and arguments and --lane "
                               "'alpha-fixture-net' while the request is open, or provision-abort to give up")
        self.assertIn(CLAIM_KEY, self.vault.store)
        self.backend.approve()
        self.assertEqual(self.provision()["agent_id"], WORKER_AGENT)
        self.assertEqual(len(self.backend.operations), 1, "one agent, however often it was asked for")

    def test_a_server_that_cannot_ask_the_owner_refuses_and_the_secret_is_forgotten(self):
        self.backend.mode = "none"
        self.begin()
        self.assertEqual(self.refused(), "agent.create needs the owner's approval, which this server cannot ask for "
                                         "yet (approval_required); nothing was created and the pending secret is "
                                         "forgotten")
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        self.assertEqual(list(self.backend.agents), [CORE])
        self.begin()
        self.vault.fail_delete = True
        self.assertTrue(self.refused().endswith("; the claim secret could not be deleted from the vault (RuntimeError)"))

    def test_a_denial_and_each_refusal_name_the_reason_and_forget_the_secret(self):
        self.begin()
        self.assertIn("agent.create was not approved (no reason given)", self.refused(sleep=self.nap(1, "denied")))
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        self.begin()
        self.assertIn("agent.create was refused (operation_claimed)", self.refused(name="another-name"))
        self.backend.CREATES_PER_HOUR = 1
        self.begin()
        self.assertIn("agent.create was refused (rate_limited)", self.refused(operation_id="op-2"))
        self.backend.agents[CORE]["created_by"] = "@owner-other.agent:alpha-fixture.net"
        self.begin()
        self.assertIn("agent.create was refused (worker_cannot_create)", self.refused(operation_id="op-3"))
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_a_held_call_answered_as_an_error_and_a_refusal_that_says_retry_are_waited_out(self):
        self.backend.bugs.add("held_as_error")
        self.begin()
        self.assertEqual(self.provision(sleep=self.nap(approve_after=1))["agent_id"], WORKER_AGENT)
        self.setUp()
        self.backend.mode = "grant"
        self.begin()
        calls = []

        def flaky(method, url, **kw):
            if url.endswith("/mcp"):
                calls.append(url)
                if len(calls) == 1:
                    retry = {"code": "UNAVAILABLE", "details": {"reason": "mint_failed"}}
                    return 200, {}, {"result": {"isError": True, "structuredContent": retry}}
            return self.backend.request(method, url, **kw)
        self.assertEqual(self.provision(request=flaky)["agent_id"], WORKER_AGENT)
        self.assertEqual((len(calls), self.naps), (2, [10]))

    def test_what_stops_it_before_the_call_keeps_the_secret(self):
        self.assertIn("no pending claim secret", self.refused())
        self.begin()
        self.assertIn("lowercase letters, digits and inner dashes", self.refused(name="Not A Name"))
        self.assertIn("no single core connection", self.refused(lane="no-such-lane"))
        text = self.refused(request=lambda method, url, **kw: (500, {}, {"code": kw.get("bearer") or "x"}))
        self.assertEqual(text, "MCP discovery answered http 500 with no mint endpoint on the gateway's own origin")
        self.assertIn(CLAIM_KEY, self.vault.store)
        self.assertEqual(self.backend.operations, {})

    def test_an_answer_without_an_agent_and_a_claim_is_not_redeemed(self):
        self.begin()

        def empty(method, url, **kw):
            if url.endswith("/mcp"):
                return 200, {}, {"result": {"structuredContent": {"result": {"owner_notified": True}}}}
            return self.backend.request(method, url, **kw)
        self.assertIn("agent.create answered without an agent and a claim (no reason given)", self.refused(request=empty))
        self.assertNotIn("POST /api/v1/agents/provision-claims", " ".join(self.backend.paths))


class Retire(Base):
    def provisioned(self):
        agent, claim, _ = self.claim()
        self.finish(claim)
        return agent

    def retire(self, **kw):
        return pp.retire(W, workspace=self.ws, vault=self.vault, preview=self.preview, **kw)

    def test_nothing_is_removed_while_the_gateway_still_accepts_the_token(self):
        self.provisioned()
        with self.assertRaises(pp.NotRetired) as e:
            self.retire()
        self.assertIn(f"still accepts its token for {WORKER_AGENT}", str(e.exception))
        self.assertIn(WORKER_REF, self.vault.store)
        self.assertIsNotNone(C.worker_identity(W, self.ws))

    def test_once_the_server_retired_it_the_secret_and_the_row_go(self):
        agent = self.provisioned()
        self.backend.agents[agent]["deleted"] = True
        self.assertEqual(self.retire(), {"agent_id": WORKER_AGENT, "status": "retired"})
        self.assertNotIn(WORKER_REF, self.vault.store)
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertEqual(self.retire(), {"agent_id": None, "status": "no_identity"})

    def test_no_verdict_is_not_a_retirement_unless_the_caller_removes_locally_anyway(self):
        self.provisioned()

        def unreachable(token):
            raise C.InvalidToken("invalid_gateway")
        with self.assertRaises(pp.NotRetired) as e:
            pp.retire(W, workspace=self.ws, vault=self.vault, preview=unreachable)
        self.assertIn("gave no verdict (InvalidToken)", str(e.exception))
        del self.vault.store[WORKER_REF]
        with self.assertRaises(pp.NotRetired) as e:
            self.retire()
        self.assertIn("gave no verdict (no token in the vault)", str(e.exception))
        self.assertEqual(self.retire(local_anyway=True), {"agent_id": WORKER_AGENT, "status": "removed_locally"})
        self.assertIsNone(C.worker_identity(W, self.ws))

    def test_a_vault_that_cannot_be_read_or_still_holds_the_token_is_an_error(self):
        agent = self.provisioned()
        self.backend.agents[agent]["deleted"] = True
        with unittest.mock.patch.object(self.vault, "lookup", side_effect=RuntimeError("locked")):
            with self.assertRaises(pp.ProvisionError) as e:
                self.retire()
        self.assertEqual(str(e.exception), "the vault could not be read (RuntimeError); nothing removed")
        with unittest.mock.patch.object(self.vault, "delete", lambda key: None):
            with self.assertRaises(pp.ProvisionError) as e:
                self.retire()
        self.assertIn(f"still holds {WORKER_REF}", str(e.exception))


class Transport(unittest.TestCase):
    def setUp(self):
        self.backend = provision_fake.FakeBackend()
        self.backend.add_agent(CORE, "secret-alpha")

    def test_json_in_and_out_over_real_http_with_the_bearer_and_headers(self):
        claim_secret = "a" * 64
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", pp.claim_hash(claim_secret))
        with provision_fake.serve(self.backend) as base:
            status, headers, body = pp.http_json("POST", base + pp.REDEEM_PATH.format(claim_id=claim),
                                                 bearer="secret-alpha", payload={"secret": claim_secret})
            self.assertEqual((status, headers["cache-control"], body["agent_id"]), (200, "no-store", WORKER_AGENT))
            again = pp.http_json("POST", base + pp.REDEEM_PATH.format(claim_id=claim), payload={"secret": claim_secret},
                                 bearer="https://chat.alpha-fixture.net/relay|secret-alpha")
            self.assertEqual((again[0], again[2]["bearer"]), (200, body["bearer"]), "the url|secret pair is accepted")
            self.assertEqual(pp.http_json("GET", base + "/relay/v1/whoami", bearer="secret-alpha")[2], {"agent_mxid": CORE})
            self.assertEqual(pp.http_json("GET", base + "/v1/whoami", bearer="secret-alpha")[2], {"agent_mxid": CORE})
            self.assertEqual(pp.http_json("GET", base + "/v1/whoami")[0], 401)
            self.assertEqual(pp.http_json("POST", base + "/api/v1/mcp/agent-access-tokens", bearer="nope")[0], 401)
            self.assertEqual(pp.http_json("GET", base + "/text", bearer="secret-alpha")[2], {})

    def test_a_redirect_is_not_followed_and_cleartext_or_a_dead_port_is_refused(self):
        with provision_fake.serve(self.backend) as base:
            self.assertEqual(pp.http_json("GET", base + "/redirect", bearer="secret-alpha")[0], 302)
            self.assertNotIn("GET /v1/whoami", self.backend.paths, "the bearer did not follow the redirect")
        for url in ("http://chat.alpha-fixture.net/v1/whoami", "ftp://127.0.0.1/x", "https:///x"):
            with self.assertRaises(pp.ProvisionError) as e:
                pp.http_json("GET", url, bearer="secret-alpha")
            self.assertNotIsInstance(e.exception, pp.TransportError)
        with self.assertRaises(pp.TransportError) as e:
            pp.http_json("GET", base + "/v1/whoami", timeout=2)
        self.assertRegex(str(e.exception), r"^[A-Za-z]+$")


class Small(unittest.TestCase):
    def test_scrub_and_error_code(self):
        self.assertEqual(pp.scrub("a SECRET b SECRET", ["SECRET", ""]), "a [redacted] b [redacted]")
        self.assertEqual(pp.error_code({"code": "claim_expired"}), "claim_expired")
        self.assertEqual(pp.error_code({"code": "FORBIDDEN", "details": {"reason": "wrong_secret"}}), "wrong_secret")
        self.assertEqual(pp.error_code({"error": {"code": "GONE", "details": {"reason": "claim_burned"}}}), "claim_burned")
        self.assertEqual(pp.error_code({"code": "FORBIDDEN", "details": {"reason": "free text here"}}), "FORBIDDEN")
        self.assertEqual(pp.error_code({"error": "nope"}), "nope")
        self.assertEqual(pp.error_code({"error": {"code": "X.Y"}}), "X.Y")
        self.assertEqual(pp.error_code({"code": "free text is the server's"}), "")
        self.assertEqual(pp.error_code({"message": "no code"}), "")

    def test_origin_bearer_and_key(self):
        self.assertEqual(pp.origin_of("HTTPS://Chat.Example.com:443/relay"), "https://chat.example.com")
        self.assertEqual(pp.origin_of("http://[::1]:8080/x"), "http://[::1]:8080")
        for bad in ("", "ftp://x", "https://", "https://h:bad/"):
            self.assertEqual(pp.origin_of(bad), "", bad)
        self.assertEqual(pp.bearer_of("https://chat.example.com|abc"), "abc")
        self.assertEqual(pp.bearer_of(" bare "), "bare")
        self.assertEqual(pp.claim_key("ab-12"), "AG2_PROVISION_CLAIM_AB_12")
        self.assertIsNone(pp.held(H.FakeVault(), "ABSENT"), "a vault with only get() reads absent as None")

    def test_the_suggested_name_is_one_the_server_accepts(self):
        self.assertEqual(pp.suggested_name("Data Eng!"), "data-eng")
        long = pp.suggested_name("a very long role name that goes on - and on and on")
        self.assertTrue(len(long) <= 32 and pp.NAME_RE.fullmatch(long), long)
        self.assertEqual(pp.suggested_name("!!!"), "")
        self.assertEqual(pp.create_arguments("data-eng", "a" * 64, role="PR shepherd"),
                         {"name": "data-eng", "claim_key_sha256": "a" * 64, "role": "PR shepherd"})
        for bad in ("", "Data", "-x", "x-", "a" * 33, "owner/kind"):
            with self.assertRaises(pp.ProvisionError):
                pp.create_arguments(bad, "a" * 64)

    def test_find_reads_the_shallowest_value(self):
        env = {"result": {"agent_id": "@a:x"}, "authority": {"source": "approval", "agent_id": "@deep:x"}}
        self.assertEqual((pp.find(env, "agent_id"), pp.find(env, "source"), pp.find(env, "absent")),
                         ("@a:x", "approval", None))
        self.assertIsNone(pp.find({"a": {"b": {"c": {"d": {"e": 1}}}}}, "e"), "bounded depth")

    def test_envelope_prefers_structured_content_then_the_last_json_text(self):
        self.assertEqual(pp.envelope({"structuredContent": {"a": 1}, "content": [{"text": '{"b": 2}'}]}), {"a": 1})
        content = [{"text": '{"first": 1}'}, {"text": "Done."}, {"text": "[1]"}, {"no": "text"}, None]
        self.assertEqual(pp.envelope({"content": content}), {"first": 1})
        self.assertEqual(pp.envelope({}), {})

    def test_approval_state(self):
        refused = {"is_error": True, "code": "FORBIDDEN"}
        for env, state in (
                ({"result": {"claim_id": "apc_1"}, "authority": {"source": "approval", "approval_id": "ap_1"}}, "done"),
                ({"status": "succeeded"}, "done"), ({"event_id": "$1"}, "done"),
                ({"status": "approval_required", "approval_id": "ap_1"}, "held"), ({"status": "pending"}, "held"),
                ({**refused, "details": {"reason": "approval_required", "approval_reason": "agent_create"}}, "fail_closed"),
                ({**refused, "details": {"reason": "approval_required", "approval_id": "ap_1"}}, "held"),
                ({"is_error": True, "code": "UNAVAILABLE", "message": "Action approval coordination is not "
                                                                      "available"}, "fail_closed"),
                ({"is_error": True, "code": "UNAVAILABLE", "details": {"reason": "mint_failed"}}, "retry"),
                ({"is_error": True, "code": "UNAVAILABLE", "details": {"reason": "soft_delete_incomplete"}}, "retry"),
                ({**refused, "details": {"reason": "not_a_core"}}, "refused"),
                ({"status": "denied"}, "denied")):
            self.assertEqual(pp.approval_state(env), state, env)

    def test_the_real_vault_adapter_binds_to_the_secret_vault_module(self):
        calls = []
        fake = unittest.mock.Mock(lookup_vault_key=lambda k: calls.append(("lookup", k)) or "v",
                                  set_vault_key=lambda k, v: calls.append(("set", k)),
                                  delete_vault_key=lambda k: calls.append(("delete", k)))
        with unittest.mock.patch.dict(sys.modules, {"vault_intercept": fake}):
            vault = pp._vault(None)
            self.assertEqual(vault.lookup("K"), "v")
            vault.set("K", "v")
            vault.delete("K")
        self.assertEqual(calls, [("lookup", "K"), ("set", "K"), ("delete", "K")])
        self.assertIs(pp._vault(fake), fake)


class Verbs(Base):
    """`pool_identity.py`'s provisioning verbs: one JSON line each, never a secret."""

    def setUp(self):
        super().setUp()
        self.started = []
        own = provision_fake.KeychainShaped(self.vault)
        for target, name, fake in ((pp, "_vault", lambda v=None: own), (C, "_vault", lambda v=None: v or self.vault),
                                   (pp, "http_json", self.backend.request), (C, "preview_connection", self.preview),
                                   (C, "start_added", lambda row, **kw: self.started.append(kw) or {"ok": True})):
            p = unittest.mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pool_identity.main(["--workspace", str(self.ws), *argv])
        self.assertClean(out.getvalue() + err.getvalue())
        return rc, out.getvalue(), err.getvalue()

    def begun(self):
        rc, out, _ = self.run_cmd("provision-begin", "Reviewer")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)
        return json.loads(out)

    def test_begin_prints_only_the_hash_and_the_name(self):
        out = self.begun()
        self.assertEqual(out, {"claim_key_sha256": pp.claim_hash(self.vault.store[CLAIM_KEY]), "name": "reviewer"})
        rc, text, _ = self.run_cmd("provision-begin", "Reviewer", "--role", "QA lead", "--lane", "alpha-fixture-net")
        self.assertEqual((rc, json.loads(text)["name"]), (0, "qa-lead"))

    def test_finish_prints_the_agent_id_and_status_and_starts_the_lane(self):
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", self.begun()["claim_key_sha256"])
        rc, out, err = self.run_cmd("provision-finish", "Reviewer", "--claim-id", claim, "--agent-id", WORKER_AGENT)
        self.assertEqual((rc, json.loads(out)), (0, {"agent_id": WORKER_AGENT, "status": "ok", "seat": "skipped"}), err)
        self.assertEqual(self.started[0]["logs_dir"], self.ws / "logs")
        self.assertEqual(self.started[0]["repo"], ROOT)
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_finish_reports_a_lane_that_did_not_start_and_can_skip_starting(self):
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", self.begun()["claim_key_sha256"])
        with unittest.mock.patch.object(C, "start_added", lambda row, **kw: {"ok": False, "reason": "no python"}):
            rc, out, err = self.run_cmd("provision-finish", "Reviewer", "--claim-id", claim, "--agent-id", WORKER_AGENT)
        self.assertEqual((rc, json.loads(out)["status"]), (1, "stored_lane_not_started"))
        self.assertIn("not started: no python", err)
        self.backend.agents[WORKER_AGENT]["deleted"] = True
        self.assertEqual(self.run_cmd("retire-remote", "Reviewer")[0], 0)
        self.backend.agents.pop(WORKER_AGENT)
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", self.begun()["claim_key_sha256"])

        def refuses(row, **kw):
            raise C.LaunchFailed("no bridge script")
        with unittest.mock.patch.object(C, "start_added", refuses):
            rc, out, err = self.run_cmd("provision-finish", "Reviewer", "--claim-id", claim, "--agent-id", WORKER_AGENT)
        self.assertEqual((rc, json.loads(out)), (1, {"agent_id": WORKER_AGENT, "status": "stored_lane_not_started",
                                                   "seat": "skipped"}))
        self.assertIn("not started: no bridge script", err)
        self.backend.agents[WORKER_AGENT]["deleted"] = True
        self.assertEqual(self.run_cmd("retire-remote", "Reviewer")[0], 0)
        self.backend.agents.pop(WORKER_AGENT)
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", self.begun()["claim_key_sha256"])
        rc, out, _ = self.run_cmd("provision-finish", "Reviewer", "--claim-id", claim, "--agent-id", WORKER_AGENT, "--no-start")
        self.assertEqual((rc, json.loads(out)["status"], len(self.started)), (0, "ok", 0))

    def test_a_failed_finish_exits_1_names_the_failure_and_deletes_the_secret(self):
        self.begun()
        rc, out, err = self.run_cmd("provision-finish", "Reviewer", "--claim-id", "apc_UNKNOWN", "--agent-id",
                                    WORKER_AGENT)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.strip(), "pool_identity: the claim was not redeemed: http 404 claim_not_found")
        self.assertNotIn(CLAIM_KEY, self.vault.store)

    def test_wait_makes_the_call_redeems_and_prints_the_agent_id_and_status(self):
        self.backend.mode = "grant"
        self.begun()
        rc, out, err = self.run_cmd("provision-wait", "Reviewer", "--operation-id", "op-9", "--name", "reviewer",
                                    "--display-name", "Reviewer", "--role", "reviews PRs")
        self.assertEqual((rc, json.loads(out), err), (0, {"agent_id": WORKER_AGENT, "status": "ok", "seat": "skipped"}, ""))
        self.assertEqual(self.backend.operations["op-9"]["arguments"]["role"], "reviews PRs")
        self.assertEqual(len(self.started), 1)

    def test_wait_says_it_is_waiting_and_gives_up_at_the_timeout_keeping_the_secret(self):
        self.begun()
        rc, out, err = self.run_cmd("provision-wait", "Reviewer", "--operation-id", "op-9", "--name", "reviewer",
                                    "--timeout", "0")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("waiting up to 0s for the owner to approve agent.create", err)
        self.assertIn("pool_identity: the owner has not approved agent.create within 0s", err)
        self.assertIn(CLAIM_KEY, self.vault.store)

    def test_abort_and_retire_remote(self):
        self.begun()
        rc, out, _ = self.run_cmd("provision-abort", "Reviewer")
        self.assertEqual((rc, json.loads(out)), (0, {"status": "aborted"}))
        self.assertNotIn(CLAIM_KEY, self.vault.store)
        rc, out, _ = self.run_cmd("retire-remote", "Reviewer")
        self.assertEqual((rc, json.loads(out)["status"]), (0, "no_identity"))
        _, claim = provision_fake.created(self.backend, CORE, "reviewer", self.begun()["claim_key_sha256"])
        self.run_cmd("provision-finish", "Reviewer", "--claim-id", claim, "--agent-id", WORKER_AGENT)
        rc, out, err = self.run_cmd("retire-remote", "Reviewer")
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("retire it there first (agent.retire); nothing removed", err)
        self.assertIn(WORKER_REF, self.vault.store)
        rc, out, _ = self.run_cmd("retire-remote", "Reviewer", "--local-anyway")
        self.assertEqual((rc, json.loads(out)["status"]), (1, "removed_locally"))
        self.assertNotIn(WORKER_REF, self.vault.store)


class Collected(unittest.TestCase):
    def test_every_test_in_this_file_is_above_the_runner(self):
        source = Path(__file__).read_text()
        self.assertEqual(len(re.findall(r"^    def test_", source, re.M)), 51)
        self.assertTrue(source.rstrip().endswith("unittest.main()"))


if __name__ == "__main__":
    unittest.main()
