#!/usr/bin/env python3
"""The host's core gateway bridge as sparrowd runs it (`src/sparrowd.py --core`).

`decide` names the bridge the launchd wrapper would run: the legacy unsuffixed one while its row is not
parked and it has a token, else the named primary lane, else none (a reason instead). `main` builds that
bridge's environment the way its usual launcher does — the legacy one from `launchd/gateway-bridge-lib.sh`
(the wrapper's own copy), with the proactive room dropped once a named row holds the primary; a named lane
from `connections.launch_env` — evicts a stray bridge of this checkout under that name, then execs the
loader in place. One process with the wrapper child's argv, so every probe matching that child matches
this one. Unlike a declared agent bridge it keeps the host's owner DM, device env and proactive room.

`write_claim` records, from sparrowd's own process, which lane its core serves; `claims` is the lane
keeper's check, so it leaves that lane to sparrowd while sparrowd is alive (with a grace: see
`connections.DECLARED_BRIDGE_GRACE_S`).

`revision` is what sparrowd restarts the core on: the decision, the token file's bytes and the row's
token_ref, gateway URL and agent. An exit 75 (`HOLD_RC`) is the bridge's stand-down; sparrowd holds it
until that revision changes. A restart is told the previous exit code (SPARROWD_LAST_RC): 69, the
bridge's poll stall, and 78, this launcher's own failure (no bridge ran), raise no alert; any other code
the wrapper's alert, with its deliberate-restart window and cooldown.

One supervisor per bridge: a bridge of this checkout already serving the lane is evicted only when it
is a stray (its parent gone, or this sparrowd); one another live process supervises (an app keepalive,
the gate-off wrapper) is refused, and the launcher retries with backoff until that supervisor is gone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
for _p in (str(_SRC), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import connections  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

from ag2_sparrow.exit_codes import POLL_STALL_EXIT_CODE  # noqa: E402

import workspace_lock  # noqa: E402

LOADER = _SRC / "remote-gateway-bridge.py"
LIB = _SRC / "launchd" / "gateway-bridge-lib.sh"
EVICT = _SRC / "launchd" / "evict-own-bridge.sh"
HOLD_RC = frozenset({75})
POLL_STALL_RC = str(POLL_STALL_EXIT_CODE)
LAUNCHER_FAILED_RC = 78  # EX_CONFIG: the launcher could not start a bridge; no bridge exited
LAST_RC_ENV = "SPARROWD_LAST_RC"
LOG_TAG = "sparrow-core-bridge"


CLAIM = "core.json"


class CoreUnavailable(Exception):
    """The core's bridge or its environment could not be determined now: not "no bridge"."""


def _log(msg: str) -> None:
    print(f"[{LOG_TAG}] {msg}", file=sys.stderr, flush=True)


