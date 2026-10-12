#!/usr/bin/env python3
"""Independent diagnostics must reach the heartbeat, including recovery and expiry."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
_SANDBOX = tempfile.TemporaryDirectory()
os.environ['AGENT_CONNECT_STATE_DIR'] = str(Path(_SANDBOX.name) / 'state')
os.environ['AGENT_CONNECT_TASK_DIR'] = str(Path(_SANDBOX.name) / 'tasks')
os.environ['AGENT_CONNECT_RESULT_DIR'] = str(Path(_SANDBOX.name) / 'results')
os.environ['SUTANDO_HOST_LABEL'] = 'testhost'
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import remote_gateway_bridge as bridge
spec = importlib.util.spec_from_file_location('health_check', REPO / 'src' / 'health-check.py')
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.patch = patch.object(bridge, '_STATE', self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.state / 'core-status.json').write_text(json.dumps({'status': 'running', 'step': 'private task'}))

    def heartbeat(self):
        with patch.object(bridge, '_req') as post, patch.object(bridge, '_heartbeat_retry_at', 0.0, create=True):
            self.assertTrue(bridge._post_heartbeat(set(), force=True))
        return post.call_args.args[2]

    def publish(self, status):
        hc.publish_health_report([{'name': 'private name', 'status': status, 'detail': 'secret detail'}], self.state)

    def test_failure_and_recovery_over_http(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(bridge, 'URL', f'http://127.0.0.1:{server.server_port}'), \
                    patch.object(bridge, 'TOKEN', 'test-token'), \
                    patch.object(bridge, '_heartbeat_retry_at', 0.0, create=True):
                for status in ('down', 'ok'):
                    self.publish(status)
                    self.assertTrue(bridge._post_heartbeat(set(), force=True))
            self.assertEqual([path for path, _ in received], ['/v1/heartbeat'] * 2)
            self.assertEqual([payload['status'] for _, payload in received], ['error', 'running'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_failure_overrides_running_and_recovery_restores_it(self):
        self.publish('down')
        payload = self.heartbeat()
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['step'], 'Health check: 1 failing check(s)')
        self.assertNotIn('private', json.dumps(payload))
        self.assertNotIn('secret', (self.state / 'agent-health.json').read_text())
        self.publish('ok')
        self.assertEqual(self.heartbeat()['status'], 'running')

    def test_failure_reports_without_core_status(self):
        (self.state / 'core-status.json').unlink()
        self.publish('down')
        self.assertEqual(self.heartbeat()['status'], 'error')
        self.publish('ok')
        self.assertNotIn('status', self.heartbeat())

    def test_warnings_are_not_failures(self):
        self.publish('warn')
        self.assertEqual(self.heartbeat()['status'], 'running')

    def test_expired_report_passes_core_status_through(self):
        # Regression: an expired report sent the literal 'unknown' over a valid core status, and
        # nothing on a desktop install reruns health-check, so the agent read 'unknown' forever.
        old = time.time() - bridge._HEALTH_REPORT_MAX_AGE - 1
        for failures in (0, 1):
            with self.subTest(failures=failures):
                (self.state / 'agent-health.json').write_text(json.dumps(
                    {'version': 1, 'checked_at': old, 'total': 1, 'failures': failures}))
                payload = self.heartbeat()
                self.assertEqual((payload['status'], payload['step']), ('running', 'private task'))

    def test_fresh_report_still_decides(self):
        for failures, expected in ((1, 'error'), (0, 'running')):
            with self.subTest(failures=failures):
                (self.state / 'agent-health.json').write_text(json.dumps(
                    {'version': 1, 'checked_at': time.time(), 'total': 2, 'failures': failures}))
                self.assertEqual(self.heartbeat()['status'], expected)

    def test_future_and_invalid_reports_are_unknown(self):
        valid = {'version': 1, 'checked_at': time.time(), 'total': 1, 'failures': 1}
        invalid = [[], {}, None,
                   {**valid, 'checked_at': time.time() + 60}, {**valid, 'checked_at': float('nan')},
                   {**valid, 'failures': True}, {**valid, 'failures': -1}, {**valid, 'total': 0}]
        for report in invalid:
            with self.subTest(report=report):
                (self.state / 'agent-health.json').write_text(json.dumps(report))
                self.assertEqual(self.heartbeat()['status'], 'unknown')
        (self.state / 'agent-health.json').write_text('{')
        self.assertEqual(self.heartbeat()['status'], 'unknown')

    def test_invalid_diagnostics_do_not_hide_explicit_core_failure(self):
        (self.state / 'agent-health.json').write_text('{')
        for status in ('error', 'offline'):
            (self.state / 'core-status.json').write_text(json.dumps({'status': status}))
            self.assertEqual(self.heartbeat()['status'], status)

    def test_legacy_client_without_report_keeps_status(self):
        self.assertEqual(self.heartbeat()['status'], 'running')

    def test_write_failure_preserves_previous_report_and_cleans_tempfile(self):
        self.publish('down')
        with patch.object(hc.os, 'replace', side_effect=OSError('disk full')):
            self.publish('ok')
        self.assertEqual(self.heartbeat()['status'], 'error')
        self.assertEqual(list(self.state.glob('.agent-health-*')), [])

    def test_cleanup_error_never_breaks_health_check(self):
        with patch.object(hc.os, 'replace', side_effect=OSError('disk full')), \
                patch.object(Path, 'unlink', side_effect=OSError('read only')):
            self.publish('down')

    def test_unwritable_state_directory_is_best_effort(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('read only')):
            self.publish('down')
        self.assertFalse((self.state / 'agent-health.json').exists())

    def test_residual_recheck_publishes_verified_recovery(self):
        failing = [{'name': 'fixture-check', 'status': 'down', 'detail': ''}]
        passing = [{'name': 'fixture-check', 'status': 'ok', 'detail': ''}]
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['health-check.py', '--quiet', '--fix', '--emit-task']))
            stack.enter_context(patch.object(hc, 'run_all_checks', side_effect=[failing, passing]))
            for name in ('track_health_fix', 'apply_skill_symlink_fixes',
                         'apply_task_watcher_sentinel_fix', 'apply_claude_hooks_fix',
                         'emit_task_for_failures'):
                stack.enter_context(patch.object(hc, name))
            stack.enter_context(patch.object(hc, 'fix_down_bridges', return_value=[]))
            stack.enter_context(patch.object(hc, '_any_core_alive', return_value=False))
            stack.enter_context(patch.object(hc.time, 'sleep'))
            stack.enter_context(patch('builtins.print'))
            publish = stack.enter_context(patch.object(hc, 'publish_health_report',
                                                       wraps=lambda c: hc_publish(c, self.state)))
            with self.assertRaises(SystemExit):
                hc.main()
            self.assertEqual([call.args[0] for call in publish.call_args_list], [failing, passing])
            self.assertEqual(self.heartbeat()['status'], 'running')

    def test_main_publishes_before_json_and_quiet_exit(self):
        checks = [{'name': 'core', 'status': 'down', 'detail': ''}]
        for args in (['health-check.py', '--json'], ['health-check.py', '--quiet']):
            with self.subTest(args=args), patch.object(sys, 'argv', args), \
                    patch.object(hc, 'run_all_checks', return_value=checks), \
                    patch.object(hc, 'track_health_fix'), patch.object(hc, 'WORKSPACE_DIR', self.state.parent), \
                    patch.object(hc, 'publish_health_report', wraps=lambda c: hc_publish(c, self.state)) as publish, \
                    patch('builtins.print'):
                try:
                    hc.main()
                except SystemExit as e:
                    self.assertEqual(e.code, 1)
                publish.assert_called_once_with(checks)
                self.assertEqual(self.heartbeat()['status'], 'error')


STALE_STATUS_AGE = getattr(bridge, '_CORE_STATUS_MAX_AGE', 3600) + 60


class CoreLivenessTests(unittest.TestCase):
    """The core's beat (health snapshot `alive`) stands in for a missing or stale self-report."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for name, value in (('_STATE', self.state), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def heartbeat(self, alive):
        snap = {'agents': [{'role': 'core', 'alive': alive}]} if alive is not None else None
        with patch.object(bridge, '_health_snapshot', return_value=snap), \
                patch.object(bridge, '_req') as post:
            self.assertTrue(bridge._post_heartbeat(set(), force=True))
        return post.call_args.args[2]

    def write_status(self, status, age):
        (self.state / 'core-status.json').write_text(
            json.dumps({'status': status, 'step': 'x', 'ts': time.time() - age}))

    def test_missing_core_status_reports_the_beat(self):
        # Regression: no core-status.json meant a status-less heartbeat, so the broker never
        # recorded a status for the agent and showed it 'unknown' forever.
        self.assertEqual(self.heartbeat(True).get('status'), 'idle')
        self.assertEqual(self.heartbeat(False).get('status'), 'offline')

    def test_malformed_core_status_reports_the_beat(self):
        for body in ('[1, 2]', '{ nope', json.dumps({'status': 7})):
            with self.subTest(body=body):
                (self.state / 'core-status.json').write_text(body)
                self.assertEqual(self.heartbeat(True).get('status'), 'idle')

    def test_no_beat_to_judge_claims_nothing(self):
        self.assertNotIn('status', self.heartbeat(None))

    def test_a_dead_beat_overrides_an_old_self_report(self):
        self.write_status('idle', STALE_STATUS_AGE)
        payload = self.heartbeat(False)
        self.assertEqual(payload['status'], 'offline')
        self.assertNotIn('step', payload)

    def test_a_dead_beat_does_not_override_a_recent_self_report(self):
        self.write_status('running', 30)
        self.assertEqual(self.heartbeat(False)['status'], 'running')

    def test_an_old_self_report_stands_while_the_core_beats(self):
        for status in ('idle', 'running'):
            with self.subTest(status=status):
                self.write_status(status, STALE_STATUS_AGE)
                self.assertEqual(self.heartbeat(True)['status'], status)

    def test_a_fresh_failing_report_still_wins_over_the_beat(self):
        (self.state / 'agent-health.json').write_text(json.dumps(
            {'version': 1, 'checked_at': time.time(), 'total': 1, 'failures': 1}))
        self.assertEqual(self.heartbeat(True)['status'], 'error')


