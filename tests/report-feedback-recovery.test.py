#!/usr/bin/env python3
"""Recovery gating through the production CLI, using isolated workspace state."""
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import plistlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/report-feedback/report-feedback.py'
spec = importlib.util.spec_from_file_location('rf_recovery', SCRIPT)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)
VERSIONS = {
    'sutando': {'version': 'test-sutando', 'commit': 'a' * 40},
    'ag2space': {'version': 'test-ag2space', 'commit': 'b' * 40},
}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(rf, 'resolve_workspace', return_value=self.ws),
            mock.patch.object(rf, 'read_cloud_auth', return_value=('https://example.test', 'test')),
            mock.patch.object(rf, 'post_feedback', return_value=200),
            mock.patch.object(rf.time, 'time', return_value=10000),
            mock.patch.object(rf, 'build_versions', return_value=VERSIONS),
        ]
        self.workspace, self.auth, self.post, self.clock, self.versions = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.prefs()

    def run_cli(self, *args):
        with mock.patch.object(sys, 'argv', ['report-feedback.py', *args]):
            rf.main()

    def queue(self, title='bridge broken'):
        self.run_cli('--auto', '--title', title)
        return next(r['id'] for r in rf.list_drafts(self.ws) if r['payload']['title'] == title)

    def prefs(self, **values):
        # These cases exercise automatic reporting, so they opt in explicitly.
        path = self.ws / 'state/feedback-prefs.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'autoReport': True, **values}))

    def test_auto_does_not_post_before_recovery(self):
        self.run_cli('--auto', '--title', 'transient bridge failure')
        self.post.assert_not_called()

    def test_auto_is_held_without_reading_auth_logs_or_creating_card(self):
        self.prefs(askFirst=True, sendLogs=True)
        with mock.patch.object(rf, 'logs_excerpt', side_effect=AssertionError('too early')):
            incident = self.queue()
            self.run_cli('--apply')
        self.assertEqual(rf.load_draft(self.ws, incident)['payload']['recovery']['state'], 'not_started')
        self.auth.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(rf.hitl_manager(self.ws).active(), [])

    def test_deadline_is_exact_and_duplicate_does_not_extend_it(self):
        incident = self.queue()
        self.clock.return_value = 13599
        self.assertEqual(self.queue(), incident)
        self.run_cli('--apply')
        self.post.assert_not_called()
        self.clock.return_value = 13600
        self.run_cli('--apply')
        self.assertEqual(self.post.call_count, 1)
        ctx = self.post.call_args.args[1]['context']
        self.assertFalse(ctx['owner_approved'])
        self.assertEqual(ctx['recovery']['release_reason'], 'no_recovery_within_one_hour')
        self.run_cli('--apply')
        self.assertEqual(self.post.call_count, 1)

    def test_delayed_release_marks_original_observation_and_age(self):
        observed, released = 1767225600, 1768608000
        self.clock.return_value = observed
        incident = self.queue()
        original = rf.load_draft(self.ws, incident)
        self.clock.return_value = released - 1
        self.run_cli('--auto', '--title', 'bridge broken', '--body', 'later observation')
        self.assertEqual(rf.list_drafts(self.ws), [original])
        self.clock.return_value = released
        self.run_cli('--apply')
        self.run_cli('--apply')
        self.post.assert_called_once()
        payload = self.post.call_args.args[1]
        self.assertTrue(payload['body'].startswith('bridge broken\n'))
        self.assertIn('Original observation (UTC): 2026-01-01T00:00:00Z', payload['body'])
        self.assertIn('Released (UTC): 2026-01-17T00:00:00Z', payload['body'])
        self.assertIn('Elapsed hold duration: 16 days, 0:00:00', payload['body'])
        self.assertIn('Historical report', payload['body'])
        self.assertIn('at least 72 hours', payload['body'])
        self.assertEqual(payload['context']['idempotency_key'], incident)
        self.assertEqual(payload['context']['versions'], VERSIONS)
        self.assertEqual(payload['context']['recovery'], {
            **original['payload']['recovery'],
            'released': released, 'release_reason': 'no_recovery_within_one_hour',
            'observed_at': observed, 'held_seconds': 1382400, 'historical': True,
        })
        persisted = json.loads(rf.filed_receipt(self.ws, incident).read_text())
        self.assertEqual(persisted['created'], observed)
        self.assertEqual(persisted['payload']['body'], original['payload']['body'])
        self.assertNotIn('observed_at', persisted['payload']['recovery'])

    def test_historical_warning_at_72_hour_boundary(self):
        observed = 1767225600
        for seconds, release_text, duration, historical in (
            (3600, '2026-01-01T01:00:00Z', '1:00:00', False),
            (259199, '2026-01-03T23:59:59Z', '2 days, 23:59:59', False),
            (259200, '2026-01-04T00:00:00Z', '3 days, 0:00:00', True),
        ):
            with self.subTest(seconds=seconds):
                self.post.reset_mock()
                self.clock.return_value = observed
                self.queue(f'held for {seconds} seconds')
                self.clock.return_value = observed + seconds
                self.run_cli('--apply')
                self.post.assert_called_once()
                payload = self.post.call_args.args[1]
                self.assertIn('Original observation (UTC): 2026-01-01T00:00:00Z', payload['body'])
                self.assertIn(f'Released (UTC): {release_text}', payload['body'])
                self.assertIn(f'Elapsed hold duration: {duration}', payload['body'])
                self.assertEqual('Historical report' in payload['body'], historical)
                self.assertEqual(payload['context']['recovery']['held_seconds'], seconds)
                self.assertIs(payload['context']['recovery']['historical'], historical)
        self.post.reset_mock()
        self.clock.return_value = observed
        incident = self.queue('signed out at release')
        self.auth.return_value = (None, None)
        self.clock.return_value = observed + 3600
        self.run_cli('--apply')
        self.post.assert_not_called()
        self.assertEqual(rf.load_draft(self.ws, incident)['payload']['recovery']['released'], observed + 3600)
        self.auth.return_value = ('https://example.test', 'test')
        self.clock.return_value = observed + 16 * 86400
        self.run_cli('--apply')
        self.post.assert_called_once()
        payload = self.post.call_args.args[1]
        self.assertIn('Released (UTC): 2026-01-01T01:00:00Z', payload['body'])
        self.assertIn('Elapsed hold duration: 1:00:00', payload['body'])
        self.assertNotIn('Historical report', payload['body'])
        self.assertEqual(payload['context']['recovery']['released'], observed + 3600)
        self.assertEqual(payload['context']['recovery']['held_seconds'], 3600)
        self.assertIs(payload['context']['recovery']['historical'], False)

    def test_legacy_draft_age_is_unknown(self):
        released = 1768608000
        for created, observed_at, observation_text in (
            (None, None, 'unknown'),
            ('invalid timestamp', None, 'unknown'),
            (released + 1, released + 1, '2026-01-17T00:00:01Z'),
        ):
            with self.subTest(created=created):
                self.post.reset_mock()
                self.clock.return_value = 1767225600
                incident = self.queue(f'unknown age {created!r}')
                rec = rf.load_draft(self.ws, incident)
                if created is None:
                    del rec['created']
                else:
                    rec['created'] = created
                rf.save_draft(self.ws, rec)
                self.clock.return_value = released
                self.run_cli('--apply')
                self.run_cli('--apply')
                self.post.assert_called_once()
                payload = self.post.call_args.args[1]
                self.assertIn(f'Original observation (UTC): {observation_text}', payload['body'])
                self.assertIn('Released (UTC): 2026-01-17T00:00:00Z', payload['body'])
                self.assertIn('Elapsed hold duration: unknown', payload['body'])
                self.assertNotIn('Historical report', payload['body'])
                recovery = payload['context']['recovery']
                self.assertEqual(recovery['observed_at'], observed_at)
                self.assertIsNone(recovery['held_seconds'])
                self.assertIsNone(recovery['historical'])
                self.assertEqual(recovery['released'], released)
                persisted = json.loads(rf.filed_receipt(self.ws, incident).read_text())
                if created is None:
                    self.assertNotIn('created', persisted)
                else:
                    self.assertEqual(persisted['created'], created)

    def test_manual_submission_body_remains_unchanged(self):
        versions_body = ('\n\n### Build versions\n'
                         f'- Sutando: test-sutando (commit: {"a" * 40})\n'
                         f'- AG2Space: test-ag2space (commit: {"b" * 40})')
        for body in ('  Owner report.\nSecond line.  ', ''):
            for ask in (False, True):
                with self.subTest(body=body, ask=ask):
                    self.post.reset_mock()
                    self.clock.return_value = 1767225600
                    title = f'manual {body!r} {ask}'
                    self.run_cli('--title', title, '--body', body, *(['--ask'] if ask else []))
                    if ask:
                        self.post.assert_not_called()
                        incident = rf.list_drafts(self.ws)[0]['id']
                        self.clock.return_value = 1768608000
                        self.run_cli('--decide', incident, 'file')
                    self.post.assert_called_once()
                    payload = self.post.call_args.args[1]
                    self.assertEqual(payload['body'], (body.strip() or title) + versions_body)
                    self.assertNotIn('recovery', payload['context'])

    def test_started_recovery_holds_beyond_deadline_then_success_suppresses(self):
        incident = self.queue()
        self.run_cli('--recovery', incident, 'started')
        self.clock.return_value = 20000
        self.run_cli('--apply')
        self.run_cli('--recovery', incident, 'succeeded')
        self.run_cli('--apply')
        self.post.assert_not_called()
        self.assertEqual(rf.list_drafts(self.ws), [])
        self.assertTrue(rf._draft_path(self.ws, incident).with_suffix('.suppressed').exists())

    def test_failure_releases_only_its_incident_and_preserves_log_opt_out(self):
        incident = self.queue()
        self.queue('different incident')
        self.run_cli('--recovery', incident, 'failed')
        self.assertEqual(self.post.call_count, 1)
        ctx = self.post.call_args.args[1]['context']
        self.assertEqual(ctx['recovery']['release_reason'], 'recovery_failed')
        self.assertTrue(ctx['logs_opted_out'])
        self.assertEqual(len(rf.list_drafts(self.ws)), 1)

    def test_ask_first_is_delayed_and_success_cancels_unsent_card(self):
        self.prefs(askFirst=True)
        incident = self.queue()
        self.run_cli('--recovery', incident, 'failed')
        self.assertEqual(len(rf.hitl_manager(self.ws).active()), 1)
        self.post.assert_not_called()
        self.run_cli('--recovery', incident, 'succeeded')
        self.assertEqual(rf.hitl_manager(self.ws).active(), [])
        self.run_cli('--apply')
        self.post.assert_not_called()

    def test_off_switch_at_release_suppresses_and_does_not_revive(self):
        self.queue()
        self.prefs(autoReport=False)
        self.clock.return_value = 13600
        self.run_cli('--apply')
        self.prefs(autoReport=True)
        self.run_cli('--apply')
        self.post.assert_not_called()
        self.assertEqual(rf.list_drafts(self.ws), [])

    def test_unknown_outcome_and_unknown_incident_do_not_release(self):
        incident = self.queue()
        for args in [(incident, 'healthy-ish'), ('../invalid', 'failed')]:
            with self.assertRaises(SystemExit):
                self.run_cli('--recovery', *args)
        self.post.assert_not_called()

    def test_manual_submission_stays_immediate(self):
        self.run_cli('--title', 'user explicitly requested')
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(rf.list_drafts(self.ws), [])

    def test_ambiguous_post_is_never_automatically_retried(self):
        incident = self.queue()
        self.post.side_effect = OSError('connection lost')
        self.run_cli('--recovery', incident, 'failed')
        self.run_cli('--apply')
        self.run_cli('--apply')
        self.assertEqual(self.post.call_count, 1)
        self.assertTrue(rf.posting_marker(self.ws, incident).exists())
        self.assertEqual(len(rf.hitl_manager(self.ws).active()), 1)

    def test_signout_keeps_report_and_uses_current_log_preference_on_retry(self):
        incident = self.queue()
        self.auth.return_value = (None, None)
        self.run_cli('--recovery', incident, 'failed')
        self.post.assert_not_called()
        self.assertIsNotNone(rf.load_draft(self.ws, incident))
        self.auth.return_value = ('https://example.test', 'test')
        self.prefs(sendLogs=True)
        with mock.patch.object(rf, 'logs_excerpt', return_value=('scrubbed', ['a.log'])):
            self.run_cli('--apply')
        self.assertEqual(self.post.call_args.args[1]['context']['last_logs_excerpt'], 'scrubbed')

    def test_restart_and_concurrent_flushes_post_once(self):
        self.queue()
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                received.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(b'{"ok":true}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        code = (
            'import importlib.util,sys,time; from pathlib import Path\n'
            f's=importlib.util.spec_from_file_location("rf", {str(SCRIPT)!r}); '
            'rf=importlib.util.module_from_spec(s); s.loader.exec_module(rf)\n'
            f'rf.resolve_workspace=lambda: Path({str(self.ws)!r})\n'
            f'rf.read_cloud_auth=lambda ws: ({base!r}, "test-token")\n'
            'sys.argv=["report-feedback.py", "--apply"]; rf.main()\n'
        )
        workers = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, stdout + stderr)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['context']['recovery']['release_reason'], 'no_recovery_within_one_hour')

    def test_stop_hook_skips_worker_lock_even_with_a_pending_click(self):
        hook_path = SCRIPT.parent / 'hooks/apply-clicks.py'
        code = (
            'import importlib.util; from pathlib import Path\n'
            f's=importlib.util.spec_from_file_location("hook", {str(hook_path)!r}); '
            'hook=importlib.util.module_from_spec(s); s.loader.exec_module(hook)\n'
            'rf=hook.load_rf(); rf.pending_clicks=lambda ws: 1\n'
            f'assert hook.main(Path({str(self.ws)!r}), rf=rf) == 0\n'
        )
        with rf.locked_file(self.ws / 'state/feedback-reports.lock'):
            result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                    text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_renders_a_periodic_apply_command(self):
        result = subprocess.run([sys.executable, str(SCRIPT.parent / 'install-worker.py'), '--render'],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = plistlib.loads(result.stdout)
        self.assertEqual(job['StartInterval'], 60)
        self.assertTrue(job['RunAtLoad'])
        self.assertEqual(Path(job['ProgramArguments'][1]).resolve(), SCRIPT.resolve())
        self.assertEqual(job['ProgramArguments'][2], '--apply')

    def test_pending_hold_cannot_be_filed_by_decide_or_stop(self):
        incident = self.queue()
        with self.assertRaises(SystemExit) as result:
            self.run_cli('--decide', incident, 'file')
        self.assertEqual(result.exception.code, 3)
        self.clock.return_value = 13600
        rf.apply_clicks(self.ws, rf.read_prefs(self.ws), 'test', release_automatic=False)
        self.post.assert_not_called()

    def test_terminal_failure_cannot_restart_or_reset_after_release(self):
        incident = self.queue()
        rf.update_recovery(self.ws, incident, 'failed')
        with self.assertRaisesRegex(ValueError, 'final failed'):
            rf.update_recovery(self.ws, incident, 'started')
        self.auth.return_value = (None, None)
        self.run_cli('--apply')
        with self.assertRaisesRegex(ValueError, 'already released'):
            rf.update_recovery(self.ws, incident, 'started')

    def test_daily_cap_is_checked_when_releasing(self):
        for i in range(6):
            self.queue(f'failure {i}')
        self.clock.return_value = 13600
        self.run_cli('--apply')
        self.assertEqual(self.post.call_count, 5)
        self.assertEqual(len(rf.list_drafts(self.ws)), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
