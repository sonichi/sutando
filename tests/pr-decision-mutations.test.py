#!/usr/bin/env python3
"""Execute offline regressions against intentionally damaged production policies."""
import importlib.util
import io
import json
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceptance', ROOT/'tests/pr-decision-evidence.test.py')
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
mutations = [
 ('ignore-code-owner-rule', 'decision', 'parameters.get("require_code_owner_review") is True', 'False'),
 ('ignore-rule-approval-count', 'decision', 'parameters["required_approving_review_count"] > 0', 'False'),
 ('ignore-decision-metadata-drift', 'decision', 'fingerprint(before) != fingerprint(after)', 'False'),
 ('ignore-expected-head', 'decision', '(expected_head and head != expected_head)', 'False'),
 ('missing-comments-as-empty', 'decision', '    if not isinstance(comments, list)', '    comments = [] if comments is None else comments\n    if not isinstance(comments, list)'),
 ('oldest-comments-instead-of-latest', 'decision', '[-8:]', '[:8]'),
 ('missing-rules-as-empty', 'decision', '    if not isinstance(rules, list)', '    rules = [] if rules is None else rules\n    if not isinstance(rules, list)'),
]
results=[]
original_classify = tests.classify
for name, family, old, new in mutations:
 path = ROOT/('skills/review-preflight/scripts/decision_evidence.py' if family=='decision' else 'skills/agent-room-ops/history_collection.py')
 source = path.read_text()
 if old not in source:
  raise RuntimeError('Mutation target absent: '+name)
 module=types.ModuleType(name)
 exec(compile(source.replace(old,new,1),str(path),'exec'),module.__dict__)
 tests.classify = module.classify if family=='decision' else original_classify
 suite=unittest.defaultTestLoader.loadTestsFromModule(tests)
 result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
 results.append({'mutation':name,'killed':not result.wasSuccessful(),'failing_tests':[str(t) for t,_ in result.failures+result.errors]})
tests.classify=original_classify
print(json.dumps({'mutations':results,'killed':sum(r['killed'] for r in results),'total':len(results)},indent=2))
raise SystemExit(0 if all(r['killed'] for r in results) else 1)
