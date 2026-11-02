#!/usr/bin/env python3
"""The converted harnesses never read a credential source the outer environment names.

Each case runs a real entry point (the hermetic gateway import, room_ops' gateway tests,
bee-actions, the Hatch adapter, the relay harness, the credential owner's suite) in a child whose
environment names sentinel credential sources (a token file, a device .env, a channel dir,
a config dir holding channel .env files) and carries marked token, URL and instance or
agent identity values. An audit hook records every open, every environment handed to a
grandchild, and the process's own environment at each gateway-module import and at exit;
no sentinel file may be opened and no marked value may appear in any of them.
The Hatch adapter suite's case runs from tests/hermetic-env-sentinels-hatch-adapter.test.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import hermetic_env, isolate_os_environ
from _helpers.credential_scope import selector_names

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "tests" / "_helpers" / "sentinel_audit.py"
MARK = "hermetic-sentinel"
CHANNEL = f"{MARK}-channel"
LINE = "REMOTE_TASK_TOKEN=sentinel-file-token-not-a-secret\nREMOTE_TASK_URL=http://127.0.0.1:9\n"
# Identity and token names outside the scanned credential prefixes; the scan adds every
# GATEWAY_/RELAY_/REMOTE_TASK_/AG2_REMOTE_ token, URL and identity name the audited code reads.
IDENTITY = ("REMOTE_MEDIA_HS_TOKEN", "MATRIX_AS_TOKEN", "AGENT_MXID", "AGENT_ID", "SUTANDO_INSTANCE_ID",
            "SUTANDO_WORKER_ID", "SUTANDO_WORKER_SEAT", "SPARROW_HA_OWNER")
VALUES = tuple(sorted(set(IDENTITY) | selector_names()))

GATEWAY_IMPORT = (
    "import sys; sys.path.insert(0, %r)\n"
    "from _helpers.hermetic_gateway import isolate_then_import\n"
    "isolate_then_import()\n" % str(REPO / "tests"))


def sentinels(root: Path, values: bool = True) -> "dict[str, str]":
    """The outer environment: path selectors naming synthetic files under `root`, and every
    credential value and identity selector set to a marked value."""
    ccd = root / "claude-config"
    for ch in ("ag2space", CHANNEL):
        (ccd / "channels" / ch).mkdir(parents=True)
        (ccd / "channels" / ch / ".env").write_text(LINE)
    (root / "token").write_text(LINE)
    (root / "device.env").write_text(LINE)
    names = {"REMOTE_TASK_TOKEN_FILE": str(root / "token"), "AG2_DEVICE_ENV": str(root / "device.env"),
             "REMOTE_TASK_CHANNEL_DIR": CHANNEL, "CLAUDE_CONFIG_DIR": str(ccd)}
    if values:
        names.update({k: f"{MARK}-{k.lower()}" for k in VALUES})
    return names


def audit(target: "list[str]", timeout: int = 600, values: bool = True,
          home: "Path | None" = None, extra: "dict[str, str] | None" = None
          ) -> "tuple[dict, dict, subprocess.CompletedProcess]":
    """Run `target` under the audit hook with the sentinels (and `extra`) set; (record, sentinels, process)."""
    root = Path(tempfile.mkdtemp(prefix="hermetic-audit-")).resolve()
    names = {**sentinels(root / MARK, values), **(extra or {})}
    out = root / "audit.json"
    proc = subprocess.run([sys.executable, str(AUDIT), str(out), *target], cwd=REPO,
                          env=hermetic_env(home or root / "home", **names), capture_output=True, text=True,
                          timeout=timeout)
    return json.loads(out.read_text()), names, proc


def leaks(record: dict) -> "list[str]":
    """Each sentinel file opened (or any path under the sentinel channel name, which selects a
    directory under whichever config dir is set), and each marked value present in a child's
    environment, in this process's environment at a gateway-module import, or at exit."""
    bad = [f"opened {p}" for p in record["opens"] if MARK in p]
    envs = [("child env", e) for e in record["child_envs"]] + [("import env", e) for e in record["import_envs"]]
    envs.append(("final env", record["final_env"]))
    for where, env in envs:
        bad += [f"{where} {k}={v}" for k, v in env.items() if MARK in v]
    return bad


