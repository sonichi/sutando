#!/usr/bin/env python3
"""TRANSITIONAL (delete with the skill's pending_questions_compat.py): every open entry an
older head wrote to this host's legacy file — an ask-id entry of this branch's earlier heads,
#5003's id-less `ledger_entry` + stamp, main's prose section and main's `- **[label, ts]**`
bullet — gets its row through the normal add_row path and is marked moved in place ONLY once
the row is confirmed complete; a second pass changes nothing; an entry still being asked, a
closed one and another host's file are left alone; the ingest is wired into the explicit pass
and the report names each host's last mutation, a missing file included."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "pending-questions" / "scripts"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SKILL))
pqs = importlib.import_module("pending_questions_store")
compat = importlib.import_module("pending_questions_compat")

_spec = importlib.util.spec_from_file_location("rdb", REPO / "tests" / "pending-questions-room-db.test.py")
rdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdb)
HOST = rdb.HOST
SENT = "**Sent:** queued owner-dm via proactive-ask-old.txt at 2026-09-01T00:00:00Z"


def legacy_entry(ask_id, question="Pick a launch date?", sent=SENT, status="open", fields=True):
    """The shape this branch's earlier heads wrote: quoted text, Status, Ask id, pq-fields, Sent."""
    data = {"question": question, "context": None, "default_action": "Tuesday", "reason": None,
            "options": [["Hold", "wait"]], "priority": "High", "asked_at": 1_790_000_000.0}
    fl = "<!-- pq-fields: " + base64.b64encode(json.dumps(data).encode()).decode() + " -->\n" if fields else ""
    return (f"## 2026-09-29T00:00:00Z — {question}\n\n> {question}\n\n**Status:** {status}\n"
            f"**Ask id:** {ask_id}\n{fl}{sent}\n\n")


def base_entry(question="Ship the release?", ask_id="ask-1759000000000-1-abcdef", stamped=True, context=None):
    """What #5003 (the PR's base) writes: `ledger_entry` — heading, block-quoted text, `**Status:** open`,
    the `(sending <ask id>)` placeholder — which its `stamp` then replaces with the queued line.
    No **Ask id:** line exists in this shape."""
    text = question + (f"\n\nContext: {context}" if context else "")
    quote = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())
    sent = f"**Sent:** queued owner-dm via proactive-{ask_id}.txt at 2026-09-30T10:00:00Z" if stamped \
        else f"**Sent:** (sending {ask_id})"
    return f"## 2026-09-30T10:00:00Z — {question}\n\n{quote}\n\n**Status:** open\n{sent}\n\n"


MAIN_PROSE = ("## Rename the repo before the launch?\n\nThe org name changes next week; the redirect "
              "costs a day of CI.\nDefault: rename after launch.\n\n")
MAIN_RESOLVED = "## Drop Python 3.8?\n\nDone last week.\n\n**Status:** resolved\n\n"
MAIN_BULLET = "- **[CLA check, 2026-09-20]** PR #1753 has no CLA signal; merge anyway?\n"


class _Ws(rdb._Ws):
    def setUp(self):
        super().setUp()
        self.pq = self.ws / "hosts" / HOST / "pending-questions.md"

    def file(self, *entries):
        self.pq.write_text("# Open\n\n" + "".join(entries) + "# Resolved\n\n## [RESOLVED] old\n\n**Status:** resolved\n"
                           "**Ask id:** ask-archived\n**Sent:** x\n")


