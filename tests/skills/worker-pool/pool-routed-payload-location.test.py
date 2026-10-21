#!/usr/bin/env python3
"""A routed task's payload lives in its recipient's folder, not in tasks/ (#4836).

The core's readers list tasks/*.txt and treat every entry as the core's; a
payload left there after routing is adopted, answered, and the outbox item is
spent before the worker's answer. So: body first, then sentinel, then the tasks/
copy goes; the resolver hands the worker the body; a finished body is archived
where history readers look; a withdrawn hold puts the body back in tasks/.
"""
import contextlib
import importlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))
pd, rt, pr, rie, prh = (importlib.import_module(m) for m in
                        ("pool_delivery", "pool_router", "pool_roster", "resolve_inbox_entry",
                         "pool_route_handler"))

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
TEXT = "id: task-1\nsource: ag2space\nchannel_id: !bound:example.test\ntask: hello\n"


def roster(bindings):
    return {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}}, "bindings": bindings}


class T(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir()
        (self.ws / "tasks" / "task-1.txt").write_text(TEXT)
        self.task = {"id": "task-1", "source": "ag2space", "channel_id": ROOM}

    def tearDown(self):
        self.td.cleanup()

    def test_routed_payload_leaves_tasks_and_sits_beside_the_sentinel(self):
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        body = pd.body_path(self.ws, A, "task-1")
        self.assertTrue(body.is_file(), "body in the recipient's folder")
        self.assertEqual(body.read_text(), TEXT)
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel")
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").glob("task-*.txt")), [],
                         "no reader of tasks/*.txt sees a routed task")

    def test_core_target_keeps_the_payload_in_tasks(self):
        out = rt.route(self.ws, self.task, roster({}))
        self.assertEqual(out["targets"], [pr.CORE])
        self.assertTrue((self.ws / "tasks" / "task-1.txt").is_file())
        self.assertFalse(list((self.ws / "deliveries").glob(f"*/{pd.BODY_DIR}/task-1.txt")))

    def test_fan_out_gives_every_recipient_a_body_before_the_copy_goes(self):
        out = rt.route(self.ws, self.task, roster({ROOM: [A, B]}))
        self.assertEqual(sorted(out["delivered"]), sorted([A, B]))
        for w in (A, B):
            self.assertEqual(pd.body_path(self.ws, w, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def test_replay_after_a_crash_between_body_and_sentinel_delivers_once(self):
        # A pass died after staging the body: no sentinel, no tasks/ copy either
        # (a later pass may have retired it). The replay must still deliver.
        folder = self.ws / "deliveries" / A
        pd.body_path(self.ws, A, "task-1").parent.mkdir(parents=True)
        pd.body_path(self.ws, A, "task-1").write_text(TEXT)
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        self.assertEqual(out["skipped"], [])
        out2 = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out2["already"], [A])
        self.assertEqual([p.name for p in folder.glob("task-1.*")], ["task-1.txt"], "one sentinel")
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "one body")

    def test_no_payload_anywhere_is_still_no_payload(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["skipped"], [A])
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_resolver_hands_the_worker_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        entry = str(self.ws / "deliveries" / A / "task-1.txt")
        got = rie.resolve(entry)
        self.assertEqual(got, Path(os.path.abspath(pd.body_path(self.ws, A, "task-1"))))
        self.assertEqual(got.read_text(), TEXT)
        p = subprocess.run([sys.executable, str(SCRIPTS / "resolve_inbox_entry.py"), entry],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(), [os.path.abspath(pd.body_path(self.ws, A, "task-1"))],
                         "the payload is the body, named for its task")

    def test_resolver_still_finds_a_pre_move_payload_in_tasks(self):
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        (folder / "task-1.txt").touch()
        got = rie.resolve(str(folder / "task-1.txt"))
        self.assertEqual(got, Path(os.path.abspath(self.ws / "tasks" / "task-1.txt")))

    def test_read_payload_and_residue_see_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(pd.read_payload(self.ws, "task-1"), TEXT)
        self.assertNotEqual(pd.residue(self.ws, A, "task-1"), "stale-sentinel")

    def test_finished_body_is_archived_by_prune_and_the_sentinel_retired(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        (self.ws / "results" / "task-1.txt").write_text("done\n")
        pd.mark_done(self.ws, A, "task-1", published=True)
        acts = pd.prune_spent(self.ws, A)
        self.assertIn("task-1", acts["retired"])
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual(pd.archived_payload(self.ws, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_withdrawn_hold_returns_the_body_to_the_core(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        pd.clear_pending(self.ws, A, "task-1")
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual((self.ws / "tasks" / "task-1.txt").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_body_is_never_a_sentinel(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(rt.holders(self.ws, "task-1"), [A])
        self.assertIsNone(pd.parse_sentinel(pd.BODY_DIR))
        self.assertEqual([p.name for p in pd.pending(self.ws, A)], ["task-1.txt"], "the body folder is not listed")

    def test_a_symlinked_body_folder_is_refused_not_read(self):
        outside = Path(tempfile.mkdtemp(dir=self.td.name, prefix="outside-"))
        (outside / "task-1.txt").write_text("OUTSIDE-SECRET\n")
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        (folder / "task-1.txt").touch()
        os.symlink(outside, folder / pd.BODY_DIR)
        (self.ws / "tasks" / "task-1.txt").unlink()
        with self.assertRaises(pd.RecipientAliasError):
            pd.payload_location(self.ws, "task-1", A)
        with self.assertRaises(pd.NotDelivered):
            rie.resolve(str(folder / "task-1.txt"))
        (self.ws / "tasks" / "task-2.txt").write_text(TEXT.replace("task-1", "task-2"))
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, {**self.task, "id": "task-2"}, roster({ROOM: A}))
        self.assertFalse((outside / "task-2.txt").exists(), "nothing staged through the alias")

    # A recipient folder that is a symlink must never lead a lookup outside deliveries/.
    def _alias(self):
        outside = Path(tempfile.mkdtemp(dir=self.td.name, prefix="outside-"))
        (outside / pd.BODY_DIR).mkdir()
        (outside / pd.BODY_DIR / "task-1.txt").write_text("OUTSIDE-SECRET\n")
        (outside / "task-1.txt").touch()
        os.symlink(outside, self.ws / "deliveries" / A)
        (self.ws / "tasks" / "task-1.txt").unlink()
        return outside

    def test_an_aliased_recipient_folder_is_refused_not_read(self):
        self._alias()
        with self.assertRaises(pd.RecipientAliasError):
            pd.payload_location(self.ws, "task-1")
        with self.assertRaises(pd.RecipientAliasError):
            pd.payload_location(self.ws, "task-1", A)
        with self.assertRaises(pd.RecipientAliasError):
            pd.read_payload(self.ws, "task-1")
        with self.assertRaises(OSError):
            prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)

    def test_the_resolver_refuses_a_sentinel_seen_through_an_alias(self):
        self._alias()
        with self.assertRaises(pd.NotDelivered):
            rie.resolve(str(self.ws / "deliveries" / A / "task-1.txt"))

    def test_no_deliveries_root_means_no_body(self):
        (self.ws / "deliveries").rmdir()
        (self.ws / "tasks" / "task-1.txt").unlink()
        self.assertIsNone(pd.payload_location(self.ws, "task-1"))

    def test_read_task_with_the_payload_gone_everywhere_still_raises(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        with self.assertRaises(FileNotFoundError):
            prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)

    def test_read_task_follows_a_moved_payload_to_its_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        got = prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)
        self.assertEqual((got["id"], got["channel_id"]), ("task-1", ROOM))

    def test_core_target_with_no_payload_is_skipped(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({}))
        self.assertEqual(out["skipped"], [pr.CORE])

    # An existing body counts as an earlier pass only when it IS the payload.
    def _plant(self, make):
        folder = self.ws / "deliveries" / A
        (folder / pd.BODY_DIR).mkdir(parents=True)
        make(folder / pd.BODY_DIR / "task-1.txt")
        return folder

    def _refused_and_untouched(self, folder):
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertFalse((folder / "task-1.txt").exists(), "no sentinel over a foreign body")
        self.assertTrue((self.ws / "tasks" / "task-1.txt").is_file(), "the core keeps its copy")

    def test_a_planted_symlink_body_is_refused_without_a_sentinel(self):
        target = Path(self.td.name) / "elsewhere"
        target.write_text("PLANTED\n")
        self._refused_and_untouched(self._plant(lambda b: os.symlink(target, b)))

    def test_a_planted_body_with_other_bytes_is_refused_without_a_sentinel(self):
        self._refused_and_untouched(self._plant(lambda b: b.write_text("PLANTED\n")))

    def test_an_earlier_pass_copy_with_the_same_bytes_is_accepted(self):
        folder = self._plant(lambda b: b.write_text(TEXT))
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        self.assertTrue((folder / "task-1.txt").is_file())
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def _stage(self, existing):
        folder = self.ws / "deliveries" / A / pd.BODY_DIR
        folder.mkdir(parents=True)
        if existing is not None:
            existing(folder / "task-1.txt")
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, dfd)
        return folder, dfd

    def test_stage_eexist_on_the_same_inode_is_a_prior_pass(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(lambda b: os.link(src, b))
        rt._stage_body(src, "task-1.txt", dfd)
        self.assertTrue(os.path.samefile(src, folder / "task-1.txt"))

    def test_a_failed_copy_leaves_no_temp_name_behind(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(None)
        for target, boom in ((rt.shutil, "copyfileobj"), (rt.os, "rename")):
            with self.subTest(fails=boom):
                with mock.patch.object(rt.os, "link", side_effect=OSError(18, "cross-device")), \
                        mock.patch.object(target, boom, side_effect=OSError(28, "no space")):
                    with self.assertRaises(OSError):
                        rt._stage_body(src, "task-1.txt", dfd)
                self.assertEqual(list(folder.iterdir()), [], "no temp name, no body")

    def test_a_planted_symlink_body_with_no_core_copy_gets_no_sentinel(self):
        target = Path(self.td.name) / "elsewhere"
        target.write_text(TEXT)
        folder = self._plant(lambda b: os.symlink(target, b))
        (self.ws / "tasks" / "task-1.txt").unlink()
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertFalse((folder / "task-1.txt").exists())

    def test_stage_copies_through_a_temp_name_when_a_link_is_refused(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(None)
        with mock.patch.object(rt.os, "link", side_effect=OSError(18, "cross-device")):
            rt._stage_body(src, "task-1.txt", dfd)
        self.assertEqual((folder / "task-1.txt").read_text(), TEXT)
        self.assertFalse(os.path.samefile(src, folder / "task-1.txt"), "a copy, not a link")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["task-1.txt"], "no temp name left")

    def test_stage_eexist_on_other_bytes_fails_loudly(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(lambda b: b.write_text("OTHER\n"))
        with self.assertRaises(rt.ConflictingDelivery):
            rt._stage_body(src, "task-1.txt", dfd)
        self.assertEqual((folder / "task-1.txt").read_text(), "OTHER\n")

    def test_stage_eexist_on_a_fifo_fails_loudly(self):
        src = self.ws / "tasks" / "task-1.txt"
        _, dfd = self._stage(lambda b: os.mkfifo(b))
        with self.assertRaises(rt.ConflictingDelivery):
            rt._stage_body(src, "task-1.txt", dfd)

    def test_same_body_check_refuses_what_it_cannot_open_or_compare(self):
        src = self.ws / "tasks" / "task-1.txt"
        _, dfd = self._stage(lambda b: b.write_text(TEXT))
        with self.assertRaises(rt.UnreadableEvidence):
            rt._require_same_body(src, "task-missing.txt", dfd, "task-1")
        with self.assertRaises(rt.UnreadableEvidence):
            rt._require_same_body(self.ws / "tasks" / "gone.txt", "task-1.txt", dfd, "task-1")

    # A sentinel is retired only once its body reached tasks/archive/.
    def _finished(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        (self.ws / "results" / "task-1.txt").write_text("done\n")
        pd.mark_done(self.ws, A, "task-1", published=True)

    def test_a_failed_archive_keeps_the_sentinel_and_the_body(self):
        self._finished()
        with mock.patch.object(pd.os, "link", side_effect=PermissionError(13, "denied")):
            acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], [])
        self.assertIn("task-1", acts["kept"])
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "body kept")
        self.assertFalse(pd.archived_payload(self.ws, "task-1").exists())
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], ["task-1"], "the next pass retires it once the archive works")

    def test_an_existing_archive_lets_the_body_go_and_retires(self):
        self._finished()
        arch = pd.archived_payload(self.ws, "task-1")
        arch.parent.mkdir(parents=True)
        arch.write_text(TEXT)
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], ["task-1"])
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())

    def test_an_archive_holding_the_same_inode_lets_the_body_go(self):
        self._finished()
        arch = pd.archived_payload(self.ws, "task-1")
        arch.parent.mkdir(parents=True)
        os.link(pd.body_path(self.ws, A, "task-1"), arch)
        self.assertEqual(pd.prune_spent(self.ws, A)["retired"], ["task-1"])
        self.assertEqual(arch.read_text(), TEXT)

    # Anything at the archive name that is not provably this body keeps body and sentinel.
    def _collide(self, make):
        self._finished()
        arch = pd.archived_payload(self.ws, "task-1")
        arch.parent.mkdir(parents=True)
        make(arch)
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], [])
        self.assertIn("task-1", acts["kept"])
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "body kept")
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
        return arch

    def test_an_archive_with_other_bytes_keeps_the_body_and_sentinel(self):
        arch = self._collide(lambda a: a.write_text("OTHER\n"))
        self.assertEqual(arch.read_text(), "OTHER\n")

    def test_an_archive_of_the_same_length_but_other_bytes_is_not_the_body(self):
        self._collide(lambda a: a.write_text(TEXT[:-2] + "X\n"))

    def test_an_archive_that_is_a_directory_keeps_the_body_and_sentinel(self):
        self._collide(lambda a: a.mkdir())

    def test_an_archive_that_is_a_fifo_keeps_the_body_and_sentinel(self):
        self._collide(lambda a: os.mkfifo(a))

    def test_an_archive_that_is_a_live_symlink_to_the_same_bytes_is_not_followed(self):
        twin = Path(self.td.name) / "twin.txt"
        twin.write_text(TEXT)
        arch = self._collide(lambda a: os.symlink(twin, a))
        self.assertTrue(arch.is_symlink())

    def _kept_with_archive(self, expect):
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "body kept")
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
        arch = pd.archived_payload(self.ws, "task-1")
        self.assertEqual(arch.read_text() if arch.exists() else None, expect)

    def test_without_hard_links_the_body_and_sentinel_are_kept(self):
        self._finished()
        with mock.patch.object(pd.os, "link", side_effect=OSError(18, "cross-device")):
            self.assertIn("task-1", pd.prune_spent(self.ws, A)["kept"])
        self._kept_with_archive(None)

    def test_an_archive_planted_between_a_probe_and_a_publish_is_never_replaced(self):
        self._finished()
        arch = pd.archived_payload(self.ws, "task-1")
        arch.parent.mkdir(parents=True)

        real_lexists, real_link = os.path.lexists, os.link

        def probe(path):
            # The plant lands right after a probe that saw nothing there.
            if Path(path) == arch:
                arch.write_text("FOREIGN\n")
                return False
            return real_lexists(path)

        def plant_then_link(src, dst, **kw):
            if not arch.exists():
                arch.write_text("FOREIGN\n")
            return real_link(src, dst, **kw)

        for link in (mock.DEFAULT, plant_then_link):
            with self.subTest(link="no hard links" if link is mock.DEFAULT else "link loses the race"):
                with contextlib.suppress(FileNotFoundError):
                    arch.unlink()
                link_kw = ({"side_effect": OSError(18, "cross-device")} if link is mock.DEFAULT
                           else {"side_effect": plant_then_link})
                with mock.patch.object(pd.os, "link", **link_kw), \
                        mock.patch.object(pd.os.path, "lexists", side_effect=probe):
                    self.assertIn("task-1", pd.prune_spent(self.ws, A)["kept"])
                self.assertIn(arch.read_text() if arch.exists() else None, (None, "FOREIGN\n"),
                              "a foreign archive is never overwritten by the body")
                self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT)
                self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file())

    def test_a_symlinked_body_folder_is_not_archived(self):
        self._finished()
        folder = self.ws / "deliveries" / A
        real = Path(tempfile.mkdtemp(dir=self.td.name, prefix="real-"))
        os.rename(folder / pd.BODY_DIR, real / pd.BODY_DIR)
        os.symlink(real / pd.BODY_DIR, folder / pd.BODY_DIR)
        with self.assertRaises(pd.RecipientAliasError):
            pd.prune_spent(self.ws, A)
        self.assertFalse(pd.archived_payload(self.ws, "task-1").exists())
        self.assertTrue((folder / "task-1.txt").is_file(), "sentinel kept")
        self.assertFalse(pd._archive_and_retire(self.ws, A, "task-1"), "the archiver refuses it on its own too")
        self.assertEqual((real / pd.BODY_DIR / "task-1.txt").read_text(), TEXT)

    # A withdrawn hold retires nothing until its body is provably back in tasks/.
    def _held(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        pd.mark_done(self.ws, A, "task-1", published=False)
        return self.ws / "tasks" / "task-1.txt"

    def _all_kept(self):
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "body kept")
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
        self.assertTrue(pd.pending_flag(self.ws, A, "task-1").is_file(), "hold kept")

    def test_a_withdrawn_hold_that_cannot_link_back_keeps_everything(self):
        self._held()
        with mock.patch.object(pd.os, "link", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(OSError):
                pd.clear_pending(self.ws, A, "task-1")
        self._all_kept()
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def test_a_withdrawn_hold_never_takes_a_foreign_core_copy_for_its_own(self):
        core = self._held()
        core.write_text("FOREIGN\n")
        with self.assertRaises(OSError):
            pd.clear_pending(self.ws, A, "task-1")
        self._all_kept()
        self.assertEqual(core.read_text(), "FOREIGN\n")

    def test_a_core_copy_created_after_the_check_is_never_overwritten(self):
        core = self._held()
        real_link = os.link

        def plant_then_link(src, dst, **kw):
            Path(dst).write_text("FOREIGN\n")
            return real_link(src, dst, **kw)

        with mock.patch.object(pd.os, "link", side_effect=plant_then_link):
            with self.assertRaises(OSError):
                pd.clear_pending(self.ws, A, "task-1")
        self._all_kept()
        self.assertEqual(core.read_text(), "FOREIGN\n")

    def test_a_core_copy_holding_the_same_bytes_completes_the_withdrawal(self):
        core = self._held()
        core.write_text(TEXT)
        pd.clear_pending(self.ws, A, "task-1")
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())
        self.assertFalse(pd.pending_flag(self.ws, A, "task-1").exists())
        self.assertEqual(core.read_text(), TEXT)

    def test_a_withdrawal_racing_the_routes_retirement_leaves_one_payload(self):
        # The withdrawal starts at the router's unlink of the core copy, the window between
        # its body check and that unlink; it must wait for the router's locks, then restore.
        import threading
        core = self.ws / "tasks" / "task-1.txt"
        real_unlink = os.unlink
        done = threading.Event()
        errors = []

        def withdraw():
            try:
                pd.clear_pending(self.ws, A, "task-1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            done.set()

        def unlink_in_the_window(path, *a, **kw):
            if Path(str(path)) == core and not done.is_set():
                threading.Thread(target=withdraw, daemon=True).start()
                done.wait(1.0)
            return real_unlink(path, *a, **kw)

        with mock.patch.object(rt.os, "unlink", side_effect=unlink_in_the_window):
            rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertTrue(done.wait(10), "the withdrawal finished")
        self.assertEqual(errors, [])
        survivors = [q for q in (core, pd.body_path(self.ws, A, "task-1")) if q.exists()]
        self.assertEqual([q.read_text() for q in survivors], [TEXT], "exactly one payload survives")

    def _unreadable_body(self, how):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        body = pd.body_path(self.ws, A, "task-1")
        if how == "EACCES":
            @contextlib.contextmanager
            def denied():
                body.chmod(0)
                try:
                    yield
                finally:
                    body.chmod(0o644)
            return body, denied()
        real_open = os.open

        def eio(path, *a, **kw):
            if str(path) == str(body):
                raise OSError(5, "Input/output error", str(path))
            return real_open(path, *a, **kw)

        return body, mock.patch.object(pd.os, "open", side_effect=eio)

    def test_an_unreadable_delivered_body_is_retryable_not_absent_to_the_resolver(self):
        entry = str(self.ws / "deliveries" / A / "task-1.txt")
        for how in ("EACCES", "EIO"):
            with self.subTest(how=how):
                if how == "EIO":
                    self.tearDown(); self.setUp()
                body, ctx = self._unreadable_body(how)
                with ctx:
                    with self.assertRaises(pd.NotDelivered):
                        rie.resolve(entry)
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(rie.main([entry]), 1, "retryable, never the typed no-payload exit")

    def test_an_unreadable_delivered_body_keeps_its_sentinel_through_sweep_and_prune(self):
        for how in ("EACCES", "EIO"):
            with self.subTest(how=how):
                if how == "EIO":
                    self.tearDown(); self.setUp()
                body, ctx = self._unreadable_body(how)
                with ctx:
                    self.assertEqual(pd.residue(self.ws, A, "task-1"), "unreadable")
                    self.assertEqual(pd.sweep(self.ws, A)["kept"], ["task-1"])
                    self.assertEqual(pd.prune_spent(self.ws, A)["kept"], ["task-1"])
                self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
                self.assertTrue(os.path.lexists(body), "body kept")

    # The archiver reads the body through the folder fd it locked, so a swap never redirects it.
    def test_a_body_folder_swapped_before_the_link_never_archives_a_decoy(self):
        self._finished()
        folder = self.ws / "deliveries" / A
        real_link = os.link

        def swap_then_link(src, dst, **kw):
            os.rename(folder / pd.BODY_DIR, folder / "moved")
            (folder / pd.BODY_DIR).mkdir()
            (folder / pd.BODY_DIR / "task-1.txt").write_text("DECOY\n")
            return real_link(src, dst, **kw)

        with mock.patch.object(pd.os, "link", side_effect=swap_then_link):
            self.assertEqual(pd.prune_spent(self.ws, A)["retired"], ["task-1"])
        self.assertEqual(pd.archived_payload(self.ws, "task-1").read_text(), TEXT, "the real body is archived")
        self.assertFalse((folder / "moved" / "task-1.txt").exists(), "and nothing real is stranded")
        self.assertEqual((folder / pd.BODY_DIR / "task-1.txt").read_text(), "DECOY\n")

    def test_boot_sweep_archives_a_finished_body_before_retiring_its_sentinel(self):
        self._finished()
        out = pd.sweep(self.ws, A)
        self.assertEqual(out["retired"], ["task-1"])
        self.assertEqual(pd.archived_payload(self.ws, "task-1").read_text(), TEXT)
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_boot_sweep_keeps_what_it_cannot_archive(self):
        self._finished()
        with mock.patch.object(pd.os, "link", side_effect=PermissionError(13, "denied")):
            out = pd.sweep(self.ws, A)
        self.assertEqual((out["retired"], out["kept"]), ([], ["task-1"]))
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT)
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file())

    def test_a_non_regular_body_is_not_archived_and_the_sentinel_stays(self):
        self._finished()
        b = pd.body_path(self.ws, A, "task-1")
        b.unlink()
        b.mkdir()
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], [])
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file())

    def test_a_sentinel_with_no_body_retires_as_before(self):
        self._finished()
        pd.body_path(self.ws, A, "task-1").unlink()
        self.assertEqual(pd.prune_spent(self.ws, A)["retired"], ["task-1"])


