#!/usr/bin/env python3
"""sparrowd runs each agent bridge a claude-local install declared, and nothing else.

The declaration is written by the adapter's own writer (the contract both sides share). sparrowd turns
a valid one into exactly one worker, skips every other with one reason, and marks each worker by its
own declaration path. The wrapper takes the token from the vault (mocked: never the Keychain), injects
the agent's dirs and the task stamper, and refuses to run the bridge without the token.
Run: python3 tests/sparrowd-agent-bridges.test.py"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sparrow_agent_bridge as bridge  # noqa: E402

from ag2_sparrow import _dirs, gateway_credential_sources as _sources, local_task_protocol  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli  # noqa: E402

AGENT = "@probe.agent:ag2.space"
SEG = "probe.agent_ag2.space"
AGENT_URL = "https://agent-gw.example/relay"


def vault(token="tok-agent", url=AGENT_URL, asked=None):
    """A get_key over the agent's two vault entries: its token, and its gateway URL when `url` is set."""
    store = {cli.vault_key(SEG): token, **({cli.url_vault_key(SEG): url} if url is not None else {})}

    def get(key):
        if asked is not None:
            asked.append(key)
        return store[key]
    return get


def load_sparrowd():
    spec = importlib.util.spec_from_file_location("sparrowd_agent_under_test", REPO / "src" / "sparrowd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recording_bridge_environ(seen: dict):
    """Patch the wrapper's environment builder to keep what it returns in seen["env"]: what the bridge runs under."""
    real = bridge.bridge_environ

    def spy(*args, **kwargs):
        seen["env"] = real(*args, **kwargs)
        return seen["env"]
    return mock.patch.object(bridge, "bridge_environ", spy)


def runs_under(env: dict) -> bool:
    """Whether os.environ, read key by key, is exactly `env`."""
    return set(os.environ) == set(env) and all(os.environ.get(k) == v for k, v in env.items())


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.sd = load_sparrowd()

    def declare(self, agent=AGENT, seg=SEG):
        """The declaration exactly as `install` writes it under a host supervisor."""
        root = self.ws / "agents" / seg
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": agent, "TOKEN_VAULT_KEY": cli.vault_key(seg)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        return cli.bridge_home(root) / cli.DECLARATION

    def edit(self, path, **over):
        body = json.loads(path.read_text())
        body.update(over)
        path.write_text(json.dumps(body))


class Specs(Base):
    def test_a_marked_declaration_is_exactly_one_worker_on_the_wrapper(self):
        decl = self.declare()
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[cli.vault_key(SEG)])
        self.assertEqual(skipped, [])
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual((spec.name, spec.argv, spec.cwd, spec.env),
                         (cli.worker_name(SEG), [sys.executable, str(REPO / "src" / "sparrow_agent_bridge.py"), str(decl)],
                          str(REPO), None))
        body = json.loads(decl.read_text())
        root = self.ws / "agents" / SEG
        self.assertEqual(body["env"], {"AGENT_CONNECT_TASK_DIR": f"{root}/tasks", "AGENT_CONNECT_RESULT_DIR": f"{root}/results",
                                       "AGENT_CONNECT_STATE_DIR": f"{root}.bridge/state", "AGENT_MXID": AGENT})

    def test_each_bad_declaration_is_skipped_with_one_reason(self):
        decl = self.declare()
        key = cli.vault_key(SEG)
        good = decl.read_text()
        cases = [
            (lambda: decl.write_text("{torn"), "malformed"),
            (lambda: self.edit(decl, written_by="someone"), "not written by"),
            (lambda: self.edit(decl, name="../etc"), "name '../etc' is not the derived"),
            (lambda: self.edit(decl, name="bridge-other-0000"), "name 'bridge-other-0000' is not the derived"),
            (lambda: self.edit(decl, module="os"), "module 'os' is not the derived"),
            (lambda: self.edit(decl, env={"AGENT_CONNECT_TASK_DIR": 3}), "AGENT_MXID None is not the agent"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_MXID="@other:x")),
             "AGENT_MXID '@other:x' is not the agent the segment"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_MXID="")), "is not the agent"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_MXID=AGENT + " ")),
             f"AGENT_MXID '{AGENT} ' is not the agent"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_CONNECT_TASK_DIR="/elsewhere/tasks")),
             "env is not the canonical one"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_CONNECT_STATE_DIR=str(self.ws / "agents" / "x"))),
             "env is not the canonical one"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_CONNECT_TASK_DIR=3)),
             "env is not the canonical one"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], LANG="C")),
             "env keys differ from the bridge's own (LANG)"),
            (lambda: self.edit(decl, token_vault_key="AG2_RELAY_TOKEN_BRIDGE_OTHER_00000000"),
             "token_vault_key 'AG2_RELAY_TOKEN_BRIDGE_OTHER_00000000' is not the derived"),
            (lambda: self.edit(decl, token_env="OTHER_TOKEN"), "token_env 'OTHER_TOKEN' is not the derived"),
            (lambda: self.edit(decl, token_env=None), "token_env None is not the derived"),
        ]
        for spoil, why in cases:
            decl.write_text(good)
            spoil()
            specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[key])
            self.assertEqual(specs, [], why)
            self.assertEqual(len(skipped), 1, skipped)
            self.assertIn(why, skipped[0])
        decl.write_text(good)
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=["OTHER"])
        self.assertEqual(specs, [])
        self.assertIn(f"the vault has no {key}", skipped[0], "an absent vault key is never run")

    def test_a_declaration_chooses_neither_the_vault_key_nor_the_env(self):
        """The reviewed repro: a session-writable file naming another secret, a URL and a loader."""
        evil = self.ws / "agents" / "evil.bridge" / "worker.json"
        evil.parent.mkdir(parents=True)
        root = self.ws / "agents" / "evil"
        evil.write_text(json.dumps({
            "written_by": bridge.WRITTEN_BY, "name": "evil", "module": "ag2_sparrow.remote_gateway_bridge",
            "token_vault_key": "ANTHROPIC_API_KEY", "token_env": "REMOTE_TASK_TOKEN",
            "env": {"AGENT_CONNECT_TASK_DIR": f"{root}/tasks", "AGENT_CONNECT_RESULT_DIR": f"{root}/results",
                    "AGENT_CONNECT_STATE_DIR": f"{root}.bridge/state", "REMOTE_TASK_URL": "https://attacker.example",
                    "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib"}}))
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=["ANTHROPIC_API_KEY"])
        self.assertEqual(specs, [], "sparrowd never lists it")
        self.assertEqual(len(skipped), 1)
        self.assertIn("is not the agent the segment evil names", skipped[0])
        mxid = "@evil:x"
        seg = bridge.agent_identity.root_segment(mxid)
        evil2 = self.ws / "agents" / (seg + ".bridge") / "worker.json"
        evil2.parent.mkdir()
        canonical = bridge.adapter.bridge_env(str(self.ws / "agents" / seg), mxid)
        for over, why in (({"token_vault_key": "ANTHROPIC_API_KEY"}, "token_vault_key 'ANTHROPIC_API_KEY' is not the derived"),
                          ({"env": dict(canonical, REMOTE_TASK_URL="https://attacker.example")},
                           "env keys differ from the bridge's own (REMOTE_TASK_URL)"),
                          ({"env": dict(canonical, DYLD_INSERT_LIBRARIES="/tmp/x.dylib")},
                           "env keys differ from the bridge's own (DYLD_INSERT_LIBRARIES)")):
            body = {"written_by": bridge.WRITTEN_BY, "name": cli.worker_name(seg), "module": bridge.BRIDGE_MODULE,
                    "token_vault_key": cli.vault_key(seg), "token_env": "REMOTE_TASK_TOKEN", "env": canonical}
            body.update(over)
            evil2.write_text(json.dumps(body))
            specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=["ANTHROPIC_API_KEY", cli.vault_key(seg)])
            self.assertEqual(specs, [], over)
            self.assertIn(why, " ".join(skipped))
            run = mock.Mock()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bridge.main([str(evil2)], workspace=self.ws, get_key=mock.Mock(), run=run), 2)
            run.assert_not_called()

    def test_a_declaration_must_sit_directly_under_agents(self):
        decl = self.declare()
        nested = self.ws / "agents" / "deep" / decl.parent.name / "worker.json"
        nested.parent.mkdir(parents=True)
        nested.write_text(decl.read_text())
        self.assertIn("not a declaration directly under", bridge.load(nested, self.ws / "agents")[1])
        other = decl.parent / "other.json"
        other.write_text(decl.read_text())
        self.assertIn("not a declaration directly under", bridge.load(other, self.ws / "agents")[1])
        self.assertEqual(bridge.load(decl, self.ws / "agents")[1], "", "the canonical one still loads")

    def test_two_agents_two_workers_two_markers_and_a_borrowed_name_is_skipped(self):
        a, b = self.declare(), self.declare("@second:ag2.space", "second_ag2.space")
        keys = [cli.vault_key(SEG), cli.vault_key("second_ag2.space")]
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=keys)
        self.assertEqual((len(specs), skipped), (2, []))
        self.assertEqual(len({s.name for s in specs}), 2)
        markers = [self.sd.supervision_marker(s) for s in specs]
        lit = lambda d: str(d).replace(".", "\\.")  # noqa: E731 - the fixture paths have no other ERE metacharacter
        want = [f"(^|[/ ])sparrow_agent_bridge\\.py {lit(d)}$" for d in (a, b)]
        self.assertEqual(sorted(markers), sorted(want), "each agent's own declaration path, never "
                         "the shared wrapper's name, so a second agent is not refused as supervised elsewhere")
        core = self.sd.WorkerSpec(name="remote-gateway-bridge", argv=["py", "/x/src/remote-gateway-bridge.py"])
        self.assertEqual(self.sd.supervision_marker(core), "remote-gateway-bridge.py", "other workers unchanged")
        self.edit(b, name=json.loads(a.read_text())["name"])
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=keys)
        self.assertEqual(len(specs), 1)
        self.assertIn("is not the derived", skipped[0], "a name is never borrowed from another agent")

    def test_worker_specs_and_main_carry_the_agent_bridges(self):
        self.declare()
        fake_vault = types.SimpleNamespace(list_vault_keys=lambda: [cli.vault_key(SEG)])
        with mock.patch.object(self.sd, "resolve_workspace", return_value=self.ws), \
                mock.patch.dict(sys.modules, {"vault_intercept": fake_vault}), \
                contextlib.redirect_stderr(io.StringIO()):
            names = [s.name for s in self.sd.worker_specs()]
            self.assertEqual((names[0], names.count(cli.worker_name(SEG))), ("remote-gateway-bridge", 1))
            seen = []
            with mock.patch.object(self.sd, "external_supervisor", side_effect=lambda m: seen.append(m)), \
                    mock.patch.object(self.sd, "run", return_value=0) as run:
                self.assertEqual(self.sd.main(), 0)
            decl = str(cli.bridge_home(self.ws / "agents" / SEG) / cli.DECLARATION)
            self.assertTrue(any(m.endswith(decl.replace(".", "\\.") + "$") for m in seen), seen)
            self.assertEqual(run.call_args[0][1], self.ws / "state" / "sparrowd")

    def test_an_unreadable_vault_manifest_is_a_scan_that_could_not_be_made(self):
        """Not "no key present": a supervisor fed an empty set would stop every agent bridge."""
        self.declare()

        def broken():
            raise ValueError("manifest")
        fake = types.SimpleNamespace(list_vault_keys=broken)
        with mock.patch.dict(sys.modules, {"vault_intercept": fake}):
            with self.assertRaises(bridge.ScanUnavailable) as cm:
                self.sd._agent_bridge_specs(self.ws)
            self.assertIn("vault manifest unreadable (ValueError: manifest)", str(cm.exception))
            err = io.StringIO()
            with mock.patch.object(self.sd, "resolve_workspace", return_value=self.ws), contextlib.redirect_stderr(err):
                names = [s.name for s in self.sd.worker_specs()]
        self.assertEqual(names, ["remote-gateway-bridge"], "the static workers still run")
        self.assertIn("not supervised — agent bridges not scanned: vault manifest unreadable", err.getvalue())

    def test_what_a_declarations_own_bytes_can_do_is_a_skip_never_unavailable_never_an_exception(self):
        decl = self.declare()
        key = cli.vault_key(SEG)
        good = decl.read_text()

        def as_dir():
            decl.unlink()
            decl.mkdir()
        cases = [
            (lambda: decl.write_bytes(b"\xff\xfe\x00"), "malformed (UnicodeDecodeError)"),
            (lambda: decl.write_text("{}" + " " * bridge.MAX_DECLARATION_BYTES),
             f"{bridge.MAX_DECLARATION_BYTES + 2} bytes is not a declaration (cap {bridge.MAX_DECLARATION_BYTES})"),
            (as_dir, "not a regular file"),
            (lambda: (decl.unlink(), os.mkfifo(decl)), "not a regular file"),
            (lambda: decl.write_text("[1]"), "not written by"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_MXID="@\ud800:x")),
             "AGENT_MXID '@\\ud800:x' is not the agent"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_CONNECT_TASK_DIR="/a\x00b")),
             "env is not the canonical one"),
            (lambda: self.edit(decl, env=dict(json.loads(good)["env"], AGENT_CONNECT_TASK_DIR={"x": []})),
             "env is not the canonical one"),
        ]
        for spoil, why in cases:
            if decl.is_dir():
                decl.rmdir()
            decl.unlink(missing_ok=True)
            decl.write_text(good)
            spoil()
            found, skipped = bridge.declarations(self.ws, [key])
            self.assertEqual(found, [], why)
            self.assertEqual(len(skipped), 1, skipped)
            self.assertIn(f"{decl}: {why}", skipped[0])
            specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[key])
            self.assertEqual((specs, len(skipped)), ([], 1), "the launcher passes the skip through")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bridge.main([str(decl)], workspace=self.ws, get_key=mock.Mock(), run=mock.Mock()), 2)
        decl.unlink()
        self.assertIn("absent", bridge.load(decl, self.ws / "agents")[1])
        decl.write_text(good)
        with mock.patch.object(bridge.json, "loads", side_effect=RecursionError):  # a parser without a deep stack
            self.assertIn("malformed (RecursionError)", bridge.declarations(self.ws, [key])[1][0])
        decl.write_text(good)
        self.assertEqual(len(bridge.declarations(self.ws, [key])[0]), 1, "restored: served")

    def test_an_unlistable_agents_dir_or_home_is_unavailable_an_unreadable_declaration_is_held(self):
        self.assertEqual(bridge.declarations(self.ws, []), ([], []), "absent: nothing declared")
        decl = self.declare()
        key = cli.vault_key(SEG)
        self.assertEqual(len(bridge.declarations(self.ws, [key])[0]), 1)
        agents = self.ws / "agents"
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        for target in (agents, decl.parent):
            mode = target.stat().st_mode
            os.chmod(target, 0)
            try:
                with self.assertRaises(bridge.ScanUnavailable) as cm:
                    bridge.declarations(self.ws, [key])
                # a home that cannot be entered fails at the declaration's own stat
                named = decl if target == decl.parent else target
                self.assertIn(f"{named}: unreadable (PermissionError)", str(cm.exception))
                with self.assertRaises(bridge.ScanUnavailable):
                    self.sd._agent_bridge_specs(self.ws, vault_keys=[key])
            finally:
                os.chmod(target, mode)
        other = self.declare("@second:ag2.space", "second_ag2.space")
        keys = [key, cli.vault_key("second_ag2.space")]
        os.chmod(decl, 0)
        try:
            held = []
            found, skipped = bridge.declarations(self.ws, keys, held)
            self.assertEqual([p for p, _ in found], [other], "the other declarations are still read")
            self.assertEqual(skipped, [f"{decl}: unreadable (PermissionError)"])
            self.assertEqual(held, [cli.worker_name(SEG)], "its worker is kept as it is, not dropped")
            self.assertEqual(bridge.declarations(self.ws, keys)[1], skipped, "held is optional")
            held = []
            specs, _ = self.sd._agent_bridge_specs(self.ws, vault_keys=keys, held=held)
            self.assertEqual(([s.argv[-1] for s in specs], held), ([str(other)], [cli.worker_name(SEG)]))
        finally:
            os.chmod(decl, 0o644)
        shutil.rmtree(other.parent)
        self.assertEqual(len(bridge.declarations(self.ws, [key])[0]), 1, "readable again: served again")
        (agents / "stray.bridge").mkdir()
        self.assertEqual(len(bridge.declarations(self.ws, [key])[0]), 1, "a home without a declaration is not one")
        decl.write_text("{torn")
        found, skipped = bridge.declarations(self.ws, [key])
        self.assertEqual(found, [])
        self.assertIn("malformed (JSONDecodeError)", skipped[0], "a malformed one is a skip, not an unavailable scan")
        os.chmod(decl, 0)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bridge.main([str(decl)], workspace=self.ws, get_key=mock.Mock(), run=mock.Mock()), 2)
        finally:
            os.chmod(decl, 0o644)


