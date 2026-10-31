#!/usr/bin/env python3
"""The claude-code adapter ships a well-formed Claude Code mod whose embedded
Python mirrors Sparrow's rules, its helper scripts write what Sparrow's parser
reads, and nothing in it names a host; its inbox CLI, launcher, cards and pane reads.

The mod's own `*.test.ts` needs the `claude` binary; this file is the part CI
can run with stdlib Python alone. Shared fixtures: claude_code_fixtures.py."""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import (  # noqa: E402
    _dirs, accepter, ActionReply, ADAPTER, BYPASS_PANE, CANARY, card, CardBase, cli, cm, cm_dict, complete_safe,
    ConnectBase, CONSOLE_SH, contract_scenarios, contract_set_problem, contract_shape, covered_code, detector,
    FakeTmux, fd_path, GEN, hermetic_env, hmac_policy, hold_consumer, hold_mod, holder_lock, HOST_PATH_RE, inbox,
    isolated_child_env, launcher, MOD, NONCE, OWNER_MXID, parse_task_headers, PKG, raises, ran, READY_PANE, real_ids,
    REAL_TMUX_VERSION, records, REGISTER, resolve_access_tier, ROOM, run_verifier, Runner, SESSION_ID, sm, SRC, STALL,
    stamp, STAMP, STUB_SH, StubTm, task_io, TMUX_DIR, tmux_pane_keys, tmux_probe, tree_bytes, trust, TRUST_PANE,
    unhold, verify_py, wait_for, wait_gone, write_fake_claude,
)


class PluginShape(unittest.TestCase):
    def test_manifest_and_hooks_point_at_the_module(self):
        manifest = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "ag2space-inbox")
        for key in ("version", "description"):
            self.assertTrue(manifest.get(key), key)
        hooks = json.loads((MOD / "hooks" / "hooks.json").read_text())
        self.assertEqual(hooks["modules"], ["./register.ts"])
        self.assertTrue(REGISTER.is_file())
        src = REGISTER.read_text()
        for name in ("ag2space_inbox_watch", "ag2space_reply", STAMP):
            self.assertIn(name, src)

    def test_no_hardcoded_host_paths_in_added_files(self):
        for path in sorted(p for p in (*ADAPTER.rglob("*"), *MOD.rglob("*")) if p.is_file()):
            self.assertIsNone(HOST_PATH_RE.search(path.read_text(errors="replace")), path)

    def test_mod_tests_exist_for_the_claude_harness_and_ci_runs_them(self):
        self.assertTrue(list((MOD / "tests").glob("*.test.ts")))
        ci = PKG.parents[1] / ".github" / "workflows" / "ci.yml"
        if ci.exists():         # the repo checkout, not a standalone package
            steps = [ln.strip() for ln in ci.read_text().splitlines() if not ln.strip().startswith("#")]
            self.assertTrue(any(ln.endswith("plugin test ag2space-inbox") and "claude" in ln for ln in steps),
                            "CI runs the Mod's cases under the CLI's harness")

    def test_the_mod_holds_no_copy_of_the_hmac_strictness_rule(self):
        """Strict or not is hmac_policy's call through `inbox envelope`, per task: the Mod reads no
        AG2_HMAC_STRICT and compares no verdict against a mode of its own."""
        src = REGISTER.read_text()
        self.assertNotIn("AG2_HMAC_STRICT", src)
        self.assertNotIn("strict &&", src)
        self.assertIn("['envelope', '--verdict', verdict, '--key-seen', seen]", src)
        self.assertIn("policy.data.submit", src)

    def test_pinned_non_owner_note_is_the_shared_owners(self):
        mod = re.search(r"^const NON_OWNER_NOTE = `(.*)`$", REGISTER.read_text(), re.M).group(1)
        self.assertEqual(mod, task_io.NON_OWNER_NOTE)
        self.assertNotIn("adapters/hatch", REGISTER.read_text())


class VerifierMirrorsSparrow(unittest.TestCase):
    """VERIFY_PY must answer exactly what src/task_envelope.verify_text and
    team_result_guard.resolve_access_tier answer, vector by vector."""

    def setUp(self):
        if not (SRC / "task_envelope.py").is_file():
            self.skipTest("src/task_envelope.py not beside the package (standalone checkout)")
        sys.path.insert(0, str(SRC))
        import task_envelope
        self.te = task_envelope
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.key = secrets.token_hex(32)
        self.ws.joinpath("state", "auth").mkdir(parents=True)
        self.key_file = self.ws / "state" / "auth" / "task-hmac.key"
        self.key_file.write_text(self.key)

    def vectors(self):
        base = "id: task-v1\nsource: ag2space\naccess_tier: owner\ntask: hi\n"
        signed = stamp(base, self.key)
        flipped = signed.replace("access_tier: owner", "access_tier: guest")
        crlf = stamp(base.replace("\n", "\r\n"), self.key)
        no_id = stamp("source: ag2space\naccess_tier: team\ntask: hi\n", self.key)
        # Tier shapes, in order: post-task only, pre-task wins over post-task, pre-task
        # conflict, the legacy `other` alias, an unknown tier, no tier at all, collaborator.
        tiers = [
            "id: t\ntask: hi\naccess_tier: team\n",
            "id: t\naccess_tier: owner\ntask: hi\naccess_tier: guest\n",
            "id: t\naccess_tier: owner\naccess_tier: guest\ntask: hi\n",
            "id: t\naccess_tier: other\ntask: hi\n",
            "id: t\naccess_tier: admin\ntask: hi\n",
            "id: t\ntask: hi\n",
            "id: t\ncollaborator: true\naccess_tier: team\ntask: hi\n",
        ]
        return [signed, flipped, base, crlf, no_id] + [stamp(t, self.key) for t in tiers]

    def check(self, script: str, key_file: str, workspace: Path) -> list[str]:
        """Every vector where the mod's verifier and Sparrow's own readers disagree."""
        diffs = []
        for i, text in enumerate(self.vectors()):
            verdict, tier, collab = run_verifier(script, text, key_file)
            want_verdict = self.te.verify_text(text, workspace)["verdict"]
            task_file = workspace / f"vec{i}.txt"; task_file.write_text(text)
            want_tier = resolve_access_tier(task_file)
            head = text.split("\ntask:", 1)[0].split("\n")
            want_collab = str("collaborator: true" in head).lower()
            if (verdict, tier, collab) != (want_verdict, want_tier, want_collab):
                diffs.append(f"vector {i}: mod {verdict} {tier} {collab} != sparrow {want_verdict} {want_tier} {want_collab}")
        return diffs

    def test_verdicts_and_tiers_agree_with_a_good_key(self):
        self.assertEqual(self.check(verify_py(), str(self.key_file), self.ws), [])

    def test_verdicts_agree_without_a_key_and_with_malformed_keys(self):
        empty = Path(self.tmp.name) / "nokey"; empty.mkdir()
        self.assertEqual(self.check(verify_py(), str(empty / "missing"), empty), [])
        spaced = " ".join(self.key[i:i + 2] for i in range(0, 64, 2))
        for bad in (spaced, self.key[:62], "zz" * 32, ""):
            ws = Path(tempfile.mkdtemp(dir=self.tmp.name))
            ws.joinpath("state", "auth").mkdir(parents=True)
            kf = ws / "state" / "auth" / "task-hmac.key"; kf.write_text(bad)
            self.assertEqual(self.check(verify_py(), str(kf), ws), [], repr(bad[:8]))
            self.assertEqual(run_verifier(verify_py(), self.vectors()[0], str(kf))[0], "unverifiable", repr(bad[:8]))

    def test_the_key_digest_is_the_one_the_policy_reads(self):
        """The fourth field binds the verdict to the key bytes the verifier read: the same digest
        hmac_policy.key_digest gives for that file, "absent" when there is none."""
        text = self.vectors()[0]
        for content in (self.key, self.key + "\n", "short", ""):
            self.key_file.write_text(content)
            got = subprocess.run([sys.executable, "-c", verify_py()], input=text, capture_output=True, text=True,
                                 env=hermetic_env(AG2_HMAC_KEY_FILE=str(self.key_file)), check=True).stdout.split()
            self.assertEqual(got[3], hmac_policy.key_digest(str(self.key_file)), repr(content[:8]))
        for missing in (str(self.ws / "none"), ""):
            got = subprocess.run([sys.executable, "-c", verify_py()], input=text, capture_output=True, text=True,
                                 env=hermetic_env(AG2_HMAC_KEY_FILE=missing), check=True).stdout.split()
            self.assertEqual((got[0], got[3], hmac_policy.key_digest(missing)), ("unverifiable", "absent", "absent"))

    def test_mutants_are_caught(self):
        always_verified = verify_py().replace('out("verified" if', 'out("verified" if True or')
        self.assertIn('if True or', always_verified)
        self.assertTrue(self.check(always_verified, str(self.key_file), self.ws))
        owner_only = verify_py().replace('def out(v): print(v,tier,collab,seen)', 'def out(v): print(v,"owner",collab,seen)')
        self.assertIn('"owner",collab', owner_only)
        self.assertTrue(self.check(owner_only, str(self.key_file), self.ws))


class DropTask(unittest.TestCase):
    def test_dropper_writes_a_task_last_file_sparrow_parses_and_the_mod_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"; inbox.mkdir()
            key = Path(tmp) / "task-hmac.key"; key.write_text(secrets.token_hex(32))
            out = subprocess.run(["bash", str(ADAPTER / "drop-task.sh"), str(inbox), str(key), "t1", "What is 2+2?"],
                                 capture_output=True, text=True, check=True)
            dest = inbox / "task-t1.txt"
            self.assertEqual(out.stdout.strip(), str(dest))
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ["task-t1.txt"])  # no temp left
            text = dest.read_text()
            parsed = parse_task_headers(text)
            self.assertEqual(parsed.get("id"), "task-t1")
            self.assertEqual(parsed.get("source"), "ag2space")
            self.assertEqual(parsed.get("access_tier"), "owner")
            self.assertEqual(parsed.body.rstrip("\n"), "What is 2+2?")
            lines = text.rstrip("\n").split("\n")
            self.assertTrue(lines[-1].startswith("task: "), "task: is the last header")
            self.assertTrue(lines[0].startswith("id: ") and lines[1].startswith(STAMP))
            self.assertEqual(run_verifier(verify_py(), text, str(key)), ("verified", "owner", "false"))
            flipped = text.replace("access_tier: owner", "access_tier: guest")
            self.assertEqual(run_verifier(verify_py(), flipped, str(key))[0], "invalid")


class Measure(unittest.TestCase):
    def test_measure_reports_the_mtime_delta_in_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task-t1.txt"; task.write_text("id: task-t1\ntask: x\n")
            result = Path(tmp) / "result.txt"; result.write_text("4\n")
            base = 1_700_000_000_000_000_000
            os.utime(task, ns=(base, base)); os.utime(result, ns=(base, base + 1_500_000_000))
            out = subprocess.run(["bash", str(ADAPTER / "measure.sh"), str(task), str(result), "2"],
                                 capture_output=True, text=True, check=True)
            self.assertIn("latency_ms=1500", out.stdout)
            self.assertIn("result body: 4", out.stdout)

    def test_measure_times_out_without_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task-t1.txt"; task.write_text("task: x\n")
            out = subprocess.run(["bash", str(ADAPTER / "measure.sh"), str(task), str(Path(tmp) / "none.txt"), "1"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("TIMEOUT", out.stdout)


class FakeClaudeWrapper(unittest.TestCase):
    """The harness's fake claude works through an interpreter path containing a space."""

    def test_the_wrapper_execs_the_quoted_interpreter_from_a_path_with_a_space(self):
        with tempfile.TemporaryDirectory() as d:
            spaced = Path(d) / "py dir" / "python3"
            spaced.parent.mkdir()
            spaced.symlink_to(sys.executable)
            home = Path(d) / "home"
            home.mkdir()
            claude = write_fake_claude(Path(d) / "claude", py=str(spaced))
            self.assertEqual(claude.read_text().splitlines()[0], "#!/bin/sh")
            self.assertIn('exec "%s" "$0.py" "$@"' % spaced, claude.read_text())
            r = subprocess.run([str(claude), "--version"], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(home)}, cwd=d)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "2.1.296 (Claude Code)"), r.stderr)
            r = subprocess.run([str(claude), "--plugin-dir", "x"], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(home), "AG2_INBOX_DIR": d}, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            rec = json.loads((home / "claude-session.json").read_text())
            self.assertEqual(rec["argv"], [str(claude), "--plugin-dir", "x"], "argv[0] is the wrapper the launcher ran")


