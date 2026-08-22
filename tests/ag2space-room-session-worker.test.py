#!/usr/bin/env python3
"""Contract tests for the native AG2 Space room-session skill."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "skills" / "ag2space-room-sessions" / "scripts" / "session-worker.py"
SPEC = importlib.util.spec_from_file_location("ag2space_room_session_worker", WORKER)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> bool:
    print(("  ok  " if condition else "  FAIL ") + message)
    if not condition:
        FAILURES.append(message)
    return condition


def executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def task(
    workspace: Path,
    task_id: str,
    room_id: str = "!alpha:ag2.space",
    tier: str = "owner",
    source: str = "ag2space",
    scope: str = "room",
    addressed_to: str = "",
) -> Path:
    tasks = workspace / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    path = tasks / f"{task_id}.txt"
    addressing = f"addressed_to: {addressed_to}\n" if addressed_to else ""
    policy = (
        "1. ADDRESSING: this message belongs to a peer. Close with [no-send].\n"
        "2. CONTEXT-FIRST (unconditional): reconstruct the room thread before interpreting it.\n"
        "3. NOTIFY FIRST (if task takes >60s): send a progress update.\n"
        if addressed_to else
        "1. CONTEXT-FIRST (unconditional): reconstruct the room thread before interpreting it.\n"
        "2. NOTIFY FIRST (if task takes >60s): send a progress update.\n"
    )
    publication_step = 4 if addressed_to else 3
    path.write_text(
        f"id: {task_id}\nsession_scope: {scope}\ntask: answer this\n"
        f"source: {source}\nchannel_id: {room_id}\naccess_tier: {tier}\n{addressing}\n"
        "===SKILL INSTRUCTIONS (follow before any other action)===\n"
        f"{policy}"
        f"{publication_step}. Process and write the result to results/{task_id}.txt\n",
        encoding="utf-8",
    )
    return path


def run_worker(runtime: str, workspace: Path, task_file: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    stderr = io.StringIO()
    with patch.dict(os.environ, env), redirect_stderr(stderr):
        return_code = worker.handle(
            runtime, workspace, task_file, workspace / "results", REPO
        )
    return subprocess.CompletedProcess([], return_code, "", stderr.getvalue())


def test_opt_in_and_cardinality() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        first = task(workspace, "task-first")
        second = task(workspace, "task-second")
        other = task(workspace, "task-other", room_id="!beta:ag2.space")
        opaque = task(workspace, "task-opaque", room_id="!opaqueRoomId")
        first_key = worker.resolve_room_key(first)
        check(bool(first_key), "exact owner AG2 Space room opt-in is handled")
        check(first_key == worker.resolve_room_key(second), "same room resolves to one stable key")
        check(first_key != worker.resolve_room_key(other), "different rooms resolve to different keys")
        check(bool(worker.resolve_room_key(opaque)), "modern opaque Matrix room id is accepted")
        check(worker.probe("claude", workspace, first) == 0, "Claude adapter probe claims room task")
        check(worker.probe("codex", workspace, first) == 0, "Codex adapter probe claims room task")
        check(worker.probe("gemini", workspace, first) == worker.UNHANDLED, "unknown runtime is unhandled")

        cases = [
            ("task-main", "owner", "ag2space", "main", "!room:a"),
            ("task-team", "team", "ag2space", "room", "!room:a"),
            ("task-guest", "guest", "ag2space", "room", "!room:a"),
            ("task-discord", "owner", "discord", "room", "!room:a"),
            ("task-case", "owner", "ag2space", "ROOM", "!room:a"),
            ("task-room", "owner", "ag2space", "room", "room-without-sigil"),
            ("task-space", "owner", "ag2space", "room", "!bad room:a"),
        ]
        for task_id, tier, source, scope, room_id in cases:
            candidate = task(workspace, task_id, room_id, tier, source, scope)
            check(worker.resolve_room_key(candidate) is None, f"{task_id} keeps legacy path")

        outside = workspace / "outside.txt"
        outside.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        check(worker.probe("codex", workspace, outside) == worker.UNHANDLED, "task outside tasks dir is rejected")
        check(worker.probe("codex", workspace, workspace / "tasks" / "missing.txt") == worker.UNHANDLED,
              "missing task is unhandled")


def test_session_state_reuses_room_not_message() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        room_key = worker.resolve_room_key(task(workspace, "task-one"))
        other_key = worker.resolve_room_key(task(workspace, "task-two", room_id="!other:a"))
        assert room_key and other_key
        session_id, created = worker._session_id(workspace, "codex", room_key)
        check(created, "first message proposes a new provider session")
        worker._record_session(workspace, "codex", room_key, session_id)
        resumed, created = worker._session_id(workspace, "codex", room_key)
        check(not created and resumed == session_id, "second message in room resumes provider session")
        other_id, created = worker._session_id(workspace, "codex", other_key)
        check(created and other_id != session_id, "different room proposes a different provider session")
        worker._record_session(workspace, "codex", room_key, session_id)
        state = json.loads(worker._state_path(workspace).read_text(encoding="utf-8"))
        row = state["sessions"]["codex"][room_key]
        check(row["created_at"] <= row["updated_at"], "session timestamps retain creation time")
        try:
            worker._record_session(workspace, "codex", room_key, "not-a-session")
        except ValueError:
            check(True, "invalid provider session id is refused")
        else:
            check(False, "invalid provider session id is refused")


def standing_stub(log_path: Path) -> str:
    # The log path is EMBEDDED, not read from env: pane env propagation is the
    # most version-sensitive tmux behavior, and the stub must not depend on it.
    return f"""#!/usr/bin/env python3