class Sentinels(unittest.TestCase):
    def check(self, target, expect_opens=True, values=True, extra=None):
        record, names, proc = audit(target, values=values, extra=extra)
        if expect_opens:
            self.assertTrue(record["opens"], "the audit recorded no open: the hook never ran")
            self.assertTrue(record["import_envs"], "no gateway module was imported: the case ran nothing")
        bad = leaks(record)
        self.assertFalse(bad, "\n".join(["LEAK " + b for b in bad] + [proc.stdout[-1500:], proc.stderr[-1500:]]))
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
        return proc

    def test_the_hermetic_gateway_import_reads_no_outer_selector(self):
        # Path selectors alone first: an instance or token value would steer discovery past the files.
        for values in (False, True):
            with self.subTest(values=values):
                self.check(["-c", GATEWAY_IMPORT], values=values)

    def test_test_control_gates_pass_and_no_credential_does(self):
        root = Path(tempfile.mkdtemp(prefix="hermetic-gates-")).resolve()
        names = sentinels(root / MARK)
        gates = {"CI": "1", "GITHUB_ACTIONS": "true", "SUTANDO_TEST_PIPE_POOL": "1"}
        disguised = {"SUTANDO_TEST_REMOTE_TASK_TOKEN": f"{MARK}-t", "SUTANDO_TEST_AG2_DEVICE_ENV": f"{MARK}-d"}
        with mock.patch.dict(os.environ, {**names, **gates, **disguised}):
            env = hermetic_env(root / "home")
        self.assertEqual({k: env.get(k) for k in gates}, gates)
        self.assertEqual([k for k in env if k in names or k in disguised or MARK in env[k]], [])

    def test_values_cover_every_selector_the_credential_modules_read(self):
        self.assertTrue(selector_names(), "the credential scan found nothing: its roots moved")
        self.assertLessEqual(set(selector_names()), set(VALUES))

    def test_room_ops_gateway_tests_read_no_outer_selector(self):
        for f in ("tests/room-ops-say-extra-content.test.py", "tests/turn-ledger-stop-gate.test.py"):
            with self.subTest(f=f):
                self.check([f])

    def test_bee_actions_room_requests_read_no_outer_selector(self):
        self.check(["tests/bee-actions.test.py"])

    def test_the_relay_harness_hands_its_bridges_no_outer_selector(self):
        self.check(["tests/gateway-voice-outcome-contract.test.py"])

    def test_the_credential_owner_suite_reads_no_outer_selector(self):
        self.check(["tests/gateway-credential-sources.test.py"])

    def test_channel_env_resolve_and_its_shell_children_read_no_outer_selector(self):
        self.check(["tests/channel-env-resolve.test.py"])

    def test_runtime_harness_callers_take_no_selector_or_telemetry_from_its_file(self):
        # The real callers run unchanged up to their first child, which is recorded and never started.
        harness = Path(tempfile.mkdtemp(prefix="h-")).resolve()
        marker = harness / "harness-bin"
        (harness / "environment.json").write_text(json.dumps(
            {"PATH": f"{marker}{os.pathsep}{os.environ['PATH']}", "TMPDIR": str(harness),
             "REMOTE_TASK_TOKEN": f"{MARK}-harness-token", "SUTANDO_TELEMETRY": "1"}))
        outer = {"SUTANDO_RUNTIME_SOCKET": str(harness / "api.sock"), "TMPDIR": str(harness)}
        for f in ("tests/runtime-cli-task-list-unfinished.test.py", "tests/runtime-api-request-expiry.test.py"):
            with self.subTest(f=f):
                record, _, proc = audit(["--first-child", f], timeout=60, extra=outer)
                self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
                self.assertEqual(len(record["child_envs"]), 1, "the caller started no child")
                child = record["child_envs"][0]
                self.assertTrue(child.get("PATH", "").startswith(str(marker)),
                                "the recorded child was not built from the harness file")
                self.assertFalse(leaks(record), leaks(record))
                self.assertEqual({e.get("SUTANDO_TELEMETRY") for e in (child, record["final_env"])}, {"0"})

    def test_a_suite_that_stops_all_patches_reads_no_outer_selector(self):
        self.check(["tests/core-restart-heartbeat-and-cron-hold.test.py"])


