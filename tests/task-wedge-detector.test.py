"""Task-scoped wedge evidence, recovery and publication contract."""
import importlib
import json
import multiprocessing
import os
import types
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))
from _helpers.hermetic_env import isolate_os_environ
isolate_os_environ()
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
import wedge_detector as detector
from ag2_sparrow.task_step_publisher import StepPublisher


def concurrent_status(workspace, gate, answers):
    gate.wait(10)
    try:
        status = detector.status_for(workspace, 'task-a',
                                     {'phase': 'Running tests', 'summary': 'Running tests', 'since': 100.0},
                                     now=180, signals=[{'kind': 'provider-limit', 'task_id': 'task-a'}])
        answers.put(('ok', status['phase']))
    except Exception as exc:
        answers.put(('error', repr(exc)))


class Wedges(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ws = Path(directory.name)
        (self.ws / 'state' / 'activity').mkdir(parents=True)
        (self.ws / 'results').mkdir()
        self.normal = {'phase': 'Running tests', 'summary': 'Running tests', 'since': 100.0}
        self.activity('task-a', 100.0)

    def activity(self, task, stamp, worker=None):
        path = self.ws / 'state' / 'activity' / (task + '.json')
        path.write_text(json.dumps({'task_id': task, 'last_activity_at': stamp, 'worker': worker}))

    def check(self, now, signals=(), task='task-a', status=None):
        return detector.status_for(self.ws, task, self.normal if status is None else status,
                                   now=now, signals=list(signals))

    def signal(self, kind='provider-limit', task='task-a'):
        value = {'kind': kind, 'raw': 'private-provider-token-and-prompt'}
        if task is not None:
            value['task_id'] = task
        return value

    def blocked(self, status):
        self.assertEqual(status['phase'], 'Waiting')
        self.assertEqual(status['waiting']['reason'], 'blocked')
        self.assertTrue(status['summary'].strip())
        self.assertNotIn('private-provider-token-and-prompt', json.dumps(status))
        self.assertNotEqual(status['summary'], 'Waiting')

    def notices(self):
        return list((self.ws / 'results').glob('proactive*.txt'))

    def test_fresh_activity_overrides_stale_proactive_status(self):
        signal = dict(self.signal('core-status-stale', task=None), since=90)
        self.assertEqual(self.check(100, [signal]), self.normal)
        self.assertEqual(self.check(120, [signal]), self.normal)

    def test_accepted_snapshot_does_not_revert_to_queue_after_pickup_expires(self):
        path = self.ws / 'state' / 'activity' / 'task-a.json'
        path.write_text(json.dumps({'task_id': 'task-a', 'phase': 'RUNNING', 'last_activity_at': 100}))
        status = dict(self.normal, phase='Waiting', waiting={'reason': 'queued'})
        self.blocked(self.check(3800, status=status))

    def test_each_signature_debounces_and_keeps_stable_overlay(self):
        for kind in ('provider-limit', 'pane-prompt-blocked', 'watcher-not-ready', 'core-status-stale'):
            with self.subTest(kind=kind):
                signal = self.signal(kind)
                self.assertEqual(self.check(100, [signal]), self.normal)
                self.assertEqual(self.check(119, [signal]), self.normal)
                waiting = self.check(120, [signal])
                self.blocked(waiting)
                self.assertEqual(self.check(130, [signal]), waiting)
                self.assertEqual(self.check(131), self.normal)
        self.assertEqual(self.notices(), [])

    def test_transient_signature_does_not_accumulate_disconnected_observations(self):
        signal = self.signal()
        self.check(100, [signal])
        self.assertEqual(self.check(119), self.normal)
        self.assertEqual(self.check(120, [signal]), self.normal)
        self.assertEqual(self.check(139, [signal]), self.normal)
        self.blocked(self.check(140, [signal]))
        self.assertEqual(self.notices(), [])

    def test_silence_boundary_and_new_activity_recovery(self):
        self.assertEqual(self.check(279), self.normal)
        waiting = self.check(280)
        self.blocked(waiting)
        self.assertEqual(self.check(290), waiting)
        self.activity('task-a', 291)
        self.assertEqual(self.check(291), self.normal)
        self.assertEqual(self.check(470), self.normal)
        self.blocked(self.check(471))

    def test_long_running_work_with_fresh_activity_never_fires(self):
        for stamp in range(100, 1300, 60):
            self.activity('task-a', stamp)
            self.assertEqual(self.check(stamp + 59), self.normal)
        self.assertEqual(self.notices(), [])

    def test_newest_task_event_counts_as_activity_not_other_tasks(self):
        log = self.ws / 'state' / 'agent-activity.jsonl'
        log.write_text('\n'.join(json.dumps(row) for row in (
            {'ts': 275, 'kind': 'working', 'task': {'id': 'task-a'}},
            {'ts': 120, 'kind': 'working', 'task': {'id': 'task-a'}},
            {'ts': 490, 'kind': 'working', 'task': {'id': 'task-other'}},
        )) + '\n')
        self.assertEqual(self.check(300), self.normal)
        self.blocked(self.check(455))

    def test_waiting_delegated_and_queue_are_not_silence(self):
        for phase, reason in (('Waiting', 'permission'), ('Waiting', 'queued'),
                              ('Waiting', 'busy'), ('Delegated', 'delegated')):
            status = dict(self.normal, phase=phase, waiting={'reason': reason})
            self.assertEqual(self.check(1000, status=status), status)
        self.assertEqual(self.notices(), [])

    def test_task_scoped_signal_cannot_wedge_another_task(self):
        self.activity('task-b', 100, worker='worker-b')
        signal = self.signal()
        self.check(100, [signal])
        self.blocked(self.check(120, [signal]))
        self.assertEqual(self.check(100, [signal], task='task-b'), self.normal)
        self.assertEqual(self.check(120, [signal], task='task-b'), self.normal)

    def test_global_core_failure_does_not_mark_known_worker(self):
        self.activity('task-worker', 100, worker='worker-b')
        signal = self.signal('watcher-not-ready', task=None)
        self.check(100, [signal])
        self.blocked(self.check(120, [signal]))
        self.assertEqual(self.check(100, [signal], task='task-worker'), self.normal)
        self.assertEqual(self.check(120, [signal], task='task-worker'), self.normal)

    def test_terminal_status_clears_pending_incident(self):
        signal = self.signal()
        self.check(100, [signal])
        self.blocked(self.check(120, [signal]))
        self.assertIsNone(detector.status_for(self.ws, 'task-a', None, now=180, signals=[signal]))
        self.assertEqual(self.notices(), [])
        self.activity('task-a', 181)
        self.assertEqual(self.check(181, [signal]), self.normal)
        self.blocked(self.check(201, [signal]))

    def test_finished_result_or_terminal_activity_does_not_notify(self):
        signal = self.signal()
        self.check(100, [signal])
        self.check(120, [signal])
        result = self.ws / 'results' / 'task-a.txt'
        result.write_text('Completed')
        self.assertEqual(self.check(180, [signal]), self.normal)
        self.assertEqual(self.notices(), [])
        result.unlink()
        log = self.ws / 'state' / 'agent-activity.jsonl'
        log.write_text(json.dumps({'ts': 175, 'kind': 'done', 'task': {'id': 'task-a'}}) + '\n')
        self.assertEqual(self.check(181, [signal]), self.normal)
        self.assertEqual(self.notices(), [])

    def test_new_execution_generation_restarts_debounce(self):
        signal = self.signal()
        self.check(100, [signal])
        self.blocked(self.check(120, [signal]))
        path = self.ws / 'state' / 'activity' / 'task-a.json'
        data = json.loads(path.read_text())
        data.update(generation=2, last_activity_at=130)
        path.write_text(json.dumps(data))
        self.assertEqual(self.check(130, [signal]), self.normal)
        self.assertEqual(self.check(149, [signal]), self.normal)
        self.blocked(self.check(150, [signal]))
        self.assertEqual(self.notices(), [])

    def test_notice_is_durable_private_deduplicated_and_rearms_after_recovery(self):
        signal = self.signal()
        self.check(100, [signal])
        self.check(120, [signal])
        self.assertEqual(self.notices(), [])
        self.check(180, [signal])
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        first = notices[0].read_text()
        self.assertIn('[dm-only]', first)
        self.assertNotIn('private-provider-token-and-prompt', first)
        importlib.reload(detector)
        self.check(190, [signal])
        self.assertEqual(self.notices(), notices)
        self.assertEqual(notices[0].read_text(), first)
        self.activity('task-a', 191)
        self.assertEqual(self.check(191), self.normal)
        self.check(200, [signal])
        self.check(220, [signal])
        self.check(280, [signal])
        self.assertEqual(len(self.notices()), 2)

    def test_new_activity_between_polls_starts_a_new_silence_episode(self):
        self.blocked(self.check(280))
        self.activity('task-a', 500)
        waiting = self.check(680)
        self.blocked(waiting)
        self.assertEqual(waiting['since'], 680)
        self.assertEqual(self.notices(), [])
        self.check(739)
        self.assertEqual(self.notices(), [])
        self.check(740)
        self.assertEqual(len(self.notices()), 1)

    def test_unowned_task_never_inherits_a_global_core_signal(self):
        signal = self.signal('provider-limit', task=None)
        self.assertEqual(self.check(100, [signal], task='task-unknown'), self.normal)
        self.assertEqual(self.check(120, [signal], task='task-unknown'), self.normal)
        self.assertEqual(self.check(180, [signal], task='task-unknown'), self.normal)
        self.assertEqual(self.notices(), [])

    def test_live_signal_collector_classifies_only_fresh_supervisor_evidence(self):
        module = types.ModuleType('wedge_probes')
        module.probe_all = lambda workspace, now: []
        path = self.ws / 'state' / 'core-supervisor.json'
        cases = [
            ({'state': 'blocked-known', 'detail': 'You have hit your usage limit'}, 100, 'provider-limit'),
            ({'state': 'blocked-human', 'prompt': 'Please sign in to continue'}, 100, 'pane-prompt-blocked'),
            ({'state': 'running', 'detail': 'You have hit your usage limit'}, 100, None),
            ({'state': 'blocked-known', 'detail': 'You have hit your usage limit'}, 1, None),
            ({'state': 'blocked-human'}, 200, None),
            ('{invalid', 100, None),
        ]
        with patch.dict(sys.modules, {'wedge_probes': module}):
            for data, modified, expected in cases:
                with self.subTest(data=data, modified=modified):
                    detector._PROBE_CACHE.clear()
                    path.write_text(data if isinstance(data, str) else json.dumps(data))
                    os.utime(path, (modified, modified))
                    signals = detector._signals(self.ws, 100)
                    self.assertEqual([signal['kind'] for signal in signals], [expected] if expected else [])
        detector._PROBE_CACHE.clear()

    def test_concurrent_production_calls_reserve_exactly_one_owner_notice(self):
        signal = self.signal()
        self.check(100, [signal])
        self.check(120, [signal])
        context = multiprocessing.get_context('spawn')
        gate, answers = context.Event(), context.Queue()
        processes = [context.Process(target=concurrent_status, args=(self.ws, gate, answers)) for _ in range(6)]
        try:
            for process in processes:
                process.start()
            gate.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([answers.get(timeout=2) for _ in processes], [('ok', 'Waiting')] * 6)
            self.assertEqual(len(self.notices()), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            answers.close()

    def test_future_or_malformed_activity_cannot_invent_elapsed_silence(self):
        unknown = dict(self.normal, since=2000)
        path = self.ws / 'state' / 'activity' / 'task-a.json'
        for stamp in (2000, 'invalid', float('nan'), float('inf'), True, None):
            self.activity('task-a', stamp)
            self.assertEqual(self.check(1000, status=unknown), unknown)
        path.write_text('{broken')
        self.assertEqual(self.check(1000), self.normal)
        self.assertEqual(self.notices(), [])

    def test_malformed_jsonl_task_field_is_ignored_not_a_lost_scan(self):
        log = self.ws / 'state' / 'agent-activity.jsonl'
        log.write_text('\n'.join(json.dumps(row) for row in (
            {'ts': 279, 'task': ['task-a']},
            {'ts': 279, 'task': 'task-a'},
            {'ts': 100, 'kind': 'working', 'task': {'id': 'task-a'}},
        )) + '\n')
        self.blocked(self.check(280))

    def test_configured_threshold_and_manifest_defaults(self):
        manifest = json.loads((REPO / 'skills' / 'agent-activity' / 'manifest.json').read_text())
        self.assertIs(manifest['config']['LEDGER_STEP_PUBLISH_ENABLED'], True)
        self.assertEqual(manifest['config']['WEDGE_SILENCE_SECONDS'], 180)
        self.assertEqual(detector.status_for(self.ws, 'task-a', self.normal, now=399,
                                             signals=[], silence_s=300), self.normal)
        self.blocked(detector.status_for(self.ws, 'task-a', self.normal, now=400,
                                        signals=[], silence_s=300))

    def test_accepted_task_without_any_step_uses_production_activity_writer(self):
        from activity_bus import ActivityStore, LifecycleTransition
        sys.path.insert(0, str(REPO / 'skills' / 'agent-activity' / 'scripts'))
        import phase_line
        store = ActivityStore(self.ws)
        store.apply(LifecycleTransition('task-accepted', 'RUNNING', ts=100, room='!test:example'))
        with patch.object(detector, '_signals', return_value=[]), \
                patch.object(phase_line, 'setting', side_effect=lambda key, default: default):
            self.assertEqual(phase_line.published_status(self.ws, 'task-accepted', now=279)['phase'],
                             'Understanding the request')
            self.blocked(phase_line.published_status(self.ws, 'task-accepted', now=280))

    def test_phase_line_to_real_publisher_silence_and_recovery(self):
        sys.path.insert(0, str(REPO / 'skills' / 'agent-activity' / 'scripts'))
        import phase_line
        clock, calls, sent_at = [100.0], [], []
        pub = StepPublisher(self.ws / 'state', lambda body: calls.append(body) or sent_at.append(clock[0]) or {'status': 'completed'},
                            lambda message: None, lambda: clock[0],
                            status_of=lambda task: phase_line.published_status(self.ws, task, now=clock[0]))
        tasks = {'task-a': {'task_id': 'wire-a', 'execution_generation': 1, 'room_id': '!test:example'}}
        with patch.object(detector, '_signals', return_value=[]), \
                patch.object(phase_line, 'setting', side_effect=lambda key, default: default), \
                patch.object(phase_line.time, 'time', side_effect=lambda: clock[0]):
            phase_line.on_step(self.ws, 'task-a', 'Bash', {'command': 'pytest -q'},
                               'private test invocation', spawn=lambda *args: None)
            pub.publish(tasks)
            for stamp in range(110, 291, 10):
                clock[0] = stamp
                pub.publish(tasks)
            self.assertEqual([row['arguments']['phase'] for row in calls], ['Running tests', 'Waiting'])
            clock[0] = 300
            self.activity('task-a', 300)
            phase_line.on_step(self.ws, 'task-a', 'Read', {}, 'private filename', spawn=lambda *args: None)
            pub.publish(tasks)
        self.assertEqual([row['arguments']['phase'] for row in calls],
                         ['Running tests', 'Waiting', 'Reading files'])
        self.assertEqual(sent_at, [100, 280, 300])
        self.assertNotIn('private', json.dumps(calls))
        self.assertEqual(self.notices(), [])

    def test_real_publisher_emits_waiting_on_tick_and_resumes_without_duplicates(self):
        clock, signals, calls = [100.0], [], []
        pub = StepPublisher(self.ws / 'state', lambda body: calls.append(body) or {'status': 'completed'},
                            lambda message: None, lambda: clock[0],
                            status_of=lambda task: self.check(clock[0], signals, task=task))
        tasks = {'task-a': {'task_id': 'wire-a', 'execution_generation': 1, 'room_id': '!test:example'}}
        pub.publish(tasks)
        signals.append(self.signal())
        for stamp in (110, 120, 130, 140):
            clock[0] = stamp
            pub.publish(tasks)
        self.assertEqual([row['arguments']['phase'] for row in calls], ['Running tests', 'Waiting'])
        signals.clear()
        clock[0] = 150
        pub.publish(tasks)
        self.assertEqual([row['arguments']['phase'] for row in calls],
                         ['Running tests', 'Waiting', 'Running tests'])
        self.assertEqual(self.notices(), [])


if __name__ == '__main__':
    unittest.main()
