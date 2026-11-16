"""Terminal holders acquire folded origins through the real media route."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from activity_rows import append, task_from_file
from activity_projection import project_activity_media
from dedup_recovery import plan_dedup_recovery
from local_task_protocol import serialize_task_last

spec = importlib.util.spec_from_file_location('activity_origins_api', ROOT / 'src/agent-api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class Projection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ws = Path(temp.name).resolve()
        for name in ('tasks', 'results', 'state'):
            (self.ws / name).mkdir()
        self.patch = patch.object(api, 'WORKSPACE_DIR', self.ws)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = http.server.HTTPServer(('127.0.0.1', 0), api.Handler)
        self.server.timeout = 0.1
        self.addCleanup(self.server.server_close)

    def get(self, name):
        result = {}
        def request():
            try:
                url = f'http://127.0.0.1:{self.server.server_port}/media/{name}'
                with urllib.request.urlopen(url, timeout=5) as response:
                    result.update(body=response.read(), cache=response.headers['Cache-Control'])
            except Exception as exc:
                result['error'] = exc
        thread = threading.Thread(target=request)
        thread.start()
        while thread.is_alive():
            self.server.handle_request()
        thread.join()
        if 'error' in result:
            raise result['error']
        return result

    def test_accepted_fold_projects_terminal_holder_without_new_activity(self):
        for name, event in [('task-holder', '$first'), ('task-followup', '$second')]:
            text = serialize_task_last([('id', name), ('source', 'gateway'),
                                       ('channel_id', 'room'), ('user_id', 'owner'),
                                       ('source_message_id', event)], 'hello')
            (self.ws / 'tasks' / f'{name}.txt').write_text(text)
        task, _ = task_from_file(self.ws / 'tasks/task-holder.txt')
        append('replied', kind='done', done=True, room='room', task=task, workspace=self.ws)
        (self.ws / 'results/task-holder.txt').write_text('answered')
        live = self.ws / 'state/agent-activity.jsonl'
        for name in ('agent-activity.archive.jsonl', 'agent-activity.archive.2026-11-01.jsonl'):
            (self.ws / 'state' / name).write_bytes(live.read_bytes())
        paths = sorted((self.ws / 'state').glob('*.jsonl'))
        original = {p: p.read_bytes() for p in paths}
        before = {p: json.loads(self.get('state/' + p.name)['body']) for p in paths}
        action, _ = plan_dedup_recovery(self.ws / 'results', self.ws / 'tasks',
                                        'task-followup', 'task-holder', 'room', 'task-reask',
                                        holder_delivery_state=lambda _: 'accepted')
        self.assertEqual(action, 'honour')
        for path in paths:
            response = self.get('state/' + path.name)
            actual = json.loads(response['body'])
            expected = before[path]
            expected['task']['origins'] = ['$first', '$second']
            self.assertEqual(actual, expected)
            self.assertEqual(response['cache'], 'no-store')
            self.assertEqual(path.read_bytes(), original[path])
        self.assertTrue(json.loads(live.read_bytes())['done'])

    def test_generic_media_and_unusual_rows_are_preserved(self):
        body = b'bad json\n[]\n42\n{"task":null}\n{"task":{"id":[]}}\n'
        path = self.ws / 'state/agent-activity.jsonl'
        path.write_bytes(body)
        self.assertEqual(self.get('state/' + path.name)['body'], body)
        other = self.ws / 'state/agent-activity.other.jsonl'
        other.write_bytes(body)
        self.assertIsNone(project_activity_media(other, self.ws))
        response = self.get('state/' + other.name)
        self.assertEqual(response, dict(body=body, cache='public, max-age=300'))
        outside = self.ws / 'agent-activity.jsonl'
        outside.write_bytes(body)
        self.assertIsNone(project_activity_media(outside, self.ws))


if __name__ == '__main__':
    unittest.main()
