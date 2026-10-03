#!/usr/bin/env python3
"""Round-37 review findings on the pending-questions skill, as regressions.

1. Discovery roots and ownership: the store adapter is resolved at the edge by src/skill_roots.py
   across BOTH installed roots (`<repo>/skills` and `<workspace>/skills`, the pair install.sh
   links) and injected into src/pending_questions_reader.py, which scans nothing itself. A
   conforming declarer under `<workspace>/skills/custom` is what production selects when the
   shipped one is absent; one in each root is a refusal in every edge, never a silent pick.
2. The reminder reads an injected adapter through the manifest contract's two entry points —
   `reconcile_pass(ws)` then `gather(ws)` — so a minimal adapter implementing only the documented
   contract runs without TypeError and its held ask is reconciled; a reconcile error is kept.

Run: python3 tests/pending-questions-review-r37.test.py
"""
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
import pending_questions_reader as reader  # noqa: E402
import skill_roots  # noqa: E402

SHIPPED = (REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_room_db.py").resolve()
WORKSPACE_ITEM = "from the workspace store?"

# A conforming declarer: the documented entry points, nothing of the shipped skill's.
CUSTOM = f'''
def room_store(ws):
    return None, "custom"
def gather(ws):
    return {{"waiting": [{{"ask_id": "ask-ws", "title": "{WORKSPACE_ITEM}", "snippet": "", "body": "",
             "asked_at": None, "priority": "medium", "in_room": True}}], "done": 0, "pending_close": [],
            "unavailable": False, "reason": None, "link": None, "notes": [], "store": "workspace"}}
def waiting(ws):
    return gather(ws)["waiting"]
def count(ws):
    return {{"open": 1, "done": 0, "pending_close": 0, "unavailable": False, "reason": None}}
def reconcile_pass(ws):
    return {{"flushed": [], "moved": [], "closed": [], "errors": []}}
def resolve(ws, ask_id, status):
    return True, f"{{ask_id}} -> {{status}} in the workspace store"
def ask_owner(question, store=None, **kw):
    return {{"outbox": None, "record": "row", "where": "workspace", "question": question}}
def report_lines(out):
    return [f"recorded: in the workspace store ({{out['question']}})"]
'''

# A minimal adapter: ONLY what skills/MANIFEST.md documents for the reminder's pass.
MINIMAL = '''
import json
from pathlib import Path
def room_store(ws):
    return None, "minimal"
def _rows(ws):
    p = Path(ws) / "room.json"
    return json.loads(p.read_text()) if p.exists() else []
def gather(ws):
    if (Path(ws) / "raise-read").exists():
        raise ConnectionError("read down")
    rows = [{"ask_id": r["ask_id"], "title": r["title"], "snippet": "", "body": "", "asked_at": None,
             "priority": "medium", "in_room": True} for r in _rows(ws)]
    return {"waiting": rows, "done": 0, "pending_close": [], "unavailable": False, "reason": None,
            "link": None, "notes": [], "store": "minimal"}
def reconcile_pass(ws):
    held = Path(ws) / "held.json"
    if (Path(ws) / "fail").exists():
        return {"flushed": [], "moved": [], "closed": [], "errors": ["replay refused"]}
    if (Path(ws) / "raise").exists():
        raise ConnectionError("replay down")
    if not held.exists():
        return {"flushed": [], "moved": [], "closed": [], "errors": []}
    rows = _rows(ws) + [json.loads(held.read_text())]
    (Path(ws) / "room.json").write_text(json.dumps(rows))
    held.unlink()
    return {"flushed": [rows[-1]["ask_id"]], "moved": [], "closed": [], "errors": []}
def resolve(ws, ask_id, status):
    return True, "closed"
'''


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Roots(unittest.TestCase):
    """A scratch workspace with `skills/custom` declaring a conforming adapter."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-r37-"))
        self.ws = self.tmp / "workspace"
        for d in ("results", "state", "tasks", "logs"):
            (self.ws / d).mkdir(parents=True)
        self.custom = self.ws / "skills" / "custom"
        self.custom.mkdir(parents=True)
        (self.custom / "adapter.py").write_text(CUSTOM)
        (self.custom / "manifest.json").write_text(json.dumps({"name": "custom", "pending_questions_store": "adapter.py"}))
        self.custom_script = (self.custom / "adapter.py").resolve()
        self.no_repo_skills = self.tmp / "no-repo-skills"
        self.no_repo_skills.mkdir()

    def shipped_absent(self):
        """The engine root with no declarer: the owner's workspace skill is the only one."""
        return mock.patch.object(skill_roots, "REPO_SKILLS", self.no_repo_skills)

    def edges(self):
        """(name, callable -> what that production edge reads), all with the workspace at self.ws."""
        api = _load("agent_api_r37", REPO / "src" / "agent-api.py")
        dash = _load("dashboard_r37", REPO / "src" / "dashboard.py")
        api.WORKSPACE_DIR = self.ws
        dash.WORKSPACE_DIR = self.ws
        cpq = _load("cpq_r37", REPO / "src" / "check-pending-questions.py")
        ask = _load("ask_owner_r37", REPO / "scripts" / "ask-owner.py")

        def reader_cli():
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                reader.main(["list", "--workspace", str(self.ws)])
            return out.getvalue()

        def shim():
            with mock.patch("workspace_default.resolve_workspace", return_value=self.ws), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                cpq.main([])
            return out.getvalue()

        def ask_owner():
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                ask.main(["which store?", "--urgency", "durable", "--workspace", str(self.ws)])
            return out.getvalue() + err.getvalue()

        # (edge, read, what the workspace store's answer looks like there)
        return [("reader CLI", reader_cli, WORKSPACE_ITEM), ("check-pending-questions", shim, WORKSPACE_ITEM),
                ("ask-owner", ask_owner, "in the workspace store"),
                ("agent-api", lambda: json.dumps(api._pending_gather()), WORKSPACE_ITEM),
                ("dashboard", lambda: json.dumps(dash.get_pending_count()), '"open": 1')]


class Finding1Roots(_Roots):
    def test_the_roots_are_the_pair_install_sh_links_from_the_workspace_helper(self):
        self.assertEqual(skill_roots.skill_roots(self.ws), [REPO / "skills", self.ws / "skills"])
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws) as rw:
            self.assertEqual(skill_roots.skill_roots(), [REPO / "skills", self.ws / "skills"])
        rw.assert_called_once_with(migrate=False)
        self.assertEqual(skill_roots.skill_roots(REPO), [REPO / "skills"], "the same directory once")

    def test_a_workspace_declarer_alone_is_what_production_selects(self):
        """The reviewer's injection, with the shipped skill absent: the owner's store owns the questions."""
        with self.shipped_absent():
            decl = skill_roots.declared(reader.DECLARATION, self.ws)
            self.assertEqual(decl, skill_roots.Declaration(self.custom_script, None))
            self.assertEqual(reader.gather(self.ws, decl)["store"], "workspace")
            for name, read, picked in self.edges():
                self.assertIn(picked, read(), name)
            ok, msg = reader.resolve(self.ws, "ask-ws", "Answered", skill_roots.declared(reader.DECLARATION, self.ws))
            self.assertEqual((ok, msg), (True, "ask-ws -> Answered in the workspace store"))

    def test_a_declarer_in_each_root_is_refused_in_every_edge_never_picked(self):
        """Both roots declare: the shipped skill in <repo>/skills, custom in <workspace>/skills."""
        decl = skill_roots.declared(reader.DECLARATION, self.ws)
        self.assertIsNone(decl.script)
        self.assertIn("more than one skill declares pending_questions_store: custom, pending-questions", decl.reason)
        with self.assertRaisesRegex(skill_roots.DeclarationConflict, "custom, pending-questions"):
            skill_roots.declared_script(reader.DECLARATION, [REPO / "skills", self.ws / "skills"])
        g = reader.gather(self.ws, decl)
        self.assertEqual((g["unavailable"], g["done"], g["waiting"]), (True, None, []))
        for name, read, picked in self.edges():
            out = read()
            self.assertIn("more than one skill declares", out, name)
            self.assertNotIn(picked, out, f"{name} picked one")
        ok, msg = reader.resolve(self.ws, "ask-ws", "Answered", decl)
        self.assertFalse(ok)
        self.assertIn("more than one skill declares", msg)

    def test_without_the_workspace_declarer_the_shipped_skill_is_selected_as_before(self):
        (self.custom / "manifest.json").unlink()
        self.assertEqual(skill_roots.declared(reader.DECLARATION, self.ws), skill_roots.Declaration(SHIPPED, None))
        self.assertEqual(skill_roots.declared(reader.DECLARATION, self.ws, override=self.custom_script).script,
                         self.custom_script, "--store-adapter overrides the scan")

    def test_a_core_read_scans_no_root_and_the_reader_names_none(self):
        """The repo declares a store, yet a read with nothing injected has none: discovery is the edge's."""
        for read in (reader.gather(self.ws), reader.count(self.ws)):
            self.assertTrue(read["unavailable"])
            self.assertEqual(read["reason"], reader.NO_ADAPTER)
        self.assertEqual(reader.resolve(self.ws, "ask-ws", "Answered")[0], False)
        src = (REPO / "src" / "pending_questions_reader.py").read_text()
        self.assertNotRegex(src, r'glob\(|manifest\.json|SKILLS_DIR|"skills"|skills_dir|declared_scripts?\(')

    def test_every_production_edge_resolves_through_the_one_roots_helper(self):
        edge_files = ("scripts/ask-owner.py", "src/check-pending-questions.py", "src/pending_questions_reader.py",
                      "src/agent-api.py", "src/dashboard.py", "src/friction-detector.py", "src/morning-briefing.py",
                      "src/obsidian-mirror.py")
        for rel in edge_files:
            text = (REPO / rel).read_text()
            self.assertRegex(text, r"(skill_roots\.)?declared\((pending_questions_reader|reader)?\.?DECLARATION, ", rel)
            self.assertNotRegex(text, r"skills/\*/manifest|\"skills\" ?\)?\.glob", rel)
        for rel in ("src/sparrowd.py", "src/skill_hooks.py"):
            self.assertNotRegex((REPO / rel).read_text(), r"pending_questions", rel)