class InboxCli(ConnectBase):
    """The Mod's lifecycle verbs, each one CLI call under claims.lock, bound to the live consumer
    generation, mirrored into the progress record under progress.lock."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        self.holder, self.consumer = hold_consumer(self.work)
        self.addCleanup(unhold, self.holder)
        self.root = str(self.work)

    def verb(self, *args, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin or b""))):
            rc = cli.main(["inbox", "--root", self.root, *args])
        return rc, json.loads(out.getvalue())

    def task(self, task_id="task-a", tier="owner", extra=""):
        lines = "" if tier is None else "access_tier: %s\n" % tier
        (self.inbox / (task_id + ".txt")).write_text("id: %s\n%s%stask: hi\n" % (task_id, extra, lines))
        return task_id

    def test_claim_admits_the_owner_and_answers_every_non_owner_and_a_tierless_task_with_the_refusal(self):
        rc, out = self.verb("claim", self.task("task-o"))
        self.assertEqual((rc, out["admission"], out["access_tier"], out["collaborator"]), (0, "owner", "owner", False))
        refused = {"task-t": ("team", ""), "task-g": ("guest", ""), "task-c": ("team", "collaborator: true\n"),
                   "task-n": (None, "")}
        for t, (tier, extra) in refused.items():
            rc, out = self.verb("claim", self.task(t, tier, extra))
            self.assertEqual((rc, "not admitted" in out["refused"]), (1, True), (t, out))
            self.assertEqual((self.results / (t + ".txt")).read_text(), task_io.NON_OWNER_REFUSED, t)
            self.assertNotIn(t, records.load_plugin(self.root).get("inflight") or {}, "never claimed for the session")
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, "only ABSENT may be submitted" in out["refused"]), (1, True), "answered once")
        self.assertIn("malformed: no access_tier header", self.verb("claim", self.task("task-n2", None))[1]["refused"])

    def resolved(self, out):
        return (out.get("resolved"), out.get("answered"), out.get("interrupted"), out.get("retry"))

    def test_an_unconfirmed_turn_on_the_real_verbs_stays_unresolved_with_its_mark_until_the_next_start_answers_it(self):
        """The real verbs along the Mod's unconfirmed-turn path: a refused confirmation leaves the claim CALLING
        and a second claim is a final refusal (so the Mod never asks it again while unanswered); the mark
        survives reconciliation; the next start's recovery writes the interrupted answer, after which the
        claim is refused as resolved, the mark is gone and the Mod's clear lands."""
        t = self.task("task-uc")
        rc, out = self.verb("claim", t)
        attempt = str(out["attempt"])
        h = "ab" * 32
        token = self.verb("authorize", t, attempt, "--text-hash", h)[1]["token"]
        rc, out = self.verb("confirm", t, "turn-1", "bad" + token, "--text-hash", h)
        self.assertEqual((rc, out.get("retry")), (1, None), "a refused confirmation is final")
        self.assertEqual(self.verb("progress", "--stuck", t, "--stuck-detail", "turn not confirmed")[0], 0)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry"), "is CALLING" in out["refused"]), (1, None, True), "final: never asked again")
        self.assertEqual(self.verb("progress")[1]["stuck"].keys(), {t}, "the mark survives reconciliation while CALLING")
        self.assertFalse((self.results / (t + ".txt")).exists())
        with mock.patch.object(inbox.task_io, "verify_lineage", return_value=False):
            self.assertEqual(inbox.recover_dead_generation(self.root), 1, "the next start")
        self.assertEqual((self.results / (t + ".txt")).read_text(), inbox.INTERRUPTED_BODY)
        (self.results / "archive").mkdir(exist_ok=True)
        (self.results / (t + ".txt")).rename(self.results / "archive" / (t + ".txt"))   # the gateway drains it
        rc, out = self.verb("progress", "--stuck", t, "--stuck-detail", "turn not confirmed")
        self.assertEqual((rc, t in out["stuck"]), (0, False), "the Mod's probe: the mark is dropped as resolved")
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry"), "only ABSENT" in out["refused"]), (1, None, True), "resolved: refused")
        self.assertEqual(records.load_plugin(self.root).get("stuck") or {}, {}, "the claim cleared the mark")
        self.assertEqual(self.verb("progress", "--unstuck", t)[0], 0, "the Mod's clear lands")

    def test_every_answer_in_the_harness_contract_is_what_the_real_verbs_answer(self):
        """The Mod harness's fake CLI is bound to tests/cli-contract.ts, scenario by scenario; the same
        scenarios run here against the real verbs, so the fake cannot answer what the CLI does not."""
        diffs, ran = [], []
        for sc in contract_scenarios():
            ran.append(sc["name"])
            # The harness answers envelope from its options; the same choice is recorded in the config here.
            self.configure(**{"off": {"HMAC_KEY_FILE": "", "HMAC_STRICT": ""}, "lenient": {"HMAC_STRICT": "0"},
                              "strict": {"HMAC_STRICT": "1"}}.get(sc.get("hmac"), {}))
            key = self.home / "task-hmac.key"
            if sc.get("key") == "present":
                key.write_text("ab" * 32)
            elif sc.get("key") == "missing" and key.exists():
                key.unlink()
            self.task(sc["task"], sc["tier"])
            if "result" in sc:
                (self.results / (sc["task"] + ".txt")).write_text(sc["result"])
            if "archived" in sc:
                (self.results / "archive").mkdir(exist_ok=True)
                (self.results / "archive" / (sc["task"] + ".txt")).write_text(sc["archived"])
            saved = {}
            for i, step in enumerate(sc["steps"]):
                argv = [saved.get(a[1:-1], a) if a.startswith("{") else a for a in step["argv"]]
                rc, out = self.verb(*argv, stdin=step.get("stdin", "").encode() or None)
                if step.get("save") and step["save"] in out:
                    saved[step["save"]] = str(out[step["save"]])
                want = contract_shape(step["expect"], rc, out)
                if want:
                    diffs.append("%s, step %d %s: %s; got %s %s" % (sc["name"], i, argv[0], want, rc, out))
        self.assertEqual(diffs, [])
        self.assertEqual(contract_set_problem(ran), "", "the pinned scenario set")

    def test_the_contract_set_pin_fails_on_a_dropped_or_an_extra_scenario(self):
        names = [sc["name"] for sc in contract_scenarios()]
        self.assertEqual(len(names), 31)
        self.assertEqual(contract_set_problem(names), "")
        for i in (0, 17, len(names) - 1):
            self.assertIn("missing [%r]" % names[i], contract_set_problem(names[:i] + names[i + 1:]), i)
        self.assertIn("extra ['a new scenario']", contract_set_problem(names + ["a new scenario"]))
        self.assertEqual(contract_set_problem(names[::-1]), "order differs")

    def test_interrupt_answers_a_task_the_stopped_session_cannot_take_once_and_the_refusal_for_a_non_owner(self):
        t = self.task("task-late")
        rc, out = self.verb("interrupt", t, "--detail", "session id changed")
        self.assertEqual((rc, *self.resolved(out)), (0, True, True, True, None))
        self.assertEqual((self.results / "task-late.txt").read_text(), inbox.INTERRUPTED_BODY)
        self.assertNotIn(t, records.load_plugin(self.root).get("inflight") or {})
        rc, out = self.verb("interrupt", t)
        self.assertEqual((rc, *self.resolved(out)), (0, True, False, False, None), "resolved, not answered again")
        self.assertEqual(self.verb("claim", t)[0], 1, "and never submitted afterwards")
        rc, out = self.verb("interrupt", self.task("task-g", "guest"))
        self.assertEqual((rc, *self.resolved(out)), (0, True, True, False, None))
        self.assertEqual((self.results / "task-g.txt").read_text(), task_io.NON_OWNER_REFUSED, "the policy still answers")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "_publish", return_value=None):
            rc, out = self.verb("interrupt", self.task("task-p"))
        self.assertEqual((rc, out.get("retry"), "not answered yet" in out["refused"]), (1, True, True), "parked: retried")
        lc = inbox._lifecycle(self.root)
        self.assertEqual(lc.claims.load("task-p")["phase"], inbox.lifecycle.RESULT_STAGED)
        self.assertFalse((self.results / "task-p.txt").exists(), "no result yet")
        rc, out = self.verb("interrupt", "task-p")
        self.assertEqual((rc, *self.resolved(out)), (0, True, True, True, None), "the staged answer is published")
        self.assertEqual((self.results / "task-p.txt").read_text(), inbox.INTERRUPTED_BODY)
        self.assertEqual(lc.claims.load("task-p")["phase"], inbox.lifecycle.SETTLED)
        with mock.patch.object(inbox.lifecycle.Lifecycle, "decide", return_value=("settle", inbox.lifecycle.ARCHIVED)), \
                mock.patch.object(inbox.lifecycle.Lifecycle, "classify", return_value=inbox.lifecycle.ARCHIVED):
            rc, out = self.verb("interrupt", self.task("task-archived"))
        self.assertEqual((rc, *self.resolved(out)), (0, True, False, False, None), "an archived task is resolved")
        with mock.patch.object(inbox, "_lifecycle", side_effect=RuntimeError("no root")):
            rc, out = self.verb("interrupt", self.task("task-nolc"))
        self.assertEqual((rc, out.get("retry")), (1, True), "even the lifecycle failing to open is a retry")

    def test_terminal_retires_and_every_other_outcome_retries_whatever_its_cause(self):
        """No failure class decides: each case gives no result and retry, then the next call answers it once.
        The custom exception is one nothing anticipates, which shows the rule holds by construction."""
        L = inbox.lifecycle
        real_wd = L.write_durable

        class Unanticipated(Exception):
            pass

        def failing_body(error):
            def write(path, data):
                if str(path).endswith("/body"):
                    raise error
                return real_wd(path, data)
            return write
        cases = {
            "enospc": mock.patch.object(L, "write_durable", side_effect=failing_body(OSError(errno.ENOSPC, "No space left"))),
            "live-unready": mock.patch.object(L.Lifecycle, "decide", return_value=("park", L.LIVE_UNREADY)),
            "unknown": mock.patch.object(L.Lifecycle, "decide", return_value=("park", L.UNKNOWN)),
            "claim-unreadable": mock.patch.object(L.Lifecycle, "decide", side_effect=L.ClaimUnreadable("torn claim")),
            "admission-oserror": mock.patch.object(inbox, "admit", side_effect=OSError(errno.EIO, "I/O error")),
            "unanticipated": mock.patch.object(L, "write_durable", side_effect=failing_body(Unanticipated("surprise"))),
        }
        for name, patch in cases.items():
            t = self.task("task-" + name)
            with patch:
                rc, out = self.verb("interrupt", t)
            self.assertEqual((rc, out.get("retry"), out.get("resolved")), (1, True, None), (name, out))
            self.assertFalse((self.results / (t + ".txt")).exists(), name)
            rc, out = self.verb("interrupt", t)
            self.assertEqual((rc, *self.resolved(out)), (0, True, True, True, None), name)
            self.assertEqual(sorted(p.name for p in self.results.glob(t + "*")), [t + ".txt"], "one result: " + name)
            self.assertEqual(self.resolved(self.verb("interrupt", t)[1])[:2], (True, False), "then resolved: " + name)
        broken = mock.Mock()
        broken.claims.load.side_effect = L.ClaimUnreadable("torn")
        self.assertEqual(inbox.resolved(broken, "task-x"), (False, "ClaimUnreadable: torn"),
                         "a disposition that cannot be read is unresolved")

    def test_a_resolving_interrupt_clears_the_durable_stuck_mark_whatever_the_mod_remembers(self):
        t = self.task("task-marked")
        self.assertEqual(self.verb("progress", "--stuck", t, "--stuck-detail", "parked before a restart")[0], 0)
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            self.assertIn("FAIL unanswered", self.main("verify")[1])
            rc, out = self.verb("interrupt", t)
            self.assertEqual((rc, out["resolved"], out["answered"]), (0, True, True))
            self.assertEqual(records.load_plugin(self.root)["stuck"], {}, "cleared in the resolving write")
            self.assertNotIn("unanswered", self.main("verify")[1])
            self.assertEqual(self.verb("progress", "--stuck", t)[0], 0)      # a mark left on an already-answered task
            rc, out = self.verb("interrupt", t)
            self.assertEqual((rc, out["resolved"], out["answered"]), (0, True, False))
            self.assertEqual(records.load_plugin(self.root)["stuck"], {}, "a resolved reply always clears it")
            self.assertNotIn("unanswered", self.main("verify")[1])
        self.assertEqual(self.verb("progress", "--intake", "open")[1]["stuck"], {}, "every progress reply carries the marks")

    def test_a_mark_never_outlives_its_answer_even_when_the_task_file_is_gone(self):
        """Published out of band (not through interrupt or the Mod), the task file archived: the Mod never
        retires it, yet verify, the card and the next record write all drop the mark."""
        self.assertEqual(self.verb("progress", "--stuck", "task-gone", "--stuck-detail", "parked")[0], 0)
        self.assertIn("task-gone", records.load_plugin(self.root)["stuck"])
        (self.results / "task-gone.txt").write_text("published by another route\n")
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            self.assertNotIn("unanswered", self.main("verify")[1])
        lc = launcher.Launcher(dict(cli.read_config(cli.config_path()), ROOT=self.root), cli.config_dir())
        with mock.patch.object(lc.tm, "capture", return_value="pane"):
            self.assertIsNone(lc.observe({"generation": {"pane_id": "%0"}, "stall": None}).get("stall"))
        self.assertEqual(self.verb("progress", "--intake", "open")[1]["stuck"], {}, "pruned by the next write")
        self.assertEqual(inbox.unresolved_stuck(self.root, ["not", "a", "map"]), {})
        with mock.patch.object(inbox, "_lifecycle", side_effect=RuntimeError("no root")):
            self.assertEqual(inbox.unresolved_stuck(self.root, {"t": {"since": 1}}), {"t": {"since": 1}},
                             "an unreadable lifecycle keeps the marks rather than hiding them")

    def test_an_unready_result_is_the_lifecycles_call_and_never_clears_a_mark(self):
        """Whitespace-only and undecodable bodies classify LIVE_UNREADY: interrupt and claim retry, the mark
        stays; a real body resolves and clears it, through interrupt and through the claim's refusal."""
        for t, body in (("task-ws", b" \n\t"), ("task-bad", b"bad \xff\xfe byte\n")):
            self.task(t)
            self.assertEqual(self.verb("progress", "--stuck", t)[0], 0)
            (self.results / (t + ".txt")).write_bytes(body)
            rc, out = self.verb("interrupt", t)
            self.assertEqual((rc, out.get("retry"), "LIVE_UNREADY" in out["refused"]), (1, True, True), t)
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, out.get("retry")), (1, True), "a claim of an unready result is a retry too: " + t)
            self.assertIn(t, records.load_plugin(self.root)["stuck"], "unresolved: the mark stays")
            (self.results / (t + ".txt")).write_text("a real answer\n")
        rc, out = self.verb("claim", "task-ws")
        self.assertEqual((rc, "retry" in out), (1, False), "a ready result: the claim refuses for good")
        self.assertNotIn("task-ws", records.load_plugin(self.root)["stuck"], "and the CLI cleared the mark")
        rc, out = self.verb("interrupt", "task-bad")
        self.assertEqual((rc, out["resolved"]), (0, True))
        self.assertEqual(records.load_plugin(self.root)["stuck"], {})
        t = self.task("task-claimable")
        self.assertEqual(self.verb("progress", "--stuck", t, "--stuck-detail", "not claimable yet")[0], 0)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, out["admission"]), (0, "owner"))
        self.assertNotIn(t, records.load_plugin(self.root)["stuck"], "a successful claim clears the mark in its write")
        real_flocked = records.flocked

        @contextlib.contextmanager
        def progress_lock_fails(path, blocking=True):
            if Path(path).name == records.PROGRESS_LOCK:
                raise OSError(errno.EIO, "progress lock I/O error")
            with real_flocked(path, blocking) as fd:
                yield fd
        cases = (("task-clearfail", mock.patch.object(records, "write_progress", side_effect=records.RecordError("disk"))),
                 ("task-lockfail", mock.patch.object(records, "flocked", progress_lock_fails)))
        for t, failing in cases:
            self.task(t)
            self.assertEqual(self.verb("progress", "--stuck", t)[0], 0)
            (self.results / (t + ".txt")).write_text("answered out of band\n")
            with failing:
                rc, out = self.verb("claim", t)
            self.assertEqual((rc, out.get("retry"), "cleanup pending" in out["refused"]), (1, True, True),
                             "a terminal task whose mark could not be cleared is kept: " + t)
            self.assertIn(t, records.load_plugin(self.root)["stuck"])
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, "retry" in out, "only ABSENT" in out["refused"]), (1, False, True), "then final: " + t)
            self.assertNotIn(t, records.load_plugin(self.root)["stuck"])
        with mock.patch.object(records, "load_plugin", side_effect=records.RecordError("torn")):
            self.assertFalse(inbox._clear_stuck(self.root, "task-x"), "an unreadable record is never read as cleared")
        with mock.patch.object(records, "flocked", progress_lock_fails):
            rc, out = self.verb("claim", self.task("task-mirrorlock"))
        self.assertEqual((rc, out.get("hard"), "progress.lock" in out.get("error", "")), (3, True, True),
                         "a lock failure at the claim's mirror is the hard contract, never a final refusal")
        with mock.patch.object(records, "flocked", progress_lock_fails), self.assertRaises(records.RecordError):
            records.write_progress(self.root, lambda r: r)
        with mock.patch.object(records.fcntl, "flock", side_effect=OSError(errno.ENOLCK, "no locks")), \
                self.assertRaises(records.RecordError):
            records.write_progress(self.root, lambda r: r)
        with self.assertRaises(records.RecordError):
            records.write_progress(self.root, mock.Mock(side_effect=OSError(errno.EIO, "in the mutate")))

    def claims_lock_fails(self, times=1):
        real, left = records.flocked, [times]

        @contextlib.contextmanager
        def flocked(path, blocking=True):
            if Path(path).name == records.CLAIMS_LOCK and left[0] > 0:
                left[0] -= 1
                raise OSError(errno.EIO, "claims lock I/O error")
            with real(path, blocking) as fd:
                yield fd
        return mock.patch.object(records, "flocked", flocked)

    def test_every_verb_retries_a_claims_lock_failure_before_its_step_and_answers_hard_after_it(self):
        t = self.task("task-steps")
        for args in (("claim", t), ("interrupt", t), ("authorize", t, "1", "--text-hash", "h1"),
                     ("confirm", t, "turn-1", "tok", "--text-hash", "h1"), ("publish", t)):
            with self.claims_lock_fails():
                rc, out = self.verb(*args)
            self.assertEqual((rc, out.get("retry"), "did not commit" in out["refused"]), (1, True, True), args[0])
        with self.claims_lock_fails():
            rc, out = self.verb("stage", t, "turn-1", stdin=b"body")
        self.assertEqual((rc, out.get("retry")), (1, True), "stage")
        self.assertFalse((self.results / (t + ".txt")).exists())
        rc, out = self.verb("claim", t)
        self.assertEqual(rc, 0, "nothing committed: the retry claims it")
        with self.claims_lock_fails():
            self.assertEqual(self.verb("authorize", t, "1", "--text-hash", "h1")[1].get("retry"), True)
        rc, out = self.verb("authorize", t, "1", "--text-hash", "h1")
        self.assertEqual(rc, 0, "and the retried authorize issues the one token")
        with mock.patch.object(records, "write_progress", side_effect=records.RecordError("mirror")):
            rc, out = self.verb("confirm", t, "turn-1", out["token"], "--text-hash", "h1")
        self.assertEqual((rc, out.get("hard"), out.get("turn_id")), (3, True, "turn-1"),
                         "after the step committed: hard, the answer saved")

    def test_a_wrong_shaped_record_is_hard_and_the_claim_stays_recoverable(self):
        lc = inbox._lifecycle(self.root)
        for bad in (1, "bad", ["x"], {"t": 1}):
            t = self.task("task-shape%d" % len(str(bad)))
            records.plugin_path(self.root).write_text(json.dumps({"inflight": bad}))
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, out.get("hard")), (3, True), (bad, out))
            self.assertIn("inflight", out["error"])
            self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.PREPARED, "the committed claim stands")
            self.assertTrue(records.read_progress_alarm(cli.config_dir()), "and the launcher's alarm is up")
            records.plugin_path(self.root).unlink()
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, out["admission"]), (0, "owner"), "recovered once the record is sound")
            self.assertEqual(records.read_progress_alarm(cli.config_dir()), {})
        records.plugin_path(self.root).write_text(json.dumps({"stuck": {"t": 3}}))
        with self.assertRaises(records.RecordError):
            records.load_plugin(self.root)
        records.plugin_path(self.root).unlink()
        for error in (TypeError("int is not iterable"), ValueError("bad"), KeyError("k")):
            with self.assertRaises(records.RecordError) as e:
                records.write_progress(self.root, mock.Mock(side_effect=error))
            self.assertIn(type(error).__name__, str(e.exception))

    def failing_claim_json(self, error, times=1):
        real, left = inbox.lifecycle.write_durable, [times]

        def write(path, data):
            if Path(path).name == "claim.json" and left[0] > 0:
                left[0] -= 1
                raise error
            return real(path, data)
        return mock.patch.object(inbox.lifecycle, "write_durable", side_effect=write)

    def test_a_store_write_that_fails_is_a_retry_by_disposition_and_a_decision_stays_final(self):
        lc = inbox._lifecycle(self.root)
        t = self.task("task-enospc")
        with self.failing_claim_json(OSError(errno.ENOSPC, "No space left on device")):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        self.assertIsNone(lc.claims.load(t), "nothing committed")
        rc, out = self.verb("claim", t)
        self.assertEqual(rc, 0, "the healthy retry claims it")
        rc, out = self.verb("authorize", t, "1", "--text-hash", "h1")
        token = out["token"]

        class Unanticipated(Exception):
            pass
        for error in (OSError(errno.ENOSPC, "No space left on device"), Unanticipated("surprise")):
            with self.failing_claim_json(error):
                rc, out = self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")
            self.assertEqual((rc, out.get("retry")), (1, True), (error, out))
            self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.CALLING, "the claim is as it was")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "confirm_turn",
                               side_effect=inbox.lifecycle.ClaimUnreadable("torn claim.json")):
            rc, out = self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")
        self.assertEqual((rc, out.get("retry")), (1, True), "an unreadable claim resolved nothing: a retry")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.CALLING)
        rc, out = self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")
        self.assertEqual((rc, out["turn_id"]), (0, "turn-1"), "the reconfirm keeps its turn")
        u = self.task("task-unreadable")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "decide", side_effect=inbox.lifecycle.ClaimUnreadable("torn")):
            rc, out = self.verb("claim", u)
        self.assertEqual((rc, out.get("retry")), (1, True), "at claim too")
        self.assertIsNone(lc.claims.load(u), "unchanged")
        self.assertEqual(self.verb("claim", u)[0], 0, "the healthy retry claims it")
        rc, out = self.verb("confirm", t, "turn-2", "bad", "--text-hash", "h1")
        self.assertEqual((rc, "retry" in out), (1, False), "TurnRefused stays final")
        rc, out = self.verb("claim", self.task("task-guest", "guest"))
        self.assertEqual((rc, "retry" in out, "not admitted" in out["refused"]), (1, False, True), "the admission refusal")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "prepare", side_effect=inbox.lifecycle.StateError("not fenced")):
            rc, out = self.verb("claim", self.task("task-fence"))
        self.assertEqual((rc, "retry" in out), (1, False), "a StateError about what the state says stays final")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "prepare", side_effect=inbox.lifecycle.AttemptInProgress("busy")):
            self.assertEqual("retry" in self.verb("claim", self.task("task-busy"))[1], False)
        self.assertTrue(issubclass(inbox.lifecycle.LedgerIOError, inbox.lifecycle.StateIOError))
        self.assertIsInstance(inbox.lifecycle._state_io("write", "x", OSError("e")), inbox.lifecycle.StateIOError)

    def test_anything_after_the_commit_point_is_hard_never_a_retry(self):
        lc = inbox._lifecycle(self.root)
        real = records.flocked

        @contextlib.contextmanager
        def release_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                raise OSError(errno.EIO, "unlock I/O error")
        t = self.task("task-release")
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("hard"), "retry" in out), (3, True, False), out)
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.PREPARED, "the committed claim is kept")
        rc, out = self.verb("claim", t)
        self.assertEqual(rc, 0, "and claimed again for this generation")
        token = self.verb("authorize", t, str(out["attempt"]), "--text-hash", "h1")[1]["token"]
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("confirm", t, "turn-9", token, "--text-hash", "h1")
        self.assertEqual((rc, out.get("hard"), out.get("turn_id")), (3, True, "turn-9"), "hard, the turn kept")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.TURN_CONFIRMED)
        real_fsync = inbox.lifecycle.fsync_dir
        calls = []

        def fsync_after_rename_fails(path):
            calls.append(path)
            if any(n.endswith("claim.json") for n in os.listdir(path)) and len(calls) > 1:
                raise OSError(errno.EIO, "dir fsync")
            return real_fsync(path)
        u = self.task("task-fsync")
        with mock.patch.object(inbox.lifecycle, "fsync_dir", side_effect=fsync_after_rename_fails):
            rc, out = self.verb("claim", u)
        self.assertEqual((rc, out.get("hard"), "retry" in out), (3, True, False), out)
        self.assertIn("landed", out["error"])
        self.assertEqual(lc.claims.load(u)["phase"], inbox.lifecycle.PREPARED, "the renamed claim stands")
        with self.assertRaises(inbox.lifecycle.RenamedNotDurable), \
                mock.patch.object(inbox.lifecycle, "fsync_dir", side_effect=OSError(errno.EIO, "x")):
            inbox.lifecycle.write_durable(Path(self.root) / "state" / "probe", b"x")
        with mock.patch.object(inbox.lifecycle, "fsync_dir", side_effect=OSError(errno.EIO, "x")), \
                self.assertRaises(inbox.lifecycle.StateCommitted):
            lc.claims.stage(u, b"body")

    def confirmed(self, t):
        self.task(t)
        attempt = self.verb("claim", t)[1]["attempt"]
        token = self.verb("authorize", t, str(attempt), "--text-hash", "h1")[1]["token"]
        self.assertEqual(self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")[0], 0)

    def test_a_token_is_bound_to_its_request_and_a_lost_reply_is_reconcilable(self):
        """Authorize A commits and the lock release fails; authorize B (another text) gets no token; authorize
        A again gets the stored one; a committed step whose claim cannot be re-read is a retry, not hard."""
        lc = inbox._lifecycle(self.root)
        real = records.flocked

        @contextlib.contextmanager
        def release_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                raise OSError(errno.EIO, "unlock I/O error")
        t = self.task("task-bind")
        attempt = str(self.verb("claim", t)[1]["attempt"])
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("authorize", t, attempt, "--text-hash", "hA")
            self.assertEqual((rc, out.get("token")), (3, lc.claims.load(t)["call"]["nonce"]))
            rc, out = self.verb("authorize", t, attempt, "--text-hash", "hB")
            self.assertEqual((rc, "token" in out, "retry" in out), (1, False, False), "another text: refused, no token")
        rc, out = self.verb("authorize", t, attempt, "--text-hash", "hB")
        self.assertEqual((rc, "token" in out), (1, False))
        rc, out = self.verb("authorize", t, attempt, "--text-hash", "hA")
        self.assertEqual((rc, out["token"]), (0, lc.claims.load(t)["call"]["nonce"]), "the same request: the stored token")
        rc, out = self.verb("authorize", t, str(int(attempt) + 1), "--text-hash", "hA")
        self.assertEqual((rc, "token" in out), (1, False), "the same text for another attempt gets none")
        rec = lc.claims.load(t)
        self.assertIsNone(inbox._token_for(rec, rec["attempt"] + 1, "hA"))
        self.assertEqual(inbox._token_for(rec, rec["attempt"], "hA"), rec["call"]["nonce"])
        u = self.task("task-reread")
        attempt = str(self.verb("claim", u)[1]["attempt"])
        real_load = inbox.lifecycle.ClaimStore.load
        calls = []

        def load_fails_after_commit(store, task_id):
            calls.append(task_id)
            if task_id == u and store.load_fail_armed:
                raise inbox.lifecycle.ClaimUnreadable("torn on re-read")
            return real_load(store, task_id)
        inbox.lifecycle.ClaimStore.load_fail_armed = False

        @contextlib.contextmanager
        def release_fails_then_reread_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                inbox.lifecycle.ClaimStore.load_fail_armed = True
                raise OSError(errno.EIO, "unlock I/O error")
        try:
            with mock.patch.object(records, "flocked", release_fails_then_reread_fails), \
                    mock.patch.object(inbox.lifecycle.ClaimStore, "load", load_fails_after_commit):
                rc, out = self.verb("authorize", u, attempt, "--text-hash", "hA")
        finally:
            del inbox.lifecycle.ClaimStore.load_fail_armed
        self.assertEqual((rc, out.get("retry"), "hard" in out), (1, True, False), "never hard without a token")
        self.assertEqual(lc.claims.load(u)["phase"], inbox.lifecycle.CALLING, "the authorization did commit")
        rc, out = self.verb("authorize", u, attempt, "--text-hash", "hA")
        self.assertEqual((rc, out["token"]), (0, lc.claims.load(u)["call"]["nonce"]), "reconciled by the next ask")
        token = out["token"]
        self.assertEqual(self.verb("confirm", u, "turn-1", token, "--text-hash", "hA")[0], 0)
        rc, out = self.verb("confirm", u, "turn-1", token, "--text-hash", "hA")
        self.assertEqual((rc, out["turn_id"]), (0, "turn-1"), "the same confirmation again is its own answer")
        rc, out = self.verb("confirm", u, "turn-1", "0" * 32, "--text-hash", "hA")
        self.assertEqual(rc, 1, "the same turn with another nonce is refused")
        self.assertEqual(self.verb("confirm", u, "turn-2", token, "--text-hash", "hA")[0], 1, "another turn is refused")
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("authorize", self.task("task-noprep"), "9", "--text-hash", "h")
        self.assertEqual((rc, "retry" in out), (1, False), "an absent claim: refused")
        p = self.task("task-wrong-attempt")
        self.verb("claim", p)
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("authorize", p, "9", "--text-hash", "h")
        self.assertEqual((rc, out.get("retry")), (1, True), "still PREPARED, nothing committed: a retry")

    def test_publish_and_park_deliver_the_recorded_answer_of_an_unfinished_stage(self):
        lc = inbox._lifecycle(self.root)
        for verb, t in (("publish", "task-fin-pub"), ("park", "task-fin-park")):
            self.confirmed(t)
            self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the accepted answer\n")[0], 0)
            rec = lc.claims.load(t)
            lc.claims.save(dict(rec, phase=inbox.lifecycle.TURN_CONFIRMED))     # the move's last save never landed
            args = (verb, t) if verb == "publish" else (verb, t, "turn-1", "--detail", "turn completed without a reply")
            rc, out = self.verb(*args)
            self.assertEqual((rc, out["outcome"]), (0, "settled"), (verb, out))
            self.assertEqual((self.results / (t + ".txt")).read_text(), "the accepted answer\n",
                             "%s delivers the recorded body, never a second one" % verb)
            self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.SETTLED)
        t = "task-fin-missing"
        self.confirmed(t)
        real_link = os.link
        with mock.patch.object(inbox.lifecycle.os, "link", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            rc, out = self.verb("stage", t, "turn-1", stdin=b"never linked\n")
        self.assertEqual((rc, out.get("retry")), (1, True), "this answer is accepted, its staging unfinished: a retry")
        self.assertTrue(lc.claims.load(t).get("staged"), "the descriptor is recorded, its blob is not")
        rc, out = self.verb("stage", t, "turn-1", stdin=b"another answer\n")
        self.assertEqual((rc, "retry" in out), (1, False), "a different body is refused for good")
        rc, out = self.verb("publish", t)
        self.assertEqual((rc, out.get("retry")), (1, True), "no verifying blob yet: a retry, nothing published")
        rc, out = self.verb("park", t, "turn-1", "--detail", "x")
        self.assertEqual(rc, 1, "and never the protocol failure in place of the accepted answer")
        self.assertFalse((self.results / (t + ".txt")).exists())
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"never linked\n")[0], 0, "the identical retry finishes")
        self.assertEqual(self.verb("publish", t)[0], 0)
        self.assertEqual((self.results / (t + ".txt")).read_text(), "never linked\n")
        del real_link
        real = records.flocked

        @contextlib.contextmanager
        def release_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                raise OSError(errno.EIO, "unlock I/O error")
        t = "task-pub-release"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"done\n")[0], 0)
        with mock.patch.object(records, "flocked", release_fails):
            rc, out = self.verb("publish", t)
        self.assertEqual((rc, out.get("hard"), out.get("outcome")), (3, True, "settled"),
                         "a delivered answer is never reported refused")

    def test_a_hard_reply_is_read_from_the_claim_as_it_stands(self):
        """The hard reply after a committed step carries what the re-read claim proves: authorize's stored
        token while CALLING, stage's "staged" only at RESULT_STAGED, else the actual phase."""
        lc = inbox._lifecycle(self.root)
        real = records.flocked

        @contextlib.contextmanager
        def release_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                raise OSError(errno.EIO, "unlock I/O error")
        real_fsync = inbox.lifecycle.fsync_dir

        def fsync_after(name):
            def fsync(path):
                if Path(path).name not in ("claims",) and any(n == name for n in os.listdir(path)):
                    raise OSError(errno.EIO, "dir fsync")
                return real_fsync(path)
            return fsync
        for t, failing in (("task-auth-release", mock.patch.object(records, "flocked", release_fails)),
                           ("task-auth-fsync", mock.patch.object(inbox.lifecycle, "fsync_dir",
                                                                 side_effect=fsync_after("claim.json")))):
            self.task(t)
            attempt = self.verb("claim", t)[1]["attempt"]
            with failing:
                rc, out = self.verb("authorize", t, str(attempt), "--text-hash", "h1")
            rec = lc.claims.load(t)
            self.assertEqual((rc, out.get("hard"), out.get("phase")), (3, True, "CALLING"), (t, out))
            self.assertEqual(out["token"], rec["call"]["nonce"], "the stored one-use token: go ahead, once")
            self.assertEqual(rec["phase"], inbox.lifecycle.CALLING)
            rc, out = self.verb("authorize", t, str(attempt), "--text-hash", "h1")
            self.assertEqual((rc, out["token"]), (0, rec["call"]["nonce"]), "never a second token: the same one again")
            self.assertEqual(self.verb("authorize", t, str(attempt), "--text-hash", "h2")[0], 1, "none for another text")
        t = "task-auth-fsync"
        token = lc.claims.load(t)["call"]["nonce"]
        self.assertEqual(self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")[0], 0)
        with mock.patch.object(inbox.lifecycle, "fsync_dir", side_effect=fsync_after("claim.json")):
            rc, out = self.verb("stage", t, "turn-1", stdin=b"answer\n")
        self.assertEqual((rc, out.get("hard"), out.get("phase"), "outcome" in out), (3, True, "TURN_CONFIRMED", False),
                         "a write landed but the claim never reached RESULT_STAGED: never 'staged'")
        with mock.patch.object(inbox.lifecycle.ClaimStore, "load", side_effect=inbox.lifecycle.ClaimUnreadable("torn")):
            self.assertIsNone(inbox._committed(lc, t, "stage"), "unreadable: the caller answers retry")
        self.assertEqual(inbox._committed(lc, "task-none", "authorize"), {"task_id": "task-none", "phase": "absent"})
        self.assertEqual(records.progress_alarm_path(cli.config_dir()).parent, cli.config_dir(),
                         "the alarm lives in the config dir, outside state/")
        self.assertNotIn(str(Path(self.root) / "state"), str(records.progress_alarm_path(cli.config_dir())))

    def test_an_unwritable_progress_record_alarms_the_launcher_verify_and_the_card_until_a_write_lands(self):
        t = self.task("task-alarm")
        with mock.patch.object(records, "write_progress", side_effect=records.RecordError("progress.lock: EIO")):
            self.assertEqual(self.verb("progress", "--stuck", t)[0], 3)
        alarm = records.read_progress_alarm(cli.config_dir())
        self.assertIn("progress.lock: EIO", alarm["error"])
        with mock.patch.object(records, "write_progress", side_effect=records.RecordError("still broken")):
            self.assertEqual(self.verb("claim", t)[0], 3)
        self.assertEqual(records.read_progress_alarm(cli.config_dir())["since"], alarm["since"], "the first failure's time")
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            out = self.main("verify")[1]
        self.assertIn("FAIL progress: progress record unwritable since", out)
        lc = launcher.Launcher(dict(cli.read_config(cli.config_path()), ROOT=self.root), cli.config_dir())
        with mock.patch.object(lc.tm, "capture", return_value="pane"), \
                mock.patch.object(records, "load_plugin", side_effect=records.RecordError("torn")):
            obs = lc.observe({"generation": {"pane_id": "%0"}, "stall": None})
        self.assertEqual(obs["stall"]["reason"], "abnormal:progress-unwritable", "the launcher's own record names it")
        self.assertIn("still broken", obs["stall"]["checks"])
        self.assertEqual(self.verb("progress", "--intake", "open")[0], 0, "the first durable write")
        self.assertEqual(records.read_progress_alarm(cli.config_dir()), {})
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            self.assertNotIn("FAIL progress", self.main("verify")[1])
        self.assertIsNone(detector.derive({}, "pane", records.load_plugin(self.root), 1.0)["stall"])
        records.progress_alarm_path(cli.config_dir()).write_text("[")
        self.assertEqual(records.read_progress_alarm(cli.config_dir())["since"], None, "an unreadable alarm stays raised")
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            self.assertIn("since an unknown time", self.main("verify")[1])
        obs = detector.derive({}, "pane", {"progress_unwritable": {"since": None, "error": "x"}}, 5.0)
        self.assertEqual(obs["stall"]["since"], 5.0)
        with mock.patch.object(records, "write_durable", side_effect=OSError(errno.EROFS, "ro")):
            records.raise_progress_alarm(cli.config_dir(), "best effort")      # never raises
        records.clear_progress_alarm(cli.config_dir())
        records.clear_progress_alarm(cli.config_dir())                          # idempotent

    def test_a_stuck_interrupt_is_named_by_verify_and_raises_the_card_and_clears(self):
        self.assertEqual(self.verb("progress", "--stuck", "task-s", "--stuck-detail", "parked")[0], 0)
        self.assertEqual(self.verb("progress", "--stuck", "task-s", "--stuck-detail", "still parked")[0], 0)
        marks = records.load_plugin(self.root)["stuck"]
        self.assertEqual(list(marks), ["task-s"])
        self.assertEqual(marks["task-s"]["detail"], "still parked", "the first time is kept, the detail refreshed")
        obs = detector.derive({}, "pane", records.load_plugin(self.root), 1000.0)
        self.assertEqual((obs["stall"]["reason"], obs["stall"]["task"]), ("abnormal:task-unanswered", "task-s"))
        self.assertEqual(detector.derive(obs, "other pane", records.load_plugin(self.root), 2000.0)["stall"], obs["stall"],
                         "the same stall across ticks, so one card")
        with mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            out = self.main("verify")[1]
        self.assertIn("FAIL unanswered: tasks without an answer, retried with backoff: task-s since", out)
        self.assertIn("(still parked)", out)
        self.assertEqual(self.verb("progress", "--unstuck", "task-s")[0], 0)
        self.assertEqual(records.load_plugin(self.root)["stuck"], {})
        self.assertIsNone(detector.derive({}, "pane", records.load_plugin(self.root), 1000.0)["stall"])
        self.assertNotIn("unanswered", self.main("verify")[1])
        records.plugin_path(self.root).write_text("[")
        self.assertNotIn("unanswered", self.main("verify")[1], "an unreadable record names no stuck answer, never crashes")
        self.assertEqual(detector.stuck_answers({"stuck": {"a": {"since": "x"}, "b": 3}}), {})
        self.assertEqual(detector.stuck_answers({"stuck": ["a"]}), {})

    def test_with_allow_non_owner_every_non_owner_is_claimed_with_its_admission_and_a_tierless_task_still_refused(self):
        self.configure(ALLOW_NON_OWNER="1")
        for t, tier, extra, kind in (("task-t", "team", "", "non_owner"), ("task-g", "guest", "", "non_owner"),
                                     ("task-c", "team", "collaborator: true\n", "collaborator")):
            rc, out = self.verb("claim", self.task(t, tier, extra))
            self.assertEqual((rc, out["admission"], out["access_tier"]), (0, kind, tier), t)
        rc, out = self.verb("claim", self.task("task-n", None))
        self.assertEqual((rc, "malformed" in out["refused"]), (1, True))
        self.assertEqual(self.verb("claim", self.task("task-o"))[1]["admission"], "owner", "owner tasks unchanged")

    def test_claim_authorize_confirm_stage_publish_through_the_cli_with_the_record_mirrored(self):
        t = self.task()
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, out["attempt"], out["generation"]["pid"]), (0, 1, self.consumer))
        self.assertEqual(records.load_plugin(self.root)["inflight"][t], {"attempt": 1, "phase": "PREPARED"})
        rc, out = self.verb("authorize", t, "1", "--text-hash", "h1")
        token = out["token"]
        self.assertEqual((rc, len(token)), (0, 32))
        self.assertEqual(records.load_plugin(self.root)["inflight"][t]["phase"], "CALLING")
        rc, out = self.verb("authorize", t, "1", "--text-hash", "h1")
        self.assertEqual((rc, out["token"]), (0, token), "one token per attempt: the same request gets that one again")
        rc, out = self.verb("authorize", t, "1", "--text-hash", "h2")
        self.assertEqual((rc, "no submit authorization" in out["refused"]), (1, True), "and another text gets none")
        rc, out = self.verb("confirm", t, "turn-1", "bad", "--text-hash", "h1")
        self.assertEqual((rc, "nonce or text hash" in out["refused"]), (1, True))
        rc, out = self.verb("confirm", t, "turn-1", token, "--text-hash", "h2")
        self.assertEqual(rc, 1)
        rc, out = self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")
        self.assertEqual((rc, out["turn_id"]), (0, "turn-1"))
        self.assertEqual(records.load_plugin(self.root)["inflight"][t], {"turn_id": "turn-1", "phase": "TURN_CONFIRMED"})
        rc, out = self.verb("stage", t, "turn-9", stdin=b"four\n")
        self.assertEqual((rc, "parked" in out["refused"]), (1, True), "only the recorded turn stages")
        rc, out = self.verb("stage", t, "turn-1", stdin=b"four\n")
        self.assertEqual((rc, out["outcome"]), (0, "staged"))
        rc, out = self.verb("stage", t, "turn-1", stdin=b"four\n")
        self.assertEqual(rc, 0, "an identical retry finishes")
        rc, out = self.verb("stage", t, "turn-1", stdin=b"five\n")
        self.assertEqual(rc, 1, "different bytes park with the first")
        rc, out = self.verb("publish", t)
        self.assertEqual((rc, out["outcome"]), (0, "settled"))
        self.assertEqual((self.results / "task-a.txt").read_bytes(), b"four\n")
        self.assertNotIn(t, records.load_plugin(self.root)["inflight"], "released at SETTLED")
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "LIVE_READY" in out["refused"]), (1, True), "only ABSENT may be submitted")

    def test_claim_refuses_without_a_live_consumer_and_a_second_cli_gets_the_same_attempt_unchanged(self):
        from ag2_sparrow import task_lifecycle as tl
        t = self.task()
        rc, out = self.verb("claim", t)
        self.assertEqual(rc, 0)
        claim_path = self.work / "state" / "claims" / t / "claim.json"
        before = claim_path.read_bytes()
        r = subprocess.run([sys.executable, "-c", covered_code("import sys; sys.path.insert(0, %r); from ag2_sparrow.adapters.claude_code import cli\n"
                            "sys.exit(cli.main(['inbox', '--root', %r, 'claim', %r]))" % (str(PKG), self.root, t))],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, json.loads(r.stdout)["attempt"]), (0, 1), "CLI B under the same generation gets the attempt back")
        self.assertEqual(claim_path.read_bytes(), before, "bytes unchanged")
        other = self.task("task-o")
        lc = tl.Lifecycle(self.root, lineage=lambda *a: True)
        lc.prepare(other, "x", mode=tl.LOCAL, generation={"pid": os.getpid(), "start": holder_lock.start_time(os.getpid())})
        rc, out = self.verb("claim", other)
        self.assertEqual((rc, "PREPARED by a live or unobserved writer" in out["refused"]), (1, True), "another live generation's claim")
        for live in (None, False):
            with mock.patch.object(task_io, "holder_consumer", return_value={"pid": self.consumer, "start": "s", "live": live}):
                rc, out = self.verb("claim", self.task("task-u%s" % live))
            self.assertEqual((rc, "no live consumer" in out["refused"]), (1, True), "an unobserved or dead consumer binds nothing")
        unhold(self.holder)
        self.holder = type("H", (), {"stdin": io.StringIO(), "wait": lambda self_, timeout=0: None})()
        self.assertTrue(wait_gone(self.consumer))
        rc, out = self.verb("claim", self.task("task-n"))
        self.assertEqual((rc, "no live consumer" in out["refused"]), (1, True))

    def test_park_publishes_the_protocol_failure_once_and_never_replays(self):
        t = self.task()
        self.verb("claim", t)
        token = self.verb("authorize", t, "1", "--text-hash", "h")[1]["token"]
        self.verb("confirm", t, "turn-1", token, "--text-hash", "h")
        rc, out = self.verb("park", t, "turn-1", "--detail", "turn aborted without a reply")
        self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", "turn aborted without a reply"))
        self.assertIn("ended without a reply (turn aborted without a reply)", (self.results / "task-a.txt").read_text())
        delivered = (self.results / "task-a.txt").read_bytes()
        rc, out = self.verb("park", t, "turn-1", "--detail", "turn aborted without a reply")
        self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", "turn aborted without a reply"),
                         "the identical park again is its own answer: settled, the protocol failure named")
        rc, out = self.verb("park", t, "turn-1")
        self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", False), "another detail: not what was delivered")
        self.assertEqual((self.results / "task-a.txt").read_bytes(), delivered, "a settled claim parks nothing again")
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"late\n")[0], 1, "and stages nothing")

    def test_a_park_whose_reply_was_lost_after_it_settled_answers_settled_on_the_identical_retry(self):
        """The compound I/O case: the park's publication settled, the lock's release failed and the claim could
        not be re-read (retry); the Mod's identical park then answers settled, never 'staging parked', so it
        retires the offer instead of retaining it until a restart."""
        t = self.task("task-lost-park")
        attempt = self.verb("claim", t)[1]["attempt"]
        token = self.verb("authorize", t, str(attempt), "--text-hash", "h1")[1]["token"]
        self.assertEqual(self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")[0], 0)
        real, releases = records.flocked, []

        @contextlib.contextmanager
        def release_fails(path, blocking=True):
            with real(path, blocking) as fd:
                yield fd
            if Path(path).name == records.CLAIMS_LOCK:
                releases.append(path)
                if len(releases) == 2:      # the park's stage released; its publish, SETTLED, cannot
                    raise OSError(errno.EIO, "unlock I/O error")
        with mock.patch.object(records, "flocked", release_fails), mock.patch.object(inbox, "_committed", return_value=None):
            rc, out = self.verb("park", t, "turn-1", "--detail", "turn completed without a reply")
        self.assertEqual((rc, out.get("retry"), len(releases)), (1, True, 2), out)
        self.assertIn("could not be re-read", out["refused"])
        self.assertEqual(inbox._lifecycle(self.root).claims.load(t)["phase"], inbox.lifecycle.SETTLED)
        delivered = (self.results / (t + ".txt")).read_bytes()
        self.assertIn(b"ended without a reply (turn completed without a reply)", delivered, "the result exists")
        rc, out = self.verb("park", t, "turn-1", "--detail", "turn completed without a reply")
        self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", "turn completed without a reply"), out)
        self.assertEqual((self.results / (t + ".txt")).read_bytes(), delivered, "published once")
        self.assertNotIn(t, records.load_plugin(self.root).get("inflight") or {})

    def test_a_publication_that_fails_on_io_is_the_pre_commit_retry_with_the_call_again_hint(self):
        """An unwritable results dir: publish answers retry (the step did not commit), as the Mod's reply tool
        turns into 'call ag2space_reply again'; never the soft 'publication parked'. The turn-end park of a
        staged answer carries the same retry."""
        if os.geteuid() == 0:
            self.skipTest("root writes an unwritable dir")
        lc = inbox._lifecycle(self.root)
        t, p = "task-ro-results", "task-ro-park"
        self.confirmed(t)
        self.confirmed(p)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the answer\n")[0], 0)
        self.results.chmod(0)
        self.addCleanup(self.results.chmod, 0o755)
        try:
            rc, out = self.verb("publish", t)
            self.assertEqual((rc, out.get("retry")), (1, True), out)
            self.assertIn("the step did not commit (StateWriteError: refusing to serve: cannot publish %s"
                          % (self.results / (t + ".txt")), out["refused"])
            self.assertNotIn("publication parked", out["refused"])
            self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.RESULT_STAGED, "the claim is as it was")
            rc, out = self.verb("park", t, "turn-1", "--detail", "turn completed without a reply")
            self.assertEqual((rc, out.get("retry")), (1, True), "the turn's end gets the same retry")
            rc, out = self.verb("park", p, "turn-1", "--detail", "turn completed without a reply")
            self.assertEqual((rc, out.get("retry")), (1, True), "the failure staged, its publication a retry")
            self.assertEqual(lc.claims.load(p)["phase"], inbox.lifecycle.RESULT_STAGED)
        finally:
            self.results.chmod(0o755)
        with mock.patch.object(inbox.lifecycle.Lifecycle, "read_final", side_effect=OSError(errno.EIO, "I/O error")):
            rc, out = self.verb("publish", t)
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        self.assertIn("%s: the result landed, its durability unknown (refusing to serve: %s landed, then [Errno 5] I/O error); retried"
                      % (t, self.results / (t + ".txt")), out["refused"], "after the rename, never 'did not commit'")
        self.assertEqual((self.results / (t + ".txt")).read_text(), "the answer\n", "visible already")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.RESULT_STAGED)
        rc, out = self.verb("publish", t)
        self.assertEqual((rc, out["outcome"]), (0, "settled"), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), "the answer\n", "the staged answer, once")
        self.assertEqual(lc.claims.load(t)["published"]["won"], True, "settled on the result it found")
        self.assertEqual(self.verb("publish", t)[1]["outcome"], "settled", "idempotent at SETTLED")
        self.assertEqual(lc.publish_and_settle("task-none"), "parked", "the default still parks")
        rc, out = self.verb("park", p, "turn-1", "--detail", "turn completed without a reply")
        self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", "turn completed without a reply"),
                         "the identical park names the protocol failure it delivered, never 'the accepted answer'")
        self.assertIn("ended without a reply (turn completed without a reply)", (self.results / (p + ".txt")).read_text())

    def test_an_interruption_whose_claim_rename_landed_without_its_directory_fsync_is_resumed_by_the_next_claim(self):
        """EIO at the directory fsync after the claim.json rename inside the live reclaim's interruption: the
        reply is hard with the committed phase RESULT_STAGED (one durable transition, no intermediate), and
        the next same-generation claim publishes the durable interrupted body, never retiring the task; a
        record an older write left committed at OUTCOME_UNKNOWN is resumed the same way. No holder death."""
        lc = inbox._lifecycle(self.root)
        t = "task-fsync-after-rename"
        self.confirmed(t)
        with mock.patch.object(inbox.lifecycle.os, "link", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.verb("stage", t, "turn-1", stdin=b"accepted text\n")
        real_replace, real_fsync_dir, last, hits = os.replace, inbox.lifecycle.fsync_dir, [], []

        def replace_noting(src, dst):
            last[:] = [Path(dst).name]
            return real_replace(src, dst)

        def fsync_dir_eio_after_claim_rename(path):
            if last == ["claim.json"] and not hits:
                hits.append(path)
                raise OSError(errno.EIO, "I/O error")
            return real_fsync_dir(path)
        with mock.patch.object(inbox.lifecycle.os, "replace", replace_noting), \
                mock.patch.object(inbox.lifecycle, "fsync_dir", fsync_dir_eio_after_claim_rename):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("hard"), out.get("phase"), len(hits)), (3, True, inbox.lifecycle.RESULT_STAGED, 1), out)
        self.assertIn("claim.json was replaced, then its directory fsync failed", out["error"])
        self.assertFalse((self.results / (t + ".txt")).exists(), "the publication never ran")
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "retry" in out, "its staged answer is published now" in out["refused"]), (1, False, True), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), inbox.INTERRUPTED_BODY, "the durable body, published")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.SETTLED)
        u = "task-outcome-unknown"
        self.confirmed(u)
        rec = lc.claims.load(u)
        lc.claims.stage(u, inbox.INTERRUPTED_BODY.encode("utf-8"))
        lc.claims.save(dict(rec, phase=inbox.lifecycle.OUTCOME_UNKNOWN, staged=None,
                            outcome={"kind": "unknown", "detail": "an older write", "at": 1.0}))
        rc, out = self.verb("claim", u)
        self.assertEqual((rc, "retry" in out, out["refused"]),
                         (1, False, "%s: its recorded outcome, interrupted, is published now; only ABSENT may be submitted" % u))
        self.assertEqual((self.results / (u + ".txt")).read_text(), inbox.INTERRUPTED_BODY, "a committed intermediate is resumed, never retired")
        self.assertEqual(self.verb("claim", u)[1]["refused"], "%s is LIVE_READY; only ABSENT may be submitted" % u)

    def test_one_recovery_pass_then_the_live_claim_delivers_a_dead_generations_verified_answer_once(self):
        """Hold's cadence: ONE recovery pass per start, then the new generation's live claims; no second pass.
        A verifying blob with the blob writer failing is delivered by that one pass (no blob write); one a pass
        could not publish (results I/O) or never touched (lineage unknown) is delivered by the live claim, once."""
        lc = inbox._lifecycle(self.root)
        dead = {"pid": 2 ** 22 - 1, "start": "2026-01-01T00:00:00Z"}

        def staged_then_rolled_back(t, body):
            self.confirmed(t)
            self.assertEqual(self.verb("stage", t, "turn-1", stdin=body)[0], 0)
            lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED, generation=dead))
        a, b = "task-dead-blob-eio", "task-dead-publish-eio"
        staged_then_rolled_back(a, b"the verified answer\n")
        staged_then_rolled_back(b, b"the other answer\n")
        real_publish = inbox.lifecycle.Lifecycle._publish

        def publish_eio_for_b(life, rec):
            if rec["task_id"] == b:
                raise OSError(errno.EIO, "I/O error")
            return real_publish(life, rec)
        with mock.patch.object(inbox.lifecycle.Lifecycle, "_commit_blob", side_effect=OSError(errno.EIO, "x")) as blob, \
                mock.patch.object(inbox.lifecycle.Lifecycle, "_publish", publish_eio_for_b), \
                mock.patch.object(task_io, "verify_lineage", return_value=False):
            self.assertEqual(inbox.recover_dead_generation(self.root), 1)
        self.assertEqual(blob.call_count, 0, "a verified blob is final: never written again")
        self.assertEqual(((self.results / (a + ".txt")).read_text(), lc.claims.load(a)["phase"]),
                         ("the verified answer\n", inbox.lifecycle.SETTLED), "delivered by the one pass")
        self.assertEqual((lc.claims.load(b)["phase"], (self.results / (b + ".txt")).exists()), (inbox.lifecycle.RESULT_STAGED, False))
        rc, out = self.verb("claim", b)
        self.assertEqual((rc, "retry" in out, out["refused"]), (1, False, "%s: its staged answer is published now; only ABSENT may be submitted" % b), out)
        self.assertEqual(((self.results / (b + ".txt")).read_text(), lc.claims.load(b)["phase"]), ("the other answer\n", inbox.lifecycle.SETTLED))
        self.assertEqual(self.verb("claim", a)[1]["refused"], "%s is LIVE_READY; only ABSENT may be submitted" % a)
        c = "task-unknown-lineage"
        staged_then_rolled_back(c, b"the third answer\n")
        with mock.patch.object(task_io, "verify_lineage", return_value=None):
            self.assertEqual(inbox.recover_dead_generation(self.root), 0, "lineage unknown: recovery leaves it")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "_commit_blob", side_effect=OSError(errno.EIO, "x")) as blob:
            rc, out = self.verb("claim", c)
        self.assertEqual((rc, "staged and never published, is published now" in out["refused"], blob.call_count), (1, True, 0), out)
        self.assertEqual(((self.results / (c + ".txt")).read_text(), lc.claims.load(c)["phase"]), ("the third answer\n", inbox.lifecycle.SETTLED))
        for t in (a, b, c):
            self.assertEqual(sum(1 for p in self.results.iterdir() if p.name == t + ".txt"), 1, "published once")

    def test_a_lost_claims_publication_whose_read_back_fails_after_the_rename_settles_and_clears_in_flight(self):
        """read_final faults after the result rename landed, inside the lost claim's publication: that claim is
        a retry; the next claim finds the result, settles the claim on it, drops the in-flight mirror with the
        stuck mark, and the detector sees nothing in flight (no stall)."""
        lc = inbox._lifecycle(self.root)
        t = "task-lost-publish-rename"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the accepted answer\n")[0], 0)
        lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED))     # the move's last save never landed
        neighbor = {"phase": inbox.lifecycle.TURN_CONFIRMED, "turn_id": "turn-9"}
        records.write_progress(self.root, lambda rec: dict(
            rec, inflight=dict(rec.get("inflight") or {}, **{t: {"phase": inbox.lifecycle.TURN_CONFIRMED, "turn_id": "turn-1"},
                                                            "task-neighbor": neighbor}),
            stuck=dict(rec.get("stuck") or {}, **{t: {"since": 1}, "task-neighbor": {"since": 2}})))
        with mock.patch.object(inbox.lifecycle.Lifecycle, "read_final", side_effect=OSError(errno.EIO, "I/O error")):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        self.assertEqual(((self.results / (t + ".txt")).read_text(), lc.claims.load(t)["phase"]),
                         ("the accepted answer\n", inbox.lifecycle.RESULT_STAGED), "the rename landed")
        plugin = records.load_plugin(self.root)
        self.assertEqual((t in plugin["inflight"], t in plugin["stuck"]), (True, True), "both still seeded")
        with mock.patch.object(records, "write_progress", wraps=records.write_progress) as writes:
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, "retry" in out, out["refused"]), (1, False, "%s is LIVE_READY; only ABSENT may be submitted" % t), out)
        self.assertEqual(writes.call_count, 1, "the in-flight mirror and the stuck mark dropped in one production write")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.SETTLED, "settled on the result it found")
        plugin = records.load_plugin(self.root)
        self.assertNotIn(t, plugin.get("inflight") or {}, "no claim path leaves a finished task in flight")
        self.assertNotIn(t, plugin.get("stuck") or {}, "nor its stuck mark")
        self.assertEqual((plugin["inflight"].get("task-neighbor"), plugin["stuck"].get("task-neighbor")),
                         (neighbor, {"since": 2}), "an unrelated task's entries are untouched")
        # The neighbor is still owed, so the detector reads the record without it: this task adds nothing.
        own = dict(plugin, inflight={k: v for k, v in plugin["inflight"].items() if k != "task-neighbor"},
                   stuck={k: v for k, v in plugin["stuck"].items() if k != "task-neighbor"})
        obs = detector.derive({}, "idle\n", own, 0.0, stall_min_s=10)
        obs = detector.derive(obs, "idle\n", own, 601.0, stall_min_s=10)
        self.assertIsNone(obs["stall"], "no false stall")
        self.assertEqual(sum(1 for p in self.results.iterdir() if p.name == t + ".txt"), 1)

    def test_a_settlement_whose_ledger_append_failed_is_completed_by_the_next_claim_and_compacts(self):
        """The settled-ids append faults once after the SETTLED save: that claim is a retry (SETTLED, no ledger
        entry); the next claim completes the settlement (ledger entry, staged body gone) before its cleanup and
        final refusal; the dead generation's recovery, once the claim is due, compacts it."""
        lc = inbox._lifecycle(self.root)
        t = "task-ledger-fault"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the accepted answer\n")[0], 0)
        lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED))
        lc.claims.stage(t, b"a legacy staged body\n")       # a real file, so an unstage that does nothing fails
        real_add, hits = inbox.lifecycle.SettledIds.add, []

        def add_faults_once(ledger, task_id):
            if not hits:
                hits.append(task_id)
                raise inbox.lifecycle.LedgerIOError("cannot append to settled-ids ([Errno 5] I/O error)")
            return real_add(ledger, task_id)
        with mock.patch.object(inbox.lifecycle.SettledIds, "add", add_faults_once):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry"), hits), (1, True, [t]), out)
        ledger = lambda: inbox._lifecycle(self.root).settled.ids     # noqa: E731
        self.assertEqual((lc.claims.load(t)["phase"], t in ledger()), (inbox.lifecycle.SETTLED, False))
        self.assertEqual((self.results / (t + ".txt")).read_text(), "the accepted answer\n")
        self.assertTrue((lc.claims.dir(t) / "body").is_file(), "the body is still there after the fault")
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "retry" in out, out["refused"]), (1, False, "%s is LIVE_READY; only ABSENT may be submitted" % t), out)
        self.assertIn(t, ledger(), "the settlement completed before the final refusal")
        self.assertFalse((lc.claims.dir(t) / "body").exists(), "the real staged body removed")
        self.assertNotIn(t, records.load_plugin(self.root).get("inflight") or {})
        dead = {"pid": 2 ** 22 - 1, "start": "2026-01-01T00:00:00Z"}
        rec = lc.claims.load(t)
        lc.claims.save(dict(rec, generation=dead, settled_at=rec["settled_at"] - inbox.lifecycle.COMPACT_AFTER - 1))
        with mock.patch.object(task_io, "verify_lineage", return_value=False):
            self.assertEqual(inbox.recover_dead_generation(self.root), 1, "a claim due for compaction brings its generation in")
        self.assertIsNone(lc.claims.load(t), "compacted")
        self.assertIn(t, ledger())
        self.assertEqual(sum(1 for p in self.results.iterdir() if p.name == t + ".txt"), 1, "nothing published twice")

    def test_a_hard_publish_reply_claims_settled_only_over_a_complete_settlement(self):
        """The SETTLED claim.json rename lands and its directory fsync fails: the hard reply names the phase but
        not outcome=settled (the ledger entry and the body removal never ran), so the Mod retries; the identical
        publish again completes the settlement and only then answers settled. park routes the same way."""
        lc = inbox._lifecycle(self.root)
        real_save, real_remove = inbox.lifecycle.ClaimStore.save, inbox.lifecycle.ClaimStore.remove
        for t, call in (("task-hard-publish", lambda t: self.verb("publish", t)),
                        ("task-hard-park", lambda t: self.verb("park", t, "turn-1", "--detail", "aborted"))):
            self.confirmed(t)
            if t == "task-hard-publish":
                self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"answer\n")[0], 0)
            lc.claims.stage(t, b"legacy\n")
            once = []

            def save(store, rec):
                if not once and rec["phase"] == inbox.lifecycle.SETTLED:
                    once.append(1)
                    real_save(store, rec)
                    raise inbox.lifecycle.StateCommitted("claim.json was replaced, then its directory fsync failed")
                return real_save(store, rec)
            with mock.patch.object(inbox.lifecycle.ClaimStore, "save", save):
                rc, out = call(t)
            self.assertEqual((rc, out.get("hard"), out.get("phase"), "outcome" in out, once), (3, True, inbox.lifecycle.SETTLED, False, [1]), out)
            self.assertFalse(lc.settlement_complete(t))
            rc, out = call(t)
            self.assertEqual((rc, out.get("outcome")), (0, "settled"), out)
            self.assertEqual((inbox._lifecycle(self.root).settlement_complete(t), (lc.claims.dir(t) / "body").exists()), (True, False))

    def test_settlement_property_every_entry_path_by_every_fault_by_every_schedule_converges(self):
        """The settlement invariant as one property. For every entry path that settles (publish, park, a non-admitted
        claim, interrupt, a lost claim, recovery's no-result settle, recovery's publication) x one fault at each step
        (the ledger append fails, the append is torn and an already-loaded writer runs next, the whole record lands
        but its fsync fails, the staged-body unlink fails, the SETTLED claim.json rename lands and its directory
        fsync fails, the progress-mirror write fails) x each schedule production can run (the Mod retries; a
        restart's one recovery pass, then claims; the task file gone, only that recovery pass; and that pass with
        the claim due for compaction), driven to quiescence: no body or claim is removed before the id's ledger
        sync succeeded (the crash-after-remove window), every reply,
        the first after the fault included, that reports final comes over a complete settlement, and at the end
        the result exists exactly once (none for the no-result settle), the ledger holds the id exactly once and
        still opens, and the real staged body is gone."""
        self.maxDiff = None
        lc = inbox._lifecycle(self.root)
        dead = {"pid": 2 ** 22 - 1, "start": "2026-01-01T00:00:00Z"}
        ledger_path = Path(lc.settled.path)
        real_add, real_unstage, real_write_progress = (inbox.lifecycle.SettledIds.add, inbox.lifecycle.ClaimStore.unstage,
                                                       records.write_progress)
        real_lifecycle = inbox._lifecycle
        real_save, real_remove = inbox.lifecycle.ClaimStore.save, inbox.lifecycle.ClaimStore.remove

        def recovery():
            with mock.patch.object(task_io, "verify_lineage", return_value=False):
                try:
                    inbox.recover_dead_generation(self.root)
                except Exception as e:                          # a hold start that fails: the next start retries
                    return str(e)
            return ""

        def rolled_back(t, generation=None):
            self.confirmed(t)
            self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"answer\n")[0], 0)
            lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED, **({"generation": generation} if generation else {})))
            lc.claims.stage(t, b"legacy\n")

        def e_publish(t):
            self.confirmed(t)
            self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"answer\n")[0], 0)
            lc.claims.stage(t, b"legacy\n")
            go = lambda: self.verb("publish", t)       # noqa: E731
            return go, go, "answer\n"

        def e_park(t):
            self.confirmed(t)
            lc.claims.stage(t, b"legacy\n")
            go = lambda: self.verb("park", t, "turn-1", "--detail", "aborted")     # noqa: E731
            return go, go, inbox.PROTOCOL_FAILURE % "aborted"

        def e_refused(t):
            self.task(t, "guest")
            go = lambda: self.verb("claim", t)         # noqa: E731
            return go, go, task_io.NON_OWNER_REFUSED

        def e_interrupt(t):
            self.task(t)
            go = lambda: self.verb("interrupt", t, "--detail", "stopped")     # noqa: E731
            return go, go, inbox.INTERRUPTED_BODY

        def e_lost(t):
            rolled_back(t)
            go = lambda: self.verb("claim", t)         # noqa: E731
            return go, go, "answer\n"

        def e_no_result(t):
            self.task(t)
            self.assertEqual(self.verb("claim", t)[0], 0)
            lc.claims.save(dict(lc.claims.load(t), generation=dead))
            lc.claims.stage(t, b"legacy\n")
            task_file = self.inbox / (t + ".txt")
            text = task_file.read_text()
            task_file.unlink()

            def first():
                out = recovery()
                task_file.write_text(text)                 # the task file comes back; its claims must see the debt
                return None, {"recovery": out}
            return first, lambda: self.verb("claim", t), None

        def e_recovered(t):
            rolled_back(t, dead)
            return (lambda: (None, {"recovery": recovery()})), (lambda: self.verb("claim", t)), "answer\n"

        entries = {"publish": e_publish, "park": e_park, "refused": e_refused, "interrupt": e_interrupt,
                   "lost": e_lost, "noresult": e_no_result, "recovered": e_recovered}

        def fault(kind, fired, t, undurable):
            """One fault, once, on this case's task only."""
            once = []

            def add(led, task_id):
                if not once and task_id == t:
                    once.append(1); fired.append(kind)
                    if kind == "torn":
                        with open(led.path, "ab") as f:
                            f.write((task_id + "\n").encode()[:3])
                    if kind == "fsync":
                        # The whole record lands and is visible; its fsync fails: durable only after a later sync.
                        if (task_id + "\n") not in ledger_path.read_text():
                            with open(led.path, "ab") as f:
                                f.write((task_id + "\n").encode())
                        undurable.add(task_id)
                        raise inbox.lifecycle.LedgerIOError("cannot fsync the ledger ([Errno 5] I/O error)")
                    raise inbox.lifecycle.LedgerIOError("cannot append ([Errno 5] I/O error)")
                return real_add(led, task_id)

            def unstage(store, task_id):
                if not once and task_id == t and (store.dir(task_id) / "body").exists():
                    once.append(1); fired.append(kind)
                    raise inbox.lifecycle.StateIOError("cannot remove body ([Errno 5] I/O error)")
                return real_unstage(store, task_id)

            def save(store, rec):
                # The claim.json rename lands, then its directory fsync fails: the step is committed, the reply hard.
                if not once and rec["task_id"] == t and rec["phase"] == inbox.lifecycle.SETTLED:
                    once.append(1); fired.append(kind)
                    real_save(store, rec)
                    raise inbox.lifecycle.StateCommitted("claim.json was replaced, then its directory fsync failed")
                return real_save(store, rec)

            def write_progress(root, mutate):
                if not once:
                    once.append(1); fired.append(kind)
                    raise records.RecordError("progress.json: [Errno 5] I/O error")
                return real_write_progress(root, mutate)
            if kind in ("append", "torn", "fsync"):
                return mock.patch.object(inbox.lifecycle.SettledIds, "add", add)
            if kind == "unlink":
                return mock.patch.object(inbox.lifecycle.ClaimStore, "unstage", unstage)
            if kind == "rename":
                return mock.patch.object(inbox.lifecycle.ClaimStore, "save", save)
            return mock.patch.object(records, "write_progress", write_progress)

        @contextlib.contextmanager
        def durability_watch(undurable, problems):
            """A visible id becomes durable only by a successful fsync of the ledger. Removing the body or the claim
            of an id not yet durable is the crash window the invariant forbids: a crash there loses the only witness."""
            real_fsync = inbox.lifecycle.os.fsync

            def fsync(fd):
                real_fsync(fd)
                if undurable and fd_path(fd) == os.path.realpath(ledger_path):
                    undurable.clear()

            def unstage(store, task_id):
                if task_id in undurable and (store.dir(task_id) / "body").exists():
                    problems.append("the body removed before a successful ledger sync")
                return real_unstage(store, task_id)

            def remove(store, task_id):
                if task_id in undurable:
                    problems.append("the claim compacted before a successful ledger sync")
                return real_remove(store, task_id)
            with mock.patch.object(inbox.lifecycle.os, "fsync", fsync), \
                    mock.patch.object(inbox.lifecycle.ClaimStore, "unstage", unstage), \
                    mock.patch.object(inbox.lifecycle.ClaimStore, "remove", remove):
                yield

        def final(rc, out):
            return rc == 0 or (rc == 3 and (out.get("resolved") is True or out.get("outcome") == "settled")) \
                or (rc == 1 and not out.get("retry"))

        def check(t, reply, where, problems, undurable):
            """final => complete (and durable), on every reply; True when the reply is final."""
            rc, out = reply
            if final(rc, out) and (not complete(t) or t in undurable):
                problems.append("%s, final %r, over an incomplete settlement" % (where, out))
            return final(rc, out)

        def complete(t):
            try:
                ids = inbox.lifecycle.SettledIds(ledger_path)         # still opens: no torn or merged record
            except inbox.lifecycle.LedgerError:
                return False
            return (t in ids.ids and ledger_path.read_text().count(t + "\n") == 1
                    and not (lc.claims.dir(t) / "body").exists())

        def aged(t):
            rec = lc.claims.load(t)
            if rec and rec["phase"] == inbox.lifecycle.SETTLED:
                lc.claims.save(dict(rec, settled_at=rec["settled_at"] - inbox.lifecycle.COMPACT_AFTER - 1))
        failures, fired_cases, cases = [], 0, 0
        for e_name, entry in entries.items():
            for f_name in ("append", "torn", "fsync", "unlink", "rename", "mirror"):
                for s_name in ("retry", "restart", "gone", "late"):
                    t = "task-p-%s-%s-%s" % (e_name, f_name, s_name)
                    cases += 1
                    first, again, expected = entry(t)
                    fired, problems, undurable = [], [], set()
                    preloaded = real_lifecycle(self.root)            # a writer loaded before the fault, run next
                    with durability_watch(undurable, problems):
                        with fault(f_name, fired, t, undurable):
                            reply = first()
                        fired_cases += bool(fired)
                        done = isinstance(reply[0], int) and check(t, reply, "the first reply after the fault", problems,
                                                                   undurable)
                        stale = [preloaded] if f_name == "torn" and fired else []
                        with mock.patch.object(inbox, "_lifecycle", lambda root: stale.pop() if stale else real_lifecycle(root)):
                            if s_name in ("gone", "late"):
                                (self.inbox / (t + ".txt")).unlink(missing_ok=True)
                                if s_name == "late":
                                    aged(t)                      # due for compaction: the crash-after-remove window
                                recovery()
                            else:
                                if s_name == "restart":
                                    recovery()
                                    again = (lambda t=t: self.verb("claim", t)) if e_name not in ("noresult", "recovered") else again
                                for _ in range(0 if done and s_name == "retry" else 8):
                                    if check(t, again(), "a retry", problems, undurable):
                                        break
                                else:
                                    if not (done and s_name == "retry"):
                                        problems.append("no final report")
                                if not check(t, self.verb("claim", t), "a later claim", problems, undurable):
                                    problems.append("a later claim is not final")
                                recovery()
                    if not complete_safe(ledger_path):
                        problems.append("the ledger does not open")
                    elif not complete(t) or t in undurable:
                        problems.append("settlement incomplete at quiescence")
                    got = [p.name for p in self.results.iterdir() if p.name == t + ".txt"]
                    body = (self.results / (t + ".txt")).read_text() if got else None
                    if body != expected:
                        problems.append("result %r, expected %r" % (body, expected))
                    if problems:
                        failures.append("%s x %s x %s: %s" % (e_name, f_name, s_name, "; ".join(dict.fromkeys(problems))))
                    if not complete_safe(ledger_path):
                        ledger_path.write_text("".join(sorted(x + "\n" for x in real_ids(ledger_path))))
        self.assertEqual(failures, [])
        self.assertEqual(cases, 168)
        self.assertEqual(fired_cases, 156, "every fault fired except the mirror write where the first call writes no mirror "
                         "(the non-admitted claim's refusal, and the two recovery-first entries): 168 - 12")

    def test_every_settle_path_completes_its_settlement_after_one_ledger_fault_before_reporting_final(self):
        """The settled-ids append faults once on each path that settles: publish, park, a non-admitted claim, an
        interrupt, and a call-less claim nobody claims again (recovery). The first call is a retry, never final;
        the next completes the settlement (ledger entry, the real staged body gone) before it says settled or final."""
        lc = inbox._lifecycle(self.root)
        real_add = inbox.lifecycle.SettledIds.add
        ledger = lambda: inbox._lifecycle(self.root).settled.ids     # noqa: E731
        body = lambda t: lc.claims.dir(t) / "body"                     # noqa: E731

        def faulted_once(call):
            hits = []

            def add_faults_once(led, task_id):
                if not hits:
                    hits.append(task_id)
                    raise inbox.lifecycle.LedgerIOError("cannot append to settled-ids ([Errno 5] I/O error)")
                return real_add(led, task_id)
            with mock.patch.object(inbox.lifecycle.SettledIds, "add", add_faults_once):
                out = call()
            self.assertEqual(len(hits), 1)
            return out

        def debt(t):
            self.assertEqual((lc.claims.load(t)["phase"], t in ledger(), body(t).is_file()), (inbox.lifecycle.SETTLED, False, True))

        def whole(t):
            self.assertEqual((t in ledger(), body(t).exists()), (True, False), t)
            self.assertEqual(open(inbox._lifecycle(self.root).settled.path).read().count(t + "\n"), 1)
        t = "task-sweep-publish"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"answer\n")[0], 0)
        lc.claims.stage(t, b"legacy\n")
        rc, out = faulted_once(lambda: self.verb("publish", t))
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        debt(t)
        rc, out = self.verb("publish", t)
        self.assertEqual((rc, out.get("outcome")), (0, "settled"), out)
        whole(t)
        t = "task-sweep-park"
        self.confirmed(t)
        lc.claims.stage(t, b"legacy\n")
        rc, out = faulted_once(lambda: self.verb("park", t, "turn-1", "--detail", "aborted"))
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        debt(t)
        rc, out = self.verb("park", t, "turn-1", "--detail", "aborted")
        self.assertEqual((rc, out.get("outcome")), (0, "settled"), out)
        whole(t)
        t = self.task("task-sweep-guest", "guest")
        rc, out = faulted_once(lambda: self.verb("claim", t))
        self.assertEqual((rc, out.get("retry")), (1, True), out)
        debt(t)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "retry" in out, out["refused"]), (1, False, "%s is LIVE_READY; only ABSENT may be submitted" % t), out)
        whole(t)
        t = self.task("task-sweep-interrupt")
        rc, out = faulted_once(lambda: self.verb("interrupt", t, "--detail", "stopped"))
        self.assertEqual((rc, out.get("retry"), out.get("resolved")), (1, True, None), out)
        debt(t)
        rc, out = self.verb("interrupt", t, "--detail", "stopped")
        self.assertEqual((rc, out["resolved"]), (0, True), out)
        whole(t)
        t = self.task("task-sweep-unbound", "guest")
        faulted_once(lambda: self.verb("claim", t))
        debt(t)
        self.assertEqual(inbox.recover_dead_generation(self.root), 0, "no generation: the call-less claim is still reconciled")
        whole(t)
        rec = lc.claims.load(t)
        lc.claims.save(dict(rec, settled_at=rec["settled_at"] - inbox.lifecycle.COMPACT_AFTER - 1))
        inbox.recover_dead_generation(self.root)
        self.assertIsNone(lc.claims.load(t), "a call-less claim compacts once due")

    def test_a_claim_the_mod_lost_past_its_call_is_finished_or_interrupted_at_once(self):
        """A Mod that lost its offers (a reload inside the generation) claims the task afresh: a descriptor
        with no verifying blob is answered as interrupted now, a verifying blob whose phase move was lost is
        published, a staged answer is published; a CALLING claim and one confirmed with nothing accepted still
        wait for the next generation, and a dead generation's claim is left to hold's recovery."""
        lc = inbox._lifecycle(self.root)
        t = "task-lost-blob"
        self.confirmed(t)
        with mock.patch.object(inbox.lifecycle.os, "link", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"accepted text\n")[1].get("retry"), True)
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.TURN_CONFIRMED)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "retry" in out), (1, False), out)
        self.assertIn("answered as interrupted now; only ABSENT may be submitted", out["refused"])
        self.assertEqual((self.results / (t + ".txt")).read_text(), inbox.INTERRUPTED_BODY, "the interrupted line, not silence")
        rec = lc.claims.load(t)
        self.assertEqual((rec["phase"], rec["outcome"]["detail"]), (inbox.lifecycle.SETTLED, "turn confirmed, the accepted answer's staging was lost"))
        self.assertNotIn(t, records.load_plugin(self.root).get("inflight") or {}, "the mirror is released")
        self.assertEqual(self.verb("claim", t)[1]["refused"], "%s is LIVE_READY; only ABSENT may be submitted" % t)
        t = "task-lost-move"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the accepted answer\n")[0], 0)
        lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED))     # the move's last save never landed
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "staged and never published, is published now" in out["refused"]), (1, True), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), "the accepted answer\n", "the accepted answer, exactly")
        t = "task-lost-corrupt"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the accepted answer\n")[0], 0)
        rec = lc.claims.load(t)
        lc.claims.save(dict(rec, phase=inbox.lifecycle.TURN_CONFIRMED))
        (lc.claims.dir(t) / rec["staged"]["blob"]).write_bytes(b"not those bytes\n")     # a committed blob that does not verify
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "answered as interrupted now" in out["refused"]), (1, True), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), inbox.INTERRUPTED_BODY, "never the bytes that do not verify")
        self.assertEqual(lc.claims.load(t)["outcome"]["detail"], "turn confirmed, the accepted answer's staging was lost "
                         "(the committed result does not match its descriptor)", "the one decision, recovery's, names it")
        t = "task-lost-eio"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"the verified answer\n")[0], 0)
        lc.claims.save(dict(lc.claims.load(t), phase=inbox.lifecycle.TURN_CONFIRMED))
        real_write, hits = inbox.lifecycle.write_durable, []

        def claim_save_eio_once(path, data):
            # One EIO in the claim save that moves the verified blob on; the blob itself is never written again.
            if not hits and Path(path).name == "claim.json":
                hits.append(path)
                raise OSError(errno.EIO, "I/O error")
            return real_write(path, data)
        with mock.patch.object(inbox.lifecycle, "write_durable", claim_save_eio_once), \
                mock.patch.object(inbox.lifecycle.Lifecycle, "_commit_blob", side_effect=OSError(errno.EIO, "x")) as blob:
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry"), len(hits), blob.call_count), (1, True, 1, 0), out)
        rec = lc.claims.load(t)
        self.assertEqual((rec["phase"], bool(rec.get("staged"))), (inbox.lifecycle.TURN_CONFIRMED, True),
                         "one I/O failure while finishing a verifying blob is a retry, never an interrupt")
        self.assertFalse((self.results / (t + ".txt")).exists())
        with mock.patch.object(inbox.lifecycle.Lifecycle, "_commit_blob", side_effect=OSError(errno.EIO, "x")) as blob:
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, "staged and never published, is published now" in out["refused"], blob.call_count), (1, True, 0), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), "the verified answer\n", "the accepted answer, nothing interrupted")
        t = "task-lost-delegated"
        self.confirmed(t)
        with mock.patch.object(inbox.lifecycle.os, "link", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.verb("stage", t, "turn-1", stdin=b"accepted text\n")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "close_turn", return_value="parked") as closed:
            rc, out = self.verb("claim", t)
        self.assertEqual((closed.call_count, rc, out.get("retry")), (1, 1, True), "the decision is close_turn's alone")
        self.assertIn("could not be closed now", out["refused"])
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.TURN_CONFIRMED, "parked: nothing moved")
        t = "task-lost-staged"
        self.confirmed(t)
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"staged answer\n")[0], 0)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "its staged answer is published now" in out["refused"]), (1, True), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), "staged answer\n")
        for t, phase in (("task-lost-calling", inbox.lifecycle.CALLING), ("task-lost-confirmed", inbox.lifecycle.TURN_CONFIRMED)):
            self.task(t)
            attempt = self.verb("claim", t)[1]["attempt"]
            token = self.verb("authorize", t, str(attempt), "--text-hash", "h1")[1]["token"]
            if phase == inbox.lifecycle.TURN_CONFIRMED:
                self.verb("confirm", t, "turn-1", token, "--text-hash", "h1")
            rc, out = self.verb("claim", t)
            self.assertEqual((rc, "retry" in out, out["refused"]), (1, False, "%s: attempt %s is %s" % (t, attempt, phase)), out)
            self.assertFalse((self.results / (t + ".txt")).exists(), "%s waits for the next generation" % phase)
        t = "task-dead-gen"
        self.confirmed(t)
        lc.claims.save(dict(lc.claims.load(t), staged={"blob": "blob.1.none", "length": 1, "sha256": "0" * 64, "identity": {}},
                            generation={"pid": 2 ** 22 - 1, "start": "2026-01-01T00:00:00Z"}))
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, out["refused"]), (1, "%s: attempt 1 is TURN_CONFIRMED" % t), "another generation's: not ours to answer")
        self.assertFalse((self.results / (t + ".txt")).exists())
        t = "task-lost-parked"
        self.confirmed(t)
        with mock.patch.object(inbox.lifecycle.os, "link", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.verb("stage", t, "turn-1", stdin=b"accepted text\n")
        with mock.patch.object(inbox.lifecycle.Lifecycle, "_publish", return_value=None):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out.get("retry")), (1, True), "interrupted, its publication parked: the next claim retries")
        self.assertEqual(lc.claims.load(t)["phase"], inbox.lifecycle.RESULT_STAGED)
        rc, out = self.verb("claim", t)
        self.assertEqual((rc, "its staged answer is published now" in out["refused"]), (1, True), out)
        self.assertEqual((self.results / (t + ".txt")).read_text(), inbox.INTERRUPTED_BODY)

    def test_a_progress_write_failure_is_the_hard_exit_3_with_the_lifecycle_step_applied(self):
        """The step stands, the answer carries it with `hard`, and the Mod closes intake on 3."""
        from ag2_sparrow import task_lifecycle as tl
        t = self.task()
        with mock.patch.object(records, "write_durable", side_effect=records.RecordError("disk full")):
            rc, out = self.verb("claim", t)
        self.assertEqual((rc, out["attempt"], out["hard"]), (3, 1, True))
        self.assertIn("progress write failed", out["error"])
        self.assertEqual(tl.Lifecycle(self.root).claims.load(t)["phase"], tl.PREPARED, "the claim stands")
        self.assertEqual(records.load_plugin(self.root)["inflight"], {}, "no torn record: the old one stands")
        with mock.patch.object(records, "write_durable", side_effect=records.RecordError("disk full")):
            rc, out = self.verb("progress", "--intake", "open")
        self.assertEqual((rc, out["hard"]), (3, True))
        rc, out = self.verb("progress", "--session-id", SESSION_ID, "--intake", "open", "--bind-generation")
        self.assertEqual(rc, 0)
        plugin = records.load_plugin(self.root)
        self.assertEqual((plugin["session_id"], plugin["intake"], plugin["generation"]["pid"]), (SESSION_ID, "open", self.consumer))
        for target, step in ((records.tempfile, "mkstemp"), (records.os, "fsync"), (records.os, "replace"), (records, "fsync_dir")):
            with mock.patch.object(target, step, side_effect=OSError("io")):
                rc, out = self.verb("progress", "--intake", "stopped")
            self.assertEqual((rc, out["hard"]), (3, True), step)
            if step != "fsync_dir":
                self.assertEqual(records.load_plugin(self.root)["intake"], "open", "a failed write leaves the previous record")
            rc, out = self.verb("progress", "--intake", "open")
            self.assertEqual((rc, records.load_plugin(self.root)["intake"]), (0, "open"), "a later durable write reopens")
        self.assertEqual([n for n in os.listdir(self.work / "state") if n.endswith(".tmp")], [], "no temp survives")

    def test_the_existing_final_path_fsyncs_the_blob_dir_before_the_phase_and_stays_turn_confirmed_while_it_fails(self):
        """Blob committed, phase not saved; a retry finds the final, fsyncs the directory first; a
        failing fsync keeps TURN_CONFIRMED with zero publication; sweep unlinks are followed by a fsync."""
        from ag2_sparrow import task_lifecycle as tl
        t = self.task()
        self.verb("claim", t)
        token = self.verb("authorize", t, "1", "--text-hash", "h")[1]["token"]
        self.verb("confirm", t, "turn-1", token, "--text-hash", "h")
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"body\n")[0], 0)
        lc = tl.Lifecycle(self.root)
        rec = lc.claims.load(t)
        rec["phase"] = tl.TURN_CONFIRMED                       # the crash between the blob commit and the phase save
        lc.claims.save(rec)
        claim_dir = lc.claims.dir(t)
        calls = []
        real = tl.fsync_dir

        def failing(path):
            calls.append(Path(path))
            if Path(path) == claim_dir:
                raise OSError("fsync failed")
            return real(path)
        with mock.patch.object(tl, "fsync_dir", failing):
            for _ in range(3):
                rc, out = self.verb("stage", t, "turn-1", stdin=b"body\n")
                self.assertEqual((rc, out.get("retry")), (1, True), "the accepted body, unfinished: a retry")
            self.assertEqual(self.verb("publish", t)[0], 1, "nothing to publish while the fsync fails")
        self.assertIn(claim_dir, calls, "the existing final's directory is fsynced before the phase")
        self.assertEqual(lc.claims.load(t)["phase"], tl.TURN_CONFIRMED, "stays TURN_CONFIRMED while the fsync fails")
        self.assertFalse((self.results / "task-a.txt").exists(), "zero publication")
        self.assertEqual(self.verb("stage", t, "turn-1", stdin=b"body\n")[0], 0, "a later fsync that works finishes")
        self.assertEqual(self.verb("publish", t)[0], 0)
        # The sweep: a dead writer's private temp is unlinked, then the directory fsynced.
        t2 = self.task("task-s")
        dead = subprocess.Popen(["sleep", "0"]); dead.wait()
        lc2 = tl.Lifecycle(self.root, lineage=lambda *a: True)
        lc2.prepare(t2, "x", mode=tl.LOCAL, generation={"pid": dead.pid, "start": "gone"})
        temp = lc2.claims.dir(t2) / (".%s.1.%s.%d.%s.tmp" % (t2, "a" * 24, dead.pid, "b" * 8))
        temp.write_bytes(b"partial")
        order = []
        real_unlink = os.unlink

        def unlink(path, *a, **k):
            order.append(("unlink", Path(path).name)); return real_unlink(path, *a, **k)

        def fsync_spy(path):
            order.append(("fsync", Path(path).name)); return real(path)
        with mock.patch.object(tl.os, "unlink", unlink), mock.patch.object(tl, "fsync_dir", fsync_spy):
            self.assertEqual(inbox.recover_dead_generation(self.root), 1)
        self.assertFalse(temp.exists())
        i = order.index(("unlink", temp.name))
        self.assertEqual(order[i + 1], ("fsync", t2), "the unlink is followed by the directory fsync")

    def test_check_inode_and_the_root_resolution(self):
        st = os.stat(self.inbox)
        rc, out = self.verb("check-inode", "--dir", str(self.inbox), "--expect", "%d:%d" % (st.st_dev, st.st_ino))
        self.assertEqual((rc, out["inode"]), (0, "%d:%d" % (st.st_dev, st.st_ino)))
        rc, out = self.verb("check-inode", "--dir", str(self.inbox), "--expect", "1:1")
        self.assertEqual((rc, "not the held inbox 1:1" in out["refused"]), (1, True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {inbox.ROOT_VAR: "relative"}):
            rc = cli.main(["inbox", "check-inode", "--expect", "1:1"])
        self.assertEqual((rc, "must be absolute" in json.loads(out.getvalue())["refused"]), (1, True))


class Detector(unittest.TestCase):
    """Raw-static-plus-outstanding is this adapter's rule; abnormal text through the shared grammar."""

    def test_static_with_an_offer_in_flight_stalls_after_the_bound_and_progress_clears_it(self):
        plugin = {"inflight": {"task-a": {"phase": "TURN_CONFIRMED", "turn_id": "t"}}}
        obs = detector.derive({}, "working...\n", plugin, 1000.0, stall_min_s=10)
        self.assertIsNone(obs["stall"])
        obs = detector.derive(obs, "working...\n", plugin, 1009.0, stall_min_s=10)
        self.assertIsNone(obs["stall"], "not yet")
        obs = detector.derive(obs, "working...\n", plugin, 1010.0, stall_min_s=10)
        self.assertEqual((obs["stall"]["task"], obs["stall"]["checks"]), ("task-a", "pane static while an offer is in flight"))
        self.assertIn("static for 10 s with task-a in flight", obs["stall"]["reason"])
        token = records.stall_token(obs["stall"])
        self.assertEqual(records.stall_token(detector.derive(obs, "working...\n", plugin, 1011.0, stall_min_s=10)["stall"]), token,
                         "the token is immutable while the evidence stands")
        moved = detector.derive(obs, "working... done\n", plugin, 1012.0, stall_min_s=10)
        self.assertIsNone(moved["stall"], "a changed frame is progress")
        idle = detector.derive(obs, "working...\n", {"inflight": {}}, 1020.0, stall_min_s=10)
        self.assertIsNone(idle["stall"], "static with nothing in flight is idle, not a hang")

    def test_abnormal_text_is_read_line_anchored_and_needs_its_own_bound(self):
        plugin = {"inflight": {}}
        obs = detector.derive({}, "> Please run /login to continue\n", plugin, 0.0, abnormal_min_s=5)
        self.assertEqual(obs["abnormal"], ["needs-login"])
        self.assertIsNone(obs["stall"])
        obs = detector.derive(obs, "> Please run /login to continue\n", plugin, 5.0, abnormal_min_s=5)
        self.assertEqual(obs["stall"]["reason"], "abnormal:needs-login")
        prose = detector.derive({}, "we talked about compaction in the meeting\n", plugin, 0.0)
        self.assertEqual(prose["abnormal"], [], "prose mid-line never counts")
        self.assertEqual(detector.abnormal_names("Compacting conversation...\n"), ["compacting"])


class LauncherTmux(ConnectBase):
    """The generation, the fenced stop and the clean environment on a real scratch tmux server: the adapter's own socket under a short /tmp HOME,
    created and killed here. The pane runs the real `hold` with the fake claude."""
    home_dir = "/tmp"

    def setUp(self):
        super().setUp()
        script = self.bin / cli.COMMAND
        script.write_text(CONSOLE_SH % {"py": sys.executable, "pkg": PKG, "cov": covered_code("")})
        script.chmod(0o755)
        os.environ[CANARY[0]] = CANARY[1]
        os.environ["PATH"] = "%s:%s:/usr/bin:/bin" % (self.bin, TMUX_DIR)
        self.configure()
        (self.home / "claude-linger").write_text("300")
        self.cfg = cli.load_config()[0]
        self.sock = str(launcher.socket_path(cli.config_dir()))
        self.addCleanup(self.kill_server)
        for name, value in (("CLAUDE_GRACE_S", 1.0), ("SERVER_EXIT_S", 10.0), ("CONSUMER_WAIT_S", 30.0)):
            p = mock.patch.object(launcher, name, value)
            p.start()
            self.addCleanup(p.stop)

    def kill_server(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"], capture_output=True)
        wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 15)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], capture_output=True, text=True)

    def launcher(self, **kw):
        return launcher.Launcher(self.cfg, cli.config_dir(), sleep=time.sleep, **kw)

    def created(self):
        lc = self.launcher()
        rec = lc.record()
        self.assertEqual(launcher.classify(lc.tm, rec, self.cfg, str(self.work))[0], "create")
        ok, problem = lc.create(rec)
        self.assertTrue(ok, problem)
        return lc, lc.record()

    def witness(self, rec, session_id=SESSION_ID, generation=None):
        records.write_progress(str(self.work), lambda r: dict(r, session_id=session_id,
                                                            generation=generation or rec["consumer"]))

    def test_create_runs_hold_under_a_clean_server_and_records_the_generation(self):
        lc, rec = self.created()
        gen = rec["generation"]
        self.assertRegex(gen["start_command"], launcher.START_COMMAND_RE)
        self.assertIn("--generation=%s --" % gen["nonce"], gen["start_command"])
        self.assertNotIn(str(self.work), gen["start_command"], "the root reaches hold through the environment only")
        self.assertRegex(rec["title_token"], r"^t[0-9a-f]{32}$")
        session = wait_for(self.session, 10)
        self.assertEqual(rec["consumer"]["pid"], session["pid"], "the consumer is the pane's claude")
        self.assertTrue(trust.trusted(trust.config_file(self.cfg["CLAUDE_CONFIG_DIR"]), trust.project_key(str(self.inbox))),
                        "the inbox is trusted before the session starts")
        self.assertEqual(task_io.lock_holder(str(self.work))["pid"], session["pid"])
        env = session["env"]
        for leaked in (CANARY[0], "SUTANDO_WORKSPACE_DIR", "REMOTE_TASK_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertNotIn(leaked, env, "env -i keeps the host's environment out of the pane")
        self.assertEqual((env[hold_mod.ENV_ROOT], env[hold_mod.ENV_SESSION_ID], env["AG2_AUTOARM"], env[hold_mod.TITLE_OFF_VAR]),
                         (str(self.work), SESSION_ID, "1", "1"))
        glob = self.tmux("show-environment", "-g").stdout
        self.assertNotIn(CANARY[1], glob, "the server's global environment carries no canary")
        self.assertNotIn("SUTANDO_WORKSPACE_DIR", glob)
        self.assertEqual(self.tmux("show-options", "-gv", "update-environment").stdout.strip(), "",
                         "update-environment cleared: an attach brings no client environment")
        self.assertEqual(self.tmux("display-message", "-p", "-t", gen["pane_id"], "#{pane_title}").stdout.strip(),
                         rec["title_token"])

    def test_create_reads_the_pane_back_when_the_launchers_environment_has_no_locale(self):
        """The managed job sets no locale; a client without one escapes the TAB pane_fields splits on."""
        with mock.patch.dict(os.environ):
            for name in [k for k in os.environ if k == "LANG" or k.startswith("LC_")]:
                del os.environ[name]
            lc, rec = self.created()
            fields = lc.tm.pane_fields(rec["generation"]["pane_id"])
        self.assertEqual(fields["pane_pid"], rec["generation"]["pane_pid"], "the pane is read back by its tuple")
        self.assertEqual(wait_for(self.session, 10)["env"].get("LC_CTYPE"), "en_US.UTF-8",
                         "the clean server gives the pane a UTF-8 locale")

    def test_the_pane_reads_back_without_a_locale_even_from_a_client_without_u(self):
        """The separator alone suffices: no LANG/LC_*, and a client that does not pass -u."""
        real = launcher.Tmux.run

        def without_u(tm, *args, **kw):
            seen.append(args)
            return tm._run([tm.tmux, "-S", tm.sock, *args], capture_output=True, text=True,
                           timeout=kw.get("timeout", launcher.PROBE_TIMEOUT_S), env=kw.get("env"), stdin=subprocess.DEVNULL)
        seen = []
        with mock.patch.dict(os.environ):
            for name in [k for k in os.environ if k == "LANG" or k.startswith("LC_")]:
                del os.environ[name]
            with mock.patch.object(launcher.Tmux, "run", without_u):
                lc, rec = self.created()
                fields = lc.tm.pane_fields(rec["generation"]["pane_id"])
        self.assertTrue(seen, "the -u-less client ran")
        self.assertEqual((fields["pane_pid"], fields["title"]), (rec["generation"]["pane_pid"], rec["title_token"]))
        self.assertIs(launcher.Tmux.run, real)
        self.tmux("select-pane", "-t", rec["generation"]["pane_id"], "-T", "a %s title" % launcher.FIELD_SEP)
        got = lc.tm.pane_fields(rec["generation"]["pane_id"])
        self.assertEqual(got["title"], "a %s title" % launcher.FIELD_SEP)
        self.assertEqual(got["start_command"], rec["generation"]["start_command"], "a free-text title never shifts a field")

    def test_adoption_needs_the_pane_tuple_the_consumer_and_this_generations_witness(self):
        lc, rec = self.created()
        root = str(self.work)
        self.assertEqual(launcher.classify(lc.tm, rec, self.cfg, root, created=rec["generation"]["nonce"])[0], "adopt",
                         "the creating launcher waives the witness until the Mod writes one")
        fresh = self.launcher()
        verdict, why = launcher.classify(fresh.tm, rec, self.cfg, root)
        self.assertEqual((verdict, "plugin witness" in why), ("unknown", True), "no witness, no adoption")
        self.witness(rec, generation={"pid": rec["consumer"]["pid"] + 1, "start": rec["consumer"]["start"]})
        self.assertEqual(launcher.classify(fresh.tm, rec, self.cfg, root)[0], "unknown", "another generation's witness")
        self.witness(rec, session_id="11111111-2222-3333-4444-555555555555")
        verdict, why = launcher.classify(fresh.tm, rec, self.cfg, root)
        self.assertEqual((verdict, "session id 11111111" in why), ("unknown", True), "a changed id is never adopted")
        self.witness(rec)
        self.assertEqual(launcher.classify(fresh.tm, rec, self.cfg, root), ("adopt", ""))
        with mock.patch.object(records, "load_plugin", side_effect=records.RecordError("inflight is int")):
            self.assertEqual(launcher.classify(fresh.tm, rec, self.cfg, root),
                             ("unknown", "the plugin record is unreadable (inflight is int)"), "never adopted on a bad record")
        self.assertEqual(fresh.tick(fresh.record())[0], None, "adopt keeps serving")
        self.assertEqual(fresh.record()["launcher"]["pid"], os.getpid(), "the adopter names itself in the record")
        self.assertEqual(len(self.tmux("list-sessions").stdout.splitlines()), 1, "no second session")
        self.assertEqual(launcher.classify(fresh.tm, dict(rec, title_token="t" + "0" * 32), self.cfg, root)[0], "unknown",
                         "a pane whose title is not the recorded token is not adopted")

    def test_stop_sends_exit_then_kills_the_session_through_the_fence_and_frees_the_root(self):
        lc, rec = self.created()
        seen = []
        real = lc.fenced

        def fenced(gen, title, action):
            rc = real(gen, title, action)
            if action.startswith("send-keys"):
                seen.append((rc, self.tmux("capture-pane", "-p", "-t", gen["pane_id"]).stdout))
            return rc
        with mock.patch.object(lc, "fenced", fenced):
            self.assertEqual(lc.stop_session(rec, "test"), "stopped")
        self.assertEqual(seen[0][0], 0)
        self.assertIn("/exit", seen[0][1], "/exit reached the pane")
        self.assertIs(lc.tm.has_session(), False)
        self.assertTrue(wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 15), "hold released the root")
        self.assertEqual(launcher.classify(lc.tm, lc.record(), self.cfg, str(self.work))[0], "create",
                         "the next start creates and resumes by id")

    def test_a_claude_that_exits_on_exit_is_stopped_for_the_manager_uninstall_and_the_card(self):
        """The real stop path: /exit ends claude, hold and the server, so the fenced kill-session finds no
        server; the session is gone, which is stopped, not cancelled."""
        (self.home / "claude-exit-on-exit").write_text("1")
        kills = []
        lc, rec = self.created()
        real = lc.fenced

        def fenced(gen, title, action):
            rc = real(gen, title, action)
            if action.startswith("kill-session"):
                kills.append(rc)
            return rc
        with mock.patch.object(lc, "fenced", fenced):
            self.assertEqual(lc.stop_session(rec, "test"), "stopped")
        self.assertEqual(len(kills), 1)
        self.assertNotEqual(kills[0], 0, "the kill-session found no server to send to")
        self.assertIs(lc.tm.has_session(), False)
        self.assertTrue(wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 15))

        lc, rec = self.created()
        lc.state["sig"] = signal.SIGTERM
        self.assertEqual(lc._serve(), 0, "a manager stop of an exiting claude is 0, never 74")
        self.assertTrue(wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 15))

        lc, rec = self.created()
        self.assertEqual(cli.stop_stray(self.cfg), "", "uninstall stops it")
        self.assertTrue(wait_for(lambda: task_io.occupancy(str(self.work)) == "free", 15))

        lc, rec = self.created()
        store_root = card.store_root(_dirs.state_dir())
        shutil.rmtree(store_root.parent, ignore_errors=True)
        self.addCleanup(shutil.rmtree, store_root.parent, True)
        lc.store_root = store_root
        card.register_install(store_root, lc.opaque, lc.root, lc.supervisor_lock)
        lc._with_record(lambda r: dict(r, stall=STALL))
        lc.raise_card(lc.record(), card.KIND_STALL, STALL["checks"])
        self.assertEqual(accepter.local_stop(_dirs.state_dir(), lc.opaque, log=lambda *a: None), "applied")
        after = lc.apply_effects(lc.record())
        self.assertEqual((after["effect"]["outcome"], after["generation"]), ("stopped", None),
                         "the card's stop retires the generation")

    def test_a_retitled_pane_is_a_distinct_title_changed_outcome_in_the_stop_verify_and_the_card(self):
        lc, rec = self.created()
        self.tmux("select-pane", "-t", rec["generation"]["pane_id"], "-T", "retitled-by-claude")
        out = lc.stop_session(rec, "test")
        self.assertTrue(out.startswith(launcher.TITLE_CHANGED + ":"), out)
        self.assertEqual(lc.record()["title_changed"]["seen"], "retitled-by-claude")
        self.assertIs(lc.tm.has_session(), True, "withheld: nothing was sent")
        verdict, why = launcher.classify(lc.tm, lc.record(), self.cfg, str(self.work))
        self.assertEqual((verdict, why.startswith(launcher.TITLE_CHANGED + ":")), ("unknown", True),
                         "the unknown card names it as its failed check")
        with mock.patch.object(cli, "auth_problem", return_value=""):
            _, text, _ = self.main("verify")
        self.assertIn("INFO %s:" % launcher.TITLE_CHANGED, text)
        self.tmux("select-pane", "-t", rec["generation"]["pane_id"], "-T", rec["title_token"])
        lc.tm.run("kill-session", "-t", rec["generation"]["session_id"])

    def test_a_same_name_replacement_with_the_same_start_command_survives_both_fenced_actions(self):
        """ABA: the recorded tuple's server dies; a new server answers the same socket with the same
        session name and the very start command (the nonce too). /exit and kill-session both refuse."""
        lc, rec = self.created()
        gen = rec["generation"]
        self.kill_server()
        stub_bin = self.home / "stub"
        stub_bin.mkdir()
        (stub_bin / cli.COMMAND).write_text(STUB_SH)
        (stub_bin / cli.COMMAND).chmod(0o755)
        subprocess.run(["env", "-i", "PATH=%s:%s:/usr/bin:/bin" % (stub_bin, TMUX_DIR), "HOME=%s" % self.home, "tmux", "-S", self.sock,
                        "-f", "/dev/null", "new-session", "-d", "-s", launcher.SESSION_NAME, "--",
                        hold_mod.pane_command(cli.COMMAND, gen["nonce"])],
                       check=True)
        fields = wait_for(lambda: lc.tm.pane_fields(gen["pane_id"]), 5)
        self.assertEqual(fields["start_command"], gen["start_command"], "the replacement carries the same nonce")
        self.tmux("select-pane", "-t", gen["pane_id"], "-T", rec["title_token"])   # and even the recorded title
        self.assertEqual(lc.stop_session(rec, "test"), "cancelled: the tuple changed before /exit; nothing sent")
        self.assertEqual(lc.fenced(gen, rec["title_token"], "kill-session -t %s" % tmux_pane_keys.tmux_quote(gen["session_id"])),
                         tmux_pane_keys.WITHHELD)
        self.assertIs(lc.tm.has_session(), True, "the replacement survives both paths")
        self.assertNotIn("/exit", self.tmux("capture-pane", "-p", "-t", gen["pane_id"]).stdout, "zero keys reached it")
        self.assertEqual(launcher.classify(lc.tm, rec, self.cfg, str(self.work))[0], "unknown")

    def test_an_unrecorded_server_on_the_socket_reads_unknown_and_never_75(self):
        subprocess.run(["tmux", "-S", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", launcher.SESSION_NAME, "cat"],
                       check=True)
        lc = self.launcher(clock=lambda: 1000.0)
        verdict, why = launcher.classify(lc.tm, lc.record(), self.cfg, str(self.work))
        self.assertEqual((verdict, "did not create and record" in why), ("unknown", True))
        self.assertEqual(lc.tick(lc.record())[0], None, "the live waiter, not an exit")
        self.assertEqual(lc.record()["deadline"], 1000.0 + launcher.UNKNOWN_WAIT_S)

    def test_a_root_with_hostile_bytes_runs_and_never_reaches_the_start_command(self):
        hostile = self.home / "w #{pane_id} #(x),}; \\ 'q'"
        hostile.mkdir()
        (hostile / "tasks").mkdir()
        (hostile / "results").mkdir()
        self.configure(ROOT=str(hostile), INBOX_DIR=str(hostile / "tasks"), RESULTS_DIR=str(hostile / "results"))
        self.cfg = cli.load_config()[0]
        self.work = hostile
        lc, rec = self.created()
        self.assertNotIn("#{pane_id}", rec["generation"]["start_command"])
        self.assertEqual(task_io.lock_holder(str(hostile))["pid"], rec["consumer"]["pid"])
        self.assertEqual(lc.stop_session(rec, "test"), "stopped")

    def test_uninstall_stops_the_session_its_dead_launcher_left_then_removes_the_config_last(self):
        lc, rec = self.created()
        order = []
        real_free, real_stray = cm._await_free, cli.stop_stray
        with mock.patch.object(cm, "_await_free", lambda root, *a, **k: order.append("free") or real_free(root, *a, **k)), \
                mock.patch.object(cli, "stop_stray", lambda cfg: order.append("stray") or real_stray(cfg)), \
                mock.patch.object(sm, "_run", side_effect=self.runner(root=False)):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(order[:2], ["stray", "free"], "the session stopped through the fence before the root must read free")
        self.assertIs(lc.tm.has_session(), False)
        self.assertFalse(cli.config_path().exists(), "config last")

    def test_creation_is_fenced_while_the_pane_key_guard_is_uncertain(self):
        guard = tmux_pane_keys.prepare_guard(self.sock).parent
        (guard / "uncertain").touch()
        lc = self.launcher()
        ok, problem = lc.create(lc.record())
        self.assertEqual((ok, "creation is fenced" in problem), (False, True))
        self.assertIs(lc.tm.has_session(), False, "nothing was created")
        self.assertEqual(lc.tick(lc.record())[0], launcher.EXIT_RETRY, "74, retryable, never 75")


class CardAccepter(CardBase):
    """The store fence: one accepter, one lock order, one save; the claude-local root beside the
    default store; Keep waiting as a saga-keyed successor; commit-unknown reconciled before any effect."""

    def test_the_card_carries_the_tail_the_attach_command_the_failed_check_and_two_actions_no_host_path(self):
        req = self.raise_card(room="")
        self.assertTrue(req.id.startswith("hitl_cl_"), "local ids are namespaced")
        self.assertEqual([a.id for a in req.actions], ["stop", "keep-waiting"])
        self.assertIn("attach: %s attach" % cli.COMMAND, req.message)
        self.assertIn("which check failed: pane static while an offer is in flight", req.message)
        self.assertIn("last 20 pane lines", req.message)
        sub = card.local(req)
        self.assertEqual((sub["discriminator"], sub["stall_token"], sub["install_id"]),
                         (card.KIND_STALL, records.stall_token(STALL), self.lc.opaque))
        self.assertEqual((sub["progress_lock"], sub["supervisor_record"]),
                         ("state/progress.lock", "state/claude-local-supervisor.json"), "root-relative coordinates")
        wire = json.dumps(cm_dict(req))
        for host in (str(self.work), str(self.home), str(PKG), cli.COMMAND + ".sock"):
            self.assertNotIn(host, wire, "the projection carries no host path")
        self.assertEqual(self.lc.record()["hitl"]["id"], req.id, "the card id lives in the supervisor record")
        before = tree_bytes(self.store_root)
        self.lc.raise_card(self.lc.record(), card.KIND_STALL, "again")
        self.assertEqual(tree_bytes(self.store_root), before, "one card per incident: a retry raises no second")

    def test_a_same_room_owner_stop_is_one_save_and_every_other_reply_is_refused_with_nothing_written(self):
        req = self.raise_card()
        before = tree_bytes(self.store_root)
        for reply, owner_ok, why in ((self.reply(req, room="!other:ag2.space"), True, "another room"),
                                     (self.reply(req), False, "only the owner"),
                                     (self.reply(req, revision=req.revision - 1), True, "stale")):
            out = self.accept(reply, owner_ok=owner_ok)
            self.assertTrue(out.startswith("rejected:"), (why, out))
            self.assertEqual(tree_bytes(self.store_root), before, why)
        saves = []
        real = card.HitlStore.save

        def counting(store, r, projection=None):
            saves.append(r.id)
            return real(store, r, projection)
        with mock.patch.object(card.HitlStore, "save", counting):
            self.assertEqual(self.accept(self.reply(req)), "applied")
        self.assertEqual(saves, [req.id], "the action, revision, status and phase in exactly one save")
        got = self.load(req.id)
        sub = card.local(got)
        self.assertEqual((got.status, got.chosen_action, got.revision, sub["effect_phase"], sub["accepted_revision"]),
                         ("in_progress", "stop", req.revision + 1, "STOPPING", req.revision))
        self.assertTrue(self.accept(self.reply(got)).startswith(("rejected:", "applied:reconciled")), "first decision wins")
        rec = self.lc.apply_effects(self.lc.record())
        self.assertEqual(self.stops, ["the owner confirmed the stop"])
        self.assertEqual((self.load(req.id).status, rec["hitl"], rec["effect"]["outcome"]), ("resolved", None, "stopped"))
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1, "one effect")

    def test_an_unprojected_card_is_refused_from_any_room_and_applied_by_the_local_stop(self):
        req = self.raise_card(room="")
        self.assertTrue(self.accept(self.reply(req, room="")).startswith("rejected:"), "no room, no remote acceptance")
        out = accepter.local_stop(self.state_dir, self.lc.opaque, log=lambda *a: None)
        self.assertEqual(out, "applied", "stop --confirm needs no projector")
        self.assertEqual(card.local(self.load(req.id))["effect_phase"], "STOPPING")

    def test_progress_since_the_card_cancels_every_action_with_nothing_written(self):
        req = self.raise_card()
        before = tree_bytes(self.store_root)
        records.save_supervisor(self.root, dict(records.load_supervisor(self.root), stall=dict(STALL, frame_hash="0" * 16)))
        for action in (card.ACTION_STOP, card.ACTION_KEEP):
            out = self.accept(self.reply(req, action))
            self.assertTrue(out.startswith("cancelled:"), out)
        self.assertEqual(tree_bytes(self.store_root), before)
        out = accepter.local_stop(self.state_dir, self.lc.opaque, log=lambda *a: None)
        self.assertTrue(out.startswith("cancelled:"), out)
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(self.stops, [], "no tmux effect, no successor")

    def test_the_mod_holding_the_gate_makes_the_accepter_see_its_progress(self):
        """The accepter waits on the gate the Mod holds; progress landed meanwhile cancels."""
        req = self.raise_card()
        got = {}
        with records.flocked(records.progress_lock_path(self.root)):
            t = threading.Thread(target=lambda: got.update(out=self.accept(self.reply(req))))
            t.start()
            time.sleep(0.3)
            self.assertNotIn("out", got, "the accepter waits on the progress gate")
            records.save_supervisor(self.root, dict(records.load_supervisor(self.root), stall=None))
        t.join(10)
        self.assertTrue(got["out"].startswith("cancelled:"), got)
        self.assertEqual(self.load(req.id).status, "pending")

    def test_an_accepter_that_committed_first_wins_over_later_progress(self):
        """The phase recorded under the locks is the linearization point."""
        req = self.raise_card()
        self.assertEqual(self.accept(self.reply(req)), "applied")
        self.lc.clear_card(None)                                   # the progress, after the commit
        self.assertEqual(self.load(req.id).status, "in_progress", "a recorded phase is never cancelled by progress")
        self.assertEqual(self.lc.record()["hitl"]["id"], req.id, "the pointer is kept for the effect")
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1)

    def test_a_new_generation_retires_the_old_card_and_an_accepted_stop_never_reaches_it(self):
        req = self.raise_card()
        self.assertEqual(self.accept(self.reply(req)), "applied")
        self.lc.retire_card()                                       # what create does before the new session
        got = self.load(req.id)
        self.assertEqual((got.status, card.local(got)["effect_evidence"]["outcome"]),
                         ("resolved", "the session was gone before the stop"))
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(self.stops, [], "the new generation is never stopped by the old decision")
        pending = self.raise_card(stall=dict(STALL, frame_hash="3" * 16))
        self.lc.retire_card()
        self.assertEqual((self.load(pending.id).status, self.lc.record()["hitl"]), ("cancelled", None))

    def test_a_pending_card_is_cancelled_when_its_stall_clears(self):
        req = self.raise_card()
        self.lc.clear_card(None)
        self.assertEqual((self.load(req.id).status, self.lc.record()["hitl"]), ("cancelled", None))

    def test_apply_action_only_inside_the_three_locks_and_on_the_outer_store(self):
        req = self.raise_card()
        store, mgr = card.manager(self.store_root)
        with self.assertRaises(accepter.Refused):
            accepter._apply(mgr, store, self.reply(req), None)
        other, _ = card.manager(self.store_root)
        with accepter.locks(self.root, self.lc.supervisor_lock, store):
            with self.assertRaises(accepter.Refused):
                accepter._apply(mgr, other, self.reply(req), None)
            accepter._apply(mgr, store, self.reply(req), card.patch_for(card.ACTION_STOP, req.revision, "i"))
        self.assertEqual(self.load(req.id).status, "in_progress", "entered with the outer two held: committed")

    def test_the_same_store_check_survives_python_O(self):
        store, mgr = card.manager(self.store_root)
        other, _ = card.manager(self.store_root)
        with self.assertRaises(accepter.Refused):
            accepter.same_store(mgr, other)
        accepter.same_store(mgr, store)
        r = subprocess.run([sys.executable, "-O", "-c", covered_code("import sys; sys.path.insert(0, %r)\n"
                            "from ag2_sparrow import claude_local_accepter as a, claude_local_card as c\n"
                            "s, m = c.manager(%r); o, _ = c.manager(%r)\n"
                            "try:\n    a.same_store(m, o)\nexcept a.Refused:\n    print('refused')\n"
                            % (str(PKG), str(self.store_root), str(self.store_root)))], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "refused", r.stderr)

    def test_the_resolved_coordinates_are_the_gate_opened_and_the_record_read_under_the_lock_order(self):
        req = self.raise_card()
        sub = card.local(req)
        opened, real = [], records.flocked
        store_cls_locked = card.HitlStore.locked

        def probe_free(path):
            fd = os.open(str(path), os.O_RDWR)
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return True
            except BlockingIOError:
                return False
            finally:
                os.close(fd)

        @contextlib.contextmanager
        def spy_flocked(path, blocking=True):
            opened.append(Path(path))
            with real(path, blocking) as fd:
                yield fd

        @contextlib.contextmanager
        def spy_locked(store):
            opened.append(("store", probe_free(self.lc.supervisor_lock), probe_free(records.progress_lock_path(self.root))))
            with store_cls_locked(store):
                yield
        with mock.patch.object(records, "flocked", spy_flocked), mock.patch.object(card.HitlStore, "locked", spy_locked):
            self.assertEqual(self.accept(self.reply(req)), "applied")
        self.assertEqual(opened[0], card.resolve_under(self.root, sub["progress_lock"]), "the gate is the resolved coordinate")
        self.assertEqual(opened[1], self.lc.supervisor_lock)
        self.assertEqual(opened[2], ("store", False, False), "the store lock is taken with the gate and supervisor lock held")
        for odd in ("state/other.lock", "results/progress.lock"):
            req = self.raise_card(stall=dict(STALL, frame_hash=os.urandom(8).hex()))
            req.subject = {card.SUBJECT: dict(card.local(req), progress_lock=odd)}
            store, _ = card.manager(self.store_root)
            store.save(req, store.projection(req.id))
            out = self.accept(self.reply(req))
            self.assertTrue(out.startswith("rejected:") and "do not name" in out, (odd, out))
            self.lc.clear_card(None)

    def test_a_coordinate_escaping_the_root_is_refused(self):
        for bad in ("../outside.lock", "/etc/passwd", "state/../../x"):
            req = self.raise_card()
            req.subject = {card.SUBJECT: dict(card.local(req), progress_lock=bad)}
            store, _ = card.manager(self.store_root)
            store.save(req, store.projection(req.id))
            out = self.accept(self.reply(req))
            self.assertTrue(out.startswith("rejected:"), (bad, out))
            self.assertEqual(self.load(req.id).status, "pending")
            self.lc.clear_card(None)
        self.assertEqual(card.resolve_under(self.root, "state/progress.lock"), Path(self.root).resolve() / "state" / "progress.lock")
        with self.assertRaises(ValueError):
            card.resolve_under(self.root, "state/../../x")

    def test_keep_waiting_is_one_successor_derived_from_the_saga_key_at_every_crash_point(self):
        req = self.raise_card()
        saves = []
        real = card.HitlStore.save
        with mock.patch.object(card.HitlStore, "save", lambda st, r, projection=None: saves.append(r.id) or real(st, r, projection)), \
                mock.patch.object(card, "reconcile_rearm"):          # the crash right after the one acceptance save
            self.assertEqual(self.accept(self.reply(req, card.ACTION_KEEP)), "applied")
        self.assertEqual(saves, [req.id])
        pred = self.load(req.id)
        saga = card.local(pred)["saga_key"]
        self.assertEqual(saga, {"incident_id": card.local(pred)["incident"], "predecessor_id": req.id,
                                "accepted_expected_revision": req.revision})
        sid = card.successor_id(saga)
        self.assertEqual(card.local(pred)["successor_id"], sid)
        snapshot = tree_bytes(self.store_root), records.load_supervisor(self.root)

        def restart():
            fresh = launcher.Launcher(cli.load_config()[0], cli.config_dir(), sleep=lambda s: None, clock=lambda: 6000.0)
            fresh.stop_session = lambda rec, why: self.stops.append(why) or "stopped"
            return fresh.apply_effects(fresh.record())

        def successors():
            return [r for r in card.manager(self.store_root)[0].all() if card.local(r).get("predecessor_id") == req.id]
        store, mgr = card.manager(self.store_root)
        crashes = {"after acceptance": lambda: None,
                   "after predecessor resolution": lambda: mgr.resolve(req.id),
                   "after successor creation": lambda: card.reconcile_rearm(store, mgr, self.load(req.id), now=1.0)}
        for name, crash in crashes.items():
            for path, data in snapshot[0].items():
                (self.store_root / path).write_bytes(data)
            for extra in set(tree_bytes(self.store_root)) - set(snapshot[0]):
                (self.store_root / extra).unlink()
            records.save_supervisor(self.root, snapshot[1])
            crash()
            rec = restart()
            rec = restart()                                          # recovered twice
            got = successors()
            self.assertEqual([r.id for r in got], [sid], name)
            self.assertEqual((rec["hitl"]["id"], self.load(req.id).status, got[0].status), (sid, "resolved", "pending"), name)
            self.assertEqual(got[0].guard, card.successor_guard(saga), "a distinct guard derived from the key")
            self.assertNotEqual(got[0].guard, req.guard)
            self.assertIsNone(got[0].decided_by, "policy=None: never auto-decided")
            self.assertEqual(card.local(got[0])["stall_token"], card.local(req)["stall_token"])
        restart()
        self.assertEqual([r.id for r in successors()], [sid], "after the pointer move too")
        self.assertEqual(self.stops, [], "Keep waiting stops nothing")
        self.assertEqual(self.accept(self.reply(self.load(sid))), "applied", "the successor is the live card")

    def test_a_planted_same_id_successor_with_another_saga_key_is_refused(self):
        req = self.raise_card()
        with mock.patch.object(card, "reconcile_rearm"):
            self.assertEqual(self.accept(self.reply(req, card.ACTION_KEEP)), "applied")
        saga = card.local(self.load(req.id))["saga_key"]
        planted = card.build(card.KIND_STALL, incident="other", install_id="x", room=ROOM, stall=STALL, tail=[],
                             attach_cmd="", failed_check="x", req_id=card.successor_id(saga), guard="g")
        planted.subject = {card.SUBJECT: dict(card.local(planted), saga_key=dict(saga, accepted_expected_revision=99))}
        store, mgr = card.manager(self.store_root)
        store.save(planted)
        before = tree_bytes(self.store_root)
        rec = self.lc.apply_effects(self.lc.record())
        self.assertEqual(tree_bytes(self.store_root), before, "fail closed: nothing created or moved")
        self.assertEqual(rec["hitl"]["id"], req.id)

    def test_commit_unknown_is_made_durable_before_the_one_effect_and_a_lost_or_unreplaced_save_is_pending(self):
        from ag2_sparrow.hitl import manager as hm
        real_fsync = hm.os.fsync

        def dir_fsync_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "dir fsync failed")
            return real_fsync(fd)
        # Post-replace, pre-fsync, the replacement survives: recovered twice, one effect.
        req = self.raise_card()
        with mock.patch.object(hm.os, "fsync", dir_fsync_fails):
            out = self.accept(self.reply(req))
        self.assertTrue(out.startswith("commit-unknown:"), out)
        self.assertEqual(card.local(self.load(req.id))["effect_phase"], "STOPPING", "visible, durability unknown")
        self.assertEqual(self.stops, [], "no effect at acceptance")
        fsynced = []
        real_dir_fsync = records.fsync_dir
        with mock.patch.object(accepter.records, "fsync_dir", lambda p: fsynced.append(Path(p)) or real_dir_fsync(p)):
            self.lc.apply_effects(self.lc.record())
        self.assertIn(self.store_root, fsynced, "re-fsync before the effect")
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1, "one effect")
        # The same, with the replacement lost: pending, zero effect.
        self.stops.clear()
        self.lc.clear_card(None)
        req = self.raise_card(stall=dict(STALL, frame_hash="1" * 16))
        path = self.store_root / ("%s.json" % req.id)
        old = path.read_bytes()
        with mock.patch.object(hm.os, "fsync", dir_fsync_fails):
            self.assertTrue(self.accept(self.reply(req)).startswith("commit-unknown:"))
        path.write_bytes(old)
        self.lc.apply_effects(self.lc.record())
        self.assertEqual((self.load(req.id).status, self.stops), ("pending", []))
        # Pre-replace failure: pending, zero effect, and a later ordinary acceptance applies once.
        with mock.patch.object(hm.os, "replace", side_effect=OSError(errno.EIO, "replace failed")):
            self.assertTrue(self.accept(self.reply(req)).startswith("commit-unknown:"))
        self.assertEqual(self.load(req.id).status, "pending")
        self.assertEqual(self.accept(self.reply(req)), "applied")
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1)

    def test_the_store_fence_one_match_only_and_a_planted_collision_mutates_nothing(self):
        """The claude-local root alone holds claude-local cards; the default store is
        never touched; a same id in both stores is zero mutation and zero effect, by bytes."""
        default_store, default_mgr = card.HitlStore(card.default_root(self.state_dir)), None
        from ag2_sparrow.hitl.manager import HitlManager
        from ag2_sparrow.hitl.schema import Action, HumanRequirement
        default_mgr = HitlManager(default_store)
        other = default_mgr.create(HumanRequirement(kind="permission", runtime="claude", message="m", guard="g1",
                                                    device={"id": "d"}, room=ROOM,
                                                    actions=[Action(id="stop", kind="allow_once", label="Stop")]))
        default_before = tree_bytes(default_store.root)
        req = self.raise_card()
        self.assertEqual(tree_bytes(default_store.root), default_before, "(a) a claude-local card never lands in the default store")
        self.assertIsNone(default_store.load(req.id))
        out = self.accept(ActionReply(hitl_id=other.id, expected_revision=other.revision, action_id="stop", guard=other.guard,
                                      room=ROOM, sender=OWNER_MXID))
        self.assertTrue(out.startswith("rejected:"), out)
        self.assertEqual(tree_bytes(default_store.root), default_before, "(a) the default store's card is never touched")
        self.assertTrue(self.accept(self.reply(req, revision=req.revision)).startswith("applied"))
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1)
        req2 = self.raise_card(stall=dict(STALL, frame_hash="2" * 16))
        self.assertNotEqual(req2.id, req.id)
        planted = json.loads((self.store_root / ("%s.json" % req2.id)).read_text())
        (default_store.root / ("%s.json" % req2.id)).write_text(json.dumps(planted))
        both = tree_bytes(self.store_root), tree_bytes(default_store.root)
        out = self.accept(self.reply(req2))
        self.assertTrue(out.startswith("rejected:") and "both stores" in out, out)
        self.assertEqual((tree_bytes(self.store_root), tree_bytes(default_store.root)), both, "(b) zero mutation")
        self.lc.apply_effects(self.lc.record())
        self.assertEqual((len(self.stops), self.load(req2.id).status), (1, "pending"), "(b) zero effect")
        self.assertTrue(self.accept(ActionReply(hitl_id="hitl_cl_nope", expected_revision=1, action_id="stop", guard="",
                                                room=ROOM, sender=OWNER_MXID)).startswith("rejected:"), "zero matches")

    def test_a_bare_exit_correlates_only_to_exactly_one_open_card_in_its_room(self):
        store, _ = card.manager(self.store_root)
        self.assertEqual(accepter.exit_text_reply(store, ROOM, OWNER_MXID), 0)
        req = self.raise_card()
        got = accepter.exit_text_reply(store, ROOM, OWNER_MXID)
        self.assertEqual((got.hitl_id, got.action_id, got.room), (req.id, "stop", ROOM))
        self.assertEqual(accepter.exit_text_reply(store, "!elsewhere:ag2.space", OWNER_MXID), 0)
        second = card.build(card.KIND_UNKNOWN, incident="i2", install_id="x", room=ROOM, stall=STALL, tail=[], attach_cmd="",
                            failed_check="x")
        card.manager(self.store_root)[1].create(second)
        self.assertEqual(accepter.exit_text_reply(store, ROOM, OWNER_MXID), 2, "more than one open: refused, never a guess")


