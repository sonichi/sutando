#!/usr/bin/env python3
"""A worker's own s.space identity across its life: create, stop/start, retire.

- `create_worker.py` starts the identity step by default, as `pool_identity.py add <worker>
  --device-flow --label <label>` in the background, and returns once the owner was sent the link.
  Once approved, the worker is on its agent's own folders before its lane starts.
  `--no-identity` (or WORKER_POOL_IDENTITY_ON_CREATE=0) skips it, and a step that cannot start
  still creates the worker, which answers through the core.
- The label is the worker's name as a device-connect label, made unique against the agents this
  host knows.
- Stop, enable and reseat keep the worker's identity: it comes back as its own agent.
- Retire removes the identity here, and tells the owner to delete the agent in the app.

The device-connect server is the in-process fake from pool-device-connect.test.py.

Run: python3 tests/skills/worker-pool/worker-identity-lifecycle.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("pool_device_connect_suite",
                                               Path(__file__).with_name("pool-device-connect.test.py"))
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)  # hermetic env, a core lane, the vault and the device-connect fake
S, C, dc = D.S, D.C, D.dc
import create_worker as cw
import pool_identity
import pool_identity_request as pir
import pool_manage
import pool_roster as pr
import spawn_worker as sw

W, NEW_AGENT, DM = D.W, D.NEW_AGENT, D.DM


class FakeChild:
    """What `_spawn` returns, for a child that runs in a thread of this process."""

    def __init__(self, thread=None, rc=None):
        self.thread, self.rc = thread, rc

    def poll(self):
        if self.thread is not None and self.thread.is_alive():
            return None
        return self.rc() if callable(self.rc) else self.rc


class Base(D.Base):
    def setUp(self):
        super().setUp()
        core = dc.pp.core_lane(self.ws, None)
        (self.ws / "state" / f"owner-routing.{core['instance']}.json").write_text(json.dumps(
            {"owner_dm": DM, "private": True, "identity": core.get("agent_mxid")}))
        self.made = []

        def fake_spawn(workspace, repo, **kw):
            wid = f"{len(self.made) + 1:032x}"
            self.made.append(wid)
            return {"worker_id": wid, "label": kw.get("label") or wid, "runtime": "claude",
                    "cwd": str(repo), "delivery_dir": str(Path(workspace) / "deliveries" / wid),
                    "tmux": {"socket": "/tmp/s.sock", "session_name": f"w-{wid}"}}
        for name, value in (("spawn", fake_spawn), ("per_instance_sentinel_supported", lambda repo: True),
                            ("core_runtime", lambda repo, runner=None: "claude")):
            p = unittest.mock.patch.object(sw, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.identity_argvs = []

    def create(self, *args, child=None):
        """create_worker's text output and rc, with the identity child replaced by `child(argv, log)`."""
        def spawn(argv, log):
            self.identity_argvs.append(argv)
            return child(argv, log) if child else FakeChild(rc=0)
        out, self.err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(pir, "_spawn", spawn), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(self.err):
            rc = cw.main(["--workspace", str(self.ws), "--repo", str(ROOT), "--identity-wait", "5", *args])
        return rc, out.getvalue() + self.err.getvalue()

    def identity_line(self, text):
        return next(ln.strip() for ln in text.splitlines() if ln.strip().startswith("identity "))

    def device_flow_child(self, release: threading.Event):
        """A child that runs the real `pool_identity.main` on its argv against the fake server; the owner
        approves once `release` is set."""
        fake = D.ConnectFake(self.backend)
        self.fake = fake
        real = dc.connect

        def wired(worker, **kw):
            def sleep(s):
                release.wait(10)
                self.backend.sleep(s)
                fake.approve()
            kw.update(request=fake.request, preview=self.preview, deliver=self.sent.append, vault=self.vault,
                      sleep=sleep, clock=self.backend.clock)
            return real(worker, **kw)
        import worker_lane
        patches = [unittest.mock.patch.object(dc, "connect", wired),
                   unittest.mock.patch.object(worker_lane, "handler_lookup", lambda ws: ("", "")),
                   unittest.mock.patch.object(C, "start_added", lambda row, **kw: {"ok": True}),
                   unittest.mock.patch.object(pool_manage, "reseat", lambda ws, repo, wid, **kw: {"result": "skipped"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        def child(argv, log):
            args = argv[2:]
            def run():
                # The child's stderr goes to its log, as the real spawn sends it.
                with open(log, "a", encoding="utf-8") as f, \
                        unittest.mock.patch.object(pool_identity, "sys", types.SimpleNamespace(
                            stderr=f, stdin=sys.stdin, exit=sys.exit)):
                    self.child_rc = pool_identity.main(args)
            self.child_rc = None
            t = self.child_thread = threading.Thread(target=run, daemon=True)
            t.start()
            return FakeChild(t, rc=lambda: self.child_rc)
        return child


class ByDefault(Base):
    def test_a_new_worker_gets_an_approval_link_and_creation_returns_before_the_approval(self):
        release = threading.Event()
        rc, text = self.create("--label", "Code Reviewer", child=self.device_flow_child(release))
        self.assertEqual(rc, 0, text)
        wid = self.made[0]
        argv = self.identity_argvs[0]
        self.assertEqual(argv[1:8], [str(Path(pir.__file__).with_name("pool_identity.py")), "--workspace",
                                     str(self.ws), "add", wid, "--device-flow", "--label"])
        self.assertEqual(argv[8], "code-reviewer")
        self.assertIn("--display-name", argv)
        self.assertNotIn("--shared-dirs", argv)
        rec = dc.read_record(self.ws, wid)
        self.assertEqual(rec["state"], "pending", "creation returned while the approval was still open")
        until = time.strftime("%H:%M", time.localtime(rec["expires_at"]))
        self.assertEqual(self.identity_line(text),
                         f'identity  requested (link expires {until}; agent "code-reviewer")')
        self.assertIsNone(C.worker_identity(wid, self.ws))
        self.assertIn(wid, pr.load_roster(self.ws)["workers"])
        child_said = io.StringIO()
        with contextlib.redirect_stdout(child_said):
            release.set()
            self.child_thread.join(10)
        self.assertEqual(self.child_rc, 0)
        self.assertEqual(C.worker_identity(wid, self.ws)["agent_mxid"], NEW_AGENT)
        self.assertEqual(self.fake.started[0]["label"], "code-reviewer")
        root = C.worker_root(NEW_AGENT, self.ws)
        self.assertEqual(C.worker_dirs(wid, self.ws), (root / "tasks", root / "results"))
        self.assertEqual(sw.inbox_env(self.ws, wid)["SUTANDO_TASKS_DIR"], str(root / "tasks"))

    def test_json_output_carries_the_identity_status(self):
        def child(argv, log):
            dc._keep(self.ws, argv[5], {"label": argv[8], "state": "pending", "expires_at": time.time() + 900})
            return FakeChild()
        out = io.StringIO()
        with unittest.mock.patch.object(pir, "_spawn", lambda argv, log: child(argv, log)), \
                contextlib.redirect_stdout(out):
            rc = cw.main(["--workspace", str(self.ws), "--repo", str(ROOT), "--label", "triage", "--json"])
        got = json.loads(out.getvalue())
        self.assertEqual((rc, got["identity"]["status"], got["identity"]["label"]), (0, "requested", "triage"))
        self.assertTrue(got["identity_line"].startswith("requested (link expires "))

    def test_a_dry_run_says_it_would_request_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cw.main(["--workspace", str(self.ws), "--repo", str(ROOT), "--label", "x", "--dry-run"])
        self.assertTrue(json.loads(out.getvalue())["would_request_identity"])


class OptOut(Base):
    def test_no_identity_skips_the_step(self):
        rc, text = self.create("--label", "triage", "--no-identity")
        self.assertEqual((rc, self.identity_argvs), (0, []))
        self.assertEqual(self.identity_line(text), "identity  skipped (--no-identity)")
        self.assertEqual(dc.read_record(self.ws, self.made[0]), {})
        self.assertIsNone(C.worker_dirs(self.made[0], self.ws))
        self.assertEqual(sw.inbox_env(self.ws, self.made[0])["SUTANDO_INBOX_KIND"], "deliveries")

    def test_the_manifest_key_turned_off_in_the_env_skips_it(self):
        with unittest.mock.patch.dict("os.environ", {pir.FLAG: "0"}):
            rc, text = self.create("--label", "triage")
        self.assertEqual((rc, self.identity_argvs), (0, []))
        self.assertEqual(self.identity_line(text), f"identity  skipped ({pir.FLAG} is off)")

    def test_the_manifest_declares_it_on(self):
        cfg = json.loads((ROOT / "skills/worker-pool/manifest.json").read_text())["config"]
        self.assertEqual(cfg[pir.FLAG], "1")


class CannotStart(Base):
    def assertCreatedWithout(self, rc, text, why):
        self.assertEqual(rc, 0, text)
        self.assertIn(self.made[0], pr.load_roster(self.ws)["workers"])
        self.assertIsNone(C.worker_identity(self.made[0], self.ws))
        self.assertIn(why, self.identity_line(text))
        self.assertTrue(self.identity_line(text).startswith("identity  skipped ("))

    def test_a_process_that_does_not_start(self):
        def child(argv, log):
            raise OSError("no exec")
        rc, text = self.create("--label", "triage", child=child)
        self.assertCreatedWithout(rc, text, "the identity process did not start (OSError)")

    def test_a_device_connect_the_server_refuses_names_the_reason(self):
        def child(argv, log):
            log.write_text("pool_identity: the server did not start a device connect (HTTP 429, rate_limited)\n"
                           '{"status": "failed"}\n')
            return FakeChild(rc=1)
        rc, text = self.create("--label", "triage", child=child)
        self.assertCreatedWithout(rc, text, "the server did not start a device connect (HTTP 429, rate_limited)")

    def test_the_real_device_flow_refused_by_the_fake_server(self):
        release = threading.Event()
        child = self.device_flow_child(release)
        self.fake.start_status = 503
        rc, text = self.create("--label", "triage", child=child)
        release.set()
        self.assertEqual(rc, 0, text)
        self.assertIn("skipped (", self.identity_line(text))
        self.assertIn("HTTP 503", self.identity_line(text))

    def test_no_owner_dm(self):
        core = dc.pp.core_lane(self.ws, None)
        (self.ws / "state" / f"owner-routing.{core['instance']}.json").unlink()
        rc, text = self.create("--label", "triage")
        self.assertCreatedWithout(rc, text, "has no owner DM")
        self.assertEqual(self.identity_argvs, [])

    def test_no_core_lane(self):
        with unittest.mock.patch.object(dc.pp, "core_lane", side_effect=dc.pp.ProvisionError("no single core")):
            rc, text = self.create("--label", "triage")
        self.assertCreatedWithout(rc, text, "no core lane to ask as: no single core")

    def test_anything_unexpected_is_reported_not_raised(self):
        with unittest.mock.patch.object(pir, "taken_labels", side_effect=RuntimeError("boom")):
            rc, text = self.create("--label", "triage")
        self.assertCreatedWithout(rc, text, "RuntimeError: boom")


class Labels(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(pir.slug("Code Reviewer!!"), "code-reviewer")
        self.assertEqual(pir.slug("  --Ops__Bot--  "), "ops-bot")
        self.assertEqual(pir.slug("", "ab" * 16), "worker-abababab")
        self.assertEqual(pir.slug("ab" * 16, "ab" * 16), "worker-abababab", "an id is not a name")
        long = pir.slug("a very long worker name that goes past thirty two")
        self.assertLessEqual(len(long), 32)
        self.assertRegex(long, dc.LABEL_RE.pattern)
        self.assertTrue(dc.LABEL_RE.fullmatch(pir.slug("über")))

    def test_collision(self):
        self.assertEqual(pir.free_label("reviewer"), "reviewer")
        self.assertEqual(pir.free_label("reviewer", {"reviewer"}), "reviewer-2")
        self.assertEqual(pir.free_label("reviewer", {"reviewer", "reviewer-2"}), "reviewer-3")
        self.assertEqual(pir.free_label("reviewer", agents=["@owner-desk-reviewer.agent:x.net"]), "reviewer-2")
        self.assertEqual(pir.free_label("view", agents=["@owner-desk-reviewer.agent:x.net"]), "view")
        full = "a" * 32
        self.assertEqual(pir.free_label(full, {full}), "a" * 30 + "-2")


class LabelsOnDisk(Base):
    def test_another_workers_pending_link_and_a_known_agent_take_their_labels(self):
        dc._keep(self.ws, "f" * 32, {"label": "triage", "state": "pending", "expires_at": time.time() + 900})
        dc._keep(self.ws, "e" * 32, {"label": "triage-2", "state": "expired", "expires_at": 1})
        labels, agents = pir.taken_labels(self.ws, "1" * 32)
        self.assertEqual(labels, {"triage"})
        self.assertIn(dc.pp.core_lane(self.ws, None)["agent_mxid"], agents)

        def child(argv, log):
            return FakeChild(rc=1)
        self.create("--label", "triage", child=child)
        self.assertEqual(self.identity_argvs[0][8], "triage-2")


class Lifecycle(D.Base):
    """A worker with its own agent keeps it across stop and enable, and its seat mints as it."""

    def setUp(self):
        super().setUp()
        D.Base.connect(self, D.ConnectFake(self.backend))
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], NEW_AGENT)
        sock = str(self.ws / "tmux.sock")
        pool_manage.wi.create_worker(self.ws, runtime="claude", tmux_socket=sock, worker_id=W, cwd=str(ROOT))
        pr.register_worker(self.ws, W, "Reviewer", runtime="claude")
        seat = f"sutando-worker-{W}"
        self.alive = {seat, seat + "-watcher", seat + "-input"}

    def runner(self, argv, **kw):
        if argv[:1] == ["tmux"] and "has-session" in argv:
            name = argv[argv.index("-t") + 1].lstrip("=")
            if name in self.alive:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", f"can't find session: {name}\n")
        if argv[:1] == ["tmux"] and "kill-session" in argv:
            self.alive.discard(argv[argv.index("-t") + 1].lstrip("="))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def acts_as(self):
        return C.worker_mcp_credential(W, workspace=self.ws, vault=self.vault)

    def test_stop_and_enable_keep_the_worker_s_own_agent(self):
        before = self.acts_as()
        self.assertEqual((before["acts_as"], before["agent_id"]), ("worker", NEW_AGENT))
        self.assertEqual(pool_manage.stop(self.ws, W, runner=self.runner)["result"], "stopped")
        self.assertEqual(C.worker_identity(W, self.ws)["agent_mxid"], NEW_AGENT)
        spawned = []
        with unittest.mock.patch.object(pool_manage.rem, "ensure_supervisor", return_value={"outcome": "x"}), \
                unittest.mock.patch.object(pool_manage.rem, "ensure_input_watch", return_value={"outcome": "x"}):
            out = pool_manage.enable(self.ws, ROOT, W, runner=self.runner,
                                     spawn=lambda ws, repo, **kw: spawned.append(kw) or {"runtime_session_id": None})
        self.assertEqual(out["result"], "enabled", out)
        after = self.acts_as()
        self.assertEqual((after["acts_as"], after["agent_id"], after["bearer"]),
                         ("worker", NEW_AGENT, before["bearer"]))
        self.assertFalse(C.worker_identity(W, self.ws).get("disabled"))

    def test_retire_removes_it_here_and_leaves_deleting_the_agent_to_the_owner(self):
        told = []
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault):
            out = pool_manage.retire(self.ws, W, runner=self.runner, tell=lambda text: told.append(text) or True)
        self.assertEqual(out["result"], "retired", out)
        ident = out["identity"]
        self.assertEqual((ident["result"], ident["agent"], ident["agent_removal"], ident["owner_told"]),
                         ("removed", NEW_AGENT, "owner_step", True))
        self.assertIn("delete it in the app", ident["note"])
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertEqual(len(told), 1)
        self.assertTrue(told[0].startswith("[dm-only]\n"))
        self.assertIn(NEW_AGENT, told[0])
        self.assertIn("Reviewer", told[0])

    def test_retire_withdraws_the_workers_open_cards_and_names_them_to_the_owner(self):
        from hitl import cards as hc, schema as hs
        manager = hc.manager_for(self.ws)

        def card(guard, worker=None):
            subject = {"ask_id": guard, **({"by_worker": worker} if worker else {})}
            return manager.create(hc.choice_request(f"Question {guard}?", [{"id": "approve", "label": "Approve"}], guard, subject, runtime="pending-questions"))
        mine, others, cores = card("a1", W), card("a2", "otherw1"), card("a3")
        told = []
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault):
            out = pool_manage.retire(self.ws, W, runner=self.runner, tell=lambda text: told.append(text) or True)
        ident = out["identity"]
        self.assertEqual(ident["result"], "removed", ident)
        self.assertEqual(ident["cards_withdrawn"], ["Question a1?"])
        self.assertEqual(manager.store.load(mine.id).status, hs.STATUS_CANCELLED)
        self.assertFalse((manager.store.root / f"{mine.id}.json").exists(), "withdrawn and archived")
        self.assertFalse(manager.needs_projection(mine.id), "no lane is left to post its edit")
        for kept in (others, cores):
            self.assertEqual(manager.store.load(kept.id).status, hs.STATUS_PENDING)
        self.assertIn("1 open decision card(s) were withdrawn", told[0])
        self.assertIn("- Question a1?", told[0])

    def test_a_failed_identity_removal_keeps_the_workers_cards(self):
        from hitl import cards as hc, schema as hs
        manager = hc.manager_for(self.ws)
        mine = manager.create(hc.choice_request("Question b1?", [{"id": "approve", "label": "Approve"}], "b1",
                                                {"ask_id": "b1", "by_worker": W}, runtime="pending-questions"))
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch.object(C, "retire_worker_identity", side_effect=C.ConnectionsError("row locked")):
            out = pool_manage.retire(self.ws, W, runner=self.runner, tell=lambda text: True)
        self.assertEqual(out["identity"]["result"], "failed", out)
        self.assertNotIn("cards_withdrawn", out["identity"])
        self.assertEqual(manager.store.load(mine.id).status, hs.STATUS_PENDING)

    def test_a_card_withdrawal_that_raises_is_reported_and_the_worker_is_still_retired(self):
        told = []
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch("hitl.cards.withdraw_worker_cards", side_effect=OSError("store gone")):
            out = pool_manage.retire(self.ws, W, runner=self.runner, tell=lambda text: told.append(text) or True)
        ident = out["identity"]
        self.assertEqual(ident["result"], "removed", ident)
        self.assertEqual(ident["cards_withdrawn"], [])
        self.assertIn("store gone", ident["cards_error"])
        self.assertIn("could not be withdrawn (OSError: store gone)", told[0])

    def test_retire_by_default_queues_the_note_to_the_owner_s_dm(self):
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault):
            out = pool_manage.retire(self.ws, W, runner=self.runner)
        self.assertTrue(out["identity"]["owner_told"])
        notes = [p.read_text() for p in (self.ws / "results").glob("proactive-*.txt")]
        self.assertTrue(any(NEW_AGENT in n and "Delete agent" in n for n in notes), notes)



