#!/usr/bin/env python3
"""Tests for the Sutando Instance Manifest registry (M1).

Contract (taxonomy parts 4/5): the manifest is a small, versioned,
secret-free existence record with the Server as single writer — atomic
writes, 0600, installed_at survives rewrites, clean shutdown marks stopped,
a missing manifest never fails shutdown, and discovery (list) is file-based
so it answers with no daemon running. A crash leaves status "running"
behind BY DESIGN (manifest-running + dead socket = stale_or_crashed).

Run: python3 tests/runtime-api-instance-registry.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import instance_registry as reg  # noqa: E402


class InstanceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def test_write_and_list_roundtrip(self):
        p = reg.write_manifest("@qingyun-001:ag2.space",
                               workspace="/ws", endpoint="/run/rt.sock",
                               backend="tmux", owner="@qingyun:ag2.space")
        m = json.loads(p.read_text())
        self.assertEqual(m["schema_version"], 1)
        self.assertEqual(m["identity"]["agent_id"], "@qingyun-001:ag2.space")
        self.assertEqual(m["endpoint"], {"type": "unix", "path": "/run/rt.sock"})
        self.assertEqual(m["status"], "running")
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["identity"]["agent_id"], "@qingyun-001:ag2.space")

    def test_file_is_private_and_secret_free(self):
        p = reg.write_manifest("a1", workspace="/ws", endpoint="/s.sock")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        text = p.read_text().lower()
        for needle in ("token", "secret", "password", "key"):
            self.assertNotIn(needle, text)

    def test_installed_at_survives_rewrite_and_stop_marks_stopped(self):
        p = reg.write_manifest("a1")
        first = json.loads(p.read_text())["installed_at"]
        reg.write_manifest("a1", status="running")
        self.assertEqual(json.loads(p.read_text())["installed_at"], first)
        reg.mark_stopped("a1")
        m = json.loads(p.read_text())
        self.assertEqual(m["status"], "stopped")
        self.assertEqual(m["installed_at"], first)

    def test_mark_stopped_missing_manifest_is_noop(self):
        reg.mark_stopped("never-registered")  # must not raise

    def test_unreadable_manifest_listed_not_hidden(self):
        (Path(self.tmp.name) / "broken.json").write_text("{nope")
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["error"], "unreadable manifest")

    def test_desired_state_roundtrip_and_listing(self):
        reg.write_manifest("a1", endpoint="/s.sock")
        # no desired file -> no desired_state key
        self.assertNotIn("desired_state", reg.list_instances()[0])
        reg.write_desired_state("a1", "paused", reason="owner pause",
                                restore={"pending_tasks": True})
        d = reg.read_desired_state("a1")
        self.assertEqual(d["desired_state"], "paused")
        self.assertEqual(d["restore"], {"pending_tasks": True})
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)  # .desired.json is not an instance
        self.assertEqual(listed[0]["desired_state"], "paused")
        with self.assertRaises(ValueError):
            reg.write_desired_state("a1", "exploded")

    def test_manifest_carries_structured_launcher(self):
        reg.write_manifest("a1", launcher={"type": "command",
                                           "executable": "/x/bin/sutando",
                                           "args": ["serve"]})
        m = reg.list_instances()[0]
        self.assertEqual(m["launcher"]["args"], ["serve"])

    def _touch_launcher(self, name="fake-launch"):
        import stat as _stat
        launcher = Path(self.tmp.name) / name
        # a launcher that just creates a marker and exits 0 (readiness is
        # injected, so the launcher body is irrelevant to attachability)
        launcher.write_text("#!/bin/sh\ntouch \"$SUTANDO_STARTED_MARKER\"\n")
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        return launcher

    def test_start_not_registered_and_no_launcher(self):
        self.assertFalse(reg.start_instance("ghost", _ready=lambda m: {"attachable": True})["ok"])
        reg.write_manifest("a1", endpoint=str(Path(self.tmp.name) / "run" / "rt.sock"))
        self.assertIn("launcher", reg.start_instance(
            "a1", _ready=lambda m: {"attachable": False, "stage": "server"})["error"])

    def test_start_waits_for_attachable_then_marks_running(self):
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        launcher = self._touch_launcher()
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        # readiness flips to attachable only after the launcher marker appears
        marker = Path(self.tmp.name) / "started.marker"
        os.environ["SUTANDO_STARTED_MARKER"] = str(marker)
        ready = lambda m: {"attachable": marker.exists()} if marker.exists() \
            else {"attachable": False, "stage": "core"}
        out = reg.start_instance("a1", wait_s=8, _ready=ready)
        os.environ.pop("SUTANDO_STARTED_MARKER", None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "started")
        self.assertEqual(reg.read_desired_state("a1")["desired_state"], "running")

    def test_start_exports_manifest_instance_id_not_agent_id(self):
        # divergent ids: the manifest's instance_id must win (identity-drift fix)
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        launcher = self._touch_launcher()
        reg.write_manifest("agent-A", endpoint=str(sock), instance="inst-B",
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        captured = {}
        import subprocess as _sp
        orig = _sp.Popen
        def spy(*a, **kw):
            captured.update(kw.get("env") or {})
            return orig(*a, **kw)
        _sp.Popen = spy
        try:
            reg.start_instance("agent-A", wait_s=1, instance="inst-B",
                               _ready=lambda m: {"attachable": False, "stage": "x"})
        except Exception:
            pass
        finally:
            _sp.Popen = orig
        self.assertEqual(captured.get("SUTANDO_INSTANCE_ID"), "inst-B")

    def test_start_idempotent_when_already_attachable(self):
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": "/bin/sh",
                                     "args": [], "working_directory": self.tmp.name})
        out = reg.start_instance("a1", _ready=lambda m: {"attachable": True})
        self.assertEqual(out["state"], "already_running")

    def test_start_timeout_names_the_failing_stage(self):
        import stat as _stat
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        # a launcher that stays alive so we reach the timeout (not exit) branch
        launcher = Path(self.tmp.name) / "sleeper"
        launcher.write_text("#!/bin/sh\nsleep 5\n")
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        out = reg.start_instance(
            "a1", wait_s=1,
            _ready=lambda m: {"attachable": False, "stage": "core"})
        import subprocess
        subprocess.run(["pkill", "-f", str(launcher)], capture_output=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "core")
        self.assertIn("Core did not become attachable", out["error"])

    def test_start_injects_instance_env_from_manifest(self):
        # the launcher records its own env; assert instance vars came from the
        # manifest, not this test's shell
        sock = Path(self.tmp.name) / "run" / "q-1" / "rt.sock"
        envdump = Path(self.tmp.name) / "env.txt"
        import stat as _stat
        launcher = Path(self.tmp.name) / "dump-env"
        # Atomic: the readiness predicate below is exists(), and a bare `>` creates
        # the file EMPTY before env writes, so a rename is what makes existence imply content.
        launcher.write_text('#!/bin/sh\nenv > "%s.part" && mv "%s.part" "%s"\n'
                            % (envdump, envdump, envdump))
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        reg.write_manifest("q-1", endpoint=str(sock), instance="q-1",
                           tmux_socket="/run/q-1/tmux.sock", session="core-q1",
                           config_dir="/cfg/q-1",
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        # readiness true once the env dump has CONTENT — exists() alone flips
        # the instant the shell opens the file, before a byte is written.
        reg.start_instance("q-1", wait_s=5, instance="q-1",
                           _ready=lambda m: {"attachable": envdump.exists()
                                            and envdump.stat().st_size > 0})
        text = envdump.read_text()
        self.assertIn("SUTANDO_INSTANCE_ID=q-1", text)
        self.assertIn("SUTANDO_TMUX_SOCKET=/run/q-1/tmux.sock", text)
        self.assertIn("SUTANDO_TMUX_SESSION=core-q1", text)
        self.assertIn("CLAUDE_CONFIG_DIR=/cfg/q-1", text)

    def test_agent_id_is_filename_sanitized(self):
        p = reg.write_manifest("../evil/../../id")
        self.assertEqual(p.parent, Path(self.tmp.name))
        self.assertNotIn("/", p.name.replace(".json", ""))

    def test_empty_agent_id_is_refused(self):
        with self.assertRaises(ValueError):
            reg.write_manifest("")




class EdgeBranches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = str(
            Path(self.tmp.name) / "reg")

    def tearDown(self):
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def test_platform_default_dirs_without_env(self):
        # both platform branches are pure path computation — call them
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        got = reg.registry_dir()
        self.assertIn("sutando", str(got))
        os.environ["XDG_DATA_HOME"] = self.tmp.name
        try:
            if sys.platform != "darwin":
                self.assertTrue(str(reg.registry_dir()).startswith(self.tmp.name))
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    def test_both_platform_branches_via_patch(self):
        from unittest import mock
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        # cover BOTH platform-exclusive lines regardless of the runner's OS
        with mock.patch.object(reg.sys, "platform", "darwin"):
            self.assertIn("Application Support", str(reg.registry_dir()))
        with mock.patch.object(reg.sys, "platform", "linux"):
            os.environ["XDG_DATA_HOME"] = self.tmp.name
            try:
                self.assertTrue(
                    str(reg.registry_dir()).startswith(self.tmp.name))
            finally:
                os.environ.pop("XDG_DATA_HOME", None)

    def test_missing_registry_dir_lists_empty(self):
        self.assertEqual(reg.list_instances(), [])

    def test_desired_state_missing_is_none(self):
        self.assertIsNone(reg.read_desired_state("@nobody:x"))

    def test_attachable_stages_fail_closed(self):
        # dead endpoint -> server stage; each probe failure names its stage
        out = reg.attachable(
            {"identity": {"agent_id": "@a:x"},
             "endpoint": {"path": str(Path(self.tmp.name) / "no.sock")}})
        self.assertFalse(out["attachable"])

    def test_registry_keys_compose_agent_and_instance(self):
        # (1) two actors, both instance "default": the key is never the
        # instance id alone — distinct manifests, no overwrite
        pa = reg.write_manifest("@a:x", endpoint="/a.sock")
        pb = reg.write_manifest("@b:x", endpoint="/b.sock")
        self.assertNotEqual(pa, pb)
        self.assertEqual(json.loads(pa.read_text())["endpoint"]["path"],
                         "/a.sock")
        # (2) one actor, two instances: independent manifests, desired state
        # and lifecycle — a sibling can never mark or restore the other
        p1 = reg.write_manifest("@a:x", endpoint="/a-work.sock",
                                instance="work")
        self.assertNotEqual(pa, p1)
        self.assertEqual(json.loads(pa.read_text())["instance_id"], "default")
        self.assertEqual(json.loads(p1.read_text())["instance_id"], "work")
        reg.write_desired_state("@a:x", "paused", instance="work")
        self.assertIsNone(reg.read_desired_state("@a:x"))
        self.assertEqual(
            reg.read_desired_state("@a:x", "work")["desired_state"], "paused")
        reg.mark_stopped("@a:x", "work")
        self.assertEqual(json.loads(p1.read_text())["status"], "stopped")
        self.assertEqual(json.loads(pa.read_text())["status"], "running")
        pairs = {(m.get("identity", {}).get("agent_id"), m.get("instance_id"))
                 for m in reg.list_instances()}
        self.assertEqual(pairs, {("@a:x", "default"), ("@a:x", "work"),
                                 ("@b:x", "default")})

    def _stub_daemon(self, info_agent, health_state, info_instance=None):
        import socket as _s
        import threading
        import uuid as _uuid
        sock_path = str(Path(self.tmp.name) / f"stub-{_uuid.uuid4().hex[:6]}.sock")
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(2)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                try:
                    data = conn.recv(65536).decode()
                    if not data.strip():
                        continue
                    req = json.loads(data.splitlines()[0])
                    r = ({"agentId": info_agent,
                          **({"instanceId": info_instance}
                             if info_instance else {})}
                         if req.get("method") == "sutando.info"
                         else {"state": health_state})
                    conn.sendall(json.dumps(
                        {"jsonrpc": "2.0", "id": req["id"],
                         "result": r}).encode() + b"\n")
                finally:
                    conn.close()

        threading.Thread(target=serve, daemon=True).start()
        return sock_path, srv

    def test_attachable_identity_and_core_stages(self):
        sock, srv = self._stub_daemon("@imposter:x", "online")
        try:
            out = reg.attachable({"identity": {"agent_id": "@real:x"},
                                  "endpoint": {"path": sock}})
            self.assertEqual(out["stage"], "identity")
        finally:
            srv.close()
        sock, srv = self._stub_daemon("@real:x", "starting")
        try:
            out = reg.attachable({"identity": {"agent_id": "@real:x"},
                                  "endpoint": {"path": sock}})
            self.assertEqual(out["stage"], "core")
        finally:
            srv.close()
        sock, srv = self._stub_daemon("@real:x", "online")
        try:
            out = reg.attachable({"identity": {"agent_id": "@real:x"},
                                  "endpoint": {"path": sock}})
            self.assertTrue(out["attachable"])
        finally:
            srv.close()

    def test_attachable_rejects_sibling_instance_of_same_stand(self):
        # (3) endpoint answers the RIGHT agentId but the WRONG instanceId —
        # a stale/swapped socket must fail closed, never route work there
        sock, srv = self._stub_daemon("@real:x", "online",
                                      info_instance="other-install")
        try:
            out = reg.attachable({"identity": {"agent_id": "@real:x"},
                                  "instance_id": "mine",
                                  "endpoint": {"path": sock}})
            self.assertFalse(out["attachable"])
            self.assertEqual(out["stage"], "identity")
        finally:
            srv.close()

    def test_start_refuses_non_executable_launcher(self):
        plain = Path(self.tmp.name) / "not-exec.sh"
        plain.write_text("#!/bin/sh\n")
        reg.write_manifest("@ne:x", endpoint=str(Path(self.tmp.name) / "s.sock"),
                           launcher={"type": "process", "executable": str(plain),
                                     "args": [],
                                     "working_directory": self.tmp.name})
        out = reg.start_instance("@ne:x", wait_s=1,
                                 _ready=lambda m: {"attachable": False})
        self.assertFalse(out["ok"])
        self.assertIn("not executable", out["error"])

    def test_start_short_circuits_when_already_running(self):
        launcher = Path(self.tmp.name) / "l.sh"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        reg.write_manifest("@run:x", endpoint=str(Path(self.tmp.name) / "s.sock"),
                           launcher={"type": "process",
                                     "executable": str(launcher), "args": [],
                                     "working_directory": self.tmp.name})
        out = reg.start_instance("@run:x", wait_s=1,
                                 _ready=lambda m: {"attachable": True})
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("state"), "already_running")


class CompositeKeyInjectivity(unittest.TestCase):
    """Distinct (agent_id, instance_id) tuples must never share a durable
    filename. Two ways that broke: the delimiter could occur inside a
    component, and a lossy sanitizer mapped different components onto one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def _register(self, agent, instance, endpoint):
        return reg.write_manifest(agent, instance=instance, endpoint=endpoint)

    @staticmethod
    def _key_mod():
        import instance_key
        return instance_key

    def test_delimiter_inside_a_component_does_not_collide(self):
        delim = self._key_mod().DELIM
        first = self._register("agent", f"work{delim}er", "/one.sock")
        second = self._register(f"agent{delim}work", "er", "/two.sock")
        self.assertNotEqual(first, second)
        rows = reg.list_instances()
        self.assertEqual(len(rows), 2, f"a tuple was overwritten: {rows}")
        self.assertEqual(
            {(r["identity"]["agent_id"], r["instance_id"]) for r in rows},
            {("agent", f"work{delim}er"), (f"agent{delim}work", "er")})

    def test_legacy_double_dash_pair_does_not_collide(self):
        first = self._register("agent", "worker", "/one.sock")
        second = self._register("agent--worker", "default", "/two.sock")
        self.assertNotEqual(first, second)
        rows = reg.list_instances()
        self.assertEqual(len(rows), 2, f"a tuple was overwritten: {rows}")
        endpoints = {r["endpoint"]["path"] for r in rows}
        self.assertEqual(endpoints, {"/one.sock", "/two.sock"})

    def test_sanitizer_collision_pair_survives_as_two_rows(self):
        first = self._register("blue/red", "default", "/one.sock")
        second = self._register("blue_red", "default", "/two.sock")
        self.assertNotEqual(first, second)
        self.assertEqual(len(reg.list_instances()), 2)

    def test_key_is_reversible_for_every_shape(self):
        km = self._key_mod()
        for agent, inst in (("agent", "default"), ("agent", "worker"),
                            (f"agent{km.DELIM}work", "er"),
                            ("blue/red", "default"), ("blue_red", "x y"),
                            ("@a-1:ag2.space", "100%"), ("a%2Bb", "c")):
            with self.subTest(agent=agent, instance=inst):
                self.assertEqual(km.decode_key(km.instance_key(agent, inst)),
                                 (agent, inst))

    def test_delimiter_cannot_occur_inside_an_encoded_component(self):
        km = self._key_mod()
        self.assertNotIn(km.DELIM, km.encode_part(f"a{km.DELIM}b"))

    def test_unusable_identity_is_rejected_not_silently_rewritten(self):
        km = self._key_mod()
        for agent in ("", None, ".", "..", "x\x00y", "a" * 129):
            with self.subTest(agent=agent):
                with self.assertRaises(ValueError):
                    km.instance_key(agent, "default")

    def test_default_instance_keeps_the_bare_actor_filename(self):
        p = self._register("@a-1:ag2.space", None, "/one.sock")
        self.assertEqual(p.name, "@a-1:ag2.space.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