class CardRefusals(CardBase):
    """Every acceptance the reloaded card cannot back is refused with nothing written."""

    def test_moved_coordinates_another_install_a_decided_card_and_a_second_keep_are_refused(self):
        req = self.raise_card()
        before = tree_bytes(self.store_root)
        real = accepter.coordinates
        calls = []

        def moved(root, sub):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError("lock coordinates changed under the locks")
            return real(root, sub)
        with mock.patch.object(accepter, "coordinates", moved):
            self.assertEqual(self.accept(self.reply(req)), "rejected:%s: lock coordinates changed under the locks" % req.id)
        records.save_supervisor(self.root, dict(records.load_supervisor(self.root), install_id="another"))
        self.assertTrue(self.accept(self.reply(req)).endswith("the card names another install"))
        records.save_supervisor(self.root, dict(records.load_supervisor(self.root), install_id=self.lc.opaque))
        self.assertEqual(tree_bytes(self.store_root), before, "nothing written")
        store, mgr = card.manager(self.store_root)
        keep = self.load(req.id)
        keep.subject = {card.SUBJECT: dict(card.local(keep), saga_key={"predecessor_id": req.id})}
        store.save(keep, store.projection(req.id))
        self.assertEqual(self.accept(self.reply(self.load(req.id), card.ACTION_KEEP)), "rejected:%s already rearmed" % req.id)
        self.assertFalse(accepter.reconcile_unconfirmed(store, mgr, self.load(req.id)), "pending: nothing to reconcile")
        self.assertIsNone(card.reconcile_rearm(store, mgr, self.load(req.id)), "not rearming")
        mgr.cancel(req.id)
        self.assertEqual(self.accept(self.reply(self.load(req.id))), "rejected:%s already cancelled" % req.id)

    def test_a_saga_key_that_does_not_derive_its_successor_is_refused(self):
        req = self.raise_card()
        store, mgr = card.manager(self.store_root)
        bad = self.load(req.id)
        saga = {"predecessor_id": "hitl_cl_someone-else"}
        bad.subject = {card.SUBJECT: dict(card.local(bad), effect_phase=card.PHASE_REARMING, saga_key=saga,
                                          successor_id=card.successor_id(saga))}
        with self.assertRaises(ValueError):
            card.reconcile_rearm(store, mgr, bad)

    def test_the_local_stop_refuses_to_guess_between_two_open_cards(self):
        self.raise_card()
        second = card.build(card.KIND_UNKNOWN, incident="i2", install_id=self.lc.opaque, room=ROOM, stall=STALL, tail=[],
                            attach_cmd="", failed_check="x")
        card.manager(self.store_root)[1].create(second)
        out = accepter.local_stop(self.state_dir, self.lc.opaque, log=lambda *a: None)
        self.assertTrue(out.startswith("rejected:2 stall cards are open"), out)


