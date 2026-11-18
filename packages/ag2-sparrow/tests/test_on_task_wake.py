"""REMOTE_TASK_ON_TASK — the optional wake command Sparrow runs per queued task.

Fires once per newly published task file with the path as its last argument,
hands it the task's bytes on stdin and its routing headers in env, never
blocks or breaks the pull loop, never re-fires on redelivery or restart, and
never hands the relay credentials to the command.
"""
import importlib
import json
import os
import pathlib
import shlex
import sys
import tempfile
import time

sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

SECRET = "s3cret-relay-token-abcdef0123456789"
MEDIA_SECRET = "hs-media-token-0123456789abcdef"

RECORDER = """
import json, os, sys, time
out = os.environ["ON_TASK_OUT"]
with open(os.path.join(out, "%d-%d.json" % (os.getpid(), time.time_ns())), "w") as f:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, f)
"""

STDIN_RECORDER = """
import json, os, sys, time
data = sys.stdin.buffer.read()
out = os.environ["ON_TASK_OUT"]
with open(os.path.join(out, "%d-%d.json" % (os.getpid(), time.time_ns())), "w") as f:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ), "stdin": data.hex()}, f)
"""


def _load(base, on_task=None):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ["REMOTE_TASK_URL"] = "https://gw.example/relay"
    os.environ["REMOTE_TASK_TOKEN"] = SECRET
    os.environ["REMOTE_MEDIA_HS_TOKEN"] = MEDIA_SECRET
    if on_task is None:
        os.environ.pop("REMOTE_TASK_ON_TASK", None)
    else:
        os.environ["REMOTE_TASK_ON_TASK"] = on_task
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _task(tid="task-1784700000000"):
    return {
        "id": tid,
        "task": "[AG2Space qingyun] wake up",
        "source": "ag2space",
        "channel_id": "!room:ag2.space",
        "user_id": "@qingyun:ag2.space",
        "access_tier": "owner",
        "timestamp": "2026-10-10T00:00:00Z",
    }


def _recorder_cmd(base, source=RECORDER):
    script = base / "recorder.py"
    script.write_text(source)
    out = base / "calls"
    out.mkdir()
    os.environ["ON_TASK_OUT"] = str(out)
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", out


def _calls(out, want, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = sorted(out.glob("*.json"))
        if len(got) >= want:
            break
        time.sleep(0.05)
    time.sleep(0.3)  # a late duplicate would land here
    return [json.loads(p.read_text()) for p in sorted(out.glob("*.json"))]


def test_fires_once_per_new_task_with_its_path():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        tid, durable = m._write_task(_task())
        assert durable
        dest = m.TASKS_DIR / f"{tid}.txt"
        calls = _calls(out, 1)
        assert len(calls) == 1, calls
        assert calls[0]["argv"] == [str(dest)]
        assert calls[0]["env"]["SPARROW_TASK_ID"] == tid
        assert calls[0]["env"]["SPARROW_TASK_FILE"] == str(dest)
        # The file the command is pointed at is already complete.
        assert dest.read_text().endswith("\n")
        print("PASS test_fires_once_per_new_task_with_its_path")


def test_unset_runs_nothing():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base, None)
        assert m.ON_TASK_CMD == ""
        spawned = []
        real = m.subprocess.Popen


        def spy(*a, **k):
            if "SPARROW_TASK_ID" in (k.get("env") or {}):
                spawned.append(a)
            return real(*a, **k)

        m.subprocess.Popen = spy  # type: ignore[assignment]
        try:
            tid, _ = m._write_task(_task())
        finally:
            m.subprocess.Popen = real
        assert (m.TASKS_DIR / f"{tid}.txt").exists()
        assert spawned == [], spawned
        print("PASS test_unset_runs_nothing")


