#!/usr/bin/env python3
"""Tests for skills/marketplace (agent-driven Marketplace installs).

The cloud is a scripted fake; bundles are built in-memory. Nothing touches the
network or the real Claude config dir.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "marketplace" / "scripts" / "marketplace.py"
spec = importlib.util.spec_from_file_location("marketplace", SCRIPT)
marketplace = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(marketplace)
cloud_auth = marketplace.cloud_auth
skill_install = marketplace.skill_install

SKILL_UUID = "11111111-1111-1111-1111-111111111111"
PAID_UUID = "22222222-2222-2222-2222-222222222222"
TOOL_UUID = "33333333-3333-3333-3333-333333333333"


def bundle(files, wrapper=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{wrapper}/{name}" if wrapper else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = bundle({"SKILL.md": "---\nname: demo\n---\n", "manifest.json": '{"version": "1.2.0"}'}, wrapper="demo")


class FakeContext(marketplace.Context):
    def __init__(self, dest_root, catalog=None, inventory=None, me=None, agent_id="@bot:ag2.space"):
        super().__init__("https://sutando.ag2.space", "sutk_test", Path(dest_root), agent_id)
        self.calls = []
        self.catalog_items = catalog or []
        self.inventory_body = inventory or {"installed": [], "cloudTools": [], "connectors": []}
        self.me_body = me or {"plan": "free", "walletCredits": 100}
        self.install_responses = {}
        self.install_errors = {}
        self.bundles = {}

    def http(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/api/station/catalog"):
            return {"items": self.catalog_items, "featuredConnectors": [{"id": "gmail", "slug": "gmail", "name": "Gmail", "kind": "connector"}]}
        if path == "/api/me":
            return self.me_body
        if path == "/api/me/inventory":
            return self.inventory_body
        if path.endswith("/install"):
            ident = path.split("/")[3]
            if ident in self.install_errors:
                raise self.install_errors[ident]
            return self.install_responses[ident]
        if path.endswith("/uninstall"):
            return {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")

    def download(self, url):
        return self.bundles[url]


def item(uuid, slug, kind="skill", credits=0, model="free", acquired=False, tier="free"):
    return {"id": uuid, "slug": slug, "name": slug, "kind": kind, "acquired": acquired,
            "tierRequired": tier, "price": {"model": model, "credits": credits}}


def run(ctx, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = marketplace.main(argv + ["--json"], ctx=ctx)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, **kw):
        return FakeContext(self.root, **kw)

    def test_plan_writes_nothing_and_free_plan_exits_zero(self):
        ctx = self.ctx(catalog=[item(SKILL_UUID, "demo"), item(TOOL_UUID, "leads", kind="cloud_tool")])
        code, out = run(ctx, ["install", "demo", "leads"])
        self.assertEqual(code, marketplace.EXIT_OK)
        self.assertFalse(out["confirm_required"])
        self.assertEqual([i["action"] for i in out["plan"]["items"]], ["install", "activate"])
        self.assertFalse(any(p.endswith("/install") for _, p, _ in ctx.calls))
        self.assertFalse((self.root / "demo").exists())

    def test_paid_plan_requires_confirmation(self):
        ctx = self.ctx(catalog=[item(PAID_UUID, "pricey", credits=40, model="one_time")])
        code, out = run(ctx, ["install", "pricey"])
        self.assertEqual(code, marketplace.EXIT_CONFIRM)
        self.assertEqual(out["plan"]["total_credits"], 40)

    def test_owned_paid_item_costs_nothing(self):
        ctx = self.ctx(catalog=[item(PAID_UUID, "pricey", credits=40, model="one_time", acquired=True)])
        code, out = run(ctx, ["install", "pricey"])
        self.assertEqual(code, marketplace.EXIT_OK)
        self.assertEqual(out["plan"]["items"][0]["action"], "download")

    def test_install_posts_uuid_not_slug_and_equips_agent(self):
        ctx = self.ctx(catalog=[item(SKILL_UUID, "demo")])
        ctx.install_responses[SKILL_UUID] = {"ok": True, "bundleUrl": "https://b/demo", "signingHash": hashlib.sha256(GOOD).hexdigest(), "version": "1.2.0"}
        ctx.bundles["https://b/demo"] = GOOD
        code, out = run(ctx, ["install", "demo", "--yes"])
        self.assertEqual(code, marketplace.EXIT_OK)
        posts = [(p, b) for m, p, b in ctx.calls if m == "POST"]
        self.assertEqual(posts, [(f"/api/skills/{SKILL_UUID}/install", {"agentId": "@bot:ag2.space"})])
        target = self.root / "demo"
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertFalse(target.is_symlink())
        prov = json.loads((target / ".sutando-source.json").read_text())
        self.assertEqual((prov["source"], prov["skill_id"], prov["version"]), ("marketplace", SKILL_UUID, "1.2.0"))
        self.assertEqual(out["installed"], ["demo"])
        self.assertFalse(out["restart_required"])

    def test_no_agent_id_sends_no_body(self):
        ctx = self.ctx(catalog=[item(TOOL_UUID, "leads", kind="cloud_tool")], agent_id=None)
        ctx.install_responses[TOOL_UUID] = {"ok": True}
        run(ctx, ["install", "leads", "--yes"])
        self.assertIn(("POST", f"/api/skills/{TOOL_UUID}/install", None), ctx.calls)

    def test_restart_required_only_for_newly_activated_tools(self):
        ctx = self.ctx(catalog=[item(TOOL_UUID, "leads", kind="cloud_tool")])
        ctx.install_responses[TOOL_UUID] = {"ok": True}  # fresh install: no `deduped` key
        code, out = run(ctx, ["install", "leads", "--yes"])
        self.assertEqual(out["activated"], ["leads"])
        self.assertTrue(out["restart_required"])
        self.assertIn("scroll down to Runtime, and click Restart engine", out["restart_hint"])

        ctx = self.ctx(catalog=[item(TOOL_UUID, "leads", kind="cloud_tool")])
        ctx.install_responses[TOOL_UUID] = {"ok": True, "deduped": True}
        code, out = run(ctx, ["install", "leads", "--yes"])
        self.assertFalse(out["restart_required"])

    def test_already_owned_tool_is_not_reposted(self):
        ctx = self.ctx(catalog=[item(TOOL_UUID, "leads", kind="cloud_tool", acquired=True)])
        code, out = run(ctx, ["install", "leads", "--yes"])
        self.assertEqual(out["already"], ["leads"])
        self.assertFalse(any(m == "POST" for m, _, _ in ctx.calls))

    def test_checksum_mismatch_installs_nothing(self):
        ctx = self.ctx(catalog=[item(SKILL_UUID, "demo")])
        ctx.install_responses[SKILL_UUID] = {"bundleUrl": "https://b/demo", "signingHash": "0" * 64}
        ctx.bundles["https://b/demo"] = GOOD
        code, out = run(ctx, ["install", "demo", "--yes"])
        self.assertEqual(code, marketplace.EXIT_FAILED)
        self.assertIn("checksum mismatch", out["failed"][0]["reason"])
        self.assertFalse((self.root / "demo").exists())

    def test_unsigned_bundle_refused(self):
        ctx = self.ctx(catalog=[item(SKILL_UUID, "demo")])
        ctx.install_responses[SKILL_UUID] = {"bundleUrl": "https://b/demo", "signingHash": None}
        ctx.bundles["https://b/demo"] = GOOD
        code, out = run(ctx, ["install", "demo", "--yes"])
        self.assertIn("unsigned", out["failed"][0]["reason"])
        self.assertFalse((self.root / "demo").exists())

    def test_cloud_errors_map_to_plain_reasons_and_rest_continue(self):
        ctx = self.ctx(catalog=[item(PAID_UUID, "pricey", credits=40, model="one_time"), item(TOOL_UUID, "leads", kind="cloud_tool")])
        ctx.install_errors[PAID_UUID] = cloud_auth.CloudError(402, "insufficient_credits", "", {"error": "insufficient_credits", "required": 40})
        ctx.install_responses[TOOL_UUID] = {"ok": True}
        code, out = run(ctx, ["install", "pricey", "leads", "--yes"])
        self.assertEqual(code, marketplace.EXIT_FAILED)
        self.assertIn("needs 40 credits", out["failed"][0]["reason"])
        self.assertEqual(out["activated"], ["leads"])

    def test_error_explanations(self):
        e = marketplace.explain_error
        self.assertIn("sign in again", e(cloud_auth.CloudError(401, "unauthenticated")))
        self.assertIn("requires the pro plan (you're on free)", e(cloud_auth.CloudError(403, "tier_required", "", {"tierRequired": "pro"}), "free"))

    def test_unknown_connector_tier_and_bundled_are_skipped(self):
        (self.root / "engine-skill").parent.mkdir(parents=True, exist_ok=True)
        real = Path(self.tmp.name) / "_engine"
        real.mkdir()
        os.symlink(real, self.root / "bundled")
        ctx = self.ctx(catalog=[item(SKILL_UUID, "bundled"), item(PAID_UUID, "prothing", tier="pro")])
        code, out = run(ctx, ["install", "nope", "gmail", "prothing", "bundled"])
        reasons = {i["slug"]: i.get("reason", "") for i in out["plan"]["items"]}
        self.assertIn("not found", reasons["nope"])
        self.assertIn("connector", reasons["gmail"])
        self.assertIn("pro plan", reasons["prothing"])
        self.assertIn("bundled", reasons["bundled"])
        self.assertTrue((self.root / "bundled").is_symlink())


class TestStatusUpdateUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_skill(self, slug, version=None, disabled=False, source=None):
        d = self.root / slug
        d.mkdir()
        (d / ("SKILL.md.disabled" if disabled else "SKILL.md")).write_text("x")
        if version:
            (d / "manifest.json").write_text(json.dumps({"version": version}))
        if source:
            (d / ".sutando-source.json").write_text(json.dumps({"source": source}))

    def inv(self, *rows, tools=()):
        return {"installed": list(rows), "cloudTools": list(tools), "connectors": []}

    def test_status_classifies_and_scopes_to_agent(self):
        self.make_skill("current", "1.0.0")
        self.make_skill("stale", "0.9.0")
        self.make_skill("off", "1.0.0", disabled=True)
        rows = [
            {"slug": "current", "version": "1.0.0", "agents": []},
            {"slug": "stale", "version": "1.0.0", "agents": ["@bot:ag2.space"]},
            {"slug": "off", "version": "1.0.0"},
            {"slug": "gone", "version": "1.0.0"},
            {"slug": "other-agent", "version": "1.0.0", "agents": ["@someone-else:ag2.space"]},
        ]
        ctx = FakeContext(self.root, inventory=self.inv(*rows, tools=[
            {"slug": "mine", "agents": []},
            {"slug": "pinned-elsewhere", "agents": ["@old-identity:ag2.space"]},
        ]))
        st = marketplace.collect_status(ctx)
        self.assertEqual({s["slug"]: s["status"] for s in st["skills"]},
                         {"current": "ok", "stale": "outdated", "off": "disabled", "gone": "missing",
                          "other-agent": "missing"})
        by_slug = {s["slug"]: s for s in st["skills"]}
        # never silently dropped: owned rows pinned to another (possibly retired) agent id stay visible
        self.assertEqual(by_slug["other-agent"]["assigned_to_other_agents"], ["@someone-else:ag2.space"])
        self.assertNotIn("assigned_to_other_agents", by_slug["stale"])
        tools = {t["slug"]: t for t in st["cloud_tools"]}
        self.assertEqual(set(tools), {"mine", "pinned-elsewhere"})
        self.assertEqual(tools["pinned-elsewhere"]["assigned_to_other_agents"], ["@old-identity:ag2.space"])

    def test_status_text_names_other_agent_owners_and_skips_junk_rows(self):
        ctx = FakeContext(self.root, inventory={
            "installed": [{"slug": "theirs", "version": "1", "agents": ["@old:ag2.space"]}],
            "cloudTools": ["junk", {"slug": "leads"}],
            "connectors": [],
        })
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            marketplace.main(["status"], ctx=ctx)
        self.assertIn("- theirs: missing (equipped on @old:ag2.space, not this agent)", out.getvalue())
        self.assertIn("Cloud tools: leads", out.getvalue())
        self.assertNotIn("need updating", out.getvalue(), "another agent's skill is not this agent's update")

    def test_update_skips_other_agents_skills_unless_named(self):
        ctx = FakeContext(self.root, inventory=self.inv(
            {"slug": "gone", "version": "1"},
            {"slug": "theirs", "version": "1", "agents": ["@other:ag2.space"]},
        ))
        code, out = run(ctx, ["update"])
        self.assertEqual([p["slug"] for p in out["pending"]], ["gone"])
        code, out = run(ctx, ["update", "theirs"])
        self.assertEqual([p["slug"] for p in out["pending"]], ["theirs"])

    def test_update_refetches_only_missing_and_outdated(self):
        self.make_skill("current", "1.0.0")
        self.make_skill("demo", "0.9.0")
        ctx = FakeContext(self.root, inventory=self.inv({"slug": "current", "version": "1.0.0"}, {"slug": "demo", "version": "1.2.0"}),
                          catalog=[item(SKILL_UUID, "demo", acquired=True)])
        code, out = run(ctx, ["update"])
        self.assertEqual([p["slug"] for p in out["pending"]], ["demo"])
        self.assertFalse(any(m == "POST" for m, _, _ in ctx.calls))

        ctx.install_responses[SKILL_UUID] = {"deduped": True, "bundleUrl": "https://b/demo", "signingHash": hashlib.sha256(GOOD).hexdigest(), "version": "1.2.0"}
        ctx.bundles["https://b/demo"] = GOOD
        code, out = run(ctx, ["update", "--yes"])
        self.assertEqual(code, marketplace.EXIT_OK)
        self.assertEqual(out["installed"], ["demo"])
        self.assertEqual(skill_install.local_version(self.root / "demo"), "1.2.0")

    def test_uninstall_needs_confirmation_then_removes_marketplace_dir(self):
        self.make_skill("demo", "1.0.0", source="marketplace")
        ctx = FakeContext(self.root, inventory=self.inv({"slug": "demo", "priceCredits": 30}))
        code, out = run(ctx, ["uninstall", "demo"])
        self.assertEqual(code, marketplace.EXIT_CONFIRM)
        self.assertEqual(out["paid_credits"], 30)
        self.assertTrue((self.root / "demo").exists())
        code, out = run(ctx, ["uninstall", "demo", "--yes"])
        self.assertEqual(code, marketplace.EXIT_OK)
        self.assertIn(("POST", "/api/skills/demo/uninstall", {}), ctx.calls)
        self.assertFalse((self.root / "demo").exists())

    def test_uninstall_keeps_a_directory_another_installer_owns(self):
        self.make_skill("demo", "1.0.0", source="openai-skills")
        ctx = FakeContext(self.root, inventory=self.inv({"slug": "demo"}))
        code, out = run(ctx, ["uninstall", "demo", "--yes"])
        self.assertFalse(out["removed_local"])
        self.assertTrue((self.root / "demo").exists())

    def test_uninstall_unowned_is_an_error(self):
        ctx = FakeContext(self.root)
        code, _ = run(ctx, ["uninstall", "demo", "--yes"])
        self.assertEqual(code, marketplace.EXIT_FAILED)
        self.assertFalse(any(m == "POST" for m, _, _ in ctx.calls))


class TestSkillInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, data, slug="demo"):
        return skill_install.atomic_install(slug, self.root, lambda t: skill_install.extract_bundle(data, t), {"v": 1})

    def test_root_and_wrapper_layouts(self):
        self.install(bundle({"SKILL.md": "a", "scripts/run.py": "b"}), "flat")
        self.install(bundle({"SKILL.md": "a", "scripts/run.py": "b"}, wrapper="pkg"), "wrapped")
        for slug in ("flat", "wrapped"):
            self.assertTrue((self.root / slug / "SKILL.md").is_file())
            self.assertTrue((self.root / slug / "scripts" / "run.py").is_file())

    def test_rejects_traversal_links_and_missing_skill_md(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evil")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.install(buf.getvalue())

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            s = tarfile.TarInfo("SKILL.md")
            s.size = 1
            tar.addfile(s, io.BytesIO(b"x"))
            link = tarfile.TarInfo("escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.install(buf.getvalue())

        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            self.install(bundle({"README.md": "x"}))
        with self.assertRaisesRegex(ValueError, "tar.gz"):
            self.install(b"not a tarball")
        self.assertEqual([p.name for p in self.root.iterdir()], [])

    def test_a_skill_md_directory_is_not_a_skill(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            d = tarfile.TarInfo("SKILL.md")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            f = tarfile.TarInfo("SKILL.md/inner.txt")
            f.size = 1
            tar.addfile(f, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "no SKILL.md at its root"):
            self.install(buf.getvalue())

    def test_refuses_symlink_target_and_bad_slug(self):
        real = self.root / "_real"
        real.mkdir()
        os.symlink(real, self.root / "demo")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.install(GOOD)
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_install.remove_skill("demo", self.root)
        with self.assertRaisesRegex(ValueError, "safe skill slug"):
            self.install(GOOD, slug="../x")

    def test_failed_swap_restores_previous(self):
        target = self.root / "demo"
        target.mkdir()
        (target / "old.txt").write_text("old")
        real = os.replace
        calls = 0

        def fail_second(a, b):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("boom")
            return real(a, b)

        with mock.patch.object(skill_install.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "boom"):
                self.install(GOOD)
        self.assertEqual((target / "old.txt").read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["demo"])



class TestCoverageEdges(unittest.TestCase):
    """The paths the main suites don't walk: rendering, context wiring, error exits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, ctx, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = marketplace.main(argv, ctx=ctx)
        return code, out.getvalue()

    def test_find_renders_rows_and_filters_connectors(self):
        ctx = FakeContext(self.root, catalog=[
            {**item(TOOL_UUID, "leads", kind="cloud_tool", acquired=True), "price": {"model": "per_call", "credits": 5, "unitLabel": "run"}, "description": "find leads"},
            {"slug": "gmail", "kind": "connector"},
        ])
        code, out = self.text(ctx, ["find", "lead", "--kind", "cloud_tool"])
        self.assertEqual(code, 0)
        self.assertIn("1 result(s)", out)
        self.assertIn("- leads [cloud_tool] 5 credits per run, tier free — owned", out)
        self.assertIn("find leads", out)
        self.assertIn(("GET", "/api/station/catalog?kind=cloud_tool&q=lead", None), ctx.calls)

    def test_text_plan_and_results_rendering(self):
        (self.root / "off").mkdir()
        (self.root / "off" / "SKILL.md.disabled").write_text("x")
        ctx = FakeContext(self.root, me={"plan": "free", "walletCredits": 5}, catalog=[
            item(PAID_UUID, "pricey", credits=40, model="one_time"),
            item(SKILL_UUID, "off", acquired=True),
            {**item(TOOL_UUID, "soon", kind="cloud_tool"), "comingSoon": True},
        ])
        code, out = self.text(ctx, ["install", "pricey", "off", "soon", "pricey"])
        self.assertEqual(code, marketplace.EXIT_CONFIRM)
        self.assertIn("- pricey: install skill (40 credits once)", out)
        self.assertIn("installed but disabled", out)
        self.assertIn("coming soon", out)
        self.assertIn("Not enough credits", out)
        self.assertIn("confirm with the owner", out)

        ctx.install_errors[PAID_UUID] = cloud_auth.CloudError(404, "skill not found")
        code, out = self.text(ctx, ["install", "pricey", "off", "soon", "--yes"])
        self.assertEqual(code, marketplace.EXIT_FAILED)
        self.assertIn("Failed pricey: not found in the marketplace", out)
        self.assertIn("Skipped soon: coming soon", out)
        self.assertIn("Already set up: off", out)

    def test_nothing_to_do_and_free_hint(self):
        ctx = FakeContext(self.root, catalog=[item(TOOL_UUID, "leads", kind="cloud_tool", acquired=True)])
        self.assertIn("Nothing to do.", self.text(ctx, ["install", "leads"])[1])
        ctx = FakeContext(self.root, catalog=[item(TOOL_UUID, "leads", kind="cloud_tool")])
        self.assertIn("Free — safe to re-run with --yes.", self.text(ctx, ["install", "leads"])[1])
        ctx.install_responses[TOOL_UUID] = {}
        code, out = self.text(ctx, ["install", "leads", "--yes"])
        self.assertIn("Activated cloud tools: leads", out)
        self.assertIn("RESTART REQUIRED", out)

    def test_materialize_failures(self):
        ctx = FakeContext(self.root, catalog=[item(SKILL_UUID, "demo")])
        ctx.install_responses[SKILL_UUID] = {"bundleUrl": None}
        self.assertIn("no downloadable bundle", run(ctx, ["install", "demo", "--yes"])[1]["failed"][0]["reason"])
        bad = bundle({"README.md": "x"})
        ctx.install_responses[SKILL_UUID] = {"bundleUrl": "https://b/bad", "signingHash": hashlib.sha256(bad).hexdigest()}
        ctx.bundles["https://b/bad"] = bad
        self.assertIn("download/install failed", run(ctx, ["install", "demo", "--yes"])[1]["failed"][0]["reason"])
        ctx.install_responses[SKILL_UUID] = {"bundleUrl": "https://b/bad", "signingHash": None}
        ctx.bundles["https://b/bad"] = GOOD
        with mock.patch.object(skill_install, "atomic_install") as inst:
            code, out = run(ctx, ["install", "demo", "--yes", "--allow-unsigned"])
        self.assertEqual(out["installed"], ["demo"])
        inst.assert_called_once()

    def test_status_text_rendering(self):
        (self.root / "stale").mkdir()
        (self.root / "stale" / "SKILL.md").write_text("x")
        (self.root / "stale" / "manifest.json").write_text('{"version": "0.1.0"}')
        real = self.root / "_engine"
        real.mkdir()
        os.symlink(real, self.root / "bundled")
        cfg = self.root / "cfg"
        cfg.mkdir()
        inv = {
            "installed": [{"slug": "stale", "version": "0.2.0"}, {"slug": "bundled"}, {"slug": "../bad"}, "junk"],
            "cloudTools": [{"slug": "leads", "agents": ["@old:ag2.space"]}],
            "connectors": [{"toolkit": "gmail", "status": "ACTIVE"}],
        }
        ctx = FakeContext(self.root, inventory=inv, me={"plan": "max", "walletCredits": 7})
        ctx.config_dir = cfg
        code, out = self.text(ctx, ["status"])
        self.assertIn("Plan: max, credits: 7", out)
        self.assertIn("- stale: outdated (0.1.0 → 0.2.0)", out)
        self.assertIn("leads (equipped on @old:ag2.space, not this agent — still callable)", out)
        self.assertIn("- bundled: bundled", out)
        self.assertIn("Connectors: gmail (ACTIVE)", out)
        self.assertIn("1 skill(s) need updating", out)
        self.assertIn("aren't wired into the core yet", out)
        (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"sutando-station": {}}}))
        self.assertTrue(marketplace.station_mcp_registered(ctx))
        ctx.config_dir = None
        self.assertIsNone(marketplace.station_mcp_registered(ctx))

        empty = FakeContext(self.root)
        code, out = self.text(empty, ["status"])
        self.assertIn("- none owned", out)
        self.assertIn("Cloud tools: none active", out)

    def test_update_texts_and_vanished_skill(self):
        ctx = FakeContext(self.root, inventory={"installed": [{"slug": "current", "version": None}]})
        (self.root / "current").mkdir()
        (self.root / "current" / "SKILL.md").write_text("x")
        self.assertIn("All owned skills are up to date.", self.text(ctx, ["update"])[1])

        ctx = FakeContext(self.root, inventory={"installed": [{"slug": "gone", "version": "1"}]})
        code, out = self.text(ctx, ["update", "gone"])
        self.assertIn("- gone: missing", out)
        code, out = run(ctx, ["update", "--yes"])
        self.assertEqual(out["failed"][0]["reason"], "no longer in the marketplace")

    def test_uninstall_cloud_error_and_cloud_tool_note(self):
        ctx = FakeContext(self.root, inventory={"installed": [], "cloudTools": [{"slug": "leads"}]})
        code, out = self.text(ctx, ["uninstall", "leads", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("disappear from the core after the next restart", out)

        def boom(method, path, body=None):
            if path.endswith("/uninstall"):
                raise cloud_auth.CloudError(0, "network", "down")
            return FakeContext.http(ctx, method, path, body)

        ctx.http = boom
        code, out = self.text(ctx, ["uninstall", "leads", "--yes"])
        self.assertEqual(code, marketplace.EXIT_FAILED)
        self.assertIn("could not reach Sutando Cloud (down)", out)

    def test_main_error_exits(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(marketplace, "build_context", side_effect=marketplace.Usage("sign in")):
            self.assertEqual(marketplace.main(["status"]), marketplace.EXIT_USAGE)
        ctx = FakeContext(self.root)
        for exc, code in ((cloud_auth.CloudError(401, "unauthenticated"), marketplace.EXIT_USAGE),
                          (cloud_auth.CloudError(500, "boom"), marketplace.EXIT_FAILED)):
            ctx.inventory = mock.Mock(side_effect=exc)
            with contextlib.redirect_stderr(err):
                self.assertEqual(marketplace.main(["status"], ctx=ctx), code)
        with contextlib.redirect_stderr(err):
            self.assertEqual(marketplace.main(["uninstall", "../x", "--yes"], ctx=ctx), marketplace.EXIT_USAGE)
        self.assertIn("cloud error 500 boom", err.getvalue())

    def test_helpers(self):
        self.assertEqual(marketplace.credits_for({"price": {"model": "one_time", "credits": "x"}}), 0)
        self.assertEqual(marketplace.price_label({"price": {"model": "free"}}), "free")
        self.assertIn("not enough credits", marketplace.explain_error(cloud_auth.CloudError(402, "insufficient_credits")))
        self.assertEqual(marketplace.local_state(self.root, "nothing"), "absent")
        (self.root / "empty").mkdir()
        self.assertEqual(marketplace.local_state(self.root, "empty"), "absent")

    def test_build_context_and_identity(self):
        args = marketplace.parser().parse_args(["--dest-root", str(self.root), "status"])
        with mock.patch.object(cloud_auth, "read_cloud_auth", return_value=(None, None)):
            with self.assertRaises(marketplace.Usage):
                marketplace.build_context(args)
        with mock.patch.object(cloud_auth, "read_cloud_auth", return_value=(None, "sutk_x")), \
                mock.patch.object(marketplace, "_agent_id", return_value="@a:b"), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            ctx = marketplace.build_context(args)
        self.assertEqual((ctx.base, ctx.token, ctx.dest_root, ctx.agent_id),
                         (cloud_auth.DEFAULT_CLOUD_ORIGIN, "sutk_x", self.root.resolve(), "@a:b"))
        self.assertIsInstance(marketplace._workspace(), Path)
        with mock.patch.dict(sys.modules, {"sutando_config": None}):
            self.assertEqual(marketplace._workspace(), marketplace.REPO_ROOT / "workspace")
        fake = mock.Mock(DEFAULT_ACTOR="local-agent")
        with mock.patch.dict(sys.modules, {"rundir": fake}):
            fake.agent_id.return_value = "local-agent"
            self.assertIsNone(marketplace._agent_id())
            fake.agent_id.return_value = "@bot:ag2.space"
            self.assertEqual(marketplace._agent_id(), "@bot:ag2.space")
            fake.agent_id.side_effect = RuntimeError("no state")
            self.assertIsNone(marketplace._agent_id())

    def test_real_context_http_and_download(self):
        ctx = marketplace.Context("https://sutando.ag2.space", "sutk_x", self.root, None)
        with mock.patch.object(cloud_auth, "cloud_request", return_value={"plan": "pro"}) as req:
            self.assertEqual(ctx.me(), {"plan": "pro"})
        req.assert_called_once_with("https://sutando.ag2.space", "sutk_x", "GET", "/api/me", None, insecure_hosts=frozenset())
        with self.assertRaisesRegex(ValueError, "non-https"):
            ctx.download("http://evil/x.tgz")
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"data"
        with mock.patch.object(marketplace.urllib.request, "urlopen", return_value=resp) as op:
            self.assertEqual(ctx.download("https://b/x.tgz"), b"data")
        self.assertIsNone(op.call_args.args[0].get_header("Authorization"))
        resp.__enter__.return_value.read.return_value = b"x" * 3
        with mock.patch.object(marketplace.urllib.request, "urlopen", return_value=resp), \
                mock.patch.object(skill_install, "MAX_BYTES", 2):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                ctx.download("https://b/x.tgz")

    def test_skill_install_limits_and_helpers(self):
        with mock.patch.object(skill_install, "MAX_FILES", 1):
            with self.assertRaisesRegex(ValueError, "files; safety limit"):
                skill_install.extract_bundle(bundle({"SKILL.md": "a", "b": "c"}), self.root)
        with mock.patch.object(skill_install, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "exceeds safety limit"):
                skill_install.extract_bundle(bundle({"SKILL.md": "abc"}), self.root)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            d = tarfile.TarInfo("pkg")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            for name, mode in (("pkg/SKILL.md", 0o644), ("pkg/run.sh", 0o755)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = 1, mode
                tar.addfile(info, io.BytesIO(b"x"))
        out = self.root / "out"
        out.mkdir()
        skill_install.extract_bundle(buf.getvalue(), out)
        self.assertTrue(os.access(out / "run.sh", os.X_OK))
        self.assertFalse(skill_install.remove_skill("nope", self.root))
        (out / "manifest.json").write_text("{bad")
        (out / ".sutando-source.json").write_text("[1]")
        self.assertIsNone(skill_install.local_version(out))
        self.assertEqual(skill_install.read_provenance(out), {})
        self.assertEqual(skill_install.read_provenance(self.root / "missing"), {})


if __name__ == "__main__":
    unittest.main()
