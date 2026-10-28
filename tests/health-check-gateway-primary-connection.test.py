#!/usr/bin/env python3
"""Health and status readers follow the connections record's primary row, for any set of servers.

A moved install (s.space primary, the ag2.space row parked) leaves the legacy
`gateway-status.json` to go stale by design: no reader may warn on it. An install whose only
gateway is a connection row (no legacy token anywhere) is configured. With three servers the
primary is whichever row says so (dev.s.space here), never a server named in code.

Run: python3 tests/health-check-gateway-primary-connection.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import ag2space_links, connections, dotspace  # noqa: E401,E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


ciw = _load("ciw_primary", REPO / "src" / "core-input-watch.py")
ss = _load("ss_primary", REPO / "src" / "services_status.py")
hc = _load("hc_primary", REPO / "src" / "health-check.py")
rh = _load("rh_primary", REPO / "src" / "runtime-health.py")
rd = _load("rundir_primary", REPO / "src" / "runtime-api" / "rundir.py")


def row(server, instance, *, primary=False, **extra):
    ref = connections.token_ref_for(instance)
    out = {"server": server, "instance": instance, "gateway_url": f"https://chat.{server}",
           "token_ref": ref, "primary": primary, "added_by": "login" if instance else "env",
           "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity",
           "agent_mxid": f"@agent:{server}"}
    out.update(extra)
    return out


class Install:
    """A temp workspace holding a connections record and the bridges' sidecars."""

    def __init__(self, case, rows, **record):
        self.ws = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.state = self.ws / "state"
        self.state.mkdir()
        if rows is not None:
            payload = {"schema_version": 1, "connections": rows, **record}
            (self.state / "connections.json").write_text(json.dumps(payload))
        patcher = mock.patch.object(hc, "WORKSPACE_DIR", self.ws)
        patcher.start()
        case.addCleanup(patcher.stop)

    def sidecar(self, instance, *, connected=True, age_s=5.0):
        now = time.time()
        (self.state / connections.sidecar_name(instance)).write_text(json.dumps(
            {"connected": connected, "ts": now - age_s, "last_ok_ts": now - age_s if connected else None}))


def bridge_check(case):
    with mock.patch.object(hc, "_gateway_configured", return_value=True), \
         mock.patch.object(hc, "probe_pids", return_value=(["4242"], True)), \
         mock.patch.object(hc, "_gateway_lock_pids", return_value={}):
        return hc.check_gateway_bridge()


class MovedInstall(unittest.TestCase):
    def test_parked_legacy_row_with_a_stale_status_file_raises_no_warning(self):
        inst = Install(self, [row("ag2.space", "", parked=True), row("s.space", "s-space", primary=True)])
        inst.sidecar("", age_s=86400)  # the parked legacy bridge's last word, a day old
        inst.sidecar("s-space")
        r = bridge_check(self)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["detail"], "running + connected (s.space)")
        self.assertNotIn("may not be delivered", r["detail"])

    def test_a_parked_named_row_is_no_stalled_lane(self):
        inst = Install(self, [row("ag2.space", "ag2-space", parked=True), row("s.space", "s-space", primary=True)])
        inst.sidecar("ag2-space", age_s=86400)
        inst.sidecar("s-space")
        self.assertEqual(hc._gateway_stale_lanes(), [])
        self.assertEqual(bridge_check(self)["status"], "ok")

    def test_the_primary_going_silent_is_still_reported_by_its_server(self):
        inst = Install(self, [row("ag2.space", "", parked=True), row("s.space", "s-space", primary=True)])
        inst.sidecar("s-space", age_s=3600)
        r = bridge_check(self)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("s.space messages may not be delivered", r["detail"])

    def test_runtime_identity_reads_the_primary_sidecar(self):
        inst = Install(self, [row("ag2.space", "", parked=True), row("s.space", "s-space", primary=True)])
        self.assertEqual(hc._gateway_primary_sidecar().name, "gateway-status.s-space.json")
        (inst.state / "gateway-status.json").write_text("{not json")  # the parked bridge's leftovers
        self.assertIn("nothing to verify", hc.check_runtime_identity()["detail"])

    def test_without_a_record_the_legacy_sidecar_is_the_primary(self):
        Install(self, None)
        self.assertEqual(hc._gateway_primary_sidecar().name, "gateway-status.json")
        self.assertEqual(hc._gateway_connected_detail(), "running + connected")


