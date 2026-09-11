#!/usr/bin/env python3
"""Every reader agrees with the WRITER on one sentinel contract.

WHY THIS EXISTS. The ownership confirmation in src/restart.sh needs the sentinel
to be an identity RECORD: a pid on line 1, then `key=value` claims. A reader that
`int()`s the whole file turns every recorded watcher into `unknown`
(services_status) and `warn (unreadable PID sentinel ...)` with restart advice
(health-check) — a silent, always-on break of the liveness signal, and it begins
the moment a writer emits a record.

WHAT IT PINS. The PRODUCTION shell writer publishes into a scratch state dir and
the PRODUCTION python readers are then called on that file. Nothing here
re-implements either side, so a divergence between them is what fails.

  A) util_paths.read_sentinel_record / read_sentinel_pid on a real record
  B) ...on the LEGACY pid-only sentinel, which is what a not-yet-restarted
     watcher leaves behind for the whole rolling-upgrade window
  C) ...and the malformed / empty / absent cases stay None rather than raising
  D) services_status.probe_pidfile + probe_watcher_sentinels classify a record
     sentinel as `running`, with the CONTROL that a dead pid still reads offline
  E) health-check.check_task_watcher classifies a record sentinel as `ok`,
     with the CONTROL that a pid-only sentinel is unchanged
  F) neither consumer parses the sentinel itself (structural: a private
     `int(...read_text...)` is how this defect got in)
  G) the shared ownership policy reads the same record, and refuses the inputs
     a containment check accepted

Run: python3 tests/watcher-sentinel-record-readers.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SENTINEL_SH = REPO / "src" / "watcher_sentinel.sh"
sys.path.insert(0, str(REPO / "src"))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_record(pid_file: Path, pid: int, *, instance: str = "", inc: str = "inc-1",
                 code: str = "/x/src/watch-tasks-stream.sh", ver: str = "abc1234",
                 ws: str = "/x/ws") -> subprocess.CompletedProcess:
    """Publish through the PRODUCTION writer — never a python re-spelling of it."""
    return subprocess.run(
        ["bash", "-c",
         f'. "{SENTINEL_SH}"\n'
         f'sentinel_write_record "{pid_file}" "{pid}" "{instance}" "{inc}" '
         f'"{code}" "{ver}" "{ws}"'],
        capture_output=True, text=True)


# --- A/B/C: the shared reader ------------------------------------------------

def case_reader(state: Path) -> None:
    import util_paths as up

    pf = state / "watch-tasks-stream.pid"
    r = write_record(pf, 4242, inc="inc-A", ver="deadbee", ws=str(state))
    check("the production shell writer published a record", r.returncode == 0 and pf.exists(),
          f"rc={r.returncode} stderr={r.stderr[:120]!r}")
    if not pf.exists():
        return
    body = pf.read_text()
    check("  ...and it really is multi-line (else every reader check is vacuous)",
          len(body.splitlines()) >= 7, f"{len(body.splitlines())} lines: {body!r}")

    check("read_sentinel_pid returns the pid of a RECORD", up.read_sentinel_pid(pf) == 4242,
          f"got {up.read_sentinel_pid(pf)!r}")
    rec = up.read_sentinel_record(pf)
    check("read_sentinel_record returns the pid", rec.get("pid") == 4242, f"got {rec!r}")
    for k, v in (("incarnation", "inc-A"), ("version", "deadbee"),
                 ("code_path", "/x/src/watch-tasks-stream.sh"), ("workspace", str(state))):
        check(f"  ...and the {k} the writer wrote", rec.get(k) == v, f"got {rec.get(k)!r}")
    check("  ...and started_at is an epoch", str(rec.get("started_at", "")).isdigit(),
          f"got {rec.get('started_at')!r}")

    legacy = state / "legacy.pid"
    legacy.write_text("7777\n")
    check("read_sentinel_pid accepts the LEGACY pid-only sentinel",
          up.read_sentinel_pid(legacy) == 7777, f"got {up.read_sentinel_pid(legacy)!r}")
    lrec = up.read_sentinel_record(legacy)
    check("  ...and its record is the pid with no identity claims",
          lrec.get("pid") == 7777 and set(lrec) == {"pid", "pid_line"}, f"got {lrec!r}")

    for label, text in (("empty", ""), ("whitespace", "  \n"), ("junk", "not-a-pid\n"),
                        ("negative", "-5\n"), ("zero", "0\n")):
        p = state / f"bad-{label}.pid"
        p.write_text(text)
        check(f"a {label} sentinel reads None, never a raised ValueError",
              up.read_sentinel_pid(p) is None, f"got {up.read_sentinel_pid(p)!r}")
    check("an ABSENT sentinel reads None", up.read_sentinel_pid(state / "nope.pid") is None)
    check("  ...and its record is empty", up.read_sentinel_record(state / "nope.pid") == {})
    junk = state / "bad-junk.pid"
    jrec = up.read_sentinel_record(junk)
    check("a junk sentinel's record carries no pid key", "pid" not in jrec, f"got {jrec!r}")
    check("  ...but DOES carry line 1, so a consumer can say why it is unusable",
          jrec.get("pid_line") == "not-a-pid", f"got {jrec!r}")
    check("CONTROL: the non-positive detail wording survives the migration",
          _load("services_status", "src/services_status.py")
          .probe_pidfile(state / "bad-zero.pid", lambda p: True)[1].startswith("non-positive"),
          "services-status.test.py pins that wording")


# --- D: services_status ------------------------------------------------------

def case_services_status(state: Path) -> None:
    ss = _load("services_status", "src/services_status.py")

    pf = state / "watch-tasks-stream.pid"
    status, detail, _ = ss.probe_pidfile(pf, lambda pid: pid == 4242)
    check("services_status.probe_pidfile: a RECORD sentinel is running",
          status == "running", f"got {status!r} / {detail!r}")
    check("  ...and names the pid", "4242" in detail, f"got {detail!r}")

    st2, d2, _ = ss.probe_pidfile(pf, lambda pid: False)
    check("CONTROL: the same record with a DEAD pid still reads offline",
          st2 == "offline", f"got {st2!r} / {d2!r}")
    st3, d3, _ = ss.probe_pidfile(state / "bad-junk.pid", lambda pid: True)
    check("CONTROL: an unparseable sentinel still reads unknown",
          st3 == "unknown", f"got {st3!r} / {d3!r}")

    unreadable = state / "isadir.pid"
    unreadable.mkdir(exist_ok=True)
    st_u, d_u, _ = ss.probe_pidfile(unreadable, lambda pid: True)
    check("CONTROL: a sentinel that cannot be read AT ALL is unknown, not offline",
          st_u == "unknown" and "unreadable pidfile" in d_u, f"got {st_u!r} / {d_u!r}")
    shutil.rmtree(unreadable)

    st5, d5, _ = ss.probe_pidfile(state / "never-written.pid", lambda pid: True)
    check("CONTROL: an ABSENT pidfile is offline, not unknown",
          st5 == "offline" and d5 == "no pidfile", f"got {st5!r} / {d5!r}")
    empty = state / "empty.pid"
    empty.write_text("  \n")
    st6, d6, _ = ss.probe_pidfile(empty, lambda pid: True)
    check("CONTROL: an EMPTY pidfile is offline, not unknown",
          st6 == "offline" and d6 == "empty pidfile", f"got {st6!r} / {d6!r}")

    st4, d4, _ = ss.probe_watcher_sentinels(state, lambda pid: pid == 4242)
    check("services_status.probe_watcher_sentinels: the record host is running",
          st4 == "running", f"got {st4!r} / {d4!r}")


# --- E: health-check ---------------------------------------------------------

def _run_task_watcher_check(hc, ws: Path, pid_text: str, argv: str) -> dict:
    state = ws / "state"
    (state / "cores").mkdir(parents=True, exist_ok=True)
    (state / "cores" / f"{hc._host_label()}.alive").write_text("{}")
    (state / "watch-tasks-stream.pid").write_text(pid_text)
    saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees, hc._ps_snapshot,
             hc._pid_parent, hc._pid_instance_id, hc._pid_actor_id)
    try:
        hc.WORKSPACE_DIR = ws
        hc._proc_argv = lambda pid: argv
        hc._watcher_trees = lambda *a, **k: {}
        hc._ps_snapshot = lambda *a, **k: ""
        hc._pid_parent = lambda pid, ps=None: None
        hc._pid_instance_id = lambda pid: ""
        hc._pid_actor_id = lambda pid: ""
        return hc.check_task_watcher()
    finally:
        (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees, hc._ps_snapshot,
         hc._pid_parent, hc._pid_instance_id, hc._pid_actor_id) = saved


def case_health_check(state: Path, box: Path) -> None:
    hc = _load("health_check", "src/health-check.py")
    record = (state / "watch-tasks-stream.pid").read_text()

    ws = box / "hc-record"
    res = _run_task_watcher_check(hc, ws, record, "bash src/watch-tasks-stream.sh")
    check("health-check.check_task_watcher: a RECORD sentinel with a live watcher is ok",
          res.get("status") == "ok", f"got {res.get('status')!r} / {res.get('detail')!r}")
    check("  ...and never advises a restart over an unreadable sentinel",
          "unreadable PID sentinel" not in res.get("detail", ""), f"detail: {res.get('detail')!r}")

    ws2 = box / "hc-legacy"
    res2 = _run_task_watcher_check(hc, ws2, "4242\n", "bash src/watch-tasks-stream.sh")
    check("CONTROL: the LEGACY pid-only sentinel is unchanged — still ok",
          res2.get("status") == "ok", f"got {res2.get('status')!r} / {res2.get('detail')!r}")

    ws3 = box / "hc-dead"
    res3 = _run_task_watcher_check(hc, ws3, record, "")
    check("CONTROL: a record whose pid is dead still warns",
          res3.get("status") == "warn", f"got {res3.get('status')!r} / {res3.get('detail')!r}")

    ws4 = box / "hc-junk"
    res4 = _run_task_watcher_check(hc, ws4, "not-a-pid\n", "bash src/watch-tasks-stream.sh")
    check("CONTROL: a genuinely unreadable sentinel still warns as unreadable",
          res4.get("status") == "warn" and "unreadable PID sentinel" in res4.get("detail", ""),
          f"got {res4.get('status')!r} / {res4.get('detail')!r}")


# --- F: no private parser survives -------------------------------------------

def case_no_private_parser() -> None:
    """Structural, per REVIEW.md lesson 14: two copies that currently agree pass
    every behavioural test, so the duplication itself has to be pinned."""
    pat = re.compile(r"int\(\s*[A-Za-z_][A-Za-z_0-9]*(?:_file|path|sp|pf)?\s*\.read_text\(\)")
    for rel in ("src/health-check.py", "src/services_status.py"):
        hits = [f"{rel}:{i}" for i, ln in enumerate((REPO / rel).read_text().splitlines(), 1)
                if pat.search(ln)]
        check(f"{rel} keeps no private sentinel parser", not hits, f"{hits}")
    src = (REPO / "src" / "util_paths.py").read_text()
    check("CONTROL: the scanner CAN fire (it matches the shape it forbids)",
          bool(pat.search("    pid = int(pid_file.read_text().strip())")),
          "the pattern matches nothing, so the two zeros above are vacuous")
    check("util_paths owns read_sentinel_pid and read_sentinel_record",
          "def read_sentinel_pid" in src and "def read_sentinel_record" in src)


# --- G: the shared ownership policy ------------------------------------------

def case_ownership_policy(state: Path) -> None:
    """src/watcher_identity.py is the ONE policy restart.sh and health-check ask.
    Its refusals are the inputs a containment check used to accept."""
    import watcher_identity as wi

    pf = state / "own.pid"
    write_record(pf, 4242, instance="", inc="inc-G", code="/x/src/watch-tasks-stream.sh",
                 ws="/x/ws")
    marker = state / "own.incarnation"
    marker.write_text("inc-G\n")

    # The shell names the marker beside the sentinel; the policy is handed that
    # path by its caller, so the two spellings must agree on one file.
    shell_marker = subprocess.run(
        ["bash", "-c", f'. "{SENTINEL_SH}"; sentinel_incarnation_path "{pf}"'],
        capture_output=True, text=True).stdout.strip()
    check("the shell and the policy name the same incarnation marker",
          shell_marker == str(marker), f"shell said {shell_marker!r}")

    got = wi.confirm_record(pf, "", "/x/ws", str(marker))
    check("a COMPLETE record naming this install confirms",
          got == (4242, "/x/src/watch-tasks-stream.sh"), f"got {got!r}")

    for field in ("instance", "incarnation", "code_path", "workspace"):
        cut = state / f"no-{field}.pid"
        cut.write_text("\n".join(ln for ln in pf.read_text().splitlines()
                                  if not ln.startswith(f"{field}=")) + "\n")
        try:
            wi.confirm_record(cut, "", "/x/ws", str(marker))
            check(f"a record with no {field} is REFUSED", False, "it confirmed")
        except wi.Refused as exc:
            check(f"a record with no {field} is REFUSED", str(exc).startswith(f"{field}:"),
                  f"reason was {exc}")

    legacy = state / "legacy-only.pid"
    legacy.write_text("4242\n")
    try:
        wi.confirm_record(legacy, "", "/x/ws", str(marker))
        check("a pid-only sentinel is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("a pid-only sentinel is REFUSED", "records a pid only" in str(exc), str(exc))

    code = "/x/src/watch-tasks-stream.sh"
    for label, argv in (
            ("an interpreter carrying the path as DATA", f"python3 -c pass {code}"),
            ("a shell whose executed slot is a flag", f"bash -c 'sleep 1' {code}"),
            ("a stranger that merely mentions it", f"/bin/sleep 30 {code}")):
        try:
            wi.confirm_process(4242, argv, code)
            check(f"{label} is REFUSED", False, "it confirmed")
        except wi.Refused as exc:
            check(f"{label} is REFUSED", "argv:" in str(exc), str(exc))
    try:
        wi.confirm_process(4242, "/bin/bash /other/watch-tasks-stream.sh", code)
        check("another checkout's watcher is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("another checkout's watcher is REFUSED", "code_path:" in str(exc), str(exc))
    try:
        wi.confirm_process(4242, f"bash {code}", code)
        check("CONTROL: the real invocation DOES confirm", True)
    except wi.Refused as exc:
        check("CONTROL: the real invocation DOES confirm", False, str(exc))
    check("CONTROL: the documented relative start confirms too",
          wi.runs_code_path("bash src/watch-tasks-stream.sh", code))

    # The remaining refusals a signaller can hit, each named by its own check.
    for label, fixture, want in (
            ("a sentinel whose line 1 is junk", "not-a-pid\n", "line 1 of"),
            ("an EMPTY sentinel", "", "line 1 of")):
        bad = state / f"bad-policy-{len(label)}.pid"
        bad.write_text(fixture)
        try:
            wi.confirm_record(bad, "", "/x/ws", str(marker))
            check(f"{label} is REFUSED", False, "it confirmed")
        except wi.Refused as exc:
            check(f"{label} is REFUSED", want in str(exc), str(exc))
    try:
        wi.confirm_record(pf, "", "", str(marker))
        check("a scope that resolved NO workspace is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("a scope that resolved NO workspace is REFUSED", "workspace:" in str(exc), str(exc))
    for label, value, want in (("an empty code_path", "code_path=", "code_path:"),
                               ("an empty incarnation", "incarnation=", "incarnation:")):
        key = value.split("=")[0]
        blank = state / f"blank-{key}.pid"
        blank.write_text("\n".join(
            (value if ln.startswith(f"{key}=") else ln) for ln in pf.read_text().splitlines()) + "\n")
        try:
            wi.confirm_record(blank, "", "/x/ws", str(marker))
            check(f"{label} is REFUSED", False, "it confirmed")
        except wi.Refused as exc:
            check(f"{label} is REFUSED", want in str(exc), str(exc))
    try:
        wi.confirm_record(pf, "", "/x/ws", str(state / "no-such.incarnation"))
        check("a missing incarnation marker is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("a missing incarnation marker is REFUSED", "exposes no marker" in str(exc), str(exc))
    other = state / "other.incarnation"
    other.write_text("inc-OLD\n")
    try:
        wi.confirm_record(pf, "", "/x/ws", str(other))
        check("a marker from a PREVIOUS incarnation is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("a marker from a PREVIOUS incarnation is REFUSED",
              "the live marker says" in str(exc), str(exc))
    adir = state / "dir.incarnation"
    adir.mkdir(exist_ok=True)
    try:
        wi.confirm_record(pf, "", "/x/ws", str(adir))
        check("an unreadable incarnation marker is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("an unreadable incarnation marker is REFUSED", "incarnation:" in str(exc), str(exc))

    # The real argv LIST, when a caller could read one: the authoritative form.
    check("a vector whose executed slot is the watcher confirms",
          wi.is_watcher_argv("", ["/bin/bash", code]) is True)
    check("  ...and one whose executed slot is a flag does not",
          wi.is_watcher_argv("", ["/bin/bash", "-c", code]) is False)
    check("  ...nor one whose argv[0] is not a shell",
          wi.is_watcher_argv("", ["/usr/bin/python3", code]) is False)
    check("a one-token argv proves nothing", wi.is_watcher_argv("bash") is False)
    check("executed_script returns None when the slot is a flag",
          wi.executed_script("bash -c true") is None)
    check("  ...and the vector form keeps the operands",
          wi.executed_script("", ["/bin/bash", code, "--flag"]) == code)
    check("runs_code_path refuses an empty code_path",
          wi.runs_code_path(f"bash {code}", "") is False)

    check("a shell running some other script alone is not our watcher",
          wi.is_watcher_argv("/bin/bash /x/other.sh") is False)
    check("  ...and one that merely mentions ours later is UNPROVABLE, not proven",
          wi.is_watcher_argv(f"/bin/bash /x/other.sh {code}") is None)
    check("executed_script returns None when argv[0] is no shell",
          wi.executed_script(f"python3 {code}") is None)
    check("  ...and None for a flattened argv with operands it cannot split",
          wi.executed_script("/bin/bash a b") is None)
    peer = state / "peer-instance.pid"
    write_record(peer, 4242, instance="worker-1", inc="inc-G", code=code, ws="/x/ws")
    try:
        wi.confirm_record(peer, "", "/x/ws", str(marker))
        check("another INSTANCE's record is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("another INSTANCE's record is REFUSED",
              "this scope resolves" in str(exc), str(exc))
    elsewhere = state / "elsewhere.pid"
    write_record(elsewhere, 4242, instance="", inc="inc-G", code=code, ws="/other/install")
    try:
        wi.confirm_record(elsewhere, "", "/x/ws", str(marker))
        check("another install's workspace is REFUSED", False, "it confirmed")
    except wi.Refused as exc:
        check("another install's workspace is REFUSED",
              "this install is" in str(exc), str(exc))
    try:
        wi.confirm_process(4242, f"/bin/bash /x/other.sh {code}", code)
        check("an UNPROVABLE argv is REFUSED, not confirmed", False, "it confirmed")
    except wi.Refused as exc:
        check("an UNPROVABLE argv is REFUSED, not confirmed",
              "unprovable identity" in str(exc), str(exc))
    locked = state / "locked.incarnation"
    locked.write_text("inc-G\n")
    locked.chmod(0o000)
    try:
        open(locked).read()
        readable = True
    except OSError:
        readable = False
    if not readable:
        try:
            wi.confirm_record(pf, "", "/x/ws", str(locked))
            check("an incarnation marker we cannot OPEN is REFUSED", False, "it confirmed")
        except wi.Refused as exc:
            check("an incarnation marker we cannot OPEN is REFUSED",
                  "unreadable" in str(exc), str(exc))
    locked.chmod(0o644)

    # The CLI the shell bridge calls, in-process: rc 0 confirms, rc 1 refuses.
    check("CLI owner-pid confirms a complete record",
          wi.main(["owner-pid", "--sentinel", str(pf), "--instance", "",
                   "--workspace", "/x/ws", "--incarnation-file", str(marker)]) == 0)
    check("CLI owner-pid refuses a pid-only sentinel",
          wi.main(["owner-pid", "--sentinel", str(legacy), "--instance", "",
                   "--workspace", "/x/ws", "--incarnation-file", str(marker)]) == 1)
    check("CLI runs-watcher confirms the real invocation",
          wi.main(["runs-watcher", "--pid", "4242", "--argv", f"bash {code}",
                   "--code-path", code]) == 0)
    check("CLI runs-watcher refuses the DATA argument",
          wi.main(["runs-watcher", "--pid", "4242", "--argv", f"python3 -c pass {code}",
                   "--code-path", code]) == 1)


def main() -> int:
    print("watcher sentinel record — writer/reader contract:")
    with tempfile.TemporaryDirectory(prefix="sentinel-readers-") as td:
        box = Path(td)
        state = box / "state"
        state.mkdir(parents=True, exist_ok=True)
        case_reader(state)
        case_services_status(state)
        case_health_check(state, box)
        case_no_private_parser()
        case_ownership_policy(state)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All sentinel reader-contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