class Isolator(unittest.TestCase):
    def test_stopping_every_patch_never_restores_the_outer_environment(self):
        code = ("import os, sys; sys.path.insert(0, %r)\n"
                "from unittest import mock\n"
                "from _helpers.hermetic_env import isolate_os_environ\n"
                "assert isolate_os_environ() is os.environ\n"
                "mock.patch.stopall()\n"
                "print(os.environ.get('GATEWAY_TOKEN', 'absent'))\n" % str(REPO / "tests"))
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env=hermetic_env(GATEWAY_TOKEN=f"{MARK}-gateway-token"))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), "absent")

    def test_the_harness_file_hands_over_paths_only(self):
        harness = Path(tempfile.mkdtemp(prefix="h-")).resolve()
        (harness / "environment.json").write_text(json.dumps(
            {"PATH": "/harness/bin:" + os.environ["PATH"], "REMOTE_TASK_TOKEN": "synthetic-harness-token",
             "SUTANDO_TELEMETRY": "1", "DO_NOT_TRACK": "0"}))
        code = ("import os, subprocess, sys; sys.path.insert(0, %r)\n"
                "from _helpers.hermetic_env import isolate_os_environ, isolated_child_env, runtime_harness\n"
                "isolate_os_environ(**runtime_harness()[1])\n"
                "env = isolated_child_env(**runtime_harness()[1])\n"
                "subprocess.run([sys.executable, '-c', 'import json, os; print(json.dumps(dict(os.environ)))'], env=env)\n"
                % str(REPO / "tests"))
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env=hermetic_env(SUTANDO_RUNTIME_SOCKET=str(harness / "api.sock")))
        self.assertEqual(got.returncode, 0, got.stderr)
        child = json.loads(got.stdout)
        self.assertTrue(child["PATH"].startswith("/harness/bin:"), "the harness PATH is the positive control")
        self.assertNotIn("REMOTE_TASK_TOKEN", child)
        self.assertEqual((child["SUTANDO_TELEMETRY"], child["DO_NOT_TRACK"]), ("0", "1"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_node_path_opt_in_survives_isolation_so_the_jsdom_tests_run(self):
        modules = Path(tempfile.mkdtemp(prefix="node-")).resolve() / "node_modules"
        (modules / "jsdom").mkdir(parents=True)
        (modules / "jsdom" / "index.js").write_text("module.exports = {};\n")
        code = ("import importlib.util, sys; sys.path.insert(0, %r)\n"
                "spec = importlib.util.spec_from_file_location('tutorial', %r)\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "import os; print(os.environ.get('NODE_PATH')); print(mod._jsdom_node() is not None)\n"
                % (str(REPO / "tests"), str(REPO / "tests" / "tutorial-getting-started.test.py")))
        env = hermetic_env()
        env["NODE_PATH"] = str(modules)
        # Outside the repo, so the repo's own node_modules cannot stand in for the opt-in.
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env=env, cwd=modules.parent)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip().splitlines()[-2:], [str(modules), "True"], got.stderr)

    def test_an_unreadable_patch_registry_refuses_isolation(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from unittest import mock\n"
                "del mock._patch._active_patches\n"
                "from _helpers.hermetic_env import isolate_os_environ\n"
                "isolate_os_environ()\n" % str(REPO / "tests"))
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env=hermetic_env())
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("refusing to isolate unverified", got.stderr)


def _user_site(base: Path) -> Path:
    """Where a Python started with PYTHONUSERBASE=base looks for its user site-packages."""
    if hasattr(sysconfig, "get_preferred_scheme"):
        scheme = sysconfig.get_preferred_scheme("user")
    else:
        scheme = "osx_framework_user" if sysconfig.get_config_var("PYTHONFRAMEWORK") else f"{os.name}_user"
    return Path(sysconfig.get_path("purelib", scheme, vars={"userbase": str(base)}))


PTH = ("import os; p = os.environ.get('REMOTE_TASK_TOKEN_FILE'); "
       "p and open(%r, 'w').write(open(p).read())\n")


