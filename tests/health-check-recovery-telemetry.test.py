#!/usr/bin/env python3
"""Offline recovery lifecycle and privacy regression tests."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['DO_NOT_TRACK'] = '1'
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('health_check', ROOT / 'src/health-check.py')
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class RecoveryMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'recovery.json'
        self.events = []
        self.mock = patch.object(hc, '_recovery_metric', side_effect=lambda event, **props: self.events.append((event, props)))
        self.mock.start()
        self.addCleanup(self.mock.stop)
        for name, value in [('RECOVER_WEDGE_SEC', 600), ('RECOVER_CONFIRM_SEC', 120),
                            ('RECOVER_COOLDOWN_SEC', 1800), ('RECOVER_MAX_PER_HOUR', 3)]:
            p = patch.object(hc, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_core(self, now, key='private-task-path', age=900, alive=True, status_ts=10, restart=True):
        return hc.recover_core_if_wedged(
            state_file=self.state, now=now, alive_fn=lambda: alive,
            oldest_task_fn=lambda: (key, age) if key else None,
            status_ts_fn=lambda: status_ts, just_booted_fn=lambda: False,
            restart_fn=lambda: restart, stopped_fn=lambda: False, sender=lambda _: False)

    def restart(self):
        self.run_core(10000)
        self.assertEqual(self.run_core(10121)['action'], 'restarted')

    def test_restart_is_not_recovery_and_progress_emits_once(self):
        self.restart()
        self.assertEqual([e[0] for e in self.events], ['core_recovery_attempted', 'core_restart_result'])
        self.run_core(10130, alive=False)
        self.assertEqual(len(self.events), 2)
        self.run_core(10140, status_ts=11)
        self.run_core(10150, status_ts=12)
        self.assertEqual(self.events[-1], ('core_recovery_result', {
            'outcome': 'progress_resumed', 'trigger': 'wedged', 'duration_bucket': '<1m'}))
        self.assertEqual(len(self.events), 3)
        self.assertNotIn('private-task-path', json.dumps(self.events))

    def test_dead_core_trigger_and_unknown_probe(self):
        self.run_core(10000, alive=False, key=None)
        self.run_core(10121, alive=False, key=None)
        self.assertEqual(self.events[0], ('core_recovery_attempted', {'trigger': 'dead'}))
        self.run_core(10130, alive=None, key=None)
        self.assertEqual(len(self.events), 2)
        self.run_core(10140, alive=True, key=None)
        self.assertEqual(self.events[-1][1]['outcome'], 'progress_resumed')

    def test_failed_restart_and_no_guard_events(self):
        self.run_core(10000)
        self.run_core(10001)
        self.assertEqual(self.events, [])
        self.assertEqual(self.run_core(10121, restart=False)['action'], 'restart_failed')
        self.assertEqual(self.events[-1][1]['outcome'], 'failed')
        self.assertNotIn('recovery_metric_pending', json.loads(self.state.read_text()))

    def test_recurrence_without_false_success(self):
        self.restart()
        self.run_core(12000)
        self.run_core(12121)
        self.assertEqual(self.events[2][1]['outcome'], 'still_unhealthy')
        self.assertEqual(self.events[3], ('core_recovery_attempted', {'trigger': 'wedged'}))

    def test_give_up_dedup_even_when_notification_fails(self):
        self.state.write_text(json.dumps({'wedge_first_seen': 9000, 'wedge_task': 'private-task-path',
                                         'wedge_status_ts': 10, 'wedge_mode': 'wedged', 'restart_history': [9900, 9910, 9920]}))
        self.run_core(10000)
        self.run_core(10030)
        self.assertEqual(self.events, [('core_recovery_gave_up', {})])

    def test_health_fix_observed_next_tick_and_only_once(self):
        checks = [{'name': 'private-plugin-name', 'status': 'down', 'detail': 'secret'},
                  {'name': 'voice-agent', 'status': 'down'}]
        hc.track_health_fix(checks, start=True, state_file=self.state, now=10)
        checks[0]['status'] = 'ok'
        hc.track_health_fix(checks, state_file=self.state, now=100)
        hc.track_health_fix(checks, state_file=self.state, now=110)
        self.assertEqual(self.events, [('health_fix_started', {}), ('health_fix_result', {
            'outcome': 'partially_resolved', 'duration_bucket': '1-5m'})])
        self.assertNotIn('private-plugin-name', json.dumps(self.events))
        self.assertNotIn('secret', json.dumps(self.events))

    def test_telemetry_failure_cannot_break_recovery(self):
        self.mock.stop()
        import telemetry
        with patch.object(telemetry, 'capture', side_effect=RuntimeError('offline')):
            self.restart()

    def test_opt_out_and_short_lived_delivery(self):
        self.mock.stop()
        import telemetry
        with patch.object(telemetry, '_dispatch') as dispatch:
            hc._recovery_metric('core_recovery_attempted', trigger='wedged')
            dispatch.assert_not_called()
        with patch.object(telemetry, 'capture') as capture:
            hc._recovery_metric('core_recovery_attempted', trigger='wedged')
            capture.assert_called_once_with('core_recovery_attempted', {'trigger': 'wedged'}, flush=True)


if __name__ == '__main__':
    unittest.main()
