#!/usr/bin/env python3
"""The Activity title/outcome side file: its writer, the hatch answer path that writes it, and the
bridge's guard, archive and orphan sweep that read and retire it. In-process, so coverage sees it.
Run: python3 tests/sparrow-result-labels.test.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import remote_gateway_bridge as gw, result_labels as rl  # noqa: E402
from ag2_sparrow.adapters.hatch import cli  # noqa: E402

CODER = '\U0001f469‍\U0001f4bb'


class Dir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.result = self.root / 'task-1.txt'


class Writer(Dir):
    def test_side_path_sits_beside_the_result(self):
        self.assertEqual(rl.side_path(self.result), self.root / 'task-1.meta.json')
        self.assertEqual(rl.side_path(str(self.result)), self.root / 'task-1.meta.json')

    def test_clean_makes_one_line_and_keeps_joiners(self):
        self.assertEqual(rl.clean(None), '')
        self.assertEqual(rl.clean(42), '')
        self.assertEqual(rl.clean(' a\x07\nb\tc d‮e​f '), 'a b c d e f')
        self.assertEqual(rl.clean(f'x {CODER} र्‌क'), f'x {CODER} र्‌क')

    def test_fit_cuts_on_a_grapheme_boundary(self):
        self.assertEqual(rl.fit('short', 10), 'short')
        self.assertEqual(rl.fit('abcdefghijk', 5), 'abcd…')
        self.assertEqual(rl.fit('abc' + CODER + 'zzzz', 5), 'abc…')
        self.assertEqual(rl.fit('abc' + 'é́' + 'zz', 5), 'abc…')
        self.assertEqual(rl.fit('abc   defgh', 5), 'abc…')

    def test_check_fits_and_notes_each_cut(self):
        labels, notes = rl.check('t' * 61, 'o' * 151)
        self.assertEqual((len(labels['title']), len(labels['outcome'])), (60, 150))
        self.assertEqual(notes, ['--title was over 60 characters and was cut to fit',
                                 '--outcome was over 150 characters and was cut to fit'])
        self.assertEqual(rl.check('Fix it', None), ({'title': 'Fix it'}, []))
        self.assertEqual(rl.check('', '  '), ({}, []))

    def test_write_publishes_atomically_and_privately(self):
        side = rl.write(self.result, {'title': 'Fix\nit', 'outcome': 'Fixed.', 'other': 'x'})
        self.assertEqual(side, self.root / 'task-1.meta.json')
        self.assertEqual(json.loads(side.read_text()), {'title': 'Fix it', 'outcome': 'Fixed.'})
        self.assertEqual(side.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.root.iterdir()], ['task-1.meta.json'])

    def test_write_with_no_labels_removes_a_stale_side_file(self):
        rl.write(self.result, {'title': 'Old'})
        self.assertIsNone(rl.write(self.result, {'title': ' ', 'outcome': None}))
        self.assertFalse(rl.side_path(self.result).exists())
        self.assertIsNone(rl.write(self.result, {}))

    def test_a_failed_write_leaves_no_temp_file(self):
        with patch.object(rl.os, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                rl.write(self.result, {'title': 'T'})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_returns_only_what_this_writer_could_make(self):
        side = rl.side_path(self.result)
        self.assertEqual(rl.read(side), {})
        for raw in (b'x' * (rl._READ_MAX + 1), b'\xff\xfe', b'{not json', b'["a"]'):
            side.write_bytes(raw)
            self.assertEqual(rl.read(side), {}, raw[:10])
        side.write_text(json.dumps({'title': 't' * 80 + '\n', 'outcome': '', 'x': 'y'}))
        self.assertEqual(rl.read(side), {'title': 't' * 59 + '…'})

    def test_archive_moves_under_a_unique_name(self):
        archive = self.root / 'archive'
        rl.archive(self.root, 'task-1', archive)
        self.assertFalse(archive.exists() and any(archive.iterdir()))
        for n in range(2):
            rl.write(self.result, {'title': f'T{n}'})
            with patch.object(rl.time, 'time_ns', return_value=7):
                rl.archive(self.root, 'task-1', archive)
        self.assertEqual(sorted(p.name for p in archive.iterdir()),
                         ['task-1-7.meta.json', 'task-1-8.meta.json'])
        self.assertFalse(rl.side_path(self.result).exists())


class HatchLabels(Dir):
    def args(self, **kw):
        return types.SimpleNamespace(**{'title': None, 'outcome': None, 'reason': None, **kw})

    def test_a_no_reply_outcome_defaults_to_its_reason(self):
        self.assertEqual(cli._answer_labels(self.args(reason='an ack'), silent=True), {'outcome': 'an ack'})
        self.assertEqual(cli._answer_labels(self.args(reason='an ack', outcome='Acked.'), silent=True),
                         {'outcome': 'Acked.'})
        self.assertEqual(cli._answer_labels(self.args(reason='an ack'), silent=False), {})

    def test_an_over_long_label_is_cut_with_a_warning(self):
        err = []
        with patch.object(cli.sys, 'stderr', types.SimpleNamespace(write=err.append, flush=lambda: None)):
            labels = cli._answer_labels(self.args(title='t' * 70), silent=False)
        self.assertEqual(len(labels['title']), 60)
        self.assertIn('warning: --title was over 60 characters and was cut to fit', ''.join(err))

    def test_the_cli_takes_the_flags_and_reads_labels_before_committing(self):
        seen = []
        real = cli._answer_labels
        with patch.object(cli, '_installed_config', return_value={}), \
                patch.object(cli, '_wake_item', return_value=(self.root, self.root, 'task-1', 'task-1', '')), \
                patch.object(cli, '_answer_labels', lambda a, s: seen.append(real(a, s)) or seen[-1]), \
                patch.object(cli, '_delivered_item', return_value=None):
            self.assertEqual(cli.main(['reply', str(self.result), '--body', '4', '--title', 'Add',
                                       '--outcome', 'Added.']), 2)
            self.assertEqual(cli.main(['no-reply', str(self.result), '--reason', 'ack',
                                       '--title', 'Ack']), 2)
        self.assertEqual(seen, [{'title': 'Add', 'outcome': 'Added.'}, {'title': 'Ack', 'outcome': 'ack'}])
        self.assertFalse(rl.side_path(self.result).exists())

    def commit(self, labels, publish=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(cli, '_prune_claims'))
        stack.enter_context(patch.object(cli, '_claim', return_value=True))
        stack.enter_context(patch.object(cli.outbox, 'item_status', return_value=None))
        stack.enter_context(patch.object(cli, '_line_report', return_value='none'))
        if publish is not None:
            stack.enter_context(patch.object(cli, '_publish_once', publish))
        with stack:
            return cli._commit_answer_locked(self.root, self.root, 'task-1', self.root, 'task-1',
                                             self.result, '4\n', labels)

    def test_the_side_file_is_written_before_the_result(self):
        seen = []

        def publish(result_file, body):
            seen.append(rl.read(rl.side_path(result_file)))
            result_file.write_text(body)
            return True
        self.assertEqual(self.commit({'title': 'Add'}, publish), (True, 'none'))
        self.assertEqual(seen, [{'title': 'Add'}])

    def test_an_answered_name_writes_no_side_file(self):
        self.result.write_text('first\n')
        self.assertEqual(self.commit({'title': 'Add'}), (False, 'none'))
        self.assertFalse(rl.side_path(self.result).exists())

    def test_an_unpublished_answer_takes_its_side_file_back(self):
        self.assertEqual(self.commit({'title': 'Add'}, lambda *_: False), (False, 'none'))
        self.assertFalse(rl.side_path(self.result).exists())

        def boom(*_):
            raise OSError('disk')
        with self.assertRaises(OSError):
            self.commit({'title': 'Add'}, boom)
        self.assertFalse(rl.side_path(self.result).exists())

    def test_an_answer_without_labels_removes_a_stale_side_file(self):
        rl.write(self.result, {'title': 'Stale'})
        self.assertEqual(self.commit({}, lambda f, b: bool(f.write_text(b))), (True, 'none'))
        self.assertFalse(rl.side_path(self.result).exists())


class BridgeLabels(Dir):
    def setUp(self):
        super().setUp()
        self.tasks, self.log = self.root / 'tasks', []
        self.tasks.mkdir()
        stack = contextlib.ExitStack()
        for name, value in dict(RESULTS_DIR=self.root, ARCHIVE_RESULTS_DIR=self.root / 'archive',
                                TASKS_DIR=self.tasks, _log=self.log.append).items():
            stack.enter_context(patch.object(gw, name, value))
        self.addCleanup(stack.close)

    def task(self, tier):
        (self.tasks / 'task-1.txt').write_text(f'id: task-1\nsource: ag2space\naccess_tier: {tier}\ntask: x\n')

    def test_no_side_file_adds_nothing(self):
        self.assertEqual(gw._guarded_result_labels('task-1'), {})

    def test_owner_labels_pass_and_non_owner_leaks_are_left_out(self):
        rl.write(self.result, {'title': 'Add two', 'outcome': 'Sent ghp_' + 'a' * 36})
        self.task('owner')
        self.assertEqual(gw._guarded_result_labels('task-1'),
                         {'title': 'Add two', 'outcome': 'Sent ghp_' + 'a' * 36})
        self.task('team')
        self.assertEqual(gw._guarded_result_labels('task-1'), {'title': 'Add two'})
        self.assertTrue(any('its outcome is left out' in line for line in self.log))

    def test_with_no_guard_the_labels_are_left_out(self):
        rl.write(self.result, {'title': 'Add two'})
        with patch.object(gw, '_team_guard_fns', side_effect=ImportError('gone')):
            self.assertEqual(gw._guarded_result_labels('task-1'), {})
        self.assertTrue(any('result guard is unavailable' in line for line in self.log))

    def test_an_archive_failure_is_logged_for_the_next_sweep(self):
        with patch.object(gw.result_labels, 'archive', side_effect=OSError('ro')):
            gw._archive_result_labels('task-1')
        self.assertTrue(any('could not be archived' in line for line in self.log))

    def aged(self, name):
        side = self.root / name
        side.write_text('{"title": "T"}')
        stale = time.time() - gw.RESULT_LABELS_ORPHAN_S - 1
        os.utime(side, (stale, stale))
        return side

    def test_the_sweep_archives_only_old_orphans(self):
        orphan, live, sending, pinned, young = (self.aged(f'task-{n}.meta.json')
                                                for n in ('o', 'l', 's', 'p', 'y'))
        os.utime(young, None)
        (self.root / 'task-l.txt').write_text('x')
        (self.root / 'task-s.sending.1').write_text('x')
        pin = gw.disposal.pin_path(self.root, 'task-p')
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text('x')
        gw._sweep_orphan_result_labels(time.time())
        self.assertEqual([p.exists() for p in (orphan, live, sending, pinned, young)],
                         [False, True, True, True, True])
        self.assertEqual(len(list((self.root / 'archive').glob('task-o-*.meta.json'))), 1)

    def test_the_sweep_survives_an_unreadable_directory_or_file(self):
        self.aged('task-o.meta.json')
        with patch.object(type(self.root), 'glob', side_effect=OSError('gone')):
            gw._sweep_orphan_result_labels(time.time())
        with patch.object(type(self.root), 'lstat', side_effect=OSError('gone')):
            gw._sweep_orphan_result_labels(time.time())
        self.assertTrue((self.root / 'task-o.meta.json').exists())


if __name__ == '__main__':
    unittest.main()
