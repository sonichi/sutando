"""doc_sync: row-keyed merge onto the CURRENT remote (refuse only on a same-row change on
both sides), structure lines never merged, writer stamp honest when the id is unset, config
failures name the key, and put verified by re-get."""

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
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

    def test_structure_change_refuses(self):
        mine = BASE.replace("prose line", "prose line EDITED")
        merged, applied, conflicts = ds.merge(BASE, mine, BASE, "me")
        self.assertEqual(applied, []); self.assertIn("structure", conflicts[0]); self.assertEqual(merged, BASE)

    def test_writer_stamp_is_honest_when_unset_and_replaces_an_old_stamp(self):
        self.assertEqual(ds.writer_id(None, env={}), "unknown")
        self.assertEqual(ds.writer_id(None, env={"SUTANDO_CORE_ID": "3"}), "3")
        self.assertEqual(ds.writer_id("air", env={"SUTANDO_CORE_ID": "3"}), "air")
        self.assertEqual(ds.stamp("x | y (w:old)", "new"), "x | y (w:new)")


class FakeOps:
    def __init__(self, gets, puts=None):
        self.gets, self.puts, self.calls = list(gets), list(puts or []), []

    def get(self, room, folder, name):
        self.calls.append(("get", folder, name)); return self.gets.pop(0)

    def put(self, room, folder, name, content):
        self.calls.append(("put", folder, name, content)); return self.puts.pop(0)


def ok(content): return {"ok": True, "content": content}
NOT_FOUND = {"ok": False, "reason": "ops/X.md not found"}


class PutFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.ws = Path(self.tmp.name)
        ds.time.sleep = lambda s: None

    def tearDown(self):
        self.tmp.cleanup()

    def _wire(self, fake):
        ds.run_get, ds.run_put = fake.get, fake.put

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

    def test_unverified_reget_is_5_and_base_untouched(self):
        ds.base_path(self.ws, "ops", "X.md").parent.mkdir(parents=True)
        ds.base_path(self.ws, "ops", "X.md").write_text(BASE)
        f = self.ws / "e.md"; f.write_text(edit(BASE, "org/repo#1", "org/repo#1 | mine"))
        fake = FakeOps([ok(BASE), ok(BASE)], puts=[{"ok": True}]); self._wire(fake)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ds.cmd_put("!r", "ops", "X.md", self.ws, f, "me"), 5)
        self.assertEqual(ds.base_path(self.ws, "ops", "X.md").read_text(), BASE)

    def test_missing_room_fails_naming_the_key(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ds.main(["get", "--name", "X.md", "--workspace", str(self.ws)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--room", err.getvalue()); self.assertIn("ROOM_DOC_ROOM", err.getvalue())


if __name__ == "__main__":
    unittest.main()