import json, pathlib, re, sys
with pathlib.Path({str(log_path)!r}).open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
print('READY', flush=True)
for line in sys.stdin:
    m = re.search(r'\"([^\"]*\\.prompt\\.txt)\"', line)
    if not m:
        continue
    prompt = pathlib.Path(m.group(1)).read_text()
    dest = re.search(r'to (.+?) in a single write', prompt).group(1)
    pathlib.Path(dest).write_text('standing room result\\n')
    print('DONE', flush=True)
"""


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_claude_standing_session_e2e() -> None:
    if shutil.which("tmux") is None:
        print("  SKIP tmux not available - standing-session e2e tests not run")
        return
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        log = root / "claude.jsonl"
        log.touch()
        executable(root / "claude", standing_stub(log))
        env = {
            "PATH": f"{root}:{os.environ['PATH']}",
            "SUTANDO_ROOM_TMUX_DIR": str(root / "tmux"),
            "SUTANDO_ROOM_SPAWN_WAIT": "15",
        }
        first = task(workspace, "task-claude-one", addressed_to="@peer:ag2.space")
        second = task(workspace, "task-claude-two")
        third = task(workspace, "task-claude-three")
        try:
            # The real detached monitor runs: the shared result appearing proves the
            # whole pipeline (inject -> private out-file -> validated publication).
            with patch.dict(os.environ, env), patch.object(worker, "_notify"):
                check(worker.handle("claude", workspace, first, workspace / "results", REPO) == 0,
                      "standing session spawns for the room's first message")
                result_one = workspace / "results" / first.name
                if check(wait_for(lambda: result_one.exists()),
                         "monitor publishes the session's first result"):
                    check(result_one.read_text() == "standing room result\n",
                          "published body is the session's exact private output")

                check(worker.handle("claude", workspace, second, workspace / "results", REPO) == 0,
                      "second message is claimed without a new spawn")
                check(wait_for(lambda: (workspace / "results" / second.name).exists()),
                      "reused pane serves the second message")
                spawns = [json.loads(line) for line in log.read_text().splitlines()]
                check(len(spawns) == 1, "one pane serves consecutive same-room messages")
                check(spawns and "--session-id" in spawns[0],
                      "first spawn proposes a new provider session id")

                room_key = worker.resolve_room_key(first)
                assert room_key
                worker._tmux(workspace, "kill-session", "-t", f"={worker._pane_name('claude', room_key)}")
                check(worker.handle("claude", workspace, third, workspace / "results", REPO) == 0,
                      "message after a pane death is still claimed")
                check(wait_for(lambda: (workspace / "results" / third.name).exists()),
                      "respawned pane serves the message")
                spawns = [json.loads(line) for line in log.read_text().splitlines()]
                if check(len(spawns) == 2 and "--session-id" in spawns[0] and "--resume" in spawns[1],
                         "respawn uses resume after create"):
                    created = spawns[0][spawns[0].index("--session-id") + 1]
                    check(spawns[1][spawns[1].index("--resume") + 1] == created,
                          "respawn resumes the recorded provider session id")
                    state = json.loads(worker._state_path(workspace).read_text(encoding="utf-8"))
                    check(state["sessions"]["claude"][room_key]["session_id"] == created,
                          "standing session id is persisted for resume")

                spool = worker._spool_dir(workspace) / f"{first.stem}.prompt.txt"
                body = spool.read_text(encoding="utf-8")
                check("answer this" in body and "SKILL INSTRUCTIONS" in body,
                      "spool prompt carries task content and trusted execution policy")
                check("ADDRESSING:" in body and "CONTEXT-FIRST (unconditional)" in body,
                      "spool prompt preserves addressing and context-first policy")
                check('"addressed_to": "@peer:ag2.space"' in body,
                      "spool prompt preserves the trusted addressed-to target")
                check(f"Process and write the result to results/{first.stem}.txt" not in body,
                      "spool prompt omits the original numbered publication directive")
                check(str(worker._out_path(workspace, first.stem)) in body,
                      "spool prompt names only the private out-file contract")
                check(str(workspace / "results" / first.name) not in body,
                      "spool prompt never names the shared results path")
                check(str(first) not in body, "spool prompt never names the original task path")
        finally:
            with patch.dict(os.environ, env):
                worker._tmux(workspace, "kill-server")


def test_monitor_watchdog_policies() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        results = workspace / "results"
        results.mkdir()
        settled = task(workspace, "task-settled")
        (results / settled.name).write_text("already done\n")
        timeouts = {"SUTANDO_TIER_HARD_TIMEOUT": "5", "SUTANDO_TIER_STALL_TIMEOUT": "0.2",
                    "SUTANDO_TIER_HEARTBEAT_INTERVAL": "0"}
        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_notify") as notify, patch.object(worker, "_tmux") as tmux:
            worker.monitor("claude", workspace, settled, results)
        check(not tmux.called and not notify.called,
              "monitor exits untouched when the result is already ready")

        finished = task(workspace, "task-finished", room_id="!finished:a")
        worker._atomic_text(worker._out_path(workspace, finished.stem), "real session output\n")
        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_notify") as notify, patch.object(worker, "_tmux") as tmux, \
             patch.object(worker.time, "sleep"):
            worker.monitor("claude", workspace, finished, results)
        check((results / finished.name).read_text() == "real session output\n",
              "monitor is the publisher of the session's private output")
        check(not tmux.called and not notify.called,
              "a finished out-file needs no kill and no failure notice")

        dead = task(workspace, "task-dead", room_id="!dead:a")
        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_pane_alive", return_value=False), \
             patch.object(worker, "_notify"), patch.object(worker.time, "sleep"):
            worker.monitor("claude", workspace, dead, results)
        check("exited before finishing" in (results / dead.name).read_text(),
              "a dead pane without output publishes the honest failure body")

        died_late = task(workspace, "task-died-late", room_id="!died-late:a")
        out_late = worker._out_path(workspace, died_late.stem)

        def write_then_report_dead(ws, n):
            worker._atomic_text(out_late, "finished just in time\n")
            return False

        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_pane_alive", side_effect=write_then_report_dead), \
             patch.object(worker, "_notify"), patch.object(worker.time, "sleep"):
            worker.monitor("claude", workspace, died_late, results)
        check((results / died_late.name).read_text() == "finished just in time\n",
              "a finished out-file beats the failure verdict when the pane dies")

        frozen = task(workspace, "task-frozen", room_id="!frozen:a")
        events: list[str] = []
        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_pane_alive", return_value=True), \
             patch.object(worker, "_pane_content", return_value="frozen screen"), \
             patch.object(worker, "_notify"), patch.object(worker.time, "sleep"), \
             patch.object(worker, "_tmux",
                          side_effect=lambda ws, *a: events.append(a[0])), \
             patch.object(worker, "_settle",
                          side_effect=lambda *a: events.append("settle")):
            worker.monitor("claude", workspace, frozen, results)
        check(events == ["kill-session", "settle"],
              "a frozen pane is fenced (killed) BEFORE the terminal verdict")

        flaky = task(workspace, "task-flaky", room_id="!flaky:a")
        with patch.dict(os.environ, timeouts), \
             patch.object(worker, "_pane_alive", return_value=None), \
             patch.object(worker, "_notify"), patch.object(worker.time, "sleep"), \
             patch.object(worker, "_tmux"):
            worker.monitor("claude", workspace, flaky, results)
        check("froze" in (results / flaky.name).read_text(),
              "a failing liveness probe degrades to stall handling, never a dead verdict")

        busy = task(workspace, "task-busy", room_id="!busy:a")
        out_busy = worker._out_path(workspace, busy.stem)
        ceiling = {**timeouts, "SUTANDO_TIER_HARD_TIMEOUT": "0.3",
                   "SUTANDO_TIER_STALL_TIMEOUT": "5"}
        finish_timer = threading.Timer(0.6, lambda: worker._atomic_text(out_busy, "slow but done\n"))
        finish_timer.start()
        try:
            with patch.dict(os.environ, ceiling), \
                 patch.object(worker, "_pane_alive", return_value=True), \
                 patch.object(worker, "_pane_content",
                              side_effect=lambda ws, n: f"tick {time.monotonic()}"), \
                 patch.object(worker, "_notify") as notify, patch.object(worker.time, "sleep"), \
                 patch.object(worker, "_tmux") as tmux:
                worker.monitor("claude", workspace, busy, results)
        finally:
            finish_timer.cancel()
        check(not tmux.called, "an active turn past the safety ceiling is never killed")
        check((results / busy.name).read_text() == "slow but done\n",
              "supervision continues past the ceiling until the result is delivered")
        check(wait_for(lambda: notify.called, 2)
              and "safety ceiling" in notify.call_args.args[1],
              "the safety ceiling posts a notice without abandoning ownership")


def test_claude_handle_dispatch_without_tmux() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        results = workspace / "results"
        results.mkdir()
        started = task(workspace, "task-dispatch")
        with patch.object(worker, "_ensure_standing_session", return_value="pane") as ensure, \
             patch.object(worker, "_inject") as inject, \
             patch.object(worker, "_spawn_monitor") as monitor, \
             patch.object(worker, "_notify") as notify, \
             patch.object(worker, "_run_codex") as codex:
            code = worker.handle("claude", workspace, started, results, REPO)
        check(code == 0 and ensure.called and inject.called and monitor.called,
              "claude handle ensures the pane, injects, and detaches the monitor")
        check(not codex.called, "claude handle never runs a per-message provider")
        check("standing session" in notify.call_args.args[1],
              "claude handle acks the room after a successful injection")
        spool = worker._spool_dir(workspace) / f"{started.stem}.prompt.txt"
        check(spool.is_file() and str(spool) in inject.call_args.args[2],
              "injection points the pane at the spool prompt")
        spool_text = spool.read_text(encoding="utf-8")
        check(str(worker._out_path(workspace, started.stem)) in spool_text,
              "the spool prompt carries the private out-file contract")
        check(str(results / started.name) not in spool_text,
              "the session is never pointed at the shared results path")

        broken = task(workspace, "task-broken", room_id="!broken:a")
        with patch.object(worker, "_ensure_standing_session",
                          side_effect=RuntimeError("tmux new-session failed")), \
             patch.object(worker, "_notify"):
            code = worker.handle("claude", workspace, broken, results, REPO)
        check(code == 1, "a spawn failure falls back to the watcher path")


def test_startup_failure_never_poisons_the_session_id() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        doomed = task(workspace, "task-doomed")
        room_key = worker.resolve_room_key(doomed)
        assert room_key
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict(os.environ, {"SUTANDO_ROOM_SPAWN_WAIT": "5"}), \
             patch.object(worker, "_tmux", return_value=ok), \
             patch.object(worker, "_pane_alive", return_value=None), \
             patch.object(worker, "_pane_dead", return_value=True), \
             patch.object(worker, "_pane_content", return_value="claude: auth error"):
            try:
                worker._ensure_standing_session(workspace, "claude", room_key, REPO)
            except RuntimeError as exc:
                check("auth error" in str(exc),
                      "startup failure carries the dying pane's screen for diagnosis")
            else:
                check(False, "startup failure carries the dying pane's screen for diagnosis")
        _, created = worker._session_id(workspace, "claude", room_key)
        check(created, "a failed startup never records the session id (no poisoned resume)")


def test_codex_create_resume_and_result_ownership() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        log = root / "codex.jsonl"
        thread_id = str(uuid.uuid4())
        executable(root / "codex", f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['ARG_LOG']).open('a') as out:
    out.write(json.dumps(args) + '\\n')
pathlib.Path(args[args.index('-o') + 1]).write_text('codex room result\\n')
if 'resume' not in args:
    print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}))
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}", "ARG_LOG": str(log)}
        first = task(workspace, "task-codex-one")
        second = task(workspace, "task-codex-two")
        check(run_worker("codex", workspace, first, env).returncode == 0, "Codex creates room thread")
        check(run_worker("codex", workspace, second, env).returncode == 0, "Codex resumes room thread")
        args = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        check("resume" not in args[0] and "resume" in args[1], "Codex uses exec then exec resume")
        check(thread_id in args[1], "Codex resumes the reported thread id")
        state = json.loads(worker._state_path(workspace).read_text(encoding="utf-8"))
        room_key = worker.resolve_room_key(first)
        assert room_key
        check(state["sessions"]["codex"][room_key]["session_id"] == thread_id,
              "Codex reported thread id is persisted")

        existing = task(workspace, "task-existing", room_id="!existing:a")
        (workspace / "results" / existing.name).write_text("consumer wins\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, existing, env).returncode == 0,
              "existing consumer result settles task")
        check(len(log.read_text().splitlines()) == before, "settled task never launches provider")
        check((workspace / "results" / existing.name).read_text() == "consumer wins\n",
              "publish-once contract never clobbers consumer")

        whitespace = task(workspace, "task-whitespace", room_id="!whitespace:a")
        (workspace / "results" / whitespace.name).write_text("  \n")
        before = len(log.read_text().splitlines())
        result = run_worker("codex", workspace, whitespace, env)
        check(result.returncode == 1 and len(log.read_text().splitlines()) == before + 1,
              "whitespace live result invokes provider then falls back without clobbering")

        archived = task(workspace, "task-archived", room_id="!archived:a")
        archive = workspace / "results" / "archive"
        archive.mkdir()
        (archive / f"{archived.stem}-123.txt").write_text("already sent\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, archived, env).returncode == 0,
              "gateway-archived result settles task")
        check(len(log.read_text().splitlines()) == before, "archived task is never replayed")

        retained = task(workspace, "task-retained", room_id="!retained:a")
        retention = workspace / "results" / "archive-2026-08-21"
        retention.mkdir()
        (retention / retained.name).write_text("already sent\n")
        check(run_worker("codex", workspace, retained, env).returncode == 0,
              "retention archive also prevents replay")

        archived_blank = task(workspace, "task-archived-blank", room_id="!archived-blank:a")
        (archive / archived_blank.name).write_text("\n")
        before = len(log.read_text().splitlines())
        check(run_worker("codex", workspace, archived_blank, env).returncode == 0,
              "whitespace archived result is not treated as completion")
        check(len(log.read_text().splitlines()) == before + 1,
              "whitespace archive invokes the provider path")


def test_codex_event_and_state_edges() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        valid_id = str(uuid.uuid4())
        executable(root / "codex", f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(args[args.index('-o') + 1]).write_text('edge result\\n')
mode = os.environ.get('MODE')
if mode == 'fail':
    print('codex failed', file=sys.stderr)
    raise SystemExit(8)
if mode == 'events':
    print('not-json')
    print(json.dumps({{'type': 'thread.started', 'thread_id': 'bad'}}))
    print(json.dumps({{'type': 'other', 'thread_id': '{valid_id}'}}))
    print(json.dumps({{'type': 'thread.started', 'thread_id': '{valid_id}'}}))
""")
        env = {"PATH": f"{root}:{os.environ['PATH']}"}
        edge = task(workspace, "task-events")
        room_key = worker.resolve_room_key(edge)
        assert room_key
        worker._atomic_text(
            worker._state_path(workspace),
            json.dumps({"schema_version": 1, "sessions": {"codex": {
                room_key: {"session_id": "corrupt"}
            }}}),
        )
        check(run_worker("codex", workspace, edge, {**env, "MODE": "events"}).returncode == 0,
              "Codex ignores malformed events and replaces corrupt state")

        no_id = task(workspace, "task-no-id", room_id="!no-id:a")
        result = run_worker("codex", workspace, no_id, env)
        check(result.returncode == 1 and "valid thread.started" in result.stderr,
              "Codex requires a valid new thread id")

        failed = task(workspace, "task-codex-failure", room_id="!codex-failure:a")
        result = run_worker("codex", workspace, failed, {**env, "MODE": "fail"})
        check(result.returncode == 1 and "codex failed" in result.stderr,
              "Codex provider failure returns watcher fallback signal")