class Wrapper(Base):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/host/relay.env", "REMOTE_TASK_TOKEN_FILE": "/host/t",
                                           "REMOTE_PROACTIVE_ROOM": "!owner-dm", "AGENT_MXID": "@core:x",
                                           "REMOTE_TASK_TOKEN": "host-token", "KEEP_ME": "1"})
        env.start()
        self.addCleanup(env.stop)
        saved = (_dirs._injected, local_task_protocol._TASK_STAMPER)
        self.addCleanup(lambda: (setattr(_dirs, "_injected", saved[0]),
                                 setattr(local_task_protocol, "_TASK_STAMPER", saved[1])))

    def test_the_bridge_runs_with_the_agents_dirs_the_stamper_and_the_vault_token(self):
        decl = self.declare()
        root = self.ws / "agents" / SEG
        seen = {}

        def fake_run(module, run_name, alter_sys):
            from task_envelope import stamp_text
            seen.update(module=module, run_name=run_name, task=_dirs.task_dir(), result=_dirs.result_dir(),
                        state=_dirs.state_dir(), stamper=local_task_protocol._TASK_STAMPER is stamp_text,
                        token=os.environ.get("REMOTE_TASK_TOKEN"), mxid=os.environ.get("AGENT_MXID"),
                        device=os.environ.get("AG2_DEVICE_ENV"), token_file=os.environ.get("REMOTE_TASK_TOKEN_FILE"),
                        room=os.environ.get("REMOTE_PROACTIVE_ROOM"), keep=os.environ.get("KEEP_ME"),
                        url=os.environ.get("REMOTE_TASK_URL"),
                        extra=sorted(set(os.environ) - set(base) - set(bridge.HOST_ONLY)
                                     - set(_sources.bridge_token_environ("", AGENT_URL))))
        base = set(os.environ)
        asked = []
        rc = bridge.main([str(decl)], workspace=self.ws, get_key=vault(asked=asked), run=fake_run)
        self.assertEqual(rc, 0)
        self.assertEqual(asked, [cli.vault_key(SEG), cli.url_vault_key(SEG)])
        self.assertEqual(seen, {"module": "ag2_sparrow.remote_gateway_bridge", "run_name": "__main__",
                                "task": root / "tasks", "result": root / "results", "state": Path(f"{root}.bridge/state"),
                                "stamper": True, "token": "tok-agent", "mxid": AGENT, "device": None, "token_file": None,
                                "room": "", "keep": "1", "url": AGENT_URL, "extra": []})

    # Synthetic host values, one under every name a host's gateway token may sit under.
    HOST_TOKENS = {"REMOTE_TASK_TOKEN": "host-token", "AG2_REMOTE_TOKEN": "host-alias-token",
                   "GATEWAY_TOKEN": "host-gateway-token", "RELAY_TOKEN": "host-relay-token",
                   "REMOTE_MEDIA_HS_TOKEN": "host-media-token"}

    def built_environment(self) -> dict:
        """The environment the bridge module runs under when sparrowd holds a token under every name."""
        decl = self.declare()
        seen = {}
        with mock.patch.dict(os.environ, self.HOST_TOKENS), recording_bridge_environ(seen):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=vault(),
                             run=lambda module, run_name, alter_sys: seen.update(applied=runs_under(seen["env"])))
        self.assertEqual(rc, 0)
        self.assertIs(seen.get("applied"), True, "the bridge ran under exactly the built environment")
        return seen["env"]

    def test_the_hosts_REMOTE_TASK_TOKEN_is_replaced_by_the_agents(self):
        env = self.built_environment()
        self.assertEqual(env.get("REMOTE_TASK_TOKEN"), "tok-agent")
        self.assertNotIn("host-token", env.values())

    def test_the_hosts_AG2_REMOTE_TOKEN_never_reaches_the_bridge(self):
        env = self.built_environment()
        self.assertNotIn("AG2_REMOTE_TOKEN", env)
        self.assertNotIn("host-alias-token", env.values())

    def test_the_hosts_GATEWAY_TOKEN_never_reaches_the_bridge(self):
        env = self.built_environment()
        self.assertNotIn("GATEWAY_TOKEN", env)
        self.assertNotIn("host-gateway-token", env.values())

    def test_the_hosts_RELAY_TOKEN_never_reaches_the_bridge(self):
        env = self.built_environment()
        self.assertNotIn("RELAY_TOKEN", env)
        self.assertNotIn("host-relay-token", env.values())

    def test_the_hosts_REMOTE_MEDIA_HS_TOKEN_never_reaches_the_bridge(self):
        env = self.built_environment()
        self.assertNotIn("REMOTE_MEDIA_HS_TOKEN", env)
        self.assertNotIn("host-media-token", env.values())

    def test_every_name_the_owner_strips_is_stripped_before_the_agents_token_is_set(self):
        host = {k: f"host-{i}" for i, k in enumerate(_sources.bridge_strip_keys())}
        built = bridge.bridge_environ({"env": {"AGENT_MXID": AGENT}}, "tok-agent", base={**host, "KEEP_ME": "1"})
        self.assertEqual({k: v for k, v in built.items() if k in host},
                         {k: v for k, v in _sources.bridge_token_environ("tok-agent").items() if k in host})
        self.assertEqual(built["KEEP_ME"], "1")

    def test_the_carried_config_comes_only_from_the_owner(self):
        decl = {"env": {"AGENT_MXID": AGENT}}
        host = {"REMOTE_PROACTIVE_TRUST_OK": "raw-unreviewed", "KEEP_ME": "1"}
        with mock.patch.object(_sources, "bridge_carried_config", lambda env, read: {}):
            built = bridge.bridge_environ(decl, "tok-agent", base=host)
        for key in _sources.BRIDGE_CARRIED_CONFIG:
            self.assertNotIn(key, built, "a host value bypassed the owner's empty answer")
        with mock.patch.object(_sources, "bridge_carried_config", lambda env, read: {"REMOTE_PROACTIVE_TRUST_OK": "1"}):
            built = bridge.bridge_environ(decl, "tok-agent", base=host)
        self.assertEqual(built["REMOTE_PROACTIVE_TRUST_OK"], "1")
        self.assertEqual(built["KEEP_ME"], "1")

    def test_no_host_gateway_url_reaches_the_bridge_under_any_name(self):
        host = {"REMOTE_TASK_URL": "https://host/1", "AG2_REMOTE_URL": "https://host/2", "GATEWAY_URL": "https://host/3",
                "RELAY_URL": "https://host/4", "KEEP_ME": "1"}
        built = bridge.bridge_environ({"env": {"AGENT_MXID": AGENT}}, "https://agent/relay|s", base=host)
        self.assertEqual({k for k in host if k in built}, {"KEEP_ME"}, "the endpoint comes only from the agent's token")

    def test_no_token_means_no_bridge(self):
        decl = self.declare()
        run = mock.Mock()

        def missing(key):
            raise KeyError(key)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=missing, run=run)
        self.assertEqual(rc, 1)
        run.assert_not_called()
        self.assertIn(f"the vault has no {cli.vault_key(SEG)}", err.getvalue())
        self.assertEqual(os.environ.get("REMOTE_TASK_TOKEN"), "host-token", "nothing touched before the token is in hand")

    def test_a_bad_declaration_or_usage_never_runs_and_the_defaults_resolve(self):
        run = mock.Mock()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.main([], workspace=self.ws, run=run), 2)
            self.assertEqual(bridge.main([str(self.ws / "none.json")], workspace=self.ws, run=run), 2)
        run.assert_not_called()
        decl = self.declare()
        fake_vault = types.SimpleNamespace(get_vault_key=lambda k: f"{AGENT_URL}|tok")
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                mock.patch.dict(sys.modules, {"vault_intercept": fake_vault}):
            self.assertEqual(bridge.main([str(decl)], run=run), 0)
        run.assert_called_once()

