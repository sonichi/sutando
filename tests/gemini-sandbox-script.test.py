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
printf '%s\n' "$HOME" > "$FAKE_GEMINI_HOME"
case "${FAKE_GEMINI_MODE:-ok}" in
  ok)      printf '{"response": "The answer is 42.\\nSecond line.", "stats": {"x": 1}}' ;;
  empty)   printf '{"response": "   ", "stats": {}}' ;;
  error)   printf '{"response": "", "error": {"type": "Quota", "message": "rate limited"}}' ;;
  garbage) printf 'not json at all' ;;
  fail)    echo "boom" >&2; exit 3 ;;
esac
''')
fake.chmod(0o755)
# A fake container runtime, so the platform check passes on Linux CI as it does on macOS.
(fake_bin / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
(fake_bin / "docker").chmod(0o755)
only_gemini = tmp / "only-gemini"
only_gemini.mkdir()
(only_gemini / "gemini").write_text(fake.read_text())
(only_gemini / "gemini").chmod(0o755)

env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
       "FAKE_GEMINI_ARGS": str(tmp / "args"), "FAKE_GEMINI_STDIN": str(tmp / "stdin"),
       "FAKE_GEMINI_PWD": str(tmp / "pwd"), "FAKE_GEMINI_HOME": str(tmp / "home"),
       "GEMINI_API_KEY": "test-key-not-real"}
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

seen_home = (tmp / "home").read_text().strip()
check(seen_home != os.environ.get("HOME") and "gemini-sandbox-home." in seen_home,
      "with GEMINI_API_KEY set the run gets a fresh empty HOME, not the user's")
check(not pathlib.Path(seen_home).exists(), "and that HOME is removed when the run ends")

r_nokey = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "nokey.txt"), "--", "hi"],
                         env={k: v for k, v in {**env, "FAKE_GEMINI_MODE": "ok"}.items() if k != "GEMINI_API_KEY"},
                         capture_output=True, text=True)
check(r_nokey.returncode == 0 and (tmp / "home").read_text().strip() == os.environ.get("HOME")
      and "keeping HOME" in r_nokey.stderr,
      "without the key HOME is kept for the CLI's own auth, and stderr says so")

# A PATH with everything the script needs and no container runtime at all. /usr/bin
# cannot be on it: GitHub's Linux runners ship /usr/bin/docker.
import shutil
tools = tmp / "tools"
tools.mkdir()
for name in ("bash", "uname", "mktemp", "rm", "sed", "cat", "printf", "env"):
    found = shutil.which(name)
    if found:
        os.symlink(found, tools / name)
os.symlink(sys.executable, tools / "python3")
no_runtime = {**env, "FAKE_GEMINI_MODE": "ok", "PATH": f"{only_gemini}{os.pathsep}{tools}"}
r_nort = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "nort.txt"), "--", "hi"],
                        env=no_runtime, capture_output=True, text=True)
if os.uname().sysname == "Darwin":
    check(r_nort.returncode == 0 and (tmp / "nort.txt").exists(),
          "on macOS the curated PATH is enough and seatbelt needs no runtime (control for the case below)")
else:
    check(r_nort.returncode == 2 and "docker or podman" in r_nort.stderr and not (tmp / "nort.txt").exists(),
          "off macOS, no container runtime means refuse, not run unconfined")

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
