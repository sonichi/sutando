"""Publication task context reaches the existing owner through both real CLIs."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication_transport', ROOT / 'tests/github-status-runtime-cli.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class TaskContextTests(fixture.TransportTests):
    def scoped_status(self, task='fixture-task-a'):
        return self.harness.run_cli('--room', '!isolated:fixture', '--runtime-tool',
                                    str(ROOT / 'src/runtime-cli/sutando-runtime.py'), '--task-id', task)

    def test_supplied_task_reaches_approval_and_durable_execution(self):
        proc = self.scoped_status()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pub = json.loads(proc.stdout)['publication']
        approval = self.srv.store.get(pub['approval_request_id'])
        execution = self.srv.store.get(pub['execution_request_id'])
        self.assertEqual(approval['params']['taskId'], 'fixture-task-a')
        self.assertEqual(execution['params']['taskId'], 'fixture-task-a')
        self.assertEqual(self.sent[0]['taskId'], 'fixture-task-a')
        self.assertEqual(len(self.sent), 1)

    def test_changed_execution_task_refuses_before_consumption(self):
        original = self.srv.dispatcher.handle
        async def changed(method, params):
            if method == 'capability.execute':
                self.approval_id = params['approvalRequestId']
                params = {**params, 'taskId': 'fixture-task-b'}
            return await original(method, params)
        self.srv.dispatcher.handle = changed
        proc = self.scoped_status()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        pub = json.loads(proc.stdout)['publication']
        self.assertEqual(self.sent, [])
        self.assertEqual(pub['state'], 'OUTCOME_UNKNOWN')
        approval = self.srv.store.get(self.approval_id)
        self.assertEqual(approval['params']['taskId'], 'fixture-task-a')
        self.assertFalse(approval['consumedAt'])


if __name__ == '__main__':
    unittest.main()