class TestIngest(_Ws):
    def test_an_old_head_entry_gets_its_row_and_is_marked_moved_once(self):
        self.file(legacy_entry("ask-old"))
        db = self.db()
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual((moved, errors), (["ask-old"], []))
        [row] = db.open_entries()
        self.assertEqual((row["ask_id"], row["host"], row["title"]), ("ask-old", HOST, "Pick a launch date?"))
        self.assertIn("**Approve** -> Tuesday\n**Hold** -> wait", row["body"])
        self.assertIn(SENT, row["body"])
        self.assertEqual(db.client.rows(pqs.DB_SCHEMA)[0]["cells"]["priority"], "high")
        text = self.pq.read_text()
        self.assertIn("**Status:** moved — as row q-ask-old", text)
        self.assertEqual(text.count("**Status:**"), 2, "only the entry's own line changed")
        self.assertIn("## [RESOLVED] old", text, "the archive is untouched")
        calls_before = len(db.client.calls)
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []))
        self.assertEqual(len(db.client.calls), calls_before, "a second pass reads nothing from the room")
        self.assertEqual(self.pq.read_text(), text)
        self.assertEqual(len(db.entries()), 1)

    def test_the_bases_stamped_entry_is_ingested_and_its_placeholder_one_is_left_alone(self):
        """#5003's writer emits no **Ask id:** line: an id-less, digest-keyed open entry."""
        self.file(base_entry(), base_entry("Still asking?", "ask-live", stamped=False))
        db = self.db()
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual(errors, [])
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].startswith("legacy-"), moved)
        [row] = db.open_entries()
        self.assertEqual(row["title"], "Ship the release?")
        self.assertIn("> Ship the release?", row["body"])
        self.assertIn("**Sent:** queued owner-dm via proactive-ask-1759000000000-1-abcdef.txt", row["body"])
        text = self.pq.read_text()
        self.assertIn(f"**Status:** moved — as row q-{moved[0]}", text)
        self.assertIn("**Sent:** (sending ask-live)", text)
        self.assertEqual(text.count("**Status:** open"), 1, "the live one keeps its open status")
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []))

    def test_mains_prose_and_bullets_are_ingested_and_its_settled_ones_are_not(self):
        self.file(MAIN_BULLET, "\n", MAIN_PROSE, MAIN_RESOLVED, "## [RESOLVED] already answered\n\nold\n\n")
        db = self.db()
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual(errors, [])
        self.assertEqual(len(moved), 2, moved)
        titles = sorted(e["title"] for e in db.open_entries())
        self.assertEqual(titles, ["CLA check, 2026-09-20", "Rename the repo before the launch?"])
        bodies = {e["title"]: e["body"] for e in db.open_entries()}
        self.assertIn("Default: rename after launch.", bodies["Rename the repo before the launch?"])
        self.assertIn("**Sent:** delivery not recorded — moved from the per-host file", bodies["Rename the repo before the launch?"])
        self.assertIn("PR #1753 has no CLA signal", bodies["CLA check, 2026-09-20"])
        text = self.pq.read_text()
        self.assertRegex(text, r"## Rename the repo before the launch\?\n\n\*\*Status:\*\* moved — as row q-legacy-")
        self.assertRegex(text, r"merge anyway\? \*\*Status:\*\* moved — as row q-legacy-[0-9a-f]{16}\n")
        self.assertIn("## Drop Python 3.8?\n\nDone last week.\n\n**Status:** resolved", text)
        self.assertNotIn("already answered\n\nold\n\n**Status:**", text)
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []), "idempotent by digest")
        self.assertEqual(len(db.entries()), 2)

    def test_a_digest_key_is_stable_across_the_mark_and_distinct_per_entry(self):
        self.assertEqual(compat.digest_key(MAIN_PROSE), compat.digest_key(MAIN_PROSE.replace("\n\n", "\n\n**Status:** moved — as row x\n\n", 1)))
        self.assertNotEqual(compat.digest_key(MAIN_PROSE), compat.digest_key(MAIN_BULLET))
        self.assertTrue(re.fullmatch(r"legacy-[0-9a-f]{16}", compat.digest_key(MAIN_PROSE)))

    def test_entries_still_being_asked_or_closed_are_left_alone(self):
        self.file(legacy_entry("ask-live", sent="**Sent:** (sending ask-live)"),
                  legacy_entry("ask-done", status="answered"))
        db = self.db()
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []))
        self.assertEqual(db.entries(), [])
        self.assertNotIn("moved", self.pq.read_text())

    def test_an_entry_without_fields_gets_a_row_from_its_text(self):
        self.file(legacy_entry("ask-plain", question="Rename the repo?", fields=False))
        db = self.db()
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db)[0], ["ask-plain"])
        [row] = db.open_entries()
        self.assertIn("Rename the repo?", row["body"])
        self.assertIn(SENT, row["body"])

    def test_a_crash_between_the_row_and_the_mark_is_finished_by_the_next_pass(self):
        self.file(legacy_entry("ask-old"))
        db = self.db()
        with mock.patch.object(compat, "_mark_moved", side_effect=OSError("disk full")):
            moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual(moved, [])
        self.assertIn("disk full", errors[0])
        self.assertEqual(len(db.entries()), 1)
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), (["ask-old"], []))
        self.assertEqual(len(db.entries()), 1, "the row is never written twice")
        self.assertIn("**Status:** moved", self.pq.read_text())

    def test_a_crash_between_the_row_and_its_body_leaves_the_file_entry_open_until_the_row_is_complete(self):
        """The row exists but is marked incomplete: the file entry must NOT be marked moved; the
        next pass resumes the row through add_row, confirms it, and only then marks the file."""
        self.file(legacy_entry("ask-old"))
        doc = rdb.fake_client.FakeDoc()
        real, state = doc.put_row_body, {"fail": True}

        async def dies_once(*a, **k):
            if state["fail"]:
                state["fail"] = False
                raise ConnectionError("socket closed between row and body")
            return await real(*a, **k)
        doc.put_row_body = dies_once
        db = self.db(rdb.InProcClient(doc))
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, db)
        self.assertEqual(moved, [])
        self.assertIn("socket closed", errors[0])
        [row] = db.entries()
        self.assertTrue(row["incomplete"])
        self.assertIn("**Status:** open", self.pq.read_text(), "not marked moved while the row is incomplete")
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), (["ask-old"], []))
        [row] = db.entries()
        self.assertFalse(row["incomplete"])
        self.assertEqual([e["ask_id"] for e in db.open_entries()], ["ask-old"])
        self.assertIn("**Status:** moved — as row q-ask-old", self.pq.read_text())

    def test_a_row_write_failure_leaves_the_file_entry_open_for_the_next_pass(self):
        self.file(legacy_entry("ask-old"))
        moved, errors = compat.ingest_legacy_file_entries(self.ws, HOST, self.db(rdb.InProcClient(fail="down")))
        self.assertEqual(moved, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("**Status:** open", self.pq.read_text())

    def test_no_file_or_another_hosts_file_is_nothing_to_do(self):
        db = self.db()
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []))
        other = self.ws / "hosts" / "other-host" / "pending-questions.md"
        other.parent.mkdir()
        other.write_text("# Open\n\n" + legacy_entry("ask-theirs"))
        self.assertEqual(compat.ingest_legacy_file_entries(self.ws, HOST, db), ([], []))
        self.assertEqual(db.entries(), [])

    def test_the_ingest_is_wired_into_the_explicit_pass_only(self):
        self.file(legacy_entry("ask-old"))
        db = self.db()
        rec = pqs.reconcile_pending(db, self.ws, HOST)
        self.assertEqual((rec["moved"], rec["errors"]), (["ask-old"], []))
        store_src = (SKILL / "pending_questions_store.py").read_text()
        self.assertEqual(store_src.count("ingest_legacy_file_entries("), 1, "one call site")
        callers = []
        for root in ("src", "scripts", "skills", "packages"):
            for p in (REPO / root).rglob("*.py"):
                if re.search(r"(import|from) pending_questions_compat\b", p.read_text(errors="replace")):
                    callers.append(p.relative_to(REPO).as_posix())
        self.assertEqual(callers, ["skills/pending-questions/scripts/pending_questions_store.py"])

    def test_the_module_states_its_removal_condition(self):
        doc = compat.__doc__
        self.assertIn("docs/migration-transition-window.md", doc)
        self.assertIn("30 days", doc)
        self.assertIn("skills/pending-questions/scripts/pending_questions_compat.py report", doc)
        self.assertRegex(doc, r"NEWEST mtime")


