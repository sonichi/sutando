"""Self-upgrade for a Hatch install: move ag2-sparrow up to the backend's target_version.

The keepawake run decides (`maybe_start`) and logs the decision on its line. First, under
the upgrade lock, it settles an attempt still marked started after 30 minutes: crashed,
with the snapshot restored if another version was left installed. Then at most one
attempt per target version, and only up; never when min_version is above the target,
without the target's sha256 map (wheel filename -> sha256), with AUTO_UPGRADE=0, or for
an editable or URL install. The upgrade runs detached (`run_upgrade`) under
STATE_DIR/upgrade.lock, which keepawake also holds across its probe and restart, and
first re-reads the installed version: a target not above it is skipped. Its order is
snapshot, stop, install, start, verify: the installed files are copied to
STATE_DIR/rollback/<version>/, Sparrow is stopped gracefully, `pip install --no-deps
--only-binary=:all: --require-hashes` puts the pinned wheel into this interpreter, `pip
check` must run and report no new problem naming ag2-sparrow, install is re-run with
--no-start (which never starts the service), and Sparrow is started and must import the
target and reconnect. A reply posted while Sparrow is stopped waits in the results dir
and its outbox, which the upgrade never touches, and is delivered once Sparrow starts.
Any failure restores the snapshot (no index, no network), re-runs install and starts
Sparrow; the outcome is "rolled-back" only when the previous version is installed again
and Sparrow reconnects, else "restore-failed" with what is installed. Either leaves the
health reason upgrade-failed. A changed hook definition (install's REQUIRED hook: line)
is never applied: the hook wakes the agent once with it."""
from __future__ import annotations

import argparse
import contextlib
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
PIP_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 300
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(
    r"([0-9]{1,9}(?:\.[0-9]{1,9}){0,9})(?:(a|b|rc)([0-9]{1,9}))?"
    r"(?:\.post([0-9]{1,9}))?(?:\.dev([0-9]{1,9}))?")
_PRE = {"a": 0, "b": 1, "rc": 2}
_OWN_RE = re.compile(r"ag2[-_.]sparrow", re.I)


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


