#!/usr/bin/env python3
"""Find, classify and repair the agent CLI (Claude Code or Codex) the core runs on.

One owner for "is this install's core CLI usable, and if not, what fixes it".
Both runtime launchers call `ensure` before they replace a core; health-check
and `doctor` call `discover` to report the same verdict without acting.

Every candidate binary (PATH order, then the Sutando-managed install, then the
known install locations) is launched with `--version` and `--help`, and gets
exactly one class:

  ok              usable, within the tested major version
  newer-untested  usable, but a newer major than this engine was tested with
  too-old         below the minimum version
  known-bad       a version listed as broken for the core
  incompatible    reads a version, but lacks a flag the launcher passes
  broken          does not launch (not executable, dangling link, exec error)
  missing         no candidate at all

The first usable candidate in that order wins, so a host whose first `claude`
on PATH works keeps using exactly that binary.

Repairs never touch the user's own installs or config. The assisted repair
installs a compatible CLI into a Sutando-owned directory, only with explicit
consent, with retry and backoff, a disk-space check before the download, and
an atomic switch that keeps the previous version when anything fails. Success
is reported only after the new binary has actually been launched.

CLI:
  python3 src/runtime_compat.py doctor [--runtime claude|codex|all] [--json]
  python3 src/runtime_compat.py ensure --runtime R [--wait S] [--repair] [--yes]
  python3 src/runtime_compat.py repair --runtime R --yes [--json] [--consent-source app
                                      --consent-by MXID] [--plan-hash H]
  python3 src/runtime_compat.py which --runtime R [--enforce-auth]
  python3 src/runtime_compat.py notify-owner --runtime R [--json]
Exit codes for ensure and which: 0 usable (prints the binary path), 3 binary unusable,
4 login required, 2 usage error. The repair matrix:
docs/runtime-compat-repair-matrix.md.

With SUTANDO_RUNTIME_CONSENT_SURFACE=app the desktop app is where the owner answers:
a launch never waits and never DMs, probes and download retries are time-bounded, and
the app runs `repair --consent-source app` and, if its card goes unanswered, `notify-owner`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

OK = "ok"
NEWER_UNTESTED = "newer-untested"
TOO_OLD = "too-old"
KNOWN_BAD = "known-bad"
INCOMPATIBLE = "incompatible"
BROKEN = "broken"
MISSING = "missing"
UNPROBED = "unknown"
USABLE = (OK, NEWER_UNTESTED)
AUTH_LOGIN_VALUES = ("ok", "logged-out", "unknown")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNUSABLE = 3
EXIT_LOGIN = 4

PROBE_TIMEOUT_S = 30
INSTALL_TIMEOUT_S = 900
MIN_FREE_BYTES = 512 * 1024 * 1024
WAIT_POLL_S = 5.0
DOCTOR_SCHEMA = 1
APP_PROBE_TIMEOUT_S = 10
APP_DISCOVERY_CAP_S = 60
APP_RETRY_BUDGET_S = 30
REPAIR_PHASES = ("checking-disk", "downloading", "verifying", "switching", "done")

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_DISK_FULL_RE = re.compile(r"ENOSPC|no space left on device", re.I)
_NETWORK_RE = re.compile(
    r"ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|ECONNRESET|E403|E407|EPROTO|"
    r"network|proxy|certificate|SELF_SIGNED|UNABLE_TO_GET_ISSUER|getaddrinfo|"
    r"resolve host|Failed to connect|curl: \(\d+\)", re.I)
_NODE_MISSING_RE = re.compile(r"node: No such file|env: node|node: not found", re.I)

Version = Tuple[int, int, int]
Runner = Callable[..., "subprocess.CompletedProcess"]


def app_surface(env: Optional[dict] = None) -> bool:
    """The desktop app shows the repair card, so the engine must answer fast and never DM."""
    env = os.environ if env is None else env
    return env.get("SUTANDO_RUNTIME_CONSENT_SURFACE") == "app"


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    binary: str
    package: str
    min_version: Version
    tested_major: int
    required_flags: Tuple[str, ...]
    known_bad: Tuple[Version, ...] = ()
    manual_install: str = ""


RUNTIMES: Dict[str, RuntimeSpec] = {
    "claude": RuntimeSpec(
        name="claude", binary="claude", package="@anthropic-ai/claude-code",
        min_version=(2, 0, 0), tested_major=2,
        required_flags=("--name", "--dangerously-skip-permissions", "--add-dir",
                        "--settings", "--remote-control", "--chrome", "--plugin-dir",
                        "--resume", "--session-id"),
        manual_install="curl -fsSL https://claude.ai/install.sh | bash",
    ),
    "codex": RuntimeSpec(
        name="codex", binary="codex", package="@openai/codex",
        min_version=(0, 40, 0), tested_major=0,
        required_flags=("--cd", "--add-dir", "--sandbox", "--ask-for-approval",
                        "--search", "--no-alt-screen", "--config", "--model"),
        manual_install="npm install -g --prefix ~/.local @openai/codex",
    ),
}


@dataclass
class Candidate:
    path: str
    source: str
    status: str = MISSING
    version: Optional[str] = None
    missing_flags: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status in USABLE


@dataclass
class Report:
    runtime: str
    candidates: List[Candidate]
    selected: Optional[Candidate]
    interrupted: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.selected is not None:
            return self.selected.status
        return self.candidates[0].status if self.candidates else MISSING

    @property
    def problem(self) -> Optional[Candidate]:
        """The candidate whose failure the user should fix first."""
        if self.selected is not None or not self.candidates:
            return None
        return self.candidates[0]

    @property
    def selected_reason(self) -> str:
        """One line: the binary a launch uses and why, or why there is none."""
        binary = RUNTIMES[self.runtime].binary if self.runtime in RUNTIMES else self.runtime
        sel = self.selected
        skipped = self.candidates[:self.candidates.index(sel)] if sel else self.candidates
        why = "; ".join(f"{c.path} ({c.source}) is {c.status}: {c.detail}" for c in skipped)
        if sel is None:
            return f"no usable {binary}: {why or 'nothing is installed'}"
        chose = f"chose {sel.path} ({sel.source}) {sel.version}"
        if why:
            return f"{chose} because {why}"
        if sel.source != "PATH":
            return f"{chose} because no {binary} is on PATH"
        return f"{chose}; it passes doctor"

    def to_dict(self) -> dict:
        return {"runtime": self.runtime, "status": self.status,
                "selected": asdict(self.selected) if self.selected else None,
                "selected_reason": self.selected_reason,
                "candidates": [asdict(c) for c in self.candidates],
                "interrupted_updates": list(self.interrupted)}


@dataclass
class RepairStep:
    status: str
    reason: str
    command: str
    consent_required: bool
    touches: str
    verify: str
    assisted: Optional[str] = None
    assisted_touches: str = ""


@dataclass
class RepairResult:
    ok: bool
    reason: str
    detail: str = ""
    user_action: str = ""
    attempts: int = 0
    path: Optional[str] = None
    version: Optional[str] = None


def parse_version(text: str) -> Optional[Version]:
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _fmt(v: Version) -> str:
    return ".".join(str(p) for p in v)


def _run(argv: Sequence[str], *, env: Optional[dict] = None, timeout: float = PROBE_TIMEOUT_S,
         cwd: Optional[str] = None) -> "subprocess.CompletedProcess":
    return subprocess.run(list(argv), env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no output"


def managed_root(env: Optional[dict] = None) -> Path:
    """Sutando-owned install root; never a user or global prefix."""
    env = os.environ if env is None else env
    if env.get("SUTANDO_RUNTIMES_DIR"):
        return Path(env["SUTANDO_RUNTIMES_DIR"])
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "sutando" / "runtimes"


SYSTEM_LOCATIONS: Dict[str, Tuple[str, ...]] = {
    "claude": ("/opt/homebrew/bin/claude", "/usr/local/bin/claude"),
    "codex": ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"),
}


def known_locations(spec: RuntimeSpec, env: dict) -> List[str]:
    home = Path(env.get("HOME") or str(Path.home()))
    if spec.name == "claude":
        dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin"]
    else:
        dirs = [Path(env.get("CODEX_INSTALL_BIN") or str(home / ".npm-global" / "bin")),
                home / ".local" / "bin"]
    if "SUTANDO_RUNTIME_SYSTEM_DIRS" in env:
        system = [os.path.join(d, spec.binary)
                  for d in env["SUTANDO_RUNTIME_SYSTEM_DIRS"].split(os.pathsep) if d]
    else:
        system = list(SYSTEM_LOCATIONS.get(spec.name, ()))
    return [str(d / spec.binary) for d in dirs] + system


def candidate_paths(spec: RuntimeSpec, env: dict, root: Path) -> List[Tuple[str, str]]:
    """Every distinct install, in selection order: PATH, managed, then known locations."""
    out: List[Tuple[str, str]] = []
    seen = set()

    def add(path: str, source: str) -> None:
        if not os.path.lexists(path) or os.path.isdir(path):
            return
        key = os.path.realpath(path)
        if key in seen:
            return
        seen.add(key)
        out.append((path, source))

    for d in (env.get("PATH") or "").split(os.pathsep):
        if d:
            add(os.path.join(d, spec.binary), "PATH")
    add(str(root / "bin" / spec.binary), "managed")
    for p in known_locations(spec, env):
        add(p, "known")
    return out


def probe(path: str, spec: RuntimeSpec, *, source: str = "PATH", env: Optional[dict] = None,
          run: Runner = _run, timeout: float = PROBE_TIMEOUT_S, deadline: Optional[float] = None,
          now: Callable[[], float] = time.monotonic) -> Candidate:
    """Launch one binary and classify it. Never raises. With `deadline`, both calls
    together end by it (a `now()` value)."""
    c = Candidate(path=path, source=source)

    def limit() -> float:
        return timeout if deadline is None else max(0.001, min(timeout, deadline - now()))
    if not os.path.exists(path):
        c.status, c.detail = BROKEN, "points to a missing file (an update may have been interrupted)"
        return c
    if not os.access(path, os.X_OK):
        c.status, c.detail = BROKEN, "is not executable"
        return c
    try:
        t = limit()
        r = run([path, "--version"], env=env, timeout=t)
    except subprocess.TimeoutExpired:
        c.status, c.detail = BROKEN, f"--version did not answer within {t:g}s"
        return c
    except OSError as exc:
        c.status, c.detail = BROKEN, f"could not be launched ({exc.strerror or exc})"
        return c
    if r.returncode != 0:
        c.status = BROKEN
        c.detail = f"--version exited {r.returncode}: {_tail(r.stderr or r.stdout)}"
        return c
    version = parse_version(r.stdout or r.stderr)
    if version is None:
        c.status, c.detail = BROKEN, f"--version printed no version: {_tail(r.stdout)}"
        return c
    c.version = _fmt(version)
    if version in spec.known_bad:
        c.status, c.detail = KNOWN_BAD, f"{c.version} is known to break the core"
        return c
    if version < spec.min_version:
        c.status = TOO_OLD
        c.detail = f"{c.version} is older than the minimum {_fmt(spec.min_version)}"
        return c
    try:
        h = run([path, "--help"], env=env, timeout=limit())
    except (subprocess.TimeoutExpired, OSError) as exc:
        c.status, c.detail = BROKEN, f"--help could not run ({exc})"
        return c
    text = (h.stdout or "") + (h.stderr or "")
    c.missing_flags = [f for f in spec.required_flags if not re.search(re.escape(f) + r"\b", text)]
    if h.returncode != 0 or c.missing_flags:
        c.status = INCOMPATIBLE
        c.detail = (f"{c.version} lacks {', '.join(c.missing_flags)}" if c.missing_flags
                    else f"--help exited {h.returncode}")
        return c
    if version[0] > spec.tested_major:
        c.status = NEWER_UNTESTED
        c.detail = f"{c.version} is newer than the tested {spec.tested_major}.x"
        return c
    c.status, c.detail = OK, c.version
    return c


def interrupted_updates(root: Path, runtime: str, pid_alive: Callable[[int], bool]) -> List[str]:
    """Staging dirs a killed repair left behind; their owner is no longer running."""
    base = root / runtime
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.glob(".staging-*")):
        parts = d.name.split("-")
        pid = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else 0
        if not pid or not pid_alive(pid):
            out.append(str(d))
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover(runtime: str, *, env: Optional[dict] = None, run: Runner = _run,
             root: Optional[Path] = None, spec: Optional[RuntimeSpec] = None,
             pid_alive: Callable[[int], bool] = _pid_alive,
             now: Callable[[], float] = time.monotonic) -> Report:
    env = dict(os.environ if env is None else env)
    spec = spec or RUNTIMES[runtime]
    root = root or managed_root(env)
    cap = now() + APP_DISCOVERY_CAP_S if app_surface(env) else None
    cands: List[Candidate] = []
    for path, source in candidate_paths(spec, env, root):
        # Known locations stand in only when no PATH or managed install works.
        if source == "known" and any(c.usable for c in cands):
            break
        if cap is None:
            cands.append(probe(path, spec, source=source, env=env, run=run))
        elif now() >= cap:
            cands.append(Candidate(path, source, UNPROBED, detail="not probed in time"))
        else:
            cands.append(probe(path, spec, source=source, env=env, run=run, now=now,
                               timeout=APP_PROBE_TIMEOUT_S,
                               deadline=min(now() + APP_PROBE_TIMEOUT_S, cap)))
    selected = next((c for c in cands if c.usable), None)
    return Report(runtime, cands, selected, interrupted_updates(root, runtime, pid_alive))


def _npm_prefix_of(path: str, spec: RuntimeSpec) -> Optional[str]:
    bin_dir = os.path.dirname(path)
    prefix = os.path.dirname(bin_dir)
    if os.path.basename(bin_dir) == "bin" and os.path.isdir(
            os.path.join(prefix, "lib", "node_modules", spec.package)):
        return prefix
    return None


def plan_hash(step: RepairStep) -> str:
    """What the app's consent click refers to; a different plan needs a new click."""
    return hashlib.sha256(json.dumps(asdict(step), sort_keys=True).encode()).hexdigest()