class HostCredentialFiles(Base):
    """The shipped path, in this process: sparrowd's wrapper hands the bridge module an environment that keeps
    the host's CLAUDE_CONFIG_DIR (for access.json) but the bridge opens no channel .env under it."""
    CANARY = "canary-host-file-token-0123456789"
    BRIDGE = "ag2_sparrow.remote_gateway_bridge"

    def setUp(self):
        super().setUp()
        self.cfg = self.ws / "host-config"
        channel = self.cfg / "channels" / "ag2space"
        channel.mkdir(parents=True)
        self.dotenv = channel / ".env"
        self.dotenv.write_text(f"REMOTE_TASK_TOKEN={self.CANARY}\nREMOTE_PROACTIVE_TRUST_OK=1\n", encoding="utf-8")
        self.access = channel / "access.json"
        self.access.write_text(json.dumps({"@someone:ag2.space": "team"}), encoding="utf-8")
        self.opens = []
        import builtins
        from ag2_sparrow import bounded_file_read

        def recording(real):
            def opener(path, *a, **k):
                if isinstance(path, (str, os.PathLike)):
                    self.opens.append(os.fspath(path))
                return real(path, *a, **k)
            return opener
        # The bounded reader (the wrapper's bound name too) and open itself: every file touched is on the record.
        for target, name in ((builtins, "open"), (bounded_file_read, "read_regular"), (bounded_file_read, "read_regular_text"),
                             (bridge, "read_regular")):
            real = getattr(target, name, bounded_file_read.read_regular)
            patch = mock.patch.object(target, name, recording(real), create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.saved_module = sys.modules.pop(self.BRIDGE, None)
        self.addCleanup(lambda: sys.modules.__setitem__(self.BRIDGE, self.saved_module) if self.saved_module
                        else sys.modules.pop(self.BRIDGE, None))

    def launch(self, host_env):
        """Run the wrapper under `host_env` with the bridge module imported where the bridge would run: the
        module, the environment it saw, every path opened by the child alone, and its stderr."""
        decl = self.declare()
        seen = {}
        err = io.StringIO()

        def child(module, run_name, alter_sys):
            self.assertEqual(module, self.BRIDGE)
            seen["parent_opens"] = list(self.opens)
            del self.opens[:]
            import importlib
            seen["mod"] = importlib.import_module(self.BRIDGE)
            seen["tier_map"] = seen["mod"]._load_tier_map()
            seen["access_path"] = seen["mod"]._ag2space_access_path()
            seen["applied"] = runs_under(seen["env"])
            seen["child_opens"] = list(self.opens)
        with mock.patch.dict(os.environ, host_env), contextlib.redirect_stderr(err), recording_bridge_environ(seen):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=vault(), run=child)
        self.assertEqual(rc, 0)
        self.assertIs(seen.get("applied"), True, "the bridge ran under exactly the built environment")
        seen["stderr"] = err.getvalue()
        return seen

    def test_the_bridge_child_never_opens_the_hosts_channel_env_and_keeps_access_json_and_the_trust_setting(self):
        seen = self.launch({"CLAUDE_CONFIG_DIR": str(self.cfg), "REMOTE_TASK_TOKEN": "host-token"})
        mod, env = seen["mod"], seen["env"]
        self.assertNotIn(str(self.dotenv), seen["child_opens"], "the child opened the host's channel .env")
        self.assertFalse(any(p.endswith(".env") for p in seen["child_opens"]), seen["child_opens"])
        self.assertFalse(mod._SECRETS.contains(self.CANARY), "the host's file token is in the child's registry")
        self.assertFalse(mod._SECRETS.holds_or_is_piece(self.CANARY))
        self.assertNotIn(self.CANARY, env.values())
        self.assertNotIn(self.CANARY, seen["stderr"])
        self.assertNotIn("host-token", env.values())
        self.assertEqual(mod.TOKEN, "tok-agent")
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), str(self.cfg), "the config root stays: access.json needs it")
        self.assertEqual(seen["access_path"], str(self.access))
        self.assertIn(str(self.access), seen["child_opens"], "the tier map is still read from the host's config root")
        self.assertEqual(env.get("REMOTE_PROACTIVE_TRUST_OK"), "1",
                         "the host's file setting reaches the child, carried by the wrapper")
        self.assertIn(str(self.dotenv), seen["parent_opens"], "the wrapper, as the host, is what read the file")
        self.assertEqual(mod.PROACTIVE_ROOM, "")

    def test_the_hosts_environment_setting_wins_and_the_child_still_opens_nothing(self):
        seen = self.launch({"CLAUDE_CONFIG_DIR": str(self.cfg), "REMOTE_TASK_TOKEN": "host-token",
                            "REMOTE_PROACTIVE_TRUST_OK": "0"})
        self.assertEqual(seen["child_opens"], [p for p in seen["child_opens"] if not p.endswith(".env")])
        self.assertEqual(seen["env"].get("REMOTE_PROACTIVE_TRUST_OK"), "0")
        self.assertEqual(seen["parent_opens"], [p for p in seen["parent_opens"] if p != str(self.dotenv)],
                         "set in the host's environment: not even the wrapper opens the file")
        self.assertFalse(seen["mod"]._SECRETS.contains(self.CANARY))



