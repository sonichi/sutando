#!/usr/bin/env python3
"""Several agents per host, continued: recovery bound to the runtime's identity rule, every printed command from
one formatter, the printed repair run verbatim, the bridge waiting for its session, and selection."""
from __future__ import annotations

import contextlib
import json
import io
import os
import re
import shlex
import subprocess
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_local_multi_agent as lm  # noqa: E402
from test_local_multi_agent import (  # noqa: E402
    ALPHA, BETA, CL, CLIS, CX, MultiBase, PAIRS, cb, claude_cli, cm, codex_cli, dash_key, expand, fx, job, per_manager, sm, without_root_var,
)


PKG_SRC = Path(__file__).resolve().parents[1] / "ag2_sparrow"
# A string that names one of the adapters' commands followed by a verb is a command someone can copy; every such
# line must come from cm.job_command. These are the exceptions, each a provenance mark or an internal argv.
EMITTER_RE = re.compile(r"(\{[^{}]*command[^{}]*\}|\{COMMAND\}|%s|(?:command|COMMAND)\s*\+\s*[\"']\s*|"
                        r"ag2-sparrow-(?:claude|codex)-local) (?:install|uninstall|verify|attach|stop|debug-run|run|"
                        r"connect|list|hold|inbox)\b")
EMITTERS_ALLOWED = {
    ("connect_common.py", 'return f"# Written by {self.writer or self.command} install;'): "the unit's provenance marker",
    ("connect_common.py", 'return f"<!-- Written by {self.writer or self.command} install;'): "the plist's marker",
    ("connect_common.py", 'written_by=f"{shape.command} install")'): "the declaration's provenance",
    ("connect_common.py", 'return (f"{shape.command} run is the manager\'s entrypoint'): "names the refused verb",
    ("connect_bridge.py", 'as {command} connect writes it"'): "names the seat's writer",
    ("connect_bridge.py", 'bridge_home(root) / DECLARATION, written_by=f"{command} install", host=mode'): "the "
    "declaration's provenance",
    ("connect_bridge.py", '                  written_by=f"{command} install")'): "the declaration's provenance (_withdraw)",
    ("connect_bridge.py", 'remove_bridge_declaration(path, written_by=f"{command} install")'): "the declaration's provenance",
    ("adapters/claude_code/hold.py", '"""`ag2-sparrow-claude-local hold --generation'): "docstring",
    ("adapters/claude_code/hold.py", 'return "%s hold --generation=%s --" % (command, nonce)'): "the pane's own argv",
    ("adapters/claude_code/inbox.py", '"""`ag2-sparrow-claude-local inbox <verb>`'): "docstring",
    ("adapters/claude_code/launcher.py", "START_COMMAND_RE = re.compile(r'^\"?ag2-sparrow-claude-local hold"): "argv check",
}


