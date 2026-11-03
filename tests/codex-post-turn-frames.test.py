#!/usr/bin/env python3
"""One reading of a Codex worker's pane, and the evidence kept when it is not idle.

A Codex worker's tasks once waited 36 minutes: its notifier never saw an idle pane
while the supervisor read the same seat as abnormal. The frame that blocked it was a
live-area redraw, gone from scrollback by the time anyone looked, so the cause is
unconfirmed. These are the guards that came out of it:

- fixture frames (a post-turn idle frame, the "Recap" layout, the dim placeholder, a
  typed prompt, a retry banner) read the same through pane_gate and the supervisor;
- the supervisor captures with -e, as the notifier does: without the escapes the dim
  placeholder reads as typed text;
- a pane judged abnormal or not idle is saved, bounded per seat;
- a notifier stalled for STALL_ROUNDS rounds reaches the supervisor's report.
"""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("pool_supervise", SCRIPTS / "pool_supervise.py")
sup = importlib.util.module_from_spec(spec)
sys.modules["pool_supervise"] = sup
spec.loader.exec_module(sup)
from delivery import pane_gate as pg  # noqa: E402

ps, wi = sup.ps, sup.wi
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# The composer and footer as Codex draws them (capture-pane -e): a bold glyph, a dim placeholder.
COMPOSER = "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"
FOOTER = ("  \x1b[38;2;246;226;183mmodel medium\x1b[39m · \x1b[38;2;171;223;167m~/work/repo\x1b[39m"
          " · Main [default]             ⚠ \x1b[38;2;196;167;103m2 warnings\x1b[39m · \x1b[1mf2\x1b[0m to view")
TURN = "• Saved the page and its README.\n  Local tests passed. Result written to the named file."
FRAMES = {
    "post-turn idle": (f"{TURN}\n  Worked for 5m 38s • 10:58 PM\n"
                       "\x1b[2m  1 background terminal running · /ps to view · /stop to close\x1b[0m\n\n"
                       f"{COMPOSER}\n\n{FOOTER}\n"),
    "recap layout": (f"{TURN}\n  Worked for 42s • 10:13 PM\n\n"
                     "  ↳ Recap: The MVP is stopped because both dev connections fail authentication\n"
                     "           with HTTP 503; results were recorded.\n"
                     "           Next: Restore dev authentication to unblock verification.\n\n"
                     f"{COMPOSER}\n\n{FOOTER}\n"),
    "placeholder only": f"{COMPOSER}\n\n{FOOTER}\n",
    "typed prompt": f"{TURN}\n\n\x1b[1m›\x1b[0m please also run the smoke test\n\n{FOOTER}\n",
    "retry banner": ("• Working (12s • esc to interrupt)\n\n"
                     "API Error (503) · Retrying in 4s (attempt 2/10)\n\n"
                     f"{COMPOSER}\n\n{FOOTER}\n"),
}
WANT = {  # pane_gate state, supervisor pane kind
    "post-turn idle": ("idle-ready", ps.PANE_IDLE),
    "recap layout": ("idle-ready", ps.PANE_IDLE),
    "placeholder only": ("idle-ready", ps.PANE_IDLE),
    "typed prompt": ("pending", ps.PANE_IDLE),
    "retry banner": ("abnormal", ps.PANE_ABNORMAL),
}
for name, frame in FRAMES.items():
    check(f"{name}: pane_gate and the supervisor agree",
          (pg.classify_pane(frame, pg.CODEX).state, sup.classify_pane_text(frame, "codex")), WANT[name])
check("the typed prompt is the pending text", pg.classify_pane(FRAMES["typed prompt"], pg.CODEX).pending,
      "please also run the smoke test")
for banner in ("Please log in to continue", "Run /login to continue"):
    coloured = f"\x1b[31m{banner}\x1b[0m\n\n{COMPOSER}\n"
    check(f"a coloured '{banner}' still reads logged out, not abnormal",
          sup.classify_pane_text(coloured, "codex"), ps.PANE_LOGGED_OUT)
plain = re.sub(r"\x1b\[[0-9;]*m", "", FRAMES["post-turn idle"])
check("without -e the dim placeholder reads as typed text", pg.classify_pane(plain, pg.CODEX).state, "pending")

# The supervisor's own capture: -e, and the frame kept only when the pane is not readable as fine.
ws = Path(tempfile.mkdtemp(prefix="codex-post-turn-"))
wid = wi.new_worker_id()
wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",  # the reader's runtime is passed below
                 session_id="11111111-1111-4111-8111-111111111111",
                 tmux_socket="/recorded/tmux.sock", worker_id=wid)
sup.pr.register_worker(ws, wid, "e2")
session = wi.tmux_session_name(wid)
seen = []


def runner_for(frame):
    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, frame, "")
    return run


kind, _ = sup.observe_pane(ws, wid, "codex", runner=runner_for(FRAMES["post-turn idle"]))
check("the supervisor captures with -e", "-e" in seen[-1], True)
check("an idle pane reads idle", kind, ps.PANE_IDLE)
frames_dir = ws / "state" / "pane-frames" / session
check("an idle pane is not saved", frames_dir.exists(), False)
kind, _ = sup.observe_pane(ws, wid, "codex", runner=runner_for(FRAMES["retry banner"]))
check("a retry banner reads abnormal", kind, ps.PANE_ABNORMAL)
saved = sorted(frames_dir.glob("*-abnormal.txt"))
check("the abnormal frame is saved", len(saved), 1)
check("the saved frame is the capture", "Retrying in 4s" in saved[0].read_text(), True)

