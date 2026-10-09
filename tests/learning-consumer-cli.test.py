import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'skills/learning-window/scripts/dispatch_collection.py'


class ConsumerCLITests(unittest.TestCase):
    def test_detached_consumer_does_not_inherit_parent_inbox_role(self):
        self.consumer.write_text("import os\nassert os.environ['SUTANDO_CORE_SESSION']=='0'\nassert 'SUTANDO_INSTANCE_ID' not in os.environ\nassert os.environ['FIXTURE_ADAPTER_VALUE']=='preserved'\n" + self.consumer.read_text())
        with patch.dict(os.environ, {'SUTANDO_CORE_SESSION':'1', 'SUTANDO_INSTANCE_ID':'parent-worker', 'FIXTURE_ADAPTER_VALUE':'preserved'}):
            result,state = self.call('valid')
            self.assertEqual(os.environ['SUTANDO_CORE_SESSION'], '1')
            self.assertEqual(os.environ['SUTANDO_INSTANCE_ID'], 'parent-worker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['consumer_returncode'], 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / 'state'
        self.cap = self.root / 'cap.py'; self.consumer = self.root / 'consumer.py'
        self.cap.write_text('''import sys,json,datetime
scope=sys.argv[1];args=sys.argv[2:];assert args.pop(0)=='--strict';command=args.pop(0)
if command=='rooms': value={'ok':True,'rooms':['r']}
else:
 read=lambda k:datetime.datetime.fromisoformat(args[args.index(k)+1]).timestamp()*1000
 start,end=read('--since'),read('--until')
 value={'ok':True,'scope':scope,'membership_count':1,'since_ms':start,'until_ms':end,
 'rooms':[{'room_id':'r','coverage':'reached_cutoff','errors':[], 'messages':[{'event_id':'e','ts':end-1,'body':'Asked to test'}]}]}
print(json.dumps(value))
''')
        self.consumer.write_text('''import sys,json,pathlib,hashlib,time
mode,prompt=sys.argv[1:3]
contract=json.loads(next(l[26:] for l in prompt.splitlines() if l.startswith('Proposal return contract: ')))
output=pathlib.Path(contract['path'])
if mode=='missing': sys.exit(0)
if mode=='timeout': time.sleep(1);sys.exit(0)
if mode=='invalid': output.write_text(json.dumps({'success':True}));sys.exit(0)
assert contract['additional_proposal_fields'] is False
assert 'hostname' in contract['scope_meaning']
receipt=next(p for p in (output.parent.parent/'receipts').glob('*.json') if json.loads(p.read_text())['scope']=='prod')
digest=hashlib.sha256(receipt.read_bytes()).hexdigest()
scope=next(r['scope'] for r in contract['receipts'] if r['receipt_digest']==digest)
row={'person_key':'person','scope':scope,'receipt_digest':digest,
 'text':'Candidate from fixture','references':[{'room_id':'r','event_id':'e','excerpt':'Asked to test'}]}
output.write_text(json.dumps({'schema':1,'proposals':[row]}))
''')

    def call(self, mode):
        config = {'consumer_argv': [sys.executable, str(self.consumer), mode], 'consumer_prompt': 'Fixture only',
                  'bootstrap_ms': 0, 'capabilities': {s: [sys.executable, str(self.cap), s] for s in ['prod','dev']},
                  'proposal_stores': {'person': 'adapter-id'}, 'consumer_timeout': 0.1 if mode=='timeout' else 3}
        if hasattr(self, 'checker'):
            config['proposal_check_argv'] = self.checker
        if hasattr(self, 'readback'):
            config['document_readback_argv'] = [sys.executable, str(self.readback)]
        path = self.root / 'manifest.json';path.write_text(json.dumps({'config':config}))
        result = subprocess.run([sys.executable,str(ENTRY),'--config',str(path),'--directory',str(self.state)],
                                capture_output=True,text=True,timeout=10)
        state=json.loads((self.state/'dispatch-state.json').read_text())
        return result,state

    def test_timeout_stops_descendant_before_readback_and_next_attempt(self):
        import signal
        import time
        child_pid = self.root / 'child.pid'
        late = self.root / 'late-write'
        child = self.root / 'child.py'
        child.write_text("import signal,time,pathlib\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(1)\npathlib.Path(" + repr(str(late)) + ").write_text('late')\ntime.sleep(20)\n")
        self.consumer.write_text("import subprocess,sys,pathlib,time\np=subprocess.Popen([sys.executable," + repr(str(child)) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\npathlib.Path(" + repr(str(child_pid)) + ").write_text(str(p.pid))\ntime.sleep(20)\n")
        self.configure_readback('same')
        result, state = self.call('timeout')
        pid = int(child_pid.read_text())
        def cleanup():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state['error'], 'TimeoutExpired')
        self.assertIn('document_readbacks_after', state)
        self.consumer.write_text('import sys;sys.exit(0)')
        _, next_state = self.call('missing')
        self.assertEqual(next_state['phase'], 'consumer_exited')
        time.sleep(1.2)
        self.assertFalse(late.exists(), 'timed-out descendant wrote after readback and next dispatch')

    def test_dispatcher_cancellation_stops_child_before_failed_readback(self):
        import signal
        import time
        self.call('missing')
        self.configure_readback('same')
        manifest = self.root / 'manifest.json'
        value = json.loads(manifest.read_text())
        value['config']['document_readback_argv'] = [sys.executable, str(self.readback)]
        value['config']['consumer_timeout'] = 20
        manifest.write_text(json.dumps(value))
        pidfile = self.root / 'cancel-child.pid'
        late = self.root / 'cancel-late'
        child = "import pathlib,time;time.sleep(1);pathlib.Path(" + repr(str(late)) + ").write_text('late');time.sleep(20)"
        self.consumer.write_text("import subprocess,sys,pathlib,time\np=subprocess.Popen([sys.executable,'-c'," + repr(child) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\npathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid))\ntime.sleep(20)")
        process = subprocess.Popen([sys.executable, str(ENTRY), '--config', str(manifest), '--directory', str(self.state)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        until = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists())
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 1)
        state = json.loads((self.state / 'dispatch-state.json').read_text())
        self.assertEqual(state['error'], 'InterruptedError')
        self.assertIn('document_readbacks_after', state)
        time.sleep(1.2)
        self.assertFalse(late.exists())

    def test_cancellation_during_process_creation_is_not_lost(self):
        import signal
        import time
        self.call('missing')
        self.configure_readback('same')
        manifest = self.root / 'manifest.json'
        value = json.loads(manifest.read_text())
        value['config']['document_readback_argv'] = [sys.executable, str(self.readback)]
        manifest.write_text(json.dumps(value))
        late = self.root / 'creation-late'
        pidfile = self.root / 'creation.pid'
        self.consumer.write_text("import pathlib,time;time.sleep(1);pathlib.Path(" + repr(str(late)) + ").write_text('late');time.sleep(20)")
        wrapper = self.root / 'cancel-during-popen.py'
        wrapper.write_text("import os,signal,sys,pathlib\nsys.path.insert(0," + repr(str(ENTRY.parent)) + ")\nimport dispatch_collection as dispatch\noriginal=dispatch.subprocess.Popen\ndef create(*args,**kwargs):\n p=original(*args,**kwargs)\n if kwargs.get('start_new_session'):\n  pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid))\n  os.kill(os.getpid(),signal.SIGTERM)\n return p\ndispatch.subprocess.Popen=create\nsys.exit(dispatch.main())\n")
        result = subprocess.run([sys.executable, str(wrapper), '--config', str(manifest),
                                 '--directory', str(self.state)], capture_output=True, text=True, timeout=10)
        pid = int(pidfile.read_text())
        def cleanup():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        state = json.loads((self.state / 'dispatch-state.json').read_text())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(state['error'], 'InterruptedError')
        self.assertIn('document_readbacks_after', state)
        time.sleep(1.2)
        self.assertFalse(late.exists(), 'cancellation during Popen allowed a late write')
        self.consumer.write_text('import sys;sys.exit(0)')
        _, next_state = self.call('missing')
        self.assertEqual(next_state['phase'], 'consumer_exited')

    def test_exited_consumer_cannot_leave_background_writer(self):
        import time
        late = self.root / 'exit-late'
        child = "import pathlib,time;time.sleep(1);pathlib.Path(" + repr(str(late)) + ").write_text('late')"
        self.consumer.write_text("import subprocess,sys\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)")
        result, state = self.call('missing')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(state['phase'], 'consumer_exited')
        time.sleep(1.2)
        self.assertFalse(late.exists())

    def test_optional_launcher_uses_resolved_python(self):
        import time
        self.call('valid')
        (self.state / 'dispatch-state.json').unlink()
        stub_dir = self.root / 'bin'
        stub_dir.mkdir()
        stub = stub_dir / 'python3'
        stub.write_text('#!/bin/sh\necho developer-tools-stub >&2\nexit 71\n')
        stub.chmod(0o755)
        log = self.root / 'launch.log'
        env = dict(os.environ, SUTANDO_PY=sys.executable, PATH=str(stub_dir) + os.pathsep + os.environ['PATH'])
        run = subprocess.run(['bash', str(ENTRY.with_name('launch.sh')), str(self.root / 'manifest.json'),
                              str(self.state), str(log)], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        until = time.monotonic() + 5
        state = {}
        while time.monotonic() < until:
            try:
                state = json.loads((self.state / 'dispatch-state.json').read_text())
            except FileNotFoundError:
                pass
            if state.get('phase') in ('consumer_exited', 'failed'):
                break
            time.sleep(0.02)
        self.assertEqual(state.get('phase'), 'consumer_exited', log.read_text())
        self.assertEqual(state['proposal_return']['proposal_return'], 'persisted')
        self.assertNotIn('developer-tools-stub', log.read_text())

    def test_direct_dispatch_entry_matches_cli_for_valid_and_timeout_readbacks(self):
        import contextlib
        import io
        sys.path.insert(0, str(ENTRY.parent))
        import dispatch_collection
        self.checker = [sys.executable, str(ENTRY.with_name('check_return.py'))]
        self.configure_readback('same')
        for mode in ('valid', 'timeout'):
            _, cli = self.call(mode)
            output = io.StringIO()
            with patch.object(sys, 'argv', [str(ENTRY), '--config', str(self.root / 'manifest.json'),
                                          '--directory', str(self.state)]), contextlib.redirect_stdout(output):
                code = dispatch_collection.main()
            direct = json.loads(output.getvalue())
            self.assertEqual(direct['phase'], cli['phase'])
            self.assertEqual(code, 0 if mode == 'valid' else 1)
            self.assertEqual(direct['document_retention']['person']['physical_retention'], 'unchanged')
            self.assertEqual(direct['learning_outcome'], 'unverified')
        (self.root / 'manifest.json').write_text('{}')
        with patch.object(sys, 'argv', [str(ENTRY), '--config', str(self.root / 'manifest.json'), '--directory', str(self.state)]):
            with self.assertRaises(ValueError):
                dispatch_collection.main()

    def test_actual_cli_checker_repairs_quote_before_real_pending_writer(self):
        self.checker = [sys.executable, str(ENTRY.with_name('check_return.py'))]
        self.consumer.write_text(self.consumer.read_text() + """
import subprocess,os
ctx=pathlib.Path(contract['preflight_argv'][-1])
assert ctx.exists() and os.stat(ctx).st_mode & 0o777 == 0o600
assert json.loads(ctx.read_text())['output_path']==str(output)
assert not (output.parent.parent/'pending-facts').exists()
row['references'][0]['excerpt']='Asked ... test'
output.write_text(json.dumps({'schema':1,'proposals':[row]}))
result=subprocess.run(contract['preflight_argv'],capture_output=True,text=True)
assert result.returncode==2 and json.loads(result.stdout)['rejected'][0]['index']==0
assert not (output.parent.parent/'pending-facts').exists()
row['references'][0]['excerpt']='Asked to test'
output.write_text(json.dumps({'schema':1,'proposals':[row]}))
result=subprocess.run(contract['preflight_argv'],capture_output=True,text=True)
assert result.returncode==0 and json.loads(result.stdout)['valid_indexes']==[0]
assert not (output.parent.parent/'pending-facts').exists()
""")
        result, state = self.call('valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['proposal_return']['proposal_return'], 'persisted')
        self.assertEqual(len(state['proposal_return']['accepted_candidate_ids']), 1)
        self.assertEqual(state['learning_outcome'], 'unverified')

    def test_actual_cli_context_is_fresh_and_missing_output_never_reuses_prior(self):
        self.checker = [sys.executable, str(ENTRY.with_name('check_return.py'))]
        _, first = self.call('valid')
        _, second = self.call('missing')
        self.assertNotEqual(first['proposal_context_path'], second['proposal_context_path'])
        context = json.loads(Path(second['proposal_context_path']).read_text())
        self.assertEqual(context['output_path'], second['proposal_output_path'])
        result = subprocess.run(self.checker + ['--context', second['proposal_context_path']], capture_output=True,text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['proposal_validation'], 'unknown')
        self.assertEqual(second['proposal_return']['proposal_return'], 'unknown')

    def configure_readback(self, mode):
        self.readback = self.root / 'readback.py'
        doc = "('aaaa' if n==0 else 'bbbb')" if mode == 'changed' else "'aaaa'"
        code = "import pathlib,json,sys\n"
        code += "p=pathlib.Path(__file__).with_suffix('.count');n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))\n"
        code += "sys.exit(1)\n" if mode == 'failed' else ("print(json.dumps({'ok':True,'body':{'person':{'id':" +
                 repr('wrong' if mode == 'cross' else 'adapter-id') + ",'slug':'person','doc':" + doc + "}}}))\n")
        self.readback.write_text(code)

    def test_actual_cli_readback_persisted_before_consumer_and_pending_survives(self):
        self.configure_readback('same')
        self.consumer.write_text("import json,pathlib\nstate=json.loads(pathlib.Path(" + repr(str(self.state / 'dispatch-state.json')) + ").read_text())\nassert state['document_readbacks_before']['person']['readback']=='verified'\n" + self.consumer.read_text())
        result, state = self.call('valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['document_retention']['person']['physical_retention'], 'unchanged')
        self.assertEqual(state['learning_outcome'], 'unverified')
        self.assertNotIn('document', state['document_readbacks_before']['person'])
        pending = json.loads((self.state / 'pending-facts/pending-candidates.json').read_text())
        self.assertTrue(all(row['status'] == 'pending' for row in pending['candidates'].values()))

    def test_actual_cli_equal_length_mutation_is_unattributed_change(self):
        self.configure_readback('changed')
        _, state = self.call('valid')
        self.assertEqual(state['document_retention']['person']['physical_retention'], 'changed_unattributed')
        self.assertEqual(state['document_readbacks_before']['person']['document_codepoints'], state['document_readbacks_after']['person']['document_codepoints'])

    def test_actual_cli_unknown_cross_identity_does_not_become_empty_or_success(self):
        for mode in ['failed', 'cross']:
            self.configure_readback(mode)
            _, state = self.call('valid')
            self.assertEqual(state['document_retention']['person']['physical_retention'], 'unknown')
            self.assertEqual(state['proposal_return']['proposal_return'], 'persisted')

    def test_actual_cli_timeout_still_captures_after_without_learning_credit(self):
        self.configure_readback('same')
        result, state = self.call('timeout')
        self.assertEqual(result.returncode, 1)
        self.assertIn('document_readbacks_after', state)
        self.assertEqual(state['learning_outcome'], 'unverified')
        self.assertEqual(self.readback.with_suffix('.count').read_text(), '2')

    def test_actual_cli_persists_pending_with_real_production_writer(self):
        result,state=self.call('valid')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(state['proposal_return']['proposal_return'],'persisted')
        self.assertEqual(state['learning_outcome'],'unverified')
        pending=json.loads((self.state/'pending-facts/pending-candidates.json').read_text())
        self.assertEqual(len(pending['candidates']),1)
        row=next(iter(pending['candidates'].values()))
        self.assertEqual(row['store_identity'],'adapter-id')
        self.assertEqual(row['status'],'pending')
        self.assertEqual(len(list((self.state/'receipts').glob('*.json'))),2)

    def test_actual_cli_missing_or_claimed_success_return_stays_unknown(self):
        for mode in ['missing','invalid']:
            result,state=self.call(mode)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(state['proposal_return']['proposal_return'],'unknown')
            self.assertEqual(state['learning_outcome'],'unverified')
        self.assertFalse((self.state/'pending-facts').exists())

    def test_actual_cli_partial_collection_reports_unread_and_preserves_prior_pending(self):
        calls = self.root / 'consumer-calls'
        self.consumer.write_text("from pathlib import Path\nwith Path(" + repr(str(calls)) + ").open('a') as f: f.write('called\\n')\n" + self.consumer.read_text())
        first, _ = self.call('valid')
        self.assertEqual(first.returncode, 0)
        pending = self.state / 'pending-facts/pending-candidates.json'
        before = pending.read_bytes()
        self.cap.write_text(self.cap.read_text().replace("print(json.dumps(value))", "if command=='history' and scope=='prod': value['rooms'][0].update(errors=['TimeoutError'],coverage='read_failed')\nprint(json.dumps(value))"))
        result, state = self.call('valid')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state['phase'], 'collection_incomplete')
        self.assertFalse(state['consumer_started'])
        self.assertNotIn('consumer_attempted', state)
        unread = [r for r in state['collection_summaries'] if r['unread_rooms']]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]['unread_rooms'], ['r'])
        self.assertEqual(unread[0]['scope'], 'prod')
        self.assertEqual(pending.read_bytes(), before)
        self.assertEqual(calls.read_text().splitlines(), ['called'])
        self.assertEqual(len(list((self.state / 'receipts').glob('*.json'))), 4)

    def test_actual_cli_failure_stage_retained_without_consumer_or_pending_mutation(self):
        first, _ = self.call('valid')
        self.assertEqual(first.returncode, 0)
        pending = self.state / 'pending-facts/pending-candidates.json'
        before = pending.read_bytes()
        self.cap.write_text(self.cap.read_text().replace("print(json.dumps(value))", "if command=='history' and scope=='dev': value['until_ms'] += 1\nprint(json.dumps(value))"))
        self.consumer.write_text("raise AssertionError('consumer must not run')\n")
        result, state = self.call('valid')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state['collection']['errors'], {'dev': 'ValueError'})
        self.assertEqual(state['collection']['error_stages'], {'dev': 'window_validation'})
        self.assertFalse(state['consumer_started'])
        self.assertEqual(pending.read_bytes(), before)
        self.assertEqual(len(state['collection_summaries']), 3)

    def test_actual_cli_timeout_retains_receipts_without_success(self):
        result,state=self.call('timeout')
        self.assertEqual(result.returncode,1)
        self.assertEqual(state['phase'],'failed')
        self.assertEqual(state['learning_outcome'],'unverified')
        self.assertEqual(len(list((self.state/'receipts').glob('*.json'))),2)

    def test_actual_cli_fresh_output_paths_do_not_reuse_prior_valid_return(self):
        _,first=self.call('valid');_,second=self.call('missing')
        self.assertNotEqual(first['proposal_output_path'],second['proposal_output_path'])
        self.assertEqual(second['proposal_return']['proposal_return'],'unknown')
        self.assertTrue(Path(first['proposal_output_path']).exists())


if __name__=='__main__':unittest.main()
