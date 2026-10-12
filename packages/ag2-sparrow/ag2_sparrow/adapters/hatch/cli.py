"""`ag2-sparrow-hatch install|verify|sparrow`: set up and check the Hatch hook on this host.

install writes the hook script to ~/hooks/scripts/, the hook definition to
~/hooks/definitions/ and the config the script reads; verify checks that setup;
sparrow checks only that Sparrow is running (the hook's heartbeat runs it)."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

from ... import _dirs
from .task_fields import valid_mxid

HOOK_ID = "ag2-sparrow-hatch"
SCRIPT_NAME = f"{HOOK_ID}.sh"
HEARTBEAT_INTERVAL_S = 300
# Sparrow rewrites its status file every poll round (long poll or backoff, each
# at most about a minute), so an older file means it is not running.
SPARROW_STATUS_MAX_AGE_S = 180
# sha256 of hook scripts shipped before install recorded what it wrote (0.5.0).
RELEASED_SCRIPT_SHA256 = frozenset({
    "c68a6fce020a5aaea9f5c593021daccb95aeaf15ba6962d0303e42933ee23fb7",
})
# Same charset as remote_gateway_bridge._INSTANCE_RE.
INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
# `sparrow` exit codes, read by the hook; 1 stays a crash, 2 a usage error.
SPARROW_OK, SPARROW_DOWN, SPARROW_NOT_CONNECTED = 0, 10, 11


def _shipped(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def scripts_dir() -> Path:
    return _home() / "hooks" / "scripts"


def definitions_dir() -> Path:
    return _home() / "hooks" / "definitions"


def config_path() -> Path:
    env = os.environ.get("SPARROW_HATCH_CONFIG")
    return Path(env) if env else _home() / ".config" / HOOK_ID / "config"


def installed_marker_path() -> Path:
    """sha256 of the hook script install last wrote, so an upgrade can tell its own
    earlier copy from a local edit."""
    return config_path().with_name("installed-script.sha256")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ours() -> frozenset:
    try:
        recorded = installed_marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    return RELEASED_SCRIPT_SHA256 | ({recorded} if recorded else set())


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{path} (installed)", f"{path} (ag2-sparrow)"))


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _install_file(path: Path, text: str, mode: int, *, refuse_drift: bool, force: bool,
                  ours: frozenset = frozenset()) -> bool:
    """Write `text` to `path`; print a diff for a differing copy that is not one of
    `ours` (sha256 of copies ag2-sparrow wrote). False = refused."""
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            os.chmod(path, mode)
            print(f"unchanged: {path}")
            return True
        if _sha256(old) in ours:
            _write(path, text, mode)
            print(f"upgraded: {path}")
            return True
        print(f"DIFF: {path} differs from the ag2-sparrow copy:")
        print(_diff(old, text, path), end="")
        if refuse_drift and not force:
            print(f"REFUSED: {path} was modified locally; keep it, or re-run with --force "
                  "to replace it with the copy above", file=sys.stderr)
            return False
    _write(path, text, mode)
    print(f"installed: {path}")
    return True


def render_config(mxid: str, task_dir: Path, result_dir: Path, activity_file: Path,
                  python: str, state_dir: Path, gateway_instance: str = "") -> str:
    rows = (("AGENT_MXID", mxid), ("TASK_DIR", str(task_dir)), ("RESULT_DIR", str(result_dir)),
            ("ACTIVITY_FILE", str(activity_file)), ("SPARROW_PYTHON", python),
            ("STATE_DIR", str(state_dir)), ("GATEWAY_INSTANCE", gateway_instance))
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in rows)


def read_config(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [raw]
        out[key.strip()] = parts[0] if parts else ""
    return out


def install(args) -> int:
    if not valid_mxid(args.mxid):
        print(f"error: {args.mxid!r} is not an MXID like @my-agent:ag2.space", file=sys.stderr)
        return 2
    instance = (os.environ.get("GATEWAY_INSTANCE") if args.gateway_instance is None
                else args.gateway_instance)
    instance = (instance or "").strip()
    if instance and not INSTANCE_RE.fullmatch(instance):
        print(f"error: GATEWAY_INSTANCE {instance!r} must match {INSTANCE_RE.pattern}",
              file=sys.stderr)
        return 2
    task_dir = Path(args.task_dir) if args.task_dir else _dirs.task_dir()
    result_dir = Path(args.result_dir) if args.result_dir else _dirs.result_dir()
    state_dir = Path(args.state_dir) if args.state_dir else _dirs.state_dir()
    activity = Path(args.activity_file) if args.activity_file else _home() / ".muse" / "last_active"
    script = scripts_dir() / SCRIPT_NAME
    shipped = _shipped(SCRIPT_NAME)
    if not _install_file(script, shipped, 0o755, refuse_drift=True, force=args.force,
                         ours=_ours()):
        return 1
    _write(installed_marker_path(), _sha256(shipped) + "\n", 0o644)
    definition = _shipped("hook-definition.json").replace("<AGENT_MXID>", args.mxid)
    _install_file(definitions_dir() / f"{HOOK_ID}.json", definition, 0o644,
                  refuse_drift=False, force=True)
    config = render_config(args.mxid, task_dir.resolve(), result_dir.resolve(),
                           activity, sys.executable, state_dir.resolve(), instance)
    _install_file(config_path(), config, 0o600, refuse_drift=False, force=True)
    activity.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nNext: register the hook with hooks.add using {definitions_dir() / f'{HOOK_ID}.json'}, "
          "test it with hooks.dry_run, start it with hooks.enable, then run: ag2-sparrow-hatch verify")
    return 0


def _check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


RESTART_HINT = ("restart it: systemctl --user restart ag2-sparrow under the README's user "
                "service, else start ag2-sparrow again as at install")


def sparrow_status_path(state_dir: Path, instance: str | None = None) -> Path:
    """`instance` None reads GATEWAY_INSTANCE from the environment, as Sparrow does."""
    instance = (os.environ.get("GATEWAY_INSTANCE") if instance is None else instance) or ""
    instance = instance.strip()
    return state_dir / f"gateway-status{'.' + instance if instance else ''}.json"


def check_sparrow(state_dir: Path, now: float | None = None,
                  instance: str | None = None) -> tuple[int, str]:
    """SPARROW_OK, SPARROW_DOWN or SPARROW_NOT_CONNECTED, with a detail, judged by the
    status file Sparrow rewrites each poll."""
    path = sparrow_status_path(state_dir, instance)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        ts = float(status["ts"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return SPARROW_DOWN, (f"{path} unreadable ({type(e).__name__}); Sparrow has not run with "
                       f"this state dir; {RESTART_HINT}")
    age = int((time.time() if now is None else now) - ts)
    if age > SPARROW_STATUS_MAX_AGE_S:
        return SPARROW_DOWN, (f"{path} last written {age}s ago; Sparrow is not running, so no task "
                       f"reaches the hook; {RESTART_HINT}")
    if not status.get("connected"):
        return SPARROW_NOT_CONNECTED, (
            f"Sparrow is running but not connected: {status.get('error') or 'no error given'}; "
            "if the token was rejected, a restart does not help: put a valid relay token from "
            "the AG2 Space Agent Portal in REMOTE_TASK_TOKEN (the .env file AG2_DEVICE_ENV "
            "names), then restart Sparrow")
    return SPARROW_OK, f"running and connected ({path} written {max(age, 0)}s ago)"


def sparrow(args) -> int:
    code, detail = check_sparrow(Path(args.state_dir) if args.state_dir else _dirs.state_dir(),
                                 instance=args.gateway_instance)
    print(detail)
    return code


def verify(_args) -> int:
    results: list = []
    script = scripts_dir() / SCRIPT_NAME
    if not script.is_file():
        _check(results, False, "hook script", f"{script} missing; run ag2-sparrow-hatch install")
    elif not os.access(script, os.X_OK):
        _check(results, False, "hook script", f"{script} is not executable")
    elif script.read_text(encoding="utf-8", errors="replace") != _shipped(SCRIPT_NAME):
        _check(results, False, "hook script", f"{script} differs from this ag2-sparrow's copy")
    else:
        _check(results, True, "hook script", str(script))

    definition_path = definitions_dir() / f"{HOOK_ID}.json"
    definition: dict = {}
    try:
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
        bad = "<AGENT_MXID>" in json.dumps(definition) or \
            Path(os.path.expanduser(definition.get("script_path", ""))) != script
        _check(results, not bad, "hook definition",
               str(definition_path) if not bad else f"{definition_path} has a placeholder or "
               "a script_path that is not the installed script")
    except (OSError, ValueError) as e:
        _check(results, False, "hook definition", f"{definition_path}: {e}")

    cfg_path = config_path()
    cfg: dict = {}
    try:
        cfg = read_config(cfg_path)
        problems = []
        if not valid_mxid(cfg.get("AGENT_MXID", "")):
            problems.append("AGENT_MXID is not an MXID")
        for key in ("TASK_DIR", "RESULT_DIR", "STATE_DIR", "ACTIVITY_FILE", "SPARROW_PYTHON"):
            if not cfg.get(key):
                problems.append(f"{key} missing")
        if cfg.get("SPARROW_PYTHON"):
            probe = subprocess.run([cfg["SPARROW_PYTHON"], "-c",
                                    "import ag2_sparrow.adapters.hatch.task_fields"],
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                problems.append(f"{cfg['SPARROW_PYTHON']} cannot import ag2_sparrow")
        _check(results, not problems, "config",
               "; ".join(problems) if problems else f"{cfg_path} ({cfg['AGENT_MXID']})")
    except (OSError, subprocess.SubprocessError) as e:
        _check(results, False, "config", f"{cfg_path}: {e}")

    task_dir = Path(cfg.get("TASK_DIR") or _dirs.task_dir())
    readable = task_dir.is_dir() and os.access(task_dir, os.R_OK | os.X_OK)
    _check(results, readable, "task_dir",
           str(task_dir) if readable else f"{task_dir} is not a readable directory; start "
           "ag2-sparrow with the same AGENT_CONNECT_TASK_DIR")

    state_dir = Path(cfg.get("STATE_DIR") or _dirs.state_dir())
    code, detail = check_sparrow(state_dir, instance=cfg.get("GATEWAY_INSTANCE"))
    _check(results, code == SPARROW_OK, "sparrow", detail)

    missing = [tool for tool in ("jq", "flock", "timeout") if shutil.which(tool) is None]
    _check(results, not missing, "tools",
           "jq, flock, timeout on PATH" if not missing else f"missing on PATH: {', '.join(missing)}")

    activity = Path(cfg.get("ACTIVITY_FILE") or _home() / ".muse" / "last_active")
    interval = definition.get("poll_interval_secs")
    problems = []
    if not isinstance(interval, int) or not 0 < interval < HEARTBEAT_INTERVAL_S:
        problems.append(f"poll_interval_secs {interval!r} must be under {HEARTBEAT_INTERVAL_S}")
    if not (activity.parent.is_dir() and os.access(activity.parent, os.W_OK)):
        problems.append(f"{activity.parent} (activity marker dir) is not writable")
    _check(results, not problems, "heartbeat",
           "; ".join(problems) if problems else
           f"armed: a wake after {HEARTBEAT_INTERVAL_S}s quiet unless {activity} is fresher")

    mxid = cfg.get("AGENT_MXID") or "@your-agent:ag2.space"
    print(f"\nManual test: from another account, mention {mxid} in an AG2 Space room. Within a "
          "few polls a task file appears in the task_dir, the hook wakes the worker, and the "
          "reply lands in that message's thread tagging you. hooks.logs shows the wake.")
    return 0 if all(results) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag2-sparrow-hatch",
                                 description="Meta Hatch adapter for ag2-sparrow.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the Hatch hook script, definition and config")
    p.add_argument("--mxid", required=True, help="the agent's AG2 Space MXID")
    p.add_argument("--task-dir", help="Sparrow's task dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--result-dir", help="Sparrow's result dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--state-dir", help="Sparrow's state dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--activity-file", help="activity marker (default: ~/.muse/last_active)")
    p.add_argument("--gateway-instance", help="Sparrow's GATEWAY_INSTANCE (default: from the "
                   "environment, as ag2-sparrow reads it)")
    p.add_argument("--force", action="store_true", help="replace a locally modified hook script")
    p.set_defaults(func=install)
    v = sub.add_parser("verify", help="check the installed hook")
    v.set_defaults(func=verify)
    s = sub.add_parser("sparrow", help=f"check that Sparrow is running (exit {SPARROW_DOWN}) "
                       f"and connected (exit {SPARROW_NOT_CONNECTED})")
    s.add_argument("--state-dir", help="Sparrow's state dir (default: as ag2-sparrow resolves it)")
    s.add_argument("--gateway-instance", help="Sparrow's GATEWAY_INSTANCE (default: from the "
                   "environment)")
    s.set_defaults(func=sparrow)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
