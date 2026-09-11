#!/usr/bin/env python3
"""gemini-sandbox.sh keeps codex's `-o FILE -- PROMPT` contract, with a fake gemini
on PATH so nothing here needs a key or the network.

  - the assistant text, and only it, lands in FILE
  - events reach stderr as they arrive, and a heartbeat when they do not, which is what
    the bounded runner watches; a quiet run is not a dead one
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
# stream-json: newline delimited events, which is what the stall watchdog needs to see.
case "${FAKE_GEMINI_MODE:-ok}" in
  ok)      printf '{"type":"init"}\n{"type":"message","role":"assistant","content":"The answer is 42.\\nSecond line."}\n{"type":"result","status":"success"}\n' ;;
  empty)   printf '{"type":"init"}\n{"type":"message","role":"assistant","content":"   "}\n{"type":"result","status":"success"}\n' ;;
  error)   printf '{"type":"init"}\n{"type":"result","status":"error","error":{"type":"Quota","message":"rate limited"}}\n' ;;
  garbage) printf 'not json at all\n' ;;
  fail)    echo "boom" >&2; exit 3 ;;
  slow)    printf '{"type":"init"}\n'; sleep "${FAKE_GEMINI_SLEEP:-6}"; printf '{"type":"message","role":"assistant","content":"late but complete"}\n{"type":"result","status":"success"}\n' ;;
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
check("--output-format" in args and args[args.index("--output-format") + 1] == "stream-json",
      "streaming output, so the stall watchdog sees the run working")
check((tmp / "stdin").read_text() == "", "stdin is closed, so gemini cannot wait on it")
check(os.path.realpath((tmp / "pwd").read_text().strip()) == os.path.realpath(str(work)), "runs in --cd")

seen_home = (tmp / "home").read_text().strip()
check(seen_home != os.environ.get("HOME") and "gemini-sandbox-home." in seen_home,
      "with GEMINI_API_KEY set the run gets a fresh empty HOME, not the user's")
check(not pathlib.Path(seen_home).exists(), "and that HOME is removed when the run ends")

# Every key based auth scrubs HOME, not only GEMINI_API_KEY.
base_env = {k: v for k, v in env.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY",
                                                       "GOOGLE_APPLICATION_CREDENTIALS",
                                                       "GEMINI_SANDBOX_AUTH_HOME")}
for var in ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
    r_key = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / f"{var}.txt"), "--", "hi"],
                           env={**base_env, "FAKE_GEMINI_MODE": "ok", var: "x"},
                           capture_output=True, text=True)
    seen = (tmp / "home").read_text().strip()
    check(r_key.returncode == 0 and seen != os.environ.get("HOME") and "gemini-sandbox-home." in seen,
          f"{var} alone also gets a fresh empty HOME")

# OAuth: an isolated auth home is used as HOME, and without one the run is refused.
auth_home = tmp / "auth-home"
auth_home.mkdir()
r_auth = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "auth.txt"), "--", "hi"],
                        env={**base_env, "FAKE_GEMINI_MODE": "ok", "GEMINI_SANDBOX_AUTH_HOME": str(auth_home)},
                        capture_output=True, text=True)
check(r_auth.returncode == 0 and os.path.realpath((tmp / "home").read_text().strip()) == os.path.realpath(str(auth_home))
      and "GEMINI_SANDBOX_AUTH_HOME" in r_auth.stderr,
      "with no key, GEMINI_SANDBOX_AUTH_HOME is used as HOME and stderr says so")
r_nokey = subprocess.run(["bash", str(SCRIPT), "--cd", str(work), "-o", str(tmp / "nokey.txt"), "--", "hi"],
                         env={**base_env, "FAKE_GEMINI_MODE": "ok"}, capture_output=True, text=True)
check(r_nokey.returncode == 2 and not (tmp / "nokey.txt").exists() and "refusing" in r_nokey.stderr,
      "with no key and no auth home the run is refused, never run with the owner's HOME")

# Through the real bounded runner with a short stall: a gemini quiet for longer than
# the stall must still land its answer, because the heartbeat is what the runner sees.
BOUNDED = REPO / "skills" / "claude-codex" / "scripts" / "codex-bounded.sh"
slow_out = tmp / "slow.txt"
r_slow = subprocess.run(["bash", str(BOUNDED), "--stall", "2", "--max", "60", "--",
                         "bash", str(SCRIPT), "--cd", str(work), "-o", str(slow_out), "--", "take your time"],
                        env={**env, "FAKE_GEMINI_MODE": "slow", "FAKE_GEMINI_SLEEP": "6",
                             "GEMINI_SANDBOX_HEARTBEAT": "1"},
                        capture_output=True, text=True, timeout=120)
check(r_slow.returncode == 0 and slow_out.exists() and slow_out.read_text().strip() == "late but complete",
      "a gemini quiet for longer than the stall guard still lands its answer, rc 0 not 125")
check("waiting" in r_slow.stderr, "the heartbeat is what kept it alive, and it is visible")

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
check(r.returncode == 0 and not out.exists(),
      "an unparsable event is noted and skipped, and a run that produced no assistant text writes no file")

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