class UserSite(unittest.TestCase):
    """A host user site runs its .pth files before the audit hook exists, where no open is recorded."""

    def plant(self, base: Path) -> Path:
        witness = base.parent / "pth-witness"
        site_dir = _user_site(base)
        site_dir.mkdir(parents=True)
        (site_dir / "zz-sentinel-reader.pth").write_text(PTH % str(witness))
        return witness

    def test_an_outer_user_base_never_reaches_a_child(self):
        root = Path(tempfile.mkdtemp(prefix="hermetic-usersite-")).resolve()
        witness = self.plant(root / "userbase")
        with mock.patch.dict(os.environ, {"PYTHONUSERBASE": str(root / "userbase")}):
            record, _, proc = audit(["-c", GATEWAY_IMPORT])
            env = hermetic_env(root / "home")
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertFalse(witness.exists(), "a host .pth read the sentinel token file before the audit hook")
        self.assertFalse(leaks(record))
        self.assertNotIn("PYTHONUSERBASE", env)
        self.assertEqual(env.get("PYTHONNOUSERSITE"), "1")

    def test_the_home_user_site_is_disabled_too(self):
        root = Path(tempfile.mkdtemp(prefix="hermetic-usersite-")).resolve()
        home = root / "home"
        env = hermetic_env(home)
        probe = subprocess.run([sys.executable, "-c", "import site; print(site.getuserbase())"],
                               env=env, capture_output=True, text=True, check=True)
        witness = self.plant(Path(probe.stdout.strip()))
        record, _, proc = audit(["-c", GATEWAY_IMPORT], home=home)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertFalse(witness.exists(), "the temp HOME's user site ran before the audit hook")
        self.assertFalse(leaks(record))

    def test_the_planted_pth_is_live_when_the_user_site_is_on(self):
        # Control: the same plant does read the sentinel once a child enables that user site.
        root = Path(tempfile.mkdtemp(prefix="hermetic-usersite-")).resolve()
        witness = self.plant(root / "userbase")
        names = sentinels(root / MARK)
        env = hermetic_env(root / "home", PYTHONUSERBASE=str(root / "userbase"), **names)
        env.pop("PYTHONNOUSERSITE", None)
        subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=60)
        self.assertTrue(witness.exists(), "the planted .pth never ran: the control proves nothing")


class CoverageBootstrap(unittest.TestCase):
    """Under the coverage runner children import coverage from PYTHONPATH; a sitecustomize.py next
    to that install would run at startup, before the audit hook, if its directory were exposed."""

    def plant(self) -> "tuple[Path, Path, Path]":
        root = Path(tempfile.mkdtemp(prefix="hermetic-covsite-")).resolve()
        site = root / "site-packages"
        (site / "coverage").mkdir(parents=True)
        (site / "coverage" / "__init__.py").write_text("")
        witness = root / "sitecustomize-witness"
        (site / "sitecustomize.py").write_text(PTH % str(witness))
        return root, site, witness

    def runner(self, site: Path):
        fake = types.ModuleType("coverage")
        fake.__file__ = str(site / "coverage" / "__init__.py")
        return mock.patch.dict(sys.modules, {"coverage": fake}), mock.patch.dict(
            os.environ, {"SUTANDO_TEST_SUBPROCESS_COVERAGE": "1"})

    def test_a_sitecustomize_beside_coverage_never_runs_in_a_child(self):
        root, site, witness = self.plant()
        mods, env_patch = self.runner(site)
        with mods, env_patch:
            record, _, proc = audit(["-c", GATEWAY_IMPORT])
            env = hermetic_env(root / "home")
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertFalse(witness.exists(), "a sitecustomize beside coverage ran before the audit hook")
        self.assertFalse(leaks(record))
        self.assertNotIn(str(site), env["PYTHONPATH"].split(os.pathsep))
        probe = subprocess.run([sys.executable, "-c", "import coverage; print(coverage.__file__)"],
                               env=env, capture_output=True, text=True, check=True, timeout=60)
        self.assertEqual(Path(probe.stdout.strip()).resolve(), site / "coverage" / "__init__.py",
                         "the child no longer reaches coverage: the runner would lose its measurement")

    def test_the_planted_sitecustomize_is_live_when_its_directory_is_on_the_path(self):
        # Control: the directory the helper used to add runs the plant and reads the sentinel.
        root, site, witness = self.plant()
        env = hermetic_env(root / "home", PYTHONPATH=str(site), **sentinels(root / MARK))
        subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=60)
        self.assertTrue(witness.exists(), "the planted sitecustomize never ran: the control proves nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
