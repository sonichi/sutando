"""doc_sync: row-keyed merge onto the CURRENT remote (refuse only on a same-row change on
both sides), structure lines never merged, writer stamp honest when the id is unset, config
failures name the key, and put verified by re-get."""

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("doc_sync", REPO / "skills" / "agent-room-ops" / "doc_sync.py")
ds = importlib.util.module_from_spec(spec)
sys.modules["doc_sync"] = ds
spec.loader.exec_module(ds)

BASE = "\n".join([
    "# List", "prose line", "## Active",
    "org/repo#1 | shepherd: a | status: active | one",
    "org/repo#2 | shepherd: a | status: active | two",
    "## History",
    "org/repo#9 | shepherd: a | status: merged | nine",
])


def edit(text, key, new_line):
    return "\n".join(new_line if l.startswith(key + " ") else l for l in text.split("\n"))


class MergeTests(unittest.TestCase):
    def test_parse_keys_records_by_id_and_section(self):
        structure, records = ds.parse(BASE)
        self.assertEqual(sorted(records), ["org/repo#1", "org/repo#2", "org/repo#9"])
        self.assertEqual(records["org/repo#9"].section, "History")
        self.assertEqual(structure, ["# List", "prose line", "## Active", "## History"])

    def test_non_conflicting_interleaving_keeps_both_sides(self):
        mine = edit(BASE, "org/repo#1", "org/repo#1 | shepherd: a | status: active | one EDITED")
        remote = BASE.replace("## Active\n", "## Active\norg/repo#3 | shepherd: b | status: active | three (w:b)\n")
        merged, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(conflicts, [])
        self.assertIn("org/repo#3 | shepherd: b | status: active | three (w:b)", merged)  # theirs kept
        self.assertIn("org/repo#1 | shepherd: a | status: active | one EDITED (w:me)", merged)  # mine applied, stamped
        self.assertEqual(applied, ["edit org/repo#1"])

    def test_same_row_changed_on_both_sides_refuses_and_names_the_writer(self):
        mine = edit(BASE, "org/repo#1", "org/repo#1 | mine")
        remote = edit(BASE, "org/repo#1", "org/repo#1 | theirs (w:w3)")
        merged, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(applied, [])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("org/repo#1", conflicts[0]); self.assertIn("w:w3", conflicts[0])
        self.assertEqual(merged, remote)  # nothing rewritten

    def test_a_pure_move_with_identical_text_is_applied(self):
        # The resolution rule says move the row, do not retype it; the merge must see a pure move.
        row = "org/repo#9 | shepherd: a | status: merged | nine"
        kept = [l for l in BASE.split("\n") if l != row]
        kept.insert(kept.index("## Active") + 1, row)
        mine = "\n".join(kept)
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual(conflicts, [])
        self.assertEqual(applied, ["move org/repo#9 History -> Active"])
        self.assertEqual(ds.parse(merged)[1]["org/repo#9"].section, "Active")

    def test_every_delta_is_accounted_for_and_already_present_is_reported(self):
        mine = edit(BASE, "org/repo#1", "org/repo#1 | mine")
        remote = edit(BASE, "org/repo#1", "org/repo#1 | mine")  # someone landed my exact edit first
        merged, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual((merged, conflicts), (remote, []))
        self.assertEqual(applied, ["already-present org/repo#1"])  # not silence

    def test_duplicate_key_in_the_callers_file_refuses(self):
        mine = BASE.replace("## History\n", "## History\norg/repo#2 | shepherd: a | status: merged | two\n")  # copied, not moved
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual((merged, applied), (BASE, []))
        self.assertIn("org/repo#2", conflicts[0]); self.assertIn("YOUR file", conflicts[0])

    def test_conflict_returns_the_remote_untouched_even_after_a_partial_apply(self):
        mine = edit(edit(BASE, "org/repo#1", "org/repo#1 | mine"), "org/repo#2", "org/repo#2 | mine2")
        remote = edit(BASE, "org/repo#2", "org/repo#2 | theirs (w:w1)")
        merged, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(merged, remote); self.assertEqual(applied, []); self.assertEqual(len(conflicts), 1)

    def test_blank_line_changes_in_structure_do_not_refuse(self):
        mine = BASE.replace("## History\n", "\n## History\n")
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual((applied, conflicts), ([], []))

    def test_move_between_sections_is_applied_when_remote_unchanged(self):
        mine = BASE.replace("org/repo#2 | shepherd: a | status: active | two\n", "").replace(
            "## History\n", "## History\norg/repo#2 | shepherd: a | status: merged | two\n")
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual(conflicts, [])
        _, recs = ds.parse(merged)
        self.assertEqual(recs["org/repo#2"].section, "History")
        self.assertEqual(applied, ["move org/repo#2 Active -> History"])

    def test_retire_applies_and_retire_vs_remote_edit_conflicts(self):
        mine = BASE.replace("org/repo#2 | shepherd: a | status: active | two\n", "")
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual((applied, conflicts), (["retire org/repo#2"], []))
        self.assertNotIn("org/repo#2", merged)
        remote = edit(BASE, "org/repo#2", "org/repo#2 | theirs")
        _, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(applied, []); self.assertIn("org/repo#2", conflicts[0])

    def test_add_lands_at_top_of_its_section_and_duplicate_add_conflicts(self):
        mine = BASE.replace("## Active\n", "## Active\norg/repo#4 | new row\n")
        merged, applied, _ = ds.merge(BASE, mine, BASE, "me")
        lines = merged.split("\n")
        self.assertEqual(lines[lines.index("## Active") + 1], "org/repo#4 | new row (w:me)")
        remote = BASE.replace("## Active\n", "## Active\norg/repo#4 | their row (w:w2)\n")
        _, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(applied, []); self.assertIn("org/repo#4", conflicts[0])

    def test_a_key_duplicated_remotely_refuses_by_name(self):
        # A row in two sections has no single identity; editing "the first one" silently is wrong.
        mine = edit(BASE, "org/repo#2", "org/repo#2 | mine")
        remote = BASE.replace("## History\n", "## History\norg/repo#2 | shepherd: a | status: merged | two\n")
        self.assertEqual(ds.duplicates(remote), {"org/repo#2": 2})
        merged, applied, conflicts = ds.merge(BASE, mine, remote, "me")
        self.assertEqual(applied, []); self.assertIn("org/repo#2", conflicts[0]); self.assertIn("2 times", conflicts[0])
        self.assertEqual(merged, remote)

    def test_duplicate_report_names_sections_and_lines(self):
        remote = BASE.replace("## History\n", "## History\norg/repo#2 | shepherd: a | status: merged | two\n")
        self.assertEqual(ds.duplicate_report(BASE), [])
        self.assertEqual(ds.duplicate_report(remote), ["org/repo#2: L5 Active, L7 History"])

    def test_history_convention_retirement_is_a_record_move_not_a_structure_change(self):
        # The list retires a row as `key — MERGED <date> … was: <old row>`; that is the same record, moved.
        row = "org/repo#2 | shepherd: a | status: active | two"
        kept = [l for l in BASE.split("\n") if l != row]
        kept.insert(kept.index("## History") + 1, "org/repo#2 — MERGED 2026-09-02 14:16Z by owner. was: " + row)
        mine = "\n".join(kept)
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual(conflicts, [])
        self.assertEqual(applied, ["move org/repo#2 Active -> History"])
        self.assertEqual(ds.parse(merged)[1]["org/repo#2"].section, "History")
        self.assertIn("org/repo#2 — MERGED 2026-09-02 14:16Z by owner. was: " + row + " (w:me)", merged)

    def test_duplicates_see_both_row_shapes(self):
        remote = BASE + "\norg/repo#1 — MERGED 2026-09-02 by owner. was: org/repo#1 | one"
        self.assertEqual(ds.duplicates(remote), {"org/repo#1": 2})
        self.assertNotIn("org/repo#1", ds.duplicates(BASE))

    def test_structure_change_refuses(self):
        mine = BASE.replace("prose line", "prose line EDITED")
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual(applied, []); self.assertIn("structure", conflicts[0]); self.assertEqual(merged, BASE)

    def test_retire_already_gone_remotely_is_absorbed_and_edit_of_a_removed_row_conflicts(self):
        gone = "\n".join(l for l in BASE.split("\n") if not l.startswith("org/repo#9 "))
        merged, applied, conflicts = ds.merge(BASE, gone, gone, "me")  # I retired #9; remote already did
        self.assertEqual((applied, conflicts, merged), (["already-present org/repo#9"], [], gone))
        mine = edit(BASE, "org/repo#1", "org/repo#1 | mine")
        removed = "\n".join(l for l in BASE.split("\n") if not l.startswith("org/repo#1 "))
        merged, applied, conflicts = ds.merge(BASE, mine, removed, "me")
        self.assertEqual(applied, []); self.assertEqual(merged, removed)
        self.assertEqual(conflicts, ["org/repo#1: edited by me, removed remotely"])

    def test_move_into_a_section_the_remote_lacks_conflicts(self):
        base = BASE + "\n## Parked"                      # base and mine share the header: no structure change
        mine = "\n".join(l for l in base.split("\n") if not l.startswith("org/repo#9 ")) + "\norg/repo#9 | shepherd: a | status: merged | nine"
        merged, applied, conflicts = ds.merge(base, mine, BASE, "me")  # remote never had ## Parked
        self.assertEqual(applied, []); self.assertEqual(merged, BASE)
        self.assertEqual(conflicts, ["org/repo#9: section '## Parked' not found remotely"])

    def test_add_already_present_remotely_is_absorbed_and_add_into_a_missing_section_conflicts(self):
        row = "org/repo#3 | shepherd: a | status: active | three"
        mine = BASE.replace("## Active\n", "## Active\n" + row + "\n")
        merged, applied, conflicts = ds.merge(BASE, mine, mine, "me")   # remote already carries my add
        self.assertEqual((applied, conflicts, merged), (["already-present org/repo#3"], [], mine))
        base = BASE + "\n## Parked"
        mine = base + "\norg/repo#4 | parked by me"
        merged, applied, conflicts = ds.merge(base, mine, BASE, "me")     # remote never had ## Parked
        self.assertEqual((applied, merged), ([], BASE))
        self.assertEqual(conflicts, ["org/repo#4: section '## Parked' not found remotely"])

    def test_writer_stamp_is_honest_when_unset_and_replaces_an_old_stamp(self):
        self.assertEqual(ds.writer_id(None, env={}), "unknown")
        self.assertEqual(ds.writer_id(None, env={"SUTANDO_CORE_ID": "3"}), "3")
        self.assertEqual(ds.writer_id("air", env={"SUTANDO_CORE_ID": "3"}), "air")
        self.assertEqual(ds.stamp("x | y (w:old)", "new"), "x | y (w:new)")


