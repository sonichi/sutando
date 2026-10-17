"""Self-upgrade for a Hatch install: move ag2-sparrow up to the backend's target_version.

The keepawake run starts it (`maybe_start`) at most once per target version, and only up:
never to a lower or equal version, never when min_version is above the target, never
without the target's sha256 map (wheel filename -> sha256), never with AUTO_UPGRADE=0.
The upgrade runs detached (`run_upgrade`), since the scheduler's timeout would cut it
short: `pip install --no-deps --require-hashes` of the pinned version into this
interpreter, `pip check`, install re-run non-interactively, Sparrow restarted, then
verified (the new version imports and Sparrow reconnects). Any failure reinstalls the
previous version, hash-pinned when an earlier upgrade recorded its hashes, restarts
Sparrow, and leaves the health reason upgrade-failed. A changed hook definition
(install's REQUIRED hook: line) is never applied: the hook wakes the agent once with it."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ... import __version__, _dirs
from . import cli as _cli, keepalive as _keepalive

PACKAGE = "ag2-sparrow"
STATE_NAME = "hatch-upgrade.json"
NOTICE_NAME = "hatch-wake-notice"
LOG_NAME = "hatch-upgrade.log"
UPGRADE_FAILED = "upgrade-failed"
PIP_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 300
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?")
_PRE = {"a": 0, "b": 1, "rc": 2}


def parse_version(text) -> "tuple | None":
    """A sortable key for a plain PEP 440 version; None for anything else, which is never
    passed to pip."""
    m = _VERSION_RE.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        return None
    release = tuple(int(p) for p in m.group(1).split("."))
    release = release + (0,) * (6 - len(release)) if len(release) <= 6 else release
    pre, post, dev = m.group(2), m.group(4), m.group(5)
    # A bare .devN sorts before every pre-release of its release.
    stage = (_PRE[pre], int(m.group(3))) if pre else ((-1, 0) if dev and post is None else (3, 0))
    return (release, stage, int(post) if post else -1,
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


def valid_hashes(value) -> "dict | None":
    """A non-empty {wheel filename: sha256} map, else None."""
    if not isinstance(value, dict) or not value:
        return None
    ok = all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}", k)
             and isinstance(v, str) and SHA256_RE.fullmatch(v) for k, v in value.items())
    return dict(value) if ok else None


def pip_install(state_dir: Path, version: str,
                hashes: "dict | None") -> subprocess.CompletedProcess:
    """Install exactly `version` with no dependency changes, pinned to every sha256 in
    `hashes` when given. pip accepts --hash only in a requirements file, so it goes there."""
    pip = [sys.executable, "-m", "pip", "install", "--no-deps", "--disable-pip-version-check"]
    if hashes is None:
        return _run([*pip, f"{PACKAGE}=={version}"], PIP_TIMEOUT_S)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fd, req = tempfile.mkstemp(dir=str(state_dir), prefix=".upgrade-", suffix=".txt")
    pins = " ".join(f"--hash=sha256:{h}" for h in sorted(set(hashes.values())))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{PACKAGE}=={version} {pins}\n")
        return _run([*pip, "--require-hashes", "-r", req], PIP_TIMEOUT_S)
    finally:
        Path(req).unlink(missing_ok=True)


def pip_check() -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "pip", "check"], PIP_TIMEOUT_S)


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


def maybe_start(cfg: dict, setup: "dict | None", installed: str = __version__,
                now: "float | None" = None) -> "str | None":
    """Start an upgrade to the served target when one is due; what was done, or None."""
    if (cfg.get("AUTO_UPGRADE") or "").strip() == "0":
        return None
    package = (setup or {}).get("package")
    package = package if isinstance(package, dict) else {}
    target, hashes = package.get("target_version"), valid_hashes(package.get("target_sha256"))
    want, have = parse_version(target), parse_version(installed)
    if want is None or have is None:
        return None
    if want < have:
        print(f"NOTE upgrade: target_version {target} is below the installed {installed}; "
              "nothing done", file=sys.stderr)
        return None
    if want == have:
        return None
    floor = package.get("min_version")
    if floor is not None and (parse_version(floor) is None or parse_version(floor) > want):
        print(f"NOTE upgrade: min_version {floor!r} is above target_version {target}; "
              "nothing done", file=sys.stderr)
        return None
    if hashes is None:
        print(f"NOTE upgrade: {target} has no valid target_sha256 map, so it was not installed",
              file=sys.stderr)
        return None
    state_dir = _keepalive._state_dir(cfg)
    state = load_state(state_dir)
    if target in state["attempted"]:
        return None
    # Recorded before the start, so a crash mid-upgrade is never retried for this target.
    state["attempted"][target] = {"at": time.time() if now is None else now,
                                  "from": installed, "outcome": "started"}
    save_state(state_dir, state)
    argv = [sys.executable, "-m", "ag2_sparrow.adapters.hatch.self_upgrade", target,
            json.dumps(hashes, sort_keys=True)]
    if not _spawn(argv, state_dir / LOG_NAME):
        _finish(state_dir, target, "not-started")
        return f"{target}:not-started"
    return f"{target}:started"


def _finish(state_dir: Path, target: str, outcome: str, **extra) -> None:
    state = load_state(state_dir)
    state["attempted"].setdefault(target, {}).update(outcome=outcome)
    state.update(extra)
    save_state(state_dir, state)


def _restart(cfg: dict) -> "tuple[bool, str]":
    holder = _cli.live_lock_holder(_cli._lock_record(_keepalive._state_dir(cfg),
                                                     _keepalive._instance(cfg)))
    if holder is None:
        return _keepalive.restart_listener(cfg)
    return _keepalive.replace_sparrow(cfg, holder, why="to run the upgraded ag2-sparrow")


def _rollback(cfg: dict, state_dir: Path, previous: str, target: str, why: str) -> str:
    print(f"upgrade to {target} failed ({why}); reinstalling {previous}", file=sys.stderr)
    record = load_state(state_dir).get("installed") or {}
    hashes = valid_hashes(record.get("sha256")) if record.get("version") == previous else None
    if hashes is None:
        print(f"NOTE upgrade: no sha256 is recorded for {previous}, so it is reinstalled "
              "without a hash pin", file=sys.stderr)
    r = pip_install(state_dir, previous, hashes)
    if r.returncode != 0:
        print(f"error: reinstalling {previous} failed: {_why(r)}", file=sys.stderr)
    else:
        _run(install_argv(cfg), INSTALL_TIMEOUT_S)
    ok, how = _restart(cfg)
    if not ok:
        print(f"error: Sparrow did not restart after the rollback: {how}", file=sys.stderr)
    _finish(state_dir, target, "rolled-back",
            failed={"target": target, "from": previous, "why": why, "at": time.time()})
    return "rolled-back"


def run_upgrade(cfg: dict, target: str, hashes: dict, previous: str = __version__) -> str:
    """Upgrade to `target`, pinned to `hashes`, or roll back to `previous`; the outcome."""
    state_dir = _keepalive._state_dir(cfg)
    r = pip_install(state_dir, target, hashes)
    if r.returncode != 0:
        return _rollback(cfg, state_dir, previous, target, f"pip install: {_why(r)}")
    r = pip_check()
    if r.returncode != 0:
        return _rollback(cfg, state_dir, previous, target, f"pip check: {_why(r)}")
    r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
    if r.returncode != 0:
        return _rollback(cfg, state_dir, previous, target, f"install: {_why(r)}")
    hook = next((line.strip() for line in (r.stdout or "").splitlines()
                 if line.startswith("REQUIRED hook:")), None)
    since = time.time()
    ok, how = _restart(cfg)
    if not ok:
        return _rollback(cfg, state_dir, previous, target, f"restart: {how}")
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
