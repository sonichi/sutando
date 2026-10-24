"""Self-upgrade for a Hatch install: move ag2-sparrow up to the backend's target_version.

The keepawake run decides (`maybe_start`) and logs the decision on its line. First, under
the upgrade lock, it settles an attempt still marked started after 30 minutes: crashed,
with the snapshot restored if another version was left installed. Then at most one
attempt per target version, and only up; never when min_version is above the target,
without the target's sha256 map (wheel filename -> sha256), with AUTO_UPGRADE=0, or for
an editable or URL install. The upgrade runs detached (`run_upgrade`) under
STATE_DIR/upgrade.lock, which keepawake also holds across its probe and restart, and
first re-reads the installed version: a target not above it is skipped. Its order is
snapshot, stop, install, start, verify: every file in the package namespace (the
top-level names RECORD lists, every ag2_sparrow-*.dist-info, the console scripts) is
copied with its sha256 to STATE_DIR/rollback/<version>/ before anything changes; Sparrow
is stopped through the manager that owns it and proven gone (keepalive.quiesce_sparrow;
a hand-made unit is never touched); `pip install --no-deps --only-binary=:all:
--require-hashes` puts the pinned wheel into this interpreter; `pip check` must run and
report no new problem naming ag2-sparrow; install is re-run with --no-start (which never
starts the service); and Sparrow is started through the same manager and must import the
target and reconnect. A reply posted while Sparrow is stopped waits in the results dir
and its outbox, which the upgrade never touches, and is delivered once Sparrow starts.
Any failure restores the snapshot (no index, no network): every namespace path the
snapshot does not list is removed, the copies are put back, and the namespace must hash
equal to the snapshot, whatever RECORD says afterwards; then install is re-run and Sparrow
started. The outcome is "rolled-back" only when all of that held and Sparrow reconnects,
else "restore-failed" with what is installed; a stop that cannot be proven aborts the
restore before any file changes. Either leaves the health reason upgrade-failed. The
attempt record is a journal: its phase (snapshot, quiesced, installing, installed,
restoring) is written before each step, so an attempt found dead (the lock free) is
settled from it: nothing installed means crashed (Sparrow restarted if it was stopped), a
namespace that differs from the snapshot or an interrupted restore is restored, and an
interrupted restore resumes on the next run. A changed hook definition (install's
REQUIRED hook: line) is never applied: the hook wakes the agent once with it."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ... import __version__, _dirs, file_lock
from . import cli as _cli, keepalive as _keepalive

PACKAGE = "ag2-sparrow"
STATE_NAME = "hatch-upgrade.json"
NOTICE_NAME = "hatch-wake-notice"
LOG_NAME = "hatch-upgrade.log"
LOCK_NAME = "upgrade.lock"
ROLLBACK_DIR = "rollback"
UPGRADE_FAILED = _keepalive.UPGRADE_FAILED
# A "started" attempt this old, with no upgrade holding the lock, crashed.
STALE_START_S = 30 * 60
# Phases written under the lock; a dead attempt in one of these may have changed files.
MUTATING_PHASES = ("installing", "installed", "restoring")
PIP_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 300
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(
    r"([0-9]{1,9}(?:\.[0-9]{1,9}){0,9})(?:(a|b|rc)([0-9]{1,9}))?"
    r"(?:\.post([0-9]{1,9}))?(?:\.dev([0-9]{1,9}))?")
_PRE = {"a": 0, "b": 1, "rc": 2}
_OWN_RE = re.compile(r"ag2[-_.]sparrow", re.I)
_INFO_RE = re.compile(r"ag2[-_.]sparrow-[^/]+\.(dist|egg)-info", re.I)


def parse_version(text) -> "tuple | None":
    """A sortable key for a plain PEP 440 version; None for anything else, which is never
    passed to pip."""
    m = _VERSION_RE.fullmatch(text) if isinstance(text, str) and len(text) <= 64 else None
    if m is None:
        return None
    release = [int(p) for p in m.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre, post, dev = m.group(2), m.group(4), m.group(5)
    # A bare .devN sorts before every pre-release of its release.
    stage = (_PRE[pre], int(m.group(3))) if pre else ((-1, 0) if dev and post is None else (3, 0))
    return (tuple(release), stage, int(post) if post else -1,
            int(dev) if dev else float("inf"))


def state_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_NAME


def notice_path(state_dir: Path) -> Path:
    """The hook wakes the agent once with this file's text, then removes it."""
    return Path(state_dir) / NOTICE_NAME


