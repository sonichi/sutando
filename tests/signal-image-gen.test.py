#!/usr/bin/env python3
"""src/signal_image_gen.py + the Signal Room launch site — the enforced generation path.

The wrapper takes a prompt (+ optional size preset) and a BARE output name; the
output root is SIGNAL_TASK_OUTPUT_ROOT or, absent that, its working directory —
both pinned by the task's in-band launch, which the bridge (`signal_room_tasks`)
writes as `codex exec --sandbox workspace-write -C <root>`: codex's own seatbelt
then allows writes under that root and nowhere else. Covers: a generated artifact
(stubbed provider) lands 0600 under the root and its path is printed; every
path-shaped name (traversal, absolute, separators, wrong or missing extension) is
refused; an absent, relative, non-normalized, out-of-results, non-Signal-Room,
nested, file, or SYMLINKED root is refused; existing names are never overwritten;
the provider's failure writes nothing; the cwd-derived root works exactly like the
exported one; the id->root derivation refuses unknown, foreign-lane, malformed and
dir-less ids; and the launch site itself: a Signal Room task's block is
workspace-write in the task root with the variable exported — never read-only —
the root exists 0700 before the task is published, and every other lane's block
is unchanged byte for byte.

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
import signal_room_tasks as S  # noqa: E402

import signal_worker_launch as launch  # noqa: E402
from policy.guardrail import (SANDBOXED_DELEGATION_CODEX, sandboxed_delegation_command,  # noqa: E402
                              sandboxed_delegation_lines, sandboxed_delegation_text)

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


def run(argv, env=None, prov=provider, results=RESULTS, cwd=str(WS)):
    out, err = io.StringIO(), io.StringIO()
    rc = gen.run(argv, {gen.OUTPUT_ROOT_ENV: ROOT} if env is None else env, prov, results, out=out, err=err, cwd=cwd)
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

print("== the root is the exported variable or the working directory, and must be the real task dir ==")
elsewhere = WS / "elsewhere" / "results" / TASK
elsewhere.mkdir(parents=True)
link_root = Path(CANON) / "task-signal-2-link"
link_root.symlink_to(ROOT)
file_root = Path(CANON) / "task-signal-3-file"
file_root.write_text("x")
Path(ROOT, "sub").mkdir()
Path(CANON, "task-7-owner").mkdir()
cases = [
    ("unset, and the working directory is not a task dir", {}),
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
for label, env, cwd in (("a forged variable wins over a good working directory, and is refused",
                         {gen.OUTPUT_ROOT_ENV: str(elsewhere)}, ROOT),
                        ("a symlinked working directory", {}, str(link_root)),
                        ("the non-canonical working directory spelling", {}, str(RESULTS / TASK))):
    rc, out, err = run(["--prompt", "x", "--name", "env.png"], env=env, cwd=cwd)
    check(f"{label}: exit 2, nothing written anywhere", rc == 2 and out == ""
          and {p: sorted(os.listdir(p)) for p in snapshot} == snapshot, f"rc={rc} err={err.strip()}")
rc, out, err = run(["--prompt", "x", "--name", "env.png"], env={gen.OUTPUT_ROOT_ENV: ROOT, "SIGNAL_TASK_OUTPUT_ROOT_X": "1"})
check("the canonical real task dir is accepted by variable", rc == 0 and Path(ROOT, "env.png").exists())
rc, out, err = run(["--prompt", "x", "--name", "cwd.png"], env={}, cwd=ROOT)
check("the canonical real task dir is accepted as the working directory, no variable needed",
      rc == 0 and out == os.path.join(ROOT, "cwd.png") and Path(ROOT, "cwd.png").read_bytes() == PNG, err)
fd = gen.open_output_root(ROOT, RESULTS)
check("open_output_root hands back a directory descriptor for the real dir",
      stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == os.stat(ROOT).st_ino)
os.close(fd)

print("== the id->root derivation is verified against the task file ==")
(TASKS / f"{TASK}.txt").write_text(f"id: {TASK}\nsource: signal-room\naccess_tier: team\nsource_room_id: !a:hs\ntask: draw\n")
env = launch.worker_env(TASK, TASKS, RESULTS, base_env={"PATH": "/usr/bin"})
check("exactly one variable is added, the canonical task dir",
      env == {"PATH": "/usr/bin", gen.OUTPUT_ROOT_ENV: ROOT}, str(env))
check("it is the bridge's own spelling of the root", S.worker_output_root(RESULTS, TASK) == ROOT)
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
check("a processed task still derives", launch.worker_env(TASK, TASKS, RESULTS, base_env={})[gen.OUTPUT_ROOT_ENV] == ROOT)
check("nothing launches through the derivation module any more",
      not any(hasattr(launch, n) for n in ("sandbox_profile", "sandbox_argv", "launch_argv", "SANDBOX_EXEC", "main"))
      and "sandbox-exec" not in Path(launch.__file__).read_text())

print("== the launch site: the bridge's block runs the worker under workspace-write in the task root ==")
LAUNCH_TASKS = WS / "launch-tasks"
seen_at_publish = []
real_replace = os.replace


def spy_replace(src, dst):
    if Path(dst).parent != LAUNCH_TASKS:      # the counter's own atomic write is not the publish
        return real_replace(src, dst)
    root = S.worker_output_root(RESULTS, Path(dst).stem)
    try:
        st = os.lstat(root)
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
root = S.worker_output_root(RESULTS, tid)
body = (LAUNCH_TASKS / f"{tid}.txt").read_text()
argv = sandboxed_delegation_command("workspace-write", root, {gen.OUTPUT_ROOT_ENV: root}, network=True)
check("the block's invocation: codex workspace-write, the task root as its cwd (-C), the variable exported",
      argv in body and argv == (f"{gen.OUTPUT_ROOT_ENV}={root} codex exec --sandbox workspace-write -C {root} "
                                f"-c sandbox_workspace_write.network_access=true --skip-git-repo-check -- \"$(cat <prompt-file>)\" < /dev/null"), argv)
check("never read-only, and exactly one launch", "read-only" not in body and body.count("codex exec") == 1)
check("the root is the CANONICAL results dir spelling the wrapper accepts",
      root == os.path.join(CANON, tid) and body.count(root) >= 4, root)
check("the root existed, a plain 0700 directory, BEFORE the task was published (the launch trigger)",
      seen_at_publish == [(True, 0o700)] and os.path.isdir(root), str(seen_at_publish))
check("the contract tells the worker: the wrapper saves in the working directory, announced as [file: root/name]",
      f"[file: {root}/<name>]" in body and "signal_image_gen.py --prompt" in body
      and "working directory" in body and "signal_worker_launch" not in body)
check("the block follows the confined request, as on every other lane",
      body.index("draw a cat") < body.index("===SUTANDO SYSTEM INSTRUCTIONS")
      and body.rstrip().endswith("===END SUTANDO SYSTEM INSTRUCTIONS==="))
check("it is the shared owner's block, stating the lane and tier",
      "This Signal Room task is TEAM tier, not owner tier." in body
      and f"Write only the sandboxed agent's safe user-facing answer to results/{tid}.txt." in body)
check("the derivation module agrees with the launch", launch.output_root_for(tid, LAUNCH_TASKS, RESULTS) == root)
plain = S.submit_signal_room_task("just a question", LAUNCH_TASKS, lambda t: t, room_id="!r:hs")
plain_body = (LAUNCH_TASKS / f"{plain}.txt").read_text()
check("without an output root the body is the request alone: no contract, no launch block, no dir",
      "codex" not in plain_body and "[Signal Room task" not in plain_body
      and not os.path.exists(S.worker_output_root(RESULTS, plain)))

slack = "\n".join(sandboxed_delegation_lines("Slack", "from a TEAM tier sender", "`results/{task_id}.txt`", "scope"))
check("a non-Signal lane's block is unchanged: read-only from the core's own cwd, no -C, no variable",
      "codex exec --sandbox read-only --skip-git-repo-check -- \"$(cat <prompt-file>)\" < /dev/null" in slack
      and " -C " not in slack and gen.OUTPUT_ROOT_ENV not in slack and "workspace-write" not in slack)
check("the shared owner's default text is the read-only rendering, byte for byte",
      SANDBOXED_DELEGATION_CODEX == sandboxed_delegation_text()
      and SANDBOXED_DELEGATION_CODEX.startswith(
          "Delegate it to Codex: `codex exec --sandbox read-only --skip-git-repo-check "
          "-- \"$(cat <prompt-file>)\" < /dev/null`. The `< /dev/null` is REQUIRED"))
check("a root with shell metacharacters is quoted, never a second argument",
      "-C '/r/a b'" in sandboxed_delegation_command("workspace-write", "/r/a b")
      and "X='/r/a b' codex" in sandboxed_delegation_command("workspace-write", "/r/a b", {"X": "/r/a b"}))

print("== inside that launch, the wrapper: root by variable or by working directory, nothing else ==")
env = {k: v for k, v in os.environ.items() if k != gen.OUTPUT_ROOT_ENV}
env.update(SUTANDO_WORKSPACE=str(WS), SUTANDO_TEST_MODE="1")
worker = [sys.executable, "-c", (
    f"import os, sys; sys.path.insert(0, {str(REPO / 'src')!r}); import signal_image_gen as g\n"
    f"sys.exit(g.run(sys.argv[1:], os.environ, lambda p, a: g.PNG_MAGIC + b'e2e', {str(RESULTS)!r}))")]
r = subprocess.run([*worker, "--prompt", "e2e", "--name", "e2e.png"], cwd=ROOT, env=env, capture_output=True, text=True)
check("run in the task root (codex's -C) with no variable: writes there and prints the path",
      r.returncode == 0 and r.stdout.strip() == os.path.join(ROOT, "e2e.png")
      and Path(ROOT, "e2e.png").read_bytes() == gen.PNG_MAGIC + b"e2e", r.stderr)
r = subprocess.run([*worker, "--prompt", "e2e", "--name", "var.png"], cwd=str(WS),
                   env=dict(env, **{gen.OUTPUT_ROOT_ENV: ROOT}), capture_output=True, text=True)
check("run elsewhere with the variable exported: writes under the variable's root",
      r.returncode == 0 and r.stdout.strip() == os.path.join(ROOT, "var.png") and Path(ROOT, "var.png").exists(), r.stderr)
forged = str(elsewhere)
r = subprocess.run([*worker, "--prompt", "x", "--name", "forged.png"], cwd=ROOT,
                   env=dict(env, **{gen.OUTPUT_ROOT_ENV: forged}), capture_output=True, text=True)
check("a forged variable is refused even from the right working directory",
      r.returncode == 2 and "refused" in r.stderr and sorted(os.listdir(forged)) == [], r.stderr)
r = subprocess.run([*worker, "--prompt", "x", "--name", "stray.png"], cwd=str(WS), env=env, capture_output=True, text=True)
check("run outside any task root with no variable: refused, nothing written",
      r.returncode == 2 and "refused" in r.stderr and not (WS / "stray.png").exists(), r.stderr)
r = subprocess.run([sys.executable, str(REPO / "src" / "signal_image_gen.py"), "--prompt", "x", "--name", "bare.png"],
                   cwd=str(REPO), env=env, capture_output=True, text=True)
check("the real CLI outside the launch refuses before any provider call", r.returncode == 2 and "bare.png" not in listing())

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — enforced Signal Room image generation")
