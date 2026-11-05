#!/usr/bin/env python3
"""`result_publish.py --title/--outcome`: the desktop core's final result carries its Activity labels
in the side file the bridge reads, written before the result; a result with no labels is unchanged.
Run: python3 tests/result-publish-labels.test.py
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import multiprocessing
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
rl = importlib.import_module('result_labels')
rp = importlib.import_module('result_publish')
gw = importlib.import_module('ag2_sparrow.remote_gateway_bridge')


class Publish(unittest.TestCase):
    rp, rl = rp, rl

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.result = self.root / 'task-1.txt'
        self.side = self.rl.side_path(self.result)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(self.rp, 'undeliverable_reason', return_value=None))
        self.addCleanup(stack.close)

    def run_cli(self, body, *flags):
        err = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(body)), contextlib.redirect_stderr(err):
            code = self.rp.main(['result_publish.py', *flags, str(self.result)])
        return code, err.getvalue()

    def side_doc(self, body):
        """The side file's labels, after checking they are bound to `body`."""
        doc = json.loads(self.side.read_text(encoding='utf-8'))
        self.assertEqual(doc.pop(self.rl.DIGEST_KEY), self.rl.digest(body))
        return doc

    def test_the_flags_publish_the_labels_before_the_result(self):
        seen = []
        real = self.rp.publish_text

        def publish(path, text, *a):
            seen.append(self.side_doc(text))
            return real(path, text, *a)
        with patch.object(self.rp, 'publish_text', publish):
            code, err = self.run_cli('Done.', '--title', 'Add the two numbers',
                                     '--outcome', 'Replied with 4')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(seen, [{'title': 'Add the two numbers', 'outcome': 'Replied with 4'}])
        self.assertEqual(self.result.read_text(), 'Done.')

    def test_no_flags_publish_as_before(self):
        self.assertEqual(self.run_cli('Done.'), (0, ''))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['task-1.txt'])
        self.assertEqual(self.result.read_text(), 'Done.')

    def test_a_result_without_labels_never_takes_an_earlier_attempts(self):
        self.rl.write(self.result, {'title': 'An earlier attempt'}, 'Earlier.')
        self.assertEqual(self.run_cli('Done.'), (0, ''))
        self.assertEqual(self.rl.read(self.side, self.rl.digest('Done.')), {})

    def test_labelled_publishes_make_a_missing_results_directory(self):
        for n, flags in enumerate((['--title', 'Add two'], ['--outcome', 'Added'],
                                   ['--title', 'Add two', '--outcome', 'Added'])):
            with self.subTest(flags=flags):
                self.result = self.root / f'new-{n}' / 'results' / 'task-1.txt'
                self.side = self.rl.side_path(self.result)
                self.assertEqual(self.run_cli('Done.', *flags), (0, ''))
                self.assertEqual(self.result.read_text(), 'Done.')
                self.assertTrue(self.side_doc('Done.'))

    def test_a_no_send_reason_is_the_outcome_unless_one_is_given(self):
        body = '[no-send] already answered\nin the thread'
        self.run_cli(body, '--title', 'Answer the question')
        self.assertEqual(self.side_doc(body), {'title': 'Answer the question',
                                               'outcome': 'already answered in the thread'})
        self.run_cli('[no-send] already answered', '--outcome', 'Left it unanswered')
        self.assertEqual(self.side_doc('[no-send] already answered'), {'outcome': 'Left it unanswered'})
        self.side.unlink()
        self.run_cli('[no-send]')
        self.assertFalse(self.side.exists())

    def test_only_a_leading_no_send_gives_a_reason(self):
        self.assertEqual(self.rp.result_labels_for('See `[no-send]` in the docs.'), ({}, []))
        self.assertEqual(self.rp.result_labels_for('[no-send] ' + 'x' * 200)[0]['outcome'],
                         'x' * (self.rl.OUTCOME_MAX - 1) + '…')
        with patch.object(self.rp, '_helpers', side_effect=ImportError('gone')):
            self.assertEqual(self.rp.result_labels_for('[no-send] quiet'), ({}, []))

    def test_an_over_long_label_is_cut_with_a_warning(self):
        code, err = self.run_cli('Done.', '--title', 'Fix ' + 'x' * 80, '--outcome', 'Fixed\x07 it')
        self.assertEqual(code, 0)
        self.assertIn('warning: --title was over 60 characters and was cut to fit', err)
        doc = self.side_doc('Done.')
        self.assertEqual((len(doc['title']), doc['outcome']), (self.rl.TITLE_MAX, 'Fixed it'))

    def test_a_failed_publish_leaves_labels_no_other_body_can_take(self):
        with patch.object(self.rp, 'publish_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_cli('Failed.', '--title', 'Wrong')
        self.assertFalse(self.result.exists())
        self.run_cli('Done.')
        self.assertEqual(self.rl.read(self.side, self.rl.digest('Done.')), {})

    def test_a_refused_result_writes_no_side_file(self):
        with patch.object(self.rp, 'undeliverable_reason', return_value='task-1 is a cron task'):
            code, err = self.run_cli('Done.', '--title', 'Add two')
        self.assertEqual(code, 2)
        self.assertIn('refused', err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_malformed_command_line_is_refused_before_anything_is_written(self):
        for flags in (['--title'], ['--title', 'a', '--title', 'b'], ['--bogus'], [str(self.root / 'x.txt')]):
            with self.subTest(flags=flags):
                self.assertEqual(self.run_cli('Done.', *flags)[0], 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_without_the_label_writer_the_result_still_publishes(self):
        with patch.object(self.rp, '_labels_module', side_effect=ImportError('gone')):
            self.assertEqual(self.run_cli('Done.'), (0, ''))
            code, err = self.run_cli('Again.', '--title', 'Add two')
        self.assertEqual(code, 0)
        self.assertIn('published without --title/--outcome', err)
        self.assertEqual(self.result.read_text(), 'Again.')
        self.assertFalse(self.side.exists())

    def test_the_label_writer_is_found_inside_or_outside_a_package(self):
        inside = f'{self.rp.__package__}.result_labels' if self.rp.__package__ else 'no.such.module'
        with patch.dict(sys.modules, {inside: None}):
            self.assertTrue(hasattr(self.rp._labels_module(), 'write'))

    def test_allow_undeliverable_still_takes_labels(self):
        code, _ = self.run_cli('Done.', '--allow-undeliverable', '--outcome', 'Logged it')
        self.assertEqual((code, self.side_doc('Done.')), (0, {'outcome': 'Logged it'}))


class PublishVendored(Publish):
    """The same CLI as ag2-sparrow vendors it."""
    rp = importlib.import_module('ag2_sparrow.result_publish')
    rl = importlib.import_module('ag2_sparrow.result_labels')



def _racer(result, n, fail, start):
    """One publisher in its own process: body n with title n, or one whose publish fails."""
    start.wait()
    sys.stdin = io.StringIO(f'body {n}')
    with contextlib.redirect_stderr(io.StringIO()):
        if fail:
            with patch.object(rp, 'publish_text', side_effect=OSError('disk full')):
                with contextlib.suppress(OSError):
                    rp.main(['result_publish.py', '--title', f'title {n}', result])
        else:
            rp.main(['result_publish.py', '--title', f'title {n}', '--outcome', f'outcome {n}', result])


class Concurrent(unittest.TestCase):
    """Overlapping publishers of one name, some failing: the body that ends up published has its
    own labels or none, never another publisher's, and no publisher removes another's."""

    def test_overlapping_publishers_never_pair_a_body_with_another_title(self):
        ctx = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(rp, 'undeliverable_reason', return_value=None):
            for round_ in range(30):
                result = Path(tmp) / f'task-{round_}.txt'
                start = ctx.Event()
                procs = [ctx.Process(target=_racer, args=(str(result), n, n % 3 == 2, start))
                         for n in range(6)]
                for p in procs:
                    p.start()
                start.set()
                for p in procs:
                    p.join(30)
                    self.assertEqual(p.exitcode, 0)
                body = result.read_text()
                n = int(body.split()[-1])
                self.assertNotEqual(n % 3, 2, 'a failed publish published its body')
                labels = rl.read(rl.side_path(result), rl.digest(body))
                self.assertIn(labels, ({}, {'title': f'title {n}', 'outcome': f'outcome {n}'}))
                self.assertTrue(rl.side_path(result).exists(), 'a publisher removed the side file')
                # Once the race is over, a publisher's labels are its own again.
                done = ctx.Event()
                done.set()
                last = ctx.Process(target=_racer, args=(str(result), 9, False, done))
                last.start()
                last.join(30)
                self.assertEqual(rl.read(rl.side_path(result), rl.digest('body 9')),
                                 {'title': 'title 9', 'outcome': 'outcome 9'})


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

    def generation(self):
        return gw._read_ready_generation(self.root / 'task-1.txt')[1]

    def test_the_bridge_reads_the_labels_the_cli_wrote(self):
        self.publish('--title', 'Add two', '--outcome', 'Replied with 4')
        self.assertEqual(gw._guarded_result_labels('task-1', self.generation()),
                         {'title': 'Add two', 'outcome': 'Replied with 4'})

    def test_with_no_labels_the_bridge_adds_nothing(self):
        self.publish()
        self.assertEqual(gw._guarded_result_labels('task-1', self.generation()), {})

    def test_no_result_glob_matches_the_side_file(self):
        side = rl.side_path(self.root / 'task-1.txt')
        for pattern in ('task-*.txt', '*.txt', 'proactive-*'):
            self.assertFalse(side.match(pattern), pattern)


if __name__ == '__main__':
    unittest.main()
