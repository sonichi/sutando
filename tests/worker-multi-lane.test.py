#!/usr/bin/env python3
"""Worker-wide inbox adoption and destination routing across two live lane leases."""
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env
isolate_os_environ()

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'packages/ag2-sparrow')]
import connections as C
import worker_lane as L
from ag2_sparrow import delivery_destination

W = 'a' * 32
CHILD = r'''
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import connections as C, worker_lane as L, workspace_lock as lock
ws, worker, instance = Path(sys.argv[3]), sys.argv[4], sys.argv[5]
role = 'gateway-bridge.' + instance
held = lock.acquire(role, ws)
print(held.status, flush=True)
try:
    for line in sys.stdin:
        if line.strip() == 'stop':
            break
        own = C.worker_dirs(worker, ws)[1]
        moved = L.adopt_stray_results(worker, ws, own)
        moved += L.adopt_stray_proactive(worker, ws / 'results', own)
        print(json.dumps(moved), flush=True)
finally:
    lock.release(role, ws)
'''


class MultiLane(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.rows = []
        self.lanes = {}
        for server in ('alpha.example', 'beta.example'):
            core = server.replace('.', '-')
            base = dict(server=server, gateway_url='https://' + server, added_by='owner',
                        added_at='2026-11-01T00:00:00Z', verified=True, server_source='identity')
            self.rows.append(dict(base, instance=core, token_ref=C.token_ref_for(core),
                                  primary=server.startswith('alpha'), agent_mxid='@core:' + server))
            lane = C.worker_instance_for(server, W)
            self.lanes[server] = lane
            self.rows.append(dict(base, instance=lane, token_ref=C.token_ref_for(lane), primary=False,
                                  agent_mxid='@worker:' + server, worker=W))
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(schema_version=1, connections=self.rows)))
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.tasks, self.results = C.ensure_worker_dirs(W, self.ws)
        (self.ws / 'results').mkdir(exist_ok=True)

    def process(self, server):
        proc = subprocess.Popen([sys.executable, '-c', CHILD, str(ROOT / 'src'),
                                 str(ROOT / 'packages/ag2-sparrow'), str(self.ws), W, self.lanes[server]],
                                env=isolated_child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.stdin.write('stop\n')
                proc.stdin.flush()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
        self.addCleanup(cleanup)
        self.assertNotEqual(proc.stdout.readline().strip(), 'deferred')
        return proc

    def command(self, proc):
        proc.stdin.write('adopt\n')
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def result(self, server, suffix='one'):
        name = 'task-' + self.lanes[server] + '~' + suffix + '.txt'
        (self.ws / 'results' / name).write_text('answer\n')
        return name

    def test_two_live_sessions_race_one_result_and_only_primary_adopts(self):
        primary, secondary = self.process('alpha.example'), self.process('beta.example')
        name = self.result('beta.example')
        for proc in (secondary, primary):
            proc.stdin.write('adopt\n')
            proc.stdin.flush()
        other, mine = json.loads(secondary.stdout.readline()), json.loads(primary.stdout.readline())
        self.assertEqual((other, mine), ([], [name]))
        self.assertEqual(self.command(primary), [])
        self.assertEqual(self.command(secondary), [])
        self.assertEqual((self.results / name).read_text(), 'answer\n')
        self.assertFalse((self.ws / 'results' / name).exists())
        for lane in self.lanes.values():
            self.assertEqual(L.lane_dirs(self.ws, lane), (self.tasks, self.results, True))

    def test_primary_switch_moves_adoption_lease_without_changing_inbox(self):
        old, new = self.process('alpha.example'), self.process('beta.example')
        before = self.result('beta.example', 'before')
        self.assertEqual(self.command(old), [before])
        C.set_primary('beta-example', workspace=self.ws)
        after = self.result('alpha.example', 'after')
        note = 'proactive-7.by-' + W + '.txt'
        (self.ws / 'results' / note).write_text('[channel: !r:alpha.example]\nhello\n')
        self.assertEqual(self.command(old), [])
        self.assertTrue((self.ws / 'results' / after).exists())
        self.assertEqual(self.command(new), [after, note])
        self.assertEqual(C.worker_dirs(W, self.ws), (self.tasks, self.results))
        self.assertEqual(self.command(old) + self.command(new), [])
        self.assertEqual(sorted(p.name for p in self.results.glob('*.txt')), sorted([before, after, note]))
        gate = L.claim_gate(W, instance=self.lanes['alpha.example'], workspace=self.ws,
                            route=delivery_destination.route)
        self.assertTrue(gate(self.results / note), 'old primary still serves its addressed room')

    def test_routing_is_selected_at_drain_time_in_the_shared_worker_folder(self):
        gates = {server: L.claim_gate(W, instance=lane, workspace=self.ws, route=delivery_destination.route)
                 for server, lane in self.lanes.items()}
        for server, body in [('beta.example', '[channel: !r:beta.example]\nhello'),
                             ('alpha.example', 'hello')]:
            note = self.results / ('proactive-9.by-' + W + '.txt')
            note.write_text(body)
            self.assertEqual([s for s, gate in gates.items() if gate(note)], [server])
        C.set_primary('beta-example', workspace=self.ws)
        self.assertEqual([s for s, gate in gates.items() if gate(note)], ['beta.example'])


if __name__ == '__main__':
    unittest.main()