@expand
class Round6(MultiBase):
    sparrowd = lm.Round5.sparrowd

    def test_no_command_line_is_formatted_outside_the_one_formatter(self):
        """Every command a line can print comes from cm.job_command (so it carries the job's selector): no other
        string in the adapters or the shared modules formats one, beyond the provenance marks listed."""
        files = [PKG_SRC / "connect_common.py", PKG_SRC / "connect_bridge.py", PKG_SRC / "adapters/codex/connect_cli.py",
                 *sorted((PKG_SRC / "adapters/claude_code").glob("*.py"))]
        found = set()
        for path in files:
            rel = str(path.relative_to(PKG_SRC))
            for line in path.read_text().splitlines():
                if EMITTER_RE.search(line) and not line.strip().startswith("#"):
                    match = [k for k in EMITTERS_ALLOWED if k[0] == rel and k[1] in line]
                    self.assertTrue(match, f"{rel}: a command formatted outside cm.job_command: {line.strip()}")
                    found.update(match)
        self.assertEqual(found, set(EMITTERS_ALLOWED), "every exception is still there (drop the stale ones)")

    def tamper_bridge_agent(self, shape, agent):
        """The job's own marked bridge unit, its agent changed: the marker stays, the identity no longer holds."""
        unit = cm.supervisor_path(cb.bridge_shape(shape))
        text = unit.read_text()
        old = cm.unit_environ(text)["AGENT_MXID"]
        unit.write_text(text.replace(old, agent))

    def _misbound(self, adapter):
        """A fixed job's bridge unit bound to B's root but naming agent A (as the bridge runtime would refuse):
        nothing is recovered, `--agent A` never reaches the fixed job, and its own uninstall refuses with the
        manual path instead of tearing down a bridge it cannot prove its own."""
        single = job(adapter, "")
        self.host.add(single, self.root_of(BETA))
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.installed(adapter, BETA)
        self.tamper_bridge_agent(single, ALPHA)
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                self.launcher_pid(single))
        self.assertEqual((cm.recovered_config(single), cm.recorded_agent(single)), ({}, ""))
        rc, out, err = self.run_cli(adapter, "uninstall", "--agent", ALPHA)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                          self.launcher_pid(single)), kept, "--agent A never reaches B's job")
        rc, out, err = self.run_cli(adapter, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {cm.supervisor_path(cb.bridge_shape(single))}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), cm.supervisor_path(cb.bridge_shape(single)).read_bytes(),
                          self.launcher_pid(single)), kept, "and its own uninstall refuses before it moves anything")

    test_a_bridge_unit_naming_another_agent_than_its_root_is_never_recovered = per_manager(_misbound)

    def test_a_declaration_naming_another_agent_than_its_root_is_never_recovered(self):
        """Under sparrowd, the fixed job's declaration beside its own root B, edited to name agent A: the same
        identity rule as the bridge runtime refuses it, so nothing is recovered, `--agent A` never reaches B's job,
        and B's uninstall refuses naming the declaration's manual path."""
        argv = self.sparrowd()
        single = job(CL.name, "")
        decl = cb.bridge_home(self.root_of(BETA)) / cb.DECLARATION
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                self.host.add(single, self.root_of(BETA))
                os.environ["AGENT_MXID"] = BETA
                with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
                    self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
                body = json.loads(decl.read_text())
                body["env"]["AGENT_MXID"] = ALPHA
                decl.write_text(json.dumps(body))
                cm.config_path(single).unlink()
                kept = (cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single))
                self.assertEqual((cm.recovered_config(single), cm.recorded_agent(single)), ({}, ""))
                self.run_cli(CL.name, "uninstall", "--agent", ALPHA)
                rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"rm {decl}", err)
                self.assertEqual((cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single)),
                                 kept)
                decl.unlink()
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--single")[0], 0)

    def test_a_declaration_serving_another_root_is_never_recovered(self):
        """The fixed job's declaration beside its own root B, edited to feed another root's tasks: it serves not the
        root the job's unit records, so nothing is recovered and B's uninstall refuses naming it."""
        argv = self.sparrowd()
        single = job(CL.name, "")
        decl = cb.bridge_home(self.root_of(BETA)) / cb.DECLARATION
        self.host.add(single, self.root_of(BETA))
        os.environ["AGENT_MXID"] = BETA
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        body = json.loads(decl.read_text())
        body["env"]["AGENT_CONNECT_TASK_DIR"] = str(self.root_of(ALPHA) / "tasks")
        decl.write_text(json.dumps(body))
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single))
        self.assertEqual(cm.recovered_config(single), {})
        rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {decl}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), decl.read_bytes(), self.launcher_pid(single)), kept)

    def test_an_unreadable_or_unbound_declaration_fails_closed_and_names_its_manual_path(self):
        """An agent's own job reduced to an unreadable declaration, and a legacy fixed job whose declaration its
        unit cannot vouch for: uninstall refuses (non-zero), names the declaration and its `rm`, and moves nothing."""
        argv = self.sparrowd()
        a, single = job(CL.name, ALPHA), job(CL.name, "")
        a_decl, b_decl = (cb.bridge_home(self.root_of(x)) / cb.DECLARATION for x in (ALPHA, BETA))
        os.environ["AGENT_MXID"] = ALPHA
        self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        gone = self.host.launchers[a.job]
        gone.stop_session()
        gone.enabled = gone.registered = gone.loaded = False
        for path in (cm.config_path(a), cm.supervisor_path(a)):
            path.unlink()
        a_decl.chmod(0)
        self.addCleanup(lambda: a_decl.exists() and a_decl.chmod(0o644))
        rc, out, err = self.run_cli(CL.name, "uninstall", "--key", a.agent)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"the bridge declaration at {a_decl} cannot be used", err)
        self.assertIn(f"rm {a_decl}", err)
        self.assertTrue(os.path.lexists(a_decl))
        a_decl.chmod(0o644)
        a_decl.unlink()
        self.host.add(single, self.root_of(BETA))
        os.environ["AGENT_MXID"] = BETA
        with mock.patch.object(cm, "job_for", side_effect=lambda shape, agent: cm.for_key(shape, "")):
            self.assertEqual(self.run_cli(CL.name, *argv)[0], 0)
        without_root_var(cm.supervisor_path(single))
        cm.config_path(single).unlink()
        kept = (cm.supervisor_path(single).read_bytes(), b_decl.read_bytes(), self.launcher_pid(single))
        rc, out, err = self.run_cli(CL.name, "uninstall", "--single")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"rm {b_decl}", err)
        self.assertEqual((cm.supervisor_path(single).read_bytes(), b_decl.read_bytes(), self.launcher_pid(single)), kept,
                         "the legacy fixed job's unit is not removed while its declaration is left unnamed")
        b_decl.unlink()
        self.assertEqual(self.run_cli(CL.name, "uninstall", "--single")[0], 0)