def load_state(state_dir: Path) -> dict:
    try:
        state = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state = state if isinstance(state, dict) else {}
    if not isinstance(state.get("attempted"), dict):
        state["attempted"] = {}
    return state


def save_state(state_dir: Path, state: dict) -> None:
    _cli._write(state_path(state_dir), json.dumps(state, sort_keys=True, indent=1) + "\n", 0o600)


def failed_since(state_dir: Path, installed: str = __version__) -> "float | None":
    """When the failed upgrade to or from this version failed; None when none did."""
    failed = load_state(state_dir).get("failed")
    if isinstance(failed, dict) and installed in (failed.get("from"), failed.get("target")):
        at = failed.get("at")
        return float(at) if isinstance(at, (int, float)) else time.time()
    return None


@contextlib.contextmanager
def upgrade_lock(state_dir: Path):
    """Yields True while this process holds the upgrade lock, False when another does."""
    path = Path(state_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            file_lock.lock_fd(fd, blocking=False)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            file_lock.unlock_fd(fd)
    finally:
        os.close(fd)


def upgrade_running(state_dir: Path) -> bool:
    with upgrade_lock(state_dir) as held:
        return not held


def _run(argv: list, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(e).__name__}: {e}")


def _spawn(argv: list, log: Path) -> bool:
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("ab") as fh:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        print(f"upgrade: could not start ({e})", file=sys.stderr)
        return False
    return True


def _why(r: subprocess.CompletedProcess) -> str:
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def _distribution():
    """The installed ag2-sparrow, read from disk each call; None when pip did not install it."""
    try:
        return importlib.metadata.distribution(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def unsupported_install(dist) -> str:
    """Why this install cannot be upgraded and restored file by file; '' when it can."""
    if dist is None:
        return "ag2-sparrow is not installed as a package here"
    if dist.read_text("direct_url.json") is not None:
        return "ag2-sparrow is an editable or URL install (direct_url.json)"
    if not dist.files:
        return "ag2-sparrow's install record (RECORD) is missing"
    return ""


def _installed_paths(dist) -> "list[Path]":
    return [Path(os.path.normpath(dist.locate_file(f))) for f in dist.files or []]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_scope(dist) -> "tuple[Path, list[str], list[str]]":
    """(site, roots, extra): the site dir, the top-level names RECORD puts in it, and the
    recorded files outside it (console scripts)."""
    site = Path(os.path.normpath(dist.locate_file("")))
    roots, extra = set(), []
    for path in _installed_paths(dist):
        try:
            roots.add(path.relative_to(site).parts[0])
        except (ValueError, IndexError):
            extra.append(str(path))
    return site, sorted(roots), sorted(set(extra))


def namespace(site: Path, roots: list, extra: list) -> "dict[str, str]":
    """{path: sha256} of every file now under the package namespace: `roots` and any
    ag2_sparrow-*.dist-info in `site` (whatever version), plus the `extra` files; no
    __pycache__."""
    found = {}
    site = Path(site)
    if site.is_dir():
        for child in sorted(site.iterdir()):
            if child.name not in roots and not _INFO_RE.fullmatch(child.name):
                continue
            files = [child] if child.is_file() else sorted(
                p for p in child.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
            for path in files:
                found[str(path)] = _sha256(path)
    for path in map(Path, extra):
        if path.is_file():
            found[str(path)] = _sha256(path)
    return found


def snapshot(state_dir: Path, version: str, dist) -> Path:
    """Copy the whole package namespace, hashed, into STATE_DIR/rollback/<version>/."""
    dest = Path(state_dir) / ROLLBACK_DIR / version
    tmp = dest.with_name(f".{version}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "files").mkdir(parents=True)
    site, roots, extra = _record_scope(dist)
    if not roots and not extra:
        raise ValueError("the install recorded no files")
    entries = []
    for i, (path, digest) in enumerate(sorted(namespace(site, roots, extra).items())):
        name = f"{i:05d}"
        shutil.copy2(path, tmp / "files" / name)
        entries.append({"path": path, "copy": name, "sha256": digest})
    if not entries:
        raise ValueError("the install's files are missing")
    manifest = {"version": version, "site": str(site), "roots": roots, "extra": extra,
                "files": entries}
    (tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def _manifest(state_dir: Path, version: str) -> "tuple[Path, dict]":
    snap = Path(state_dir) / ROLLBACK_DIR / version
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version or not manifest.get("files"):
        raise ValueError(f"the snapshot in {snap} is not one of {version}")
    return snap, manifest


def namespace_drift(state_dir: Path, version: str) -> "list[str] | None":
    """How the namespace differs from the snapshot of `version` (empty when it matches);
    None when there is no snapshot to compare with."""
    try:
        _, manifest = _manifest(state_dir, version)
    except (OSError, ValueError):
        return None
    want = {e["path"]: e["sha256"] for e in manifest["files"]}
    have = namespace(manifest["site"], manifest["roots"], manifest["extra"])
    return sorted([f"added {p}" for p in have.keys() - want.keys()]
                  + [f"missing {p}" for p in want.keys() - have.keys()]
                  + [f"changed {p}" for p in want.keys() & have.keys() if want[p] != have[p]])


def restore(state_dir: Path, version: str) -> None:
    """Put the snapshot of `version` back: remove every namespace path it does not list,
    copy its files in, and require the namespace to hash equal to it. RECORD, which a
    failed install may have left stale or half-swapped, is never consulted."""
    snap, manifest = _manifest(state_dir, version)
    site, roots, extra = manifest["site"], manifest["roots"], manifest["extra"]
    want = {e["path"]: e for e in manifest["files"]}
    emptied = set()
    for path in map(Path, namespace(site, roots, extra)):
        if str(path) not in want:
            path.unlink()
            emptied.add(path.parent)
    for entry in manifest["files"]:
        target = Path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap / "files" / entry["copy"], target)
    for cache in sorted(Path(site).glob("*/**/__pycache__"), reverse=True):
        if cache.parts[len(Path(site).parts)] in roots:
            shutil.rmtree(cache, ignore_errors=True)  # the failed version's bytecode
    for directory in sorted(emptied, key=lambda d: len(d.parts), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()  # only a directory the failed version left empty
    drift = namespace_drift(state_dir, version)
    if drift:
        raise ValueError(f"the files do not match the snapshot of {version}: "
                         + "; ".join(drift[:5]) + (" ..." if len(drift) > 5 else ""))


def pip_install(state_dir: Path, version: str, hashes: dict) -> subprocess.CompletedProcess:
    """Install exactly `version` as a wheel with no dependency changes, pinned to every
    sha256 in `hashes`. pip accepts --hash only in a requirements file, so it goes there."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fd, req = tempfile.mkstemp(dir=str(state_dir), prefix=".upgrade-", suffix=".txt")
    pins = " ".join(f"--hash=sha256:{h}" for h in sorted(set(hashes.values())))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{PACKAGE}=={version} {pins}\n")
        return _run([sys.executable, "-m", "pip", "install", "--no-deps", "--only-binary=:all:",
                     "--disable-pip-version-check", "--require-hashes", "-r", req],
                    PIP_TIMEOUT_S)
    finally:
        Path(req).unlink(missing_ok=True)


def valid_hashes(value) -> "dict | None":
    """A non-empty {wheel filename: sha256} map, else None."""
    if not isinstance(value, dict) or not value:
        return None
    ok = all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}", k)
             and isinstance(v, str) and SHA256_RE.fullmatch(v) for k, v in value.items())
    return dict(value) if ok else None


def own_check_problems() -> "set[str] | None":
    """`pip check` lines that name ag2-sparrow (it has no requirements of its own); None
    when pip check could not run or did not finish, which is a failure, never a pass."""
    r = _run([sys.executable, "-m", "pip", "check"], PIP_TIMEOUT_S)
    if r.returncode == 0:
        return set()
    if r.returncode != 1:
        return None
    return {line.strip() for line in f"{r.stdout}\n{r.stderr}".splitlines()
            if _OWN_RE.search(line)}


def install_argv(cfg: dict) -> list:
    """`install` with the choices the config recorded, so a re-run changes nothing else."""
    argv = [_keepalive._command(cfg), "install", "--mxid", cfg.get("AGENT_MXID", ""),
            "--no-start", f"--gateway-instance={_keepalive._instance(cfg)}"]
    for key, flag in (("TASK_DIR", "--task-dir"), ("RESULT_DIR", "--result-dir"),
                      ("STATE_DIR", "--state-dir"), ("ACTIVITY_FILE", "--activity-file"),
                      ("CRON_DIR", "--cron-dir"), ("KEEPALIVE_LOG", "--keepalive-log")):
        if cfg.get(key):
            argv += [flag, cfg[key]]
    try:
        ours = _cli.service_path().read_text(encoding="utf-8").startswith(_cli.SERVICE_MARKER)
    except OSError:
        ours = False
    if not ours:
        argv.append("--no-service")
    if not _keepalive._found(_keepalive.cron_dir(cfg), _keepalive.TASK):
        argv.append("--no-keepalive")
    return argv


def installed_version() -> str:
    """The version a fresh interpreter imports, which is what Sparrow will run."""
    r = _run([sys.executable, "-c", "import ag2_sparrow; print(ag2_sparrow.__version__)"], 60)
    return r.stdout.strip() if r.returncode == 0 else ""


def _decide(cfg: dict, setup: "dict | None", installed: str) -> "tuple[str, str, dict | None]":
    """(decision, detail, hashes); decision is '' when an upgrade to the target is due."""
    if (cfg.get("AUTO_UPGRADE") or "").strip() == "0":
        return "off", "AUTO_UPGRADE=0 in the install config", None
    package = (setup or {}).get("package")
    package = package if isinstance(package, dict) else {}
    target, hashes = package.get("target_version"), valid_hashes(package.get("target_sha256"))
    if target is None:
        return "no-target", "the served setup names no target_version", None
    want, have = parse_version(target), parse_version(installed)
    if want is None or have is None:
        return "bad-version", f"cannot compare {target!r} with {installed!r}", None
    if want < have:
        return "below-installed", f"target_version {target} is below the installed {installed}", None
    if want == have:
        return "current", f"{installed} is the target_version", None
    floor = package.get("min_version")
    if floor is not None and (parse_version(floor) is None or parse_version(floor) > want):
        return "min-above-target", f"min_version {floor!r} is above target_version {target}", None
    if hashes is None:
        return "no-hashes", f"{target} has no valid target_sha256 map", None
    why = unsupported_install(_distribution())
    if why:
        return "unsupported-install", why, None
    owner = _keepalive._unit_owner()
    if owner == "other":
        return "hand-made-service", (f"{_cli.service_path()} was not written by install, so "
                                     "its Sparrow is not stopped or upgraded unattended"), None
    if owner == _keepalive.UNIT_UNREACHABLE:
        return "unit-unreachable", (f"{_cli.service_path()} is installed but systemctl --user "
                                    "did not answer"), None
    return "", "", hashes


def maybe_start(cfg: dict, setup: "dict | None", installed: str = __version__,
                now: "float | None" = None) -> "str | None":
    """Decide on the served target and start an upgrade when one is due. Returns the
    decision for the keepawake line; None only when there is no served setup."""
    now = time.time() if now is None else now
    state_dir = _keepalive._state_dir(cfg)
    # Before any decision: an interrupted upgrade must be settled even if its target is current.
    with upgrade_lock(state_dir) as held:
        settled = reconcile(cfg, state_dir, now) if held else None
    if settled:
        return settled
    if setup is None:
        return None
    decision, detail, hashes = _decide(cfg, setup, installed)
    if decision:
        print(f"upgrade: {decision}: {detail}", file=sys.stderr)
        return decision
    target = setup["package"]["target_version"]
    with upgrade_lock(state_dir) as held:
        if not held:
            print("upgrade: running: an upgrade already holds the lock", file=sys.stderr)
            return "running"
        state = load_state(state_dir)
        tried = state["attempted"].get(target)
        if isinstance(tried, dict):
            print(f"upgrade: {target} was already attempted ({tried.get('outcome')})",
                  file=sys.stderr)
            return f"{target}:attempted"
        # Recorded before the start, so a crash mid-upgrade is never retried for this target.
        state["attempted"][target] = {"at": now, "from": installed, "outcome": "started"}
        save_state(state_dir, state)
    argv = [sys.executable, "-m", "ag2_sparrow.adapters.hatch.self_upgrade", target,
            json.dumps(hashes, sort_keys=True)]
    if not _spawn(argv, state_dir / LOG_NAME):
        _finish(state_dir, target, "not-started")
        return f"{target}:not-started"
    print(f"upgrade: started {installed} -> {target}", file=sys.stderr)
    return f"{target}:started"


def reconcile(cfg: dict, state_dir: Path, now: float) -> "str | None":
    """Settle a dead attempt, with the upgrade lock held by the caller (so no worker runs):
    one still "started" after STALE_START_S with no phase, or one in any phase, which only
    a lock holder writes. Nothing installed and the namespace equal to the snapshot means
    crashed, Sparrow restarted if the attempt had stopped it; a mutating phase, a drifted
    namespace or an interrupted restore is restored, in a phase that resumes next run."""
    state = load_state(state_dir)
    for target, tried in state["attempted"].items():
        if not isinstance(tried, dict):
            continue
        at, phase = tried.get("at"), tried.get("phase")
        stale = (tried.get("outcome") == "started" and isinstance(at, (int, float))
                 and (phase is not None or now - at > STALE_START_S))
        if not (stale or phase == "restoring"):
            continue
        previous, dist = str(tried.get("from") or ""), _distribution()
        current = dist.version if dist is not None else ""
        drift = namespace_drift(state_dir, previous) if previous else None
        why = "the restore did not finish" if phase == "restoring" else "the upgrade did not finish"
        print(f"upgrade: {target} ({phase or 'started'}): {why}; {current or 'no version'} "
              f"is installed", file=sys.stderr)
        if phase not in MUTATING_PHASES and not drift:
            tried.update(outcome="crashed", phase="crashed")
            state["failed"] = {"target": target, "from": previous, "installed": current,
                               "why": why, "at": now}
            save_state(state_dir, state)
            if phase == "quiesced":
                ok, how = start_sparrow(cfg, tried.get("manager"))
                print(("started: " if ok else "error: Sparrow did not start: ") + how,
                      file=sys.stderr if not ok else sys.stdout)
            return f"{target}:crashed"
        if drift is None:
            tried.update(outcome="restore-failed", phase="restore-failed")
            state["failed"] = {"target": target, "from": previous, "installed": current,
                               "why": why, "at": now, "outcome": "restore-failed",
                               "problems": [f"no snapshot of {previous or 'the previous version'}"]}
            save_state(state_dir, state)
            print(f"error: {target} left files changed and there is no snapshot of {previous}",
                  file=sys.stderr)
            return f"{target}:restore-failed"
        return f"{target}:{_rollback(cfg, state_dir, previous, target, why, tried.get('manager'))}"
    return None


def _phase(state_dir: Path, target: str, phase: str, **fields) -> None:
    """Journal the step about to run, so a dead attempt is settled from where it got to."""
    state = load_state(state_dir)
    state["attempted"].setdefault(target, {}).update(phase=phase, **fields)
    save_state(state_dir, state)


def _finish(state_dir: Path, target: str, outcome: str, **extra) -> None:
    state = load_state(state_dir)
    state["attempted"].setdefault(target, {}).update(outcome=outcome, phase=outcome)
    state.update(extra)
    save_state(state_dir, state)


def stop_sparrow(cfg: dict) -> "tuple[bool, str, str | None]":
    """Stop the Sparrow this install owns and prove it is gone: (quiet, how, manager); the
    manager ("unit" or "detached") is what starts it again. Never a hand-made unit's."""
    return _keepalive.quiesce_sparrow(cfg)


def start_sparrow(cfg: dict, manager: "str | None") -> "tuple[bool, str]":
    """Start Sparrow through the manager that was stopped, and through nothing else."""
    owner = _keepalive._unit_owner()
    expected = {"unit": "ours", "detached": None}.get(manager, manager)
    if manager not in ("unit", "detached") or owner != expected:
        return False, (f"Sparrow was stopped through {manager or 'no manager'} and "
                       f"{_cli.service_path()} is now {owner or 'absent'}; not started")
    return _keepalive.restart_listener(cfg)


def _rollback(cfg: dict, state_dir: Path, previous: str, target: str, why: str,
              manager: "str | None" = None) -> str:
    """Restore `previous` and check it: "rolled-back" only when the namespace hashes equal
    to its snapshot, install re-ran and Sparrow runs it connected, else "restore-failed"
    with what is installed. No file changes unless Sparrow is proven stopped."""
    print(f"upgrade to {target} failed ({why}); restoring {previous}", file=sys.stderr)
    _phase(state_dir, target, "restoring")
    problems = []
    quiet, how, stopped_by = stop_sparrow(cfg)
    manager = stopped_by or manager
    if not quiet:
        problems.append(f"stop: {how}; nothing was restored")
    else:
        try:
            restore(state_dir, previous)
        except (OSError, ValueError) as e:
            problems.append(f"restore: {e}")
        else:
            r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
            if r.returncode != 0:
                problems.append(f"install: {_why(r)}")
        since = time.time()
        started, how = start_sparrow(cfg, manager)
        if not started:
            problems.append(f"start: {how}")
        elif _keepalive._await_connected(state_dir, _keepalive._instance(cfg), since) != 0:
            problems.append("Sparrow did not reconnect")
    installed = installed_version()
    if installed != previous:
        problems.append(f"{installed or 'no version'} is installed, not {previous}")
    outcome = "restore-failed" if problems else "rolled-back"
    if problems:
        print(f"error: the rollback to {previous} failed: {'; '.join(problems)}",
              file=sys.stderr)
    _finish(state_dir, target, outcome,
            failed={"target": target, "from": previous, "why": why, "at": time.time(),
                    "outcome": outcome, "installed": installed, "problems": problems})
    return outcome


def run_upgrade(cfg: dict, target: str, hashes: dict) -> str:
    """Upgrade to `target`, pinned to `hashes`, or roll back; the outcome. Skipped when
    another upgrade holds the lock."""
    state_dir = _keepalive._state_dir(cfg)
    with upgrade_lock(state_dir) as held:
        if not held:
            print("upgrade already running; skipped", file=sys.stderr)
            return "already-running"
        return _upgrade_locked(cfg, state_dir, target, hashes)


def _upgrade_locked(cfg: dict, state_dir: Path, target: str, hashes: dict) -> str:
    dist = _distribution()
    why = unsupported_install(dist)
    if why:
        print(f"upgrade: skipped: {why}", file=sys.stderr)
        _finish(state_dir, target, "unsupported-install")
        return "unsupported-install"
    # Read again under the lock: the queued target may be stale by now.
    previous = dist.version
    want, have = parse_version(target), parse_version(previous)
    if want is None or have is None or want <= have:
        print(f"upgrade: skipped: {previous} is installed, not below {target}", file=sys.stderr)
        _finish(state_dir, target, "not-above-installed")
        return "not-above-installed"
    try:
        snapshot(state_dir, previous, dist)
    except (OSError, ValueError) as e:
        print(f"upgrade: skipped: no rollback snapshot of {previous}: {e}", file=sys.stderr)
        _finish(state_dir, target, "no-snapshot")
        return "no-snapshot"
    _phase(state_dir, target, "snapshot", **{"from": previous})
    baseline = own_check_problems()
    if baseline is None:
        print("upgrade: skipped: pip check could not run", file=sys.stderr)
        _finish(state_dir, target, "no-pip-check")
        return "no-pip-check"
    quiet, how, manager = stop_sparrow(cfg)
    if not quiet:
        print(f"upgrade: Sparrow is not proven stopped ({how}); nothing was installed",
              file=sys.stderr)
        if manager:
            start_sparrow(cfg, manager)
        _finish(state_dir, target, "stop-failed")
        return "stop-failed"
    _phase(state_dir, target, "quiesced", manager=manager)

    def rollback(why: str) -> str:
        return _rollback(cfg, state_dir, previous, target, why, manager)
    _phase(state_dir, target, "installing")
    r = pip_install(state_dir, target, hashes)
    if r.returncode != 0:
        return rollback(f"pip install: {_why(r)}")
    _phase(state_dir, target, "installed")
    after = own_check_problems()
    if after is None:
        return rollback("pip check could not run")
    new = sorted(after - baseline)
    if new:
        return rollback(f"pip check: {new[0]}")
    r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
    if r.returncode != 0:
        return rollback(f"install: {_why(r)}")
    hook = next((line.strip() for line in (r.stdout or "").splitlines()
                 if line.startswith("REQUIRED hook:")), None)
    since = time.time()
    ok, how = start_sparrow(cfg, manager)
    if not ok:
        return rollback(f"start: {how}")
    if installed_version() != target:
        return rollback("the new version does not import")
    if _keepalive._await_connected(state_dir, _keepalive._instance(cfg), since) != 0:
        return rollback("Sparrow did not reconnect")
    state = load_state(state_dir)
    state.pop("failed", None)
    state["installed"] = {"version": target, "sha256": hashes, "at": time.time()}
    state["attempted"].setdefault(target, {}).update(outcome="ok", phase="ok")
    if hook:
        state["attempted"][target]["hook"] = hook
        _cli._write(notice_path(state_dir),
                    f"ag2-sparrow upgraded itself to {target}, and its hook definition changed; "
                    f"the change was not applied: {hook}\n", 0o600)
    save_state(state_dir, state)
    print(f"upgraded: {previous} -> {target}" + (f"; {hook} (not applied)" if hook else ""))
    return "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag2-sparrow-hatch-upgrade")
    ap.add_argument("target")
    ap.add_argument("hashes", help="JSON {wheel filename: sha256}")
    args = ap.parse_args(argv)
    try:
        hashes = valid_hashes(json.loads(args.hashes))
    except ValueError:
        hashes = None
    if parse_version(args.target) is None or hashes is None:
        print("error: target must be a version and hashes a {filename: sha256} map",
              file=sys.stderr)
        return 2
    cfg = _cli._installed_config()
    cfg.setdefault("STATE_DIR", str(_dirs.state_dir()))
    return 0 if run_upgrade(cfg, args.target, hashes) == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