class SiblingReaders(unittest.TestCase):
    """core-input-watch and the services row read the primary's sidecar through one helper."""

    ROWS = [row("ag2.space", "", parked=True), row("s.space", "s-space", primary=True)]

    def test_the_helper_names_the_primary_sidecar_or_the_unsuffixed_one(self):
        inst = Install(self, self.ROWS)
        self.assertEqual(connections.primary_sidecar_name(inst.state), "gateway-status.s-space.json")
        bare = Install(self, None)
        self.assertEqual(connections.primary_sidecar_name(bare.state), "gateway-status.json")

    def test_an_unreadable_record_names_the_unsuffixed_sidecar(self):
        inst = Install(self, None)
        (inst.state / "connections.json").write_text("{not json")
        self.assertEqual(connections.primary_sidecar_name(inst.state), "gateway-status.json")

    def test_gateway_alive_follows_the_primary_not_the_parked_legacy_file(self):
        inst = Install(self, self.ROWS)
        inst.sidecar("", connected=False, age_s=5)  # parked bridge, fresh but down
        inst.sidecar("s-space")
        with mock.patch.object(ciw, "_pgrep", side_effect=AssertionError("fell back to pgrep")):
            self.assertIs(ciw.gateway_alive("", str(inst.state)), True)

    def test_gateway_alive_reads_down_from_the_primary_sidecar(self):
        inst = Install(self, self.ROWS)
        inst.sidecar("", age_s=5)
        inst.sidecar("s-space", connected=False)
        with mock.patch.object(ciw, "_pgrep", side_effect=AssertionError("fell back to pgrep")):
            self.assertIs(ciw.gateway_alive("", str(inst.state)), False)

    def test_services_row_probes_the_primary_sidecar(self):
        inst = Install(self, self.ROWS)
        with mock.patch.object(ss, "STATE_DIR", inst.state):
            spec = next(s for s in ss.service_registry() if s["id"] == "gateway")
        self.assertEqual(spec["probe"][1], inst.state / "gateway-status.s-space.json")
        inst.sidecar("s-space")
        status, _, _ = ss.probe_gateway(spec["probe"][1], "x", time.time(), pgrep=lambda pat: [])
        self.assertEqual(status, "running")

    def test_lane_label_and_skip_use_stored_rows_only(self):
        Install(self, self.ROWS + [row("dev.s.space", "dev", disabled=True)])
        with mock.patch.object(connections, "status_for", side_effect=AssertionError("probed")):
            self.assertEqual(hc._gateway_lane_label("dev"), "dev (dev.s.space)")
            skip = hc._gateway_lanes_not_served(hc.WORKSPACE_DIR / "state")
        self.assertTrue({"", "s-space", "dev"} <= skip)


class ThreeServers(unittest.TestCase):
    ROWS = [row("ag2.space", "", parked=True), row("s.space", "s-space"),
            row("dev.s.space", "dev-s-space", primary=True)]

    def test_dev_s_space_primary_is_read_as_the_primary_and_s_space_as_a_lane(self):
        inst = Install(self, self.ROWS)
        inst.sidecar("", age_s=86400)
        inst.sidecar("dev-s-space")
        inst.sidecar("s-space", age_s=3600)
        self.assertEqual(hc._gateway_primary_sidecar().name, "gateway-status.dev-s-space.json")
        self.assertEqual(hc._gateway_lanes_not_served(inst.state), {"", "dev-s-space"})
        self.assertEqual([ln for ln, _, _ in hc._gateway_lane_verdicts()], [], "only s-space is a lane, and it is stale")
        r = bridge_check(self)
        self.assertEqual(r["status"], "warn", r)
        self.assertTrue(r["detail"].startswith("running + connected (dev.s.space), but lane s-space (s.space)"), r)

    def test_primary_server_drives_dotspace_defaults(self):
        inst = Install(self, self.ROWS)
        primary = connections.primary_server(inst.ws)
        self.assertEqual(primary, "dev.s.space")
        self.assertEqual(dotspace.current_server({}, primary_server=primary), "dev.s.space")
        self.assertEqual(dotspace.default_link_via({}, primary_server=primary), "dev.s.space")