class RetireWhileTheApprovalIsPending(D.Base):
    """Retiring a worker whose device-flow approval is still open: the approval is withdrawn, and an
    approval the owner gives anyway is named to them, since nothing here saves or uses that agent."""

    def setUp(self):
        super().setUp()
        sock = str(self.ws / "tmux.sock")
        pool_manage.wi.create_worker(self.ws, runtime="claude", tmux_socket=sock, worker_id=W, cwd=str(ROOT))
        pr.register_worker(self.ws, W, "Reviewer", runtime="claude")
        self.runner = Lifecycle.runner.__get__(self)
        self.alive = set()

    def start_flow(self, *, approve: bool, cards=None):
        """dc.connect in a thread, held on its first poll until `self.go` is set."""
        self.go, self.fake = threading.Event(), D.ConnectFake(self.backend, expires_in=60)
        self.raised = []

        def sleep(seconds):
            self.go.wait(10)
            self.backend.sleep(seconds if approve else 200)
            if approve:
                self.fake.approve()

        def run():
            try:
                dc.connect(W, label="reviewer", worker_label="Reviewer", workspace=self.ws, vault=self.vault, request=self.fake.request,
                           preview=self.preview, deliver=self.sent.append, sleep=sleep, clock=self.backend.clock,
                           cards=cards)
            except dc.DeviceConnectError as e:
                self.raised.append(e.status)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if dc.read_record(self.ws, W).get("state") == "pending":
                return
            time.sleep(0.01)
        self.fail("the flow never sent its link")

    def retire(self, tell="sent"):
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault):
            out = pool_manage.retire(self.ws, W, runner=self.runner,
                                     tell=self.sent.append if tell == "sent" else tell)
        self.assertEqual(out["result"], "retired", out)
        return out

    def test_an_unapproved_link_is_withdrawn_and_lapses_with_nothing_left(self):
        self.start_flow(approve=False)
        out = self.retire()
        self.assertEqual((out["pending_approval"]["result"], out["pending_approval"]["label"]),
                         ("withdrawn", "reviewer"))
        self.go.set()
        self.thread.join(10)
        self.assertEqual(self.raised, ["expired"])
        self.assertIsNone(C.worker_identity(W, self.ws))
        self.assertEqual(dc.read_record(self.ws, W)["state"], "withdrawn")
        self.assertEqual(len(self.sent), 2, self.sent)  # the link, then its withdrawal; no "expired, ask again"
        self.assertIn("is withdrawn", self.sent[1])
        self.assertIn('do not approve agent "reviewer"', self.sent[1])
        self.assertNotIn("expired", " ".join(self.sent[1:]))
        self.assertEqual(dc.resend(W, workspace=self.ws, spawn=lambda argv: 1)["status"], "refused")

    def test_an_approval_given_after_the_retirement_is_named_to_the_owner(self):
        self.start_flow(approve=True)
        self.retire()
        self.go.set()
        self.thread.join(10)
        self.assertEqual(self.raised, ["worker_retired"])
        self.assertIsNone(C.worker_identity(W, self.ws))
        rec = dc.read_record(self.ws, W)
        self.assertEqual((rec["state"], rec["agent_id"]), ("retired_after_approval", NEW_AGENT))
        self.assertIn(f"**Delete agent `{NEW_AGENT}` in the app**", self.sent[-1])
        self.assertIn("approved after your worker Reviewer was retired", self.sent[-1])
        self.assertNoSecret(self.fake, *self.sent, json.dumps(rec))

    def test_a_worker_with_no_open_approval_reports_none(self):
        self.assertEqual(self.retire()["pending_approval"], {"result": "none"})

    def owner_notes(self):
        return [p.read_text() for p in sorted((self.ws / "results").glob("proactive-*.txt"))]

    def pending_record(self):
        """An open approval whose card the owner was shown as $card1."""
        dc.write_record(self.ws, W, {"label": "reviewer", "worker_label": "Reviewer", "state": "pending",
                                     "event_id": "$card1", "server": "alpha", "expires_at": time.time() + 900})

    def test_retire_swaps_the_approval_card_for_a_withdrawn_card(self):
        actions = D.FakeActions()
        sink = dc.CardSink(actions.execute, DM, self.ws)
        asked = []
        self.start_flow(approve=False, cards=sink)
        self.assertEqual([c["state"] for c in actions.cards()], ["pending"])
        with unittest.mock.patch.object(dc, "default_cards", lambda ws, core: asked.append(core) or sink):
            out = self.retire(tell=None)
        self.assertEqual(out["pending_approval"], {"result": "withdrawn", "label": "reviewer", "owner_told": True})
        self.assertEqual(asked, [dc.pp.core_lane(self.ws, None)], "the sink is the core lane's")
        approval, withdrawn = actions.cards()[:2]
        self.assertEqual(actions.calls[1]["arguments"]["reply_to"], "$card1", "it replaces the approval card")
        self.assertEqual((withdrawn["id"], withdrawn["state"], withdrawn["header"]["status"]),
                         (approval["id"], "expired", {"label": "Withdrawn"}))
        self.assertEqual(withdrawn["header"]["title"], "Reviewer")
        self.assertNotIn("buttons", withdrawn, "nothing left to approve on it")
        self.assertIn("Do not approve reviewer", json.dumps(withdrawn))
        self.assertIn('agent \\"reviewer\\" (pool worker Reviewer) is withdrawn',
                      json.dumps(actions.calls[1]["arguments"]["body"]))
        self.assertTrue(actions.calls[1]["operation_id"].startswith(f"worker-approval:{W}:"))
        self.assertTrue(actions.calls[1]["operation_id"].endswith(":withdrawn"))
        self.assertEqual((self.sent, self.owner_notes()), ([], []), "the card told the owner; no text as well")
        self.go.set()
        self.thread.join(10)
        self.assertEqual(self.raised, ["expired"])
        self.assertEqual([c["state"] for c in actions.cards()], ["pending", "expired"],
                         "the lapsed flow posts no 'link expired' card over the withdrawn one")
        self.assertEqual(dc.read_record(self.ws, W)["state"], "withdrawn")

    def test_a_withdrawn_card_the_server_refuses_falls_back_to_the_text_note(self):
        self.pending_record()
        refusing = D.FakeActions(refuse=True)
        with unittest.mock.patch.object(dc, "default_cards", lambda ws, core: dc.CardSink(refusing.execute, DM)):
            out = self.retire(tell=None)
        self.assertEqual(out["pending_approval"]["owner_told"], True)
        self.assertEqual(len(refusing.calls), 1, "the card was tried first")
        notes = self.owner_notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0], dc.withdrawn_note("Reviewer", "reviewer"))
        self.assertEqual(dc.read_record(self.ws, W)["state"], "withdrawn")

    def test_a_card_sink_that_raises_falls_back_to_the_text_note(self):
        for error in (dc.ac.CardError("bad card"), OSError("disk")):
            with self.subTest(error=type(error).__name__):
                self.pending_record()
                sent, shown = [], []

                class Raising:
                    def show(self, body, card, operation_id, previous=""):
                        shown.append(previous)
                        raise error
                out = dc.withdraw(self.ws, W, cards=Raising(), deliver=sent.append)
                self.assertEqual(out, {"result": "withdrawn", "label": "reviewer", "owner_told": True})
                self.assertEqual(shown, ["$card1"])
                self.assertEqual(sent, [dc.withdrawn_note("Reviewer", "reviewer")])

    def test_an_owner_note_that_cannot_be_delivered_is_reported_as_not_told(self):
        self.pending_record()

        class Shown:
            def show(self, *a, **kw):
                return ""
        out = dc.withdraw(self.ws, W, cards=Shown(), deliver=lambda text: False)
        self.assertEqual(out, {"result": "withdrawn", "label": "reviewer", "owner_told": False})

    def test_no_card_sink_to_be_had_falls_back_to_the_text_note(self):
        for error in (dc.pp.ProvisionError("no single core"), C.ConnectionsError("no rows"), OSError("disk")):
            with self.subTest(error=type(error).__name__):
                for p in (self.ws / "results").glob("proactive-*.txt"):
                    p.unlink()
                self.pending_record()
                with unittest.mock.patch.object(dc, "default_cards", side_effect=error):
                    out = dc.withdraw(self.ws, W)
                self.assertEqual(out["owner_told"], True)
                self.assertEqual(self.owner_notes(), [dc.withdrawn_note("Reviewer", "reviewer")])

    def test_a_record_without_a_card_sends_only_the_text(self):
        self.pending_record()
        dc.write_record(self.ws, W, {**dc.read_record(self.ws, W), "event_id": ""})
        actions, sent = D.FakeActions(), []
        out = dc.withdraw(self.ws, W, cards=dc.CardSink(actions.execute, DM, self.ws), deliver=sent.append)
        self.assertEqual((out["owner_told"], actions.calls), (True, []))
        self.assertEqual(sent, [dc.withdrawn_note("Reviewer", "reviewer")])

    def test_a_withdrawal_that_raises_is_reported_and_the_worker_is_still_retired(self):
        self.pending_record()
        with unittest.mock.patch.object(dc, "withdraw", side_effect=RuntimeError("boom")):
            out = self.retire()
        self.assertEqual(out["pending_approval"], {"result": "failed", "why": "RuntimeError: boom"})
        self.assertEqual(pr.load_roster(self.ws)["workers"][W]["state"], "retired")