class FakeOps:
    def __init__(self, gets, puts=None):
        self.gets, self.puts, self.calls = list(gets), list(puts or []), []
        self.unqueued = 0

    def get(self, room, folder, name):
        self.calls.append(("get", folder, name)); return self.gets.pop(0)

    def put(self, room, folder, name, content):
        self.calls.append(("put", folder, name, content))
        if self.puts: return self.puts.pop(0)
        self.unqueued += 1; return {"ok": True}


def ok(content): return {"ok": True, "content": content}
NOT_FOUND = {"ok": False, "reason": "ops/X.md not found"}


class PutFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.ws = Path(self.tmp.name)
        ds.time.sleep = lambda s: None

    def tearDown(self):
        self.tmp.cleanup()

    def _wire(self, fake):
        real_get, real_put = ds.run_get, ds.run_put
        ds.run_get, ds.run_put = fake.get, fake.put
        self.addCleanup(lambda: setattr(ds, "run_get", real_get) or setattr(ds, "run_put", real_put))
        self.addCleanup(lambda: self.assertEqual(
            fake.unqueued, 0, "a put ran with no queued response — mis-wired test"))

    def test_get_writes_base_and_retries_a_false_not_found_once(self):
        fake = FakeOps([NOT_FOUND, ok(BASE)]); self._wire(fake)
        self.assertEqual(ds.cmd_get("!r", "ops", "X.md", self.ws), 0)
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), BASE)
        self.assertEqual(len(fake.calls), 2)

    def test_put_without_base_refuses(self):
        fake = FakeOps([]); self._wire(fake)
        f = self.ws / "e.md"; f.write_text(BASE)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 4)
        self.assertEqual(fake.calls, [])

    def test_put_merges_onto_moved_remote_and_verifies_by_reget(self):
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        mine = edit(BASE, "org/repo#1", "org/repo#1 | mine"); f = self.ws / "e.md"; f.write_text(mine)
        remote = BASE.replace("## Active\n", "## Active\norg/repo#3 | theirs (w:b)\n")
        expected = ds.merge(BASE, mine, remote, "me")[0]
        fake = FakeOps([ok(remote), ok(expected)], puts=[{"ok": True}]); self._wire(fake)
        self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 0)
        self.assertEqual([c[0] for c in fake.calls], ["get", "put", "get"])
        self.assertEqual(fake.calls[1][3], expected)
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), expected)  # base refreshed

    def test_put_conflict_refuses_without_writing(self):
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        f = self.ws / "e.md"; f.write_text(edit(BASE, "org/repo#1", "org/repo#1 | mine"))
        fake = FakeOps([ok(edit(BASE, "org/repo#1", "org/repo#1 | theirs (w:w3)"))]); self._wire(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 4)
        self.assertFalse(any(c[0] == "put" for c in fake.calls))

    def test_a_writer_landing_between_the_pre_put_read_and_the_put_is_lost_undetectably(self):
        # The known-lost case: the window is one round trip, not zero. B edits a DIFFERENT row after
        # our pre-put read; the unconditional put overwrites it and the re-get verifies our bytes.
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        f = self.ws / "e.md"; f.write_text(edit(BASE, "org/repo#1", "org/repo#1 | mine"))
        b_row = "org/repo#2 | theirs, IMPORTANT EDIT (w:b)"
        state = {"remote": BASE, "puts": 0, "b_landed": False}

        def get(room, folder, name):
            content = state["remote"]
            if not state["b_landed"]:  # B lands exactly once, right after our pre-put read
                state["remote"] = edit(state["remote"], "org/repo#2", b_row); state["b_landed"] = True
            return ok(content)

        def put(room, folder, name, content):
            state["puts"] += 1; state["remote"] = content; return {"ok": True}

        real_get, real_put = ds.run_get, ds.run_put
        ds.run_get, ds.run_put = get, put
        self.addCleanup(lambda: setattr(ds, "run_get", real_get) or setattr(ds, "run_put", real_put))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 0)  # reports success
        self.assertEqual(state["puts"], 1)
        self.assertIn("org/repo#1 | mine (w:me)", state["remote"])
        self.assertNotIn("IMPORTANT EDIT", state["remote"])  # B's row is gone, and nothing said so
        # Fails only once a conditional write closes the window: then fix the docstring, then this.

    def test_unverified_reget_is_5_and_base_untouched(self):
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        f = self.ws / "e.md"; f.write_text(edit(BASE, "org/repo#1", "org/repo#1 | mine"))
        fake = FakeOps([ok(BASE), ok(BASE)], puts=[{"ok": True}]); self._wire(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 5)
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), BASE)

    def test_put_distinguishes_nothing_changed_from_already_present(self):
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        # (a) edited == base: a genuine no-op
        f = self.ws / "e.md"; f.write_text(BASE)
        self._wire(FakeOps([ok(BASE)]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 0)
        self.assertIn("no row changes", out.getvalue()); self.assertNotIn("already present", out.getvalue())
        # (b) edited != base but the remote already carries my exact edit: reported by name, not silence
        mine = edit(BASE, "org/repo#1", "org/repo#1 | mine"); f.write_text(mine)
        fake = FakeOps([ok(mine)]); self._wire(fake)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 0)
        self.assertIn("already present remotely", out.getvalue()); self.assertIn("org/repo#1", out.getvalue())
        self.assertFalse(any(c[0] == "put" for c in fake.calls))  # nothing written
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), mine)  # base advanced to the remote

    def test_transport_seams_pass_room_folder_name_through_to_doc(self):
        import types
        calls = []
        fake_doc = types.SimpleNamespace(
            doc_get=lambda room, folder, name: calls.append(("get", room, folder, name)) or {"ok": True, "content": "x"},
            doc_put=lambda room, content, folder, name: calls.append(("put", room, folder, name, content)) or {"ok": True})
        saved = sys.modules.get("doc"); sys.modules["doc"] = fake_doc
        try:
            self.assertEqual(ds.run_get("!r", "ops", "X.md")["content"], "x")
            self.assertTrue(ds.run_put("!r", "ops", "X.md", "body")["ok"])
        finally:
            if saved is None: del sys.modules["doc"]
            else: sys.modules["doc"] = saved
        self.assertEqual(calls, [("get", "!r", "ops", "X.md"), ("put", "!r", "ops", "X.md", "body")])

    def test_not_found_twice_is_3_and_a_plain_failure_is_1(self):
        fake = FakeOps([NOT_FOUND, NOT_FOUND]); self._wire(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_get("!r", "ops", "X.md", self.ws), 3)
        self.assertEqual(len(fake.calls), 2)
        fake = FakeOps([{"ok": False, "reason": "gateway 502"}]); self._wire(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_duplicates("!r", "ops", "X.md"), 1)

    def test_duplicates_command_reports_count_and_main_dispatches_with_and_without_workspace(self):
        twice = BASE + "\norg/repo#1 | copied into History"
        fake = FakeOps([ok(twice), ok(BASE), ok(BASE)]); self._wire(fake)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ds.main(["duplicates", "--room", "!r", "--folder", "ops", "--name", "X.md"]), 0)
            self.assertEqual(ds.main(["get", "--room", "!r", "--folder", "ops", "--name", "X.md", "--workspace", str(self.ws)]), 0)
        self.assertIn("1 duplicated key(s) in ops/X.md", out.getvalue())
        self.assertIn("org/repo#1:", out.getvalue())
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), BASE)
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            ds._required(None, "--name", "ROOM_DOC_NAME")
        self.assertEqual(cm.exception.code, 2)

    def test_transport_failures_are_1_at_each_step_ok_without_content_pre_put_read_and_put(self):
        fake = FakeOps([{"ok": True}]); self._wire(fake)                       # ok=true, no content string
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_get("!r", "ops", "X.md", self.ws), 1)
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        f = self.ws / "e.md"; f.write_text(edit(BASE, "org/repo#1", "org/repo#1 | mine"))
        fake = FakeOps([{"ok": False, "reason": "gateway 502"}]); self._wire(fake)   # pre-put read fails
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 1)
        self.assertFalse(any(c[0] == "put" for c in fake.calls))
        fake = FakeOps([ok(BASE)], puts=[{"ok": False, "reason": "denied"}]); self._wire(fake)  # put refused
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 1)
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), BASE)   # base untouched

    def test_missing_room_fails_naming_the_key(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ds.main(["get", "--name", "X.md", "--workspace", str(self.ws)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--room", err.getvalue()); self.assertIn("ROOM_DOC_ROOM", err.getvalue())


if __name__ == "__main__":
    unittest.main()