class TestReport(_Ws):
    def test_each_host_is_reported_a_missing_file_explicitly_and_the_clock_starts_at_the_newest(self):
        self.pq.write_text("# Open\n")
        os.utime(self.pq, (1_700_000_000, 1_700_000_000))
        (self.ws / "hosts" / "newer-host").mkdir()
        newer = self.ws / "hosts" / "newer-host" / "pending-questions.md"
        newer.write_text("# Open\n")
        os.utime(newer, (1_700_086_400, 1_700_086_400))
        (self.ws / "hosts" / "bare-host").mkdir()
        rows = compat.report(self.ws)
        self.assertEqual([(h, m) for h, _, m in rows],
                         [("bare-host", None), ("newer-host", 1_700_086_400.0), (HOST, 1_700_000_000.0)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(compat.main(["report", "--workspace", str(self.ws)]), 0)
        text = out.getvalue()
        self.assertIn("bare-host: no legacy file at", text)
        self.assertIn(f"{HOST}: last mutation of the legacy file 2023-11-14T22:13:20Z", text)
        self.assertIn("30-day clock starts at the newest: 2023-11-15T22:13:20Z", text)
        self.assertNotIn("/Users/", text.replace(str(self.ws), ""))

    def test_the_ingests_own_mark_is_a_mutation_the_clock_sees(self):
        self.file(legacy_entry("ask-old"))
        os.utime(self.pq, (1_700_000_000, 1_700_000_000))
        compat.ingest_legacy_file_entries(self.ws, HOST, self.db())
        self.assertGreater(compat.report(self.ws)[0][2], time.time() - 60)


if __name__ == "__main__":
    unittest.main()
