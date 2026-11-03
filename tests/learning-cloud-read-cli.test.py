import json
import contextlib
import io
import types
from unittest.mock import patch
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ENTRY = Path(__file__).resolve().parents[1] / 'skills/learning-window/scripts/cloud_person_read.py'


sys.path.insert(0, str(ENTRY.parent))
import cloud_person_read


class CloudReadCLI(unittest.TestCase):
    def call(self, module, key='person'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'src').mkdir(); (root/'src/cloud_auth.py').write_text(module)
            result = subprocess.run([sys.executable,str(ENTRY),'--engine',str(root),'--workspace',str(root),'--',key],
                                  capture_output=True,text=True,timeout=5)
            fake = types.ModuleType('cloud_auth')
            def load(name):
                self.assertEqual(name, 'cloud_auth')
                exec(module, fake.__dict__)
                return fake
            output = io.StringIO()
            with patch.object(cloud_person_read.importlib, 'import_module', side_effect=load), patch.object(sys, 'path', list(sys.path)), patch.object(sys, 'argv',
                    [str(ENTRY), '--engine', str(root), '--workspace', str(root), '--', key]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = cloud_person_read.main()
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, result.returncode)
            if output.getvalue():
                self.assertEqual(json.loads(output.getvalue()), json.loads(result.stdout))
            return result


    def test_actual_cli_delegates_get_auth_workspace_and_timeout(self):
        r=self.call('''def read_cloud_auth(ws):
 assert ws.is_dir()
 return 'https://fixture.invalid','fixture-only'
def cloud_request(base,token,method,path,timeout):
 assert method=='GET' and path=='/api/people/person' and timeout==10
 assert token=='fixture-only'
 return {'person':{'id':'fixture-id','slug':'person','doc':'full'}}
''')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['body']['person']['doc'],'full')
        self.assertNotIn('fixture-only',r.stdout+r.stderr)

    def test_transport_failure_does_not_expose_error_text_or_credentials(self):
        r=self.call('''def read_cloud_auth(ws): return 'https://fixture.invalid','fixture-only'
def cloud_request(*a,**k): raise ValueError('fixture-only private error')
''')
        self.assertEqual(r.returncode,1)
        self.assertEqual(json.loads(r.stdout),{'ok':False,'error_type':'ValueError'})
        self.assertNotIn('fixture-only',r.stdout+r.stderr)

    def test_signed_out_and_invalid_person_key_fail_without_request(self):
        r=self.call('def read_cloud_auth(ws): return None,None\n')
        self.assertEqual(r.returncode,1)
        r=self.call('raise AssertionError("must not import")\n','../other')
        self.assertEqual(r.returncode,2)
        self.assertNotIn('AssertionError',r.stderr)


if __name__=='__main__':unittest.main()
