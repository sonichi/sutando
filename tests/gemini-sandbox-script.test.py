#!/usr/bin/env python3
"""gemini-sandbox.sh keeps codex's `-o FILE -- PROMPT` contract, with a fake gemini
on PATH so nothing here needs a key or the network.

  - the final answer, and only it, lands in FILE
  - a nonzero gemini exit is forwarded and FILE is not written
  - exit 0 with an empty answer writes no file and exits 0 (Stage 2 no-output)
  - an error object, or a non-JSON body, is a failure even on exit 0
  - the prompt reaches gemini verbatim, headless, in plan mode, sandboxed
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "claude-gemini" / "scripts" / "gemini-sandbox.sh"
fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


tmp = pathlib.Path(tempfile.mkdtemp(prefix="gemini-sandbox-"))
fake_bin = tmp / "bin"
fake_bin.mkdir()
fake = fake_bin / "gemini"
# The fake records its argv and stdin, then behaves as FAKE_GEMINI_MODE says.
fake.write_text(r'''#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_GEMINI_ARGS"
cat > "$FAKE_GEMINI_STDIN"
pwd > "$FAKE_GEMINI_PWD"
case "${FAKE_GEMINI_MODE:-ok}" in
  ok)      printf '{"response": "The answer is 42.\\nSecond line.", "stats": {"x": 1}}' ;;
  empty)   printf '{"response": "   ", "stats": {}}' ;;
  error)   printf '{"response": "", "error": {"type": "Quota", "message": "rate limited"}}' ;;
  garbage) printf 'not json at all' ;;
  fail)    echo "boom" >&2; exit 3 ;;
esac
''')
fake.chmod(0o755)

env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
       "FAKE_GEMINI_ARGS": str(tmp / "args"), "FAKE_GEMINI_STDIN": str(tmp / "stdin"),
       "FAKE_GEMINI_PWD": str(tmp / "pwd")}
work = tmp / "work"
work.mkdir()


def run(mode, *extra, out=None):
    out = out or (tmp / f"out-{mode}.txt")
    e = {**env, "FAKE_GEMINI_MODE": mode}
    r = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(out), *extra, "--",
                        "What is", "the answer?"], env=e, capture_output=True, text=True)
    return r, out


r, out = run("ok")
check(r.returncode == 0, "success exits 0")
check(out.read_text() == "The answer is 42.\nSecond line.\n", "only the response lands in the file, newline-terminated")
args = (tmp / "args").read_text().splitlines()
check(args[:2] == ["--prompt", "What is the answer?"], "the prompt reaches gemini verbatim, joined by spaces")
check("--approval-mode" in args and args[args.index("--approval-mode") + 1] == "plan", "plan mode, no edits")
check("--sandbox" in args, "sandboxed")
check("--output-format" in args and args[args.index("--output-format") + 1] == "json", "json output")
check((tmp / "stdin").read_text() == "", "stdin is closed, so gemini cannot wait on it")
check(os.path.realpath((tmp / "pwd").read_text().strip()) == os.path.realpath(str(work)), "runs in --cd")

r, out = run("fail")
check(r.returncode == 3 and not out.exists(), "a nonzero gemini exit is forwarded and nothing is written")

r, out = run("empty")
check(r.returncode == 0 and not out.exists(), "exit 0 with an empty answer writes no file (Stage 2 no-output case)")

r, out = run("error")
check(r.returncode == 1 and not out.exists() and "rate limited" in r.stderr, "an error object on exit 0 is a failure, named on stderr")

r, out = run("garbage")
check(r.returncode == 1 and not out.exists(), "a non-JSON body is a failure")

r, out = run("ok", "--model", "gemini-2.5-flash")
args = (tmp / "args").read_text().splitlines()
check("--model" in args and args[args.index("--model") + 1] == "gemini-2.5-flash", "--model is passed through")

r = subprocess.run(["bash", str(SCRIPT), "-o", str(tmp / "x"), "--", "hi"], env=env, capture_output=True, text=True)
check(r.returncode == 2 and "--cd" in r.stderr, "missing --cd is a usage error")
r = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "x"), "--"], env=env, capture_output=True, text=True)
check(r.returncode == 2 and "prompt" in r.stderr, "missing prompt is a usage error")
r = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "x"), "--", "hi"],
                   env={**env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
check(r.returncode == 127 and "not found" in r.stderr, "no gemini on PATH exits 127 with a message")

if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nall passed")