def test_failing_or_slow_command_never_blocks_or_loses_the_task():
    cases = [
        ("missing binary", "/nonexistent/wake-the-agent"),
        ("nonzero exit", f"{shlex.quote(sys.executable)} -c 'raise SystemExit(3)'"),
        ("unparseable", "wake 'unterminated"),
        ("slow", f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(6)'"),
    ]
    for i, (name, cmd) in enumerate(cases):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            m = _load(base, cmd)
            logs = []
            m._log = logs.append
            t0 = time.monotonic()
            first = m._write_task(_task(f"task-17847000001{i:02d}"))
            second = m._write_task(_task(f"task-17847000002{i:02d}"))
            elapsed = time.monotonic() - t0
            assert elapsed < 3, f"{name}: the pull loop waited {elapsed:.1f}s"
            for res in (first, second):
                assert res is not None and res[1], f"{name}: task not durably queued"
                files = list(m.TASKS_DIR.glob(f"{res[0]}.txt"))
                assert len(files) == 1, f"{name}: {files}"
            if name == "nonzero exit":
                deadline = time.time() + 10
                while time.time() < deadline and not any("exited 3" in ln for ln in logs):
                    time.sleep(0.05)
            if name != "slow":
                assert any("on-task command" in ln for ln in logs), f"{name}: {logs}"
            print(f"PASS test_failing_or_slow_command ({name})")


def test_redelivery_and_restart_do_not_refire():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        tid, _ = m._write_task(_task())
        m._write_task(_task())  # relay redelivers the same id
        m = _load(base, cmd)  # Sparrow restarts with the file still queued
        m._write_task(_task())
        # The agent claims it, then the relay replays it once more.
        claimed = m.TASKS_DIR / f"{tid}.claimed-core"
        (m.TASKS_DIR / f"{tid}.txt").rename(claimed)
        m._write_task(_task())
        calls = _calls(out, 1)
        assert len(calls) == 1, f"fired {len(calls)} times"
        print("PASS test_redelivery_and_restart_do_not_refire")


def test_token_never_reaches_the_command():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        os.environ["AG2_REMOTE_TOKEN"] = SECRET
        os.environ["SOME_AGENT_COPY"] = f"Bearer {SECRET}"
        os.environ["UNRELATED_SETTING"] = "keep-me"
        try:
            m = _load(base, cmd)
            m._write_task(_task())
            calls = _calls(out, 1)
        finally:
            for k in ("AG2_REMOTE_TOKEN", "SOME_AGENT_COPY", "UNRELATED_SETTING"):
                os.environ.pop(k, None)
        assert len(calls) == 1
        env = calls[0]["env"]
        blob = json.dumps(calls[0])
        assert SECRET not in blob and MEDIA_SECRET not in blob
        for k in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_MEDIA_HS_TOKEN", "SOME_AGENT_COPY"):
            assert k not in env, k
        assert env.get("UNRELATED_SETTING") == "keep-me"
        print("PASS test_token_never_reaches_the_command")


def test_stdin_carries_the_exact_task_bytes():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base, STDIN_RECORDER)
        m = _load(base, cmd)
        tid, _ = m._write_task(_task())
        dest = m.TASKS_DIR / f"{tid}.txt"
        calls = _calls(out, 1)
        assert len(calls) == 1, calls
        assert bytes.fromhex(calls[0]["stdin"]) == dest.read_bytes()
        assert calls[0]["argv"] == [str(dest)]
        assert calls[0]["env"]["SPARROW_TASK_FILE"] == str(dest)
        print("PASS test_stdin_carries_the_exact_task_bytes")


def test_env_carries_the_routing_headers():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        task = {**_task(), "source_message_id": "$evt1:ag2.space",
                "thread_root": "$root1:ag2.space", "sender_name": "Q" * 2000}
        tid, _ = m._write_task(task)
        headers = m.local_task_protocol.parse_task_headers_trusted(
            (m.TASKS_DIR / f"{tid}.txt").read_text()).headers
        env = _calls(out, 1)[0]["env"]
        assert env["SPARROW_SOURCE"] == "ag2space"
        assert env["SPARROW_ROOM_ID"] == "!room:ag2.space"
        assert env["SPARROW_THREAD_ROOT"] == "$root1:ag2.space"
        assert env["SPARROW_SOURCE_MESSAGE_ID"] == "$evt1:ag2.space"
        assert env["SPARROW_SENDER"] == "@qingyun:ag2.space"
        assert env["SPARROW_ACCESS_TIER"] == headers["access_tier"] == "owner"
        assert env["SPARROW_SENDER_NAME"] == "Q" * 512
        assert all("\n" not in v for k, v in env.items() if k.startswith("SPARROW_"))
        print("PASS test_env_carries_the_routing_headers")


