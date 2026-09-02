#!/usr/bin/env python3
"""src/signal_image_gen.py + src/signal_worker_launch.py — the enforced generation path.

The wrapper takes a prompt (+ optional size preset) and a BARE output name; the
output root comes only from SIGNAL_TASK_OUTPUT_ROOT, which the launcher derives
server-side from a verified Signal Room task id. Covers: a generated artifact
(stubbed provider) lands 0600 under the root and its path is printed; every
path-shaped name (traversal, absolute, separators, wrong or missing extension)
is refused; an unset, relative, non-normalized, out-of-results, non-Signal-Room,
nested, file, or SYMLINKED root is refused; existing names are never overwritten;
the provider's failure writes nothing; the launcher refuses unknown, foreign-lane,
malformed and dir-less ids and hands a real worker exactly one variable; the
launcher runs the worker under a seatbelt profile whose ONLY write allowance is
the server-derived task root (a forged root, another task's dir, the results dir
and the workspace are denied by the kernel), and never launches unsandboxed.

Run: python3 tests/signal-image-gen.test.py
"""
import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import signal_image_gen as gen  # noqa: E402
import signal_worker_launch as launch  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


WS = Path(tempfile.mkdtemp(prefix="signal-image-gen-"))
RESULTS = WS / "results"
TASKS = WS / "tasks"
TASKS.mkdir()
CANON = os.path.realpath(RESULTS)
TASK = "task-signal-1-abcd"
ROOT = os.path.join(CANON, TASK)
Path(ROOT).mkdir(parents=True)
PNG = gen.PNG_MAGIC + b"\x00" * 24
calls = []


def provider(prompt, aspect):
    calls.append((prompt, aspect))
    return PNG


def run(argv, env=None, prov=provider, results=RESULTS):
    out, err = io.StringIO(), io.StringIO()
    rc = gen.run(argv, {gen.OUTPUT_ROOT_ENV: ROOT} if env is None else env, prov, results, out=out, err=err)
    return rc, out.getvalue().strip(), err.getvalue()


def listing(root=ROOT):
    return sorted(os.listdir(root))


print("== a generated artifact ==")
rc, out, err = run(["--prompt", "  a cat  ", "--name", "cat.png"])
check("exit 0 and the absolute path is printed", rc == 0 and out == os.path.join(ROOT, "cat.png"), f"rc={rc} out={out!r} err={err!r}")
check("bytes written verbatim, 0600", Path(ROOT, "cat.png").read_bytes() == PNG
      and stat.S_IMODE(os.stat(Path(ROOT, "cat.png")).st_mode) == 0o600)
check("the provider saw the stripped prompt and no aspect", calls[-1] == ("a cat", None))
rc, out, err = run(["--prompt", "wide", "--name", "wide.png", "--size", "landscape"])
check("size preset maps to an aspect ratio for the provider", rc == 0 and calls[-1] == ("wide", "16:9"))
rc, out, err = run(["--prompt", "x", "--name", "cat.png"])
check("an existing name is never overwritten (O_EXCL)", rc == 2 and Path(ROOT, "cat.png").read_bytes() == PNG)
try:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, "JPEG")
    rc, out, err = run(["--prompt", "j", "--name", "jpeg.png"], prov=lambda p, a: buf.getvalue())
    check("a non-PNG provider image is re-encoded to PNG",
          rc == 0 and Path(ROOT, "jpeg.png").read_bytes().startswith(gen.PNG_MAGIC), err)
except ImportError:
    rc, out, err = run(["--prompt", "j", "--name", "jpeg.png"], prov=lambda p, a: b"\xff\xd8\xff\x00")
    check("without Pillow a non-PNG provider image is not written", rc == 1 and "jpeg.png" not in listing())


def boom(prompt, aspect):
    raise RuntimeError("provider down")


before = listing()
rc, out, err = run(["--prompt", "x", "--name", "never.png"], prov=boom)
check("provider failure: exit 1, nothing written, nothing printed",
      rc == 1 and out == "" and listing() == before and "provider down" in err)
rc, out, err = run(["--prompt", "x", "--name", "junk.png"], prov=lambda p, a: b"GIF89a....")
check("provider bytes that are not an image: exit 1, nothing written", rc == 1 and listing() == before)

print("== path-shaped names are refused ==")
outside = WS / "outside.png"
provider_calls = len(calls)
for label, name in (("dot-dot traversal", "../x.png"), ("absolute path", str(outside)),
                    ("nested separator", "sub/x.png"), ("backslash", "sub\\x.png"),
                    ("wrong extension", "x.jpg"), ("no extension", "x"), ("only the extension", ".png"),
                    ("dot-prefixed", ".hidden.png"), ("double extension", "x.png.png"),
                    ("uppercase extension", "x.PNG"), ("space", "a b.png"), ("empty", ""),
                    ("too long", "a" * 65 + ".png")):
    rc, out, err = run(["--prompt", "x", "--name", name])
    check(f"{label}: exit 2, nothing written", rc == 2 and out == "" and listing() == before
          and not outside.exists(), f"rc={rc} out={out!r}")
