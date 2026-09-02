#!/usr/bin/env python3
"""src/signal_image_gen.py + the Signal Room block — image generation brokered by the trusted core.

The sandboxed worker never generates: its block carries the shared owner's read-only,
network-less invocation byte for byte, and the worker may only emit
`[generate-image: <prompt>]` request lines. AFTER it returns, the core's broker
(`signal_image_broker.py`, one fixed command) runs the wrapper — `--task-id <id>
--prompt <text>`, the prompt one argv element — in its own environment. The wrapper
derives the root ITSELF from the id (`signal_worker_launch.output_root_for`: a
verified task file under the tasks dir, live names included, → `<results>/<task_id>`,
created 0700 when absent) and prints the `[file: <root>/<name>]` marker the core
swaps in. Covers: the derivation from a real task file under its bare, claimed,
assigned, processed and archived names; unknown, foreign-lane, malformed and
traversal ids refused; the stub provider writes exactly one 0600 file under the root
and the marker is printed; default names never collide; every path-shaped or
traversing name is refused; no root can be named on the command line or by
variable; a symlinked or file root is refused; provider failure writes nothing; the
block text pins the instruction and the read-only invocation; and `main()` resolves
the dirs the way agent-api does.

Run: python3 tests/signal-image-gen.test.py
"""
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import signal_image_gen as gen  # noqa: E402
import signal_room_tasks as S  # noqa: E402

import signal_worker_launch as launch  # noqa: E402
from policy.guardrail import SANDBOXED_DELEGATION_CODEX, sandboxed_delegation_lines  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


WS = Path(tempfile.mkdtemp(prefix="signal-image-gen-"))
RESULTS = WS / "results"
TASKS = WS / "tasks"
TASKS.mkdir()
RESULTS.mkdir()
CANON = os.path.realpath(RESULTS)
TASK = "task-signal-1-abcd"
ROOT = os.path.join(CANON, TASK)
PNG = gen.PNG_MAGIC + b"\x00" * 24
calls = []


def provider(prompt, aspect):
    calls.append((prompt, aspect))
    return PNG


def task_file(tid, name=None, source="signal-room"):
    (TASKS / (name or f"{tid}.txt")).write_text(
        f"id: {tid}\nsource: {source}\naccess_tier: team\nsource_room_id: !a:hs\ntask: draw\n")


def run(argv, prov=provider, tasks=TASKS, results=RESULTS):
    out, err = io.StringIO(), io.StringIO()
    rc = gen.run(argv, prov, tasks, results, out=out, err=err)
    return rc, out.getvalue().strip(), err.getvalue()


def listing(root=ROOT):
    return sorted(os.listdir(root)) if os.path.isdir(root) else None


def dirs_under(base=CANON):
    return sorted(os.listdir(base))


print("== the root is derived from the task id, created 0700, and the marker is printed ==")
task_file(TASK)
check("the output dir does not exist before the first run", listing() is None)
rc, out, err = run(["--task-id", TASK, "--prompt", "  a cat  "])
check("exit 0 and the marker is printed", rc == 0 and out == f"[file: {ROOT}/image-1.png]", f"rc={rc} out={out!r} err={err!r}")
check("the root was created as a plain 0700 directory",
      stat.S_ISDIR(os.lstat(ROOT).st_mode) and stat.S_IMODE(os.lstat(ROOT).st_mode) == 0o700)
check("bytes written verbatim, 0600", Path(ROOT, "image-1.png").read_bytes() == PNG
      and stat.S_IMODE(os.stat(Path(ROOT, "image-1.png")).st_mode) == 0o600)
check("the provider saw the stripped prompt and no aspect", calls[-1] == ("a cat", None))
check("exactly one file under the root", listing() == ["image-1.png"])
rc, out, err = run(["--task-id", TASK, "--prompt", "another"])
check("the next run takes the next free default name", rc == 0 and out == f"[file: {ROOT}/image-2.png]"
      and listing() == ["image-1.png", "image-2.png"], out)
