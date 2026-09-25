import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(".").resolve()

def start_watcher(ws):
    env = dict(os.environ)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"), "--role", "standby", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)

def write_task(ws, name, body="probe"):
    final = ws / "tasks" / name
    tmp = final.with_name(f".{name}.tmp")
    tmp.write_text(f"id: {name}\naccess_tier: owner\ntask: {body}\n")
    tmp.replace(final)

def wait_for(pred, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False

def fswatch_live(p):
    r = subprocess.run(["pgrep", "-P", str(p.pid), "-x", "fswatch"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0

def read_available(p, out):
    for stream, buf in ((p.stdout, out[0]), (p.stderr, out[1])):
        try:
            os.set_blocking(stream.fileno(), False)
            c = stream.read()
            if c:
                buf.append(c)
        except Exception:
            pass

def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass

tmp = Path(tempfile.mkdtemp(prefix="margin-sweep-"))
ws = tmp / "ws"
(ws / "tasks").mkdir(parents=True)
(ws / "results" / "archive").mkdir(parents=True)
(ws / "state").mkdir()
cfg = ws / "state" / "task-event-handler.json"

p = start_watcher(ws)
out = [[], []]
wait_for(lambda: (read_available(p, out), fswatch_live(p))[1], timeout=15)

MARGINS = [0.2, 0.4, 0.6, 0.8, 1.0, 1.5]
N = 10
results = {}
latencies = []
trial = 0
for margin in MARGINS:
    passes = 0
    for _ in range(N):
        trial += 1
        handler_log = tmp / f"handler-{trial}.log"
        handler = tmp / f"handler-{trial}.sh"
        handler.write_text('#!/bin/sh\necho handle >> %s\nexit 0\n' % handler_log)
        handler.chmod(0o755)

        t_write = time.time()
        tmp_cfg = cfg.with_name(f".{cfg.name}.tmp")
        tmp_cfg.write_text(json.dumps({"handler": str(handler)}))
        tmp_cfg.replace(cfg)

        time.sleep(margin)
        write_task(ws, f"task-{trial}.txt")
        ok = wait_for(lambda: (read_available(p, out), handler_log.exists() and "handle" in handler_log.read_text())[1], timeout=6)
        if ok:
            passes += 1
        # Latency: newest RELOAD_PROBE line's timestamp minus t_write, if present.
        errtext = "".join(out[1])
        for line in errtext.splitlines():
            if line.startswith("RELOAD_PROBE "):
                try:
                    ts = float(line.split()[1])
                    if ts >= t_write - 0.05:
                        latencies.append(ts - t_write)
                except Exception:
                    pass
    results[margin] = (passes, N)
    print(f"MARGIN {margin}s: {passes}/{N} pass", flush=True)

stop(p)
print("SWEEP DONE")
for margin, (passes, n) in results.items():
    print(f"RESULT margin={margin} pass={passes}/{n}")
if latencies:
    latencies.sort()
    print(f"LATENCY samples={len(latencies)} min={min(latencies):.3f} max={max(latencies):.3f} median={latencies[len(latencies)//2]:.3f}")
else:
    print("LATENCY no samples captured")
