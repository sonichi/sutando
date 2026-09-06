#!/usr/bin/env python3
"""current_track.py is the one writer: append and rotate share a lock, nothing is lost, an
oversized newest entry is refused loudly instead of reported as nothing to do."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROTATE = REPO / "scripts" / "current-track-rotate.py"
APPEND = REPO / "scripts" / "current-track-append.py"
WRITE = REPO / "scripts" / "current-track-write.py"
sys.path.insert(0, str(REPO / "src"))
import current_track as ct  # noqa: E402


def load(path):
    """Import a hyphen-named script so its main() runs in-process (coverage sees it)."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class Cli:
    def __init__(self, mod, stdin=None):
        self.mod, self.stdin = mod, stdin

    def __call__(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        old_in = sys.stdin
        try:
            if self.stdin is not None:
                sys.stdin = io.StringIO(self.stdin)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = self.mod.main([str(a) for a in argv])
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.stdin = old_in
        return type("R", (), {"returncode": rc, "stdout": out.getvalue(), "stderr": err.getvalue()})()


def fixture(n_entries=40, size=600):
    pre = "# current track\n\nMain goal: keep the loop honest.\n\n"
    entries = [f"## 2026-09-{i%30+1:02d}T{i%24:02d}:00Z — entry {i}\n" + ("x" * size) + "\n\n" for i in range(n_entries)]
    return pre, entries


def run(*argv, stdin=None):
    return subprocess.run([sys.executable, *map(str, argv)], input=stdin, capture_output=True, text=True)


def _size_of(t):
    return len(t.encode('utf-8'))


class Plan(unittest.TestCase):
    def test_under_cap_is_a_noop(self):
        pre, ents = fixture(3); text = pre + "".join(ents)
        r = ct.plan(text, 1 << 20)
        self.assertEqual((r.head, r.archived, r.oversized), (text, "", False))

    def test_head_plus_archive_is_the_original_and_newest_kept(self):
        pre, ents = fixture(); text = pre + "".join(ents)   # no pins in this fixture
        r = ct.plan(text, 8 * 1024)
        self.assertEqual(pre + r.archived + r.head[len(pre):], text)
        self.assertTrue(r.head.startswith(pre))
        self.assertIn("entry 39", r.head); self.assertNotIn("entry 0\n", r.head)
        self.assertLessEqual(len(r.head.encode()), 8 * 1024)
        self.assertFalse(r.oversized)

    def test_no_headings_means_nothing_to_archive_but_over_budget(self):
        r = ct.plan("plain prose " * 1000, 100)
        self.assertEqual(r.archived, ""); self.assertTrue(r.oversized)

    def test_oversized_newest_entry_is_kept_whole_and_flagged(self):
        pre, ents = fixture(5, 600)
        big = "## 2026-09-06T02:00Z — the giant\n" + ("y" * 40_000) + "\n"
        text = pre + "".join(ents) + big
        r = ct.plan(text, 32 * 1024)
        self.assertTrue(r.oversized)
        self.assertEqual(r.head, pre + big)              # kept whole, never cut
        self.assertEqual(r.archived, "".join(ents))      # everything older still leaves


class PinnedEntries(unittest.TestCase):
    """An owner hold must survive its own rotation; age-only rotation lifted it silently."""

    HOLD = ("## 2026-01-01T00:00Z — HOLD: hands off #3166 and #3317, in force until 2026-09-08\n"
            "owner instruction, oldest entry in the file\n\n")

    def corpus(self, n=60, size=900):
        pre, _ = fixture(0)
        filler = "".join(f"## 2026-09-0{i%9+1}T0{i%9}:00Z — entry {i}\n" + ("x" * size) + "\n\n"
                         for i in range(n))
        return pre + self.HOLD + filler

    def test_a_hold_survives_at_every_budget_the_defect_did_not(self):
        text = self.corpus()
        for keep in (8 * 1024, 16 * 1024, 32 * 1024):
            r = ct.plan(text, keep)
            # The LIVE copy must be in the head. The archive also carries a
            # historical copy at its own position; that is the ordering contract.
            self.assertIn("hands off #3166", r.head, f"hold archived at keep={keep}")
            self.assertIn("entry 59", r.head)          # newest ordinary entry still kept
            self.assertIn("entry 0", r.archived)       # ordinary old entries still rotate

    def test_pin_none_reproduces_the_reported_loss(self):
        r = ct.plan(self.corpus(), 8 * 1024, pin=None)
        self.assertNotIn("hands off #3166", r.head)
        self.assertIn("hands off #3166", r.archived)

    def test_entries_keep_their_order_with_a_pinned_one_hoisted(self):
        r = ct.plan(self.corpus(), 8 * 1024)
        head = r.head
        self.assertLess(head.index("hands off #3166"), head.index("entry 59"))

    def test_a_custom_pin_replaces_the_default_vocabulary(self):
        # Strip EVERY default-vocabulary token, or the default pin keeps it and
        # the test proves nothing about --pin.
        text = self.corpus().replace(
            "HOLD: hands off #3166 and #3317, in force until 2026-09-08",
            "KEEPME: quietly about #3166 and #3317")
        self.assertNotIn("quietly", ct.plan(text, 8 * 1024).head)
        r = ct.plan(text, 8 * 1024, pin=re.compile("KEEPME"))
        self.assertIn("quietly", r.head)

    def test_pinned_alone_over_budget_is_oversized_never_dropped(self):
        big = "## 2026-01-01T00:00Z — HOLD: hands off\n" + ("y" * 40_000) + "\n\n"
        pre, _ = fixture(0)
        r = ct.plan(pre + big + self.corpus()[len(pre):], 32 * 1024)
        self.assertTrue(r.oversized)
        self.assertIn("hands off", r.head)


    def test_a_middle_pin_is_held_back_and_the_rest_archives_in_order(self):
        """A pin is not archived while pinned; the ordinary entries around it still go."""
        E = lambda n, body: f"## 2026-0{n}-01T00:00Z — {body}\n" + ("x" * 900) + "\n\n"
        text = ("# t\n\n" + E(1, "A") + E(2, "B")
                + E(3, "P HOLD: hands off") + E(4, "C") + E(5, "D newest"))
        r = ct.plan(text, 2400)
        self.assertNotIn("hands off", r.archived)
        self.assertIn("hands off", r.head)
        name = lambda e: e.splitlines()[0].split("— ")[1]
        head, arch = {name(e) for e in ct.split(r.head)[1]}, [name(e) for e in ct.split(r.archived)[1]]
        ordinary = [name(e) for e in ct.split(text)[1] if "hands off" not in e]
        self.assertEqual(arch, [n for n in ordinary if n not in head])
        self.assertTrue(arch, "nothing rotated, so the case never exercised the pin")

    def test_retiring_a_middle_pin_adds_no_second_copy(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        E = lambda st, n: f"## {st} — {n}\n" + ("x" * 100) + "\n\n"
        p.write_text("# t\n\n" + E("2026-01-01", "A") + E("2026-02-01", "B")
                     + E("2026-03-01", "P HOLD: hands off") + E("2026-04-01", "C"))
        ct.rotate(p, 350); ct.rotate(p, 240)
        ct.rotate(p, 120, pin=None)                        # the hold retires
        self.assertEqual(archive.read_text().count("hands off"), 1)
        names = [e.splitlines()[0].split("— ")[1] for e in ct.split(archive.read_text())[1]]
        self.assertEqual(names[:3], ["A", "B", "P HOLD: hands off"])

    def test_a_repeated_ordinary_entry_across_rotations_is_two_records(self):
        """Nothing cancels an outgoing entry by content: a real repeat is a second record."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        E = lambda st, n: f"## {st} — {n}\n" + ("x" * 100) + "\n\n"
        A, B, C = E("2026-01-01", "A"), E("2026-02-01", "B"), E("2026-03-01", "C")
        p.write_text("# t\n\n" + A + B)
        ct.rotate(p, 120, pin=None)
        p.write_text(p.read_text() + A + C)
        ct.rotate(p, 120, pin=None)
        total = archive.read_text().count("— A") + p.read_text().count("— A")
        self.assertEqual(total, 2, "a genuinely repeated entry was cancelled by an older copy")

    def test_the_refusal_names_the_cause_pins_or_one_giant_entry(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        pre, _ = fixture(0)
        p = Path(d.name) / "current-track.md"
        rot = Cli(load(ROTATE))
        # Cause 1: many pinned entries, none of them individually huge.
        holds = "".join(f"## 2026-0{i%9+1}-01T00:00Z — HOLD: hands off #{3000+i}\n" + ("h" * 900) + "\n\n"
                        for i in range(12))
        p.write_text(pre + holds + "".join(f"## 2026-09-0{i%9+1}T00:00Z — entry {i}\n" + ("x" * 500) + "\n\n"
                                            for i in range(20)))
        r = rot(p, "--keep-bytes", "4096")
        self.assertEqual(r.returncode, 3)
        self.assertIn("pinned entries", r.stderr)
        self.assertNotIn("the newest entry alone", r.stderr)
        # Cause 2: one giant unpinned entry, no pins at all.
        p.write_text(pre + "## 2026-09-06T02:00Z — the giant\n" + ("y" * 40_000) + "\n")
        r = rot(p, "--keep-bytes", "32768")
        self.assertEqual(r.returncode, 3)
        self.assertIn("the newest entry alone", r.stderr)
        self.assertNotIn("pinned entries", r.stderr)


    def test_a_pinned_entry_is_not_archived_while_it_is_pinned(self):
        """The live copy is the only copy until the hold retires; nothing is duplicated."""
        r = ct.plan(self.corpus(), 8 * 1024)
        self.assertIn("hands off #3166", r.head)
        self.assertNotIn("hands off #3166", r.archived)
        self.assertIn("entry 0", r.archived)


    def test_the_archive_is_ordered_by_departure_not_by_entry_stamp(self):
        """P,A,B,C: the pin outlives A and B in the head, so it lands after them."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        P = "## 2026-01-01T00:00Z — HOLD: hands off #3166\nowner instruction\n\n"
        A = "## 2026-05-01T00:00Z — A\n" + ("a" * 3000) + "\n\n"
        B = "## 2026-06-01T00:00Z — B\n" + ("b" * 3000) + "\n\n"
        C = "## 2026-09-06T00:00Z — C newest\n" + ("c" * 3000) + "\n\n"
        p.write_text("# t\n\n" + P + A + B + C)
        archive = p.with_name("current-track-archive.md")

        ct.rotate(p, 5 * 1024)                      # A and B leave; P is held back
        self.assertIn("hands off #3166", p.read_text())
        self.assertNotIn("hands off", archive.read_text())

        ct.rotate(p, 1024, pin=None)                # the hold retires and leaves
        after = archive.read_text()
        self.assertEqual(after.count("hands off #3166"), 1)
        self.assertLess(after.index("— A"), after.index("— B"))
        self.assertLess(after.index("— B"), after.index("hands off"))
        every = after + p.read_text()
        for name in ("hands off", "— A", "— B", "— C newest"):
            self.assertIn(name, every)

    def test_reconstruction_is_the_archive_then_whatever_the_head_still_holds(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        p.write_text(self.corpus())
        original = p.read_text()
        ct.rotate(p, 8 * 1024)
        archive = p.with_name("current-track-archive.md").read_text()
        _, orig_entries = ct.split(original)
        _, arch_entries = ct.split(archive)
        orig_order = [e.splitlines()[0] for e in orig_entries]
        arch_order = [e.splitlines()[0] for e in arch_entries]
        # Same relative order the file had; the fixture's stamps are not
        # monotonic, so sorting them would test the fixture, not the contract.
        self.assertEqual(arch_order, [h for h in orig_order if h in arch_order])


    def test_a_repeated_identical_entry_is_two_records_not_one(self):
        """The same text written twice is two records; nothing cancels an entry by content."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        P = "## 2026-01-01T00:00Z — HOLD: hands off #3166\nowner instruction\n\n"
        big = lambda n, c: f"## 2026-0{n}-01T00:00Z — {c}\n" + (c.lower() * 3000) + "\n\n"
        p.write_text("# t\n\n" + P + big(5, "A") + big(9, "C"))

        ct.rotate(p, 4 * 1024)
        self.assertEqual(archive.read_text().count("hands off #3166"), 0)
        self.assertIn("hands off #3166", p.read_text())

        p.write_text(p.read_text() + P + big(9, "D"))   # the SAME text written again later
        ct.rotate(p, 4 * 1024, pin=None)                # both retired
        total = archive.read_text().count("hands off #3166") + p.read_text().count("hands off #3166")
        self.assertEqual(total, 2, "a legitimately repeated entry was dropped as a duplicate")

    def test_a_pin_reaches_the_archive_exactly_once_when_it_retires(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        p.write_text(self.corpus())
        ct.rotate(p, 8 * 1024)
        self.assertEqual(archive.read_text().count("hands off #3166"), 0)
        ct.rotate(p, 1024, pin=None)
        self.assertEqual(archive.read_text().count("hands off #3166"), 1)

    def test_identical_pins_retired_in_separate_generations_are_two_records(self):
        """The reviewer's generation case: same pin text, two lifetimes, two archived records."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        P = "## 2026-01-01T00:00Z — HOLD: hands off #3166\nowner instruction\n\n"
        big = lambda c: f"## 2026-09-01T00:00Z — {c}\n" + (c.lower() * 3000) + "\n\n"
        p.write_text("# t\n\n" + P + big("A") + big("B"))
        ct.rotate(p, 4 * 1024)                      # generation 1: pinned, held back
        ct.rotate(p, 1024, pin=None)                # generation 1 retires
        self.assertEqual(archive.read_text().count("hands off #3166"), 1)
        p.write_text(p.read_text() + P + big("C") + big("D"))
        ct.rotate(p, 4 * 1024)                      # generation 2: the same text, pinned again
        ct.rotate(p, 1024, pin=None)                # generation 2 retires
        self.assertEqual(archive.read_text().count("hands off #3166"), 2)

    def test_odd_stamps_do_not_reorder_the_archive(self):
        """Reversed, equal and missing stamps: order comes from the file, never from headings."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        E = lambda h: f"## {h}\n" + ("x" * 900) + "\n\n"
        p.write_text("# t\n\n" + E("2026-09-01T03:00Z A") + E("2026-09-02T01:00Z P HOLD: hands off")
                     + E("2026-09-03T03:00Z B equal-stamp") + E("undated C") + E("2026-09-05T09:00Z D newest"))
        original = [e.splitlines()[0] for e in ct.split(p.read_text())[1]]
        ct.rotate(p, 2400)
        first = [e.splitlines()[0] for e in ct.split(archive.read_text())[1]]
        self.assertNotIn("## 2026-09-02T01:00Z P HOLD: hands off", first)
        self.assertEqual(first, [h for h in original if h in first])   # file order, no reordering
        ct.rotate(p, 1200, pin=None)
        after = [e.splitlines()[0] for e in ct.split(archive.read_text())[1]]
        self.assertEqual(after[:len(first)], first)                    # earlier departures untouched
        pin = "## 2026-09-02T01:00Z P HOLD: hands off"
        self.assertEqual(after.count(pin), 1)
        self.assertGreater(after.index(pin), after.index("## 2026-09-01T03:00Z A"))  # departure, not stamp
        self.assertEqual(sorted(after + [e.splitlines()[0] for e in ct.split(p.read_text())[1]]),
                         sorted(original))                             # every entry, exactly once

    def test_interruption_on_either_side_duplicates_but_never_loses(self):
        """Archive-first is a deliberate choice: retry repeats a batch, it never drops one."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        body = lambda n, c: f"## 2026-09-0{n}T00:00Z — {c}\n" + (c.lower() * 3000) + "\n\n"
        p.write_text("# t\n\n" + body(1, "A") + body(2, "B") + body(3, "C"))

        class Boom(Exception):
            pass

        # (i) before either write: nothing is committed, the retry is clean.
        with self.assertRaises(Boom):
            ct.rotate(p, 4 * 1024, _between_read_and_replace=self._raiser(Boom))
        self.assertFalse(archive.exists())

        # (ii) after the archive write, before the head commit.
        real = ct.os.replace
        def boom(a, b):
            if str(b).endswith("current-track.md"):
                raise Boom("interrupted before the head commit")
            return real(a, b)
        ct.os.replace = boom
        self.addCleanup(setattr, ct.os, "replace", real)
        with self.assertRaises(Boom):
            ct.rotate(p, 4 * 1024)
        ct.os.replace = real
        self.assertEqual(archive.read_text().count("— A"), 1)
        self.assertIn("— A", p.read_text())          # still in the head: no loss

        ct.rotate(p, 4 * 1024)                       # retry
        self.assertEqual(archive.read_text().count("— A"), 2)   # the documented duplicate
        self.assertNotIn("— A", p.read_text())
        ct.append(p, "## 2026-09-06T00:00Z — after the retry\n")
        self.assertIn("after the retry", p.read_text())

    def _raiser(self, exc):
        def go():
            raise exc("interrupted before any write")
        return go

    def test_an_edit_through_replace_leaves_no_archived_copy(self):
        """The documented limit: replace() rewrites the head and archives nothing."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"
        archive = p.with_name("current-track-archive.md")
        ct.replace(p, "# t\n\n## 2026-01-01 — alpha\nbody\n\n## 2026-02-01 — beta\nbody\n")
        ct.replace(p, "# t\n\n## 2026-02-01 — beta\nbody\n")
        self.assertNotIn("alpha", p.read_text())
        self.assertFalse(archive.exists(), "replace() must not be believed to archive")

    def test_a_newest_first_file_keeps_the_newest_entries(self):
        """context-reconstruct PREPENDS a rewritten state block, so newest-last is not universal."""
        E = lambda st, n: f"## STATE {st} — {n}\n" + ("x" * 900) + "\n\n"
        text = ("# t\n\n" + E("2026-09-06T08:33Z", "newest")
                + E("2026-09-04T21:27Z", "middle") + E("2026-09-01T10:00Z", "oldest"))
        r = ct.plan(text, 1200)
        self.assertIn("newest", r.head)
        self.assertNotIn("newest", r.archived)
        self.assertIn("oldest", r.archived)
        self.assertEqual(_size_of(r.head) + _size_of(r.archived), _size_of(text))

    def test_an_append_only_file_still_keeps_its_tail(self):
        """The orientation fix must not invert the ordinary case."""
        E = lambda st, n: f"## {st} — {n}\n" + ("x" * 900) + "\n\n"
        text = ("# t\n\n" + E("2026-09-01T10:00Z", "oldest")
                + E("2026-09-04T21:27Z", "middle") + E("2026-09-06T08:33Z", "newest"))
        r = ct.plan(text, 1200)
        self.assertIn("newest", r.head)
        self.assertNotIn("newest", r.archived)
        self.assertIn("oldest", r.archived)

    def test_the_over_budget_exit_says_it_wrote_and_how_much(self):
        """rc=3 next to 'nothing was archived' read as a no-op while 213 KB moved."""
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        pre, _ = fixture(0)
        p = Path(d.name) / "current-track.md"
        holds = "".join(f"## 2026-0{i%9+1}-01T00:00Z — HOLD: hands off #{3000+i}\n" + ("h" * 900) + "\n\n"
                        for i in range(12))
        p.write_text(pre + holds + "".join(f"## 2026-09-0{i%9+1}T00:00Z — entry {i}\n" + ("x" * 500) + "\n\n"
                                            for i in range(20)))
        before = p.read_text()
        r = Cli(load(ROTATE))(p, "--keep-bytes", "4096")
        self.assertEqual(r.returncode, 3)
        self.assertNotIn("nothing was archived", r.stderr)
        self.assertIn("ARCHIVED", r.stderr)
        after = p.read_text()
        self.assertNotEqual(before, after, "the exit-3 path claims a write it did not make")
        archive = p.with_name("current-track-archive.md").read_text()
        self.assertEqual(len(after.encode()) + len(archive.encode()), len(before.encode()))

    def test_orientation_survives_same_minute_and_mixed_separators(self):
        """Minute-truncated string compare tied 08:33:50 with 08:33:10 and sorted ' ' before 'T'."""
        E = lambda st, n: f"## STATE {st} — {n}\n" + ("x" * 900) + "\n"
        cases = [("already ordered", "2026-09-06T08:34:10Z", "2026-09-06T08:33:10Z"),
                 ("same minute", "2026-09-06T08:33:50Z", "2026-09-06T08:33:10Z"),
                 ("space sorts below T", "2026-09-06 23:00", "2026-09-06T01:00Z")]
        for why, newest, oldest in cases:
            d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
            p = Path(d.name) / "current-track.md"
            p.write_text(E(newest, "newest") + E(oldest, "oldest"))
            ct.rotate(p, 1200, pin=None)
            archive = p.with_name("current-track-archive.md")
            self.assertIn("newest", p.read_text(), f"{why}: kept the wrong end")
            self.assertNotIn("newest", archive.read_text(),
                             f"{why}: archived the entry a consumer reads first")

    def test_identical_stamps_at_both_ends_ask_the_next_pair(self):
        """Ends tying must not decide; look inward, and say None when nothing decides."""
        E = lambda st, n: f"## STATE {st} — {n}\n" + ("x" * 500) + "\n"
        same = "2026-09-06T08:33:10Z"
        entries = ct.split(E(same, "a") + E("2026-09-06T09:00:00Z", "b") + E(same, "c"))[1]
        self.assertIs(ct._orientation(entries), False)
        entries = ct.split(E(same, "a") + E("2026-09-06T07:00:00Z", "b") + E(same, "c"))[1]
        self.assertIs(ct._orientation(entries), True)
        flat = ct.split(E(same, "a") + E(same, "b"))[1]
        self.assertIsNone(ct._orientation(flat))

    def test_a_coarse_current_stamp_is_not_read_as_older(self):
        """Zero-filling made '2026-09-06' sort below '2026-09-06T08:00:00Z' and archive it."""
        E = lambda st, n: f"## STATE {st} — {n}\n" + ("x" * 900) + "\n"
        cases = [("date-only vs second", "2026-09-06", "2026-09-06T08:00:00Z"),
                 ("minute vs second", "2026-09-06T10:27", "2026-09-06T10:27:02Z")]
        for why, newest, oldest in cases:
            d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
            p = Path(d.name) / "current-track.md"
            p.write_text(E(newest, "newest") + E(oldest, "oldest"))
            ct.rotate(p, 1200, pin=None)
            archive = p.with_name("current-track-archive.md")
            self.assertIn("newest", p.read_text(), f"{why}: the current entry left the head")
            if archive.exists():
                self.assertNotIn("newest", archive.read_text(), f"{why}: archived the current entry")

    def test_shared_precision_decides_and_an_equal_prefix_is_undetermined(self):
        """Compare only what both stamps carry; an equal prefix at differing precision decides nothing."""
        k = ct._stamp_key
        date_only, second = k("## 2026-09-06 — a"), k("## 2026-09-06T08:00:00Z — b")
        self.assertEqual((date_only[1], second[1]), (3, 6))
        self.assertEqual(ct._cmp_stamps(date_only, second), 0)
        self.assertEqual(ct._cmp_stamps(k("## 2026-09-07 — a"), second), 1)
        E = lambda st: f"## STATE {st} — e\n" + ("x" * 300) + "\n"
        self.assertIsNone(ct._orientation(ct.split(E("2026-09-06") + E("2026-09-06T08:00:00Z"))[1]))

    def test_an_undetermined_file_keeps_both_ends(self):
        """No walk direction can be shown correct, so neither end may be archived."""
        E = lambda st, n: f"## STATE {st} — {n}\n" + ("x" * 900) + "\n"
        text = "# t\n\n" + E("2026-09-06", "first") + E("2026-09-06T08:00:00Z", "middle") + E("2026-09-06", "last")
        r = ct.plan(text, 1200)
        self.assertIn("first", r.head)
        self.assertIn("last", r.head)
        self.assertNotIn("first", r.archived)
        self.assertNotIn("last", r.archived)

    def test_protected_endpoints_are_charged_to_the_budget(self):
        """Pre-kept entries are spent budget; charging only pins let the walk fill the cap on top."""
        E = lambda n: f"## STATE 2026-09-06 — e{n}\n" + ("x" * 250) + "\n"
        text = "# t\n\n" + "".join(E(i) for i in range(8))
        r = ct.plan(text, 1000)
        self.assertIsNone(ct._orientation(ct.split(text)[1]))    # the branch under test
        self.assertIn("e0", r.head)
        self.assertIn("e7", r.head)
        self.assertLessEqual(len(r.head.encode()), 1000)
        self.assertFalse(r.oversized)
        self.assertTrue(r.archived, "nothing rotated, so the cap was never exercised")

    def test_endpoints_over_the_cap_still_archive_the_middle(self):
        """The first-entry exception must not fire when both ends are already protected."""
        E = lambda n: f"## STATE 2026-09-06 — e{n}\n" + ("x" * 570) + "\n"
        text = "# t\n\n" + E(0) + E(1) + E(2)
        r = ct.plan(text, 1000)
        self.assertIsNone(ct._orientation(ct.split(text)[1]))
        self.assertIn("e0", r.head)
        self.assertIn("e2", r.head)
        self.assertIn("e1", r.archived)
        self.assertNotIn("e1", r.head, "kept an avoidable third entry over an exhausted budget")
        self.assertTrue(r.oversized)

    def test_cli_pin_flags(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        p = Path(d.name) / "current-track.md"; p.write_text(self.corpus())
        rot = Cli(load(ROTATE))
        r = rot(p, "--keep-bytes", "8192", "--dry-run"); self.assertEqual(r.returncode, 0)
        r = rot(p, "--keep-bytes", "8192", "--pin", "KEEPME", "--no-pin"); self.assertEqual(r.returncode, 2)
        r = rot(p, "--keep-bytes", "8192", "--pin", "([unclosed"); self.assertEqual(r.returncode, 2)
        r = rot(p, "--keep-bytes", "8192"); self.assertEqual(r.returncode, 0)
        self.assertIn("hands off #3166", p.read_text())
        p.write_text(self.corpus())
        r = rot(p, "--keep-bytes", "8192", "--no-pin"); self.assertEqual(r.returncode, 0)
        self.assertNotIn("hands off #3166", p.read_text())


class RotateAndAppend(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.p = Path(self.d.name) / "current-track.md"
        pre, ents = fixture(); self.pre = pre; self.p.write_text(pre + "".join(ents))
        self.archive = self.p.with_name("current-track-archive.md")

    def tearDown(self):
        self.d.cleanup()

    def test_rotate_writes_archive_first_then_replaces_head(self):
        before = self.p.read_text()
        r = ct.rotate(self.p, 8 * 1024)
        self.assertEqual(self.p.read_text(), r.head)
        self.assertEqual(self.pre + self.archive.read_text() + r.head[len(self.pre):], before)

    def test_dry_run_touches_nothing(self):
        before = self.p.read_text()
        r = ct.rotate(self.p, 8 * 1024, dry_run=True)
        self.assertTrue(r.archived); self.assertEqual(self.p.read_text(), before); self.assertFalse(self.archive.exists())

    def test_concurrent_append_during_rotation_survives(self):
        """The reviewer's race: an append between rotate's read and its replace must land in the head."""
        entry = "## 2026-09-06T02:10Z — landed mid-rotation\nkeep me\n"
        done = threading.Event()

        def appender():
            ct.append(self.p, entry); done.set()

        def seam():
            threading.Thread(target=appender, daemon=True).start()
            self.assertFalse(done.wait(0.5))   # blocked on the writer lock while rotate holds it

        ct.rotate(self.p, 8 * 1024, _between_read_and_replace=seam)
        self.assertTrue(done.wait(5))
        self.assertIn(entry, self.p.read_text())
        self.assertNotIn(entry, self.archive.read_text())

    def test_append_cli_and_rotate_cli_share_the_lock_across_processes(self):
        entry = "## 2026-09-06T02:20Z — from the CLI\nvia stdin\n"
        procs = [subprocess.Popen([sys.executable, str(APPEND), str(self.p)], stdin=subprocess.PIPE, text=True) for _ in range(3)]
        rot = subprocess.Popen([sys.executable, str(ROTATE), str(self.p), "--keep-bytes", "8192"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i, pr in enumerate(procs):
            pr.communicate(entry.replace("CLI", f"CLI {i}"))
        rot.communicate()
        self.assertEqual(rot.returncode, 0)
        text = self.p.read_text() + self.archive.read_text()
        for i in range(3):
            self.assertEqual(text.count(f"from the CLI {i}"), 1)


    def test_concurrent_rewrite_during_rotation_survives(self):
        """The reviewer's second race: the skill's rewrite (create/replace) must land after rotation, never under it."""
        new_head = self.pre.replace("keep the loop honest", "OWNER REDIRECTED") + "## 2026-09-06T02:40Z — the new track\nowner redirected\n"
        done = threading.Event()

        def rewriter():
            ct.replace(self.p, new_head); done.set()

        def seam():
            threading.Thread(target=rewriter, daemon=True).start()
            self.assertFalse(done.wait(0.5))   # blocked on the writer lock while rotate holds it

        ct.rotate(self.p, 8 * 1024, _between_read_and_replace=seam)
        self.assertTrue(done.wait(5))
        self.assertEqual(self.p.read_text(), new_head)   # rewrite_survived_head
        self.assertIn("OWNER REDIRECTED", self.p.read_text())

    def test_write_cli_append_and_replace_in_process(self):
        w = load(WRITE)
        r = Cli(w, stdin="## 2026-09-06T02:50Z — appended\n")("append", self.p); self.assertEqual(r.returncode, 0)
        self.assertTrue(self.p.read_text().endswith("appended\n"))
        r = Cli(w, stdin="# fresh head\n\nMain goal: replaced.\n")("replace", self.p); self.assertEqual(r.returncode, 0)
        self.assertEqual(self.p.read_text(), "# fresh head\n\nMain goal: replaced.\n")
        r = Cli(w, stdin="   ")("replace", self.p); self.assertEqual(r.returncode, 1)
        r = Cli(w, stdin="x")("rewrite", self.p); self.assertEqual(r.returncode, 2)
        r = Cli(w, stdin="x")("append"); self.assertEqual(r.returncode, 2)
        a = load(APPEND)
        r = Cli(a, stdin="## alias\n")(self.p); self.assertEqual(r.returncode, 0); self.assertTrue(self.p.read_text().endswith("## alias\n"))
        r = Cli(a, stdin="x")(); self.assertEqual(r.returncode, 2)

    def test_cli_exit_codes_in_process(self):
        rot = Cli(load(ROTATE))
        r = rot(self.p, "--keep-bytes", "8192", "--dry-run"); self.assertEqual(r.returncode, 0); self.assertIn("would move", r.stdout)
        r = rot(self.p, "--keep-bytes", "8192"); self.assertEqual(r.returncode, 0); self.assertIn("moved", r.stdout)
        r = rot(self.p, "--keep-bytes", "8192"); self.assertEqual(r.returncode, 0); self.assertIn("nothing to do", r.stdout)
        r = rot(self.p, "--keep-bytes", "0"); self.assertEqual(r.returncode, 2)
        r = rot(self.p.with_name("absent.md")); self.assertEqual(r.returncode, 1); self.assertIn("cannot read", r.stderr)
        self.p.write_text(self.pre + "## 2026-09-06T02:00Z — the giant\n" + "y" * 40_000 + "\n")
        r = rot(self.p, "--keep-bytes", "32768"); self.assertEqual(r.returncode, 3)
        self.assertIn("STILL OVER BUDGET", r.stderr); self.assertIn("the giant", r.stderr)
        r = rot(self.p, "--keep-bytes", "32768", "--dry-run"); self.assertEqual(r.returncode, 3); self.assertIn("would keep", r.stderr)
        self.p.write_text("plain prose with no heading " * 2000)
        r = rot(self.p, "--keep-bytes", "1024"); self.assertEqual(r.returncode, 3); self.assertIn("the preamble", r.stderr)
        app = load(APPEND)
        r = Cli(app, stdin="   \n")(self.p); self.assertEqual(r.returncode, 1)
        r = Cli(app, stdin="x")(); self.assertEqual(r.returncode, 2)
        r = Cli(app, stdin="## 2026-09-06T02:30Z — no trailing newline")(self.p); self.assertEqual(r.returncode, 0)
        self.assertTrue(self.p.read_text().endswith("no trailing newline\n"))

if __name__ == "__main__":
    unittest.main()