rc, out, err = run(["--task-id", TASK, "--prompt", "named", "--name", "cat.png"])
check("an explicit bare name is honoured", rc == 0 and out == f"[file: {ROOT}/cat.png]" and Path(ROOT, "cat.png").exists())
rc, out, err = run(["--task-id", TASK, "--prompt", "x", "--name", "cat.png"])
check("an existing name is never overwritten (O_EXCL)", rc == 2 and out == "" and Path(ROOT, "cat.png").read_bytes() == PNG)
rc, out, err = run(["--task-id", TASK, "--prompt", "wide", "--size", "landscape"])
check("size preset maps to an aspect ratio for the provider", rc == 0 and calls[-1] == ("wide", "16:9"))
try:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, "JPEG")
    rc, out, err = run(["--task-id", TASK, "--prompt", "j", "--name", "jpeg.png"], prov=lambda p, a: buf.getvalue())
    check("a non-PNG provider image is re-encoded to PNG",
          rc == 0 and Path(ROOT, "jpeg.png").read_bytes().startswith(gen.PNG_MAGIC), err)
except ImportError:
    rc, out, err = run(["--task-id", TASK, "--prompt", "j", "--name", "jpeg.png"], prov=lambda p, a: b"\xff\xd8\xff\x00")
    check("without Pillow a non-PNG provider image is not written", rc == 1 and "jpeg.png" not in listing())


def boom(prompt, aspect):
    raise RuntimeError("provider down")


before = listing()
rc, out, err = run(["--task-id", TASK, "--prompt", "x"], prov=boom)
check("provider failure: exit 1, nothing written, nothing printed",
      rc == 1 and out == "" and listing() == before and "provider down" in err)
rc, out, err = run(["--task-id", TASK, "--prompt", "x"], prov=lambda p, a: b"GIF89a....")
check("provider bytes that are not an image: exit 1, nothing written", rc == 1 and listing() == before)

print("== the id is verified against the task file: unknown, foreign and malformed ids are refused ==")
task_file("task-signal-5-lane", source="discord")
(TASKS / "task-signal-6-body.txt").write_text("id: task-signal-6-body\naccess_tier: team\ntask: x\nsource: signal-room\n")
(TASKS / "task-7-owner.txt").write_text("id: task-7-owner\nsource: signal-room\ntask: x\n")
provider_calls, snapshot = len(calls), dirs_under()
for label, tid in (("unknown id", "task-signal-9-zzzz"), ("non-Signal-Room id", "task-7-owner"),
                   ("traversal", "../" + TASK), ("dot-dot task component", "task-signal-.."),
                   ("empty", ""), ("task from another lane", "task-signal-5-lane"),
                   ("source only in the untrusted body", "task-signal-6-body")):
    rc, out, err = run(["--task-id", tid, "--prompt", "x"])
    check(f"{label}: exit 2, no dir created, nothing written", rc == 2 and out == "" and dirs_under() == snapshot,
          f"rc={rc} err={err.strip()}")
    try:
        launch.output_root_for(tid, TASKS, RESULTS)
        check(f"{label}: the derivation refuses", False)
    except launch.LaunchRefused as exc:
        check(f"{label}: the derivation refuses", True, str(exc))
try:
    launch.output_root_for(None, TASKS, RESULTS)
    check("non-string id: the derivation refuses", False)
except launch.LaunchRefused:
    check("non-string id: the derivation refuses", True)
check("the provider was never called for a refused id", len(calls) == provider_calls)

print("== every name a task file carries derives: bare, claimed, assigned, processed, archived ==")
task_file("task-signal-2-clm", name="task-signal-2-clm.claimed-core-1.txt")
root = launch.output_root_for("task-signal-2-clm", TASKS, RESULTS)
check("a CLAIMED live file derives, and the root is created 0700", root == os.path.join(CANON, "task-signal-2-clm")
      and stat.S_IMODE(os.lstat(root).st_mode) == 0o700, root)
rc, out, err = run(["--task-id", "task-signal-2-clm", "--prompt", "x"])
check("the wrapper generates for a claimed task", rc == 0 and out == f"[file: {root}/image-1.png]", err)
task_file("task-signal-3-asg", name="task-signal-3-asg.assigned-inst-7.txt")
check("an ASSIGNED live file derives",
      launch.output_root_for("task-signal-3-asg", TASKS, RESULTS) == os.path.join(CANON, "task-signal-3-asg"))