class RecordsAndTaskIo(unittest.TestCase):
    """The record reader and the task_io delegations read every failure as a refusal or nobody."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cl-records-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_record_that_is_unreadable_not_json_or_not_an_object_is_a_record_error(self):
        (self.dir / "list.json").write_text("[1]")
        (self.dir / "bad.json").write_text("{")
        (self.dir / "dir.json").mkdir()
        for name, why in (("list.json", "not a JSON object"), ("bad.json", "not a JSON record"), ("dir.json", "")):
            with self.assertRaises(records.RecordError) as e:
                records.read_json(self.dir / name)
            self.assertIn(why, str(e.exception))
        self.assertEqual(records.read_json(self.dir / "absent.json"), {})

    def test_the_progress_gate_probe_reads_held_free_and_unopenable(self):
        root = self.dir / "root"
        self.assertFalse(records.progress_gate_held(root), "no state dir: nobody holds it")
        records.progress_lock_path(root).parent.mkdir(parents=True)
        self.assertFalse(records.progress_gate_held(root))
        with records.flocked(records.progress_lock_path(root)):
            self.assertTrue(records.progress_gate_held(root))

    def test_task_io_reads_an_unreadable_lock_state_as_nobody_or_unobserved(self):
        boom = mock.patch.object(holder_lock, "status", side_effect=OSError(errno.EIO, "io"))
        with boom:
            self.assertIsNone(task_io.lock_holder_pid(str(self.dir)))
            self.assertIsNone(task_io.holder_consumer(str(self.dir)))
        with mock.patch.object(holder_lock, "occupancy", side_effect=holder_lock.LockError("x")):
            self.assertEqual(task_io.occupancy(str(self.dir)), "unobserved")
        with mock.patch.object(holder_lock, "tombstone_consumer", return_value="published") as t:
            self.assertEqual(task_io.tombstone_consumer(str(self.dir), b"x", "launcher", "gone"), "published")
        t.assert_called_once_with(str(self.dir), b"x", by="launcher", reason="gone")


class TmuxClientUtf8(unittest.TestCase):
    """Every tmux client the adapter and its vendored helpers start passes -u, so a TAB in
    display-message output survives a process with no UTF-8 locale."""

    def recorder(self, seen, rc=0):
        def run(argv, **_):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, rc, "", "")
        return run

    def test_launcher_tmux_version_and_every_server_command(self):
        seen = []
        launcher.tmux_version("/x/tmux", run=self.recorder(seen))
        tm = launcher.Tmux("/s", tmux="/x/tmux", run=self.recorder(seen))
        tm.run("list-sessions")
        tm.pane_fields("%1")
        tm.capture("%1")
        self.assertEqual([a[:2] for a in seen], [["/x/tmux", "-u"]] * 4)

    def test_create_session_starts_a_utf8_client_and_a_utf8_server(self):
        seen = []
        tm = launcher.Tmux("/s", tmux="/x/tmux", run=self.recorder(seen))
        launcher.create_session(tm, {"SESSION_ID": SESSION_ID}, "/c", "/r", "0" * 32, environ={},
                                run=self.recorder(seen, rc=1))
        [argv] = seen
        self.assertEqual(argv[argv.index("/x/tmux") + 1], "-u")
        self.assertIn("LC_CTYPE=en_US.UTF-8", argv[:argv.index("/x/tmux")], "the server's env -i environment")

    def test_tmux_probe_has_session(self):
        seen = []
        with mock.patch.object(tmux_probe.subprocess, "run", self.recorder(seen)):
            tmux_probe.has_session("/s", "x", tmux="/x/tmux")
        self.assertEqual(seen[0][:2], ["/x/tmux", "-u"])

    def test_pane_keys_send_and_fenced_action_reach_tmux_with_u(self):
        with tempfile.TemporaryDirectory() as d:
            fake, log = Path(d) / "tmux", Path(d) / "log"
            fake.write_text("#!/bin/sh\nprintf '%%s\\n' \"$1\" >> '%s'\n" % log)
            fake.chmod(0o755)
            sock = str(Path(d) / "s")
            pred = tmux_pane_keys.predicate("0" * 32, 1, "$1", 1)
            for argv in (tmux_pane_keys.argv(sock, "%1", "Enter", tmux=str(fake)),
                         tmux_pane_keys.fence_argv(sock, "%1", pred, "send-keys -t %1 Enter", tmux=str(fake))):
                subprocess.run(argv, capture_output=True, timeout=30)
            self.assertEqual(log.read_text().split(), ["-u", "-u"])


class LocalCommands(ConnectBase):
    """§6: attach is tmux attach on the adapter's socket; stop needs --confirm; verify names what waits."""

    def test_attach_execs_tmux_on_the_adapters_own_socket(self):
        self.configure()
        seen = []
        with mock.patch.object(os, "execvp", lambda f, argv: seen.append(argv)):
            self.main("attach")
        self.assertEqual(seen[0][1:], ["-u", "-S", str(launcher.socket_path(cli.config_dir())), "attach", "-t", "=claude-local"])
        self.assertNotIn("sutando-tmux", seen[0][3])
        help_text = subprocess.run([sys.executable, "-c", covered_code("import sys; sys.path.insert(0, %r); from ag2_sparrow.adapters."
                                    "claude_code.cli import main; main(['attach', '--help'])" % str(PKG))],
                                   capture_output=True, text=True).stdout
        for phrase in ("same session", "/clear", "/compact", "never kill the server"):
            self.assertIn(phrase, " ".join(help_text.split()))

    def test_stop_refuses_without_confirm_and_without_an_open_card(self):
        self.configure()
        rc, _, err = self.main("stop")
        self.assertEqual((rc, "--confirm" in err), (2, True))
        shutil.rmtree(card.store_root(_dirs.state_dir()).parent, ignore_errors=True)
        rc, out, _ = self.main("stop", "--confirm")
        self.assertEqual((rc, "no stall card is open" in out), (1, True))

    def test_verify_names_a_card_waiting_for_projection_and_why(self):
        self.configure()
        state = _dirs.state_dir()
        shutil.rmtree(card.store_root(state).parent, ignore_errors=True)
        self.addCleanup(shutil.rmtree, card.store_root(state).parent, True)
        req = card.manager(card.store_root(state))[1].create(card.build(
            card.KIND_STALL, incident="i", install_id="x", room="", stall=STALL, tail=[], attach_cmd="", failed_check="x"))
        routing = state / "owner-routing.json"
        routing.unlink(missing_ok=True)
        line = cli.projection_lines(cli.load_config()[0])
        self.assertEqual(line, ["INFO pending projection: %s (no primary gateway); on this machine: %s stop --confirm"
                                % (req.id, cli.COMMAND)])
        routing.write_text("{}")
        self.addCleanup(routing.unlink, missing_ok=True)
        self.assertIn("(gateway predates claude-local)", cli.projection_lines(cli.load_config()[0])[0])


