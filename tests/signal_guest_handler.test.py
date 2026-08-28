"""signal_guest_handler — the guest deep_dive worker's security contract.

Load-bearing properties:
  * fail-closed without a codex binary OR without an isolated CODEX_HOME (an
    untrusted deep_dive must not inherit the owner's MCP/connectors);
  * the worker env is an allowlist — never the owner's full environment;
  * spawn shape: `codex --sandbox read-only`, OS temp cwd, stdin closed, own group;
  * output goes through the fail-closed egress guard, published to RESULT_DIR;
  * a bounded fleet -> "busy" instead of unbounded workers;
  * the module NEVER writes TASK_DIR, and agent-api routes guest before the owner
    write.

Run: `python3 tests/signal_guest_handler.test.py` (repo `src/` on the path).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import signal_guest_handler as g  # noqa: E402

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        failures += 1


def wait_for(path: Path, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def run() -> None:
    orig_which, orig_popen = shutil.which, subprocess.Popen
    home = Path(tempfile.mkdtemp())  # stand-in isolated CODEX_HOME (a real dir)
    os.environ["OWNER_SECRET"] = "sk-should-never-reach-the-worker"
    try:
        # 1a. Fail-closed: no codex binary.
        shutil.which = lambda _n: None
        os.environ["SIGNAL_GUEST_CODEX_HOME"] = str(home)
        check("worker_available False without a codex binary", g.worker_available() is False)
        d = Path(tempfile.mkdtemp()) / "results"
        g.start_guest_deep_dive("task-nocodex", "dig into item 2", d, lambda t: t)
        check("fail-closed (no codex) writes an unavailable result", wait_for(d / "task-nocodex.txt"))

        # 1b. Fail-closed: codex present but NO isolated CODEX_HOME (the CRITICAL fix —
        #     must not fall back to the owner config).
        shutil.which = lambda _n: "/usr/bin/codex"
        os.environ.pop("SIGNAL_GUEST_CODEX_HOME", None)
        check("worker_available False without an isolated CODEX_HOME", g.worker_available() is False)
        d = Path(tempfile.mkdtemp()) / "results"
        g.start_guest_deep_dive("task-nohome", "dig into item 2", d, lambda t: t)
        check("fail-closed (no isolated home) writes an unavailable result", wait_for(d / "task-nohome.txt"))
        if (d / "task-nohome.txt").exists():
            check("no-home fail-closed does NOT fall back to owner config",
                  "unavailable" in (d / "task-nohome.txt").read_text())

        # 2. Success path: isolated home + codex present. Capture the spawn.
        os.environ["SIGNAL_GUEST_CODEX_HOME"] = str(home)
        check("worker_available True with codex + isolated home", g.worker_available() is True)
        seen: dict = {}

        class FakePopen:
            def __init__(self, argv, **kw):
                seen["argv"] = list(argv)
                seen["env"] = dict(kw.get("env") or {})
                seen["stdin"] = kw.get("stdin")
                seen["start_new_session"] = kw.get("start_new_session")
                out_path = argv[argv.index("-o") + 1]
                Path(out_path).write_text("Summary: 42.\n")
                self.returncode = 0
                self.pid = os.getpid()

            def wait(self, timeout=None):
                return 0

        subprocess.Popen = FakePopen
        d2 = Path(tempfile.mkdtemp()) / "results"
        g.start_guest_deep_dive("task-ok", "dig into item 2", d2, lambda t: t)
        check("success publishes a result to RESULT_DIR", wait_for(d2 / "task-ok.txt"))
        if (d2 / "task-ok.txt").exists():
            check("published result is the guarded worker output",
                  (d2 / "task-ok.txt").read_text().strip() == "Summary: 42.")

        argv = seen.get("argv", [])
        env = seen.get("env", {})
        check("spawn is --sandbox read-only",
              "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only")
        check("spawn cwd pinned to the OS temp dir",
              "-C" in argv and argv[argv.index("-C") + 1] == tempfile.gettempdir())
        check("spawn stdin closed (DEVNULL)", seen.get("stdin") == subprocess.DEVNULL)
        check("spawn in its own process group (start_new_session)", seen.get("start_new_session") is True)
        check("prompt frames untrusted content as data",
              bool(argv) and "UNTRUSTED" in argv[-1] and "dig into item 2" in argv[-1])
        # The CRITICAL/HIGH fixes: isolated config + env allowlist.
        check("worker CODEX_HOME is the isolated home", env.get("CODEX_HOME") == str(home))
        check("worker HOME pinned to the isolated home", env.get("HOME") == str(home))
        check("worker env does NOT leak an owner secret", "OWNER_SECRET" not in env)

        # 3. Bounded fleet: both slots held -> new request is told busy.
        g._slots.acquire(); g._slots.acquire()  # MAX_CONCURRENT == 2
        d3 = Path(tempfile.mkdtemp()) / "results"
        g.start_guest_deep_dive("task-busy", "dig into item 2", d3, lambda t: t)
        ok = wait_for(d3 / "task-busy.txt", timeout=2.0)
        check("at capacity, a guest request gets a busy result (no new worker)",
              ok and "busy" in (d3 / "task-busy.txt").read_text())
        g._slots.release(); g._slots.release()

        # 4. Routing: agent-api sends guest to the sandboxed worker (returns) BEFORE
        #    the /task owner TASK_DIR write.
        api = (Path(g.__file__).resolve().parent / "agent-api.py").read_text()
        gi = api.find('data.get("access_tier") == "guest"')
        oi = api.find('TASK_DIR / f"{task_id}.txt").write_text', gi if gi != -1 else 0)
        check("agent-api routes guest to the sandboxed worker before the /task owner write",
              gi != -1 and oi != -1 and "return" in api[gi:oi] and "start_guest_deep_dive" in api[gi:oi])
        check("guest task_id carries a crypto-random suffix", "secrets.token_hex" in api[gi:oi])
        check("guest result namespace is signal-guest- (NOT task-, so it can't inject into owner voice)",
              'f"signal-guest-' in api[gi:oi])
        # Regression: the owner task-bridge's result-watcher fallthrough (which
        # injects results into the OWNER voice session) must NOT match signal-guest-.
        import re as _re
        tb = (Path(g.__file__).resolve().parent / "task-bridge.ts").read_text()
        m = _re.search(r"function _shouldFallthrough[^{]*\{([^}]*)\}", tb)
        check("_shouldFallthrough gates on task-/voice-/proactive- and excludes signal-guest-",
              m is not None and "signal-guest" not in m.group(1) and "task-" in m.group(1))
        # (Content-Length cap + task validation are asserted at handler level in
        #  agent_api_task_guard.test.py — 413/400, zero body reads, no dispatch.)
    finally:
        shutil.which, subprocess.Popen = orig_which, orig_popen
        os.environ.pop("OWNER_SECRET", None)
        os.environ.pop("SIGNAL_GUEST_CODEX_HOME", None)

    if failures:
        print(f"\n{failures} failure(s)")
        raise SystemExit(1)
    print("\nall ok")


def run_error_paths() -> None:
    """The fail-closed branches. Each is a path where the worker must degrade to a
    written result rather than raise into the API thread or leak a slot."""
    import types

    # _guard: scanner raises -> withhold, never pass the body through unverified.
    fake = types.ModuleType("policy.egress.result")
    fake.guard_result_for_tier = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    saved = sys.modules.get("policy.egress.result")
    sys.modules["policy.egress.result"] = fake
    try:
        check("guard withholds the body when the secret scanner raises",
              g._guard("hello") == "[deep_dive result withheld: could not verify it is free of secrets]")
    finally:
        if saved is not None:
            sys.modules["policy.egress.result"] = saved
        else:
            sys.modules.pop("policy.egress.result", None)

    # _write_result: an unwritable dir must not raise (a lost result reads as pending).
    raised = False
    try:
        g._write_result(Path("/dev/null/nope"), "signal-guest-x", "text")
    except Exception:
        raised = True
    check("write_result swallows an unwritable result dir", not raised)

    # _run: Popen raising -> no output -> the no-result message, and the slot comes back.
    d = Path(tempfile.mkdtemp())
    orig_popen = subprocess.Popen
    before = _slot_count()
    try:
        subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("no codex"))
        g._slots.acquire()
        g._run("signal-guest-p", "q", d, lambda s: s, "/tmp/ch")
    finally:
        subprocess.Popen = orig_popen
    body = (d / "signal-guest-p.txt").read_text()
    check("spawn failure yields the no-result message", body == "[deep_dive returned no result]")
    check("spawn failure releases its slot", _slot_count() == before)

    # _run: a hung process is killed by group and still publishes a result.
    class _Hung:
        pid = 424242
        returncode = None
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        def kill(self):
            pass
    killed = {}
    orig_kill_tree = g._kill_process_tree
    d2 = Path(tempfile.mkdtemp())
    before2 = _slot_count()
    try:
        subprocess.Popen = lambda *a, **k: _Hung()
        g._kill_process_tree = lambda proc: killed.update(pid=proc.pid)
        g._slots.acquire()
        g._run("signal-guest-t", "q", d2, lambda s: s, "/tmp/ch")
    finally:
        subprocess.Popen, g._kill_process_tree = orig_popen, orig_kill_tree
    check("a hung worker delegates whole-tree cleanup", killed.get("pid") == 424242)

    class _TreeProc:
        pid = 515151
        killed = False
        def kill(self):
            self.killed = True
    tree_proc = _TreeProc()
    if os.name == "nt":
        commands = []
        orig_run = g.subprocess.run
        try:
            g.subprocess.run = lambda argv, **kw: (
                commands.append(list(argv)),
                subprocess.CompletedProcess(argv, 0),
            )[1]
            g._kill_process_tree(tree_proc)
        finally:
            g.subprocess.run = orig_run
        check("Windows whole-tree cleanup uses taskkill /T for the worker PID",
              commands == [["taskkill", "/PID", "515151", "/T", "/F"]])
    else:
        sent = {}
        orig_getpgid, orig_killpg = g.os.getpgid, g.os.killpg
        try:
            g.os.getpgid = lambda pid: pid
            g.os.killpg = lambda pgid, sig: sent.update(pgid=pgid, sig=sig)
            g._kill_process_tree(tree_proc)
        finally:
            g.os.getpgid, g.os.killpg = orig_getpgid, orig_killpg
        check("POSIX whole-tree cleanup signals the worker process group",
              sent == {"pgid": 515151, "sig": g.signal.SIGKILL})

    fallback_proc = _TreeProc()
    if os.name == "nt":
        orig_run = g.subprocess.run
        try:
            g.subprocess.run = lambda *a, **k: (
                _ for _ in ()
            ).throw(OSError("taskkill unavailable"))
            g._kill_process_tree(fallback_proc)
        finally:
            g.subprocess.run = orig_run
    else:
        orig_getpgid = g.os.getpgid
        try:
            g.os.getpgid = lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid))
            g._kill_process_tree(fallback_proc)
        finally:
            g.os.getpgid = orig_getpgid
    check("whole-tree cleanup falls back to the worker PID", fallback_proc.killed)

    check("a hung worker still publishes a result",
          (d2 / "signal-guest-t.txt").read_text() == "[deep_dive returned no result]")
    check("a hung worker releases its slot", _slot_count() == before2)

    # start_guest_deep_dive: thread start failing must release the slot AND write.
    d3 = Path(tempfile.mkdtemp())
    orig_thread, orig_which2 = g.threading.Thread, shutil.which
    orig_home = os.environ.get("SIGNAL_GUEST_CODEX_HOME")
    before3 = _slot_count()
    try:
        os.environ["SIGNAL_GUEST_CODEX_HOME"] = tempfile.mkdtemp()
        shutil.which = lambda n: "/usr/bin/codex"
        class _BadThread:
            def __init__(self, **kw): pass
            def start(self): raise RuntimeError("no threads")
        g.threading.Thread = lambda *a, **k: _BadThread()
        g.start_guest_deep_dive("signal-guest-th", "q", d3, lambda s: s)
    finally:
        g.threading.Thread, shutil.which = orig_thread, orig_which2
        if orig_home is None:
            os.environ.pop("SIGNAL_GUEST_CODEX_HOME", None)
        else:
            os.environ["SIGNAL_GUEST_CODEX_HOME"] = orig_home
    check("thread-start failure writes an unavailable result",
          "could not start a research worker" in (d3 / "signal-guest-th.txt").read_text())
    check("thread-start failure does NOT leak a slot", _slot_count() == before3)


def run_fallback_paths() -> None:
    """The fallbacks INSIDE the fallbacks: each is reached only when the first
    recovery attempt itself fails, so none of them run in the happy timeout path."""
    orig_popen = subprocess.Popen
    orig_kill_tree, orig_unlink = g._kill_process_tree, os.unlink

    # returncode 0 but the output file is unreadable -> out stays "" (no crash).
    class _OkProc:
        pid, returncode = 1, 0
        def wait(self, timeout=None): return 0
    d = Path(tempfile.mkdtemp())
    before = _slot_count()
    try:
        subprocess.Popen = lambda *a, **k: _OkProc()
        orig_read = Path.read_text
        Path.read_text = lambda self, *a, **k: (_ for _ in ()).throw(OSError("unreadable"))
        try:
            g._slots.acquire()
            g._run("signal-guest-r", "q", d, lambda s: s, "/tmp/ch")
        finally:
            Path.read_text = orig_read
    finally:
        subprocess.Popen = orig_popen
    check("an unreadable worker output degrades to the no-result message",
          (d / "signal-guest-r.txt").read_text() == "[deep_dive returned no result]")
    check("unreadable output releases its slot", _slot_count() == before)

    # killpg itself failing must fall back to proc.kill() and still publish.
    class _Hung2:
        pid, returncode = 7, None
        killed = False
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=1)
        def kill(self):
            type(self).killed = True
    d2 = Path(tempfile.mkdtemp())
    before2 = _slot_count()
    try:
        subprocess.Popen = lambda *a, **k: _Hung2()
        g._kill_process_tree = lambda proc: proc.kill()
        g._slots.acquire()
        g._run("signal-guest-k", "q", d2, lambda s: s, "/tmp/ch")
    finally:
        subprocess.Popen, g._kill_process_tree = orig_popen, orig_kill_tree
    check("when tree cleanup falls back it kills the worker PID", _Hung2.killed)
    check("a kill fallback still publishes a result",
          (d2 / "signal-guest-k.txt").read_text() == "[deep_dive returned no result]")
    check("a kill fallback releases its slot", _slot_count() == before2)

    # the temp-file cleanup must never raise out of `finally`.
    d3 = Path(tempfile.mkdtemp())
    before3 = _slot_count()
    try:
        subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        os.unlink = lambda p: (_ for _ in ()).throw(OSError("cannot unlink"))
        g._slots.acquire()
        g._run("signal-guest-u", "q", d3, lambda s: s, "/tmp/ch")
    finally:
        subprocess.Popen, os.unlink = orig_popen, orig_unlink
    check("a failing temp-file cleanup does not escape the worker",
          (d3 / "signal-guest-u.txt").read_text() == "[deep_dive returned no result]")
    check("a failing cleanup still releases its slot", _slot_count() == before3)

    # a failure BEFORE the spawn (prompt/mkstemp) hits the outer guard.
    d4 = Path(tempfile.mkdtemp())
    before4 = _slot_count()
    orig_mkstemp = tempfile.mkstemp
    try:
        # ONLY the worker's own mkstemp may fail — _write_result uses it too, and
        # breaking that would test the harness rather than the outer guard.
        calls = {"n": 0}

        def _once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no temp")
            return orig_mkstemp(*a, **k)

        tempfile.mkstemp = _once
        g._slots.acquire()
        g._run("signal-guest-o", "q", d4, lambda s: s, "/tmp/ch")
    finally:
        tempfile.mkstemp = orig_mkstemp
    check("a pre-spawn failure is caught by the outer guard",
          (d4 / "signal-guest-o.txt").read_text() == "[deep_dive returned no result]")
    check("a pre-spawn failure releases its slot", _slot_count() == before4)


def _slot_count() -> int:
    """How many slots are currently free (drain-and-restore, no state change)."""
    n = 0
    while g._slots.acquire(blocking=False):
        n += 1
    for _ in range(n):
        g._slots.release()
    return n


run()
run_error_paths()
run_fallback_paths()
if failures:
    print(f"\n{failures} failure(s)")
    raise SystemExit(1)
print("all ok (error paths)")