def snapshot(state_dir: Path, version: str, dist) -> Path:
    """Copy every file the install recorded into STATE_DIR/rollback/<version>/."""
    dest = Path(state_dir) / ROLLBACK_DIR / version
    tmp = dest.with_name(f".{version}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "files").mkdir(parents=True)
    entries = []
    for i, path in enumerate(_installed_paths(dist)):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        name = f"{i:05d}"
        shutil.copy2(path, tmp / "files" / name)
        entries.append({"path": str(path), "copy": name})
    if not entries:
        raise ValueError("the install recorded no files")
    (tmp / "manifest.json").write_text(json.dumps({"version": version, "files": entries}),
                                       encoding="utf-8")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def restore(state_dir: Path, version: str) -> None:
    """Put the snapshot of `version` back in place of whatever is installed now."""
    snap = Path(state_dir) / ROLLBACK_DIR / version
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version or not manifest.get("files"):
        raise ValueError(f"the snapshot in {snap} is not one of {version}")
    current = _distribution()
    emptied = set()
    for path in _installed_paths(current) if current is not None else []:
        if path.is_file() or path.is_symlink():
            path.unlink()
            emptied.add(path.parent)
    for entry in manifest["files"]:
        target = Path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap / "files" / entry["copy"], target)
    for directory in sorted(emptied, key=lambda d: len(d.parts), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()  # only a directory the failed version left empty


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
    return "", "", hashes


def maybe_start(cfg: dict, setup: "dict | None", installed: str = __version__,
                now: "float | None" = None) -> "str | None":
    """Decide on the served target and start an upgrade when one is due. Returns the
    decision for the keepawake line; None only when there is no served setup."""
    now = time.time() if now is None else now
    state_dir = _keepalive._state_dir(cfg)
    # Before any decision: an interrupted upgrade must be settled even if its target is current.
    with upgrade_lock(state_dir) as held:
        crashed = _reconcile(cfg, state_dir, now) if held else None
    if crashed:
        return crashed
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


def _reconcile(cfg: dict, state_dir: Path, now: float) -> "str | None":
    """Settle an attempt still marked started after STALE_START_S, which no upgrade holds
    the lock for: it crashed. If it left another version installed, restore the snapshot."""
    state = load_state(state_dir)
    for target, tried in state["attempted"].items():
        at = tried.get("at") if isinstance(tried, dict) else None
        if not (isinstance(tried, dict) and tried.get("outcome") == "started"
                and isinstance(at, (int, float)) and now - at > STALE_START_S):
            continue
        previous, dist = str(tried.get("from") or ""), _distribution()
        current = dist.version if dist is not None else ""
        print(f"upgrade: {target} started {int(now - at)}s ago and never finished; "
              f"{current or 'no version'} is installed", file=sys.stderr)
        tried["outcome"] = "crashed"
        state["failed"] = {"target": target, "from": previous, "installed": current,
                           "why": "the upgrade did not finish", "at": now}
        save_state(state_dir, state)
        snap = Path(state_dir) / ROLLBACK_DIR / previous / "manifest.json"
        if previous and current != previous and snap.is_file():
            _rollback(cfg, state_dir, previous, target, "the upgrade did not finish")
        return f"{target}:crashed"
    return None


def _finish(state_dir: Path, target: str, outcome: str, **extra) -> None:
    state = load_state(state_dir)
    state["attempted"].setdefault(target, {}).update(outcome=outcome)
    state.update(extra)
    save_state(state_dir, state)


def stop_sparrow(cfg: dict) -> "tuple[bool, str]":
    """Stop Sparrow gracefully (SIGTERM, then wait); a stopped unit is not restarted."""
    owner = _keepalive._unit_owner()
    if owner == _keepalive.UNIT_UNREACHABLE:
        return False, (f"{_cli.service_path()} is installed but systemctl --user did not answer; "
                       "Sparrow was not stopped")
    if owner == "ours":
        r = _cli._systemctl(("--user", "stop", "ag2-sparrow.service"))
        return _cli._ok(r), f"systemctl --user stop {_cli.SERVICE_NAME} ({_cli._why(r)})"
    holder = _cli.live_lock_holder(_cli._lock_record(_keepalive._state_dir(cfg),
                                                     _keepalive._instance(cfg)))
    if holder is None:
        return True, "Sparrow was not running"
    return _keepalive._stop(holder), f"stopped Sparrow (pid {holder})"


def start_sparrow(cfg: dict) -> "tuple[bool, str]":
    return _keepalive.restart_listener(cfg)


def _rollback(cfg: dict, state_dir: Path, previous: str, target: str, why: str) -> str:
    """Restore `previous` and check it: "rolled-back" only when it is installed again and
    Sparrow runs it connected, else "restore-failed" with what is installed instead."""
    print(f"upgrade to {target} failed ({why}); restoring {previous}", file=sys.stderr)
    problems = []
    stopped, how = stop_sparrow(cfg)
    if not stopped:
        problems.append(f"stop: {how}")
    try:
        restore(state_dir, previous)
    except (OSError, ValueError) as e:
        problems.append(f"restore: {e}")
    else:
        r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
        if r.returncode != 0:
            problems.append(f"install: {_why(r)}")
    since = time.time()
    started, how = start_sparrow(cfg)
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
    baseline = own_check_problems()
    if baseline is None:
        print("upgrade: skipped: pip check could not run", file=sys.stderr)
        _finish(state_dir, target, "no-pip-check")
        return "no-pip-check"
    ok, how = stop_sparrow(cfg)
    if not ok:
        print(f"upgrade: Sparrow did not stop ({how}); nothing was installed", file=sys.stderr)
        start_sparrow(cfg)
        _finish(state_dir, target, "stop-failed")
        return "stop-failed"
    r = pip_install(state_dir, target, hashes)
    if r.returncode != 0:
        return _rollback(cfg, state_dir, previous, target, f"pip install: {_why(r)}")
    after = own_check_problems()
    if after is None:
        return _rollback(cfg, state_dir, previous, target, "pip check could not run")
    new = sorted(after - baseline)
    if new:
        return _rollback(cfg, state_dir, previous, target, f"pip check: {new[0]}")
    r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
    if r.returncode != 0:
        return _rollback(cfg, state_dir, previous, target, f"install: {_why(r)}")
    hook = next((line.strip() for line in (r.stdout or "").splitlines()
                 if line.startswith("REQUIRED hook:")), None)
    since = time.time()
    ok, how = start_sparrow(cfg)
    if not ok:
        return _rollback(cfg, state_dir, previous, target, f"start: {how}")
    if installed_version() != target:
        return _rollback(cfg, state_dir, previous, target, "the new version does not import")
    if _keepalive._await_connected(state_dir, _keepalive._instance(cfg), since) != 0:
        return _rollback(cfg, state_dir, previous, target, "Sparrow did not reconnect")
    state = load_state(state_dir)
    state.pop("failed", None)
    state["installed"] = {"version": target, "sha256": hashes, "at": time.time()}
    state["attempted"].setdefault(target, {}).update(outcome="ok")
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