def _assisted_cmd(runtime: str) -> str:
    return f"python3 src/runtime_compat.py repair --runtime {runtime} --yes"


CLAUDE_NATIVE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash -s stable"
CODEX_STANDALONE_INSTALL = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"


@dataclass(frozen=True)
class Installer:
    """One way to get a compatible CLI onto this host, and exactly what it changes."""
    kind: str
    argv: Tuple[str, ...]
    touches: str
    where: str


def choose_installer(spec: RuntimeSpec, env: dict, root: Path) -> Optional[Installer]:
    """npm into the managed root when npm exists; else the vendor's own Node-free path."""
    path = env.get("PATH")
    npm = shutil.which("npm", path=path)
    if npm:
        return Installer("npm", (npm,), f"{root} (a Sutando-owned folder) and the npm cache",
                         str(root))
    home = Path(env.get("HOME") or str(Path.home()))
    if spec.name == "claude" and shutil.which("curl", path=path) and shutil.which("bash", path=path):
        bash = shutil.which("bash", path=path)
        return Installer("native", (bash, "-c", f"set -o pipefail; {CLAUDE_NATIVE_INSTALL}"),
                         "the official Claude Code installer's own folders, "
                         f"{home / '.local' / 'bin' / 'claude'} and {home / '.local' / 'share' / 'claude'}, "
                         f"plus one link in {root}", str(home / ".local" / "bin" / spec.binary))
    brew = shutil.which("brew", path=path)
    if spec.name == "codex" and brew:
        return Installer("brew", (brew, "install", "--cask", "codex"),
                         f"Homebrew's codex cask under {os.path.dirname(os.path.dirname(brew))}, "
                         f"plus one link in {root}", os.path.join(os.path.dirname(brew), "codex"))
    return None