rc, out, err = run(["--prompt", "x", "--name", "ok.png", str(outside)])
check("an extra positional path is refused", rc == 2 and listing() == before)
rc, out, err = run(["--prompt", "x", "--name", "ok.png", "--output", str(outside)])
check("the skill's --output flag is not accepted", rc == 2 and listing() == before)
rc, out, err = run(["--prompt", "x", "--name", "ok.png", "--input", str(outside)])
check("no input path either", rc == 2 and listing() == before)
rc, out, err = run(["--prompt", "   ", "--name", "ok.png"])
check("empty prompt refused", rc == 2 and listing() == before)
rc, out, err = run(["--prompt", "x" * (gen.MAX_PROMPT_CHARS + 1), "--name", "ok.png"])
check("over-long prompt refused", rc == 2 and listing() == before)
rc, out, err = run(["--prompt", "x", "--name", "ok.png", "--size", "huge"])
check("unknown size preset refused", rc == 2 and listing() == before)
check("the provider was never called for a refused input", len(calls) == provider_calls)

print("== the root comes only from the environment, and must be the real task dir ==")
elsewhere = WS / "elsewhere" / "results" / TASK
elsewhere.mkdir(parents=True)
link_root = Path(CANON) / "task-signal-2-link"
link_root.symlink_to(ROOT)
file_root = Path(CANON) / "task-signal-3-file"
file_root.write_text("x")
Path(ROOT, "sub").mkdir()
Path(CANON, "task-7-owner").mkdir()
cases = [
    ("unset", {}),
    ("empty", {gen.OUTPUT_ROOT_ENV: ""}),
    ("relative", {gen.OUTPUT_ROOT_ENV: f"results/{TASK}"}),
    ("trailing slash (not normalized)", {gen.OUTPUT_ROOT_ENV: ROOT + "/"}),
    ("dot-dot inside (not normalized)", {gen.OUTPUT_ROOT_ENV: f"{CANON}/task-7-owner/../{TASK}"}),
    ("outside the results dir", {gen.OUTPUT_ROOT_ENV: str(elsewhere)}),
    ("the results dir itself", {gen.OUTPUT_ROOT_ENV: CANON}),
    ("nested under the task dir", {gen.OUTPUT_ROOT_ENV: os.path.join(ROOT, "sub")}),
    ("not a Signal Room task dir", {gen.OUTPUT_ROOT_ENV: os.path.join(CANON, "task-7-owner")}),
    ("a file", {gen.OUTPUT_ROOT_ENV: str(file_root)}),
    ("a missing task dir", {gen.OUTPUT_ROOT_ENV: os.path.join(CANON, "task-signal-9-none")}),
    ("a SYMLINKED task dir", {gen.OUTPUT_ROOT_ENV: str(link_root)}),
    ("traversal in the task component", {gen.OUTPUT_ROOT_ENV: os.path.join(CANON, "task-signal-..")}),
]
if str(RESULTS) != CANON:
    cases.append(("the non-canonical spelling of the results dir", {gen.OUTPUT_ROOT_ENV: str(RESULTS / TASK)}))
snapshot = {p: sorted(os.listdir(p)) for p in (ROOT, CANON, str(elsewhere))}
for label, env in cases:
    rc, out, err = run(["--prompt", "x", "--name", "env.png"], env=env)
    check(f"{label}: exit 2, nothing written anywhere", rc == 2 and out == ""
          and {p: sorted(os.listdir(p)) for p in snapshot} == snapshot, f"rc={rc} err={err.strip()}")
rc, out, err = run(["--prompt", "x", "--name", "env.png"], env={gen.OUTPUT_ROOT_ENV: ROOT, "SIGNAL_TASK_OUTPUT_ROOT_X": "1"})
check("the canonical real task dir is accepted", rc == 0 and Path(ROOT, "env.png").exists())
fd = gen.open_output_root(ROOT, RESULTS)
check("open_output_root hands back a directory descriptor for the real dir",
      stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == os.stat(ROOT).st_ino)
os.close(fd)

print("== the launcher derives the root from a verified task id ==")
(TASKS / f"{TASK}.txt").write_text(f"id: {TASK}\nsource: signal-room\naccess_tier: team\nsource_room_id: !a:hs\ntask: draw\n")
env = launch.worker_env(TASK, TASKS, RESULTS, base_env={"PATH": "/usr/bin"})
check("exactly one variable is added, the canonical task dir",
      env == {"PATH": "/usr/bin", gen.OUTPUT_ROOT_ENV: ROOT}, str(env))
