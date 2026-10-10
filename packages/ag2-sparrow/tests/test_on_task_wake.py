"""REMOTE_TASK_ON_TASK — the optional wake command Sparrow runs per queued task.

Fires once per newly published task file with the path as its last argument,
never blocks or breaks the pull loop, never re-fires on redelivery or restart,
and never hands the relay credentials to the command.
"""
import importlib
import json
import os
import pathlib
import shlex
import sys
import tempfile
import time

SECRET = "s3cret-relay-token-abcdef0123456789"
MEDIA_SECRET = "hs-media-token-0123456789abcdef"

RECORDER = """
import json, os, sys, time
out = os.environ["ON_TASK_OUT"]
with open(os.path.join(out, "%d-%d.json" % (os.getpid(), time.time_ns())), "w") as f:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, f)
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


def _recorder_cmd(base):
    script = base / "recorder.py"
    script.write_text(RECORDER)
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


if __name__ == "__main__":
    test_fires_once_per_new_task_with_its_path()
    test_unset_runs_nothing()
    test_failing_or_slow_command_never_blocks_or_loses_the_task()
    test_redelivery_and_restart_do_not_refire()
    test_token_never_reaches_the_command()
    print("ALL PASS test_on_task_wake")
