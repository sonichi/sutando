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
        with patch.object(bridge, '_req') as post, patch.object(bridge, '_heartbeat_disabled', False):
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
                    patch.object(bridge, '_heartbeat_disabled', False):
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

    def test_expired_future_and_invalid_reports_are_unknown(self):
        valid = {'version': 1, 'checked_at': time.time(), 'total': 1, 'failures': 1}
        invalid = [[], {}, None, {**valid, 'checked_at': time.time() - bridge._HEALTH_REPORT_MAX_AGE - 1},
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


hc_publish = hc.publish_health_report
if __name__ == '__main__':
    unittest.main()