@expand
class Round7(MultiBase):
    def stalled(self, shape):
        """The manager accepts the job's start, but no launcher comes to take its root."""
        runner = self.host.launchers[shape.job]
        runner.stop_session()
        runner.root = None
        return runner

    def _no_bridge_without_a_session(self, adapter):
        """A session that never holds its root gets no bridge: a fresh install never starts one and leaves no unit;
        a reinstall leaves the bridge it stopped stopped, its unit removed; the printed repair, run verbatim, wires it."""
        a = job(adapter, ALPHA)
        bridge, unit = self.host.bridges[a.job], cm.supervisor_path(cb.bridge_shape(a))
        runner = self.stalled(a)
        rc, out, err = self.install(adapter, ALPHA)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("did not acquire", err)
        self.assertFalse(bridge.started(), "a fresh install never starts the bridge")
        self.assertFalse(unit.exists(), err)
        runner.root = str(self.root_of(ALPHA))
        self.installed(adapter, ALPHA)
        self.assertTrue(bridge.active)
        self.stalled(a)
        rc, out, err = self.install(adapter, ALPHA)
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(bridge.active, "the reinstall leaves the bridge it stopped stopped")
        self.assertFalse(unit.exists(), err)
        repair = cm.job_command(a, "install")
        self.assertIn(repair, err)
        runner.root = str(self.root_of(ALPHA))
        rc, out, err = self.run_cli(adapter, *shlex.split(repair)[1:])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(bridge.active and unit.exists(), out)
    test_a_session_that_never_holds_its_root_gets_no_bridge = per_manager(_no_bridge_without_a_session)

    def _no_declaration_without_a_session(self, adapter):
        """Under sparrowd, likewise: a fresh install declares no bridge; a reinstall withdraws the declaration, so
        the supervisor stops that bridge on its next scan; the repair, run verbatim, declares it again."""
        argv = lm.Round5.sparrowd(self) if adapter == CL.name else \
            ["install", "--codex", str(self.codex), "--codex-home", str(self.codex_home)]
        lm.Round5.sparrowd(self)
        root = self.root_of(ALPHA)
        self.assertEqual(cb._sources.write_seat_credentials(     # the seat connect writes: sparrowd's only token
            cb.bridge_home(root), agent_id=ALPHA, instance="c" * 32, tier="owner",
            relay_url="https://relay.backend.example/relay", bearer="bearer-SEAT0001"), "")
        argv += ["--token-env-file", cb.seat_file(root)]
        a = job(adapter, ALPHA)
        decl = cb.bridge_home(self.root_of(ALPHA)) / cb.DECLARATION
        runner = self.stalled(a)
        rc, out, err = self.run_cli(adapter, *argv)
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(decl.exists(), out + err)
        runner.root = str(self.root_of(ALPHA))
        self.assertEqual(self.run_cli(adapter, *argv)[0], 0)
        self.assertTrue(decl.exists())
        self.stalled(a)
        rc, out, err = self.run_cli(adapter, *argv)
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(decl.exists(), out + err)
        self.assertIn("was withdrawn", err)
        runner.root = str(self.root_of(ALPHA))
        rc, out, err = self.run_cli(adapter, *shlex.split(cm.job_command(a, "install"))[1:])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(decl.exists())
    test_under_sparrowd_a_session_that_never_holds_its_root_declares_no_bridge = \
        per_manager(_no_declaration_without_a_session)

    def test_under_sparrowd_the_printed_repair_names_the_agent_by_its_selector_alone(self):
        """Under sparrowd, a job installed with no token file of its own: verify's repair line is `install --agent`,
        which, run verbatim with no AGENT_MXID in sight, takes the agent from the selector and keeps its flags."""
        argv = lm.Round5.sparrowd(self)
        b = job(CL.name, BETA)
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                with mock.patch.dict(os.environ, {"AGENT_MXID": BETA}):
                    rc, out, err = self.run_cli(CL.name, *argv, "--allow-collaborator")
                self.assertEqual(rc, 0, out + err)
                conf = cm.config_path(b)
                conf.write_text(re.sub(r"(?m)^SESSION_ID=.*$", "SESSION_ID=not-a-uuid", conf.read_text()))
                repair = cm.job_command(b, "install")
                self.assertIn(repair, "".join(self.run_cli(CL.name, "verify", "--agent", BETA)[1:]))
                rc, out, err = self.run_cli(CL.name, *shlex.split(repair)[1:])
                self.assertEqual(rc, 0, out + err)
                after = cm.read_config(conf)
                self.assertRegex(after["SESSION_ID"], claude_cli.SESSION_ID_RE)
                self.assertEqual(after[claude_cli.inbox.ALLOW_COLLABORATOR], "1")
                self.assertTrue((cb.bridge_home(self.root_of(BETA)) / cb.DECLARATION).exists())
                self.assertEqual(self.run_cli(CL.name, "uninstall", "--agent", BETA)[0], 0)

    def test_an_option_the_repair_line_gives_wins_over_the_recorded_one(self):
        """`install <selector>` takes each recorded option the command did not give: one given (even at its default,
        as `--opt=value`, or by argparse's unique prefix) wins; one not given keeps the recorded value."""
        b = job(CX.name, BETA)
        self.installed(CX.name, BETA, "--sandbox", "workspace-write", "--allow-non-owner")
        for given in (["--sandbox", "read-only"], ["--sandbox=read-only"], ["--sand", "read-only"]):
            with self.subTest(given=given):
                rc, out, err = self.run_cli(CX.name, "install", "--agent", BETA, *given)
                self.assertEqual(rc, 0, out + err)
                cfg = cm.read_config(cm.config_path(b))
                self.assertEqual((cfg["SANDBOX"], cfg["ALLOW_NON_OWNER"]), ("read-only", "1"))
                self.assertEqual(cfg["TOKEN_ENV_FILE"], str(self.tokens[BETA]))
                self.installed(CX.name, BETA, "--sandbox", "workspace-write", "--allow-non-owner")

    def test_a_reinstall_mints_a_new_session_id_for_a_malformed_one(self):
        """A kept session id the launcher refuses is replaced, not kept: the reinstall mints a valid one, keeps the
        flags it was installed with, and its session and bridge run; the other agent's job is not moved."""
        for mac in (False, True):
            with self.subTest(mac=mac):
                self.mac = mac
                a, b = job(CL.name, ALPHA), job(CL.name, BETA)
                self.installed(CL.name, ALPHA)
                self.installed(CL.name, BETA, "--allow-collaborator")
                conf, others = cm.config_path(b), self.files(a)
                before = cm.read_config(conf)
                conf.write_text(re.sub(r"(?m)^SESSION_ID=.*$", "SESSION_ID=not-a-uuid", conf.read_text()))
                rc, out, err = self.run_cli(CL.name, "install", "--agent", BETA)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("malformed", out)
                after = cm.read_config(conf)
                self.assertRegex(after["SESSION_ID"], claude_cli.SESSION_ID_RE)
                self.assertNotEqual(after["SESSION_ID"], before["SESSION_ID"])
                self.assertEqual({k: v for k, v in after.items() if k != "SESSION_ID"},
                                 {k: v for k, v in before.items() if k != "SESSION_ID"})
                self.assertTrue(self.launcher_pid(b) and self.host.bridges[b.job].active)
                self.assertEqual(self.files(a), others)
                for agent in (ALPHA, BETA):
                    self.assertEqual(self.run_cli(CL.name, "uninstall", "--agent", agent)[0], 0)