def test_runtime_edges_and_adapter_wiring() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        sleeper = executable(root / "sleepy", "#!/bin/sh\nsleep 2\n")
        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0.1", "SUTANDO_TIER_STALL_TIMEOUT": "0.05"}):
            started = time.monotonic()
            try:
                worker._run_bounded([str(sleeper)], root)
            except TimeoutError:
                check(time.monotonic() - started < 1, "stalled provider is terminated within bound")
            else:
                check(False, "stalled provider is terminated within bound")
        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0"}):
            try:
                worker._run_bounded(["true"], root)
            except ValueError:
                check(True, "non-positive provider timeout is rejected")
            else:
                check(False, "non-positive provider timeout is rejected")

        with patch.dict(os.environ, {}, clear=True):
            check(worker._timeout("SUTANDO_TIER_HARD_TIMEOUT", None) == 3600,
                  "manifest declares the hard-timeout safety-ceiling default")
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", None) == 180,
                  "manifest declares the stall-timeout default")
            check(worker._timeout("SUTANDO_TIER_HEARTBEAT_INTERVAL", None) == 120,
                  "manifest declares the heartbeat-interval default")
            check(worker._timeout("SUTANDO_ROOM_SPAWN_WAIT", None) == 30,
                  "manifest declares the spawn-wait default")
        with patch.dict(os.environ, {"SUTANDO_TIER_STALL_TIMEOUT": "12"}):
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", None) == 12,
                  "environment overrides the manifest timeout")
            check(worker._timeout("SUTANDO_TIER_STALL_TIMEOUT", 13) == 13,
                  "CLI timeout overrides the environment")

        with patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "0.05",
                                     "SUTANDO_TIER_STALL_TIMEOUT": "2"}):
            for command, message in [
                ([str(sleeper)], "hard timeout terminates a provider with open streams"),
                (["sh", "-c", "exec 1>&- 2>&-; sleep 2"],
                 "hard timeout terminates a provider after streams close"),
            ]:
                try:
                    worker._run_bounded(command, root)
                except TimeoutError:
                    check(True, message)
                else:
                    check(False, message)

        ended = MagicMock()
        ended.poll.return_value = 0
        worker._terminate(ended)
        check(not ended.wait.called, "termination is a no-op for an exited provider")
        stubborn = MagicMock(pid=123)
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("provider", 2), None]
        with patch.object(worker.os, "killpg") as killpg:
            worker._terminate(stubborn)
        check(killpg.call_count == 2, "termination escalates an unresponsive provider")

        model = "test-model"
        settings = '{"hooks":{}}'
        session = str(uuid.uuid4())
        with patch.dict(os.environ, {"SUTANDO_CORE_MODEL": model,
                                     "SUTANDO_ISOLATED_CLAUDE_SETTINGS": settings}):
            create = worker._standing_launch_command(session, resume=False)
            resume = worker._standing_launch_command(session, resume=True)
            codex = worker._codex_command(None, "prompt", root / "out", REPO)
        check("--model" in create and "--settings" in create,
              "standing launch inherits model and hook settings")
        check("--session-id" in create and "--resume" in resume,
              "standing launch distinguishes create from resume")
        check("-m" in codex, "Codex inherits selected core model")

    relative = "skills/ag2space-room-sessions/scripts/session-worker.py"
    claude_start = (REPO / "src/agent/claude/cli/start-cli.sh").read_text(encoding="utf-8")
    codex_start = (REPO / "src/agent/codex/cli/start-cli.sh").read_text(encoding="utf-8")
    watcher = (REPO / "src/watch-tasks-stream.sh").read_text(encoding="utf-8")
    check(relative in claude_start and "SUTANDO_ISOLATED_CLAUDE_SETTINGS" in claude_start,
          "Claude adapter injects room-session capability")
    check(relative in codex_start and 'version_files+=("$ROOM_SESSION_HANDLER")' in codex_start,
          "Codex adapter injects and versions room-session capability")
    check("ag2space-room-sessions" not in watcher, "generic watcher does not name concrete skill")


