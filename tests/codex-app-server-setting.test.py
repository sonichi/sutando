#!/usr/bin/env python3
"""The persisted Codex app-server choice: resolve_codex_app_server's order (env, the desktop's
runtime.json, core.codex_app_server, then on by default), persist_codex_app_server's two homes, and the
`sutando-config.sh codex-app-server[-select]` subcommands."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src import sutando_config  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sutando_config._reset_cache_for_tests()
        self.addCleanup(sutando_config._reset_cache_for_tests)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("SUTANDO_APP_SUPPORT", "SUTANDO_CODEX_APP_SERVER", "SUTANDO_DEFAULT_WORKSPACE",
                     "SUTANDO_TEST_MODE", "SUTANDO_WORKSPACE"):
            os.environ.pop(name, None)
        self.base = Path(self.tmp.name)
        # A desktop-shaped tree: <app>/engine/<checkout>, its runtime.json at <app>/runtime.json.
        self.repo = self.base / "app" / "engine" / "sutando"
        self.repo.mkdir(parents=True)
        (self.repo / "sutando.config.json").write_text("{}")
        self.workspace = self.repo / "workspace"
        self.workspace.mkdir()
        self.runtime_json = self.base / "app" / "runtime.json"

    def resolve(self):
        sutando_config._reset_cache_for_tests()
        return sutando_config.resolve_codex_app_server(self.repo)

    def layer(self, body):
        (self.workspace / "sutando.config.local.json").write_text(json.dumps(body))


class Resolve(Base):
    def test_on_by_default(self):
        self.assertTrue(self.resolve())

    def test_the_workspace_layer_turns_it_off_and_on(self):
        self.layer({"core": {"codex_app_server": False}})
        self.assertFalse(self.resolve())
        self.layer({"core": {"codex_app_server": True}})
        self.assertTrue(self.resolve())

    def test_the_desktop_choice_outranks_config(self):
        self.layer({"core": {"codex_app_server": True}})
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex", "codexAppServer": False}))
        self.assertFalse(self.resolve())
        self.runtime_json.write_text(json.dumps({"codexAppServer": True}))
        self.layer({"core": {"codex_app_server": False}})
        self.assertTrue(self.resolve())

    def test_a_non_boolean_or_unreadable_desktop_value_is_skipped(self):
        self.layer({"core": {"codex_app_server": False}})
        for body in ('{"codexAppServer": "yes"}', '{"codexAppServer": 0}', "not json", "[]"):
            self.runtime_json.write_text(body)
            self.assertFalse(self.resolve(), body)

    def test_a_non_boolean_config_value_keeps_the_default(self):
        self.layer({"core": {"codex_app_server": "false"}})
        with patch("sys.stderr"):
            self.assertTrue(self.resolve())

    def test_a_set_env_overrides_both(self):
        self.runtime_json.write_text(json.dumps({"codexAppServer": True}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = "0"
        self.assertFalse(self.resolve())
        self.runtime_json.write_text(json.dumps({"codexAppServer": False}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = "1"
        self.assertTrue(self.resolve())

    def test_an_empty_env_is_unset(self):
        self.runtime_json.write_text(json.dumps({"codexAppServer": False}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = ""
        self.assertFalse(self.resolve())


class Persist(Base):
    def test_engine_runtime_json_writers_share_one_cross_process_lock(self):
        # Another process holding <runtime.json>.lock keeps this writer from its read-merge-replace.
        import subprocess as sp
        import time as _t
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex"}))
        lock = self.runtime_json.with_name(self.runtime_json.name + ".lock")
        holder = sp.Popen([sys.executable, "-c",
                           "import fcntl,sys,time;f=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX);"
                           "print('held',flush=True);time.sleep(1.0)", str(lock)], stdout=sp.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = _t.monotonic()
        self.assertEqual(sutando_config.persist_codex_app_server(False, self.repo)[0], "updated")
        self.assertGreater(_t.monotonic() - started, 0.5, "the write waited for the lock")
        holder.wait(timeout=10)
        self.assertEqual(sutando_config.persist_desktop_runtime_selection("claude", self.repo), "updated")
        self.assertEqual(json.loads(self.runtime_json.read_text()), {"coreRuntime": "claude", "codexAppServer": False},
                         "each writer kept the other's key")

    def test_a_desktop_install_writes_runtime_json_and_keeps_its_other_keys(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex", "corePath": "/x"}))
        outcome, path = sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual((outcome, path.resolve()), ("updated", self.runtime_json.resolve()))
        self.assertEqual(json.loads(self.runtime_json.read_text()),
                         {"coreRuntime": "codex", "corePath": "/x", "codexAppServer": True})
        self.assertEqual(sutando_config.persist_codex_app_server(True, self.repo)[0], "unchanged")
        self.assertTrue(self.resolve())
        self.assertEqual(sutando_config.persist_codex_app_server(False, self.repo)[0], "updated")
        self.assertFalse(self.resolve())
        self.assertFalse((self.workspace / "sutando.config.local.json").exists())

    def test_a_desktop_runtime_json_that_is_not_an_object_is_refused(self):
        self.runtime_json.write_text("[1]")
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual(self.runtime_json.read_text(), "[1]")

    def test_without_a_desktop_the_workspace_layer_is_written(self):
        repo = self.base / "clone"
        (repo / "workspace").mkdir(parents=True)
        (repo / "sutando.config.json").write_text("{}")
        layer = repo / "workspace" / "sutando.config.local.json"
        layer.write_text(json.dumps({"core": {"runtime": "codex"}, "vault": {"enabled": False}}))
        outcome, path = sutando_config.persist_codex_app_server(True, repo)
        self.assertEqual((outcome, path.resolve()), ("updated", layer.resolve()))
        self.assertEqual(json.loads(layer.read_text()),
                         {"core": {"runtime": "codex", "codex_app_server": True}, "vault": {"enabled": False}})
        sutando_config._reset_cache_for_tests()
        self.assertTrue(sutando_config.resolve_codex_app_server(repo))
        self.assertEqual(sutando_config.persist_codex_app_server(True, repo)[0], "unchanged")

    def test_only_a_boolean_is_accepted(self):
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server("on", self.repo)  # type: ignore[arg-type]


class Edges(Base):
    """The refusals and failure paths of both readers and the writer."""

    def clone(self) -> Path:
        repo = self.base / "clone"
        (repo / "workspace").mkdir(parents=True)
        (repo / "sutando.config.json").write_text("{}")
        return repo

    def test_a_malformed_config_keeps_the_default(self):
        (self.workspace / "sutando.config.local.json").write_text("{not json")
        with patch("sys.stderr"):
            self.assertTrue(self.resolve())

    def test_missing_files_are_created_with_only_the_choice(self):
        self.assertEqual(sutando_config.persist_codex_app_server(True, self.repo)[0], "updated")
        self.assertEqual(json.loads(self.runtime_json.read_text()), {"codexAppServer": True})
        repo = self.clone()
        sutando_config.persist_codex_app_server(False, repo)
        self.assertEqual(json.loads((repo / "workspace" / "sutando.config.local.json").read_text()),
                         {"core": {"codex_app_server": False}})

    def test_an_unreadable_file_is_refused_and_left_alone(self):
        self.runtime_json.write_text("{broken")
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual(self.runtime_json.read_text(), "{broken")

    def test_a_core_key_that_is_not_an_object_is_refused(self):
        repo = self.clone()
        layer = repo / "workspace" / "sutando.config.local.json"
        layer.write_text(json.dumps({"core": "codex"}))
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server(True, repo)
        self.assertEqual(json.loads(layer.read_text()), {"core": "codex"})

    def test_a_missing_workspace_is_refused(self):
        repo = self.clone()
        shutil.rmtree(repo / "workspace")
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server(True, repo)

    def test_no_checkout_is_refused(self):
        with patch.object(sutando_config, "desktop_runtime_selection_path", return_value=None), \
             patch.object(sutando_config, "_find_repo_root", return_value=None):
            with self.assertRaises(ValueError):
                sutando_config.persist_codex_app_server(True)

    def test_a_failed_replace_leaves_the_file_and_no_temp_behind(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex"}))
        with patch.object(sutando_config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual(json.loads(self.runtime_json.read_text()), {"coreRuntime": "codex"})
        self.assertEqual(sorted(p.name for p in self.runtime_json.parent.iterdir()), ["engine", "runtime.json", "runtime.json.lock"])

class ConcurrentWriter(Base):
    """A writer that skips runtime.json.lock (the desktop's own) lands between the engine's read
    and its replace: the engine re-reads and merges, so neither key is dropped."""

    def external_write(self, path: Path, body: dict) -> None:
        tmp = path.with_name(path.name + ".desktop")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, path)

    def interleave_once(self, path: Path, body: dict):
        """_parse_json_object, with the external write made right after the engine's first read."""
        real, done = sutando_config._parse_json_object, []

        def parse(p, raw):
            out = real(p, raw)
            if not done:
                done.append(1)
                self.external_write(path, body)
            return out
        return patch.object(sutando_config, "_parse_json_object", side_effect=parse)

    def test_a_desktop_write_between_read_and_replace_survives_the_engine_write(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex"}))
        with self.interleave_once(self.runtime_json, {"coreRuntime": "codex", "corePath": "/x"}):
            self.assertEqual(sutando_config.persist_codex_app_server(False, self.repo)[0], "updated")
        self.assertEqual(json.loads(self.runtime_json.read_text()),
                         {"coreRuntime": "codex", "corePath": "/x", "codexAppServer": False})

    def test_a_desktop_toggle_between_read_and_replace_is_seen_by_the_runtime_switch(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "claude"}))
        with self.interleave_once(self.runtime_json, {"coreRuntime": "claude", "codexAppServer": False}):
            self.assertEqual(sutando_config.persist_desktop_runtime_selection("codex", self.repo), "updated")
        self.assertEqual(json.loads(self.runtime_json.read_text()),
                         {"coreRuntime": "codex", "codexAppServer": False})

    def test_a_write_landing_inside_the_engines_merge_keeps_both_keys(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex"}))
        calls = []

        def mutate(body):
            if not calls:
                self.external_write(self.runtime_json, {"coreRuntime": "codex", "codexAppServer": False})
            calls.append(dict(body))
            body["corePath"] = "/x"
            return True
        self.assertEqual(sutando_config.update_desktop_runtime_json(self.runtime_json, mutate), "updated")
        self.assertEqual(json.loads(self.runtime_json.read_text()),
                         {"coreRuntime": "codex", "codexAppServer": False, "corePath": "/x"})
        self.assertEqual(len(calls), 2, "the engine re-read the changed file and merged again")

    def test_a_file_that_keeps_changing_is_refused_not_overwritten(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex"}))
        n = [0]

        def mutate(body):
            n[0] += 1
            self.external_write(self.runtime_json, {"coreRuntime": "codex", "n": n[0]})
            body["codexAppServer"] = True
            return True
        with self.assertRaisesRegex(ValueError, "kept changing"):
            sutando_config.update_desktop_runtime_json(self.runtime_json, mutate)
        self.assertEqual(json.loads(self.runtime_json.read_text()), {"coreRuntime": "codex", "n": n[0]})
        self.assertFalse([p for p in self.runtime_json.parent.iterdir() if ".tmp." in p.name])

    def test_a_check_writes_nothing_not_even_the_lock_file(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "claude"}))
        self.assertEqual(sutando_config.persist_desktop_runtime_selection("codex", self.repo, check=True),
                         "would-update")
        self.assertEqual(sorted(p.name for p in self.runtime_json.parent.iterdir()), ["engine", "runtime.json"])

    def test_the_workspace_layer_is_written_under_its_lock_and_keeps_a_concurrent_key(self):
        layer = self.workspace / "sutando.config.local.json"
        self.layer({"core": {"runtime": "codex"}})
        with patch.object(sutando_config, "desktop_runtime_selection_path", return_value=None), \
                self.interleave_once(layer, {"core": {"runtime": "codex"}, "voice": {"on": True}}):
            outcome, written = sutando_config.persist_codex_app_server(False, self.repo)
        self.assertEqual((outcome, written.resolve()), ("updated", layer.resolve()))
        self.assertEqual(json.loads(layer.read_text()),
                         {"core": {"runtime": "codex", "codex_app_server": False}, "voice": {"on": True}})
        self.assertTrue((self.workspace / "sutando.config.local.json.lock").exists())