(TASKS / "processed").mkdir()
os.replace(TASKS / f"{TASK}.txt", TASKS / "processed" / f"{TASK}.txt")
check("a processed task still derives", launch.output_root_for(TASK, TASKS, RESULTS) == ROOT)
month = TASKS / "archive" / "2026-09"
month.mkdir(parents=True)
(month / "task-signal-4-arc.txt").write_text("id: task-signal-4-arc\nsource: signal-room\ntask: x\n")
check("an archived task derives", launch.output_root_for("task-signal-4-arc", TASKS, RESULTS) == os.path.join(CANON, "task-signal-4-arc"))
check("a claimed file of a LONGER id does not satisfy the shorter id",
      launch.find_task("task-signal-2-cl", TASKS) is None)

print("== path-shaped names are refused ==")
outside = WS / "outside.png"
provider_calls, before = len(calls), listing()
for label, name in (("dot-dot traversal", "../x.png"), ("absolute path", str(outside)),
                    ("nested separator", "sub/x.png"), ("backslash", "sub\\x.png"),
                    ("wrong extension", "x.jpg"), ("no extension", "x"), ("only the extension", ".png"),
                    ("dot-prefixed", ".hidden.png"), ("double extension", "x.png.png"),
                    ("uppercase extension", "x.PNG"), ("space", "a b.png"), ("empty", ""),
                    ("too long", "a" * 65 + ".png")):
    rc, out, err = run(["--task-id", TASK, "--prompt", "x", "--name", name])
    check(f"{label}: exit 2, nothing written", rc == 2 and out == "" and listing() == before
          and not outside.exists(), f"rc={rc} out={out!r}")

print("== no root can be named: not on the command line, not by variable ==")
elsewhere = WS / "elsewhere" / "results" / TASK
elsewhere.mkdir(parents=True)
for label, extra in (("an extra positional path", [str(outside)]), ("--output", ["--output", str(outside)]),
                     ("--root", ["--root", str(elsewhere)]), ("--input", ["--input", str(outside)])):
    rc, out, err = run(["--task-id", TASK, "--prompt", "x", *extra])
    check(f"{label} is refused", rc == 2 and listing() == before and not outside.exists())
os.environ["SIGNAL_TASK_OUTPUT_ROOT"] = str(elsewhere)
try:
    rc, out, err = run(["--task-id", TASK, "--prompt", "by-variable", "--name", "var.png"])
finally:
    del os.environ["SIGNAL_TASK_OUTPUT_ROOT"]
check("the old root variable is ignored: the file lands under the derived root",
      rc == 0 and out == f"[file: {ROOT}/var.png]" and sorted(os.listdir(elsewhere)) == [] and Path(ROOT, "var.png").exists())
check("the wrapper source names no root variable, cwd or env root",
      all(t not in Path(gen.__file__).read_text() for t in ("SIGNAL_TASK_OUTPUT_ROOT", "OUTPUT_ROOT_ENV", "getcwd")))
before = listing()
for label, argv in (("empty prompt", ["--prompt", "   "]),
                    ("over-long prompt", ["--prompt", "x" * (gen.MAX_IMAGE_PROMPT_CHARS + 1)]),
                    ("multi-line prompt", ["--prompt", "a\nb"]),
                    ("unknown size preset", ["--prompt", "x", "--size", "huge"])):
    rc, out, err = run(["--task-id", TASK, *argv])
    check(f"{label} refused", rc == 2 and listing() == before)
rc, out, err = run(["--task-id", TASK, "--prompt", "x" * gen.MAX_IMAGE_PROMPT_CHARS, "--name", "max.png"])
check("a prompt exactly at the cap is accepted", rc == 0)
check("the provider was never called for a refused input", len(calls) == provider_calls + 2)