# Bounded: the newest FRAMES_KEEP per seat; a seat name never escapes the folder.
for i in range(pg.FRAMES_KEEP + 3):
    pg.save_frame(ws, "seat-b", "not-idle", f"frame {i}\n", now=1000.0 + i)
kept = sorted((ws / "state" / "pane-frames" / "seat-b").glob("*.txt"))
check("only the newest frames are kept", len(kept), pg.FRAMES_KEEP)
check("the newest survives", kept[-1].read_text(), f"frame {pg.FRAMES_KEEP + 2}\n")
check("a blank capture is not saved", pg.save_frame(ws, "seat-b", "x", "  \n"), None)
check("a '..' seat stays inside pane-frames", pg.save_frame(ws, "..", "x", "y").parent.name, "unknown")

# A stalled notifier reaches the supervisor's report, and an idle pane clears it.
rounds = [pg.note_idle_timeout(ws, session, now=2000.0 + i) for i in range(pg.STALL_ROUNDS - 1)]
check("rounds count up", rounds, list(range(1, pg.STALL_ROUNDS)))
check("below STALL_ROUNDS nothing is reported", sup.notifier_stalls(ws, [wid]), {})
pg.note_idle_timeout(ws, session, now=2100.0)
report = sup.notifier_stalls(ws, [wid])
check("at STALL_ROUNDS the worker is reported", list(report), [wid])
check("with its rounds", report[wid]["rounds"], pg.STALL_ROUNDS)
check("a seat that is not a supervised worker is not reported", sup.notifier_stalls(ws, []), {})
out = subprocess.run([sys.executable, str(REPO / "src/delivery/pane_gate.py"), "idle-timeout",
                      "--workspace", str(ws), "--seat", "seat-c"], capture_output=True, text=True)
check("the CLI's first round is quiet", out.stdout.strip(), "1")
lines = [subprocess.run([sys.executable, str(REPO / "src/delivery/pane_gate.py"), "idle-timeout",
                         "--workspace", str(ws), "--seat", "seat-c"], capture_output=True, text=True).stdout.strip()
         for _ in range(3)]
check("the CLI says surface exactly once, on round STALL_ROUNDS", lines,
      ["2", f"surface {pg.STALL_ROUNDS}", str(pg.STALL_ROUNDS + 1)])
# Concurrent notifiers on one seat: every round counts, and exactly one says surface.
procs = [subprocess.Popen([sys.executable, str(REPO / "src/delivery/pane_gate.py"), "idle-timeout",
                           "--workspace", str(ws), "--seat", "seat-race"],
                          stdout=subprocess.PIPE, text=True) for _ in range(12)]
outs = sorted(p.communicate()[0].strip() for p in procs)
check("12 concurrent rounds are all counted", json.loads(
    (ws / "state" / "notifier-stalls" / "seat-race.json").read_text())["rounds"], 12)
check("and exactly one of them surfaces", [o for o in outs if o.startswith("surface")],
      [f"surface {pg.STALL_ROUNDS}"])
check("no temp file is left behind", sorted(p.name for p in (ws / "state" / "notifier-stalls").iterdir()
                                             if p.name.startswith(".") and not p.name.endswith(".lock")), [])

# Two frames in the same second are both kept.
a1 = pg.save_frame(ws, "seat-d", "abnormal", "one\n", now=3000.0)
a2 = pg.save_frame(ws, "seat-d", "abnormal", "two\n", now=3000.0)
check("a same-second frame sits beside the first", (a1 != a2, a1.read_text(), a2.read_text()),
      (True, "one\n", "two\n"))

# The frame identity ignores presentation: a colour change is not progress, a text change is.
red = "\x1b[31m• Working on it\x1b[0m\n" + COMPOSER + "\n"
green = "\x1b[32m• Working on it\x1b[0m\n" + COMPOSER + "\n"
moved = "\x1b[31m• Working on it, step 2\x1b[0m\n" + COMPOSER + "\n"
check("a colour-only change keeps the frame id", sup.frame_id(red) == sup.frame_id(green), True)
check("a text change moves it", sup.frame_id(red) == sup.frame_id(moved), False)

pg.clear_idle_timeouts(ws, session)
check("an idle pane clears the report", sup.notifier_stalls(ws, [wid]), {})
check("the record file is gone", (ws / "state" / "notifier-stalls" / f"{session}.json").exists(), False)

# The stall lock must not make the shared classifier POSIX-only: without fcntl it still imports.
# The stdlib that branches on msvcrt is imported first, so only file_lock sees the stand-in.
probe = (
    "import subprocess, tempfile, selectors, threading, sys, types; sys.modules['fcntl'] = None; "
    "m = types.ModuleType('msvcrt'); m.LK_LOCK, m.LK_NBLCK, m.LK_UNLCK = 1, 2, 0; "
    "m.locking = lambda *a: None; sys.modules['msvcrt'] = m; "
    f"sys.path.insert(0, {str(REPO / 'src')!r}); from delivery import pane_gate as pg; "
    "print(pg.classify_pane(sys.stdin.read(), pg.CODEX).state)")
out = subprocess.run([sys.executable, "-c", probe], input=FRAMES["post-turn idle"],
                     capture_output=True, text=True)
check("pane_gate imports and classifies where fcntl is unavailable",
      (out.returncode, out.stdout.strip()), (0, "idle-ready"))

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — Codex post-turn frames")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