def _manual_without_installer(spec: RuntimeSpec) -> str:
    if spec.name == "claude":
        return f"Install curl, then run: {CLAUDE_NATIVE_INSTALL}"
    return (f"Run: {CODEX_STANDALONE_INSTALL}  (or install Homebrew and run: brew install --cask codex, "
            "or install Node.js 22+ and run: npm install -g @openai/codex)")


def plan(report: Report, spec: Optional[RuntimeSpec] = None,
         root: Optional[Path] = None, env: Optional[dict] = None) -> RepairStep:
    """The safe next action for this report's state."""
    spec = spec or RUNTIMES[report.runtime]
    env = dict(os.environ if env is None else env)
    root = root or managed_root(env)
    verify = f"python3 src/runtime_compat.py doctor --runtime {report.runtime}"
    if report.selected is not None and report.selected.status == NEWER_UNTESTED:
        return RepairStep(NEWER_UNTESTED,
                          f"{spec.binary} {report.selected.detail}; the core will try it.",
                          "No action needed. If the core misbehaves, install the tested "
                          f"version: npm install -g --prefix ~/.local {spec.package}@{spec.tested_major}",
                          False, "nothing", verify)
    if report.selected is not None:
        return RepairStep(OK, f"{spec.binary} {report.selected.version} at "
                          f"{report.selected.path} is compatible.", "No action needed.",
                          False, "nothing", verify)
    installer = choose_installer(spec, env, root)
    assisted = _assisted_cmd(report.runtime) if installer else None
    assisted_touches = installer.touches if installer else ""
    bad = report.problem
    if bad is None:
        command = spec.manual_install if installer else _manual_without_installer(spec)
        return RepairStep(MISSING, f"{spec.binary} is not installed.", command, True,
                          f"the install location the {spec.binary} installer chooses",
                          verify, assisted, assisted_touches)
    where = f"{spec.binary} at {bad.path} {bad.detail}."
    if bad.status == BROKEN and _NODE_MISSING_RE.search(bad.detail):
        return RepairStep(BROKEN, where + " Its Node.js runtime was not found.",
                          "Install Node.js 22 or newer (brew install node), or reinstall "
                          "the desktop app, which bundles it.", True,
                          "your Node.js install", verify)
    if bad.status == BROKEN:
        command = spec.manual_install if installer else _manual_without_installer(spec)
        return RepairStep(BROKEN, where + " Sutando will not use it.", command, True,
                          f"the install at {bad.path}", verify, assisted, assisted_touches)
    prefix = _npm_prefix_of(bad.path, spec)
    if prefix:
        command = f"npm install -g --prefix {shlex.quote(prefix)} {spec.package}@{spec.tested_major}"
    elif spec.name == "claude":
        command = f"{shlex.quote(bad.path)} update"
    elif installer:
        command = f"{spec.manual_install}@{spec.tested_major}"
    else:
        command = _manual_without_installer(spec)
    return RepairStep(bad.status, where, command, True, f"the install at {bad.path}",
                      verify, assisted, assisted_touches)


