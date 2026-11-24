#!/usr/bin/env python3
"""A worker with agents on two servers: one folder state, one retirement, one restart owed per lane."""
from _helpers.hermetic_env import isolate_os_environ
isolate_os_environ()

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'packages/ag2-sparrow')]
import connections as C
import worker_lane as L

W = 'a' * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        rows, self.lanes = [], {}
        for server in ('alpha.example', 'beta.example'):
            core = server.replace('.', '-')
            base = dict(server=server, gateway_url='https://' + server, added_by='owner',
                        added_at='2026-11-01T00:00:00Z', verified=True, server_source='identity')
            rows.append(dict(base, instance=core, token_ref=C.token_ref_for(core),
                             primary=server.startswith('alpha'), agent_mxid='@core:' + server))
            lane = C.worker_instance_for(server, W)
            self.lanes[server] = lane
            rows.append(dict(base, instance=lane, token_ref=C.token_ref_for(lane), primary=False,
                             agent_mxid='@worker:' + server, worker=W))
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(schema_version=1, connections=rows)))

    def stored(self):
        return json.loads(C.record_path(self.ws).read_text()).get('worker_storage', {}).get(W, {})

    def mirrored(self, key):
        return [r.get(key) for r in C.worker_identities(W, self.ws)]


class TwoServers(Base):

    def test_the_switch_states_live_in_the_one_storage_entry_and_mirror_on_every_row(self):
        C.set_worker_dirs(W, C.WORKER_DIRS_MIGRATING, workspace=self.ws)
        self.assertEqual(C.worker_dirs_state(W, self.ws), 'migrating')
        self.assertEqual(self.stored().get('switch'), 'migrating')
        self.assertEqual(self.mirrored('dirs'), ['migrating', 'migrating'])
        self.assertIsNotNone(C.worker_target_dirs(W, self.ws))
        self.assertIsNone(C.worker_lane_dirs(W, self.ws))
        self.assertIsNone(C.worker_dirs(W, self.ws))
        C.set_worker_dirs(W, C.WORKER_DIRS_COMMITTING, workspace=self.ws)
        self.assertEqual(C.worker_lane_dirs(W, self.ws), C.worker_target_dirs(W, self.ws))
        self.assertIsNone(C.worker_dirs(W, self.ws))
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertEqual(self.stored(), {'agent_mxid': '@worker:alpha.example', 'own': True})
        self.assertEqual(self.mirrored('dirs'), ['own', 'own'])
        self.assertEqual(C.worker_dirs(W, self.ws), C.worker_target_dirs(W, self.ws))
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertEqual((C.worker_dirs_state(W, self.ws), self.mirrored('dirs')), ('', [None, None]))

    def test_a_storage_entry_holding_a_switch_and_own_at_once_is_refused(self):
        path = C.record_path(self.ws)
        base = json.loads(path.read_text())
        for home in ({'agent_mxid': '@worker:alpha.example', 'own': True, 'switch': 'migrating'},
                     {'agent_mxid': '@worker:alpha.example', 'own': False, 'switch': 'own'},
                     {'agent_mxid': '@worker:alpha.example', 'own': False, 'retiring': False},
                     {'agent_mxid': '@worker:alpha.example', 'own': False, 'extra': 1}):
            path.write_text(json.dumps({**base, 'worker_storage': {W: home}}))
            with self.subTest(home=home), self.assertRaises(C.SchemaError):
                C._read_record(path)

    def test_retirement_marks_stops_and_disables_every_identity_and_enable_undoes_it(self):
        rows = C.begin_worker_retirement(W, workspace=self.ws)
        self.assertEqual(sorted(r['instance'] for r in rows), sorted(self.lanes.values()))
        self.assertTrue(C.worker_retiring(W, self.ws))
        self.assertEqual(self.mirrored('retiring'), [True, True])
        self.assertEqual(self.mirrored('disabled'), [True, True])
        with self.assertRaises(C.ConnectionsError):
            C.set_worker_dirs(W, C.WORKER_DIRS_MIGRATING, workspace=self.ws)
        back = C.end_worker_retirement(W, workspace=self.ws)
        self.assertEqual(len(back), 2)
        self.assertFalse(C.worker_retiring(W, self.ws))
        self.assertEqual(self.mirrored('disabled'), [False, False])
        self.assertEqual(self.mirrored('retiring'), [None, None])

    def test_a_fully_retired_worker_keeps_its_anchor_but_not_its_retirement(self):
        C.begin_worker_retirement(W, workspace=self.ws)
        removed = C.retire_worker_identity(W, workspace=self.ws)
        self.assertEqual(len(removed), 2)
        self.assertEqual(C.worker_identities(W, self.ws), [])
        self.assertNotIn('retiring', self.stored())
        self.assertFalse(C.worker_retiring(W, self.ws))

    def test_a_restart_is_owed_per_lane_and_an_earlier_per_worker_one_is_taken_over(self):
        alpha, beta = self.lanes['alpha.example'], self.lanes['beta.example']
        L._owe_restart(W, self.ws, alpha, 'stopped_for_relaunch')
        L._owe_restart(W, self.ws, beta, 'launch_failed')
        a, b = L._restart_owed(W, self.ws, alpha), L._restart_owed(W, self.ws, beta)
        self.assertNotEqual(a, b)
        self.assertEqual(json.loads(b.read_text())['reason'], 'launch_failed')
        a.unlink()
        legacy = a.parent / f'{W}.json'
        legacy.write_text(json.dumps({'instance': alpha, 'reason': 'stopped_for_relaunch'}))
        self.assertFalse(L._restart_owed(W, self.ws, beta).name == legacy.name)
        self.assertTrue(legacy.exists(), 'another lane leaves it')
        self.assertTrue(L._restart_owed(W, self.ws, alpha).exists())
        self.assertFalse(legacy.exists())


class ActingIdentity(Base):
    def rows(self):
        return C._read(C.record_path(self.ws))

    def test_the_task_agent_decides(self):
        row = C.acting_identity(self.rows(), '!r:beta.example', W, self.ws, '@worker:beta.example')
        self.assertEqual(row['instance'], self.lanes['beta.example'])
        with self.assertRaises(C.NoConnectionForRoom):
            C.acting_identity(self.rows(), '!r:beta.example', W, self.ws, '@worker:alpha.example')

    def test_with_no_task_the_rooms_server_picks_the_workers_own_agent(self):
        row = C.acting_identity(self.rows(), '!r:beta.example', W, self.ws)
        self.assertEqual(row['agent_mxid'], '@worker:beta.example')
        with self.assertRaises(C.WorkerRoomAmbiguous):
            C.acting_identity(self.rows(), None, W, self.ws)
        with self.assertRaises(C.NoConnectionForRoom):
            C.acting_identity(self.rows(), '!r:gamma.example', W, self.ws)

    def test_agent_ids_names_the_agent_on_the_primarys_server(self):
        self.assertEqual(C.agent_ids(self.ws)[W], '@worker:alpha.example')

    def test_one_lane_posts_the_workers_cards(self):
        self.assertTrue(L.is_worker_card_lane(self.lanes['alpha.example'], W, self.ws))
        self.assertFalse(L.is_worker_card_lane(self.lanes['beta.example'], W, self.ws))

    def test_a_second_agent_is_refused_on_the_same_server_only(self):
        with self.assertRaises(C.WorkerIdentityExists):
            C.require_no_worker_identity(W, 'beta.example', self.ws)
        C.require_no_worker_identity(W, 'gamma.example', self.ws)


if __name__ == '__main__':
    unittest.main()