print("== the root must be a plain directory under the results dir ==")
task_file("task-signal-2-link")
Path(CANON, "task-signal-2-link").symlink_to(ROOT)
task_file("task-signal-3-file")
Path(CANON, "task-signal-3-file").write_text("x")
snapshot = {p: sorted(os.listdir(p)) for p in (ROOT, str(elsewhere))}
for label, tid in (("a SYMLINKED task dir", "task-signal-2-link"), ("a file where the task dir should be", "task-signal-3-file")):
    rc, out, err = run(["--task-id", tid, "--prompt", "x"])
    check(f"{label}: exit 2, nothing written anywhere", rc == 2 and out == ""
          and {p: sorted(os.listdir(p)) for p in snapshot} == snapshot, f"rc={rc} err={err.strip()}")
rc, out, err = run(["--task-id", TASK, "--prompt", "x"], results=WS / "nope")
check("a missing results dir: exit 2", rc == 2 and "refused" in err)
fd = gen.open_output_root(ROOT, RESULTS)
check("open_output_root hands back a directory descriptor for the real dir",
      stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == os.stat(ROOT).st_ino)
os.close(fd)
for label, bad in (("relative", f"results/{TASK}"), ("trailing slash", ROOT + "/"),
                   ("outside the results dir", str(elsewhere)), ("the results dir itself", CANON),
                   ("not a Signal Room task dir", os.path.join(CANON, "task-7-owner"))):
    try:
        gen.open_output_root(bad, RESULTS)
        check(f"open_output_root refuses {label}", False)
    except gen.GenerationRefused:
        check(f"open_output_root refuses {label}", True)

print("== the block: the read-only invocation every lane uses, plus the core-side instruction ==")
LAUNCH_TASKS = WS / "launch-tasks"
seen_at_publish = []
real_replace = os.replace


def spy_replace(src, dst):
    if Path(dst).parent == LAUNCH_TASKS:      # the counter's own atomic write is not the publish
        try:
            st = os.lstat(S.canonical_output_root(RESULTS, Path(dst).stem))
            seen_at_publish.append((stat.S_ISDIR(st.st_mode), stat.S_IMODE(st.st_mode)))
        except OSError:
            seen_at_publish.append(None)
    return real_replace(src, dst)


os.replace = spy_replace
try:
    tid = S.submit_signal_room_task("draw a cat", LAUNCH_TASKS, lambda t: t, room_id="!r:hs",
                                    output_root=RESULTS, state_dir=WS / "state")
finally:
    os.replace = real_replace
root = S.canonical_output_root(RESULTS, tid)
body = (LAUNCH_TASKS / f"{tid}.txt").read_text()
lines = S.delegation_lines(tid, RESULTS)
check("the delegation sentence IS the shared owner's read-only default, byte for byte",
      SANDBOXED_DELEGATION_CODEX in lines and SANDBOXED_DELEGATION_CODEX in body)
check("the invocation is read-only from the core's own cwd: no -C, no variable, no network, exactly one launch",
      "codex exec --sandbox read-only --skip-git-repo-check -- \"$(cat <prompt-file>)\" < /dev/null" in body
      and body.count("codex exec") == 1 and all(t not in body for t in ("workspace-write", " -C ", "network_access", "SIGNAL_TASK_OUTPUT_ROOT")))
check("the block tells the CORE to run ONE fixed broker command by task id, the answer on stdin, after the delegate returns",
      f"`python3 {Path(S.__file__).resolve().parent}/signal_image_broker.py --task-id {tid} < <the delegate's answer file>`" in body
      and "AFTER the sandboxed delegate returns" in body and "outside the sandbox" in body)
check("nothing from the answer is ever on a command line: no --prompt, no wrapper call in the block",
      "--prompt" not in body and "signal_image_gen" not in body and "nothing from the answer on the command line" in body)
check("the block states the request line, the cap, the prompt bound and the failure note",
      "[generate-image: <prompt>]" in body and f"first {S.MAX_IMAGE_REQUESTS} such lines" in body
      and f"at most {S.MAX_IMAGE_PROMPT_CHARS} characters" in body and S.IMAGE_FAILED_NOTE in body)
check("the marker the core swaps in is the wrapper's, at the canonical root",
      f"[file: {root}/<name>]" in body and root == os.path.join(CANON, tid))
