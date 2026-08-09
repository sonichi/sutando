#!/usr/bin/env python3
"""Tests for the trusted-capabilities skill."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "trusted-capabilities"
    / "scripts"
    / "catalog.py"
)
SPEC = importlib.util.spec_from_file_location("trusted_capabilities", SCRIPT)
catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


class FakeClient:
    def __init__(self, entries=None, contents=None, commit="newcommit"):
        self.entries = entries or []
        self.contents = contents or {}
        self.commit = commit

    def head(self, _repo):
        return "main", self.commit

    def tree(self, _repo, _commit):
        return self.entries

    def file(self, _repo, _commit, path):
        return self.contents[path]


class TrustedCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.source = catalog.Source("test", "owner/repo", "skills", "skill", True)
        self.entries = [
            {"path": "skills/demo/SKILL.md", "type": "blob", "size": 20},
            {"path": "skills/demo/scripts/run.py", "type": "blob", "size": 12},
        ]
        self.contents = {
            "skills/demo/SKILL.md": b"---\nname: demo\n---\n",
            "skills/demo/scripts/run.py": b"print('ok')\n",
        }

    def test_rejects_untrusted_source_and_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "not trusted"):
            catalog.resolve_source("not-allowlisted")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            catalog.clean_repo_path("../escape")
        with self.assertRaisesRegex(ValueError, "outside trusted root"):
            catalog.source_path(self.source, "scripts/release")

    def test_requires_skill_marker_and_enforces_file_limit(self):
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            catalog.files_under(
                "skills/demo", [{"path": "skills/demo/README.md", "type": "blob"}]
            )
        oversized = [
            {"path": "skills/demo/SKILL.md", "type": "blob", "size": 1}
        ] + [
            {"path": f"skills/demo/{number}.txt", "type": "blob", "size": 1}
            for number in range(catalog.MAX_FILES)
        ]
        with self.assertRaisesRegex(ValueError, "safety limit"):
            catalog.files_under("skills/demo", oversized)
        self.assertEqual(
            catalog.files_under(
                "src/server",
                [{"path": "src/server/main.py", "type": "blob", "size": 1}],
                require_skill=False,
            )[0]["path"],
            "src/server/main.py",
        )

    def test_atomic_install_records_pinned_provenance_and_replaces(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "demo"
            old.mkdir()
            (old / "old.txt").write_text("old")
            target = catalog.install_skill(
                self.source,
                "skills/demo",
                "abc123",
                self.entries,
                self.contents,
                root,
            )
            self.assertFalse((target / "old.txt").exists())
            self.assertEqual(
                (target / "SKILL.md").read_bytes(),
                self.contents["skills/demo/SKILL.md"],
            )
            metadata = json.loads((target / catalog.METADATA).read_text())
            self.assertEqual(metadata["commit"], "abc123")
            self.assertEqual(metadata["repo"], "owner/repo")
            self.assertFalse((root / ".demo.previous").exists())

    def test_static_assessment_surfaces_runtime_risks(self):
        findings = catalog.assess(
            self.entries,
            {
                "skills/demo/scripts/run.py": (
                    b"import subprocess\nsubprocess.run(['curl', 'https://example.com'])"
                )
            },
        )
        self.assertIn("contains executable scripts", findings)
        self.assertIn("references network access", findings)
        self.assertIn("can launch commands or evaluate code", findings)

    def test_manifest_helpers_and_risk_variants(self):
        sources = catalog.load_sources()
        self.assertTrue(sources["anthropic-skills"].installable)
        self.assertEqual(catalog.source_path(self.source, "skills/demo"), "skills/demo")
        root_source = catalog.Source("root", "owner/repo", ".", "skill", True)
        self.assertEqual(catalog.source_path(root_source, "demo"), "demo")
        tree = self.entries + [
            {"path": "skills/other/SKILL.md", "type": "blob", "size": 1},
            {"path": "README.md", "type": "blob", "size": 1},
        ]
        self.assertEqual(
            catalog.skill_paths(self.source, tree),
            ["skills/demo", "skills/other"],
        )
        risky = [
            {"path": "skills/demo/tool.bin"},
            {"path": "skills/demo/package.json"},
            {"path": "skills/demo/remove.sh"},
        ]
        findings = catalog.assess(
            risky,
            {"skills/demo/remove.sh": b"unlink('/tmp/item')"},
        )
        self.assertIn("contains native or binary files", findings)
        self.assertIn("declares external dependencies", findings)
        self.assertIn("contains destructive filesystem operations", findings)
        self.assertEqual(
            catalog.assess([{"path": "skills/demo/readme.txt"}]),
            ["no static risk signals found (manual review still required)"],
        )

    def test_file_validation_size_empty_slug_and_destinations(self):
        with self.assertRaisesRegex(ValueError, "no files"):
            catalog.files_under("src/empty", [], require_skill=False)
        with mock.patch.object(catalog, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                catalog.files_under(
                    "skills/demo",
                    [{"path": "skills/demo/SKILL.md", "type": "blob", "size": 2}],
                )
        with self.assertRaisesRegex(ValueError, "safe slug"):
            catalog.slug_for("skills/Bad_Name")
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(catalog.destination_root(temp), Path(temp).resolve())
        with mock.patch.object(
            catalog, "claude_home_path", return_value=Path("/runtime/skills")
        ):
            self.assertEqual(
                catalog.destination_root(None), Path("/runtime/skills")
            )

    def test_fetch_contents_success_and_limit(self):
        client = FakeClient(self.entries, self.contents)
        self.assertEqual(
            catalog.fetch_contents(client, self.source, "abc", self.entries),
            self.contents,
        )
        with mock.patch.object(catalog, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "download exceeded"):
                catalog.fetch_contents(
                    client, self.source, "abc", self.entries[:1]
                )

    def test_atomic_install_rolls_back_failed_replace_and_cleans_stale_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "demo"
            target.mkdir()
            (target / "old.txt").write_text("old")
            stale = root / ".demo.previous"
            stale.mkdir()
            (stale / "stale.txt").write_text("stale")
            real_replace = catalog.os.replace
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("replace failed")
                return real_replace(source, destination)

            with mock.patch.object(
                catalog.os, "replace", side_effect=fail_second
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    catalog.install_skill(
                        self.source,
                        "skills/demo",
                        "abc",
                        self.entries,
                        self.contents,
                        root,
                    )
            self.assertEqual((target / "old.txt").read_text(), "old")
            self.assertFalse(stale.exists())

    def test_github_client_happy_path_and_errors(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch.object(
            catalog.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            client = catalog.GitHubClient("https://api.example/")
            self.assertEqual(client.json("/test"), {"ok": True})
            self.assertEqual(client.api_base, "https://api.example")
            self.assertEqual(urlopen.call_args.args[0].full_url, "https://api.example/test")

        client = catalog.GitHubClient()
        with mock.patch.object(
            client,
            "json",
            side_effect=[
                {"default_branch": "main"},
                {"sha": "abc"},
                {"tree": [{"path": "x"}]},
            ],
        ):
            self.assertEqual(client.head("owner/repo"), ("main", "abc"))
            self.assertEqual(client.tree("owner/repo", "abc"), [{"path": "x"}])
        with mock.patch.object(
            client, "json", return_value={"truncated": True}
        ):
            with self.assertRaisesRegex(RuntimeError, "too large"):
                client.tree("owner/repo", "abc")
        with mock.patch.object(client, "_request", return_value=b"body") as request:
            self.assertEqual(client.file("owner/repo", "abc", "a b/x"), b"body")
            self.assertIn("/a%20b/x", request.call_args.args[0])

        http_error = urllib.error.HTTPError(
            "https://api.example", 403, "forbidden", {}, io.BytesIO(b"denied")
        )
        with mock.patch.object(
            catalog.urllib.request, "urlopen", side_effect=http_error
        ):
            with self.assertRaisesRegex(RuntimeError, r"\(403\).*denied"):
                client._request("https://api.example")
        with mock.patch.object(
            catalog.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                client._request("https://api.example")

    def test_resolve_remote_and_commands_for_skill(self):
        client = FakeClient(self.entries, self.contents)
        commit, entries, contents = catalog.resolve_remote(
            client, self.source, "skills/demo"
        )
        self.assertEqual((commit, entries, contents), ("newcommit", self.entries, self.contents))

        out = io.StringIO()
        with mock.patch.object(
            catalog, "load_sources", return_value={"test": self.source}
        ), redirect_stdout(out):
            self.assertEqual(catalog.cmd_sources(SimpleNamespace(), client), 0)
            args = SimpleNamespace(query="demo", limit=1)
            self.assertEqual(catalog.cmd_search(args, client), 0)
            inspect_args = SimpleNamespace(source="test", path="skills/demo")
            self.assertEqual(catalog.cmd_inspect(inspect_args, client), 0)
        self.assertIn("installable", out.getvalue())
        self.assertIn("test:skills/demo @ newcommit", out.getvalue())
        self.assertIn("risk findings:", out.getvalue())

    def test_search_tool_index_and_no_match(self):
        tool = catalog.Source("tools", "owner/tools", "src", "tool", False)
        index = catalog.Source("index", "owner/index", ".", "index", False)
        tool_client = FakeClient(
            [{"path": "src/filesystem/main.py", "type": "blob"}],
            {"src/filesystem/main.py": b"print('ok')"},
        )
        with mock.patch.object(
            catalog, "load_sources", return_value={"tools": tool}
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                catalog.cmd_search(
                    SimpleNamespace(query="filesystem", limit=1), tool_client
                ),
                0,
            )
            self.assertIn("discovery-only", output.getvalue())

        index_client = FakeClient(
            [{"path": "README.md", "type": "blob"}],
            {"README.md": b"x" * 80 + b" filesystem server " + b"y" * 200},
        )
        with mock.patch.object(
            catalog, "load_sources", return_value={"index": index}
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                catalog.cmd_search(
                    SimpleNamespace(query="filesystem", limit=1), index_client
                ),
                0,
            )
            self.assertIn("filesystem", output.getvalue())
            self.assertIn("…", output.getvalue())
        with mock.patch.object(
            catalog, "load_sources", return_value={"index": index}
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                catalog.cmd_search(
                    SimpleNamespace(query="absent", limit=1), index_client
                ),
                1,
            )
            self.assertIn("No matching", output.getvalue())

    def test_inspect_tool_and_reject_index(self):
        tool = catalog.Source("tools", "owner/tools", "src", "tool", False)
        client = FakeClient(
            [{"path": "src/server/main.py", "type": "blob", "size": 10}],
            {"src/server/main.py": b"print('ok')"},
        )
        with mock.patch.object(
            catalog, "load_sources", return_value={"tools": tool}
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                catalog.cmd_inspect(
                    SimpleNamespace(source="tools", path="src/server"), client
                ),
                0,
            )
            self.assertIn("files: 1", output.getvalue())
        index = catalog.Source("index", "owner/index", ".", "index", False)
        with mock.patch.object(
            catalog, "load_sources", return_value={"index": index}
        ):
            with self.assertRaisesRegex(ValueError, "is an index"):
                catalog.cmd_inspect(
                    SimpleNamespace(source="index", path="project"), client
                )

    def test_install_dry_run_write_and_discovery_rejection(self):
        reviewed = "a" * 40
        client = FakeClient(self.entries, self.contents, commit=reviewed)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            catalog, "load_sources", return_value={"test": self.source}
        ):
            base = dict(
                source="test", path="skills/demo", dest_root=temp, commit=None
            )
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    catalog.cmd_install(SimpleNamespace(**base, yes=False), client),
                    0,
                )
                self.assertIn("dry run only", output.getvalue())
                self.assertIn(f"--commit {reviewed} --yes", output.getvalue())
            with self.assertRaisesRegex(ValueError, "requires --commit"):
                catalog.cmd_install(SimpleNamespace(**base, yes=True), client)
            moved_client = FakeClient(
                self.entries, self.contents, commit="b" * 40
            )
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    catalog.cmd_install(
                        SimpleNamespace(**(base | {"commit": reviewed}), yes=True),
                        moved_client,
                    ),
                    0,
                )
                self.assertTrue((Path(temp) / "demo" / "SKILL.md").is_file())
                metadata = json.loads(
                    (Path(temp) / "demo" / catalog.METADATA).read_text()
                )
                self.assertEqual(metadata["commit"], reviewed)
                self.assertIn("installed:", output.getvalue())
        blocked = catalog.Source("blocked", "owner/repo", ".", "tool", False)
        with mock.patch.object(
            catalog, "load_sources", return_value={"blocked": blocked}
        ):
            with self.assertRaisesRegex(ValueError, "discovery-only"):
                catalog.cmd_install(
                    SimpleNamespace(
                        source="blocked",
                        path="demo",
                        dest_root=None,
                        commit=None,
                        yes=False,
                    ),
                    client,
                )

    def test_update_missing_current_dry_run_and_write(self):
        reviewed = "a" * 40
        client = FakeClient(self.entries, self.contents, commit=reviewed)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            catalog, "load_sources", return_value={"test": self.source}
        ):
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "unsafe skill slug"):
                catalog.cmd_update(
                    SimpleNamespace(
                        slug="../demo", dest_root=temp, commit=None, yes=False
                    ),
                    client,
                )
            with self.assertRaisesRegex(ValueError, "no managed install"):
                catalog.cmd_update(
                    SimpleNamespace(
                        slug="demo", dest_root=temp, commit=None, yes=False
                    ),
                    client,
                )
            target = root / "demo"
            target.mkdir()
            metadata = {
                "source": "test",
                "path": "skills/demo",
                "commit": reviewed,
            }
            (target / catalog.METADATA).write_text(json.dumps(metadata))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    catalog.cmd_update(
                        SimpleNamespace(
                            slug="demo", dest_root=temp, commit=None, yes=False
                        ),
                        client,
                    ),
                    0,
                )
                self.assertIn("up to date", output.getvalue())
            metadata["commit"] = "oldcommit"
            (target / catalog.METADATA).write_text(json.dumps(metadata))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    catalog.cmd_update(
                        SimpleNamespace(
                            slug="demo", dest_root=temp, commit=None, yes=False
                        ),
                        client,
                    ),
                    0,
                )
                self.assertIn("dry run only", output.getvalue())
                self.assertIn(f"--commit {reviewed} --yes", output.getvalue())
            with self.assertRaisesRegex(ValueError, "requires --commit"):
                catalog.cmd_update(
                    SimpleNamespace(
                        slug="demo", dest_root=temp, commit=None, yes=True
                    ),
                    client,
                )
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    catalog.cmd_update(
                        SimpleNamespace(
                            slug="demo",
                            dest_root=temp,
                            commit=reviewed,
                            yes=True,
                        ),
                        client,
                    ),
                    0,
                )
                self.assertIn("updated:", output.getvalue())

            blocked = catalog.Source(
                "test", "owner/repo", "skills", "skill", False
            )
            with mock.patch.object(
                catalog, "load_sources", return_value={"test": blocked}
            ):
                with self.assertRaisesRegex(ValueError, "no longer installable"):
                    catalog.cmd_update(
                        SimpleNamespace(
                            slug="demo",
                            dest_root=temp,
                            commit=reviewed,
                            yes=True,
                        ),
                        client,
                    )

    def test_parser_and_main_success_and_handled_error(self):
        self.assertEqual(catalog.parser().parse_args(["sources"]).command, "sources")
        with mock.patch.object(
            catalog, "load_sources", return_value={"test": self.source}
        ), mock.patch.object(catalog, "GitHubClient", return_value=FakeClient()):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(catalog.main(["sources"]), 0)
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(catalog.main(["inspect", "missing", "x"]), 2)
                self.assertIn("not trusted", error.getvalue())


if __name__ == "__main__":
    unittest.main()