class UnsupportedHeartbeatTests(unittest.TestCase):
    """A 404/405 backs off and retries; it no longer switches heartbeats off until restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (('_STATE', Path(self.tmp.name)), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0), ('_heartbeat_disabled', False),
                            ('_last_heartbeat_at', 0.0), ('_log', lambda m: None)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def not_found(*a, **k):
        import urllib.error
        raise urllib.error.HTTPError('http://gw/v1/heartbeat', 404, 'Not Found', {}, None)

    def test_404_backs_off_then_retries_and_recovers(self):
        # Regression: one 404/405 switched heartbeats off until the bridge restarted.
        now = [1_000_000.0]
        with patch.object(bridge.time, 'time', side_effect=lambda: now[0]), \
                patch.object(bridge, '_req', side_effect=self.not_found) as req:
            self.assertFalse(bridge._post_heartbeat(set(), force=True))
            self.assertEqual(bridge._heartbeat_retry_at - now[0], bridge.HEARTBEAT_INTERVAL)
            self.assertFalse(bridge._post_heartbeat(set(), force=True))
            self.assertEqual(req.call_count, 1)  # inside the backoff: not sent
            now[0] = bridge._heartbeat_retry_at
            self.assertFalse(bridge._post_heartbeat(set(), force=True))
            self.assertEqual(req.call_count, 2)  # retried after the window
            self.assertEqual(bridge._heartbeat_retry_at - now[0], 2 * bridge.HEARTBEAT_INTERVAL)
            for _ in range(20):
                now[0] = bridge._heartbeat_retry_at
                bridge._post_heartbeat(set(), force=True)
            self.assertEqual(bridge._heartbeat_retry_at - now[0], bridge.HEARTBEAT_UNSUPPORTED_MAX_BACKOFF)
            now[0] = bridge._heartbeat_retry_at
            req.side_effect = None
            self.assertTrue(bridge._post_heartbeat(set(), force=True))
            self.assertEqual((bridge._heartbeat_retry_at, bridge._heartbeat_backoff), (0.0, 0.0))


hc_publish = hc.publish_health_report
if __name__ == '__main__':
    unittest.main()