class Selection(MultiBase):
    def test_a_job_whose_config_records_an_agent_of_another_key_is_never_acted_on(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                a = job(adapter, ALPHA)
                cm._write(cm.config_path(a), cm.render_config(a, {"AGENT_ID": BETA, "ROOT": str(self.root_of(BETA)),
                                                                  "CODEX": str(self.codex), "CLAUDE": str(self.claude),
                                                                  "SESSION_ID": fx.SESSION_ID}), 0o600)
                before = cm.config_path(a).read_bytes()
                for argv in (["verify"], ["uninstall"], ["verify", "--work", str(self.root_of(BETA))]):
                    rc, out, err = self.run_cli(adapter, *argv)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"records {BETA}, whose job is not {cm.job_name(a)}", err)
                with mock.patch.dict(os.environ, {cm.AGENT_VAR: a.agent}):
                    rc, out, err = self.run_cli(adapter, "run")
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn("whose job is not", err)
                self.assertEqual(cm.config_path(a).read_bytes(), before)
                self.assertEqual(self.host.calls_naming(a), [])
                cm.config_path(a).unlink()

    def test_a_64_character_key_keeps_the_tmux_socket_short(self):
        """The socket of an agent's own job sits in a short run dir named by its key's digest, so a key as long
        as a root segment gets fits the platform's socket path limit; a single job keeps its socket in place."""
        socks = []
        for agent in ("@" + "a" * 30 + ":" + "b" * 33, "@" + "a" * 30 + ":" + "b" * 32 + "c"):
            shape = job(CL.name, agent)
            self.assertEqual(len(shape.agent), 64)
            sock = claude_cli.launcher.socket_path(cm.config_dir(shape))
            self.assertEqual(sock.parent.parent, cm.config_root() / cm.RUN_DIR)
            self.assertLessEqual(len(str(sock)) - len(str(self.home)), 64, sock)
            socks.append(sock)
        self.assertNotEqual(*socks)
        self.assertEqual(claude_cli.launcher.socket_path(cm.config_dir(CL)), cm.config_dir(CL) / "claude-local.sock")
        self.assertEqual(claude_cli.launcher.run_dir_problem(cm.config_dir(job(CL.name, ALPHA))), "")
        self.assertEqual(oct(os.stat(cm.run_dir(cm.config_dir(job(CL.name, ALPHA)))).st_mode & 0o777), "0o700")

    def test_another_job_of_this_adapter_whose_config_names_the_agent_refuses_its_install(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                other = job(adapter, BETA)     # a config moved by hand under another agent's key
                cm._write(cm.config_path(other), cm.render_config(other, {
                    "AGENT_ID": ALPHA, "BRIDGE": "sparrowd", "ROOT": str(self.root_of(ALPHA))}), 0o600)
                rc, out, err = self.install(adapter, ALPHA)
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn(f"{ALPHA} already has a bridge, run by {adapter} (declared for sparrowd)", err)
                self.assertFalse(cm.config_path(job(adapter, ALPHA)).exists())
                cm.config_path(other).unlink()

    def test_the_colliding_pairs_get_two_keys_and_a_digest_key_is_never_a_literal_one(self):
        for first, second in PAIRS:
            self.assertEqual(dash_key(first), dash_key(second), "the pair the dash form gave one key")
            self.assertNotEqual(cm.agent_key(first), cm.agent_key(second))
        self.assertEqual(cm.agent_key(PAIRS[0][0]), "ab_cdefghijklmnopqrstuvwxyz.example")
        digest_form = re.compile(r"__[0-9a-f]{10}$")
        ids = [f"@{loc}:{srv}" for loc in ("a", "ab", "x-y", "a.b", "q" * 20, "A", "Mixed", "a_b", "Q" * 20)
               for srv in ("s", "ab.cd", "x-1.example", "y" * 20, "S.Y", "z" * 60)]
        for agent in ids:
            segment, key = cm.agent_identity.root_segment(agent), cm.agent_key(agent)
            literal = key == segment
            self.assertEqual(literal, not digest_form.search(key), f"{agent} -> {key}")
            self.assertTrue("__" not in segment or re.search(r"__[0-9a-f]{12}$", segment),
                            "only root_segment's own digest form holds the separator, followed by 12 hex")
        self.assertEqual(len({cm.agent_key(a) for a in ids}), len(set(ids)), "distinct ids, distinct keys")

    def test_each_colliding_pair_installs_verifies_and_uninstalls_side_by_side(self):
        for (first, second), adapter in zip(PAIRS, CLIS):
            with self.subTest(pair=first, adapter=adapter):
                for agent in (first, second):
                    self.token_for(agent)
                    self.host.add(job(adapter, agent), self.agents / cm.agent_identity.root_segment(agent))
                    self.installed(adapter, agent)
                for agent in (first, second):
                    self.assertIn(f"PASS config: {cm.config_path(job(adapter, agent))}",
                                  self.run_cli(adapter, "verify", "--agent", agent)[1])
                kept = (self.files(job(adapter, second)), self.launcher_pid(job(adapter, second)))
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", first)[0], 0)
                self.assertFalse(cm.config_path(job(adapter, first)).exists())
                self.assertEqual((self.files(job(adapter, second)), self.launcher_pid(job(adapter, second))), kept)
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", second)[0], 0)

    def test_a_job_whose_config_records_another_agent_is_never_acted_on(self):
        """Should two agents ever share a key, a verb for one refuses the job the other installed."""
        with mock.patch.object(cm, "agent_key", dash_key):
            for (first, second), adapter in [(p, a) for p in PAIRS for a in CLIS]:
                for agent in (first, second):
                    self.token_for(agent)
                shared = job(adapter, first)
                self.host.add(shared, self.agents / cm.agent_identity.root_segment(first))
                with self.subTest(adapter=adapter):
                    self.installed(adapter, first)
                    files, pid, calls = self.files(shared), self.launcher_pid(shared), len(self.host.calls_naming(shared))
                    for argv in (["verify", "--agent", second], ["uninstall", "--agent", second]):
                        rc, out, err = self.run_cli(adapter, *argv)
                        self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                        self.assertIn(f"records {first}, not {second}", err)
                    rc, out, err = self.install(adapter, second)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn(f"records {first}, not {second}", err)
                    self.assertEqual((self.files(shared), self.launcher_pid(shared)), (files, pid), "the first is untouched")
                    self.assertEqual(len(self.host.calls_naming(shared)), calls)
                    self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", first)[0], 0)

    def test_install_agent_must_be_the_token_files(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                rc, out, err = self.install(adapter, ALPHA, "--agent", BETA)
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn(f"--agent {BETA} is not the agent this install serves ({ALPHA})", err)
                self.assertFalse(cm.config_root().joinpath(cm.AGENTS_DIR).exists())

    def test_install_key_and_single_must_name_the_job_its_token_makes(self):
        """A printed repair line `install --key K` or `install --single` never installs over another job: run with
        a token for another agent, install refuses and writes nothing."""
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                for extra in (["--key", job(adapter, BETA).agent], ["--single"]):
                    rc, out, err = self.install(adapter, ALPHA, *extra)
                    self.assertEqual(rc, cm.EXIT_UNUSABLE, out + err)
                    self.assertIn("nothing was changed", err)
                    self.assertEqual(cm.installed_jobs(job(adapter, "")), [])
                rc, out, err = self.install(adapter, ALPHA, "--key", job(adapter, ALPHA).agent)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.run_cli(adapter, "uninstall", "--agent", ALPHA)[0], 0)

    def test_with_none_installed_the_agentless_form_is_the_single_agent_job(self):
        for adapter in CLIS:
            with self.subTest(adapter=adapter):
                rc, out, err = self.run_cli(adapter, "uninstall")
                self.assertEqual(rc, 0, err)
                self.assertIn(f"nothing to remove: {adapter} is not installed", out)
                rc, out, err = self.run_cli(adapter, "list")
                self.assertIn("no agent is installed", out)

    def test_run_serves_the_agent_its_unit_names(self):
        configs = {}
        for agent in (ALPHA, BETA):
            for adapter in CLIS:
                shape = job(adapter, agent)
                cm._write(cm.config_path(shape), cm.render_config(shape, {"AGENT_ID": agent, "ROOT": str(self.root_of(agent)),
                                                                         "CODEX": str(self.codex), "CLAUDE": str(self.claude),
                                                                         "SESSION_ID": fx.SESSION_ID}), 0o600)
                configs[shape.job] = shape
        seen = []
        held = lambda cfg, root, *a, **kw: seen.append(("codex", root)) or 0  # noqa: E731
        launcher = mock.MagicMock(side_effect=lambda cfg, config_dir: seen.append(("claude", cfg["ROOT"], config_dir))
                                  or mock.MagicMock(serve=lambda: 0))
        with mock.patch.object(codex_cli, "_held", side_effect=held), \
                mock.patch.object(codex_cli, "version_problem", return_value=""), \
                mock.patch.object(claude_cli, "version_problem", return_value=""), \
                mock.patch.object(claude_cli.launcher, "Launcher", launcher), \
                mock.patch.object(cm, "load_config", side_effect=lambda shape: (cm.read_config(cm.config_path(shape)), "")):
            for agent in (BETA, ALPHA):
                os.environ[cm.AGENT_VAR] = cm.agent_key(agent)
                self.assertEqual(self.run_cli(CX.name, "run")[0], 0)
                self.assertEqual(self.run_cli(CL.name, "run")[0], 0)
            os.environ[cm.AGENT_VAR] = "../escape"
            for adapter in CLIS:
                rc, out, err = self.run_cli(adapter, "run")
                self.assertEqual(rc, cm.EXIT_UNUSABLE)
                self.assertIn("is not an agent key", err)
        del os.environ[cm.AGENT_VAR]
        self.assertEqual(seen, [("codex", str(self.root_of(BETA))),
                                ("claude", str(self.root_of(BETA)), cm.config_dir(job(CL.name, BETA))),
                                ("codex", str(self.root_of(ALPHA))),
                                ("claude", str(self.root_of(ALPHA)), cm.config_dir(job(CL.name, ALPHA)))])

    def test_the_agent_key(self):
        long_id = "@" + "x" * 40 + ":" + "y" * 40 + ".example"
        cases = {ALPHA: "alpha.agent_ag2.space", "@support-cc.agent:s.space": "support-cc.agent_s.space",
                 "@support-cc-2.agent:s.space": "support-cc-2.agent_s.space",
                 "@" + "a" * 30 + ":" + "b" * 33: "a" * 30 + "_" + "b" * 33,
                 "@Mixed:Case.Server": None, long_id: None}
        keys = set()
        for agent, want in cases.items():
            with self.subTest(agent=agent):
                key = cm.agent_key(agent)
                self.assertEqual(key, cm.agent_key(agent), "stable")
                self.assertTrue(cm.AGENT_KEY_RE.fullmatch(key), key)
                self.assertLessEqual(len(key), cm.AGENT_KEY_MAX)
                self.assertEqual(key, cm.agent_identity.root_segment(agent), "the key is the agent's root segment")
                if want:
                    self.assertEqual(key, want)
                shape = cm.for_key(CL, key)
                self.assertTrue(sm.UNIT_RE.fullmatch(shape.service_name), shape.service_name)
                self.assertTrue(sm.UNIT_RE.fullmatch(cb.bridge_shape(shape).service_name))
                self.assertEqual(shape.agent_label, f"ai.ag2.sparrow.claude-local.{key}")
                keys.add(key)
        self.assertEqual(len(keys), len(cases), "distinct agents, distinct keys")
        self.assertNotEqual(cm.agent_key(long_id), cm.agent_key(long_id.replace("y", "z")))
        self.assertFalse(sm.UNIT_RE.fullmatch("ag2-sparrow-claude-local.--x.service"))
        self.assertFalse(sm.UNIT_RE.fullmatch("ag2-sparrow-claude-local.a b.service"))


if __name__ == "__main__":
    unittest.main()
