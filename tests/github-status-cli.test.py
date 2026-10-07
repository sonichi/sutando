"""Offline subprocess publication boundary; fixture capabilities never use network."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills/review-preflight/scripts/github-status.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.fixture = {"mode": "stable", "pr": {"head": {"sha": "abc"}, "base": {"ref": "main"},
                        "state": "open", "merged": False, "merged_at": None, "body": "Current scope",
                        "updated_at": "2026-10-05T06:00:00Z"}}
        gh = self.path / "gh"
        gh.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
root=Path(__file__).parent
fixture=json.loads((root/'fixture.json').read_text());args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[:2]==['pr','checks']: value=[{'name':'tests','bucket':'pass','link':'https://example.test/check'}]
elif args[:2]==['pr','view']: value={'headRefOid':'abc','reviewDecision':'APPROVED','mergeStateStatus':'CLEAN'}
elif args[0]=='api' and '/rules/' in args[1]: value=[{'type':'pull_request','parameters':{'required_approving_review_count':2}}]
elif args[0]=='api' and '/comments?' in args[1]: value=[[]]
elif args==['api','repos/o/r/pulls/1']:
 value=fixture['pr'];n=sum('/pulls/1' in line for line in (root/'calls.jsonl').read_text().splitlines())
 if fixture['mode']=='changed' and n>=5:value={**value,'body':'New owner hold'}
else: raise SystemExit('Unexpected capability call')
print(json.dumps(value))
''')
        gh.chmod(0o700)
        self.tool = self.path / "runtime_tool.py"
        self.tool.write_text('''import json,sys
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
with (root/'runtime-calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[:2]==['approval','request']:
 (root/'approval-effect.json').write_text(json.dumps(args))
 value={'requestId':'fixture-approval','status':'pending'}
elif args[:2]==['request','wait']:value={'requestId':'fixture-approval','status':'approved'}
elif args[:2]==['capability','execute']:
 (root/'publication.json').write_text(json.dumps(args))
 value={'requestId':'fixture-execution','status':'completed','result':{'executed':True,'eventId':'$fixture'}}
else:raise SystemExit('Unexpected runtime call')
print(json.dumps(value))
''')

    def run_cli(self, *args):
        (self.path / "fixture.json").write_text(json.dumps(self.fixture))
        env = {**os.environ, "PATH": str(self.path) + os.pathsep + os.environ["PATH"], **getattr(self, "extra_env", {})}
        return subprocess.run([sys.executable, str(CLI), "o/r", "1", *args],
                              capture_output=True, text=True, env=env, timeout=10)

    def test_default_runs_collectors_without_publication(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["checks_status"], "passed")
        self.assertEqual(result["overall_readiness"], "unknown")
        self.assertFalse((self.path / "publication.json").exists())
        calls = [json.loads(row) for row in (self.path / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(row[0] == "api" or row[:2] in (["pr", "view"], ["pr", "checks"]) for row in calls))

    def test_foreground_open_green_publication_uses_canonical_body(self):
        proc = self.run_cli("--room", "!fixture:example.test", "--runtime-tool", str(self.tool))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = json.loads((self.path / "publication.json").read_text())
        self.assertEqual(args[:2], ["capability", "execute"])
        self.assertEqual(json.loads(args[args.index("--resource")+1]), {"roomId":"!fixture:example.test"})
        approval = json.loads((self.path / "approval-effect.json").read_text())
        self.assertEqual(args[2:8], approval[2:8])
        self.assertIn("Not merged. Required checks: passed. Overall readiness: unknown", json.loads(args[args.index("--input")+1])["body"])
        self.assertEqual(json.loads(proc.stdout)["publication"]["event_id"], "$fixture")

    def test_legacy_direct_tool_is_refused_before_collection(self):
        proc = self.run_cli("--room", "!fixture:example.test", "--room-tool", str(self.tool))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.path / "runtime-calls.jsonl").exists())
        self.assertFalse((self.path / "calls.jsonl").exists())

    def test_stale_head_never_attempts_publication(self):
        proc = self.run_cli("--expect-head", "def", "--room", "!fixture:example.test", "--runtime-tool", str(self.tool))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.path / "publication.json").exists())

    def test_same_head_final_metadata_change_never_attempts_publication(self):
        self.fixture["mode"] = "changed"
        proc = self.run_cli("--room", "!fixture:example.test", "--runtime-tool", str(self.tool))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.path / "publication.json").exists())
        self.assertEqual(json.loads(proc.stdout)["checks_status"], "unknown")

    def test_receipt_replay_and_freeform_publication_flags_are_rejected(self):
        for flag in ("--receipt", "--body"):
            proc = self.run_cli(flag, "Ready to merge")
            self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.path / "calls.jsonl").exists())
        self.assertFalse((self.path / "publication.json").exists())


if __name__ == "__main__":
    unittest.main()