class Cli(unittest.TestCase):
    """The subcommands in a copied checkout, with no desktop: the workspace layer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "clone"
        for rel in ("scripts/sutando-config.sh", "scripts/python-binary.sh", "src/sutando_config.py",
                    "src/__init__.py"):
            src = REPO / rel
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copy2(src, dest)
            else:
                dest.write_text("")
        (self.root / "sutando.config.json").write_text("{}")
        (self.root / "workspace").mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("SUTANDO_APP_SUPPORT", "SUTANDO_CODEX_APP_SERVER", "SUTANDO_DEFAULT_WORKSPACE",
                                 "SUTANDO_TEST_MODE", "SUTANDO_WORKSPACE")}
        self.env["SUTANDO_PY"] = sys.executable

    def cli(self, *args, **env):
        return subprocess.run(["bash", str(self.root / "scripts/sutando-config.sh"), *args],
                              capture_output=True, text=True, env={**self.env, **env}, timeout=60)

    def test_on_with_no_key_stored_is_updated_though_the_default_was_already_on(self):
        # `unchanged` means the stored key already held the value, not that the resolved one matched.
        self.assertEqual(self.cli("codex-app-server").stdout, "1", "on by default, nothing stored")
        r = self.cli("codex-app-server-select", "on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("updated "), r.stdout)
        self.assertTrue(self.cli("codex-app-server-select", "on").stdout.startswith("unchanged "))

    def test_get_select_get(self):
        self.assertEqual(self.cli("codex-app-server").stdout, "1", "on by default")
        r = self.cli("codex-app-server-select", "off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("updated "), r.stdout)
        self.assertEqual(self.cli("codex-app-server").stdout, "0")
        self.assertEqual(self.cli("codex-app-server", SUTANDO_CODEX_APP_SERVER="1").stdout, "1")
        self.assertTrue(self.cli("codex-app-server-select", "off").stdout.startswith("unchanged "))
        self.assertEqual(self.cli("codex-app-server-select", "on").returncode, 0)
        self.assertEqual(self.cli("codex-app-server").stdout, "1")

    def test_select_needs_on_or_off(self):
        for args in ((), ("yes",)):
            r = self.cli("codex-app-server-select", *args)
            self.assertEqual(r.returncode, 2)
            self.assertIn("needs on or off", r.stderr)


if __name__ == "__main__":
    unittest.main()
