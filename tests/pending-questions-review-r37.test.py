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
import time
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

    def test_the_flagless_entry_runs_the_shipped_adapter_once(self):
        """rui at ad33218b0: with no --store-adapter the core loaded the shipped adapter by path and
        the reminder re-imported it by module name — a second execution. The adapter's remind now
        hands its own file to the pass, so the one load serves both."""
        import pending_questions_reader as reader
        import skill_roots
        import workspace_default
        shim = _load("pq_shim_r37b", REPO / "src" / "check-pending-questions.py")
        shipped = REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_room_db.py"
        reader._LOADED.clear()
        sys.modules.pop("pending_questions_room_db", None)
        seen = []
        real = reader.reconcile_then_gather
        def spy(ws, adapter=None):
            seen.append(adapter)
            return real(ws, adapter)
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
             mock.patch.object(shim, "declared", return_value=skill_roots.Declaration(shipped, None)), \
             mock.patch.object(reader, "reconcile_then_gather", spy), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = shim.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], reader.Resolved, "the pass got the entry's one resolution (round 42)")
        self.assertEqual(Path(seen[0].reason).resolve(), shipped.resolve())
        self.assertIs(seen[0].module, reader.load_adapter(shipped), "one cached load")
        self.assertNotIn("pending_questions_room_db", sys.modules, "no second execution by module name")

    def _counting_adapter(self, name, slow=False, fail=False):
        """An adapter whose top level counts its executions in a sidecar file."""
        f = self.tmp / f"{name}.py"
        f.write_text(
            "from pathlib import Path as _P\n"
            "import time as _t\n"
            "_c = _P(__file__).with_suffix('.count')\n"
            "_c.write_text(str(int(_c.read_text()) + 1) if _c.exists() else '1')\n"
            + ("_t.sleep(0.2)\n" if slow else "")
            + ("raise RuntimeError('adapter executed ' + _c.read_text())\n" if fail else "")
            + MINIMAL)
        return f

    def _count(self, f):
        c = f.with_suffix(".count")
        return int(c.read_text()) if c.exists() else 0

    def test_concurrent_first_reads_execute_the_adapter_once_and_agree(self):
        """Round 41: unlocked miss/execute/publish executed one adapter twice under threaded first
        reads, and one caller saw UNKNOWN. Now the lock serialises the one execution."""
        import pending_questions_reader as reader
        import threading
        reader._LOADED.clear()
        f = self._counting_adapter("concurrent", slow=True)
        (self.ws / "room.json").write_text(json.dumps([{"ask_id": "ask-c", "title": "live?"}]))
        results, start = [], threading.Barrier(16)
        def read():
            start.wait()
            results.append(reader.gather(self.ws, f))
        ts = [threading.Thread(target=read) for _ in range(16)]
        for th in ts:
            th.start()
        for th in ts:
            th.join()
        self.assertEqual(self._count(f), 1, "one execution for sixteen first reads")
        self.assertEqual([g["unavailable"] for g in results], [False] * 16, "every caller saw the live store")
        self.assertEqual({len(g["waiting"]) for g in results}, {1})

    def test_a_failed_import_executes_once_per_process_and_once_per_public_invocation(self):
        """Round 41: a failing adapter was executed again by the public fallback and by the second
        phase of the pass. The failure is remembered for the failure window; one operation resolves once."""
        import pending_questions_reader as reader
        reader._LOADED.clear()
        f = self._counting_adapter("failing", fail=True)
        g = reader.reconcile_then_gather(self.ws, f)
        self.assertTrue(g["unavailable"])
        self.assertIn("adapter executed 1", g["reason"], "the first failure is the reported one")
        self.assertEqual(self._count(f), 1, "reconcile and gather shared one resolution")
        shim = _load("pq_shim_r41", REPO / "src" / "check-pending-questions.py")
        import skill_roots
        import workspace_default
        loads = mock.Mock(wraps=reader.load_adapter)
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
             mock.patch.object(shim, "declared", return_value=skill_roots.Declaration(f, None)), \
             mock.patch.object(reader, "load_adapter", loads), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = shim.main(["--notify", "--force"])
        self.assertEqual(rc, 0)
        self.assertIn("UNKNOWN", out.getvalue())
        self.assertIn("adapter executed 1", out.getvalue(), "the public fallback kept the first failure")
        self.assertEqual(self._count(f), 1, "no retry of a failed import inside the failure window")
        self.assertEqual(loads.call_count, 1, "one resolution per public invocation, cache or not")

    def test_reconcile_then_gather_hands_one_resolved_module_to_both_phases(self):
        import pending_questions_reader as reader
        reader._LOADED.clear()
        f = self._counting_adapter("identity")
        seen = []
        real = reader.resolve_adapter
        def spy(adapter=None):
            r = real(adapter)
            seen.append(r)
            return r
        loads = mock.Mock(wraps=reader.load_adapter)
        with mock.patch.object(reader, "resolve_adapter", spy), mock.patch.object(reader, "load_adapter", loads):
            reader.reconcile_then_gather(self.ws, f)
        modules = {id(r.module) for r in seen if r.module is not None}
        self.assertEqual(len(modules), 1, "one module identity across the pass")
        self.assertEqual(loads.call_count, 1, "the path is resolved once, not once per phase")
        self.assertEqual(self._count(f), 1)
        self.assertIs(reader.resolve_adapter(seen[0]), seen[0], "a Resolved is returned as is")

    def test_the_pass_calls_reconcile_pass_then_gather_and_no_keyword(self):
        """The reminder delegates to the core reader's one pass: the adapter is loaded once,
        reconcile_pass then plain gather(ws), no private keyword."""
        import pending_questions_reader as reader
        calls = []
        fake = mock.Mock(spec=["reconcile_pass", "gather"])
        fake.reconcile_pass.side_effect = lambda ws: calls.append(("reconcile_pass", ws)) or {"errors": ["e1"]}
        fake.gather.side_effect = lambda ws: calls.append(("gather", ws)) or {"waiting": [], "notes": ["n"]}
        with mock.patch.object(reader, "load_adapter", return_value=fake) as load:
            g = self.cpq.gather(str(self.minimal))
        self.assertEqual([c[0] for c in calls], ["reconcile_pass", "gather"])
        self.assertEqual({c[1] for c in calls}, {Path(self.cpq.WORKSPACE)})
        self.assertEqual(fake.gather.call_args, mock.call(Path(self.cpq.WORKSPACE)), "plain gather(ws): no private keyword")
        self.assertEqual(g["notes"][:1], ["reconcile: FAILED — e1"])
        self.assertEqual({str(c.args[0]) for c in load.call_args_list}, {str(self.minimal)}, "one adapter, loaded through the reader")

    def test_an_adapter_that_refuses_a_second_execution_still_reads_through_the_public_entry(self):
        """Round 40: the public entry loads the adapter, then the reminder used to execute the file
        again outside the failure handler. One load now serves both; the full public path exits 0."""
        once = self.tmp / "once_adapter.py"
        once.write_text(
            "from pathlib import Path as _P\n"
            "_m = _P(__file__).with_suffix('.loaded')\n"
            "if _m.exists():\n"
            "    raise ImportError('adapter second load down')\n"
            "_m.write_text('1')\n" + MINIMAL +
            "def remind(argv, workspace):\n"
            "    import pending_questions_remind as reminder\n"
            "    return reminder.main(list(argv), workspace)\n")
        (self.ws / "room.json").write_text(json.dumps([{"ask_id": "ask-once", "title": "loaded once?"}]))
        shim = _load("pq_shim_r37", REPO / "src" / "check-pending-questions.py")
        import pending_questions_reader as reader
        import workspace_default
        reader._LOADED.clear()
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = shim.main(["--store-adapter", str(once), "--notify", "--force"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("second load down", out.getvalue() + err.getvalue())
        self.assertIn("Notified: 1 pending questions", out.getvalue())
        [f] = [p for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-pending-q-")]
        self.assertIn("loaded once?", f.read_text())

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


# An adapter whose top level counts its executions, raises while a `.down` sidecar exists,
# and reports the store `label`; `extra` runs before the contract body.
def _adapter_source(label, extra=""):
    return (
        "from pathlib import Path as _P\n"
        "_c = _P(__file__).with_suffix('.count')\n"
        "_c.write_text(str(int(_c.read_text()) + 1) if _c.exists() else '1')\n"
        "if _P(__file__).with_suffix('.down').exists():\n"
        "    raise ImportError('transient dependency unavailable')\n"
        + extra + MINIMAL.replace('"minimal"', f'"{label}"'))


class _Gate:
    """A stand-in for the reader's lock: `arrived` fires when a caller reaches it; `inner` is
    what it then takes, so the test holds the entry while it replaces the adapter file."""

    def __init__(self, inner, arrived):
        self.inner, self.arrived = inner, arrived

    def __enter__(self):
        self.arrived.set()
        return self.inner.__enter__()

    def __exit__(self, *a):
        return self.inner.__exit__(*a)


class Round42LoaderLifecycle(unittest.TestCase):
    """keweichen at 711b9f7f9: a load failure was the one retained exception object, re-raised
    (and growing) for the process lifetime; the cache key was sampled before the lock, so a file
    replaced while waiting was published under the old key; the successful public reminder path
    resolved its adapter twice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-r42-"))
        self.ws = self.tmp / "ws"
        for d in ("results", "state", "logs"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "room.json").write_text(json.dumps([{"ask_id": "ask-ok", "title": "live?"}]))
        self.f = self.tmp / "adapter.py"
        reader._LOADED.clear()

    def _count(self):
        c = self.f.with_suffix(".count")
        return int(c.read_text()) if c.exists() else 0

    def _replace(self, source):
        tmp = self.f.with_name("incoming.py")
        tmp.write_text(source)
        import os
        os.replace(tmp, self.f)

    def test_a_transient_load_failure_recovers_after_the_failure_window(self):
        """Her repro: the adapter raises while its dependency is unavailable; with the dependency
        back and the file unchanged, a later operation must read live again. The failure is served
        inside FAILURE_TTL_SEC (one operation, the polls behind it), then the file is retried."""
        self.f.write_text(_adapter_source("v1"))
        self.f.with_suffix(".down").write_text("")
        first = reader.gather(self.ws, self.f)
        self.assertTrue(first["unavailable"])
        self.assertIn("transient dependency unavailable", first["reason"])
        self.f.with_suffix(".down").unlink()
        second = reader.gather(self.ws, self.f)
        self.assertTrue(second["unavailable"], "inside the window the one failure is served, not re-executed")
        self.assertEqual(self._count(), 1)
        later = time.monotonic() + reader.FAILURE_TTL_SEC + 1
        with mock.patch.object(reader.time, "monotonic", return_value=later):
            third = reader.gather(self.ws, self.f)
        self.assertFalse(third["unavailable"], third["reason"])
        self.assertEqual([q["ask_id"] for q in third["waiting"]], ["ask-ok"])
        self.assertEqual(self._count(), 2, "retried once the window passed, nothing in between")

    def test_repeated_reads_of_a_cached_failure_keep_a_string_and_a_constant_traceback(self):
        """Her repro: 10,000 reads of one retained exception accumulated 20,002 frames and ~20 MB.
        The cache holds the reason as a string; each hit raises a fresh LoadFailed."""
        import traceback
        self.f.write_text(_adapter_source("v1"))
        self.f.with_suffix(".down").write_text("")
        self.assertTrue(reader.gather(self.ws, self.f)["unavailable"])
        depths, raised = set(), []  # the exceptions are kept alive so their ids are not reused
        for _ in range(1000):
            with self.assertRaises(reader.LoadFailed) as cm:
                reader.load_adapter(self.f)
            depths.add(len(traceback.extract_tb(cm.exception.__traceback__)))
            raised.append(cm.exception)
            self.assertEqual(str(cm.exception), "ImportError: transient dependency unavailable")
        self.assertEqual(len(depths), 1, f"traceback depth must not grow with reads: {sorted(depths)}")
        self.assertEqual(self._count(), 1)
        [entry] = reader._LOADED.values()
        self.assertNotIsInstance(entry, BaseException, "the cache never retains an exception object")
        self.assertIsInstance(entry.reason, str)
        self.assertEqual(len({id(e) for e in raised}), 1000, "a fresh exception per hit, not one re-raised object")

    def test_a_file_replaced_while_a_reader_waits_at_the_lock_is_published_under_its_own_key(self):
        """Her repro: a reader sampled v1's key, waited at the lock while the file became v2, then
        executed v2 and published it under v1's key; the next reader executed v2 again. The identity
        is taken under the lock, so v2 executes once, under its key, and both readers read live."""
        import os
        import threading
        v2 = _adapter_source("v2", "_m = _P(__file__).with_suffix('.v2')\n"
                                   "if _m.exists():\n    raise RuntimeError('v2 executed twice')\n"
                                   "_m.write_text('1')\n")
        self.f.write_text(_adapter_source("v1"))
        inner, arrived, results = threading.Lock(), threading.Event(), []
        inner.acquire()
        a = threading.Thread(target=lambda: results.append(reader.gather(self.ws, self.f)))
        with mock.patch.object(reader, "_LOAD_LOCK", _Gate(inner, arrived)):
            a.start()
            self.assertTrue(arrived.wait(5), "reader A reached the lock")
            self._replace(v2)
            st = self.f.stat()
            os.utime(self.f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a distinct mtime, whatever the clock
            inner.release()
            a.join(5)
            results.append(reader.gather(self.ws, self.f))
        self.assertEqual([(g["unavailable"], g.get("store"), g["reason"]) for g in results],
                         [(False, "v2", None), (False, "v2", None)], "both readers read the live v2, never UNKNOWN")
        self.assertEqual(self._count(), 1, "v2 executed exactly once")
        self.assertEqual(len(reader._LOADED), 1, "one entry, v2 under v2's own identity")
        self.assertEqual(reader.gather(self.ws, self.f)["store"], "v2")
        self.assertEqual(self._count(), 1, "the next read is the cached v2")

    def test_a_file_replaced_during_its_execution_is_served_under_the_bytes_each_reader_ran(self):
        """Thread A executes v1 (slow) under the lock; the file becomes v2 meanwhile. A publishes
        the bytes it ran under their digest; B reads the file again and executes v2 once; v1's
        entry goes (one version per path), and the next read is the cached v2."""
        import threading
        v1 = _adapter_source("v1", "import time as _t\n_P(__file__).with_suffix('.started').write_text('')\n_t.sleep(0.4)\n")
        self.f.write_text(v1)
        started, results = self.f.with_suffix(".started"), []
        a = threading.Thread(target=lambda: results.append(reader.gather(self.ws, self.f)))
        a.start()
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.02)
        self.assertTrue(started.exists(), "v1 is executing")
        self._replace(_adapter_source("v2"))
        a.join(5)
        results.append(reader.gather(self.ws, self.f))
        self.assertEqual([(g["unavailable"], g.get("store")) for g in results], [(False, "v1"), (False, "v2")])
        self.assertEqual(self._count(), 2, "v1 once, v2 once")
        self.assertEqual(len(reader._LOADED), 1, "v1's identity is not retained beside v2's")
        reader.gather(self.ws, self.f)
        self.assertEqual(self._count(), 2, "the next read is the cached v2")

    def test_a_same_size_same_second_rewrite_is_new_bytes_not_a_cache_hit(self):
        """A stat key (path, mtime, size) is the same for this rewrite, and the stale __pycache__
        entry would run v1 again: the identity is the bytes, and they are what is compiled."""
        import os
        self.f.write_text(_adapter_source("v1"))
        self.assertEqual(reader.gather(self.ws, self.f)["store"], "v1")
        st = self.f.stat()
        self._replace(_adapter_source("v2"))  # the same length
        os.utime(self.f, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual((self.f.stat().st_mtime_ns, self.f.stat().st_size), (st.st_mtime_ns, st.st_size))
        self.assertEqual(reader.gather(self.ws, self.f)["store"], "v2")
        self.assertEqual(self._count(), 2)
        self.assertEqual(len(reader._LOADED), 1, "the old identity's module is not retained")

    def test_the_flagless_public_entry_carries_its_one_resolved_through_the_reminder(self):
        """Her repro: the stable flagless path measured load_adapter_calls=2 — the entry resolved
        once, then the shipped `remind` re-injected `__file__` and the reminder resolved the path
        again. The entry's one Resolved is now handed to `remind(resolved=...)`, through
        `reminder.main(adapter=...)`, and every phase returns it as is."""
        import pending_questions_remind as reminder
        import workspace_default
        shim = _load("pq_shim_r42", REPO / "src" / "check-pending-questions.py")
        sys.modules.pop("pending_questions_room_db", None)
        seen, loads = [], mock.Mock(wraps=reader.load_adapter)
        real = reader.resolve_adapter
        def spy(adapter=None):
            r = real(adapter)
            seen.append((adapter, r))
            return r
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws), \
             mock.patch.object(shim, "declared", return_value=skill_roots.Declaration(SHIPPED, None)), \
             mock.patch.object(reader, "load_adapter", loads), mock.patch.object(reader, "resolve_adapter", spy), \
             mock.patch.object(reminder, "main", wraps=reminder.main) as rmain, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = shim.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(loads.call_count, 1, f"one load for the whole invocation; load args {[c.args for c in loads.call_args_list]}")
        first = seen[0][1]
        self.assertIsInstance(first, reader.Resolved)
        self.assertIsNotNone(first.module, first.reason)
        self.assertEqual([a for a, _ in seen[1:]], [first] * (len(seen) - 1), "every later phase was handed the one Resolved")
        self.assertTrue(all(r is first for _, r in seen), "returned as is, never re-resolved")
        self.assertEqual(rmain.call_args.kwargs.get("adapter"), first, "the reminder got the Resolved, not a file")
        self.assertNotIn("--store-adapter", rmain.call_args.args[0], "no `__file__` re-injected on the successful path")

    def test_a_two_arg_remind_still_gets_argv_and_workspace(self):
        shim = _load("pq_shim_r42b", REPO / "src" / "check-pending-questions.py")
        self.assertFalse(shim._takes_resolved(lambda argv, ws: 0))
        self.assertTrue(shim._takes_resolved(lambda argv, ws, resolved=None: 0))
        self.assertTrue(shim._takes_resolved(lambda argv, ws, **kw: 0))
        self.assertFalse(shim._takes_resolved(3), "not a callable: the two-arg call, whose error is the adapter's")


if __name__ == "__main__":
    unittest.main()