class LauncherUnit(ConnectBase):
    """The logical exits, the entry shim's table, the stop handlers' order, the classifier."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.cfg = cli.load_config()[0]

    def test_the_launchd_entry_shim_maps_logical_75_to_0_and_everything_else_to_non_zero(self):
        self.assertEqual({rc: launcher.launchd_exit(rc) for rc in (-9, -15, 0, 1, 2, 74, 75)},
                         {-9: 1, -15: 1, 0: 1, 1: 1, 2: 2, 74: 74, 75: 0})
        for code, expect in (("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 1),
                             ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", 1),
                             ("raise SystemExit(0)", 1), ("raise SystemExit(74)", 74), ("raise SystemExit(75)", 0)):
            self.assertEqual(launcher.run_launchd([sys.executable, "-c", code]), expect, code)

    def test_run_launchd_is_the_shim_and_plain_run_is_the_launcher(self):
        seen = []
        with mock.patch.object(launcher, "run_launchd", lambda argv, popen=None: seen.append(argv) or 0):
            self.assertEqual(self.main("run", "--launchd")[0], 0)
        self.assertEqual(seen, [[str(self.bin / cli.COMMAND), "run"]], "the child is the plain launcher")
        with mock.patch.dict(os.environ, {cm.MANAGED_VAR: ""}):
            rc, _, err = self.main("run", "--launchd")
        self.assertEqual((rc, cm.MANAGED_VAR in err), (2, True), "the gate holds for the shim too")

    def test_the_classifier_returns_75_only_for_foreign_terminal_contention(self):
        root = str(self.work)
        gen = {"pane_id": "%0", "pane_pid": 1, "session_id": "$0", "session_created": 1, "server_pid": 1, "server_start": "s",
               "start_command": "c", "nonce": NONCE}
        mine = {"adapter": "claude-local", "install_id": task_io.install_id()}
        cases = [("held", mine, {"generation": gen}, "unknown"), ("unobserved", mine, {"generation": gen}, "unknown"),
                 ("unobserved", mine, {}, "unknown"), ("held", mine, {}, "unknown"),
                 ("held", {"adapter": "codex", "install_id": "x"}, {}, "foreign"),
                 ("held", {"adapter": "claude-local", "install_id": "another-install"}, {"generation": gen}, "foreign"),
                 ("free", {}, {}, "create")]
        for occ, holder, rec, expect in cases:
            with mock.patch.object(task_io, "occupancy", return_value=occ), \
                    mock.patch.object(task_io, "lock_holder", return_value=holder), \
                    mock.patch.object(task_io, "verify_lineage", return_value=None):
                verdict, _ = launcher.classify(FakeTmux(present=bool(rec), fields=None), dict(records.empty_supervisor(), **rec),
                                               self.cfg, root)
            self.assertEqual(verdict, expect, (occ, holder, rec))
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None, clock=lambda: 1.0)
        lc.tm = FakeTmux(present=None)
        with mock.patch.object(task_io, "occupancy", return_value="unobserved"):
            self.assertEqual(lc.tick(lc.record())[0], None, "unobserved is the waiter, never 75")
        with mock.patch.object(task_io, "occupancy", return_value="held"), \
                mock.patch.object(task_io, "lock_holder", return_value={"adapter": "codex", "install_id": "x"}):
            self.assertEqual(lc.tick(lc.record())[0], cm.EXIT_HELD)

    def test_the_stop_handlers_go_over_the_inherited_dispositions_before_any_lock(self):
        seen = []
        real = records.flocked

        @contextlib.contextmanager
        def spy(path, blocking=True):
            seen.append((Path(path).name, signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)))
            with real(path, blocking) as fd:
                yield fd
        before = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None)
        with mock.patch.object(launcher, "tmux_problem", return_value=""), mock.patch.object(records, "flocked", spy), \
                mock.patch.object(launcher.Launcher, "_serve", lambda self: 0):
            self.assertEqual(lc.serve(), 0)
        name, term, intr = seen[0]
        self.assertEqual(name, launcher.LAUNCHER_LOCK)
        self.assertNotIn(term, (signal.SIG_IGN, signal.SIG_DFL), "the inherited SIG_IGN was replaced before the lock")
        self.assertIs(term, intr, "one stop path")
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN, "restored on the way out")

    def test_a_second_launcher_of_the_install_is_retryable_and_a_bad_host_is_unusable(self):
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None)
        with mock.patch.object(launcher, "tmux_problem", return_value=""), \
                records.flocked(cli.config_dir() / launcher.LAUNCHER_LOCK):
            self.assertEqual(lc.serve(), launcher.EXIT_RETRY)
        with mock.patch.object(launcher, "tmux_problem", return_value="tmux 3.1 is older"):
            self.assertEqual(lc.serve(), cm.EXIT_UNUSABLE)
        with mock.patch.object(launcher, "socket_problem", return_value="too long"):
            self.assertEqual(lc.serve(), cm.EXIT_UNUSABLE)

    def test_the_waiter_keeps_its_deadline_and_its_one_card_across_launcher_restarts(self):
        """The unknown episode is not an exit; each restarted launcher resumes the recorded deadline
        and the recorded card instead of starting over or raising a second one."""
        store_root = card.store_root(_dirs.state_dir())
        shutil.rmtree(store_root.parent, ignore_errors=True)
        self.addCleanup(shutil.rmtree, store_root.parent, True)

        def launcher_at(now):
            lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None, clock=lambda: now)
            lc.tm = FakeTmux(present=None)
            return lc
        for now in (1000.0, 1050.0):
            lc = launcher_at(now)
            self.assertEqual(lc.tick(lc.record())[0], None, "the waiter stays alive")
            self.assertEqual((lc.record()["deadline"], lc.record()["hitl"]), (1000.0 + launcher.UNKNOWN_WAIT_S, None),
                             "silent until the original deadline")
        lc = launcher_at(1091.0)
        lc.tick(lc.record())
        first = lc.record()["hitl"]["id"]
        req = card.manager(store_root)[0].load(first)
        self.assertEqual((card.local(req)["discriminator"], "tmux did not answer" in req.message), (card.KIND_UNKNOWN, True))
        for now in (1100.0, 2000.0):
            lc = launcher_at(now)
            lc.tick(lc.record())
            self.assertEqual(lc.record()["hitl"]["id"], first, "the same card after a restart")
        self.assertEqual([r.id for r in card.manager(store_root)[0].all()], [first], "one card")

    def test_the_stop_bounds_nest_inside_the_managers(self):
        send = tmux_pane_keys.TIMEOUT_S + 5
        inner = 2 * send + launcher.CLAUDE_GRACE_S + launcher.SERVER_EXIT_S
        self.assertLessEqual(inner, launcher.INNER_STOP_S)
        self.assertLess(launcher.INNER_STOP_S, cli.LOCAL.stop_timeout_s)
        self.assertLess(cli.LOCAL.stop_timeout_s, cli.LOCAL.manager_timeout_s, "the installer outlasts the stop it waits on")
        self.assertEqual((cli.LOCAL.stop_timeout_s, cli.LOCAL.manager_timeout_s), (180, 240))

    def test_a_kill_session_busy_or_usage_exit_is_never_read_as_sent(self):
        gen = {"pane_id": "%0", "pane_pid": 1, "session_id": "$0", "session_created": 1, "server_pid": 1,
               "server_start": "s", "start_command": "c", "nonce": NONCE}
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None)
        lc.tm = FakeTmux(present=True)
        with mock.patch.object(launcher, "SERVER_EXIT_S", 0.0), mock.patch.object(launcher, "CLAUDE_GRACE_S", 0.0):
            for rc, word in ((tmux_pane_keys.BUSY, "busy:"), (2, "error:")):
                with mock.patch.object(lc, "fenced", side_effect=[0, rc]):
                    out = lc.stop_session({"generation": gen, "title_token": None}, "test")
                self.assertTrue(out.startswith(word), (rc, out))

    def test_creation_reads_the_pane_key_guard_once(self):
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None)
        with mock.patch.object(tmux_pane_keys, "fence_status", side_effect=["uncertain", "clear"]) as status:
            ok, problem = lc.create(lc.record())
        self.assertEqual((ok, status.call_count), (False, 1))
        self.assertIn("is uncertain", problem, "the reason is the reading that decided")

    def test_a_manager_stop_runs_the_fenced_stop_path_and_maps_its_outcome(self):
        lc = launcher.Launcher(self.cfg, cli.config_dir(), sleep=lambda s: None)
        for outcome, rc in (("stopped", 0), ("nothing to stop", 0), ("cancelled: the tuple changed before /exit; nothing sent", 74),
                            ("uncertain: x", 74)):
            lc.state["sig"] = signal.SIGTERM
            with mock.patch.object(lc, "stop_session", return_value=outcome), mock.patch.object(lc, "apply_effects", lambda r: r):
                self.assertEqual(lc._serve(), rc, outcome)


class LauncherBranches(ConnectBase):
    """Every refusal of the tmux probes, the creation, the classifier, the card effects and the loop
    is one outcome, never a traceback and never read as success."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.cfg = cli.load_config()[0]
        self.lines = []

    def launcher(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        return launcher.Launcher(self.cfg, cli.config_dir(), log=self.lines.append, **kw)

    def test_a_tmux_that_does_not_run_or_answers_garbage_reads_as_no_answer(self):
        self.assertIsNone(REAL_TMUX_VERSION("tmux", run=raises))
        tm = launcher.Tmux("/tmp/stub.sock", tmux="tmux", run=lambda *a, **k: ran(1))
        self.assertIsNone(tm.pane_fields("%0"), "a failed display-message")
        tm = launcher.Tmux("/tmp/stub.sock", tmux="tmux", run=lambda *a, **k: ran(0, launcher.FIELD_SEP.join("abcdefg")))
        self.assertIsNone(tm.pane_fields("%0"), "seven fields, not one of them a number")
        self.assertIsNone(launcher.Tmux("/tmp/stub.sock", tmux="tmux", run=raises).run("list-sessions"))

    def test_session_creation_refuses_each_unverifiable_step(self):
        good = launcher.hold.pane_command(cli.LOCAL.command, NONCE)
        fields = {"pane_id": "%0", "pane_pid": 2, "session_id": "$0", "session_created": 3, "server_pid": 4,
                  "start_command": good, "title": ""}
        cases = [(raises, None, "did not run"), (lambda *a, **k: ran(0), None, "could not be read back"),
                 (lambda *a, **k: ran(0), dict(fields, start_command="sh -c evil"), "not the fixed command")]
        for run, f, why in cases:
            tm = StubTm(fields=f)
            gen, problem = launcher.create_session(tm, self.cfg, cli.config_dir(), self.work, NONCE, {}, run)
            self.assertEqual((gen, why in problem), (None, True), problem)
        self.assertIn(("kill-session", "-t", "=%s" % launcher.SESSION_NAME), tm.calls, "the foreign pane is not kept")
        with mock.patch.object(task_io, "start_time", return_value=None):
            gen, problem = launcher.create_session(StubTm(fields=fields), self.cfg, cli.config_dir(), self.work, NONCE, {},
                                                   lambda *a, **k: ran(0))
        self.assertEqual((gen, problem), (None, "the server's start time is unreadable"))

    def test_no_consumer_within_the_bound_is_none(self):
        with mock.patch.object(task_io, "holder_consumer", return_value=None):
            self.assertEqual(launcher.await_consumer(StubTm(), str(self.work), "%0", wait_s=0, sleep=lambda s: None),
                             (None, None))

    def test_the_classifier_withholds_adoption_on_each_failed_check(self):
        mine = {"adapter": "claude-local", "install_id": task_io.install_id()}
        fields = {k: GEN[k] for k in ("pane_id", "pane_pid", "session_id", "session_created", "server_pid", "start_command")}
        rec = dict(records.empty_supervisor(), generation=GEN)
        cases = [(FakeTmux(present=False), "held", None, None, "gone but the root reads held"),
                 (FakeTmux(present=True, fields=fields), "held", False, None, "does not verify by pid and start"),
                 (FakeTmux(present=True, fields=fields), "held", True, None, "claude does not verify"),
                 (FakeTmux(present=True, fields=fields), "held", True, {"live": False}, "claude does not verify")]
        for tm, occ, lineage, consumer, why in cases:
            with mock.patch.object(task_io, "occupancy", return_value=occ), \
                    mock.patch.object(task_io, "lock_holder", return_value=mine), \
                    mock.patch.object(task_io, "verify_lineage", return_value=lineage), \
                    mock.patch.object(task_io, "holder_consumer", return_value=consumer):
                verdict, detail = launcher.classify(tm, rec, self.cfg, str(self.work))
            self.assertEqual((verdict, why in detail), ("unknown", True), detail)

    def test_creation_refuses_a_live_consumer_an_unproven_server_a_bad_root_and_a_failed_session(self):
        lc = self.launcher()
        rec = dict(lc.record(), generation=GEN)
        clear = mock.patch.object(tmux_pane_keys, "fence_status", return_value="clear")
        with clear, mock.patch.object(task_io, "holder_consumer", return_value={"live": None}):
            self.assertEqual(lc.create(rec), (False, "a recorded claude consumer still verifies or is unobserved; no second session"))
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(task_io, "verify_lineage", return_value=None):
            self.assertEqual(lc.create(rec), (False, "the recorded server is not proven gone"))
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(inbox, "prepare_root", return_value="the root is wrong"):
            self.assertEqual(lc.create({}), (False, "the root is wrong"))
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(launcher, "create_session", return_value=(None, "tmux new-session failed: x")):
            self.assertEqual(lc.create({}), (False, "tmux new-session failed: x"))
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(launcher, "create_session", return_value=(dict(GEN), "")), \
                mock.patch.object(launcher, "await_consumer", return_value=(None, None)):
            ok, problem = lc.create({})
        self.assertEqual((ok, "no consumer attached" in problem), (False, True))
        self.assertEqual(lc.record()["generation"], GEN, "the made generation is recorded before the wait")

    def test_observation_without_a_pane_keeps_the_record_and_a_cleared_stall_is_logged(self):
        lc = self.launcher()
        rec = lc.record()
        self.assertIs(lc.observe(rec), rec, "no generation, nothing captured")
        lc.store_root, lc.tm = None, StubTm()
        lc.tm.capture = lambda pane_id: "screen"
        stalled = lc._with_record(lambda r: dict(r, generation=GEN, stall={"reason": "r", "checks": "c"}))
        with mock.patch.object(detector, "derive", return_value={"stall": None}):
            out = lc.observe(stalled)
        self.assertEqual((out["stall"], self.lines), (None, ["progress: the stall cleared"]))
        self.assertIsNone(lc.record()["stall"], "with no card store the evidence is recorded alone")

    def card_mocks(self, req, sub):
        store = mock.Mock()
        store.load.return_value = req
        return [mock.patch.object(card, "manager", return_value=(store, mock.Mock())),
                mock.patch.object(accepter, "locks", lambda *a: contextlib.nullcontext()),
                mock.patch.object(card, "local", return_value=sub),
                mock.patch.object(card, "reconcile_rearm", side_effect=ValueError("the successor is foreign"))]

    def test_retiring_a_rearm_that_cannot_reconcile_is_logged_and_the_pointer_cleared(self):
        lc = self.launcher()
        lc._with_record(lambda r: dict(r, hitl={"id": "h", "store_root": "s"}))
        req = type("Req", (), {"status": "resolved", "id": "h"})()
        with contextlib.ExitStack() as stack:
            for p in self.card_mocks(req, {"effect_phase": card.PHASE_REARMING, "saga_key": "k"}):
                stack.enter_context(p)
            rec = lc.retire_card()
        self.assertEqual((rec["hitl"], self.lines), (None, ["retiring the old card: the successor is foreign"]))

    def test_effects_of_a_vanished_card_are_nothing(self):
        lc = self.launcher()
        rec = lc._with_record(lambda r: dict(r, hitl={"id": "h", "store_root": "s"}))
        with contextlib.ExitStack() as stack:
            for p in self.card_mocks(None, {}):
                stack.enter_context(p)
            self.assertIs(lc.apply_effects(rec), rec)

    def test_a_fenced_send_that_cannot_run_is_uncertain_and_no_generation_is_nothing_to_stop(self):
        lc = self.launcher(run=raises)
        self.assertEqual(lc.fenced(GEN, "", "send-keys -t %0 /exit Enter"), tmux_pane_keys.UNCERTAIN)
        self.assertEqual(lc.stop_session({"generation": None}, "test"), "nothing to stop")

    def test_a_session_that_outlives_its_kill_is_not_stopped(self):
        lc = self.launcher()
        lc.tm = StubTm(present=True)
        with mock.patch.object(launcher, "SERVER_EXIT_S", 0.05), mock.patch.object(launcher, "CLAUDE_GRACE_S", 0.0), \
                mock.patch.object(lc, "fenced", side_effect=[0, 0]):
            out = lc.stop_session({"generation": GEN, "title_token": None}, "test")
        self.assertIn("still answers", out)

    def test_an_unconfirmed_send_fences_creation_and_is_never_stopped(self):
        lc = self.launcher()
        lc.tm = StubTm(present=True)
        with mock.patch.object(lc, "fenced", return_value=tmux_pane_keys.UNCERTAIN):
            out = lc.stop_session({"generation": GEN, "title_token": None}, "test")
        self.assertTrue(out.startswith("uncertain:"), out)
        self.assertIs(lc.record()["fence_uncertain"], True)

    def test_an_unknown_session_probe_never_turns_an_unsent_action_into_stopped(self):
        """has-session answering nothing (None) is not 'gone': a withheld or unconfirmed /exit stays
        that outcome, and the stop is not reported done."""
        lc = self.launcher()
        lc.tm = StubTm(present=None)
        for rc, word in ((tmux_pane_keys.WITHHELD, "cancelled:"), (tmux_pane_keys.UNCERTAIN, "uncertain:")):
            with mock.patch.object(lc, "fenced", return_value=rc):
                out = lc.stop_session({"generation": GEN, "title_token": None}, "test")
            self.assertNotEqual(out, "stopped", rc)
            self.assertTrue(out.startswith(word), (rc, out))
            self.assertEqual(lc.unsent(GEN, "", rc, "/exit")[:len(word)], word)

    def test_a_created_session_clears_the_deadline_and_an_adopted_stall_raises_its_card(self):
        lc = self.launcher()
        lc._with_record(lambda r: dict(r, deadline=5.0))
        with mock.patch.object(launcher, "classify", return_value=("create", "")), \
                mock.patch.object(lc, "create", return_value=(True, "")):
            rc, rec = lc.tick(lc.record())
        self.assertEqual((rc, rec["deadline"]), (None, None))
        raised = []
        lc._with_record(lambda r: dict(r, generation=GEN))
        stalled = dict(lc.record(), stall={"checks": "frozen"}, hitl=None)
        with mock.patch.object(launcher, "classify", return_value=("adopt", "")), \
                mock.patch.object(lc, "observe", lambda r: stalled), \
                mock.patch.object(lc, "raise_card", lambda r, kind, check: raised.append((kind, check)) or r), \
                mock.patch.object(lc, "apply_effects", lambda r: r):
            self.assertEqual(lc.tick(lc.record())[0], None)
        self.assertEqual(raised, [(card.KIND_STALL, "frozen")])

    def test_the_loop_retries_an_unregistered_store_and_runs_until_a_tick_exits(self):
        lc = self.launcher()
        with mock.patch.object(card, "register_install", side_effect=OSError(errno.EACCES, "denied")):
            self.assertEqual(lc._serve(), launcher.EXIT_RETRY)
        self.assertIn("cannot register with the bridge's card store", self.lines[-1])
        slept = []
        lc = self.launcher(sleep=slept.append)
        with mock.patch.object(card, "register_install"), mock.patch.object(lc, "apply_effects", lambda r: r), \
                mock.patch.object(lc, "tick", side_effect=[(None, {}), (cm.EXIT_HELD, {})]):
            self.assertEqual(lc._serve(), cm.EXIT_HELD)
        self.assertEqual(slept, [launcher.TICK_S])

    def test_the_launchd_shim_forwards_a_stop_to_its_live_child(self):
        class Child:
            sent = []

            def poll(self):
                return None

            def send_signal(self, sig):
                self.sent.append(sig)

            def wait(self):
                signal.raise_signal(signal.SIGTERM)
                return 0
        self.assertEqual(launcher.run_launchd(["x"], popen=lambda argv: Child()), 1)
        self.assertEqual(Child.sent, [signal.SIGTERM])


class HoldUnit(ConnectBase):
    """hold's refusals in-process: each is one line and exit 2, the shim reaped, nothing left running."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.cfg = cli.load_config()[0]
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def args(self):
        return type("A", (), {"generation": NONCE})()

    def pane_env(self, **over):
        env = {hold_mod.ENV_ROOT: str(self.work), hold_mod.ENV_CONFIG_DIR: str(cli.config_dir()),
               hold_mod.ENV_SESSION_ID: SESSION_ID}
        env.update(over)
        return env

    def test_the_pane_command_takes_only_a_hex_generation(self):
        with self.assertRaises(ValueError):
            hold_mod.pane_command(cli.LOCAL.command, "zz")

    def test_an_unreadable_or_incomplete_config_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hold_mod.main(self.args(), environ=self.pane_env(**{hold_mod.ENV_CONFIG_DIR: str(self.home / "none")})),
                             cm.EXIT_UNUSABLE)
        self.assertIn("the config could not be read", err.getvalue())
        self.configure(CLAUDE="")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hold_mod.main(self.args(), environ=self.pane_env()), cm.EXIT_UNUSABLE)
        self.assertIn("lacks CLAUDE or PLUGIN_DIR", err.getvalue())

    def test_a_shim_that_cannot_spawn_leaves_no_descriptor(self):
        before = set(os.listdir("/dev/fd"))
        with self.assertRaises(OSError):
            hold_mod.spawn_shim(["x"], {}, popen=raises)
        self.assertLessEqual(set(os.listdir("/dev/fd")), before | {str(n) for n in range(3)})

    def test_reaping_tolerates_a_closed_pipe_and_kills_a_shim_that_will_not_exit(self):
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        calls = []

        class Stuck:
            def wait(self, timeout=None):
                calls.append("wait")
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired("shim", timeout)
                return -9

            def kill(self):
                calls.append("kill")
        hold_mod.reap(Stuck(), w)
        self.assertEqual(calls, ["wait", "kill", "wait"])

    def held(self, popen=None, state=None, root=None):
        root = Path(root or self.work)
        fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        owner = type("Owner", (), {"root_fd": fd})()
        err = io.StringIO()
        with mock.patch.object(task_io, "acquire_lock", return_value=owner), \
                mock.patch.object(task_io, "release_lock", return_value=True), \
                mock.patch.object(task_io, "holder_record_bytes", return_value=b"{}"), \
                mock.patch.object(inbox, "recover_dead_generation", return_value=0), \
                contextlib.redirect_stderr(err):
            rc = hold_mod._held(str(root), self.cfg, SESSION_ID, NONCE, {}, popen or raises, state or cm.stop_state())
        return rc, err.getvalue()

    def test_each_refusal_under_the_held_root_is_one_line(self):
        with mock.patch.object(inbox, "prepare_root", return_value="the root is not prepared"):
            self.assertEqual(self.held(), (cm.EXIT_UNUSABLE, "hold: refusing to start: the root is not prepared\n"))
        bare = self.home / "bare"
        bare.mkdir()
        with mock.patch.object(inbox, "prepare_root", return_value=""):
            rc, err = self.held(root=bare)
        self.assertEqual((rc, "no tasks/ under the held root" in err), (cm.EXIT_UNUSABLE, True), err)
        (self.work / "tasks").mkdir(exist_ok=True)
        state = dict(cm.stop_state(), sig=signal.SIGTERM)
        self.assertEqual(self.held(state=state), (128 + signal.SIGTERM, "hold: stopped before the session started\n"))
        rc, err = self.held()
        self.assertEqual((rc, "the session could not be spawned" in err), (cm.EXIT_UNUSABLE, True), err)

    def shim(self):
        children = []

        def popen(argv, **kw):
            child = subprocess.Popen(["sleep", "0"])     # the real shim exits on the unwritten pipe's EOF
            children.append(child)
            return child
        self.addCleanup(lambda: [c.kill() or c.wait() for c in children if c.poll() is None])
        return popen, children

    def test_an_unreadable_shim_start_or_a_short_go_write_reaps_the_shim(self):
        (self.work / "tasks").mkdir(exist_ok=True)
        popen, children = self.shim()
        with mock.patch.object(task_io, "start_time", return_value=None):
            rc, err = self.held(popen=popen)
        self.assertEqual((rc, "no consumer: the shim's start time is unreadable" in err), (cm.EXIT_UNUSABLE, True), err)
        real = os.write
        tries = []

        def write(fd, data):
            if data == hold_mod.GO:
                tries.append(fd)
                if len(tries) == 1:
                    raise InterruptedError()
                return 0
            return real(fd, data)
        with mock.patch.object(task_io, "start_time", return_value="t"), \
                mock.patch.object(task_io, "attach_consumer", return_value=None), mock.patch.object(os, "write", write):
            rc, err = self.held(popen=popen)
        self.assertEqual((rc, "no consumer: short write" in err, len(tries)), (cm.EXIT_UNUSABLE, True, 2), err)
        self.assertEqual([c.returncode for c in children], [0, 0], "each shim reaped")

    def test_a_session_that_survives_the_kill_is_left_to_the_manager(self):
        state = dict(cm.stop_state(), sig=signal.SIGTERM)
        with mock.patch.object(cm, "_run_until_end", return_value=None), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hold_mod._supervise(object(), state), 128 + signal.SIGTERM)
        self.assertIn("did not die within the bound", err.getvalue())


class OldMainHitl(CardBase):
    """Rolling pins against the pre-claude-local src/hitl, checked in as a fixture."""

    def old_main(self, code):
        """Run `code` with the fixture's hitl package first on sys.path; its stdout. A missing fixture
        is an error, never a skip."""
        repo = PKG.parents[1]
        fixture = repo / "tests" / "fixtures" / "old_main" / "hitl_pre_local"
        self.assertTrue((fixture / "hitl" / "manager.py").is_file(), "the checked-in fixture")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path[:0] = [%r, %r]\n%s" % (str(fixture), str(repo / "src"), code)],
                           capture_output=True, text=True, env=isolated_child_env(PYTHONPATH=""))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_unmodified_main_keeps_the_nested_subject_on_re_save_and_never_sees_the_new_root(self):
        req = self.raise_card()
        self.assertEqual(self.accept(self.reply(req)), "applied")
        path = self.store_root / ("%s.json" % req.id)
        sub_before = json.loads(path.read_text())["requirement"]["subject"]
        out = self.old_main(
            "from hitl.manager import HitlManager, HitlStore\n"
            "m = HitlManager(HitlStore(%r)); r = m.get(%r)\n"
            "m.record_projection(r.id, r.revision + 5, '$old-main')\n"
            "print(HitlManager(HitlStore(%r)).active().__len__())\n"
            % (str(self.store_root), req.id, str(card.default_root(self.state_dir))))
        self.assertEqual(out.strip(), "0", "unmodified main's default store holds no claude-local card: it projects nothing")
        after = json.loads(path.read_text())["requirement"]
        self.assertEqual(json.dumps(after["subject"], sort_keys=True), json.dumps(sub_before, sort_keys=True),
                         "saga key, phase, room and discriminator survive an old writer's re-save")
        got = self.load(req.id)
        self.assertEqual((card.local(got)["room"], card.local(got)["discriminator"], card.local(got)["effect_phase"]),
                         (ROOM, card.KIND_STALL, "STOPPING"), "the new reader derives them from the subject")
        self.lc.apply_effects(self.lc.record())
        self.assertEqual(len(self.stops), 1, "reconciles to the same requirement")


class FolderTrust(unittest.TestCase):
    """The CLI's own trust record for exactly the held inbox, under the CLI's own lock, nothing else changed."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="trust")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.ccd = self.home / "ccd"
        self.inbox = self.home / "work" / "tasks"
        self.inbox.mkdir(parents=True)

    def data(self, path=None):
        return json.loads((path or self.ccd / ".claude.json").read_text())

    def test_records_exactly_the_inbox_idempotently_and_keeps_everything_else(self):
        self.ccd.mkdir()
        conf = self.ccd / ".claude.json"
        other = {"hasCompletedOnboarding": True, "oauthAccount": {"x": 1}, "numStartups": 7,
                 "projects": {str(self.home / "work"): {"allowedTools": ["Read"]},
                              str(self.inbox): {"allowedTools": ["Bash"], "hasTrustDialogAccepted": False}}}
        conf.write_text(json.dumps(other))
        conf.chmod(0o600)
        self.assertEqual(trust.record(str(self.ccd), str(self.inbox)), "")
        got = self.data()
        self.assertEqual({k: v for k, v in got.items() if k != "projects"}, {k: v for k, v in other.items() if k != "projects"})
        self.assertEqual(got["projects"][str(self.home / "work")], {"allowedTools": ["Read"]}, "never the parent")
        self.assertEqual(got["projects"][str(self.inbox)], {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True})
        self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o600)
        self.assertFalse((self.ccd / ".claude.json.lock").exists(), "the CLI's lock is released")
        before = (conf.stat().st_ino, conf.stat().st_mtime_ns)
        self.assertEqual(trust.record(str(self.ccd), str(self.inbox)), "")
        self.assertEqual((conf.stat().st_ino, conf.stat().st_mtime_ns), before, "already trusted: nothing is written")
        self.assertEqual(sorted(p.name for p in self.ccd.iterdir()), [".claude.json"], "no temp left behind")

    def test_the_key_is_the_resolved_path_and_a_missing_file_or_dir_is_created(self):
        link = self.home / "link"
        os.symlink(self.home / "work", link)
        self.assertEqual(trust.record(str(self.ccd), str(link / "tasks")), "")
        self.assertEqual(list(self.data()["projects"]), [str(self.inbox)], "the cwd hold fchdirs to, resolved")
        self.assertEqual(stat.S_IMODE((self.ccd / ".claude.json").stat().st_mode), 0o600)
        self.assertTrue(trust.trusted(str(self.ccd / ".claude.json"), trust.project_key(str(self.inbox))))

    def test_the_cli_config_file_is_resolved_as_the_cli_does(self):
        self.assertEqual(trust.config_file("", str(self.home)), str(self.home / ".claude.json"), "no config dir: HOME")
        self.assertEqual(trust.config_file(str(self.ccd), str(self.home)), str(self.ccd / ".claude.json"))
        self.ccd.mkdir()
        (self.ccd / ".config.json").write_text("{}")
        self.assertEqual(trust.config_file(str(self.ccd)), str(self.ccd / ".config.json"), "a non-empty legacy file wins")
        (self.ccd / ".config.json").write_text("")
        (self.ccd / ".claude.json").write_text("{}")
        self.assertEqual(trust.config_file(str(self.ccd)), str(self.ccd / ".claude.json"))
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".config.json").write_text('{"a": 1}')
        self.assertEqual(trust.config_file("", str(self.home)), str(self.home / ".claude" / ".config.json"))

    def test_a_symlinked_config_is_written_through_and_stays_a_link(self):
        self.ccd.mkdir()
        real = self.home / "dotfiles.json"
        real.write_text("{}")
        os.symlink(real, self.ccd / ".claude.json")
        self.assertEqual(trust.record(str(self.ccd), str(self.inbox)), "")
        self.assertTrue(os.path.islink(self.ccd / ".claude.json"))
        self.assertIn(str(self.inbox), self.data(real)["projects"])

    def test_it_fails_closed_with_one_line_and_leaves_the_file_as_it_was(self):
        self.ccd.mkdir()
        conf = self.ccd / ".claude.json"
        for text, why in (("{not json", "Expecting property name"), ("[1]", "not a JSON object"),
                          ('{"projects": []}', "projects entry is not an object")):
            with self.subTest(text=text):
                conf.write_text(text)
                line = trust.record(str(self.ccd), str(self.inbox))
                self.assertTrue(line.startswith("cannot record Claude Code's folder trust for %s in %s (" % (self.inbox, conf)), line)
                self.assertIn(why, line)
                self.assertTrue(line.endswith("the session would stop at the trust dialog"))
                self.assertEqual(conf.read_text(), text)
        conf.write_text("{}")
        (self.ccd / ".claude.json.lock").mkdir()
        line = trust.record(str(self.ccd), str(self.inbox), wait_s=0.1)
        self.assertIn("the CLI's config lock %s stayed held" % (self.ccd / ".claude.json.lock"), line)
        self.assertTrue((self.ccd / ".claude.json.lock").is_dir(), "another holder's lock is never broken")
        self.assertEqual(conf.read_text(), "{}")
        os.rmdir(self.ccd / ".claude.json.lock")
        with mock.patch.object(trust.os, "replace", side_effect=OSError(28, "No space left on device")):
            self.assertIn("No space left on device", trust.record(str(self.ccd), str(self.inbox)))
        self.assertEqual(sorted(p.name for p in self.ccd.iterdir()), [".claude.json"], "the temp is removed, the lock released")
        with mock.patch.object(trust, "trusted", side_effect=[False, False]):
            self.assertIn("did not read back", trust.record(str(self.ccd), str(self.inbox)))

    def test_a_held_lock_released_in_time_is_waited_for(self):
        self.ccd.mkdir()
        lock = self.ccd / ".claude.json.lock"
        lock.mkdir()
        t = threading.Timer(0.2, lambda: os.rmdir(lock))
        t.start()
        self.addCleanup(t.cancel)
        self.assertEqual(trust.record(str(self.ccd), str(self.inbox), wait_s=5.0), "")

    def test_the_startup_dialogs_read_from_a_capture(self):
        self.assertEqual(trust.dialog(TRUST_PANE), "folder-trust")
        self.assertEqual(trust.dialog(BYPASS_PANE), "bypass-permissions")
        self.assertEqual(trust.dialog(TRUST_PANE.replace("❯ No, exit", "  No, exit").replace("   Yes, I", " ❯ Yes, I")),
                         "folder-trust", "the cursor on either line")
        for text in (READY_PANE, None, "", "we said Yes, I trust this folder in a chat line\n",
                     "  Yes, I trust this folder\n", "  ❯ No, exit\n"):
            self.assertIsNone(trust.dialog(text), text)
        self.assertEqual(detector.abnormal_names(TRUST_PANE), ["dialog:folder-trust"])
        obs = detector.derive({}, TRUST_PANE, {"inflight": {}}, 0.0, abnormal_min_s=5)
        self.assertEqual(obs["dialog"], "folder-trust")
        obs = detector.derive(obs, TRUST_PANE, {"inflight": {}}, 5.0, abnormal_min_s=5)
        self.assertEqual(obs["stall"]["reason"], "abnormal:dialog:folder-trust", "an unanswered dialog raises the card")


