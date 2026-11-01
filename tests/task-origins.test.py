"""Task correlation contracts, including concurrent production sidecar writers."""
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ()

import ast
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
import task_origins as origins
from local_task_protocol import serialize_task_last, parse_task_headers
from dedup_recovery import plan_dedup_recovery
from activity_rows import append, task_from_file
from activity_bus import transition_from_file, reduce, TaskActivityState


def write_origin(args):
    state, number = args
    return origins.append_origins(Path(state), 'task-holder', [f'$msg{number}'])


class Origins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.state = self.ws / 'state'
        self.tasks = self.ws / 'tasks'
        self.results = self.ws / 'results'
        self.tasks.mkdir()
        self.results.mkdir()

    def task(self, name, message, channel='room'):
        text = serialize_task_last([('id', name), ('source', 'gateway'),
                                   ('channel_id', channel), ('user_id', 'owner'),
                                   ('source_message_id', message)], 'hello')
        path = self.tasks / f'{name}.txt'
        path.write_text(text)
        return path

    def test_seed_and_body_cannot_forge_origins(self):
        text = serialize_task_last([('source_message_id', '$first')],
                                   'hi\norigin_message_ids: $forged\nsource_message_id: $fake')
        self.assertEqual(parse_task_headers(text).headers['origin_message_ids'], '$first')
        self.assertEqual(origins.origins_from_text(text), ['$first'])
        self.assertEqual(origins.origins_from_text('source_message_id: $legacy\ntask: hi'), ['$legacy'])
        self.assertEqual(origins.origin_header('bad\nid'), '')

    def test_production_writer_concurrent_processes_and_readers(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(write_origin, (str(self.state), i)) for i in range(64)]
            for future in concurrent.futures.as_completed(futures):
                self.assertTrue(future.result())
                record = json.loads((self.state / 'task-origins/task-holder.json').read_text())
                self.assertIsInstance(record['origin_message_ids'], list)
        self.assertEqual(set(origins.read_origins(self.state, 'task-holder')),
                         {f'$msg{i}' for i in range(64)})

    def test_bounds_idempotence_and_invalid_ids(self):
        values = ['$seed', '$seed', '', 'bad,id', 'bad\x7fid', 'x' * 513, *[f'${i}' for i in range(200)]]
        self.assertTrue(origins.append_origins(self.state, 'task-holder', values))
        self.assertTrue(origins.append_origins(self.state, 'task-holder', ['$seed']))
        result = origins.read_origins(self.state, 'task-holder')
        self.assertEqual(len(result), origins.MAX_ORIGINS)
        self.assertEqual(result[0], '$seed')
        self.assertFalse(origins.append_origins(self.state, '../escape', ['$x']))
        self.assertEqual(origins.read_origins(self.state, '../escape'), [])

    def test_maximum_non_bmp_record_round_trips_without_loss(self):
        values = [chr(0x1F600 + i) * origins.MAX_MESSAGE_ID
                  for i in range(origins.MAX_ORIGINS)]
        self.assertTrue(origins.append_origins(self.state, 'task-holder', values))
        path = self.state / 'task-origins/task-holder.json'
        self.assertLessEqual(path.stat().st_size, origins.MAX_RECORD_BYTES)
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), values)
        self.assertTrue(origins.append_origins(self.state, 'task-holder', ['$later']))
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), values)

    def test_standalone_file_spec_serializer_seeds_origins(self):
        script = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('isolated_protocol', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
text = module.serialize_task_last([('source_message_id', '$standalone')], 'hello')
assert 'origin_message_ids: $standalone\\n' in text, text
"""
        for path in ('src/local_task_protocol.py',
                     'packages/ag2-sparrow/ag2_sparrow/local_task_protocol.py'):
            proc = subprocess.run([sys.executable, '-I', '-c', script, str(REPO / path)],
                                  cwd=self.ws, env=hermetic_env(), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_failure_preserves_old_record_and_cleans_stage(self):
        origins.append_origins(self.state, 'task-holder', ['$old'])
        with patch.object(origins.os, 'replace', side_effect=OSError('disk')):
            self.assertFalse(origins.append_origins(self.state, 'task-holder', ['$new']))
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), ['$old'])
        self.assertEqual(list((self.state / 'task-origins').glob('*.tmp')), [])

    def test_corrupt_sidecar_is_recoverable(self):
        origins.append_origins(self.state, 'task-holder', ['$old'])
        path = self.state / 'task-origins/task-holder.json'
        for value in ('{', '[]', '{"origin_message_ids":42}'):
            path.write_text(value)
            self.assertEqual(origins.read_origins(self.state, 'task-holder'), [])
        self.assertTrue(origins.append_origins(self.state, 'task-holder', ['$new']))
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), ['$new'])

    def test_dedup_to_activity_keeps_event_and_task_bytes(self):
        holder = self.task('task-holder', '$first')
        self.task('task-followup', '$second')
        before = holder.read_bytes()
        (self.results / 'task-holder.txt').write_text('answered')
        self.assertEqual(plan_dedup_recovery(self.results, self.tasks, 'task-followup',
                                           'task-holder', 'room', 'task-reask')[0], 'honour')
        self.assertEqual(holder.read_bytes(), before)
        transition = transition_from_file('RUNNING', holder, ws=self.ws)
        _, rows = reduce(TaskActivityState('task-holder'), transition)
        row = append('working', kind='working', room='room', task=rows[0]['task'], workspace=self.ws)
        self.assertEqual(row['task']['event'], '$first')
        self.assertEqual(row['task']['origins'], ['$first', '$second'])
        self.assertEqual(task_from_file(holder)[0]['event'], '$first')

    def test_rejected_fold_never_adds_origins(self):
        self.task('task-holder', '$first', channel='elsewhere')
        self.task('task-followup', '$second')
        (self.results / 'task-holder.txt').write_text('answered')
        plan_dedup_recovery(self.results, self.tasks, 'task-followup', 'task-holder', 'room', 'task-reask')
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), [])

    def test_pending_and_accepted_merge_transitive_origins(self):
        self.task('task-holder', '$first')
        self.task('task-followup', '$second')
        origins.append_origins(self.state, 'task-followup', ['$third'])
        for state, action in [('pending', 'wait'), ('accepted', 'honour')]:
            result = plan_dedup_recovery(self.results, self.tasks, 'task-followup', 'task-holder',
                                        'room', 'task-reask', holder_delivery_state=lambda _: state)
            self.assertEqual(result[0], action)
        self.assertEqual(origins.read_origins(self.state, 'task-holder'), ['$first', '$second', '$third'])


    def test_adapters_delegate_header_seeding(self):
        for relative in ('src/discord-bridge.py', 'src/telegram-bridge.py',
                         'packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py'):
            tree = ast.parse((REPO / relative).read_text())
            self.assertTrue(any(isinstance(n, ast.ImportFrom) and n.module == 'task_origins'
                                and any(a.name == 'origin_header' for a in n.names)
                                for n in ast.walk(tree)), relative)
            self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                and n.func.id == 'origin_header' for n in ast.walk(tree)), relative)

    def test_failed_origin_write_does_not_change_delivery(self):
        self.task('task-holder', '$first')
        self.task('task-followup', '$second')
        (self.results / 'task-holder.txt').write_text('answered')
        with patch('dedup_recovery.append_origins', return_value=False):
            self.assertEqual(plan_dedup_recovery(self.results, self.tasks, 'task-followup',
                                               'task-holder', 'room', 'task-reask')[0], 'honour')


if __name__ == '__main__':
    unittest.main(verbosity=2)