class Finding2ReminderContract(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-r37-remind-"))
        self.ws = self.tmp / "ws"
        for d in ("results", "state", "logs"):
            (self.ws / d).mkdir(parents=True)
        self.minimal = self.tmp / "minimal_adapter.py"
        self.minimal.write_text(MINIMAL)
        self.cpq = _load("pq_remind_r37", REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_remind.py")
        self.cpq.notify_macos = lambda count, titles: True
        self.cpq.voice_client_connected = lambda: False

    def run_reminder(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.cpq.main(["--store-adapter", str(self.minimal), *argv], workspace=self.ws)
        return rc, out.getvalue(), err.getvalue()

    def test_a_minimal_contract_adapter_runs_and_its_held_ask_is_reconciled(self):
        """The reviewer's injection: only the documented entry points; the old call raised TypeError."""
        (self.ws / "held.json").write_text(json.dumps({"ask_id": "ask-held", "title": "held until reconciled?"}))
        rc, out, err = self.run_reminder()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("TypeError", err)
        self.assertIn("1 pending questions; nothing sent", out)
        self.assertIn("[ask-held] held until reconciled?", out)
        self.assertFalse((self.ws / "held.json").exists(), "reconcile_pass filed it")
        self.assertEqual([r["ask_id"] for r in json.loads((self.ws / "room.json").read_text())], ["ask-held"])
        rc, out, _ = self.run_reminder("--notify", "--force")
        self.assertEqual(rc, 0)
        self.assertIn("Notified: 1 pending questions", out)
        [f] = [p for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-pending-q-")]
        self.assertIn("held until reconciled?", f.read_text())

    def test_a_reconcile_error_is_kept_in_the_output_and_the_read_still_lists(self):
        (self.ws / "room.json").write_text(json.dumps([{"ask_id": "ask-row", "title": "a row?"}]))
        (self.ws / "fail").write_text("")
        rc, out, err = self.run_reminder()
        self.assertEqual(rc, 0)
        self.assertIn("reconcile: FAILED — replay refused", err)
        self.assertIn("[ask-row] a row?", out)

    def test_a_raised_reconcile_failure_is_a_note_and_the_read_still_lists(self):
        """Round 38: the injected adapter's reconcile_pass raises; the reminder must still read and
        list, with the failure as a note, not exit 1 before gather."""
        (self.ws / "room.json").write_text(json.dumps([{"ask_id": "ask-existing", "title": "still waiting?"}]))
        (self.ws / "raise").write_text("")
        rc, out, err = self.run_reminder()
        self.assertEqual(rc, 0, err)
        self.assertIn("reconcile: FAILED — adapter failed: ConnectionError: replay down", err)
        self.assertIn("[ask-existing] still waiting?", out)
        self.assertNotIn("Traceback", err)

    def test_a_raised_read_is_unknown_not_a_traceback(self):
        """Round 39: the injected adapter's gather raises; the reminder reports UNKNOWN with the
        reason, as the core reader does, exits 0, and sends nothing even with --notify."""
        (self.ws / "raise-read").write_text("")
        rc, out, err = self.run_reminder("--notify", "--force")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("UNKNOWN", out + err)
        self.assertIn("adapter failed: ConnectionError: read down", out + err)
        self.assertEqual([p for p in (self.ws / "results").iterdir()], [], "nothing is sent on an unknown count")

    def test_the_pass_calls_reconcile_pass_then_gather_and_no_keyword(self):
        calls = []
        fake = mock.Mock()
        fake.reconcile_pass.side_effect = lambda ws: calls.append(("reconcile_pass", ws)) or {"errors": ["e1"]}
        fake.gather.side_effect = lambda ws: calls.append(("gather", ws)) or {"waiting": [], "notes": ["n"]}
        with mock.patch.object(self.cpq.importlib.util, "spec_from_file_location") as sfl:
            sfl.return_value.loader.exec_module = lambda m: None
            with mock.patch.object(self.cpq.importlib.util, "module_from_spec", return_value=fake):
                g = self.cpq.gather(str(self.minimal))
        self.assertEqual([c[0] for c in calls], ["reconcile_pass", "gather"])
        self.assertEqual({c[1] for c in calls}, {self.cpq.WORKSPACE})
        self.assertEqual(fake.gather.call_args, mock.call(self.cpq.WORKSPACE), "plain gather(ws): no private keyword")
        self.assertEqual(g["notes"], ["reconcile: FAILED — e1", "n"])

    def test_the_contract_doc_and_the_shipped_adapter_agree(self):
        contract = (REPO / "skills" / "MANIFEST.md").read_text()
        self.assertIn("`reconcile_pass(workspace)`", contract)
        self.assertIn("then `gather(workspace)`", contract)
        reminder = (REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_remind.py").read_text()
        self.assertNotIn("reconcile=", reminder)
        self.assertNotIn("reconcile=", (REPO / "src" / "pending_questions_reader.py").read_text())
        shipped = (REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_room_db.py").read_text()
        self.assertRegex(shipped, r"def gather\(workspace: Path, environ=None\) -> dict:")
        self.assertIsNone(re.search(r"def gather\([^)]*reconcile", shipped), "no keyword the contract does not name")


if __name__ == "__main__":
    unittest.main()