def legacy_env(environ: Mapping[str, str], repo: Path = REPO, run: Callable = subprocess.run) -> dict:
    """The legacy bridge's environment exactly as the wrapper builds it from `environ`."""
    try:
        out = run(["bash", str(Path(repo) / "src" / "launchd" / LIB.name), "env"], cwd=str(repo),
                  env={**environ, "REPO": str(repo)}, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise CoreUnavailable(f"the legacy environment did not build ({type(e).__name__})") from None
    lines = (out.stdout or "").strip().splitlines()
    try:
        env = json.loads(lines[-1]) if out.returncode == 0 and lines else None
    except ValueError:
        env = None
    if not isinstance(env, dict):
        raise CoreUnavailable(f"the legacy environment did not build (exit {out.returncode})")
    if "REPO" not in environ:
        env.pop("REPO", None)  # the wrapper's $REPO is a shell variable, never exported
    return env


_LEGACY_ENV_CACHE: dict = {}
_TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_TIER", "AG2_REMOTE_TIER")


def _digest_file(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else ""
    except OSError:
        return "unreadable"


def _channel_dir_digest() -> str:
    """Every file of the ag2space channel dir, by name and bytes: what the lib reads the token from."""
    from util_paths import claude_home_path
    h = hashlib.sha256()
    try:
        for f in sorted(claude_home_path("channels", "ag2space").iterdir()):
            if f.is_file():
                h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    except OSError:
        h.update(b"unreadable")
    return h.hexdigest()


def cached_legacy_env(environ: Mapping[str, str], legacy: Callable) -> dict:
    """`legacy(environ)`, rebuilt only when what it reads changed: the environment's token and tier, the
    channel dir's files, the last token file's bytes. A rescan then forks the lib only on a change."""
    last = _LEGACY_ENV_CACHE.get("env") or {}
    key = (tuple(environ.get(k, "") for k in _TOKEN_VARS), _channel_dir_digest(),
           _digest_file(str(last.get("REMOTE_TASK_TOKEN_FILE") or "")))
    if _LEGACY_ENV_CACHE.get("key") != key or _LEGACY_ENV_CACHE.get("legacy") is not legacy:
        last = legacy(environ)
        key = key[:2] + (_digest_file(str(last.get("REMOTE_TASK_TOKEN_FILE") or "")),)
        _LEGACY_ENV_CACHE.update(key=key, env=last, legacy=legacy)
    return dict(last)


def decide(workspace: Path, environ: Mapping[str, str], *,
           legacy: Optional[Callable] = None, cached: bool = False) -> "tuple[Optional[dict], str]":
    """({instance, primary, parked, env?}, "") for the bridge the core runs, or (None, why not). The
    legacy decision carries its built environment (`cached`: from `cached_legacy_env`, as a rescan asks);
    a named one only its plan entry (no vault read)."""
    parked = connections.legacy_parked(workspace)
    primary = connections.primary_instance(workspace)
    if not parked:
        build = legacy or legacy_env
        env = cached_legacy_env(environ, build) if cached else build(environ)
        if env.get("REMOTE_TASK_TOKEN"):
            if primary:
                env["REMOTE_PROACTIVE_ROOM"] = ""
            return {"instance": "", "primary": primary, "parked": False, "env": env}, ""
    if primary:
        kenv = connections.keeper_environ(environ)
        try:
            entry = connections.plan_entry(primary, kenv, workspace=workspace)
        except connections.ConnectionsError as e:
            raise CoreUnavailable(f"the primary connection {primary} is not launchable ({e})") from None
        return {"instance": primary, "primary": primary, "parked": parked, "entry": entry, "environ": kenv}, ""
    if parked:
        return None, "the legacy connection is parked and no named connection is primary"
    return None, "no REMOTE_TASK_TOKEN configured and no named connection is primary"


def claim_path(workspace: Path) -> Path:
    return Path(workspace) / "state" / "sparrowd" / CLAIM


def write_claim(workspace: Path, instance: Optional[str], pid: Optional[int] = None) -> None:
    """Record that the live process `pid` (this one by default) supervises `instance` as its core; None
    withdraws the claim. Rewritten only when it changes."""
    path = claim_path(workspace)
    if instance is None:
        path.unlink(missing_ok=True)
        return
    pid = pid or os.getpid()
    body = json.dumps({"instance": instance, "pid": pid, "proc_start": workspace_lock.proc_start(pid)},
                      sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == body:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{CLAIM}.{os.getpid()}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def holds_lock(workspace: Path, pid: int) -> bool:
    """Whether `pid` is the sparrowd holding this workspace's single-instance lock (the holder writes its
    pid there; one waiting for the lock writes nothing)."""
    from ag2_sparrow.sparrowd import LOCK_NAME
    try:
        return (Path(workspace) / "state" / "sparrowd" / LOCK_NAME).read_text().strip() == str(pid)
    except OSError:
        return False


def claims(instance: str, workspace: Path) -> bool:
    """Whether a live sparrowd claims `instance` as its core: the claimed pid holds sparrowd's lock, runs
    and, when both are known, started when the claim says. A missing or unreadable claim claims nothing."""
    try:
        claim = json.loads(claim_path(workspace).read_text(encoding="utf-8"))
        pid = claim["pid"]
        if claim["instance"] != instance or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not holds_lock(workspace, pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # alive under another uid
    started, now = claim.get("proc_start"), workspace_lock.proc_start(pid)
    return not (isinstance(started, str) and started and now is not None and not workspace_lock.same_start(started, now))


def _row(instance: str, workspace: Path) -> dict:
    return next((r for r in connections.read_rows(workspace) if r.get("instance") == instance), {})


def token_file(decision: Mapping[str, object]) -> str:
    if decision["instance"]:
        return connections.lane_token_file(decision["entry"], decision["environ"])  # type: ignore[arg-type]
    return str(decision["env"].get("REMOTE_TASK_TOKEN_FILE") or "")  # type: ignore[union-attr]


def revision(decision: Mapping[str, object], workspace: Path) -> str:
    path = token_file(decision)
    held = _digest_file(path)
    row = _row(str(decision["instance"]), workspace)
    fields = [str(decision["instance"]), str(decision["primary"] or ""), str(bool(decision["parked"])), path, held]
    fields += [str(row.get(k) or "") for k in ("token_ref", "gateway_url", "agent_mxid")]
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def bridge_env(decision: Mapping[str, object], vault=None) -> dict:
    """The environment the decided bridge runs under; `connections.ConnectionsError` when a named lane's
    token or identity is missing."""
    if not decision["instance"]:
        return dict(decision["env"])  # type: ignore[arg-type]
    return connections.launch_env(decision["entry"], decision["environ"], vault=vault)  # type: ignore[arg-type]


def evict_stray(instance: str, workspace: Path, repo: Path = REPO, run: Callable = subprocess.run,
                environ: Optional[Mapping[str, str]] = None) -> int:
    """Stop a bridge of this checkout already serving `instance` ("" = the legacy bridge), as the wrapper
    does before its own launch: two pollers would double-process. Non-zero when one is left running."""
    try:
        proven = str(connections.bridge_pid(instance, workspace) or "")
    except connections.BridgeLeaseIndeterminate:
        proven = ""
    env = {**(os.environ if environ is None else environ), "EVICT_PROVEN_PID": proven,
           "EVICT_WAIT_S": os.environ.get("SUTANDO_GATEWAY_EVICT_WAIT_S", "15")}
    return run(["bash", str(Path(repo) / "src" / "launchd" / EVICT.name), "remote-gateway", str(repo),
                "GATEWAY_INSTANCE", instance], env=env).returncode


def _ppid(pid: int, run: Callable = subprocess.run) -> int:
    try:
        out = run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def outside_supervisor(instance: str, workspace: Path, ppid: Callable = _ppid) -> str:
    """The lease holder serving `instance` with a live parent other than this sparrowd: another supervisor
    keeps it, so evicting it would start an eviction loop. "" for a stray (orphaned) bridge or none."""
    try:
        holder = connections.bridge_pid(instance, workspace)
    except connections.BridgeLeaseIndeterminate:
        return ""
    parent = ppid(holder) if holder else 0
    if parent in (0, 1, os.getppid()):
        return ""
    return f"pid {holder} (parent pid {parent})"


def alias_holder(instance: str, workspace: Path) -> int:
    """The pid serving a named lane under one of its launch-key aliases, 0 when none."""
    for alias in connections.alias_instances(_row(instance, workspace)):
        pid = connections.bridge_pid(alias, workspace)
        if pid:
            return pid
    return 0


def restart_alert(last_rc: Optional[str], workspace: Path, repo: Path = REPO, run: Callable = subprocess.run) -> None:
    """The wrapper's restart alert for a restart sparrowd made; none on a first start or a poll stall."""
    if last_rc is None:
        return
    if last_rc == POLL_STALL_RC:
        _log(f"bridge exited on a poll stall (rc {POLL_STALL_RC}); relaunching without an owner alert")
        return
    if last_rc == str(LAUNCHER_FAILED_RC):
        _log("the last start failed before any bridge ran; no owner alert")
        return
    run(["bash", str(Path(repo) / "src" / "launchd" / LIB.name), "alert"],
        env={**os.environ, "REPO": str(repo), "WORKSPACE": str(workspace), "GW_LOG_TAG": LOG_TAG})


def _lane_log(instance: str, workspace: Path) -> None:
    """A named lane logs where its keeper-launched bridge does, not into sparrowd's stream."""
    path = workspace / "logs" / f"remote-gateway-bridge.{instance}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def main(argv=None, *, workspace: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None,
         execve: Callable = os.execve, evict: Callable = evict_stray, alert: Callable = restart_alert,
         vault=None, legacy: Optional[Callable] = None, outside: Callable = outside_supervisor) -> int:
    ap = argparse.ArgumentParser(description="run the host's core gateway bridge in place (sparrowd --core)")
    ap.add_argument("--expect", default=None, metavar="INSTANCE",
                    help="the bridge sparrowd's scan decided on ('' = legacy); another one now stands down")
    args = ap.parse_args(argv)
    ws = workspace or resolve_workspace()
    environ = dict(os.environ if environ is None else environ)
    last_rc = environ.pop(LAST_RC_ENV, None)
    try:
        decision, why = decide(ws, environ, legacy=legacy)
    except (CoreUnavailable, connections.ConnectionsError) as e:
        _log(f"core bridge not started: {e}")
        return LAUNCHER_FAILED_RC
    if decision is None:
        _log(f"nothing to run: {why}; held until the connections change")
        return 75
    inst = str(decision["instance"])
    if args.expect is not None and args.expect != inst:
        _log(f"the core bridge is now {inst or 'the legacy one'}, not {args.expect or 'the legacy one'}; "
             "standing down for sparrowd's next scan")
        return 75
    try:
        env = bridge_env(decision, vault=vault)
        holder = alias_holder(inst, ws) if inst else 0
    except connections.ConnectionsError as e:
        _log(f"core bridge {inst or '(legacy)'} not started: {e}")
        return LAUNCHER_FAILED_RC
    if holder:
        _log(f"lane {inst} already runs under an alias (pid {holder}); retrying with backoff")
        return LAUNCHER_FAILED_RC
    owner = outside(inst, ws)
    if owner:
        _log(f"refusing to start: {inst or 'the legacy bridge'} is already supervised outside sparrowd: {owner}. "
             "Stop that supervisor first; retrying with backoff")
        return LAUNCHER_FAILED_RC
    rc = evict(inst, ws)
    if rc != 0:
        _log(f"eviction did not complete (rc {rc}); not launching")
        return LAUNCHER_FAILED_RC
    alert(last_rc, ws)
    argv_exec = [sys.executable, str(LOADER)]
    if inst:
        connections.record_supervised_start(inst, pid=os.getpid(), repo=REPO, workspace=ws)
        _lane_log(inst, ws)
    execve(sys.executable, argv_exec, env)
    return 0  # reached only with an injected execve


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