def _main_tree(dest: Path) -> "Path | None":
    """origin/main's own src/, scripts/ and worker-pool skill, unpacked: the artifacts a host runs until it
    restarts them. None when this checkout has no main ref to read them from."""
    for ref in ("origin/main", "main"):
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "-q", ref + "^{commit}"],
                             capture_output=True, text=True)
        if sha.returncode == 0:
            arc = subprocess.run(["git", "-C", str(REPO), "archive", ref, "src", "scripts", "skills/worker-pool"], capture_output=True)
            if arc.returncode != 0:
                return None
            dest.mkdir(parents=True)
            subprocess.run(["tar", "-x", "-C", str(dest)], input=arc.stdout, check=True)
            # The CI runner shows only `Results:` lines; this one proves these suites ran.
            print(f"\nResults: rolling-upgrade suite ran against {ref} {sha.stdout.strip()[:12]}", flush=True)
            return dest
    return None


class ResolvedAnnouncement(unittest.TestCase):
    """A routed task's body is `<inbox>/bodies/<id>.txt`: named for its task, outside the
    folder a watcher lists, so every watcher announces it by an absolute path."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name).resolve() / "ws"
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "tasks" / "task-1.txt").write_text(TEXT)
        rt.route(self.ws, {"id": "task-1", "source": "ag2space", "channel_id": ROOM}, roster({ROOM: A}))
        self.inbox = self.ws / "deliveries" / A
        self.body = pd.body_path(self.ws, A, "task-1")

    def tearDown(self):
        self.td.cleanup()

    def test_the_shim_passes_the_body_path_on(self):
        script = (f'WORKSPACE_DIR="{self.ws}"; source "{REPO}/src/inbox-resolve.sh"; '
                  f'resolve_inbox_entry "$1"; echo "rc=$?"')
        env = dict(os.environ, SUTANDO_INBOX_RESOLVER=str(SCRIPTS / "resolve-inbox-entry"))
        r = subprocess.run(["bash", "-c", script, "_", str(self.inbox / "task-1.txt")], capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(r.stdout.splitlines(), [str(self.body), "rc=0"], r.stderr)

    def sweep(self, role="standby", repo=REPO):
        """One worker watcher (this tree's, or another's) over the inbox, with the shipped
        resolver, until it has decided about the sentinel (an announce or a refusal)."""
        out = Path(tempfile.mkstemp(dir=self.td.name, suffix=".out")[1])
        err = Path(tempfile.mkstemp(dir=self.td.name, suffix=".err")[1])
        env = dict(os.environ, SUTANDO_INBOX_RESOLVER=str(SCRIPTS / "resolve-inbox-entry"),
                   SUTANDO_WORKSPACE_DIR=str(self.ws), SUTANDO_RESULTS_DIR=str(self.ws / "results"),
                   SUTANDO_INSTANCE=A)
        with open(out, "w") as fo, open(err, "w") as fe:
            proc = subprocess.Popen(["bash", str(repo / "src" / "watch-tasks-stream.sh"), str(self.inbox),
                                     "--role", role, "--inbox", str(self.inbox)],
                                    stdout=fo, stderr=fe, env=env, start_new_session=True)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if "TASK_FILE:" in out.read_text() or "already answered" in err.read_text():
                    break
                time.sleep(0.25)
            time.sleep(1.0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        return out.read_text(), err.read_text()

    def test_every_role_announces_a_readable_body_named_for_its_task(self):
        for role in ("standby", "session"):
            with self.subTest(role=role):
                out, err = self.sweep(role)
                self.assertEqual(out.splitlines(), [f"TASK_FILE: {self.body}"], err[-800:])
                self.assertEqual(self.body.name, "task-1.txt")
                self.assertEqual(self.body.read_text(), TEXT, "a session reading the named file gets the task")

    def test_an_answered_routed_task_is_not_dispatched_again_after_a_restart(self):
        (self.ws / "results" / "task-1.txt").write_text("answered already\n")
        out, err = self.sweep()
        self.assertNotIn("TASK_FILE:", out, err[-800:])
        self.assertIn(f"already answered, not dispatching again: {self.body}", err, err[-800:])

    # Watcher announcement -> a notifier's own event loop and key derivation (this tree's or another's).
    def notify(self, runtime, events, repo=REPO):
        import re
        script = repo / "src" / "agent" / runtime / "cli" / "task-notifier.sh"
        text = script.read_text()
        fns = [re.search(rf"^{n}\(\) \{{.*?^\}}", text, re.S | re.M).group(0)
               for n in ("has_result", "task_payload", "enqueue_announced_task")
               + (("task_prompt",) if runtime == "claude" else ())]
        loop = re.search(r'^while :; do\n  event="".*?^done < "\$event_dir/events"', text, re.S | re.M).group(0)
        tmp = Path(tempfile.mkdtemp(dir=self.td.name, prefix=runtime + "-"))
        for d in ("queue", "payload", "inflight"):
            (tmp / d).mkdir()
        (tmp / "events").write_text(events)
        harness = "\n".join([
            "set -u",
            'log_notifier() { printf "%s\\n" "$1" >> "$STAGE_LOG"; }',
            'filename_is_claimed() { return 1; }',
            'filename_is_worker_held() { return 1; }',
            'mark_worker_stage() { printf "%s %s\\n" "$1" "$2" >> "$STAGE_LOG"; }',
            'process_announced_queue() { :; }',
            *fns, "watcher_pid=999999999", loop,
            "for f in \"$queue_dir\"/*; do [ -e \"$f\" ] || continue; n=\"$(basename \"$f\")\"; printf 'KEY %s\\n' \"$n\";"
            " printf 'PAYLOAD %s\\n' \"$(task_payload \"$n\")\"; done",
            *(['printf "PROMPT %s\\n" "$(task_prompt task-1.txt)"'] if runtime == "claude" else []),
        ])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", self.td.name),
               "TASKS_DIR": str(self.inbox), "WORKSPACE_DIR": str(self.ws), "RESULTS_DIR": str(self.ws / "results"),
               "REPO": str(repo), "queue_dir": str(tmp / "queue"), "PAYLOAD_DIR": str(tmp / "payload"),
               "INFLIGHT_DIR": str(tmp / "inflight"), "STAGE_LOG": str(tmp / "stages"),
               "event_dir": str(tmp), "RETRY_POLL_SEC": "1", "RETRY_INTERVAL": "1",
               "NOTIFIER_PY": sys.executable, "DISPATCH_PY": str(repo / "src" / "delivery" / "task_dispatch.py"),
               "SUTANDO_INBOX_RESOLVER": str(SCRIPTS / "resolve-inbox-entry"), "SUTANDO_INBOX_KIND": "deliveries"}
        r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        stages = (tmp / "stages").read_text() if (tmp / "stages").exists() else ""
        return r.stdout.splitlines(), stages

    def assert_keyed_and_read(self, lines, runtime, stages=""):
        self.assertIn("KEY task-1.txt", lines, stages)
        self.assertEqual([ln for ln in lines if ln.startswith("KEY ")], ["KEY task-1.txt"])
        payload = [ln[len("PAYLOAD "):] for ln in lines if ln.startswith("PAYLOAD ")]
        self.assertEqual(payload, [str(self.body)])
        self.assertEqual(Path(payload[0]).read_text(), TEXT, "the body is what the session reads")
        if runtime == "claude":
            self.assertIn(f"write the result to {self.ws / 'results'}/task-1.txt.",
                          next(ln for ln in lines if ln.startswith("PROMPT ")))

    def test_both_notifiers_key_a_routed_task_by_its_name_and_read_its_body(self):
        events, err = self.sweep()
        self.assertIn(f"TASK_FILE: {self.body}", events, err[-800:])
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                lines, stages = self.notify(runtime, events)
                self.assert_keyed_and_read(lines, runtime, stages)

    def test_both_notifiers_see_results_under_the_tasks_name_as_its_answer(self):
        events, err = self.sweep()
        (self.ws / "results" / "task-1.txt").write_text("answered\n")
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                lines, stages = self.notify(runtime, events)
                self.assertFalse([ln for ln in lines if ln.startswith("KEY ")], "answered: nothing queued")
                if runtime == "codex":
                    self.assertEqual(stages, "task-1.txt done\n")

    # Rolling upgrade: main's own watcher and notifiers beside this tree's, in both directions.
    def main_tree(self):
        tree = _main_tree(Path(self.td.name) / "main")
        if tree is None:
            why = "no main ref in this checkout to unpack main's watcher and notifiers from"
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                self.fail(why + " (CI must fetch it, so a skip cannot pass here)")
            self.skipTest(why)
        return tree

    def test_mains_watcher_with_this_resolver_announces_the_body_by_its_task_name(self):
        main = self.main_tree()
        for role in ("standby", "session"):
            with self.subTest(role=role):
                out, err = self.sweep(role, repo=main)
                self.assertEqual(out.splitlines(), [f"TASK_FILE: {self.body}"], err[-800:])
                for runtime in ("claude", "codex"):
                    lines, stages = self.notify(runtime, out)
                    self.assert_keyed_and_read(lines, runtime, stages)

    def test_mains_notifiers_refuse_this_watchers_body_and_never_type_the_sentinel(self):
        main = self.main_tree()
        events, err = self.sweep()
        self.assertIn(f"TASK_FILE: {self.body}", events, err[-800:])
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                lines, _ = self.notify(runtime, events, repo=main)
                self.assertFalse([ln for ln in lines if ln.startswith(("KEY ", "PAYLOAD "))], lines)
                lines, stages = self.notify(runtime, events)
                self.assert_keyed_and_read(lines, runtime, stages)


class MigrateAndRetireEdges(unittest.TestCase):
    """The migration and the locked retirement, in process, on the main-era shape:
    a sentinel whose payload is still in tasks/ and no body."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name).resolve() / "ws"
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True)
        self.core = self.ws / "tasks" / "task-1.txt"

    def tearDown(self):
        self.td.cleanup()

    def main_era(self, *recipients):
        self.core.write_text(TEXT)
        for r in recipients:
            (self.ws / "deliveries" / r).mkdir(exist_ok=True)
            (self.ws / "deliveries" / r / "task-1.txt").touch()

    def migrate(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rt.main(["migrate", "--workspace", str(self.ws)]), 0)
        return json.loads(out.getvalue())

    def test_migrate_moves_a_main_era_delivery_and_is_then_a_no_op(self):
        self.main_era(A, B)
        self.assertEqual(self.migrate(), {"kept": [], "migrated": ["task-1"]})
        for r in (A, B):
            self.assertEqual(pd.body_path(self.ws, r, "task-1").read_text(), TEXT)
        self.assertFalse(self.core.exists())
        self.assertEqual(self.migrate(), {"kept": [], "migrated": []})

    def test_migrate_leaves_a_task_the_core_also_holds(self):
        self.main_era(A, pr.CORE)
        self.assertEqual(self.migrate(), {"kept": [], "migrated": []})
        self.assertTrue(self.core.is_file())

    def test_migrate_keeps_a_task_whose_body_folder_is_an_alias(self):
        self.main_era(A)
        real = Path(tempfile.mkdtemp(dir=self.td.name))
        os.symlink(real, self.ws / "deliveries" / A / pd.BODY_DIR)
        self.assertEqual(self.migrate(), {"kept": ["task-1"], "migrated": []})
        self.assertTrue(self.core.is_file())
        self.assertEqual(list(real.iterdir()), [])

    def test_migrate_reads_nothing_through_an_aliased_recipient_folder(self):
        self.main_era(A)
        os.symlink(self.ws / "deliveries" / A, self.ws / "deliveries" / B)
        self.assertEqual(self.migrate(), {"kept": [], "migrated": []})
        self.assertTrue(self.core.is_file())

    def test_retirement_keeps_the_core_copy_unless_every_holder_has_its_body(self):
        self.main_era(A)
        rt._retire_core_copy(self.ws, "task-1", [A])               # no body folder at all
        (self.ws / "deliveries" / A / pd.BODY_DIR).mkdir()
        rt._retire_core_copy(self.ws, "task-1", [A])               # a folder, no body
        self.assertTrue(self.core.is_file())
        os.rmdir(self.ws / "deliveries" / A / pd.BODY_DIR)
        os.symlink(Path(tempfile.mkdtemp(dir=self.td.name)), self.ws / "deliveries" / A / pd.BODY_DIR)
        rt._retire_core_copy(self.ws, "task-1", [A])               # an aliased body folder
        self.assertTrue(self.core.is_file())

    def test_a_route_with_a_body_folder_but_no_payload_is_no_payload(self):
        (self.ws / "deliveries" / A / pd.BODY_DIR).mkdir(parents=True)
        out = rt.route(self.ws, {"id": "task-1", "source": "ag2space", "channel_id": ROOM}, roster({ROOM: A}))
        self.assertEqual(out["skipped"], [A])
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_a_withdrawal_with_a_non_regular_body_keeps_everything(self):
        self.core.write_text(TEXT)
        rt.route(self.ws, {"id": "task-1", "source": "ag2space", "channel_id": ROOM}, roster({ROOM: A}))
        pd.mark_done(self.ws, A, "task-1", published=False)
        body = pd.body_path(self.ws, A, "task-1")
        body.unlink()
        body.mkdir()
        with self.assertRaises(OSError):
            pd.clear_pending(self.ws, A, "task-1")
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file())
        self.assertTrue(pd.pending_flag(self.ws, A, "task-1").is_file())

    def test_a_comparison_that_cannot_read_is_never_a_match(self):
        a, b = Path(self.td.name) / "a", Path(self.td.name) / "b"
        a.write_text(TEXT)
        b.write_text(TEXT)
        with mock.patch.object(pd.os, "fdopen", side_effect=OSError(5, "Input/output error")):
            self.assertFalse(pd._same_regular_body(a, b))
        self.assertTrue(pd._same_regular_body(a, b))

    def test_the_remedy_pass_still_runs_when_migration_fails(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("pool_remedy_e", SCRIPTS / "pool_remedy.py")
        remedy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(remedy)
        broken = mock.Mock(migrate_main_era=mock.Mock(side_effect=RuntimeError("boom")))
        err = io.StringIO()
        with mock.patch.object(remedy, "_sibling", return_value=broken), \
                mock.patch.object(remedy, "_sweep", return_value=0) as sweep, contextlib.redirect_stderr(err):
            self.assertEqual(remedy.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"]), 0)
        sweep.assert_called_once()
        self.assertIn("migrate skipped: RuntimeError: boom", err.getvalue())


class LegacyInFlight(unittest.TestCase):
    """A task main's own router delivered (sentinel only, payload left in tasks/) gets its
    body on the next pass, and tasks/ stops listing it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name).resolve() / "ws"
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps(roster({ROOM: A})))
        self.text = TEXT.replace("task-1", "task-legacy")
        self.core = self.ws / "tasks" / "task-legacy.txt"
        self.core.write_text(self.text)
        self.main = _main_tree(Path(self.td.name) / "main")
        if self.main is None:
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                self.fail("no main ref to unpack main's router from (CI must fetch it)")
            self.skipTest("no main ref in this checkout to unpack main's router from")

    def tearDown(self):
        self.td.cleanup()

    def handler(self, repo):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", self.td.name)}
        r = subprocess.run([sys.executable, str(repo / "skills" / "worker-pool" / "scripts" / "pool_route_handler.py"),
                            "--task-file", str(self.core), "--workspace", str(self.ws)],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def core_lists(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("task_dispatch_t", REPO / "src" / "delivery" / "task_dispatch.py")
        tdm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tdm)
        listed = sorted(p.name for p in (self.ws / "tasks").glob("*.txt"))
        pending = [Path(str(c)).name for c in tdm.pending_candidates(self.ws / "tasks", self.ws / "results")]
        return listed, pending

    def test_the_remedy_timer_migrates_main_era_deliveries_before_anything_else_runs(self):
        folder = self.ws / "deliveries" / A
        self.handler(self.main)
        os.rename(folder / "task-legacy.txt", folder / "task-legacy.accepted")
        self.assertEqual(self.core_lists()[0], ["task-legacy.txt"], "main's route left it listed")
        import importlib.util
        spec = importlib.util.spec_from_file_location("pool_remedy_t", SCRIPTS / "pool_remedy.py")
        remedy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(remedy)
        seen = []
        with mock.patch.object(remedy, "_sweep", side_effect=lambda a: seen.append(self.core_lists()) or 0):
            self.assertEqual(remedy.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"]), 0)
        self.assertEqual(seen, [([], [])], "tasks/ stopped listing it before supervision ran")
        self.assertEqual(pd.body_path(self.ws, A, "task-legacy").read_text(), self.text)
        self.assertTrue((folder / "task-legacy.accepted").is_file(), "the delivery itself is unchanged")

    def test_a_worker_boot_migrates_before_it_prunes_a_finished_main_era_delivery(self):
        folder = self.ws / "deliveries" / A
        self.handler(self.main)
        os.rename(folder / "task-legacy.txt", folder / "task-legacy.accepted")
        (self.ws / "results" / "task-legacy.txt").write_text("answered\n")
        pd.mark_done(self.ws, A, "task-legacy", published=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location("worker_bootstrap_t", SCRIPTS / "worker_bootstrap.py")
        wb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wb)
        with contextlib.redirect_stderr(io.StringIO()):
            out = wb.prune_spent_sentinels(str(self.ws), A)
        self.assertIn("retired=1", out)
        self.assertEqual(self.core_lists(), ([], []), "no tasks/ copy is left behind with no holder")
        self.assertEqual(pd.archived_payload(self.ws, "task-legacy").read_text(), self.text)
        self.assertEqual([p.name for p in folder.glob("task-legacy.*")], [])

    def test_prune_spent_as_the_codex_notifier_calls_it_waits_for_the_migration(self):
        folder = self.ws / "deliveries" / A
        self.handler(self.main)
        os.rename(folder / "task-legacy.txt", folder / "task-legacy.accepted")
        (self.ws / "results" / "task-legacy.txt").write_text("answered\n")
        pd.mark_done(self.ws, A, "task-legacy", published=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", self.td.name)}

        def prune_spent():
            r = subprocess.run([sys.executable, str(SCRIPTS / "pool_delivery.py"), "--workspace", str(self.ws),
                                "--recipient", A, "prune-spent"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

        self.assertEqual(prune_spent(), {"retired": [], "stale": [], "kept": ["task-legacy"]})
        self.assertEqual(pd.sweep(self.ws, A)["kept"], ["task-legacy"], "the boot sweep waits too")
        self.assertTrue((folder / "task-legacy.accepted").is_file(), "sentinel kept for the migration")
        self.assertTrue(self.core.is_file(), "and so is its tasks/ copy")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rt.migrate_main_era(self.ws), {"migrated": ["task-legacy"], "kept": []})
        self.assertEqual(prune_spent(), {"retired": ["task-legacy"], "stale": [], "kept": []})
        self.assertEqual(pd.archived_payload(self.ws, "task-legacy").read_text(), self.text)
        self.assertEqual(self.core_lists(), ([], []))
        self.assertEqual([q.name for q in folder.glob("task-legacy.*")], [])

    def test_the_migrate_command_is_idempotent_and_keeps_what_it_cannot_read(self):
        self.handler(self.main)
        cmd = [sys.executable, str(SCRIPTS / "pool_router.py"), "migrate", "--workspace", str(self.ws)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", self.td.name)}
        first = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((first.returncode, json.loads(first.stdout)), (0, {"kept": [], "migrated": ["task-legacy"]}),
                         first.stderr)
        self.assertEqual(self.core_lists(), ([], []))
        again = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(again.stdout), {"kept": [], "migrated": []})
        # A second main-era delivery whose body folder is a symlink is left exactly as it was.
        other = self.ws / "tasks" / "task-legacy2.txt"
        other.write_text(self.text.replace("task-legacy", "task-legacy2"))
        (self.ws / "deliveries" / A / "task-legacy2.txt").touch()
        real = Path(tempfile.mkdtemp(dir=self.td.name))
        os.rename(self.ws / "deliveries" / A / pd.BODY_DIR, real / "b")
        os.symlink(real / "b", self.ws / "deliveries" / A / pd.BODY_DIR)
        third = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(third.stdout), {"kept": ["task-legacy2"], "migrated": []}, third.stderr)
        self.assertTrue(other.is_file(), "the core keeps its copy")

    def test_a_main_era_delivery_gets_its_body_and_leaves_tasks_under_every_sentinel_name(self):
        folder = self.ws / "deliveries" / A
        for suffix in (".txt", ".accepted", ".claimed"):
            with self.subTest(sentinel=suffix):
                self.core.write_text(self.text)
                for p in folder.glob("task-legacy.*"):
                    p.unlink()
                with contextlib.suppress(FileNotFoundError):
                    pd.body_path(self.ws, A, "task-legacy").unlink()
                self.handler(self.main)
                self.assertTrue((folder / "task-legacy.txt").is_file(), "main's router delivered it")
                self.assertTrue(self.core.is_file(), "and left its payload in tasks/")
                self.assertFalse(pd.body_path(self.ws, A, "task-legacy").exists())
                if suffix != ".txt":
                    os.rename(folder / "task-legacy.txt", folder / ("task-legacy" + suffix))
                self.handler(REPO)
                self.assertEqual(pd.body_path(self.ws, A, "task-legacy").read_text(), self.text)
                self.assertTrue((folder / ("task-legacy" + suffix)).is_file(), "the delivery itself is unchanged")
                self.assertEqual(self.core_lists(), ([], []), "the core no longer lists the task")
                self.assertEqual(rie.resolve(str(folder / "task-legacy.txt")),
                                 Path(os.path.abspath(pd.body_path(self.ws, A, "task-legacy"))))


if __name__ == "__main__":
    unittest.main(verbosity=1)
