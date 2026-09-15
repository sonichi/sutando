#!/usr/bin/env python3
"""Exercise recovery entry points across processes with real local HTTP capture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHILD = '''
import importlib.util, json, sys
from pathlib import Path
root, state, kind, raw = sys.argv[1:]
spec = importlib.util.spec_from_file_location('health_check', Path(root)/'src/health-check.py')
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)
args = json.loads(raw)
if kind == 'health':
    hc.track_health_fix(args['checks'], start=args.get('start', False),
                        state_file=Path(state)/'health.json', now=args['now'])
else:
    result = hc.recover_core_if_wedged(
        state_file=Path(state)/'core.json', now=args['now'],
        alive_fn=lambda: True, oldest_task_fn=lambda: ('private-task', 900),
        status_ts_fn=lambda: args.get('status_ts', 10), just_booted_fn=lambda: False,
        stopped_fn=lambda: False, restart_fn=lambda: args.get('restart', True),
        sender=lambda _: True)
    print(json.dumps(result))
'''


class LocalCapture(unittest.TestCase):
    def test_cross_process_recovery_and_opt_out(self):
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":1}')

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, POSTHOG_HOST=f'http://127.0.0.1:{server.server_port}',
                       POSTHOG_API_KEY='local-test-only', DO_NOT_TRACK='0', SUTANDO_TELEMETRY='1',
                       SUTANDO_STATE_DIR=directory,
                       SUTANDO_TELEMETRY_ID_FILE=str(Path(directory)/'telemetry-id'),
                       SUTANDO_SURFACE='oss', SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER='1')

            def run(kind, **args):
                result = subprocess.run([sys.executable, '-c', CHILD, str(ROOT), directory,
                                         kind, json.dumps(args)], env=env, capture_output=True,
                                        text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)

            run('core', now=10000)
            run('core', now=10121, restart=False)
            run('core', now=10122, restart=False)
            run('core', now=10123)
            run('core', now=10130, status_ts=11)
            run('core', now=10140, status_ts=12)
            checks = [{'name': 'private-a', 'status': 'down'},
                      {'name': 'private-b', 'status': 'down'}]
            run('health', now=10, start=True, checks=checks)
            checks[0]['status'] = 'ok'
            run('health', now=20, checks=checks)
            run('health', now=30, start=True, checks=checks)
            checks[1]['status'] = 'ok'
            run('health', now=40, checks=checks)
            run('health', now=50, checks=checks)

            issues = [e for e in received if e['event'].startswith('recovery_issue_')]
            detected = {e['properties']['issue_id'] for e in issues if e['event'].endswith('_detected')}
            recovered = [e['properties']['issue_id'] for e in issues if e['event'].endswith('_recovered')]
            self.assertEqual(len(detected), 3)
            self.assertEqual(len(recovered), 3)
            self.assertEqual(detected, set(recovered))
            self.assertEqual(sum(e['event'].endswith('_attempted') for e in issues), 6)
            self.assertEqual(len({e['distinct_id'] for e in received}), 1)
            self.assertNotIn('private-', json.dumps(received))
            self.assertTrue(all(e['api_key'] == 'local-test-only' for e in received))
            print('Local HTTP: 6 attempts, 3 unique issues, 3 recoveries (100%), 1 install')

            checks[0]['status'] = 'down'
            run('health', now=60, start=True, checks=checks)
            new_id = [e['properties']['issue_id'] for e in received
                      if e['event'] == 'recovery_issue_detected'][-1]
            self.assertNotIn(new_id, detected)
            print('Recurrence: new issue ID confirmed')
            before = len(received)
            env['DO_NOT_TRACK'] = '1'
            run('health', now=70, checks=[{'name': 'private-a', 'status': 'ok'}])
            self.assertEqual(len(received), before)
            print('DO_NOT_TRACK=1: zero HTTP events')


if __name__ == '__main__':
    unittest.main()
