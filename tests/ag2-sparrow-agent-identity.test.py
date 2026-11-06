#!/usr/bin/env python3
"""The agent id a bridge serves and the one path segment a per-agent root is named by: the bridge and
the installers read one chain, and two distinct ids never share a segment.
Run: python3 tests/ag2-sparrow-agent-identity.test.py"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import _dirs, agent_identity as ai  # noqa: E402
from ag2_sparrow import remote_gateway_bridge as gw  # noqa: E402

SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*")


class RootSegment(unittest.TestCase):
    TABLE = [
        ("@probe.agent:ag2.space", "probe.agent_ag2.space"),
        ("@a-b.c:dev.ag2.space", "a-b.c_dev.ag2.space"),
    ]

    def test_a_plain_mxid_keeps_its_parts_and_reads_back(self):
        for agent_id, want in self.TABLE:
            seg = ai.root_segment(agent_id)
            self.assertEqual(seg, want)
            local, server = seg.split("_")
            self.assertEqual(f"@{local}:{server}", agent_id, "exactly one '_', so the id reads back")

    def test_unusual_ids_are_sanitized_with_a_digest_and_never_equal_a_plain_one(self):
        for agent_id in ("@Probe:ag2.space", "@a_b:c", "@a:b_c", "@x/y=z:s", "@a:host:8448", "agent id",
                         ".hidden", "@..:x", "@é:x", "-lead", "@a:" + "b" * 80):
            seg = ai.root_segment(agent_id)
            self.assertTrue(SEGMENT.fullmatch(seg), (agent_id, seg))
            self.assertLessEqual(len(seg), ai.SEGMENT_MAX)
            self.assertIn("__", seg, "a non-plain segment always carries the digest separator")
            self.assertFalse(seg.startswith("."))
        self.assertTrue(ai.root_segment("@Probe:ag2.space").startswith("probe_ag2.space__"))

    def test_two_ids_that_sanitize_to_one_prefix_still_differ(self):
        a, b = ai.root_segment("@a_b:c"), ai.root_segment("@a:b_c")
        self.assertEqual(a.split("__")[0], b.split("__")[0], "the same readable prefix")
        self.assertNotEqual(a, b)
        self.assertNotEqual(ai.root_segment("@x/y:s"), ai.root_segment("@x=y:s"))
        self.assertNotEqual(ai.root_segment("@Foo:x"), ai.root_segment("@foo:x"), "case-insensitive filesystems too")

    def test_distinct_ids_never_share_a_segment(self):
        ids = {f"@{loc}:{srv}" for loc in ("a", "A", "a_", "a.b", "a-b", "a b", "ab", "a__b", "a_b")
               for srv in ("x", "X", "x_y", "x.y", "x-y", "x:1")}
        segs = {ai.root_segment(i) for i in ids}
        self.assertEqual(len(segs), len(ids))

    def test_an_unavailable_id_is_refused(self):
        for empty in ("", "   ", None):
            with self.assertRaises(ValueError):
                ai.root_segment(empty)


class Resolve(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def binding(self, value):
        (self.dir / "auth").mkdir(exist_ok=True)
        (self.dir / "auth" / "ag2space.json").write_text(json.dumps({"agent_id": value}))

    def test_env_then_files_then_the_binding(self):
        self.binding("@bound:x")
        files = {"AG2SPACE_USER_ID": "@file:x"}
        self.assertEqual(ai.resolve(self.dir, files.get, {"AGENT_ID": " @env:x "}), "@env:x")
        self.assertEqual(ai.resolve(self.dir, files.get, {}), "@file:x")
        self.assertEqual(ai.resolve(self.dir, {}.get, {}), "@bound:x")
        self.assertEqual(ai.resolve(None, {}.get, {}), "", "no state dir: no binding tier")
        self.binding(7)
        self.assertEqual(ai.resolve(self.dir, {}.get, {}), "", "a non-string id is unknown, never coerced")
        (self.dir / "auth" / "ag2space.json").write_text("{not json")
        self.assertEqual(ai.enrolled(self.dir), "")

    def test_a_seat_is_strict_and_never_reads_the_hosts_binding(self):
        self.binding("@core:x")
        seat = {"REMOTE_TASK_SEAT": "1"}
        self.assertEqual(ai.resolve(self.dir, {"AGENT_MXID": "@seat:x"}.get, seat), "@seat:x")
        self.assertEqual(ai.resolve(self.dir, {}.get, seat), "", "a seat with no identity of its own is unknown")
        self.assertEqual(ai.resolve(self.dir, {}.get, {}, strict=True), "")
        self.assertEqual(ai.resolve(self.dir, {}.get, seat, strict=False), "@core:x", "strict is the caller's to override")
        self.assertEqual(ai.resolve(self.dir, {}.get, {}), "@core:x", "the core keeps its binding tier")

    def test_env_files_skip_what_cannot_be_read_and_keep_an_explicit_blank(self):
        good, bad, blank = self.dir / "a.env", self.dir / "b.env", self.dir / "c.env"
        good.write_text("# note\n\nAGENT_MXID='@q:x'\nnoequals\n")
        bad.write_bytes(b"\xff\xfe")
        blank.write_text("AGENT_MXID=\n")
        files = ai.env_files([None, str(self.dir / "missing"), str(bad), str(blank), str(good)])
        self.assertEqual([p for p, _ in files], [str(blank), str(good)])
        self.assertEqual(ai.file_value(files, "AGENT_MXID"), "", "the first file carrying the key decides")
        self.assertEqual(ai.file_value(ai.env_files([str(good)]), "AGENT_MXID"), "@q:x")
        self.assertEqual(ai.file_value(files, "NONE"), "")
        self.assertIsNone(ai.unquote_env(None))


class BridgeReadsTheSameChain(unittest.TestCase):
    def test_the_bridge_delegates_its_identity_and_unquoting(self):
        self.assertIs(gw._unquote_env, ai.unquote_env)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gw, "_STATE", Path(d)), \
                mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": os.path.join(d, "relay.env")}):
            for k in ("AGENT_MXID", "AGENT_ID"):
                os.environ.pop(k, None)
            Path(d, "relay.env").write_text("AGENT_ID=@from-file:x\n")
            self.assertEqual(gw._reenroll_identity(), "@from-file:x")
            self.assertEqual(gw._config_from_channel_env("AGENT_ID"), "@from-file:x")
            self.assertEqual(gw._config_from_channel_env("NONE"), "")

    def test_the_gateway_status_names_the_dirs_the_bridge_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            status = Path(d) / "gateway-status.json"
            with mock.patch.object(gw, "GATEWAY_STATUS_FILE", status), \
                    mock.patch.object(gw, "TASKS_DIR", Path(d) / "tasks"), \
                    mock.patch.object(gw, "RESULTS_DIR", Path(d) / "results"):
                gw._emit_gateway_status(True)
            rec = json.loads(status.read_text())
            self.assertEqual((rec["tasks_dir"], rec["results_dir"]),
                             (os.path.realpath(Path(d) / "tasks"), os.path.realpath(Path(d) / "results")))


class PinnedLaunch(unittest.TestCase):
    """A launcher's pin decides the bridge's identity at every tier: the saved MXID, or none."""
    SAVED = "@saved:ag2.space"

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.state, self.device = d / "state", d / "device.env"
        self.channel = d / "claude" / "channels" / "ag2space" / ".env"
        self.record = self.state / "auth" / "ag2space.json"
        self.channel.parent.mkdir(parents=True)
        self.record.parent.mkdir(parents=True)
        self.caller = {"AG2_DEVICE_ENV": str(self.device), "CLAUDE_CONFIG_DIR": str(d / "claude"),
                       "AGENT_MXID": "@caller-mxid:ag2.space", "AGENT_ID": "@caller-id:ag2.space"}

    def bridge(self, env):
        with mock.patch.object(gw, "_STATE", self.state), mock.patch.dict(os.environ, env, clear=True):
            return gw._reenroll_identity()

    def plant(self, tiers):
        for path in (self.device, self.channel, self.record):
            path.unlink(missing_ok=True)
        for n, (path, key) in enumerate(tiers):
            value = f"@foreign-{n}:ag2.space"
            if path == self.record:
                path.write_text(json.dumps({"agent_id": value}))
            else:
                with path.open("a") as fh:
                    fh.write(f"{key}={value}\n")
        return "@foreign-0:ag2.space"

    def test_every_file_alias_in_both_candidates_and_the_binding_yield_to_the_pin(self):
        tiers = [(f, k) for f in (self.device, self.channel) for k in ai.FILE_KEYS] + [(self.record, "agent_id")]
        unpinned = {k: v for k, v in self.caller.items() if k not in ai.ENV_KEYS}
        for tier in tiers + [None]:
            with self.subTest(tier=tier and (tier[0].name, tier[1])):
                foreign = self.plant([tier] if tier else tiers)
                self.assertEqual(self.bridge(unpinned), foreign, "the planted tier is live without a pin")
                self.assertEqual(self.bridge(ai.launch_environ(self.caller, "")), "", "no saved MXID: no identity")
                self.assertEqual(self.bridge(ai.launch_environ(self.caller, self.SAVED)), self.SAVED)

    def test_a_pin_reads_agent_mxid_alone_and_replaces_every_identity_variable(self):
        env = ai.launch_environ({**self.caller, ai.PINNED_KEY: ""}, "")
        self.assertEqual(env, {"AG2_DEVICE_ENV": str(self.device), "CLAUDE_CONFIG_DIR": self.caller["CLAUDE_CONFIG_DIR"],
                               ai.PINNED_KEY: "1"})
        self.assertIn("AGENT_ID", self.caller, "the caller's mapping is not changed")
        self.assertEqual(ai.launch_environ({}, self.SAVED), {ai.PINNED_KEY: "1", "AGENT_MXID": self.SAVED})
        self.assertEqual(ai.pinned_environ(""), {ai.PINNED_KEY: "1"})
        self.assertEqual(ai.resolve(None, {"AGENT_MXID": "@f:x"}.get, {ai.PINNED_KEY: "1", "AGENT_ID": "@a:x"}), "")


class Dirs(unittest.TestCase):
    def tearDown(self):
        _dirs._injected = None

    def test_agents_dir_and_bridge_host_are_injected_or_default(self):
        _dirs._injected = None
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            os.environ.pop("AGENT_CONNECT_TASK_DIR", None)
            self.assertEqual(_dirs.agents_dir(), Path(home) / ".ag2-sparrow" / "agents")
            self.assertEqual(_dirs.bridge_host(), "")
            self.assertFalse(_dirs.task_dir_configured())
            os.environ["AGENT_CONNECT_TASK_DIR"] = "/x/tasks"
            self.assertTrue(_dirs.task_dir_configured())
        _dirs.set_dirs(state_dir="/ws/state", agents_dir="/ws/agents", bridge_host="sparrowd")
        self.assertEqual((_dirs.agents_dir(), _dirs.bridge_host(), _dirs.state_dir()),
                         (Path("/ws/agents"), "sparrowd", Path("/ws/state")))
        self.assertFalse(_dirs.task_dir_configured(), "the host injects no task dir")
        _dirs.set_dirs(task_dir="/ws/tasks")
        self.assertTrue(_dirs.task_dir_configured())


class SutandoEntry(unittest.TestCase):
    def test_the_entry_injects_the_workspace_and_runs_the_package_cli(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sparrow_claude_local_entry",
                                                      _REPO / "src" / "sparrow-claude-local.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seen = {}

        def fake_main(argv):
            seen.update(argv=argv, agents=_dirs.agents_dir(), host=_dirs.bridge_host(), state=_dirs.state_dir())
            return 3
        try:
            with mock.patch.object(mod, "resolve_workspace", return_value=Path("/ws")), \
                    mock.patch("ag2_sparrow.adapters.claude_code.cli.main", fake_main):
                self.assertEqual(mod.main(["verify"]), 3)
        finally:
            _dirs._injected = None
        self.assertEqual(seen, {"argv": ["verify"], "agents": Path("/ws/agents"), "host": mod.BRIDGE_HOST,
                                "state": Path("/ws/state")})


if __name__ == "__main__":
    unittest.main()