class StartupDialogAndRefusals(ConnectBase):
    """The launcher records trust before the session, names a pane at a startup dialog, and stops for good
    on a root it may never serve; verify fails on that pane; install refuses that root up front."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.cfg = cli.load_config()[0]
        self.lines = []

    def launcher(self):
        return launcher.Launcher(self.cfg, cli.config_dir(), log=self.lines.append, sleep=lambda s: None)

    def trusted(self):
        return trust.trusted(trust.config_file(self.cfg["CLAUDE_CONFIG_DIR"]), trust.project_key(str(self.inbox)))

    def test_creation_records_the_inbox_trust_first_and_refuses_when_it_cannot(self):
        lc = self.launcher()
        clear = mock.patch.object(tmux_pane_keys, "fence_status", return_value="clear")
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(trust, "record", return_value="cannot record x"), \
                mock.patch.object(launcher, "create_session") as made:
            self.assertEqual(lc.create({}), (False, "cannot record x"))
        made.assert_not_called()
        with clear, mock.patch.object(task_io, "holder_consumer", return_value=None), \
                mock.patch.object(launcher, "create_session", return_value=(None, "tmux new-session failed: x")):
            self.assertEqual(lc.create({}), (False, "tmux new-session failed: x"))
        self.assertTrue(self.trusted(), "recorded before the session was made")
        self.assertEqual(lc.refused, "")

    def test_a_layout_refusal_is_terminal_and_an_unreadable_root_is_retried(self):
        (self.work / "stray").write_text("x")
        lc = self.launcher()
        with mock.patch.object(tmux_pane_keys, "fence_status", return_value="clear"), \
                mock.patch.object(launcher, "classify", return_value=("create", "")):
            rc, _ = lc.tick(lc.record())
            self.assertEqual(rc, cm.EXIT_HELD, "the manager stops: launchd's 0, systemd's RestartPreventExitStatus")
            self.assertEqual(launcher.launchd_exit(rc), 0)
            self.assertIn("is not a fresh work dir for this adapter (it holds stray)", self.lines[0])
            self.assertEqual(self.lines[1], f"stopping: the work root needs attention, then run {cli.COMMAND} install again")
            lc = self.launcher()
            with mock.patch.object(inbox, "prepare_root", return_value="refusing to start: cannot check /w: EIO"):
                self.assertEqual(lc.tick(lc.record())[0], launcher.EXIT_RETRY)
            with mock.patch.object(inbox, "prepare_root", return_value="/w is not a prepared work dir (x)"):
                self.assertEqual(lc.tick(lc.record())[0], launcher.EXIT_RETRY)
        self.assertFalse(self.trusted(), "nothing recorded for a root it refused")

    def test_a_root_the_bridge_wrote_into_still_gets_a_new_session(self):
        from ag2_sparrow import workspace_lock
        from ag2_sparrow.hitl.manager import HitlStore, default_store
        self.assertEqual(inbox.prepare_root(str(self.work)), "", "the first session's launcher prepared it")
        workspace_lock.acquire("gateway-bridge", self.work)
        HitlStore(default_store(self.work))
        (self.work / "logs").mkdir()
        (self.work / "logs" / "gateway-bridge.log").write_text("x\n")
        lc = self.launcher()
        with mock.patch.object(tmux_pane_keys, "fence_status", return_value="clear"), \
                mock.patch.object(launcher, "create_session", return_value=(None, "reached the session step")):
            self.assertEqual(lc.create({}), (False, "reached the session step"))
        self.assertEqual(lc.refused, "")

    def test_a_pane_at_a_startup_dialog_is_logged_once_and_its_answer_too(self):
        lc = self.launcher()
        lc.store_root, lc.tm = None, StubTm()
        rec = lc._with_record(lambda r: dict(r, generation=GEN))
        frames = iter([TRUST_PANE, TRUST_PANE, READY_PANE])
        lc.tm.capture = lambda pane_id: next(frames)
        for _ in range(3):
            rec = lc.observe(rec)
        self.assertEqual(self.lines, [f"the session waits at Claude Code's folder-trust dialog; answer it in the pane "
                                      f"({cli.COMMAND} attach)", "the session is past its startup dialog"])

    def test_verify_fails_on_a_pane_waiting_at_a_dialog_and_reports_the_trust_record(self):
        runner = self.runner()
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
        self.assertIn(f"INFO folder trust: {self.inbox} not recorded (the launcher records it before it creates the session)", out)
        self.assertNotIn("FAIL session", out)
        self.assertEqual(trust.record(self.cfg["CLAUDE_CONFIG_DIR"], str(self.inbox)), "")
        records.save_supervisor(str(self.work), dict(records.empty_supervisor(), generation=GEN))
        with mock.patch.object(sm, "_run", side_effect=runner), \
                mock.patch.object(launcher.Tmux, "has_session", return_value=True), \
                mock.patch.object(launcher.Tmux, "capture", return_value=TRUST_PANE):
            rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn(f"INFO folder trust: {self.inbox} recorded", out)
        self.assertIn(f"FAIL session: the pane waits at Claude Code's folder-trust dialog, so no task is answered; run "
                      f"{cli.COMMAND} attach and answer it", out)
        for present, pane in ((True, READY_PANE), (False, TRUST_PANE), (None, TRUST_PANE)):
            with mock.patch.object(launcher.Tmux, "has_session", return_value=present), \
                    mock.patch.object(launcher.Tmux, "capture", return_value=pane):
                self.assertIsNone(cli.pane_dialog(records.load_supervisor(str(self.work))))
        self.assertIsNone(cli.pane_dialog({}))
        Path(trust.config_file(self.cfg["CLAUDE_CONFIG_DIR"])).write_text("[1]")
        with mock.patch.object(sm, "_run", side_effect=runner):
            out = self.main("verify")[1]
        self.assertIn(f"INFO folder trust: {self.inbox} unreadable (it is not a JSON object)", out)

    def test_install_refuses_a_root_the_launcher_would_refuse_and_applies_nothing(self):
        (self.work / "logs").mkdir()
        (self.work / "logs" / "other.log").write_text("x")
        cli.config_path().unlink()
        rc, out, err = self.install_with(self.runner())
        self.assertEqual(rc, 1)
        self.assertIn(f"error: refusing to start: {self.work} is not a fresh work dir for this adapter (it holds "
                      f"logs/other.log); this adapter serves only a new, dedicated work dir; nothing was applied", err)
        self.assertFalse(cli.config_path().exists() or cli.service_path().exists(), "nothing written")
        self.assertFalse((self.work / "state").exists(), "the check writes nothing either")
        (self.work / "logs" / "other.log").rename(self.work / "logs" / "gateway-bridge.log")
        self.assertEqual(self.install_with(self.runner())[0], 0, "the bridge's own log is not foreign")

    def test_an_unconfirmed_start_names_where_the_launchers_refusal_is(self):
        cli.config_path().unlink()
        rc, _, err = self.install_with(self.runner(root=False))
        self.assertEqual(rc, 1)
        self.assertIn(f"its refusal is in journalctl --user -u {cli.SERVICE_NAME}; run", err)
        with mock.patch.object(cm, "is_macos", return_value=True):
            self.assertEqual(cm.launcher_log(cli.LOCAL), str(cm.log_path(cli.LOCAL)))

    def test_uninstall_names_the_token_file_only_when_it_exists(self):
        self.assertEqual(cm.kept_line(cli.LOCAL), f"kept: {self.relay} (Sparrow's token) and the Claude login")
        self.relay.unlink()
        self.assertEqual(cm.kept_line(cli.LOCAL), "kept: the Claude login")
        cli.config_path().unlink()
        with mock.patch.object(sm, "_run", side_effect=Runner()):
            rc, out, _ = self.main("uninstall")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to remove", out)
        self.assertIn("kept: the Claude login", out)
        self.assertNotIn("relay.env", out)

    def test_occupancy_names_hold_and_its_attached_claude(self):
        with mock.patch.object(task_io, "occupancy", return_value="held"), \
                mock.patch.object(task_io, "lock_holder", return_value={"pid": 9, "owner_pid": 7}), \
                mock.patch.object(task_io, "holder_consumer", return_value={"pid": 9, "live": True}), \
                mock.patch.object(cm, "manager_detail", return_value="active/running"):
            self.assertEqual(cli.occupancy_line(str(self.work)), "held by hold pid 7, its claude pid 9 (manager active/running)")


if __name__ == "__main__":
    unittest.main()
