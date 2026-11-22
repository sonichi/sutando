"""The recovery click grammar the bridge's reply handler uses (`ag2_sparrow.hitl.recovery`, vendored
from `src/hitl/recovery.py`): which ids are recovery clicks, their labels, and the hand-off and
receipt files. Every case runs against the vendored copy, and against the src/ copy when this
package sits in its repository.

Run:
    python3 packages/ag2-sparrow/tests/test_hitl_recovery.py
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow.hitl import recovery as vendored

COPIES = [("vendored", vendored)]
_SRC = PKG_ROOT.parents[1] / "src" / "hitl" / "recovery.py"
if _SRC.is_file():
    _spec = importlib.util.spec_from_file_location("src_hitl_recovery", _SRC)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    COPIES.append(("src", _mod))


class Grammar(unittest.TestCase):
    def test_the_allowlisted_ids_parse_and_nothing_else_does(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                for aid, want in (("retry_now", ("retry_now", None)), ("refresh_options", ("refresh_options", None)),
                                  ("switch_model:sonnet-5-1m-context", ("switch_model", "sonnet-5-1m-context")),
                                  ("switch_runtime:codex", ("switch_runtime", "codex")),
                                  ("switch_account:acct_3f9a2b", ("switch_account", "acct_3f9a2b"))):
                    self.assertEqual(rec.parse(aid), want, aid)
                    self.assertTrue(rec.is_recovery(aid))
                for bad in (None, 3, "", "rm -rf /", "switch_model:", "switch_model:Opus", "switch_model:a b",
                            "switch_runtime:../x", "switch_account:me@example.com", "open_terminal", "opt1"):
                    self.assertIsNone(rec.parse(bad), bad)
                    self.assertFalse(rec.is_recovery(bad))

    def test_ids_are_built_only_from_the_grammar(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.action_id("switch_model", "haiku"), "switch_model:haiku")
                self.assertEqual(rec.action_id("restart_core"), "restart_core")
                with self.assertRaises(ValueError):
                    rec.action_id("switch_model", "Bad Key")

    def test_labels_name_what_the_runtime_calls_each_choice(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.label("switch_model:haiku", model_names={"haiku": "Haiku 4.5"}),
                                 "Switch to Haiku 4.5")
                self.assertEqual(rec.label("switch_model:haiku"), "Switch to haiku")
                self.assertEqual(rec.label("switch_runtime:codex", {"codex": "Codex"}), "Switch to Codex")
                self.assertEqual(rec.label("switch_runtime:codex"), "Switch to codex")
                self.assertEqual(rec.label("switch_account:acct_3f9a2b"), "Switch to account acct_3f9a2b")
                self.assertEqual(rec.label("refresh_options"), "Refresh options")
                with self.assertRaises(ValueError):
                    rec.label("nope")

    def test_only_the_purchase_is_tagged_apart(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.kind_of(rec.BUY_CREDITS), rec.PURCHASE_KIND)
                self.assertEqual(rec.kind_of(rec.RETRY_NOW), rec.ACTION_KIND)


class Files(unittest.TestCase):
    def test_a_click_is_handed_off_and_its_receipt_read_back(self):
        for name, rec in COPIES:
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                reply = SimpleNamespace(hitl_id="hr_01J9Z8K2", expected_revision=3,
                                        action_id="switch_model:haiku", guard="g")
                path = rec.write_action(ws, reply, "@owner:s.space", "!dm:s.space", now=7)
                self.assertEqual(path, rec.actions_dir(ws) / "hr_01J9Z8K2.json")
                body = json.loads(path.read_text())
                self.assertEqual((body["action_id"], body["sender"], body["at"]),
                                 ("switch_model:haiku", "@owner:s.space", 7))
                with self.assertRaises(ValueError):
                    rec.write_action(ws, SimpleNamespace(hitl_id="x", expected_revision=1, action_id="rm",
                                                         guard="g"), "s", "r")
                got = rec.receipt("hr_01J9Z8K2", "inc_0123abcd", "switch_model:haiku", "done", None, 9.5,
                                  {"state": "running"})
                self.assertEqual(got["at"], 9)
                rec.write_receipt(ws, got)
                self.assertEqual(rec.read_receipt(ws, "hr_01J9Z8K2"), got)
                self.assertIsNone(rec.read_receipt(ws, "hr_missing"))
                (rec.receipts_dir(ws) / "hr_list.json").write_text("[1]")
                self.assertIsNone(rec.read_receipt(ws, "hr_list"), "a non-object receipt is no receipt")
                for bad in ({**got, "outcome": "maybe"},):
                    with self.assertRaises(ValueError):
                        rec.write_receipt(ws, bad)
                with self.assertRaises(ValueError):
                    rec.receipt("h", "i", "retry_now", "maybe", None, 1)

    def test_an_id_is_one_path_component(self):
        for name, rec in COPIES:
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                body = rec.receipt("../../etc/x", "i", "retry_now", "done", None, 1)
                path = rec.write_receipt(ws, body)
                self.assertEqual(path.parent, rec.receipts_dir(ws))
                self.assertEqual(path.name, "etcx.json")
                with self.assertRaises(ValueError):
                    rec.write_receipt(ws, rec.receipt("/..", "i", "retry_now", "done", None, 1))


if __name__ == "__main__":
    unittest.main()
