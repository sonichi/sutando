"""signal_guest_handler — the guest deep_dive worker's security contract.

Load-bearing properties:
  * fail-closed without a codex binary OR without an isolated CODEX_HOME (an
    untrusted deep_dive must not inherit the owner's MCP/connectors);
  * the worker env is an allowlist — never the owner's full environment;
  * spawn shape: `codex --sandbox read-only`, cwd /tmp, stdin closed, own group;
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
        check("spawn cwd pinned to /tmp (-C /tmp)", "-C" in argv and argv[argv.index("-C") + 1] == "/tmp")
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


run()