(TASKS / "task-signal-2-link.txt").write_text("id: task-signal-2-link\nsource: signal-room\naccess_tier: team\ntask: x\n")
(TASKS / "task-signal-4-none.txt").write_text("id: task-signal-4-none\nsource: signal-room\naccess_tier: team\ntask: x\n")
(TASKS / "task-signal-5-lane.txt").write_text("id: task-signal-5-lane\nsource: discord\naccess_tier: team\ntask: x\n")
Path(CANON, "task-signal-5-lane").mkdir()
(TASKS / "task-signal-6-body.txt").write_text("id: task-signal-6-body\naccess_tier: team\ntask: x\nsource: signal-room\n")
Path(CANON, "task-signal-6-body").mkdir()
(TASKS / "task-7-owner.txt").write_text("id: task-7-owner\nsource: signal-room\ntask: x\n")
for label, tid in (("unknown id", "task-signal-9-zzzz"), ("non-Signal-Room id", "task-7-owner"),
                   ("traversal", "../" + TASK), ("empty", ""), ("non-string", None),
                   ("task from another lane", "task-signal-5-lane"),
                   ("source only in the untrusted body", "task-signal-6-body"),
                   ("task without an output dir", "task-signal-4-none"),
                   ("task whose output dir is a symlink", "task-signal-2-link")):
    try:
        launch.worker_env(tid, TASKS, RESULTS, base_env={})
        check(f"{label}: refused", False)
    except launch.LaunchRefused as exc:
        check(f"{label}: refused", True, str(exc))
(TASKS / "processed").mkdir()
os.replace(TASKS / f"{TASK}.txt", TASKS / "processed" / f"{TASK}.txt")
check("a processed task still launches", launch.worker_env(TASK, TASKS, RESULTS, base_env={})[gen.OUTPUT_ROOT_ENV] == ROOT)

print("== the launcher's sandbox: one write allowance, the exact task root ==")
profile = launch.sandbox_profile(ROOT)
allows = [line for line in profile.splitlines() if line.startswith("(allow file-write")]
check("the profile allows file-write* under exactly the task root and nothing else",
      allows == [f'(allow file-write* (subpath "{ROOT}"))'], profile)
check("every other write is denied; everything else is inherited",
      "(deny file-write*)" in profile and "(allow default)" in profile
      and profile.count("file-write") == 2 and profile.count("subpath") == 1
      and "literal" not in profile and "regex" not in profile, profile)
check("quotes and backslashes in the root are escaped, never a second string",
      '(subpath "/x/a\\"b\\\\c")' in launch.sandbox_profile('/x/a"b\\c'))
check("the worker runs under sandbox-exec with that profile, the command untouched",
      launch.sandbox_argv(ROOT, ["cmd", "x"]) == [launch.SANDBOX_EXEC, "-p", profile, "cmd", "x"])
forged = str(elsewhere)
check("a forged root is outside the single allowance", not (forged + os.sep).startswith(ROOT + os.sep))
real_sandbox = launch.SANDBOX_EXEC
launch.SANDBOX_EXEC = str(WS / "no-sandbox-exec")
try:
    launch.launch_argv(TASK, TASKS, RESULTS, ["cmd"], base_env={})
    check("without a kernel sandbox the launch is refused", False)
except launch.LaunchRefused as exc:
    check("without a kernel sandbox the launch is refused", "unsandboxed" in str(exc), str(exc))
finally:
    launch.SANDBOX_EXEC = real_sandbox
if os.access(real_sandbox, os.X_OK):
    argv, env_out = launch.launch_argv(TASK, TASKS, RESULTS, ["cmd"], base_env={})
    check("launch_argv derives the root itself and pins it for the wrapper",
          argv == launch.sandbox_argv(ROOT, ["cmd"]) and env_out == {gen.OUTPUT_ROOT_ENV: ROOT})

env = dict(os.environ, SUTANDO_WORKSPACE=str(WS), SUTANDO_TEST_MODE="1")
launcher = str(REPO / "src" / "signal_worker_launch.py")
probe = [sys.executable, "-c", f"import os; print(os.environ.get({gen.OUTPUT_ROOT_ENV!r}))"]
r = subprocess.run([sys.executable, launcher, "task-signal-9-zzzz", "--", *probe], env=env, capture_output=True, text=True)
check("CLI: an unknown id exits 2 before any command runs", r.returncode == 2 and r.stdout == "" and "refused" in r.stderr)
r = subprocess.run([sys.executable, launcher, TASK, *probe], env=env, capture_output=True, text=True)
check("CLI: missing `--` is a usage error", r.returncode == 2 and r.stdout == "")
unsandboxed = [sys.executable, "-c", (
    f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); import signal_worker_launch as l\n"
    f"l.SANDBOX_EXEC = {str(WS / 'no-sandbox-exec')!r}; sys.exit(l.main([{TASK!r}, '--', *sys.argv[1:]]))"), *probe]