check("the contract tells the WORKER it cannot write and may only request",
      "You cannot generate or save files" in body and "never write a `[file: …]` line yourself" in body)
check("the root existed, a plain 0700 directory, BEFORE the task was published",
      seen_at_publish == [(True, 0o700)] and os.path.isdir(root), str(seen_at_publish))
check("contract, then the confined request, then the block, as on every other lane",
      body.index("[Signal Room task") < body.index("draw a cat") < body.index("===SUTANDO SYSTEM INSTRUCTIONS")
      and body.rstrip().endswith("===END SUTANDO SYSTEM INSTRUCTIONS==="))
check("it is the shared owner's block, stating the lane and tier",
      "This Signal Room task is TEAM tier, not owner tier." in body
      and f"Write only the sandboxed agent's safe user-facing answer to results/{tid}.txt." in body)
check("the derivation module agrees with the publish", launch.output_root_for(tid, LAUNCH_TASKS, RESULTS) == root)
plain = S.submit_signal_room_task("just a question", LAUNCH_TASKS, lambda t: t, room_id="!r:hs")
plain_body = (LAUNCH_TASKS / f"{plain}.txt").read_text()
check("without an output root the body is the request alone: no contract, no block, no dir",
      "codex" not in plain_body and "[Signal Room task" not in plain_body
      and not os.path.exists(S.canonical_output_root(RESULTS, plain)))
slack = sandboxed_delegation_lines("Slack", "from a TEAM tier sender", "`results/{task_id}.txt`", "scope")
check("the Signal Room lane and a non-Signal lane share one delegation sentence",
      SANDBOXED_DELEGATION_CODEX in slack and slack.index(SANDBOXED_DELEGATION_CODEX) == lines.index(SANDBOXED_DELEGATION_CODEX))
check("nothing launches through the derivation module, and nothing in it reaches a worker",
      not any(hasattr(launch, n) for n in ("sandbox_profile", "sandbox_argv", "launch_argv", "SANDBOX_EXEC", "worker_env", "main"))
      and all(t not in Path(launch.__file__).read_text() for t in ("sandbox-exec", "OUTPUT_ROOT_ENV")))

print("== main(): the dirs agent-api uses, resolved the same way ==")
env = dict(os.environ, SUTANDO_WORKSPACE=str(WS), SUTANDO_TEST_MODE="1")
env.pop("SIGNAL_TASK_OUTPUT_ROOT", None)
via_main = [sys.executable, "-c", (
    f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); import signal_image_gen as g\n"
    f"g.gemini_provider = lambda p, a: g.PNG_MAGIC + b'e2e'\n"
    f"sys.argv = ['signal_image_gen', *sys.argv[1:]]; sys.exit(g.main())")]
r = subprocess.run([*via_main, "--task-id", "task-signal-2-clm", "--prompt", "e2e", "--name", "e2e.png"],
                   cwd=str(REPO), env=env, capture_output=True, text=True)
e2e_root = os.path.join(CANON, "task-signal-2-clm")
check("main() derives <workspace>/tasks and <workspace>/results, and prints the marker",
      r.returncode == 0 and r.stdout.strip() == f"[file: {e2e_root}/e2e.png]"
      and Path(e2e_root, "e2e.png").read_bytes() == gen.PNG_MAGIC + b"e2e", r.stderr)
r = subprocess.run([sys.executable, str(REPO / "src" / "signal_image_gen.py"), "--task-id", "task-signal-9-zzzz", "--prompt", "x"],
                   cwd=str(REPO), env=env, capture_output=True, text=True)
check("the real CLI refuses an unknown id before any provider call",
      r.returncode == 2 and "refused" in r.stderr and not os.path.exists(os.path.join(CANON, "task-signal-9-zzzz")), r.stderr)
r = subprocess.run([sys.executable, str(REPO / "src" / "signal_image_gen.py"), "--prompt", "x"],
                   cwd=str(REPO), env=env, capture_output=True, text=True)
check("the real CLI requires --task-id", r.returncode == 2 and "task-id" in r.stderr)

shutil.rmtree(WS, ignore_errors=True)
print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — Signal Room image generation is brokered by the trusted core")