class Configured(unittest.TestCase):
    def _env(self):
        return mock.patch.dict(os.environ, {"REMOTE_TASK_TOKEN": "", "AG2_REMOTE_TOKEN": ""})

    def test_s_space_only_install_counts_as_configured(self):
        inst = Install(self, [row("s.space", "s-space", primary=True)])
        missing = inst.ws / "no-channel" / ".env"
        with self._env(), mock.patch.object(hc, "claude_home_path", return_value=missing):
            self.assertIs(hc._gateway_configured(), True)
        read = connections.enabled_rows
        with self._env(), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(inst.ws)}), \
                mock.patch.object(rh.connections, "enabled_rows", lambda: read(inst.ws)):
            self.assertIs(rh._gateway_configured(), True)
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "connections.py"), "--workspace",
                              str(inst.ws), "enabled"], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, "s-space\n"))

    def test_parked_or_disabled_rows_alone_configure_nothing(self):
        inst = Install(self, [row("ag2.space", "", parked=True), row("s.space", "s-space", disabled=True)])
        missing = inst.ws / "no-channel" / ".env"
        with self._env(), mock.patch.object(hc, "claude_home_path", return_value=missing):
            self.assertIs(hc._gateway_configured(), False)
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "connections.py"), "--workspace",
                              str(inst.ws), "enabled"], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (1, ""))


class Readers(unittest.TestCase):
    def test_read_helpers_never_import_from_the_environment(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        with mock.patch.dict(os.environ, {"REMOTE_TASK_TOKEN": "https://chat.example.invalid|s"}):
            self.assertEqual((connections.read_rows(ws), connections.primary_row(ws),
                              connections.primary_server(ws), connections.enabled_rows(ws)), ([], None, "", []))
        self.assertFalse((ws / "state" / "connections.json").exists())

    def test_unreadable_record_reads_as_none(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        (ws / "state").mkdir()
        (ws / "state" / "connections.json").write_text("{not json")
        self.assertEqual((connections.primary_row(ws), connections.enabled_rows(ws)), (None, []))

    def test_current_server_precedence(self):
        self.assertEqual(dotspace.current_server({"AGENT_MXID": "@a:ag2.space"}, primary_server="s.space"), "ag2.space")
        self.assertEqual(dotspace.current_server({}, "https://chat.ag2.space", primary_server="s.space"), "chat.ag2.space")
        self.assertEqual(dotspace.current_server({}, primary_server="S.Space"), "s.space")
        self.assertEqual(dotspace.current_server({}, primary_server="not a/server"), "")
        self.assertEqual(dotspace.current_server({}), "")

    def test_default_link_via_falls_back_to_legacy_only_without_a_primary(self):
        self.assertEqual(dotspace.default_link_via({}, primary_server="s.space"), "s.space")
        self.assertEqual(dotspace.default_link_via({}), dotspace.legacy_dotspace().link_via)

    def test_cloud_origin_from_primary_stays_within_config(self):
        self.assertEqual(dotspace.default_cloud_origin({}, primary_server="dev.ag2.space"),
                         "https://dev-sutando.ag2.space")
        self.assertEqual(dotspace.default_cloud_origin({}, primary_server="unregistered.example"),
                         dotspace.PRODUCTION_CLOUD_ORIGIN)

    def test_links_use_the_primary_server(self):
        with mock.patch.object(connections, "primary_server", return_value="dev.s.space"):
            self.assertEqual(ag2space_links.default_via(), "dev.s.space")
        with mock.patch.object(connections, "primary_server", side_effect=OSError("boom")):
            self.assertEqual(ag2space_links.default_via(), dotspace.default_link_via())

    def test_rundir_reports_the_primary_agent_then_the_enrollment_record(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        state = ws / "state"
        (state / "auth").mkdir(parents=True)
        (state / "auth" / "ag2space.json").write_text(json.dumps({"agent_id": "@enrolled:ag2.space"}))
        self.assertEqual(rd.enrolled_agent_id(state), "@enrolled:ag2.space")
        (state / "connections.json").write_text(json.dumps({"schema_version": 1, "connections": [
            row("ag2.space", "", parked=True), row("s.space", "s-space", primary=True)]}))
        self.assertEqual(rd.enrolled_agent_id(state), "@agent:s.space")
        with mock.patch.object(connections, "primary_row", side_effect=RuntimeError("unreadable")):
            self.assertEqual(rd.enrolled_agent_id(state), "@enrolled:ag2.space")


if __name__ == "__main__":
    unittest.main()