r = subprocess.run(unsandboxed, env=env, capture_output=True, text=True)
check("CLI: a Signal Room task never launches unsandboxed — refused and logged, the command never runs",
      r.returncode == 2 and r.stdout == "" and "refused" in r.stderr and "unsandboxed" in r.stderr, r.stderr)

if os.access(launch.SANDBOX_EXEC, os.X_OK):
    print("== under the launcher, the kernel decides ==")
    r = subprocess.run([sys.executable, launcher, TASK, "--", *probe], env=env, capture_output=True, text=True)
    check("CLI: the worker command runs with the root pinned", r.returncode == 0 and r.stdout.strip() == ROOT, r.stderr)
    worker = [sys.executable, "-c", (
        "import os, sys; sys.path.insert(0, sys.argv[1]); import signal_image_gen as g\n"
        "sys.exit(g.run(sys.argv[2:], os.environ, lambda p, a: g.PNG_MAGIC + b'e2e', sys.argv[1] + '/../' + 'nonexistent'))"
    )]
    worker[2] = worker[2].replace("sys.argv[1] + '/../' + 'nonexistent'", f"{str(RESULTS)!r}")
    r = subprocess.run([sys.executable, launcher, TASK, "--", *worker, str(REPO / "src"), "--prompt", "e2e", "--name", "e2e.png"],
                       env=env, capture_output=True, text=True)
    check("end to end: launcher -> wrapper writes under the pinned root and prints its path",
          r.returncode == 0 and r.stdout.strip() == os.path.join(ROOT, "e2e.png")
          and Path(ROOT, "e2e.png").read_bytes() == gen.PNG_MAGIC + b"e2e", r.stderr)
    writer = [sys.executable, "-c", (
        "import os, sys\n"
        "for p in sys.argv[1:]:\n"
        "    try:\n"
        "        open(p, 'wb').write(b'x'); print('wrote', p)\n"
        "    except OSError as exc:\n"
        "        print('denied', p, exc.errno)\n")]
    targets = [os.path.join(forged, "forged.png"), os.path.join(CANON, "task-7-owner", "other.png"),
               os.path.join(CANON, f"{TASK}.txt"), os.path.join(CANON, "task-signal-5-lane", "x.png"),
               os.path.join(str(WS), "anywhere.png"), os.path.join(ROOT, "direct.png")]
    r = subprocess.run([sys.executable, launcher, TASK, "--", *writer, *targets], env=env, capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    check("a direct write outside the root — another task, the results dir, the workspace — is denied by the kernel",
          r.returncode == 0 and all(line.startswith("denied ") for line in lines[:-1])
          and not any(os.path.exists(t) for t in targets[:-1]), r.stdout + r.stderr)
    check("a direct write inside the root is the one thing allowed",
          lines[-1:] == [f"wrote {targets[-1]}"] and Path(ROOT, "direct.png").exists(), r.stdout + r.stderr)
    forged_worker = [sys.executable, "-c", (
        f"import os, sys; sys.path.insert(0, {str(REPO / 'src')!r}); import signal_image_gen as g\n"
        f"os.environ[g.OUTPUT_ROOT_ENV] = {forged!r}\n"
        f"rc = g.run(['--prompt', 'x', '--name', 'forged.png'], os.environ, lambda p, a: g.PNG_MAGIC + b'f', {str(RESULTS)!r})\n"
        f"open(os.path.join({forged!r}, 'raw.png'), 'wb').write(b'x')\n")]
    r = subprocess.run([sys.executable, launcher, TASK, "--", *forged_worker], env=env, capture_output=True, text=True)
    check("a forged SIGNAL_TASK_OUTPUT_ROOT: the wrapper refuses AND the kernel denies the raw write",
          r.returncode != 0 and "refused" in r.stderr and "PermissionError" in r.stderr
          and sorted(os.listdir(forged)) == [], r.stdout + r.stderr + str(os.listdir(forged)))
    r = subprocess.run([sys.executable, str(REPO / "src" / "signal_image_gen.py"), "--prompt", "x", "--name", "bare.png"],
                       env={k: v for k, v in env.items() if k != gen.OUTPUT_ROOT_ENV}, capture_output=True, text=True)
    check("the wrapper run outside the launcher refuses (no root)", r.returncode == 2 and "bare.png" not in listing())
else:
    print("== no sandbox-exec on this host: the launcher must refuse, kernel checks skipped ==")
    r = subprocess.run([sys.executable, launcher, TASK, "--", *probe], env=env, capture_output=True, text=True)
    check("CLI: a valid task is still refused without a kernel sandbox",
          r.returncode == 2 and r.stdout == "" and "unsandboxed" in r.stderr, r.stderr)

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — enforced Signal Room image generation")
