#!/usr/bin/env python3
"""Execute offline regressions against intentionally damaged production policies."""
import importlib.util
import io
import json
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceptance', ROOT/'tests/room-history-window.test.py')
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
mutations = [
 ('archive-population-32', 'history', 'dict.fromkeys(room_ids)', 'dict.fromkeys(room_ids[:32])'),
 ('ignore-window-end', 'history', 'since_ms <= ts <= until_ms', 'since_ms <= ts'),
 ('disable-event-dedup', 'history', 'key not in seen', 'True'),
 ('page-budget-is-complete', 'history', '"page_budget_exhausted"', '"server_no_cursor"'),
 ('read-failure-is-quiet', 'history', 'row["errors"].append(type(exc).__name__)', 'row["coverage"] = "server_no_cursor"'),
]
results=[]
original_collect = tests.collect_window
for name, family, old, new in mutations:
 path = ROOT/('skills/review-preflight/scripts/decision_evidence.py' if family=='decision' else 'skills/agent-room-ops/history_collection.py')
 source = path.read_text()
 if old not in source:
  raise RuntimeError('Mutation target absent: '+name)
 module=types.ModuleType(name)
 exec(compile(source.replace(old,new,1),str(path),'exec'),module.__dict__)
 tests.collect_window = module.collect_window if family=='history' else original_collect
 suite=unittest.defaultTestLoader.loadTestsFromModule(tests)
 result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
 results.append({'mutation':name,'killed':not result.wasSuccessful(),'failing_tests':[str(t) for t,_ in result.failures+result.errors]})
tests.collect_window=original_collect
print(json.dumps({'mutations':results,'killed':sum(r['killed'] for r in results),'total':len(results)},indent=2))
raise SystemExit(0 if all(r['killed'] for r in results) else 1)
