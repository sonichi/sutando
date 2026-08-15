#!/usr/bin/env python3
"""Unit tests for scripts/review-checks-prose-cap.py. Pins BOTH prose forms:
a checker covering one reports clean on the other's violation."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "rc_prose_cap", REPO / "scripts" / "review-checks-prose-cap.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

CAP, EXTS = 2, [".py"]
passed = failed = 0


def check(name, diff, want):
    global passed, failed
    got = len(pc.violations(diff, CAP, EXTS))
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name}: expected {want} violation(s), got {got}")


D = "+++ b/src/x.py\n@@ -0,0 +1,%d @@\n%s"

check("comment run of 3 fires", D % (4, "+ # a\n+ # b\n+ # c\n+ pass\n"), 1)
check("comment run of 2 is clean", D % (3, "+ # a\n+ # b\n+ pass\n"), 0)
check("docstring of 3 fires", D % (4, '+ """one\n+ two\n+ """\n+ pass\n'), 1)
check("docstring of 2 is clean", D % (3, '+ """one\n+ two"""\n+ pass\n'), 0)
check("one-line docstring is clean", D % (2, '+ """one"""\n+ pass\n'), 0)
check("both forms in one file are both counted",
      D % (7, '+ # a\n+ # b\n+ # c\n+ x=1\n+ """p\n+ q\n+ r"""\n'), 2)
check("non-Python file is out of scope",
      "+++ b/docs/a.md\n@@ -0,0 +1,4 @@\n+ # a\n+ # b\n+ # c\n+ x\n", 0)
check("deleted lines are not prose",
      "+++ b/src/x.py\n@@ -1,4 +1,1 @@\n- # a\n- # b\n- # c\n+ pass\n", 0)
check("unclosed docstring inside the diff does not fire",
      D % (2, '+ """opened only\n+ still going\n'), 0)
check("non-contiguous comments are separate runs",
      "+++ b/src/x.py\n@@ -0,0 +1,2 @@\n+ # a\n+ # b\n@@ -9,0 +9,2 @@\n+ # c\n+ # d\n", 0)

check("triple quotes inside a string literal do not fire",
      "+++ b/src/x.py\n@@ -0,0 +1,4 @@\n+T = re.compile(r'\"\"\"|x')\n+a=1\n+b=2\n+c=3\n", 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
