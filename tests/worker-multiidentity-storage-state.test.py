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


class FakeVault:
    """No keychain on the test host: the record, not the vault, is under test."""
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


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
        vault = FakeVault()
        removed = C.retire_worker_identity(W, workspace=self.ws, vault=vault, kill=lambda *a, **k: None)
        self.assertEqual(len(removed), 2)
        self.assertEqual(sorted(vault.deleted), sorted(C.token_ref_for(i) for i in self.lanes.values()))
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

    def test_a_room_addressed_card_is_its_rooms_servers_lane_alone(self):
        alpha, beta = self.lanes['alpha.example'], self.lanes['beta.example']
        for room, poster in (('!dm:beta.example', beta), ('!dm:alpha.example', alpha), ('', alpha)):
            with self.subTest(room=room):
                self.assertEqual([i for i in (alpha, beta) if L.is_worker_card_lane(i, W, self.ws, room)], [poster])

    def test_a_second_agent_is_refused_on_the_same_server_only(self):
        with self.assertRaises(C.WorkerIdentityExists):
            C.require_no_worker_identity(W, 'beta.example', self.ws)
        C.require_no_worker_identity(W, 'gamma.example', self.ws)


class NoneOnThePrimary(Base):
    """Two serving identities, neither on the core's primary server (gamma)."""

    def setUp(self):
        super().setUp()
        path = C.record_path(self.ws)
        data = json.loads(path.read_text())
        for row in data['connections']:
            row['primary'] = False
        data['connections'].append(dict(server='gamma.example', gateway_url='https://gamma.example',
                                        added_by='owner', added_at='2026-11-01T00:00:00Z', verified=True,
                                        server_source='identity', instance='gamma-example',
                                        token_ref=C.token_ref_for('gamma-example'), primary=True,
                                        agent_mxid='@core:gamma.example'))
        path.write_text(json.dumps(data))
        self.both = (self.lanes['alpha.example'], self.lanes['beta.example'])

    def posters(self, room):
        return [i for i in self.both if L.is_worker_card_lane(i, W, self.ws, room)]

    def test_the_card_is_posted_exactly_once_by_the_lane_its_destination_names(self):
        self.assertIsNone(L.primary_identity(W, self.ws))
        self.assertEqual(self.posters('!dm:beta.example'), [self.lanes['beta.example']])
        self.assertEqual(self.posters('!dm:alpha.example'), [self.lanes['alpha.example']])
        self.assertEqual(len(self.posters('')), 1, "the owner's DM: one lane, never none")
        self.assertEqual(L.card_lane(W, '', self.ws)['instance'], min(self.both))

    def test_a_destination_no_lane_reaches_has_no_poster_and_is_refused_at_the_ask(self):
        import importlib.util
        self.assertIsNone(L.card_lane(W, '!dm:gamma.example', self.ws))
        self.assertEqual(self.posters('!dm:gamma.example'), [])
        spec = importlib.util.spec_from_file_location(
            'pq_ask', ROOT / 'skills/pending-questions/scripts/pending_questions_ask.py')
        sys.path.insert(0, str(ROOT / 'skills/pending-questions/scripts'))
        ask = sys.modules['pq_ask'] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ask)
        self.assertFalse(ask.card_has_lane(W, '!dm:gamma.example', self.ws))
        self.assertTrue(ask.card_has_lane(W, '!dm:beta.example', self.ws))
        self.assertTrue(ask.card_has_lane(W, '', self.ws))


def _load_ask():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'pq_ask', ROOT / 'skills/pending-questions/scripts/pending_questions_ask.py')
    sys.path.insert(0, str(ROOT / 'skills/pending-questions/scripts'))
    ask = sys.modules['pq_ask'] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ask)
    return ask


class OneActiveOneDisabled(Base):
    """The worker's identity on beta is disabled, so alpha's lane is its only serving one."""

    def setUp(self):
        super().setUp()
        path = C.record_path(self.ws)
        data = json.loads(path.read_text())
        for row in data['connections']:
            if row['instance'] == self.lanes['beta.example']:
                row['disabled'] = True
        path.write_text(json.dumps(data))
        self.alpha, self.beta = self.lanes['alpha.example'], self.lanes['beta.example']

    def test_a_card_bound_for_the_disabled_server_is_neither_staged_nor_claimed(self):
        self.assertIsNone(L.card_lane(W, '!dm:beta.example', self.ws))
        self.assertFalse(L.is_worker_card_lane(self.alpha, W, self.ws, '!dm:beta.example'))
        self.assertFalse(L.is_worker_card_lane(self.beta, W, self.ws, '!dm:beta.example'))
        self.assertFalse(_load_ask().card_has_lane(W, '!dm:beta.example', self.ws))

    def test_a_card_bound_for_the_active_server_is_claimed_once_by_its_lane(self):
        for room in ('!dm:alpha.example', ''):
            with self.subTest(room=room):
                self.assertEqual([i for i in (self.alpha, self.beta)
                                  if L.is_worker_card_lane(i, W, self.ws, room)], [self.alpha])
                self.assertTrue(_load_ask().card_has_lane(W, room, self.ws))


class NoServingLane(Base):
    def test_no_lane_stages_or_claims_any_card(self):
        path = C.record_path(self.ws)
        data = json.loads(path.read_text())
        for row in data['connections']:
            if row.get('worker'):
                row['disabled'] = True
        path.write_text(json.dumps(data))
        ask = _load_ask()
        for room in ('!dm:alpha.example', '!dm:beta.example', ''):
            with self.subTest(room=room):
                self.assertIsNone(L.card_lane(W, room, self.ws))
                self.assertFalse(any(L.is_worker_card_lane(i, W, self.ws, room) for i in self.lanes.values()))
                self.assertFalse(ask.card_has_lane(W, room, self.ws))


if __name__ == '__main__':
    unittest.main()