def test_header_holding_the_token_is_not_exported():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        task = {**_task(), "sender_name": f"Mallory {SECRET}",
                "thread_root": f"$root-{MEDIA_SECRET}:ag2.space"}
        m._write_task(task)
        env = _calls(out, 1)[0]["env"]
        assert "SPARROW_SENDER_NAME" not in env, env.get("SPARROW_SENDER_NAME")
        assert "SPARROW_THREAD_ROOT" not in env, env.get("SPARROW_THREAD_ROOT")
        assert env["SPARROW_SOURCE"] == "ag2space"
        assert not any(SECRET in v or MEDIA_SECRET in v for v in env.values())
        print("PASS test_header_holding_the_token_is_not_exported")


def test_token_straddling_the_length_cap_is_not_exported():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        pad = "x" * (m._ON_TASK_ENV_MAX - len(SECRET) // 2)
        m._write_task({**_task(), "sender_name": pad + SECRET})
        env = _calls(out, 1)[0]["env"]
        assert "SPARROW_SENDER_NAME" not in env, len(env.get("SPARROW_SENDER_NAME", ""))
        print("PASS test_token_straddling_the_length_cap_is_not_exported")


def test_control_characters_never_reach_env():
    controls = "".join(chr(c) for c in (*range(0x20), *range(0x7F, 0xA0)))
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, None)
        assert m._env_safe("a\x1b[31mb\x07c\x9bd\te\x00f") == "a [31mb c d e f"
        assert not any(ch in m._env_safe(f"x{controls}y") for ch in controls)
        assert m._one_line("a\tb\x1bc") == "a\tb\x1bc"  # task-file headers unchanged
        m.ON_TASK_CMD = cmd
        path = base / "task-c.txt"
        path.write_text("id: task-c\nsender_name: Al\x1b]0;pwn\x07ice\x9b\x7f\n"
                        "source_message_id: $e\x1b[2J:ag2.space\ntask: hi\n")
        m._fire_on_task("task-c", path)
        env = _calls(out, 1)[0]["env"]
        assert env["SPARROW_SENDER_NAME"] == "Al ]0;pwn ice", env["SPARROW_SENDER_NAME"]
        assert env["SPARROW_SOURCE_MESSAGE_ID"] == "$e [2J:ag2.space"
        for k, v in env.items():
            if k.startswith("SPARROW_"):
                assert not any(ch in v for ch in controls), (k, v)
        print("PASS test_control_characters_never_reach_env")


def test_task_file_keeps_tab_and_esc_in_headers():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base)
        m = _load(base, cmd)
        tid, _ = m._write_task({**_task(), "sender_name": "Al\tice\x1b[1m"})
        text = (m.TASKS_DIR / f"{tid}.txt").read_text()
        assert "sender_name: Al\tice\x1b[1m\n" in text, text
        assert _calls(out, 1)[0]["env"]["SPARROW_SENDER_NAME"] == "Al ice [1m"
        print("PASS test_task_file_keeps_tab_and_esc_in_headers")


