#!/usr/bin/env python3
"""The gateway must not deliver a zero-byte result file as a reply.

`_post_ready_results` used file existence as its readiness signal, so an
unwritten result posted an empty body and archived the task as delivered,
orphaning the real answer written moments later.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

# `read_ready_result` stamps ordinary `task-*` results with `[task YYYYMMDD-NNN]`
# at the delivery boundary, so a delivered body is the answer PLUS that prefix.
_STAMP = re.compile(r"^\[task \d{8}-\d{3}\]\n\n")


def _unstamped(body: str) -> str:
    """Body as authored — lets these tests assert delivery, not the stamp."""
    return _STAMP.sub("", body or "")


_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

TID = "task-22d83e59601f3a1fef"


def _load_gateway():
    from ag2_sparrow import remote_gateway_bridge as gw  # noqa: WPS433
    return gw


class _Harness:
    """Point the module's result dirs at a temp tree and record POSTs.

    Restores every patched attribute on exit.
    """

    _PATCH = ("RESULTS_DIR", "ARCHIVE_RESULTS_DIR", "_req",
              "_save_inflight", "_forget_task_room", "_load_task_rooms")

    def __init__(self, gw, tmp: Path):
        self.gw = gw
        self.tmp = tmp
        self.posts: list[dict] = []
        self._saved = {}

    def __enter__(self):
        gw, tmp = self.gw, self.tmp
        for name in self._PATCH:
            self._saved[name] = getattr(gw, name)
        results = tmp / "results"
        results.mkdir(parents=True, exist_ok=True)
        gw.RESULTS_DIR = results
        gw.ARCHIVE_RESULTS_DIR = results / "archive"
        gw._req = lambda method, path, payload=None, **kw: (
            self.posts.append({"method": method, "path": path, "payload": payload}) or {}
        )
        gw._save_inflight = lambda *a, **k: None
        gw._forget_task_room = lambda *a, **k: None
        gw._load_task_rooms = lambda *a, **k: {}
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(self.gw, name, value)
        return False

    @property
    def results(self) -> Path:
        return self.gw.RESULTS_DIR

    def archived(self) -> list[Path]:
        adir = self.gw.ARCHIVE_RESULTS_DIR
        return sorted(adir.glob(f"{TID}-*.txt")) if adir.exists() else []


class _Outcome:
    """Snapshot of one pass, safe to assert on after the harness unwinds."""

    def __init__(self, posts, inflight, archived):
        self.posts = posts
        self.inflight = inflight
        self.archived = archived


class GatewayEmptyResultTest(unittest.TestCase):
    def setUp(self):
        try:
            self.gw = _load_gateway()
        except (Exception, SystemExit) as e:  # noqa: BLE001 - import guard
            self.skipTest(f"gateway bridge not importable: {str(e)[:80]}")

    def _run(self, contents: str | None):
        """Drive one `_post_ready_results` pass over a result file holding
        `contents` (None = no file).

        Results are snapshotted inside the harness: `__exit__` restores the
        real result dirs, so reading them afterwards would glob the operator's
        own archive.
        """
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                if contents is not None:
                    (h.results / f"{TID}.txt").write_text(contents)
                inflight = {TID}
                self.gw._post_ready_results(inflight)
                return _Outcome(posts=list(h.posts),
                                inflight=set(inflight),
                                archived=[p.name for p in h.archived()])

    # -- the defect ---------------------------------------------------------

    def test_zero_byte_result_is_not_delivered(self):
        """An unwritten result must not be POSTed or archived."""
        r = self._run("")
        self.assertEqual(
            r.posts, [],
            "gateway POSTed a zero-byte result",
        )
        self.assertIn(
            TID, r.inflight,
            "gateway dropped the task from in-flight on an empty result",
        )
        self.assertEqual(
            r.archived, [],
            "gateway archived an empty result as delivered",
        )

    def test_whitespace_only_result_is_not_delivered(self):
        """A partial write can leave whitespace, which strips to empty."""
        r = self._run("\n  \n")
        self.assertEqual(r.posts, [], "gateway POSTed a whitespace-only result")
        self.assertIn(TID, r.inflight, "gateway dropped in-flight on whitespace-only result")

    # -- the guard must not swallow real work -------------------------------

    def test_written_result_is_still_delivered(self):
        """A written result still delivers normally."""
        answer = ("AG2Space is a collaborative chat workspace where people and "
                  "AI agents can communicate and work together.")
        r = self._run(answer)
        self.assertEqual(len(r.posts), 1, f"expected exactly one POST, got {r.posts}")
        self.assertEqual(r.posts[0]["path"], "/v1/results")
        self.assertEqual(_unstamped(r.posts[0]["payload"]["body"]), answer)
        self.assertNotIn(TID, r.inflight, "delivered task should leave the in-flight set")
        self.assertEqual(len(r.archived), 1, "delivered result should be archived")

    def test_empty_then_written_delivers_on_the_second_pass(self):
        """First pass skips the empty file; second delivers the answer."""
        answer = "the real answer"
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                rfile = h.results / f"{TID}.txt"
                inflight = {TID}

                rfile.write_text("")
                self.gw._post_ready_results(inflight)
                self.assertEqual(h.posts, [], "first pass delivered the empty file")
                self.assertIn(TID, inflight, "first pass consumed the in-flight id")

                rfile.write_text(answer)
                self.gw._post_ready_results(inflight)
                self.assertEqual(len(h.posts), 1, "second pass did not deliver the answer")
                self.assertEqual(_unstamped(h.posts[0]["payload"]["body"]), answer)
                self.assertNotIn(TID, inflight)

    def test_missing_result_file_is_untouched(self):
        """No result file: unchanged behaviour."""
        r = self._run(None)
        self.assertEqual(r.posts, [])
        self.assertIn(TID, r.inflight)


if __name__ == "__main__":
    unittest.main(verbosity=2)