def _existing_ancestor(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _classify_failure(text: str) -> str:
    if _DISK_FULL_RE.search(text):
        return "disk-full"
    if _NETWORK_RE.search(text):
        return "blocked-download"
    return "install-failed"


def _swap_link(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically; a crash leaves the old link intact."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def assisted_install(runtime: str, *, env: Optional[dict] = None, run: Runner = _run,
                     root: Optional[Path] = None, spec: Optional[RuntimeSpec] = None,
                     attempts: int = 3, backoff_s: float = 2.0,
                     sleep: Callable[[float], None] = time.sleep,
                     disk_usage: Callable[[str], "shutil._ntuple_diskusage"] = shutil.disk_usage,
                     pid_alive: Callable[[int], bool] = _pid_alive,
                     min_free: int = MIN_FREE_BYTES, now: Callable[[], float] = time.time,
                     log: Callable[[str], None] = lambda _m: None,
                     progress: Callable[..., None] = lambda _phase, **_kw: None,
                     monotonic: Callable[[], float] = time.monotonic) -> RepairResult:
    """Install a compatible CLI. Callers hold the user's consent. Concurrent repairs of one
    runtime run one at a time; a waiter that finds a working result reuses it.
    `progress(phase, **detail)` is told each phase as it starts."""
    env = dict(os.environ if env is None else env)
    spec = spec or RUNTIMES[runtime]
    root = root or managed_root(env)
    retry = f"Then retry: {_assisted_cmd(runtime)}"
    installer = choose_installer(spec, env, root)
    if installer is None:
        return RepairResult(False, "no-installer", "neither npm nor the vendor installer can run here",
                            _manual_without_installer(spec))
    progress("checking-disk")
    free = disk_usage(str(_existing_ancestor(root))).free
    if free < min_free:
        need = (min_free - free) // (1024 * 1024) + 1
        return RepairResult(False, "disk-full", f"{free // (1024 * 1024)} MB free under {root}",
                            f"Free at least {need} MB on that disk. {retry}")
    base = root / runtime
    base.mkdir(parents=True, exist_ok=True)
    link = root / "bin" / spec.binary
    before = os.readlink(str(link)) if os.path.islink(str(link)) else None
    from file_lock import locked_file
    with locked_file(base / ".lock"):
        recover_swaps(root, runtime, pid_alive, log)
        for stale in interrupted_updates(root, runtime, pid_alive):
            log(f"removing an interrupted update at {stale}; the previous version was kept")
            shutil.rmtree(stale, ignore_errors=True)
        now_points = os.readlink(str(link)) if os.path.islink(str(link)) else None
        if now_points is not None and now_points != before:
            ready = probe(str(link), spec, source="managed", env=env, run=run)
            if ready.usable:
                log(f"another repair already installed {spec.binary} {ready.version}; using it")
                return RepairResult(True, "installed", f"{spec.binary} {ready.version} at {link}",
                                    "", 0, str(link), ready.version)
        budget = APP_RETRY_BUDGET_S if app_surface(env) else None
        ctx = _Ctx(spec, base, root, env, run, attempts, backoff_s, sleep, now, log, retry, installer,
                   progress, monotonic, budget)
        if installer.kind == "npm":
            return _install_npm(ctx)
        return _install_external(ctx)


@dataclass
class _Ctx:
    spec: RuntimeSpec
    base: Path
    root: Path
    env: dict
    run: Runner
    attempts: int
    backoff_s: float
    sleep: Callable[[float], None]
    now: Callable[[], float]
    log: Callable[[str], None]
    retry: str
    installer: Installer
    progress: Callable[..., None] = lambda _phase, **_kw: None
    monotonic: Callable[[], float] = time.monotonic
    budget_s: Optional[float] = None


def _run_with_retry(ctx: _Ctx, argv: Sequence[str], env: dict, before=None):
    """(None, attempts) on success, else (failed RepairResult, attempts). With a budget,
    no attempt starts after it and no backoff outlasts it."""
    end = ctx.monotonic() + ctx.budget_s if ctx.budget_s is not None else None
    last: Optional[Tuple[str, str]] = None
    for n in range(1, ctx.attempts + 1):
        if last is not None and end is not None and ctx.monotonic() >= end:
            return _failed(*last, n - 1, ctx.root, ctx.retry, ctx.installer), n - 1
        ctx.progress("downloading", attempt=n, attempts=ctx.attempts)
        if before:
            before()
        try:
            r = ctx.run(list(argv), env=env, timeout=INSTALL_TIMEOUT_S)
            rc, text = r.returncode, (r.stderr or "") + (r.stdout or "")
        except subprocess.TimeoutExpired:
            rc, text = -1, "the install timed out"
        except OSError as exc:
            rc, text = -1, f"the installer could not run: {exc}"
        if rc == 0:
            return None, n
        reason, detail = _classify_failure(text), _tail(text)
        last = (reason, detail)
        ctx.log(f"attempt {n}/{ctx.attempts} failed ({reason}): {detail}")
        left = end - ctx.monotonic() if end is not None else None
        if reason == "disk-full" or n == ctx.attempts or (left is not None and left <= 0):
            return _failed(reason, detail, n, ctx.root, ctx.retry, ctx.installer), n
        wait = ctx.backoff_s * (2 ** (n - 1))
        ctx.sleep(wait if left is None else min(wait, left))
    raise AssertionError("unreachable")  # pragma: no cover


def _install_npm(ctx: _Ctx) -> RepairResult:
    spec, base, env = ctx.spec, ctx.base, ctx.env
    staging = base / f".staging-{os.getpid()}-{int(ctx.now())}"
    npm_env = dict(env, npm_config_update_notifier="false", npm_config_fund="false",
                   npm_config_audit="false")
    if ctx.budget_s is not None:
        npm_env["npm_config_fetch_retries"] = "0"  # the attempt counter is the only retry loop
    argv = [ctx.installer.argv[0], "install", "-g", "--prefix", str(staging),
            f"{spec.package}@{spec.tested_major}"]
    try:
        failed, n = _run_with_retry(ctx, argv, npm_env,
                                    before=lambda: shutil.rmtree(staging, ignore_errors=True))
        if failed:
            return failed
        ctx.progress("verifying")
        staged = probe(str(staging / "bin" / spec.binary), spec, source="staged", env=env, run=ctx.run)
        if not staged.usable:
            return RepairResult(False, "verify-failed", f"the new {spec.binary} {staged.detail}",
                                f"The previous version was kept. Install it yourself: "
                                f"{spec.manual_install}", n)
        final = base / "versions" / staged.version
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists() and probe(str(final / "bin" / spec.binary), spec, env=env, run=ctx.run).usable:
            ctx.log(f"{spec.binary} {staged.version} is already installed and works; reusing it")
            return _publish(ctx, final / "bin" / spec.binary, n)
        return _publish(ctx, final / "bin" / spec.binary, n, staging=staging, final=final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _install_external(ctx: _Ctx) -> RepairResult:
    """The vendor's own installer, which picks its own folder; Sutando then links to it."""
    failed, n = _run_with_retry(ctx, ctx.installer.argv, ctx.env)
    if failed:
        return failed
    target = Path(ctx.installer.where)
    ctx.progress("verifying")
    got = probe(str(target), ctx.spec, source="installed", env=ctx.env, run=ctx.run)
    if not got.usable:
        return RepairResult(False, "verify-failed", f"the installed {ctx.spec.binary} {got.detail}",
                            f"Install it yourself: {_manual_without_installer(ctx.spec)}", n)
    return _publish(ctx, target, n)


_rename = os.rename
_rmtree = shutil.rmtree


class _Unusable(Exception):
    pass


def _ino(path: Optional[str]) -> Optional[int]:
    try:
        return os.stat(path).st_ino if path else None
    except OSError:
        return None


def _write_journal(journal: Path, j: dict) -> None:
    """Atomic and fsync'd: recovery acts on this record after a power loss too."""
    tmp = journal.with_name(journal.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(j, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(journal))
    try:
        fd = os.open(str(journal.parent), os.O_RDONLY)
    except OSError:  # pragma: no cover - a platform without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - a filesystem that refuses a directory fsync
        pass
    finally:
        os.close(fd)


def _publish(ctx: _Ctx, target: Path, n: int, staging: Optional[Path] = None,
             final: Optional[Path] = None) -> RepairResult:
    """Move a staged version into place and switch the link, as one transaction.
    A journal written first lets a later run undo it if this process dies midway."""
    spec = ctx.spec
    ctx.progress("switching")
    link = ctx.root / "bin" / spec.binary
    tag = f"{os.getpid()}-{int(ctx.now())}"
    j = {"phase": "started", "previous": os.readlink(str(link)) if os.path.islink(str(link)) else None,
         "final": str(final) if final else None, "aside": None, "placed": False,
         "old_ino": None, "new_ino": _ino(str(staging)) if staging else None}
    journal = ctx.base / f".swap-{tag}.json"
    _write_journal(journal, j)
    try:
        if final is not None and staging is not None:
            if final.exists():
                j["aside"] = str(final.with_name(f".replaced-{tag}-{final.name}"))
                j["old_ino"] = _ino(str(final))
                _write_journal(journal, j)
                _rename(str(final), j["aside"])
            _rename(str(staging), str(final))
            j["placed"] = True
            _write_journal(journal, j)
        _swap_link(link, target)
        live = probe(str(link), spec, source="managed", env=ctx.env, run=ctx.run)
        if not live.usable:
            raise _Unusable(live.detail)
    except _Unusable as exc:
        _undo(link, j, journal)
        return RepairResult(False, "verify-failed",
                            f"the installed {spec.binary} {exc}; the previous version was restored",
                            f"Install it yourself: {spec.manual_install}", n)
    except BaseException:
        _undo(link, j, journal)
        raise
    j["phase"] = "committed"
    _write_journal(journal, j)
    _commit(j, journal)
    return RepairResult(True, "installed", f"{spec.binary} {live.version} at {link}", "", n,
                        str(link), live.version)


def _commit(j: dict, journal: Path) -> None:
    if j.get("aside"):
        shutil.rmtree(j["aside"], ignore_errors=True)
    journal.unlink()


def _undo(link: Path, j: dict, journal: Path) -> None:
    """Put the link and the version folders back as they were. Safe to repeat after any
    interruption: a folder is removed only when its inode is the new install's."""
    if j.get("phase") != "rolling_back":
        j["phase"] = "rolling_back"
        _write_journal(journal, j)
    if j.get("previous") is not None:
        _swap_link(link, Path(j["previous"]))
    elif os.path.lexists(str(link)):
        os.unlink(str(link))
    final, aside, new_ino = j.get("final"), j.get("aside"), j.get("new_ino")
    if final and new_ino is not None and _ino(final) == new_ino:
        _rmtree(final)
    if aside and os.path.exists(aside) and final and not os.path.exists(final):
        _rename(aside, final)
    journal.unlink()


def recover_swaps(root: Path, runtime: str, pid_alive: Callable[[int], bool] = None,
                  log: Callable[[str], None] = lambda _m: None) -> List[str]:
    """Finish or undo a swap whose process died; only Sutando-owned files are touched."""
    pid_alive = pid_alive or _pid_alive
    base = root / runtime
    done = []
    for path in sorted(base.glob(".swap-*.json")) if base.is_dir() else []:
        parts = path.name.split("-")
        pid = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else 0
        if pid and pid_alive(pid):
            continue
        try:
            j = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink()
            continue
        if j.get("phase") == "committed":
            _commit(j, path)
            log(f"finished an interrupted update ({path.name})")
        else:
            _undo(root / "bin" / RUNTIMES[runtime].binary, j, path)
            log(f"undid an interrupted update ({path.name}); the previous version is back")
        done.append(str(path))
    return done


def _failed(reason: str, detail: str, n: int, root: Path, retry: str,
            installer: Optional[Installer] = None) -> RepairResult:
    source = "registry.npmjs.org" if installer is None or installer.kind == "npm" else "the vendor's download site"
    actions = {
        "disk-full": f"Free at least {MIN_FREE_BYTES // (1024 * 1024)} MB on the disk "
                     f"holding {root}. {retry}",
        "blocked-download": f"Check your network, proxy or firewall; the installer must reach "
                            f"{source}. {retry}",
        "install-failed": f"Read the installer error above. {retry}",
    }
    return RepairResult(False, reason, detail, actions[reason], n)


def auth_status(runtime: str, binary: str, *, env: Optional[dict] = None, run: Runner = _run,
                claude_config_dir: Optional[str] = None,
                keychain_check: Optional[Callable[[], bool]] = None) -> dict:
    """{login: ok|logged-out|unknown, remedy}. Claude's verdict is auth_preflight's."""
    env = dict(os.environ if env is None else env)
    if runtime == "claude":
        if not claude_config_dir:
            return {"login": "unknown", "remedy": "", "detail": "no config dir to check"}
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import auth_preflight
        kwargs = {"env": env}
        if keychain_check is not None:
            kwargs["keychain_check"] = keychain_check
        r = auth_preflight.check_auth_state(claude_config_dir, **kwargs)
        if r["verdict"] == "ok":
            return {"login": "ok", "remedy": "", "detail": "logged in"}
        return {"login": "logged-out", "remedy": r["remedy"], "detail": "; ".join(r["reasons"])}
    home = env.get("CODEX_HOME")
    login = (f"CODEX_HOME={shlex.quote(home)} " if home else "") + f"{shlex.quote(binary)} login"
    try:
        r = run([binary, "login", "status"], env=env,
                timeout=APP_PROBE_TIMEOUT_S if app_surface(env) else PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"login": "unknown", "remedy": login, "detail": f"login status failed: {exc}"}
    if r.returncode == 0:
        return {"login": "ok", "remedy": "", "detail": "logged in"}
    return {"login": "logged-out", "remedy": f"run: {login}",
            "detail": f"Codex CLI is not authenticated for {home or 'the default Codex home'}"}


def _say(msg: str) -> None:
    print(f"runtime-compat: {msg}", file=sys.stderr)


def _ask_consent(question: str) -> bool:  # pragma: no cover - needs a real terminal
    if not sys.stdin.isatty():
        return False
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"{question} [y/N] ")
            tty.flush()
            return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        return False


RETRY_PHRASE = "retry now"


def install_phrase(runtime: str) -> str:
    return f"install compatible {RUNTIMES[runtime].binary}"


class Incident:
    """One owner DM per incident, and a durable record of each owner reply.

    A reply is recorded when taken, then marked applied, answered and archived as
    each step lands, so a crash replays only the steps that did not finish."""

    def __init__(self, workspace: Path, runtime: str, clock: Callable[[], float] = time.time):
        self.workspace, self.runtime, self.clock = Path(workspace), runtime, clock
        self.path = self.workspace / "state" / f"runtime-compat-{runtime}.json"
        try:
            self.rec = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.rec = {}

    @property
    def open(self) -> bool:
        return self.rec.get("status") == "open"

    @property
    def entries(self) -> List[dict]:
        return [e for e in (self.rec.get("actions") or [])
                if isinstance(e, dict) and isinstance(e.get("task"), str)]

    def _save(self) -> None:
        from local_record import write_whole
        write_whole(self.path, self.rec)

    def _unfinished(self) -> List[dict]:
        from owner_reply import STEPS
        return [e for e in self.entries if not all(e.get(s) for s in STEPS)]

    def _open(self) -> None:
        if not self.open:
            t = int(self.clock())
            self.rec = {"status": "open", "opened_at": t, "actions": self._unfinished(),
                        "notice_queued": False,
                        "notice": f"proactive-runtime-compat-{self.runtime}-{t}.txt"}
            self._save()

    def notify(self, body: str) -> bool:
        """Queue the incident's DM if it is not queued yet. True when this call queued it."""
        from owner_reply import queue_notice
        self._open()
        if self.rec.get("notice_queued") or not queue_notice(self.workspace, self.rec["notice"], body):
            return False
        self.rec["notice_queued"] = True
        self._save()
        return True

    def take_replies(self) -> None:
        """Record the owner's new replies to this incident before anything acts on them.
        After an app fix, a late reply to its DM is recorded as already answered."""
        from owner_reply import matching_replies
        late = (not self.open and self.rec.get("resolved_by") == "app"
                and self.rec.get("notice_queued") and self.rec.get("fixed_reply"))
        if not self.open and not late:
            return
        known = {e["task"] for e in self.entries}
        got = list(matching_replies(self.workspace, (install_phrase(self.runtime), RETRY_PHRASE),
                                    skip=known))
        done = {"applied": True, "reply": self.rec["fixed_reply"]} if late else {}
        if got:
            self.rec["actions"] = self.entries + [
                dict({"task": p.stem, "phrase": phrase, "at": int(self.clock())}, **done)
                for p, phrase in got]
            self._save()

    def record_consent(self, consent: dict) -> None:
        """Persist the app's consent before the repair it allows runs."""
        self._open()
        self.rec["consent"] = consent
        self._save()

    def resolve_from_app(self, detail: str) -> None:
        """The app fixed it: answer the owner's replies, tell the DM thread, close."""
        from owner_reply import queue_notice
        reply = f"Already fixed from the app: {detail}. Nothing more to do."
        self.apply(None, reply)
        self.finish()
        if self.rec.get("notice_queued"):
            queue_notice(self.workspace, self.rec["notice"].replace(".txt", "-fixed.txt"),
                         f"Fixed from the app: {detail}. No reply needed.\n")
        self.rec.update(status="resolved", resolved_at=int(self.clock()), resolved_by="app",
                        fixed_reply=reply)
        self._save()

    def pending(self) -> set:
        """Phrases of recorded replies whose action has not been applied yet."""
        return {e["phrase"] for e in self.entries if not e.get("applied")}

    def apply(self, phrase: Optional[str], reply: str) -> None:
        """Mark recorded replies applied (all, or one phrase), with the answer they get."""
        changed = False
        for e in self.entries:
            if not e.get("applied") and (phrase is None or e["phrase"] == phrase):
                e.update(applied=True, reply=reply)
                changed = True
        if changed:
            self._save()

    def mark(self, task: str, step: str) -> None:
        for e in self.entries:
            if e["task"] == task:
                e[step] = True
        self._save()

    def finish(self) -> None:
        """Answer and archive every applied reply; each step is marked as it lands."""
        from owner_reply import finish
        for e in self._unfinished():
            if e.get("applied"):
                finish(self.workspace, e, lambda step, task=e["task"]: self.mark(task, step))

    def close(self) -> None:
        if self.open:
            self.rec.update(status="resolved", resolved_at=int(self.clock()))
            self._save()


def notice_body(runtime: str, step: RepairStep) -> str:
    binary = RUNTIMES[runtime].binary
    lines = [f"Your Sutando core could not start: {step.reason}", ""]
    if step.assisted:
        lines += [f'Reply "{install_phrase(runtime)}" and Sutando installs a compatible {binary}. '
                  f"It changes only {step.assisted_touches}, never your settings. "
                  "The core then starts by itself.", ""]
    lines += [f"Or fix it yourself: {step.command}",
              f'then reply "{RETRY_PHRASE}" and the core starts.']
    return "\n".join(lines) + "\n"


def _login_step(remedy: str, sep: str = ", ") -> str:
    return f'Log in: {remedy}{sep}then reply "{RETRY_PHRASE}" and the core starts.'


def login_notice_body(runtime: str, auth: dict) -> str:
    return (f"Your Sutando core could not start: {RUNTIMES[runtime].binary} is not logged in.\n\n"
            f"{_login_step(auth['remedy'], sep=chr(10))}\n")


def ensure(runtime: str, *, env: Optional[dict] = None, wait_s: float = 0.0,
           repair: bool = False, consent: Optional[Callable[[str], bool]] = None,
           enforce_auth: bool = False, claude_config_dir: Optional[str] = None,
           resume_hint: str = "", workspace: Optional[Path] = None, notify_owner: bool = False,
           say: Callable[[str], None] = _say,
           discover_fn: Callable[..., Report] = discover,
           repair_fn: Callable[..., RepairResult] = assisted_install,
           auth_fn: Callable[..., dict] = auth_status,
           sleep: Callable[[float], None] = time.sleep,
           now: Callable[[], float] = time.monotonic,
           clock: Callable[[], float] = time.time) -> Tuple[int, Optional[str]]:
    """Make the core CLI usable, then hand back its path. An install changes only with
    `consent`, or the owner's "install compatible <cli>" reply to this incident's DM."""
    env = dict(os.environ if env is None else env)
    inc = Incident(workspace, runtime, clock) if workspace else None
    outcome = {"rc": EXIT_UNUSABLE, "why": ""}
    if inc is not None:
        inc.finish()
    # An exception here is a crash: the record stays as it is and the next run replays it.
    rc, path = _ensure(runtime, env, wait_s, repair, consent, enforce_auth, claude_config_dir,
                       resume_hint, inc, notify_owner, outcome, say, discover_fn,
                       repair_fn, auth_fn, sleep, now)
    if inc is not None:
        inc.apply(None, _final_reply(runtime, rc, outcome))
        inc.finish()
        if rc == EXIT_OK:
            inc.close()
    return rc, path


def _final_reply(runtime: str, rc: int, outcome: dict) -> str:
    """The one answer every pending reply gets, from the launch's full verdict."""
    binary = RUNTIMES[runtime].binary
    installed = outcome.get("installed")
    done = f"Installed {installed} in Sutando's own folder" if installed else ""
    if rc == EXIT_OK:
        return (f"{done}. " if done else "") + f"{binary} works now. The core is starting."
    if rc == EXIT_LOGIN and done:
        return f"{done}, but {binary} is not logged in. {_login_step(outcome['login_remedy'])}"
    why = f"{done}, but the core" if done else "The core"
    why = f"{why} still cannot start: {outcome['why']}"
    return f"{why}. {_login_step(outcome['login_remedy'])}" if rc == EXIT_LOGIN else why


def _failed_repair_reply(result: RepairResult) -> str:
    return f"That did not fix it: {result.detail}. Next: {result.user_action}"


def _ensure(runtime, env, wait_s, repair, consent, enforce_auth, claude_config_dir, resume_hint,
            inc, notify_owner, outcome, say, discover_fn, repair_fn, auth_fn, sleep, now):
    spec = RUNTIMES[runtime]
    again = f" Then run again: {resume_hint}" if resume_hint else ""

    def replies() -> set:
        if inc is None:
            return set()
        inc.take_replies()
        return inc.pending()

    def repair_now() -> None:
        result = repair_fn(runtime, env=env, log=say)
        if result.ok:
            # Answered once the launch's outcome is known: a logged-out install does not start.
            outcome["installed"] = result.detail
        elif inc is not None:
            inc.apply(install_phrase(runtime), _failed_repair_reply(result))
        say(f"repair {'succeeded' if result.ok else 'failed'}: {result.detail}")
        if not result.ok:
            say(f"next: {result.user_action}")

    owner_said = replies()
    recover_swaps(managed_root(env), runtime, log=say)
    report = discover_fn(runtime, env=env)
    for stale in report.interrupted:
        say(f"an interrupted update left {stale}; the previous version is still in use.")
    if report.selected is None:
        step = plan(report, env=env)
        outcome["why"] = step.reason
        say(f"cannot start the core: {step.reason}")
        say(f"fix: {step.command}")
        granted = install_phrase(runtime) in owner_said
        if step.assisted and not granted and repair and consent is not None:
            granted = consent(f"Install a compatible {spec.binary}? This changes "
                              f"{step.assisted_touches}. Nothing else.")
        if step.assisted and granted:
            repair_now()
            report = discover_fn(runtime, env=env)
        elif step.assisted:
            say(f"or let Sutando install one for you: {step.assisted}")
        if report.selected is None and notify_owner and inc is not None:
            if inc.notify(notice_body(runtime, step)):
                say("asked the owner in their DM how to fix it.")

        def recheck() -> Report:
            said = replies()
            r = discover_fn(runtime, env=env)
            if (r.selected is None and install_phrase(runtime) in said and "installed" not in outcome
                    and plan(r, env=env).assisted):
                repair_now()
                r = discover_fn(runtime, env=env)
            return r
        report = _wait(report, wait_s, recheck, lambda r: r.selected is not None, say, sleep, now,
                       f"waiting up to {int(wait_s)}s for the fix; the launch continues on its own.")
        if report.selected is None:
            say(f"the core was not started.{again}")
            return EXIT_UNUSABLE, None
    sel = report.selected
    if sel.status == NEWER_UNTESTED:
        say(plan(report, env=env).reason)
    say(report.selected_reason)
    say(f"using {sel.path} ({sel.version})")
    auth = auth_fn(runtime, sel.path, env=env, claude_config_dir=claude_config_dir)
    if auth["login"] == "logged-out":
        outcome["why"] = f"{spec.binary} is not logged in"
        outcome["login_remedy"] = auth["remedy"]
        say(f"{spec.binary} is not logged in: {auth['detail']}")
        say(f"fix: {auth['remedy']}")
        if enforce_auth and notify_owner and inc is not None and inc.notify(login_notice_body(runtime, auth)):
            say("asked the owner in their DM to log in.")
        holder = {"auth": auth}

        def recheck_auth() -> dict:
            replies()
            holder["auth"] = auth_fn(runtime, sel.path, env=env, claude_config_dir=claude_config_dir)
            outcome["login_remedy"] = holder["auth"].get("remedy") or outcome["login_remedy"]
            return holder["auth"]
        _wait(auth, wait_s, recheck_auth, lambda a: a["login"] != "logged-out", say, sleep, now,
              f"waiting up to {int(wait_s)}s for the login; the launch continues on its own.")
        if holder["auth"]["login"] == "logged-out" and enforce_auth:
            say(f"{spec.binary} is not authenticated; the core was not started.{again}")
            return EXIT_LOGIN, None
    return EXIT_OK, sel.path


def _wait(value, wait_s, recheck, done, say, sleep, now, banner):
    if done(value) or wait_s <= 0:
        return value
    say(banner)
    deadline = now() + wait_s
    while now() < deadline:
        sleep(min(WAIT_POLL_S, max(0.0, deadline - now())))
        value = recheck()
        if done(value):
            say("fixed; continuing the launch.")
            return value
    return value


def _doctor_lines(report: Report, auth: Optional[dict], env: Optional[dict] = None) -> List[str]:
    step = plan(report, env=env)
    lines = [f"{report.runtime}: {report.status}"]
    for c in report.candidates:
        mark = "*" if c is report.selected else " "
        lines.append(f"  {mark} [{c.status}] {c.path} ({c.source}) {c.detail}")
    for stale in report.interrupted:
        lines.append(f"  interrupted update left {stale}; the previous version is in use")
    lines.append(f"  reason: {step.reason}")
    lines.append(f"  fix: {step.command}")
    if step.assisted:
        lines.append(f"  assisted fix (asks first; changes {step.assisted_touches}): {step.assisted}")
    if auth and auth["login"] == "logged-out":
        lines.append(f"  login: {auth['remedy']}")
    return lines


def _workspace() -> Optional[Path]:
    """The workspace the owner's DM and replies live in; None when it cannot be resolved."""
    try:
        from workspace_default import resolve_workspace
        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 - the gate still works, only without the owner DM
        return None


def _doctor_doc(report: Report, env: dict) -> dict:
    """One `doctor --json` entry; the desktop app parses this shape (schema 1)."""
    step = plan(report, env=env)
    if report.selected is None:
        auth = {"login": "unknown", "remedy": "", "detail": "no usable binary"}
    else:
        auth = auth_status(report.runtime, report.selected.path, env=env,
                           claude_config_dir=env.get("CLAUDE_CONFIG_DIR") or None)
    return dict(report.to_dict(), schema=DOCTOR_SCHEMA, plan=asdict(step),
                plan_hash=plan_hash(step), auth=auth)


def _repair_cmd(a: argparse.Namespace, env: dict) -> int:
    def emit(kind: str, **fields) -> None:
        if kind == "progress" and fields["phase"] not in REPAIR_PHASES:
            raise ValueError(f"undeclared repair phase {fields['phase']!r}")
        if a.json:
            print(json.dumps(dict(type=kind, **fields)), flush=True)

    def done(result: RepairResult, code: int) -> int:
        emit("result", **asdict(result))
        return code
    if not a.yes:
        _say("the assisted repair changes files; pass --yes to consent.")
        return done(RepairResult(False, "no-consent", "--yes was not given"), EXIT_USAGE)
    if a.consent_source == "app" and not a.plan_hash:
        _say("an app consent needs --plan-hash; refusing without one.")
        return done(RepairResult(False, "plan-hash-required", "app consent requires --plan-hash",
                                 "Resend the repair with the current plan's hash."), EXIT_USAGE)
    inc, consent_hash = None, a.plan_hash
    if a.plan_hash or a.consent_source:
        current = plan_hash(plan(discover(a.runtime, env=env), env=env))
        if a.plan_hash and a.plan_hash != current:
            _say("the situation changed since the plan was shown; review it again.")
            return done(RepairResult(False, "plan-changed", "the repair plan is no longer the one "
                                     "consented to", "Check again, then review the new plan."),
                        EXIT_USAGE)
        consent_hash = current
    ws = _workspace() if a.consent_source else None
    if a.consent_source and ws is None:
        _say("the workspace isn't available to record consent; refusing without it.")
        return done(RepairResult(False, "workspace-unavailable", "could not resolve the workspace "
                                 "to record consent", "Retry once the workspace is reachable."),
                    EXIT_USAGE)
    if ws is not None:
        inc = Incident(ws, a.runtime)
        inc.take_replies()
        inc.record_consent({"source": a.consent_source, "by": a.consent_by, "at": int(time.time()),
                            "plan_hash": consent_hash, "runtime": a.runtime})
    result = assisted_install(a.runtime, env=env, log=_say,
                              progress=lambda phase, **kw: emit("progress", phase=phase, **kw))
    emit("progress", phase="done")
    _say(f"{result.reason}: {result.detail}")
    if not result.ok:
        _say(f"next: {result.user_action}")
    elif inc is not None and a.consent_source == "app":
        inc.resolve_from_app(result.detail)
    return done(result, 0 if result.ok else EXIT_UNUSABLE)


def _notify_owner_cmd(a: argparse.Namespace, env: dict) -> int:
    def out(queued: bool, reason: str) -> int:
        if a.json:
            print(json.dumps({"queued": queued, "reason": reason}))
        else:
            _say(f"owner DM {'queued' if queued else 'not queued'}: {reason}")
        return 0
    report = discover(a.runtime, env=env)
    if report.selected is not None:
        return out(False, "usable")
    ws = _workspace()
    if ws is None:
        return out(False, "no-workspace")
    inc = Incident(ws, a.runtime)
    if inc.notify(notice_body(a.runtime, plan(report, env=env))):
        return out(True, "queued")
    return out(False, "already-sent" if inc.rec.get("notice_queued") else "not-queued")


def main(argv: Optional[Sequence[str]] = None, *, env: Optional[dict] = None,
         consent: Callable[[str], bool] = _ask_consent) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--runtime", choices=("claude", "codex", "all"), default="all")
    d.add_argument("--json", action="store_true")
    e = sub.add_parser("ensure")
    e.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    e.add_argument("--wait", type=float, default=0.0)
    e.add_argument("--repair", action="store_true")
    e.add_argument("--yes", action="store_true", help="consent to the assisted repair")
    e.add_argument("--enforce-auth", action="store_true")
    e.add_argument("--claude-config-dir", default="")
    e.add_argument("--resume-hint", default="")
    e.add_argument("--notify-owner", action="store_true",
                   help="off a terminal: DM the owner once and act on their reply")
    w = sub.add_parser("which", help="print the binary a launch would use; acts on nothing")
    w.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    w.add_argument("--enforce-auth", action="store_true")
    w.add_argument("--claude-config-dir", default="")
    r = sub.add_parser("repair")
    r.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    r.add_argument("--yes", action="store_true")
    r.add_argument("--json", action="store_true",
                   help="one JSON line per phase on stdout, then one result line")
    r.add_argument("--consent-source", default="",
                   help="who consented, e.g. app; recorded on the incident before acting")
    r.add_argument("--consent-by", default="", help="the account that consented")
    r.add_argument("--plan-hash", default="", help="refuse when the plan is no longer this one; "
                   "required with --consent-source app")
    n = sub.add_parser("notify-owner", help="queue the incident's one owner DM, if still unusable")
    n.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    n.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)
    if a.cmd == "doctor":
        names = list(RUNTIMES) if a.runtime == "all" else [a.runtime]
        reports = [discover(n, env=env) for n in names]
        if a.json:
            print(json.dumps([_doctor_doc(r, env) for r in reports], indent=2))
        else:
            for rep in reports:
                auth = (auth_status(rep.runtime, rep.selected.path, env=env,
                                    claude_config_dir=env.get("CLAUDE_CONFIG_DIR"))
                        if rep.selected else None)
                print("\n".join(_doctor_lines(rep, auth, env)))
        return 0 if all(rep.selected for rep in reports) else EXIT_UNUSABLE
    if a.cmd == "which":
        rep = discover(a.runtime, env=env)
        if rep.selected is None:
            _say(f"no usable {RUNTIMES[a.runtime].binary}: {plan(rep, env=env).reason}")
            return EXIT_UNUSABLE
        _say(rep.selected_reason)
        print(rep.selected.path)
        if a.enforce_auth:
            auth = auth_status(a.runtime, rep.selected.path, env=env,
                              claude_config_dir=a.claude_config_dir or None)
            if auth["login"] == "logged-out":
                _say(f"{RUNTIMES[a.runtime].binary} is not logged in: {auth['remedy']}")
                return EXIT_LOGIN
        return 0
    if a.cmd == "repair":
        return _repair_cmd(a, env)
    if a.cmd == "notify-owner":
        return _notify_owner_cmd(a, env)
    give = (lambda _q: True) if a.yes or env.get("SUTANDO_RUNTIME_REPAIR_CONSENT") == "1" else consent
    app = app_surface(env)
    rc, path = ensure(a.runtime, env=env, wait_s=0.0 if app else a.wait, repair=a.repair,
                      consent=give, enforce_auth=a.enforce_auth,
                      claude_config_dir=a.claude_config_dir or None,
                      resume_hint=a.resume_hint, workspace=_workspace(),
                      notify_owner=a.notify_owner and not app)
    if path:
        print(path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