class RetireNoteNotDelivered(Lifecycle):
    def test_an_agent_removal_note_that_cannot_be_queued_is_reported_as_not_told(self):
        with unittest.mock.patch.object(C, "_vault", lambda v=None: self.vault), \
                unittest.mock.patch.object(dc, "send_to_owner", side_effect=OSError("read-only results")):
            out = pool_manage.retire(self.ws, W, runner=self.runner)
        self.assertEqual(out["result"], "retired", out)
        self.assertEqual((out["identity"]["result"], out["identity"]["owner_told"]), ("removed", False))
        self.assertIsNone(C.worker_identity(W, self.ws), "the identity is gone even though the note was not")
        self.assertEqual(list((self.ws / "results").glob("proactive-*.txt")), [])


class IdentityRequest(Base):
    """pool_identity_request.request's other outcomes, driven with a scripted child."""

    def request(self, child, **kw):
        argvs = []

        def spawn(argv, log):
            argvs.append(argv)
            return child(argv, log)
        out = pir.request(self.ws, "1" * 32, "Triage", repo=ROOT, spawn=spawn, **kw)
        return out, argvs

    def test_a_link_the_owner_already_approved_reports_approved(self):
        def child(argv, log):
            dc._keep(self.ws, argv[5], {"label": argv[8], "state": "done", "expires_at": time.time() + 900})
            return FakeChild()
        out, _ = self.request(child)
        self.assertEqual((out["status"], out["label"]), ("approved", "triage"))
        self.assertEqual(pir.describe(out), 'approved (agent "triage")')

    def test_a_link_not_confirmed_in_time_reports_starting(self):
        ticks = iter(range(1000))
        out, _ = self.request(lambda argv, log: FakeChild(rc=None), wait_s=2, clock=lambda: next(ticks),
                              sleep=lambda s: None)
        self.assertEqual((out["status"], out["label"]), ("starting", "triage"))
        self.assertIn("not confirmed within 2s", out["why"])
        self.assertEqual(pir.describe(out),
                         f'starting (agent "triage"; {out["why"]})')
        self.assertIn(f"worker-identity-{'1' * 32}.log", out["why"])

    def test_a_named_lane_is_passed_to_the_child(self):
        lane = dc.pp.core_lane(self.ws, None)["instance"]
        out, argvs = self.request(lambda argv, log: FakeChild(rc=1), lane=lane)
        self.assertEqual(argvs[0][-2:], ["--lane", lane])
        self.assertEqual(out["status"], "skipped")

    def test_a_worker_that_already_has_an_agent_is_not_asked_again(self):
        with unittest.mock.patch.object(C, "worker_identity", lambda wid, ws: {"agent_mxid": NEW_AGENT}):
            out, argvs = self.request(lambda argv, log: FakeChild(rc=0))
        self.assertEqual(out, {"status": "skipped", "why": "the worker already has an agent of its own"})
        self.assertEqual(argvs, [])

    def test_no_free_label_is_an_error(self):
        taken = {"triage"} | {f"triage-{n}" for n in range(2, 100)}
        with self.assertRaisesRegex(ValueError, "no free label near 'triage'"):
            pir.free_label("triage", taken)
        with unittest.mock.patch.object(pir, "taken_labels", lambda ws, wid: (taken, [])):
            out, argvs = self.request(lambda argv, log: FakeChild(rc=0))
        self.assertEqual(out["status"], "skipped")
        self.assertIn("no free label near 'triage'", out["why"])
        self.assertEqual(argvs, [])


class Spawn(unittest.TestCase):
    def test_the_child_runs_detached_in_the_repo_and_writes_to_its_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "identity.log"
            log.write_text("earlier\n")
            proc = pir._spawn([sys.executable, "-c", "import os, sys; print(os.getcwd(), flush=True); "
                               "print(os.getsid(0) == os.getpid(), flush=True); "
                               "print(sys.stdin.read() == '', flush=True); print('err', file=sys.stderr)"], log)
            self.assertEqual(proc.wait(30), 0)
            lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "earlier", "the log is appended to")
        self.assertEqual(Path(lines[1]).resolve(), ROOT.resolve())
        self.assertEqual(lines[2:], ["True", "True", "err"],
                         "its own session (it outlives the creator), an empty stdin, and stderr in the log too")


if __name__ == "__main__":
    unittest.main()