class AgentEndpoint(Base):
    """The agent's gateway: inside a combined token, else only from the owner's AG2_RELAY_URL_<worker> vault key;
    never from the host's environment or from the declaration, which the agent's own session could write."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        saved = (_dirs._injected, local_task_protocol._TASK_STAMPER)
        self.addCleanup(lambda: (setattr(_dirs, "_injected", saved[0]),
                                 setattr(local_task_protocol, "_TASK_STAMPER", saved[1])))

    def started(self, get_key, base=None):
        decl = self.declare()
        seen, err = {}, io.StringIO()
        with mock.patch.dict(os.environ, base or {}), contextlib.redirect_stderr(err):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=get_key,
                             run=lambda module, run_name, alter_sys: seen.update(
                                 url=os.environ.get("REMOTE_TASK_URL"), token=os.environ.get("REMOTE_TASK_TOKEN")))
        return rc, seen, err.getvalue()

    def test_the_split_form_takes_its_gateway_from_the_owners_vault_key(self):
        rc, seen, _ = self.started(vault())
        self.assertEqual((rc, seen), (0, {"url": AGENT_URL, "token": "tok-agent"}))

    def test_the_split_form_without_a_vault_url_fails_with_the_one_command_that_fixes_it(self):
        rc, seen, err = self.started(vault(url=None))
        self.assertEqual((rc, seen), (1, {}))
        self.assertIn(f"`vault set {cli.url_vault_key(SEG)} <the agent's gateway URL>`", err)
        self.assertNotIn("tok-agent", err)

    def test_a_combined_token_ignores_the_vault_url(self):
        asked = []
        rc, seen, _ = self.started(vault(token=f"{AGENT_URL}|tok-agent", url="https://other-gw.example/relay", asked=asked))
        self.assertEqual((rc, seen), (0, {"url": None, "token": f"{AGENT_URL}|tok-agent"}))
        self.assertEqual(asked, [cli.vault_key(SEG)], "the URL key is not even read")

    def test_a_host_url_never_reaches_the_bridge(self):
        host = {k: "https://host-gw.example/relay" for k in ("REMOTE_TASK_URL", "AG2_REMOTE_URL", "GATEWAY_URL", "RELAY_URL")}
        for get_key, want in ((vault(), AGENT_URL), (vault(token=f"{AGENT_URL}|tok-agent"), None)):
            with self.subTest(want=want):
                rc, seen, _ = self.started(get_key, base=host)
                self.assertEqual((rc, seen["url"]), (0, want))

    def test_a_url_written_into_the_declaration_is_never_used(self):
        decl = self.declare()
        self.edit(decl, gateway_url="https://session-chosen.example/relay", url="https://session-chosen.example/relay")
        for get_key, want_rc in ((vault(url=None), 1), (vault(), 0)):
            seen, err = {}, io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = bridge.main([str(decl)], workspace=self.ws, get_key=get_key,
                                 run=lambda module, run_name, alter_sys: seen.update(url=os.environ.get("REMOTE_TASK_URL")))
            self.assertEqual(rc, want_rc, err.getvalue())
            self.assertNotEqual(seen.get("url"), "https://session-chosen.example/relay")
            self.assertNotIn("session-chosen", repr(bridge.load(decl, self.ws / "agents")[0]))

    URL_SHAPED_BEARER = "https://opaque.example/bearer-GGGG7777"

    def test_a_url_shaped_opaque_bearer_takes_its_gateway_from_the_url_key(self):
        rc, seen, _ = self.started(vault(token=self.URL_SHAPED_BEARER))
        self.assertEqual((rc, seen), (0, {"url": AGENT_URL, "token": self.URL_SHAPED_BEARER}))

    def test_a_url_shaped_token_with_no_url_key_names_both_remedies(self):
        asked = []
        rc, seen, err = self.started(vault(token=self.URL_SHAPED_BEARER, url=None, asked=asked))
        self.assertEqual((rc, seen), (1, {}))
        self.assertIn("If it is a combined token that lost its '|'", err)
        self.assertIn(f"if it is an opaque bearer, run `vault set {cli.url_vault_key(SEG)}", err)
        self.assertNotIn("GGGG7777", err)
        self.assertEqual(asked, [cli.vault_key(SEG), cli.url_vault_key(SEG)])

    def test_a_hostless_gateway_is_refused_in_both_forms_by_the_wrapper_and_the_preflight(self):
        for bad in ("https://?missing-host", "https://#missing-host", "https://:443/relay"):
            for get_key in (vault(url=bad), vault(token=f"{bad}|tok-agent", url=None)):
                with self.subTest(bad=bad):
                    rc, seen, err = self.started(get_key)
                    self.assertEqual((rc, seen), (1, {}))
                    self.assertIn("is not an http(s) URL with a host", err)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=get_key), 1)
                    self.assertIn("WILL NOT START: ", out.getvalue())
                    self.assertNotIn("tok-agent", err + out.getvalue())

    def test_a_gateway_with_userinfo_a_query_or_a_fragment_is_refused_in_both_forms(self):
        for bad in ("https://u:pw-zq9@agent-gw.example/relay", "https://agent-gw.example/relay?k=zq9",
                    "https://agent-gw.example/relay#zq9"):
            for get_key in (vault(url=bad), vault(token=f"{bad}|tok-agent", url=None)):
                with self.subTest(bad=bad):
                    rc, seen, err = self.started(get_key)
                    self.assertEqual((rc, seen), (1, {}))
                    self.assertIn("carries userinfo, a query or a fragment", err)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=get_key), 1)
                    self.assertIn("WILL NOT START: ", out.getvalue())
                    self.assertNotIn("zq9", err + out.getvalue())
                    self.assertNotIn("tok-agent", err + out.getvalue())

    def test_a_token_with_no_secret_is_refused_by_the_wrapper_and_the_preflight(self):
        get_key = vault(token=f"{AGENT_URL}|")
        rc, seen, err = self.started(get_key)
        self.assertEqual((rc, seen), (1, {}))
        self.assertIn(f"the token under {cli.vault_key(SEG)} carries no secret", err)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=get_key), 1)
        self.assertIn("WILL NOT START: bridge-probe-agent-ag2-space-", out.getvalue())
        self.assertIn("carries no secret", out.getvalue())

    def test_an_unusable_vault_url_is_refused_by_name(self):
        for bad in ("ftp://gw.example/relay", "https://gw.example/relay|x", "https:// gw", "gw.example", "https://"):
            with self.subTest(bad=bad):
                rc, seen, err = self.started(vault(url=bad))
                self.assertEqual((rc, seen), (1, {}))
                self.assertIn(cli.url_vault_key(SEG), err)

    def test_the_owner_sets_the_url_only_beside_a_bare_token(self):
        self.assertEqual(_sources.bridge_token_environ("s", AGENT_URL),
                         {"REMOTE_TASK_TOKEN": "s", "GATEWAY_CREDENTIALS_INJECTED": "1", "REMOTE_TASK_URL": AGENT_URL})
        self.assertNotIn("REMOTE_TASK_URL", _sources.bridge_token_environ(f"{AGENT_URL}|s", "https://other/relay"))
        self.assertNotIn("REMOTE_TASK_URL", _sources.bridge_token_environ("s"))

    def test_preflight_names_each_install_that_will_not_start(self):
        self.declare()
        cases = ((vault(token=f"{AGENT_URL}|tok-agent", url=None), 0, "ok: bridge-probe-agent-ag2-space-", "from its token"),
                 (vault(), 0, "ok: bridge-probe-agent-ag2-space-", f"from {cli.url_vault_key(SEG)}"),
                 (vault(url=None), 1, "WILL NOT START: bridge-probe-agent-ag2-space-", f"vault set {cli.url_vault_key(SEG)}"))
        for get_key, want_rc, head, tail in cases:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = bridge.main(["--preflight"], workspace=self.ws, get_key=get_key)
            self.assertEqual(rc, want_rc, out.getvalue())
            self.assertIn(head, out.getvalue())
            self.assertIn(tail, out.getvalue())
            self.assertNotIn("tok-agent", out.getvalue())

    def test_preflight_names_a_missing_token_and_an_unreadable_declaration_and_reads_the_real_vault_by_default(self):
        decl = self.declare()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key={}.__getitem__), 1)
        self.assertIn("WILL NOT START: bridge-probe-agent-ag2-space-", out.getvalue())
        self.assertIn(f"the vault has no {cli.vault_key(SEG)}", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(bridge, "load", side_effect=bridge.DeclarationUnreadable(f"{decl}: unreadable (X)")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=vault()), 1)
        self.assertIn("WILL NOT START: ", out.getvalue())
        self.assertIn("unreadable (X)", out.getvalue())
        fake_vault = types.SimpleNamespace(get_vault_key=vault())
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"vault_intercept": fake_vault}), contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--preflight"], workspace=self.ws), 0)
        self.assertIn(f"from {cli.url_vault_key(SEG)}", out.getvalue())

    def test_preflight_reports_an_unlistable_agents_dir_or_home_in_one_line(self):
        decl = self.declare()
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        for target in (self.ws / "agents", decl.parent):
            mode = target.stat().st_mode
            os.chmod(target, 0)
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=vault()), 1)
            finally:
                os.chmod(target, mode)
            named = decl if target == decl.parent else target
            self.assertEqual(out.getvalue(), f"WILL NOT START: {named}: unreadable (PermissionError)\n")
            self.assertEqual(err.getvalue(), "")

    def test_preflight_with_nothing_declared_passes_silently(self):
        agents = self.ws / "agents"
        shutil.rmtree(agents, ignore_errors=True)
        for setup in (lambda: None, lambda: agents.write_text("not a dir"),
                      lambda: (agents.unlink(), (agents / "stray.bridge").mkdir(parents=True))):
            setup()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(bridge.main(["--preflight"], workspace=self.ws, get_key=vault()), 0)
            self.assertEqual(out.getvalue(), "")

    def test_the_supervisor_and_the_owner_share_one_declaration_cap(self):
        self.assertEqual(bridge.MAX_DECLARATION_BYTES, _sources.MAX_DECLARATION_BYTES)
        self.assertEqual(_sources.MAX_DECLARATION_BYTES, 64 * 1024)

SEAT_URL = "https://seat-gw.example/relay"
SEAT_BEARER = "seat-bearer-QQQQ7777"


class SeatBridge(Base):
    """A seat that `connect` wired under sparrowd runs with exactly the env wire_bridge computes for it, over
    only cli.SEAT_ENV_KEEP of sparrowd's: its own token file and .env, no host credential, URL or other
    variable, and never a vault read."""

    # Allowlisted names sparrowd holds; each reaches the seat bridge unchanged.
    KEPT = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "HOME": "/home/probe", "LANG": "C.UTF-8",
            "TZ": "UTC", "SSL_CERT_FILE": "/etc/ssl/probe.pem", "DO_NOT_TRACK": "1", "SUTANDO_TELEMETRY": "0"}

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/host/relay.env", "REMOTE_TASK_TOKEN_FILE": "/host/t",
                                           "REMOTE_PROACTIVE_ROOM": "!owner-dm", "AGENT_MXID": "@core:x",
                                           "REMOTE_TASK_URL": "https://host-gw/relay", "KEEP_ME": "1",
                                           "SUTANDO_FOO": "host-value", "SUTANDO_SUPERVISED": "1",
                                           **self.KEPT, **Wrapper.HOST_TOKENS})
        env.start()
        self.addCleanup(env.stop)
        saved = (_dirs._injected, local_task_protocol._TASK_STAMPER)
        self.addCleanup(lambda: (setattr(_dirs, "_injected", saved[0]),
                                 setattr(local_task_protocol, "_TASK_STAMPER", saved[1])))
        self.root = self.ws / "agents" / SEG
        self.seat = _sources.seat_channel_dir(cli.bridge_home(self.root))

    def declare_seat(self, tier="team"):
        """The seat as connect writes it beside the root, and the declaration install then writes for it."""
        self.assertEqual(_sources.write_seat_credentials(cli.bridge_home(self.root), agent_id=AGENT, instance="e" * 32,
                                                         tier=tier, relay_url=SEAT_URL, bearer=SEAT_BEARER), "")
        cfg = {"ROOT": str(self.root), "BRIDGE": "sparrowd", "AGENT_ID": AGENT,
               "TOKEN_ENV_FILE": os.path.join(self.seat, ".env"), "TOKEN_VAULT_KEY": ""}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        return cli.bridge_home(self.root) / cli.DECLARATION

    def test_a_seat_declaration_is_one_worker_with_no_vault_key(self):
        self.declare_seat()
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[])
        self.assertEqual((skipped, [s.name for s in specs]), ([], [cli.worker_name(SEG)]))

    def test_the_seat_runs_with_its_token_file_and_device_env_no_host_credential_and_no_vault_read(self):
        decl = self.declare_seat()
        launched = json.loads(decl.read_text())["env"]
        asked, seen = [], {}

        def no_vault(key):
            asked.append(key)
            raise KeyError(key)

        def fake_run(module, run_name, alter_sys):
            found = _sources.discover(os.environ, _sources.env_files(os.environ, bridge.read_regular),
                                      read_token_file=bridge.read_regular, vault_lookup=no_vault)
            seen.update(applied=runs_under(seen["env"]), found=(found.token, found.token_source, found.url, found.fatal))
        with recording_bridge_environ(seen):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=no_vault, run=fake_run)
        self.assertEqual(rc, 0)
        self.assertIs(seen.get("applied"), True, "the bridge ran under exactly the built environment")
        env = seen["env"]
        self.assertEqual(asked, [], "a seat bridge never reads the vault")
        self.assertEqual((env["AG2_DEVICE_ENV"], env["REMOTE_TASK_TOKEN_FILE"]),
                         (os.path.join(self.seat, ".env"), os.path.join(self.seat, "token")))
        self.assertEqual({k: env[k] for k in launched}, launched, "exactly what wire_bridge computed for the seat")
        for key in _sources.bridge_strip_keys() + (_sources.INJECTED_KEY,):
            self.assertEqual(env.get(key, ""), "", f"{key} reached the seat bridge")
        self.assertEqual({k: env.get(k) for k in self.KEPT}, self.KEPT, "the allowlisted names survive")
        for key in ("KEEP_ME", "SUTANDO_FOO", "SUTANDO_SUPERVISED"):
            self.assertNotIn(key, env, f"{key} is not on the seat allowlist")
        self.assertEqual(env["REMOTE_PROACTIVE_ROOM"], "", "the host's owner room is replaced, never inherited")
        self.assertEqual(set(env) - set(launched) - set(cli.SEAT_ENV_KEEP), {"REMOTE_PROACTIVE_ROOM"},
                         "nothing outside the allowlist")
        self.assertEqual(env["AGENT_MXID"], AGENT)
        self.assertEqual(seen["found"], (SEAT_BEARER, "token_file", SEAT_URL, ""))

    def test_no_sutando_or_host_relay_token_reaches_the_seat_but_proxy_and_ca_settings_do(self):
        decl = self.declare_seat()
        planted = {"SUTANDO_BAR": "s1", "SUTANDO_BRIDGE_LOCK": "0", "AG2_REMOTE_TOKEN": "host-ag2-remote",
                   "AG2_REMOTE_TOKEN_PRD": "host-prd", "AG2_RELAY_TOKEN": "host-relay",
                   "AG2_RELAY_TOKEN_BRIDGE_X": "host-relay-x"}
        network = {"HTTPS_PROXY": "http://proxy.example:3128", "HTTP_PROXY": "http://proxy.example:3128",
                   "NO_PROXY": "localhost", "ALL_PROXY": "socks5://proxy.example:1080",
                   "https_proxy": "http://proxy.example:3128", "http_proxy": "http://proxy.example:3128",
                   "no_proxy": "localhost", "all_proxy": "socks5://proxy.example:1080",
                   "SSL_CERT_FILE": "/etc/ssl/corp.pem", "SSL_CERT_DIR": "/etc/ssl/certs",
                   "REQUESTS_CA_BUNDLE": "/etc/ssl/corp-bundle.pem", "LOGNAME": "probe"}
        seen = {}
        with mock.patch.dict(os.environ, {**planted, **network}), recording_bridge_environ(seen):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(f"vault read {k}"),
                             run=lambda module, run_name, alter_sys: None)
        self.assertEqual(rc, 0)
        env = seen["env"]
        for key, value in planted.items():
            self.assertNotEqual(env.get(key), value, f"the host's {key} reached the seat bridge")
        self.assertEqual([k for k in env if k.startswith("SUTANDO_") and k not in cli.SEAT_ENV_KEEP], [])
        self.assertEqual({k: env.get(k) for k in network}, network, "proxy and CA settings reach the seat bridge")

    def test_the_real_bridge_module_starts_as_the_seat(self):
        decl = self.declare_seat()
        name, seen = "ag2_sparrow.remote_gateway_bridge", {}
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop(name, None)

        def start(module, run_name, alter_sys):
            local_task_protocol.set_task_stamper(None)
            mod = importlib.import_module(module)
            seen.update(token=mod.TOKEN, url=mod.URL, source=mod._FOUND.token_source, fatal=mod._FOUND.fatal)
        rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(f"vault read {k}"), run=start)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"token": SEAT_BEARER, "url": SEAT_URL, "source": "token_file", "fatal": ""})

    def test_a_seat_never_holds_a_proactive_room_or_drains_a_proactive_file(self):
        decl = self.declare_seat()
        with open(os.path.join(self.seat, ".env"), "a", encoding="utf-8") as fh:
            fh.write("REMOTE_PROACTIVE_ROOM=!seat-env-room:x\n")
        self.assertEqual(os.environ["REMOTE_PROACTIVE_ROOM"], "!owner-dm", "the host sets it too")
        results = self.root / "results"
        results.mkdir(parents=True)
        planted = results / "proactive-1784700000002.txt"
        planted.write_text("a nudge for the owner\n", encoding="utf-8")
        name, seen = "ag2_sparrow.remote_gateway_bridge", {}
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop(name, None)

        def start(module, run_name, alter_sys):
            local_task_protocol.set_task_stamper(None)
            mod = importlib.import_module(module)
            seen.update(env_room=os.environ.get("REMOTE_PROACTIVE_ROOM"), bridge_room=mod.PROACTIVE_ROOM)
            mod._post_proactive()
            seen["left"] = sorted(p.name for p in results.iterdir())
        rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(f"vault read {k}"), run=start)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"env_room": "", "bridge_room": "", "left": [planted.name]},
                         "the seat's own .env and the host never give it a proactive room, and it claims no nudge")

    def test_the_owners_seat_child_env_sets_an_empty_proactive_room_last(self):
        from ag2_sparrow import connect_bridge
        self.assertIs(cli.seat_child_env, connect_bridge.seat_child_env, "cli only re-exports the owner's")
        self.assertIs(cli.SEAT_ENV_KEEP, connect_bridge.SEAT_ENV_KEEP)
        launch = {"AGENT_MXID": AGENT, "REMOTE_PROACTIVE_ROOM": "!from-launch:x"}
        built = connect_bridge.seat_child_env(launch, {"REMOTE_PROACTIVE_ROOM": "!host:x", "HOME": "/home/probe"})
        self.assertEqual(built["REMOTE_PROACTIVE_ROOM"], "", "neither the host nor the launch values override it")
        self.assertEqual(list(built)[-1], "REMOTE_PROACTIVE_ROOM")
        self.assertEqual((built["HOME"], built["AGENT_MXID"]), ("/home/probe", AGENT))

    def test_the_bridge_itself_gives_a_seat_no_proactive_room_when_its_env_names_none(self):
        """The bridge's own is_seat guard, with no REMOTE_PROACTIVE_ROOM in the process environment to mask
        it: the seat's .env names a room, and the imported bridge still holds none."""
        decl = self.declare_seat()
        with open(os.path.join(self.seat, ".env"), "a", encoding="utf-8") as fh:
            fh.write("REMOTE_PROACTIVE_ROOM=!seat-env-room:x\n")
        launched = json.loads(decl.read_text())["env"]
        env = {k: v for k, v in cli.seat_child_env(launched).items() if k != "REMOTE_PROACTIVE_ROOM"}
        name = "ag2_sparrow.remote_gateway_bridge"
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop(name, None)
        local_task_protocol.set_task_stamper(None)
        _dirs.set_dirs(task_dir=launched["AGENT_CONNECT_TASK_DIR"], result_dir=launched["AGENT_CONNECT_RESULT_DIR"],
                       state_dir=launched["AGENT_CONNECT_STATE_DIR"])
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn("REMOTE_PROACTIVE_ROOM", os.environ)
            mod = importlib.import_module(name)
            self.assertEqual((mod.TOKEN, mod._FOUND.token_source), (SEAT_BEARER, "token_file"), "it ran as the seat")
            self.assertEqual(mod.PROACTIVE_ROOM, "", "a seat took the proactive room its .env names")

    def test_a_seat_whose_env_names_another_token_file_or_another_seat_never_runs(self):
        decl = self.declare_seat()
        env_path = os.path.join(self.seat, ".env")
        text = Path(env_path).read_text()
        Path(env_path).write_text(text.replace(os.path.join(self.seat, "token"), "/host/t"))
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[])
        self.assertEqual(specs, [])
        self.assertIn("names a token file other than its own", skipped[0])
        Path(env_path).write_text(text)
        body = json.loads(decl.read_text())
        body["env"]["AG2_DEVICE_ENV"] = "/elsewhere/.env"
        decl.write_text(json.dumps(body))
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[])
        self.assertEqual(specs, [])
        self.assertIn("env is not the canonical one", skipped[0])

    def test_a_seat_codex_local_declared_is_one_worker_and_its_non_seat_declaration_is_not(self):
        from ag2_sparrow import connect_bridge as cb
        from ag2_sparrow.adapters.codex import connect_cli as codex
        decl = self.declare_seat()
        decl.unlink()
        cfg = {"ROOT": str(self.root), "BRIDGE": "sparrowd", "AGENT_ID": AGENT,
               "TOKEN_ENV_FILE": os.path.join(self.seat, ".env"), "TOKEN_VAULT_KEY": ""}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cb.wire_bridge(cfg, codex.COMMAND, codex.BRIDGE), "")
        self.assertEqual(json.loads(decl.read_text())["written_by"], f"{codex.COMMAND} install")
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[])
        self.assertEqual((skipped, [s.name for s in specs]), ([], [cli.worker_name(SEG)]))
        decl.unlink()
        body = json.loads(self.declare().read_text())
        body["written_by"] = f"{codex.COMMAND} install"
        decl.write_text(json.dumps(body))
        specs, skipped = self.sd._agent_bridge_specs(self.ws, vault_keys=[cli.vault_key(SEG)])
        self.assertEqual(specs, [])
        self.assertIn("declares only a seat's bridge", skipped[0])

    def test_preflight_passes_a_seat_without_the_vault(self):
        self.declare_seat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(["--preflight"], workspace=self.ws, get_key=lambda k: self.fail(f"vault read {k}"))
        self.assertEqual((rc, out.getvalue()), (0, f"ok: {cli.worker_name(SEG)} (gateway from its seat's token file)\n"))

    def test_a_non_seat_declaration_still_takes_the_vault_token(self):
        decl = self.declare()
        seen = {}
        with recording_bridge_environ(seen):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=vault(),
                             run=lambda module, run_name, alter_sys: None)
        self.assertEqual(rc, 0)
        self.assertEqual((seen["env"]["REMOTE_TASK_TOKEN"], seen["env"][_sources.INJECTED_KEY]), ("tok-agent", "1"))
        self.assertNotIn("AG2_DEVICE_ENV", seen["env"])
        self.assertNotIn("REMOTE_TASK_TOKEN_FILE", seen["env"])


if __name__ == "__main__":
    unittest.main()
