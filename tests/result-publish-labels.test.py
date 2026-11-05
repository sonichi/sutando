#!/usr/bin/env python3
"""`result_publish.py --title/--outcome`: the desktop core's final result carries its Activity labels
in the side file the bridge reads, written before the result; a result with no labels is unchanged.
Run: python3 tests/result-publish-labels.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(REPO / 'packages' / 'ag2-sparrow'))
import result_labels as rl  # noqa: E402
import result_publish as rp  # noqa: E402
from ag2_sparrow import remote_gateway_bridge as gw  # noqa: E402


class Publish(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.result = self.root / 'task-1.txt'
        self.side = rl.side_path(self.result)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(rp, 'undeliverable_reason', return_value=None))
        self.addCleanup(stack.close)

    def run_cli(self, body, *flags):
        err = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(body)), contextlib.redirect_stderr(err):
            code = rp.main(['result_publish.py', *flags, str(self.result)])
        return code, err.getvalue()

    def side_doc(self):
        return json.loads(self.side.read_text(encoding='utf-8'))

    def test_the_flags_publish_the_labels_before_the_result(self):
        seen = []
        real = rp.publish_text

        def publish(path, text, *a):
            seen.append(self.side_doc())
            return real(path, text, *a)
        with patch.object(rp, 'publish_text', publish):
            code, err = self.run_cli('Done.', '--title', 'Add the two numbers',
                                     '--outcome', 'Replied with 4')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(seen, [{'title': 'Add the two numbers', 'outcome': 'Replied with 4'}])
        self.assertEqual(self.result.read_text(), 'Done.')

    def test_no_flags_publish_as_before_and_remove_a_stale_side_file(self):
        rl.write(self.result, {'title': 'An earlier attempt'})
        self.assertEqual(self.run_cli('Done.'), (0, ''))
        self.assertEqual(self.result.read_text(), 'Done.')
        self.assertFalse(self.side.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['task-1.txt'])

    def test_a_no_send_reason_is_the_outcome_unless_one_is_given(self):
        self.run_cli('[no-send] already answered\nin the thread', '--title', 'Answer the question')
        self.assertEqual(self.side_doc(), {'title': 'Answer the question',
                                           'outcome': 'already answered in the thread'})
        self.run_cli('[no-send] already answered', '--outcome', 'Left it unanswered')
        self.assertEqual(self.side_doc(), {'outcome': 'Left it unanswered'})
        self.run_cli('[no-send]')
        self.assertFalse(self.side.exists())

    def test_only_a_leading_no_send_gives_a_reason(self):
        self.assertEqual(rp.result_labels_for('See `[no-send]` in the docs.'), ({}, []))
        self.assertEqual(rp.result_labels_for('[no-send] ' + 'x' * 200)[0]['outcome'],
                         'x' * (rl.OUTCOME_MAX - 1) + '…')
        with patch.object(rp, '_helpers', side_effect=ImportError('gone')):
            self.assertEqual(rp.result_labels_for('[no-send] quiet'), ({}, []))

    def test_an_over_long_label_is_cut_with_a_warning(self):
        code, err = self.run_cli('Done.', '--title', 'Fix ' + 'x' * 80, '--outcome', 'Fixed\x07 it')
        self.assertEqual(code, 0)
        self.assertIn('warning: --title was over 60 characters and was cut to fit', err)
        doc = self.side_doc()
        self.assertEqual((len(doc['title']), doc['outcome']), (rl.TITLE_MAX, 'Fixed it'))

    def test_a_failed_publish_takes_its_side_file_back(self):
        with patch.object(rp, 'publish_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_cli('Done.', '--title', 'Add two')
        self.assertFalse(self.side.exists())
        self.assertFalse(self.result.exists())

    def test_a_refused_result_writes_no_side_file(self):
        with patch.object(rp, 'undeliverable_reason', return_value='task-1 is a cron task'):
            code, err = self.run_cli('Done.', '--title', 'Add two')
        self.assertEqual(code, 2)
        self.assertIn('refused', err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_malformed_command_line_is_refused_before_anything_is_written(self):
        for flags in (['--title'], ['--title', 'a', '--title', 'b'], ['--bogus'], [str(self.root / 'x.txt')]):
            with self.subTest(flags=flags):
                self.assertEqual(self.run_cli('Done.', *flags)[0], 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_allow_undeliverable_still_takes_labels(self):
        code, _ = self.run_cli('Done.', '--allow-undeliverable', '--outcome', 'Logged it')
        self.assertEqual((code, self.side_doc()), (0, {'outcome': 'Logged it'}))


class Bridge(unittest.TestCase):
    """The bridge sees what the CLI wrote; an older bridge never matches the side file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        tasks = self.root / 'tasks'
        tasks.mkdir()
        (tasks / 'task-1.txt').write_text('id: task-1\nsource: ag2space\naccess_tier: owner\ntask: x\n')
        stack = contextlib.ExitStack()
        for name, value in dict(RESULTS_DIR=self.root, TASKS_DIR=tasks, _log=lambda *_: None).items():
            stack.enter_context(patch.object(gw, name, value))
        stack.enter_context(patch.object(rp, 'undeliverable_reason', return_value=None))
        self.addCleanup(stack.close)

    def publish(self, *flags):
        with patch.object(sys, 'stdin', io.StringIO('Done.')):
            return rp.main(['result_publish.py', *flags, str(self.root / 'task-1.txt')])

    def test_the_bridge_reads_the_labels_the_cli_wrote(self):
        self.publish('--title', 'Add two', '--outcome', 'Replied with 4')
        self.assertEqual(gw._guarded_result_labels('task-1'),
                         {'title': 'Add two', 'outcome': 'Replied with 4'})

    def test_with_no_labels_the_bridge_adds_nothing(self):
        self.publish()
        self.assertEqual(gw._guarded_result_labels('task-1'), {})

    def test_no_result_glob_matches_the_side_file(self):
        side = rl.side_path(self.root / 'task-1.txt')
        for pattern in ('task-*.txt', '*.txt', 'proactive-*'):
            self.assertFalse(side.match(pattern), pattern)


if __name__ == '__main__':
    unittest.main()
