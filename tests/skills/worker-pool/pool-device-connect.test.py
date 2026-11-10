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

    def approve(self):
        self.approved = True

    def request(self, method, url, *, bearer="", payload=None, headers=None, timeout=None):
        origin, _, path = url.partition("://")[2].partition("/")
        if "/" + path == dc.START_PATH and method == "POST":
            self.started.append(dict(payload or {}))
            code = "dc-" + secrets.token_hex(16)
            self.codes[code] = self.backend.now + self.expires_in
            link = f"{self.link_host}/api/connect-page?code={code if self.link_with_code else 'U-' + code[-6:]}"
            return 200, {}, {"device_code": code, "verify_url": link, "interval": 3, "expires_in": self.expires_in}
        if "/" + path == dc.POLL_PATH and method == "POST":
            code = (payload or {}).get("device_code")
            if code not in self.codes or self.backend.now > self.codes[code] + 30:
                return 404, {}, {"error": "unknown_code"}
            if not self.approved:
                return 202, {}, {"status": "pending"}
            if self.bearer is None:
                self.bearer = "bearer-" + secrets.token_hex(16)
                self.backend.add_agent(self.agent, self.bearer, owner=self.owner)
            del self.codes[code]
            return 200, {}, {"relay_url": self.backend.origin + "/relay", "bearer": self.bearer,
                             "agent_id": self.agent}
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
        self.assertEqual(fake.started, [{"label": "reviewer", "display_name": "Reviewer"}])
        row = C.worker_identity(W, self.ws)
        self.assertEqual(row["agent_mxid"], NEW_AGENT)
        self.assertEqual(C.secret_of(self.vault.store[row["token_ref"]]), fake.bearer)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("https://alpha-fixture.net/api/connect-page?code=U-", self.sent[0])
        self.assertIn("It works until", self.sent[0])
        self.assertTrue(self.sent[0].startswith("[dm-only]"))
        self.assertNoSecret(fake, json.dumps({k: v for k, v in out.items() if k != "row"}), self.sent[0])

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
        self.assertEqual(json.loads(out[-1]), {"agent_id": NEW_AGENT, "status": "ok"})
        self.assertNoSecret(fake, *out)

    def test_a_pasted_token_still_needs_repo_and_logs_dir(self):
        with self.assertRaises(SystemExit):
            with unittest.mock.patch("sys.stderr"):
                pool_identity.main(["--workspace", str(self.ws), "add", "Reviewer", "-"])


if __name__ == "__main__":
    unittest.main()