def test_cli_probe_and_empty_output() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        workspace = root / "workspace"
        (workspace / "results").mkdir(parents=True)
        room_task = task(workspace, "task-probe")
        probe = subprocess.run(
            [str(WORKER), "--runtime", "codex", "--workspace", str(workspace),
             "--task-file", str(room_task), "--results-dir", str(workspace / "results"),
             "--repo", str(REPO), "--probe"],
            cwd=REPO, check=False,
        )
        check(probe.returncode == 0, "CLI probe delegates to room policy")
        thread_id = str(uuid.uuid4())
        executable(root / "codex", f"""#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path(args[args.index('-o') + 1]).write_text('   \\n')
print(json.dumps({{'type': 'thread.started', 'thread_id': '{thread_id}'}}))
""")
        result = run_worker("codex", workspace, room_task, {"PATH": f"{root}:{os.environ['PATH']}"})
        check(result.returncode == 1 and "empty result" in result.stderr,
              "empty provider output falls back without publishing")


def test_direct_failure_and_cli_edges() -> None:
    with tempfile.TemporaryDirectory() as name:
        workspace = Path(name)
        results = workspace / "results"
        results.mkdir()
        unhandled = task(workspace, "task-unhandled", tier="team")
        check(worker.handle("codex", workspace, unhandled, results, REPO) == worker.UNHANDLED,
              "handle preserves the generic watcher path for an unhandled task")
        check(worker.resolve_room_key(workspace / "tasks" / "absent.txt") is None,
              "room resolver fails closed on an unreadable task")

        published = results / "publish-once.txt"
        worker._publish_once(published, "first\n")
        accepted = worker._publish_once(published, "second\n")
        check(accepted and published.read_text() == "first\n",
              "publish-once accepts an already-ready winning writer")

        raced = task(workspace, "task-raced")
        with patch.object(worker, "_completed_result_exists", side_effect=[False, True]), \
             patch.object(worker, "_run_codex") as provider:
            code = worker.handle("codex", workspace, raced, results, REPO)
        check(code == 0 and not provider.called, "locked completion check prevents a duplicate launch")

        argv = ["session-worker", "--runtime", "codex", "--workspace", str(workspace),
                "--task-file", str(raced), "--results-dir", str(results), "--repo", str(REPO)]
        with patch.object(sys, "argv", argv + ["--probe"]), patch.object(worker, "probe", return_value=17):
            check(worker.main() == 17, "CLI main dispatches probe mode")
        with patch.object(sys, "argv", argv), patch.object(worker, "handle", return_value=18):
            check(worker.main() == 18, "CLI main dispatches handle mode")
        with patch.object(sys, "argv", argv + ["--monitor"]), \
             patch.object(worker, "monitor") as watchdog:
            check(worker.main() == 0 and watchdog.called, "CLI main dispatches monitor mode")


def main() -> int:
    test_opt_in_and_cardinality()
    test_session_state_reuses_room_not_message()
    test_claude_standing_session_e2e()
    test_monitor_watchdog_policies()
    test_claude_handle_dispatch_without_tmux()
    test_startup_failure_never_poisons_the_session_id()
    test_codex_create_resume_and_result_ownership()
    test_codex_event_and_state_edges()
    test_runtime_edges_and_adapter_wiring()
    test_cli_probe_and_empty_output()
    test_direct_failure_and_cli_edges()
    print(f"\nResults: {len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