def test_child_that_ignores_or_closes_stdin_never_blocks():
    big = "x" * (512 * 1024)  # larger than any pipe buffer
    cases = [
        ("never reads", f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(6)'"),
        ("exits at once", f"{shlex.quote(sys.executable)} -c 'pass'"),
    ]
    for i, (name, cmd) in enumerate(cases):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            m = _load(base, cmd)
            m._ON_TASK_STDIN_TIMEOUT_S = 1.0
            logs = []
            m._log = logs.append
            res = m._write_task(_task(f"task-17847000003{i:02d}"))
            assert res is not None and res[1], f"{name}: task not durably queued"
            dest = m.TASKS_DIR / f"{res[0]}.txt"
            t0 = time.monotonic()
            m._fire_on_task(res[0], dest, dest.read_text() + big)
            elapsed = time.monotonic() - t0
            assert elapsed < 1, f"{name}: the pull loop waited {elapsed:.1f}s"
            if name == "never reads":
                deadline = time.time() + 10
                while time.time() < deadline and not any("did not read" in ln for ln in logs):
                    time.sleep(0.05)
                assert any("did not read its stdin" in ln for ln in logs), logs
            else:
                time.sleep(1.5)
                assert not any("failed" in ln or "Error" in ln for ln in logs), logs
            print(f"PASS test_child_that_ignores_or_closes_stdin_never_blocks ({name})")


def test_stdin_feed_in_the_calling_thread():
    """The feeder itself, run synchronously: reader, non-reader, early exit, no pipe."""
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base, None)
        m._ON_TASK_STDIN_TIMEOUT_S = 1.0
        logs = []
        m._log = logs.append
        big = b"y" * (512 * 1024)
        py = sys.executable

        def spawn(code):
            return subprocess.Popen([py, "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        reader = spawn("import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))")
        m._feed_and_reap(reader, big, "t-read")
        assert reader.stdout.read() == str(len(big)).encode()
        reader.stdout.close()
        assert reader.stdin.closed

        sleeper = spawn("import time; time.sleep(30)")
        t0 = time.monotonic()
        m._feed_on_task_stdin(sleeper, big, "t-sleep")
        assert time.monotonic() - t0 < 5
        assert sleeper.stdin.closed
        assert any("did not read its stdin for t-sleep" in ln for ln in logs), logs
        sleeper.kill()
        sleeper.wait()
        sleeper.stdout.close()

        quitter = spawn("raise SystemExit(4)")
        quitter.wait()
        m._feed_and_reap(quitter, big, "t-quit")
        assert quitter.stdin.closed
        assert any("exited 4 for t-quit" in ln for ln in logs), logs
        assert not any("write failed" in ln for ln in logs), logs
        quitter.stdout.close()

        no_pipe = subprocess.Popen([py, "-c", "pass"], stdin=subprocess.DEVNULL)
        m._feed_and_reap(no_pipe, big, "t-none")

        closed = spawn("pass")
        closed.stdin.close()
        m._feed_and_reap(closed, big, "t-closed")
        assert any("stdin write failed for t-closed" in ln for ln in logs), logs
        closed.stdout.close()
        print("PASS test_stdin_feed_in_the_calling_thread")


def test_fire_reads_the_file_when_no_text_is_given():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        cmd, out = _recorder_cmd(base, STDIN_RECORDER)
        m = _load(base, None)
        m.ON_TASK_CMD = cmd
        path = base / "task-x.txt"
        path.write_text("id: task-x\nchannel_id: !r:ag2.space\ntask: hello\n")
        m._fire_on_task("task-x", path)
        call = _calls(out, 1)[0]
        assert bytes.fromhex(call["stdin"]) == path.read_bytes()
        assert call["env"]["SPARROW_ROOM_ID"] == "!r:ag2.space"
        print("PASS test_fire_reads_the_file_when_no_text_is_given")


if __name__ == "__main__":
    test_fires_once_per_new_task_with_its_path()
    test_unset_runs_nothing()
    test_failing_or_slow_command_never_blocks_or_loses_the_task()
    test_redelivery_and_restart_do_not_refire()
    test_token_never_reaches_the_command()
    test_stdin_carries_the_exact_task_bytes()
    test_env_carries_the_routing_headers()
    test_header_holding_the_token_is_not_exported()
    test_token_straddling_the_length_cap_is_not_exported()
    test_control_characters_never_reach_env()
    test_task_file_keeps_tab_and_esc_in_headers()
    test_child_that_ignores_or_closes_stdin_never_blocks()
    test_stdin_feed_in_the_calling_thread()
    test_fire_reads_the_file_when_no_text_is_given()
    print("ALL PASS test_on_task_wake")
