#!/usr/bin/env python3
"""Report, and with --reap stop, orphaned processes left in agent scratchpads.

A process is a candidate only when ALL of these hold:
  - it is owned by the user running this script;
  - its parent is init (PPID 1), i.e. whatever started it is gone;
  - it has run longer than the minimum age (manifest config, default 2 h);
  - its working directory is inside an agent scratchpad,
    <tmp>/claude-<uid>/<project>/<session>/scratchpad (realpaths compared; a
    symlinked scratchpad, or one resolving outside claude-<uid>, is skipped);
  - the Claude Code session that owns that scratchpad has ended (owner_state);
    an owner whose state cannot be read is treated as alive;
  - no process in its group holds a listening TCP socket or a UDP socket, so a
    deliberate server is spared even after its session ended (unreadable counts
    as holding one);
  - it is not on the never-touch list (PROTECTED_SUBSTRINGS, PROTECTED_EXECUTABLES,
    apps under Applications; see SKILL.md), launchd jobs, this script or one of
    its ancestors.

Launchd jobs also have PPID 1 on macOS. They are excluded twice: every pid
`launchctl list` reports is skipped, and a job's working directory is `/` or the
plist's WorkingDirectory, never a per-session scratchpad, so the cwd rule alone
already keeps them out when launchctl cannot be read.

Stopping is per target, never by pattern. Immediately before each signal the
target is re-read (pid, start time, cwd, command); any change aborts it, which
is what makes PID reuse safe. A whole process group is signalled only when
every member of the group is itself a candidate; otherwise the single pid is.
SIGTERM first, a bounded grace, then SIGKILL for survivors after a second
identical re-check.

--spotlight drops an empty `.metadata_never_index` into each scratchpad root and
into each `claude-<uid>` directory above them, so Spotlight skips test churn.

Exit status is 0 for every run that parsed its arguments (it runs from cron);
2 on a usage error.

Run: python3 skills/process-hygiene/scripts/reap_orphans.py [--reap] [--spotlight]
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import fcntl
import glob
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]
MANIFEST = SKILL_DIR / "manifest.json"

MIN_AGE_KEY = "PROCESS_HYGIENE_MIN_AGE_S"
GRACE_KEY = "PROCESS_HYGIENE_GRACE_S"
DEFAULT_TMP_BASES = ("/tmp",)  # on macOS a symlink; roots are compared as realpaths
NEVER_INDEX = ".metadata_never_index"
LOG_NAME = "process-hygiene.log"

PROTECTED_SUBSTRINGS = (
    "sutando-core", "sutando-worker", "sutando-tmux", "sutando-relay", "sutando-station",
    "run-bridge.sh", "remote_gateway_bridge", "-bridge.py", "watch-tasks-stream.sh", "task-notifier.sh",
    "voice-agent", "core_heartbeat", "mcp-server", "mcp_server", "modelcontextprotocol", "-mcp",
    "claude-code", "Google Chrome", "Chromium", "chrome-headless-shell",
)
MIN_REAP_AGE_S = 600
# An installed app's own binary. A toolchain inside a bundle (Xcode's Contents/Developer:
# swift-frontend, its python3) or a framework's Python.app is not the user's app.
APP_ROOTS = ("/Applications/", os.path.join(os.path.expanduser("~"), "Applications") + "/")
APP_BINARY = re.compile(r"^[^\n]*?\.app/Contents/MacOS/[^/]*$")
PROTECTED_EXECUTABLES = frozenset({
    "tmux", "claude", "launchd", "chrome", "chromium", "chromium-browser", "google-chrome",
})

PS_ARGV = ["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "uid=", "-o", "stat=",
           "-o", "etime=", "-o", "lstart=", "-o", "command="]
C_LOCALE = {"LC_ALL": "C", "LANG": "C"}


ALIVE, GONE, UNKNOWN = "alive", "gone", "unknown"


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    uid: int
    stat: str
    age_s: Optional[int]
    lstart: str
    command: str

    @property
    def zombie(self) -> bool:
        return self.stat.startswith("Z")


@dataclass
class Candidate:
    proc: Proc
    cwd: str

    def identity(self) -> Tuple[int, str, str, str]:
        return (self.proc.pid, self.proc.lstart, self.cwd, self.proc.command)


@dataclass
class Target:
    kind: str  # "group" or "pid"
    ident: int  # pgid for a group, pid otherwise
    members: Dict[int, Candidate] = field(default_factory=dict)


@dataclass
class Context:
    uid: int
    min_age_s: float
    grace_s: float
    roots: List[str]
    snapshot: Callable[[], Optional[Dict[int, Proc]]]
    read_cwd: Callable[[int], Optional[str]]
    log: Callable[[str], None]
    protected_pids: Set[int] = field(default_factory=set)
    own_pgid: int = -1
    orphan_ppid: int = 1
    kill: Callable[[int, int], None] = os.kill
    killpg: Callable[[int, int], None] = os.killpg
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    owner_state: Callable[[str], str] = lambda root: UNKNOWN
    listening: Callable[[int], Optional[bool]] = lambda pid: None


def manifest_config(key: str, manifest: Path = MANIFEST) -> Optional[str]:
    try:
        value = json.loads(manifest.read_text()).get("config", {}).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return None if value is None else str(value)


def setting(cli_value: Optional[float], key: str, env=None, manifest: Path = MANIFEST) -> float:
    """CLI > env > manifest config, per skills/MANIFEST.md. Raises ValueError when unusable."""
    if cli_value is not None:
        value: object = cli_value
    else:
        env = os.environ if env is None else env
        value = env.get(key) or manifest_config(key, manifest)
    if value is None:
        raise ValueError(f"{key} is not set in the CLI, the environment or {manifest.name}")
    number = float(value)
    if number < 0:
        raise ValueError(f"{key} must be >= 0, got {value}")
    return number


def parse_etime(text: str) -> Optional[int]:
    try:
        days = 0
        if "-" in text:
            head, text = text.split("-", 1)
            days = int(head)
        fields = [int(x) for x in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(fields) <= 3:
        return None
    fields = [0] * (3 - len(fields)) + fields
    return days * 86400 + fields[0] * 3600 + fields[1] * 60 + fields[2]


def parse_ps(text: str) -> Dict[int, Proc]:
    procs: Dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 11)
        if len(parts) < 11:
            continue
        try:
            pid, ppid, pgid, uid = (int(x) for x in parts[:4])
        except ValueError:
            continue
        procs[pid] = Proc(pid=pid, ppid=ppid, pgid=pgid, uid=uid, stat=parts[4], age_s=parse_etime(parts[5]),
                          lstart=" ".join(parts[6:11]), command=parts[11] if len(parts) > 11 else "")
    return procs


def snapshot(run=subprocess.run) -> Optional[Dict[int, Proc]]:
    """Every process, or None when ps could not be read (never an empty table)."""
    try:
        out = run(PS_ARGV, capture_output=True, text=True, timeout=30, env={**os.environ, **C_LOCALE})
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    procs = parse_ps(out.stdout)
    return procs or None


def read_cwd(pid: int, system: Optional[str] = None, run=subprocess.run, readlink=os.readlink) -> Optional[str]:
    system = system or sys.platform
    if system.startswith("linux"):
        try:
            path = readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
        if path.endswith(" (deleted)"):
            return None
        return os.path.realpath(path)
    try:
        out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                  timeout=15, env={**os.environ, **C_LOCALE})
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return os.path.realpath(line[1:])
    return None


def launchd_pids(system: Optional[str] = None, run=subprocess.run) -> Set[int]:
    if not (system or sys.platform).startswith("darwin"):
        return set()
    try:
        out = run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.stdout.splitlines():
        head = line.split(None, 1)[0] if line.strip() else ""
        if head.isdigit():
            pids.add(int(head))
    return pids


def scratchpad_roots(uid: int, tmp_bases: Sequence[str]) -> List[str]:
    """Real scratchpad directories under each <base>/claude-<uid>. A symlink anywhere
    below claude-<uid>, or a claude-<uid> that is a symlink or not this user's,
    would widen the reach to its target, so such hits are skipped."""
    roots = set()
    for base in tmp_bases:
        top = os.path.join(base, f"claude-{uid}")
        try:
            st = os.lstat(top)
        except OSError:
            continue
        if os.path.islink(top) or st.st_uid != uid:
            continue
        real_top = os.path.realpath(top)
        for hit in glob.glob(os.path.join(glob.escape(top), "*", "*", "scratchpad")):
            rel = os.path.relpath(hit, top).split(os.sep)
            if any(os.path.islink(os.path.join(top, *rel[:i + 1])) for i in range(len(rel))):
                continue
            real = os.path.realpath(hit)
            if os.path.isdir(real) and real.startswith(real_top + os.sep):
                roots.add(real)
    return sorted(roots)


def inside(path: str, roots: Sequence[str]) -> bool:
    return owning_root(path, roots) is not None


def protected_reason(command: str) -> Optional[str]:
    for needle in PROTECTED_SUBSTRINGS:
        if needle in command:
            return f"never-touch ({needle})"
    for app_root in APP_ROOTS:
        exe = command.split(" -", 1)[0].rstrip()
        if command.startswith(app_root) and APP_BINARY.match(exe) and "/Contents/Developer/" not in exe:
            return "never-touch (an app under Applications)"
    argv0 = command.split(None, 1)[0] if command.strip() else ""
    if os.path.basename(argv0).lstrip("-") in PROTECTED_EXECUTABLES:
        return f"never-touch ({os.path.basename(argv0)})"
    return None


def ancestors(pid: int, procs: Dict[int, Proc]) -> Set[int]:
    seen: Set[int] = set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        pid = procs[pid].ppid
    return seen


def utc_start(pid: int, run=subprocess.run) -> Optional[str]:
    """`ps -o lstart=` in UTC and the C locale: the format of a session marker's procStart."""
    try:
        out = run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=15,
                  env={**os.environ, **C_LOCALE, "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(out.stdout.split()) or None


def default_config_dirs() -> List[str]:
    """Claude config dirs whose sessions/ and projects/ may own a scratchpad."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR", ""), os.path.join(os.path.expanduser("~"), ".claude")]
    try:
        sys.path.insert(0, str(REPO))
        from src.sutando_config import resolve_core_config_dirs  # noqa: E402
        dirs += [e.get("value", "") for e in resolve_core_config_dirs() if e.get("type") == "claude"]
    except Exception:  # a missing or invalid config adds nothing; the env and home dirs remain
        pass
    seen: List[str] = []
    for d in dirs:
        if d and os.path.isdir(d) and os.path.realpath(d) not in seen:
            seen.append(os.path.realpath(d))
    return seen


CLEAR_SLACK_S = 120


def project_slug(cwd: str) -> str:
    """Claude Code's project directory name for a cwd: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def utc_epoch(lstart: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def created_at(path: str) -> float:
    st = os.stat(path)
    return getattr(st, "st_birthtime", None) or min(st.st_mtime, st.st_ctime)


def owner_state(root: str, config_dirs: Sequence[str], min_age_s: float,
                start_of=utc_start, now=time.time) -> str:
    """Whether the session owning <tmp>/claude-<uid>/<slug>/<session>/scratchpad is ALIVE, GONE or UNKNOWN.

    Alive: a Claude Code session marker (<config>/sessions/<pid>.json) names the
    session and its pid is running with the marker's procStart, or the session
    transcript (<config>/projects/<slug>/<session>.jsonl) changed within the
    minimum age. Unknown: anything that cannot be read, a transcript found
    nowhere, or a live process in the same project that was already running
    when this session's directory was created: /clear gives a running process
    a new sessionId and leaves no link to the old one, so that process may
    still own this scratchpad. Gone: the transcript exists and none of that holds."""
    parts = Path(root).parts
    if len(parts) < 3 or parts[-1] != "scratchpad":
        return UNKNOWN
    slug, session = parts[-3], parts[-2]
    transcript_found = False
    sessions_seen = False
    others = []  # (cwd or None, start epoch or None) of live processes on other sessions
    try:
        for cfg in config_dirs:
            sessions = os.path.join(cfg, "sessions")
            try:
                is_dir = stat.S_ISDIR(os.stat(sessions).st_mode)
            except FileNotFoundError:
                is_dir = False
            if is_dir:
                sessions_seen = True
                for name in sorted(os.listdir(sessions)):
                    if not name.endswith(".json"):
                        continue
                    with open(os.path.join(sessions, name)) as fh:
                        data = json.load(fh)
                    if not isinstance(data, dict):
                        return UNKNOWN
                    pid, proc_start = data.get("pid"), data.get("procStart")
                    if not isinstance(pid, int) or not isinstance(proc_start, str):
                        if data.get("sessionId") == session:
                            return UNKNOWN
                        others.append((data.get("cwd"), None))
                        continue
                    if start_of(pid) != " ".join(proc_start.split()):
                        continue
                    if data.get("sessionId") == session:
                        return ALIVE
                    others.append((data.get("cwd"), utc_epoch(proc_start)))
            try:
                mtime = os.stat(os.path.join(cfg, "projects", slug, f"{session}.jsonl")).st_mtime
            except FileNotFoundError:
                continue
            transcript_found = True
            if now() - mtime < min_age_s:
                return ALIVE
        if not (transcript_found and sessions_seen):
            return UNKNOWN
        born = created_at(os.path.dirname(root))
        for cwd, started in others:
            same_project = not isinstance(cwd, str) or project_slug(cwd) == project_slug(slug)
            if same_project and (started is None or started <= born + CLEAR_SLACK_S):
                return UNKNOWN
    except (OSError, ValueError):  # any probe that fails, on any Python version, is unknown
        return UNKNOWN
    return GONE


def owning_root(cwd: str, roots: Sequence[str]) -> Optional[str]:
    for r in roots:
        if cwd == r or cwd.startswith(r.rstrip(os.sep) + os.sep):
            return r
    return None


def classify(proc: Proc, ctx: Context) -> Tuple[Optional[str], Optional[str]]:
    """(reason it is skipped or None, its cwd when read). Cheap rules run before lsof."""
    if proc.uid != ctx.uid:
        return "another user", None
    if proc.pid in ctx.protected_pids:
        return "this run, an ancestor, or a launchd job", None
    if proc.zombie:
        return "zombie", None
    if proc.ppid != ctx.orphan_ppid:
        return "has a live parent", None
    if proc.age_s is None or proc.age_s < ctx.min_age_s:
        return "younger than the minimum age", None
    reason = protected_reason(proc.command)
    if reason:
        return reason, None
    cwd = ctx.read_cwd(proc.pid)
    if cwd is None:
        return "cwd unreadable", None
    root = owning_root(cwd, ctx.roots)
    if root is None:
        return "cwd outside every scratchpad", cwd
    state = ctx.owner_state(root)
    if state != GONE:
        return f"owning session {state}", cwd
    return None, cwd


def _proc_listens(pid: int, read=lambda p: Path(p).read_text(), listdir=os.listdir,
                  readlink=os.readlink) -> Optional[bool]:
    """Linux: a socket inode of the pid is a LISTEN tcp row or any udp row in its own net namespace."""
    try:
        inodes = set()
        for fd in listdir(f"/proc/{pid}/fd"):
            target = readlink(f"/proc/{pid}/fd/{fd}")
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        if not inodes:
            return False
        for table in ("tcp", "tcp6", "udp", "udp6"):
            try:
                rows = read(f"/proc/{pid}/net/{table}").splitlines()[1:]
            except FileNotFoundError:
                continue
            for row in rows:
                cols = row.split()
                if len(cols) > 9 and cols[9] in inodes and (table.startswith("udp") or cols[3] == "0A"):
                    return True
        return False
    except OSError:
        return None


def listening(pid: int, system: Optional[str] = None, run=subprocess.run, linux=_proc_listens) -> Optional[bool]:
    """True when the pid holds a listening TCP or any UDP socket; None when that cannot be read."""
    if (system or sys.platform).startswith("linux"):
        return linux(pid)
    try:
        out = run(["lsof", "-w", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-iUDP", "-Fn"],
                  capture_output=True, text=True, timeout=15, env={**os.environ, **C_LOCALE})
    except (OSError, subprocess.SubprocessError):
        return None
    if any(line.startswith("n") for line in out.stdout.splitlines()):
        return True
    return False if out.returncode in (0, 1) and not out.stderr.strip() else None


def spare_servers(cands: Dict[int, Candidate], procs: Dict[int, Proc], ctx: Context) -> Dict[int, Candidate]:
    """Drop every candidate whose process group has a member holding a server socket, or one unreadable."""
    kept: Dict[int, Candidate] = {}
    verdict: Dict[int, bool] = {}
    for pid, cand in cands.items():
        pgid = cand.proc.pgid
        if pgid not in verdict:
            members = group_members(procs, pgid) | {pid}
            verdict[pgid] = all(ctx.listening(m) is False for m in sorted(members))
            if not verdict[pgid]:
                ctx.log(f"spared-server pgid={pgid} pids={','.join(str(m) for m in sorted(members))}")
        if verdict[pgid]:
            kept[pid] = cand
    return kept


def find_candidates(procs: Dict[int, Proc], ctx: Context) -> Dict[int, Candidate]:
    found: Dict[int, Candidate] = {}
    for proc in procs.values():
        reason, cwd = classify(proc, ctx)
        if reason is None and cwd is not None:
            found[proc.pid] = Candidate(proc, cwd)
    return spare_servers(found, procs, ctx)


def group_members(procs: Dict[int, Proc], pgid: int) -> Set[int]:
    return {p.pid for p in procs.values() if p.pgid == pgid and not p.zombie}


def plan(cands: Dict[int, Candidate], procs: Dict[int, Proc], ctx: Context) -> List[Target]:
    targets: List[Target] = []
    for pgid in sorted({c.proc.pgid for c in cands.values()}):
        mine = {pid: c for pid, c in cands.items() if c.proc.pgid == pgid}
        if pgid > 1 and pgid != ctx.own_pgid and group_members(procs, pgid) == set(mine):
            targets.append(Target("group", pgid, mine))
        else:
            targets.extend(Target("pid", pid, {pid: c}) for pid, c in sorted(mine.items()))
    return targets


def unchanged(cand: Candidate, procs: Dict[int, Proc], ctx: Context) -> bool:
    now = procs.get(cand.proc.pid)
    if now is None or now.zombie or (now.lstart, now.command) != (cand.proc.lstart, cand.proc.command):
        return False
    return ctx.read_cwd(cand.proc.pid) == cand.cwd


def alive_members(target: Target, procs: Dict[int, Proc]) -> Set[int]:
    if target.kind == "group":
        return group_members(procs, target.ident)
    now = procs.get(target.ident)
    return {target.ident} if now is not None and not now.zombie else set()


def recheck(target: Target, ctx: Context, after_term: bool = False) -> Tuple[bool, Optional[Set[int]]]:
    """(still safe to signal, planned members alive now or None when ps failed).

    Before SIGTERM nothing may have left or joined; after it, members may only have left."""
    procs = ctx.snapshot()
    if procs is None:
        return False, None
    alive = alive_members(target, procs)
    expected = set(target.members)
    if not (alive <= expected if after_term else alive == expected):
        return False, alive & expected
    return all(unchanged(target.members[pid], procs, ctx) for pid in alive), alive


def still_running(target: Target, ctx: Context) -> bool:
    """Cheap poll during the grace: a planned member with its original start time is alive."""
    procs = ctx.snapshot()
    if procs is None:
        return True
    return any(procs[pid].lstart == target.members[pid].proc.lstart
               for pid in alive_members(target, procs) & set(target.members))


def describe(target: Target) -> str:
    first = next(iter(target.members.values()))
    cmd = first.proc.command.replace("\n", "\\n")
    cmd = cmd if len(cmd) <= 160 else cmd[:157] + "..."
    pids = ",".join(str(p) for p in sorted(target.members))
    return f"{target.kind}={target.ident} pids={pids} age={first.proc.age_s}s cwd={first.cwd} cmd={cmd!r}"


def send(target: Target, sig: int, ctx: Context, alive: Optional[Set[int]] = None) -> bool:
    """Signal a re-checked target. A group whose leader is a zombie can refuse
    killpg with EPERM; its members, just re-checked, are then signalled one by one."""
    try:
        if target.kind == "group":
            ctx.killpg(target.ident, sig)
        else:
            ctx.kill(target.ident, sig)
        return True
    except ProcessLookupError:
        return True
    except PermissionError as e:
        if target.kind != "group":
            ctx.log(f"signal-refused sig={sig} {describe(target)} err={e}")
            return False
    refused = False
    for pid in sorted(target.members if alive is None else alive):
        try:
            ctx.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            refused = True
            ctx.log(f"signal-refused sig={sig} pid={pid} err={e}")
    return not refused


def stop(target: Target, ctx: Context) -> str:
    ok, alive = recheck(target, ctx)
    if not ok:
        if target.kind == "group" and alive:
            ctx.log(f"group-changed {describe(target)}; signalling unchanged members one by one")
            singles = [Target("pid", pid, {pid: target.members[pid]}) for pid in sorted(alive)]
            outcomes = [stop(t, ctx) for t in singles]
            return "killed" if "killed" in outcomes else ("terminated" if "terminated" in outcomes else "aborted")
        ctx.log(f"abort-changed {describe(target)}")
        return "aborted"
    if not send(target, signal.SIGTERM, ctx, alive):
        return "aborted"
    ctx.log(f"sigterm {describe(target)}")
    deadline = ctx.clock() + ctx.grace_s
    while ctx.clock() < deadline and still_running(target, ctx):
        ctx.sleep(0.1)
    ok, alive = recheck(target, ctx, after_term=True)
    if alive is not None and not alive:
        ctx.log(f"terminated {target.kind}={target.ident}")
        return "terminated"
    if not ok:
        ctx.log(f"abort-kill-changed {describe(target)}")
        return "aborted"
    if not send(target, signal.SIGKILL, ctx, alive):
        return "aborted"
    ctx.log(f"sigkill {describe(target)} survivors={','.join(str(p) for p in sorted(alive or ()))}")
    return "killed"


def ensure_never_index(roots: Sequence[str], log: Callable[[str], None]) -> List[str]:
    dirs = sorted(set(roots) | {os.path.dirname(os.path.dirname(os.path.dirname(r))) for r in roots})
    created = []
    for d in dirs:
        marker = os.path.join(d, NEVER_INDEX)
        if os.path.exists(marker):
            continue
        try:
            with open(marker, "a"):
                pass
        except OSError as e:
            log(f"spotlight-marker-failed dir={d} err={e}")
            continue
        created.append(marker)
        log(f"spotlight-marker dir={d}")
    return created


def default_log_path() -> Path:
    sys.path.insert(0, str(REPO / "src"))
    from workspace_default import resolve_workspace  # noqa: E402
    return resolve_workspace(migrate=False) / "logs" / LOG_NAME


def file_logger(path: Path) -> Callable[[str], None]:
    def log(line: str) -> None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(f"{stamp} {line}\n")
        except OSError as e:
            print(f"process-hygiene: cannot write {path}: {e}", file=sys.stderr)
    return log


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Report (default) or stop orphaned processes in agent scratchpads.")
    p.add_argument("--reap", action="store_true", help="stop candidates (default: report only)")
    p.add_argument("--spotlight", action="store_true", help="drop .metadata_never_index into scratchpad roots")
    p.add_argument("--min-age", type=float, help=f"seconds; overrides {MIN_AGE_KEY}")
    p.add_argument("--grace", type=float, help=f"seconds between SIGTERM and SIGKILL; overrides {GRACE_KEY}")
    p.add_argument("--tmp-base", action="append", help="directory holding claude-<uid>/ (repeatable; "
                   "default /tmp, which covers its realpath)")
    p.add_argument("--claude-config-dir", action="append", help="Claude config dir holding sessions/ and "
                   "projects/ (repeatable; default CLAUDE_CONFIG_DIR, the home .claude, and the claude "
                   "core_config_dirs)")
    p.add_argument("--log", type=Path, help="log file (default <workspace>/logs/process-hygiene.log)")
    return p.parse_args(argv)


def run(args: argparse.Namespace, ctx: Context) -> Dict[str, int]:
    counts = {"candidates": 0, "terminated": 0, "killed": 0, "aborted": 0, "markers": 0}
    if args.spotlight:
        counts["markers"] = len(ensure_never_index(ctx.roots, ctx.log))
    procs = ctx.snapshot()
    if procs is None:
        print("process-hygiene: ps could not be read; nothing was examined or stopped")
        ctx.log("ps-unavailable; nothing examined")
        return counts
    me = os.getpid()
    ctx.protected_pids |= ancestors(me, procs) | {me}
    cands = find_candidates(procs, ctx)
    counts["candidates"] = len(cands)
    for target in plan(cands, procs, ctx):
        print(("stopping " if args.reap else "would stop ") + describe(target))
        if args.reap:
            counts[stop(target, ctx)] += 1
    mode = "reap" if args.reap else "report"
    summary = (f"mode={mode} roots={len(ctx.roots)} candidates={counts['candidates']} "
               f"terminated={counts['terminated']} killed={counts['killed']} aborted={counts['aborted']} "
               f"spotlight_markers={counts['markers']}")
    print(f"process-hygiene: {summary}")
    ctx.log(f"summary {summary}")
    return counts


def build_context(args: argparse.Namespace, log: Callable[[str], None], orphan_ppid: int = 1) -> Context:
    uid = os.getuid()
    bases = args.tmp_base or list(DEFAULT_TMP_BASES)
    min_age = setting(args.min_age, MIN_AGE_KEY)
    configs = [os.path.realpath(d) for d in args.claude_config_dir] if args.claude_config_dir \
        else default_config_dirs()
    cache: Dict[str, str] = {}

    def owner(root: str) -> str:
        if root not in cache:
            cache[root] = owner_state(root, configs, min_age)
        return cache[root]

    return Context(uid=uid, min_age_s=min_age, grace_s=setting(args.grace, GRACE_KEY),
                   roots=scratchpad_roots(uid, bases), snapshot=snapshot, read_cwd=read_cwd, log=log,
                   protected_pids=launchd_pids(), own_pgid=os.getpgrp(), orphan_ppid=orphan_ppid,
                   owner_state=owner, listening=listening)


def run_lock(path: Path):
    """An exclusive non-blocking flock beside the log, so a manual run never overlaps the cron run.
    Returns the open file holding it, or None when another run has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv: Optional[Sequence[str]] = None, orphan_ppid: int = 1) -> int:
    args = parse_args(argv)
    log_path = args.log or default_log_path()
    try:
        log = file_logger(log_path)
        ctx = build_context(args, log, orphan_ppid)
    except ValueError as e:
        print(f"process-hygiene: {e}", file=sys.stderr)
        return 2
    if args.reap and ctx.min_age_s < MIN_REAP_AGE_S:
        print(f"process-hygiene: --reap needs a minimum age of at least {MIN_REAP_AGE_S} s, "
              f"got {ctx.min_age_s:g}", file=sys.stderr)
        return 2
    lock = run_lock(log_path.with_name(LOG_NAME.replace(".log", ".lock")))
    if lock is None:
        print("process-hygiene: another run holds the lock; nothing done")
        log("skipped another run holds the lock")
        return 0
    try:
        run(args, ctx)
    except Exception as e:  # a cron job reports and exits 0; it never retries in a loop
        print(f"process-hygiene: run failed: {e!r}")
        log(f"run-failed err={e!r}")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
