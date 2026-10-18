#!/usr/bin/env python3
"""`ag2-sparrow-connect claude-local`: host a local Claude Code session as an AG2 Space agent.

`install` records the session's paths in a 0600 config, writes a supervisor (a systemd user unit on
Linux, a launchd agent on macOS) whose first line is a marker, and starts it. `run` is what the
supervisor runs: it checks the CLI version, takes the one-session-per-inbox lock for the whole child,
starts `claude -p` in stream-json mode with the AG2 Space inbox plugin, which arms its inbox watch
itself from the launcher's env on every start, and holds stdin open so the session lives. `verify` checks
each part; `uninstall` removes only what install wrote. The relay token stays in Sparrow's own env
file, the Claude login stays in its config dir: neither is copied. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from ... import _dirs, holder_lock
from ...task_io import LockRefused, acquire_lock, release_lock

SHAPE = "claude-local"
COMMAND = "ag2-sparrow-connect"
MIN_CLI = (2, 1, 287)
MARKER = f"# Written by {COMMAND} {SHAPE} install; re-run install to update it."
PLIST_MARKER = f"<!-- Written by {COMMAND} {SHAPE} install; re-run install to update it. -->"
SERVICE_NAME = f"ag2-sparrow-{SHAPE}.service"
AGENT_LABEL = f"ai.ag2.sparrow.{SHAPE}"
# The marker the plugin reads at session.start: with it, the watch arms itself, no model turn.
AUTOARM_VAR = "AG2_AUTOARM"
STREAM_ARGV = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
ENV_KEEP = ("PATH", "HOME", "USER", "ANTHROPIC_BASE_URL")
EXIT_HELD = 75          # another session holds this inbox: a supervisor must not relaunch over it
EXIT_UNUSABLE = 2
CONFIG_KEYS = ("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "CLAUDE_CONFIG_DIR", "HMAC_KEY_FILE",
               "HMAC_STRICT", "PERMISSION_MODE")
RUN_TIMEOUT_S = 30


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def config_path() -> Path:
    return _home() / ".config" / "ag2-sparrow" / f"{SHAPE}.conf"


def service_path() -> Path:
    return _home() / ".config" / "systemd" / "user" / SERVICE_NAME


def plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def log_path() -> Path:
    return _home() / "Library" / "Logs" / "ag2-sparrow" / f"{SHAPE}.log"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def supervisor_path() -> Path:
    return plist_path() if is_macos() else service_path()


# ── the CLI pin ──────────────────────────────────────────────────────────────

def parse_version(text: str) -> "tuple[int, int, int] | None":
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def cli_version(claude: str, run=subprocess.run) -> "tuple[int, int, int] | None":
    """The installed CLI's version, or None when it cannot be read."""
    try:
        r = run([claude, "--version"], capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version((r.stdout or "") + (r.stderr or "")) if r.returncode == 0 else None


def version_problem(claude: str, run=subprocess.run) -> str:
    """One line when the CLI is missing or older than the pin, else ''."""
    found = cli_version(claude, run)
    if found is None:
        return f"{claude} --version did not answer; install the Claude Code CLI ({_v(MIN_CLI)} or newer)"
    if found < MIN_CLI:
        return (f"claude {_v(found)} is older than the {_v(MIN_CLI)} this adapter needs (the Mods API it "
                "uses arrived there); upgrade the Claude Code CLI")
    return ""


def _v(t) -> str:
    return ".".join(map(str, t))


# ── config, written by install, read by run/verify/uninstall ─────────────────

def render_config(values: dict) -> str:
    rows = "".join(f"{k}={shlex.quote(str(values.get(k, '')))}\n" for k in CONFIG_KEYS)
    return f"{MARKER}\n{rows}"


def read_config(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            cfg[k] = "".join(shlex.split(v)) if v else ""
    return cfg


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


def marked(text: str, marker: str) -> bool:
    return marker in text.splitlines()[:2]


# ── the supervisor ───────────────────────────────────────────────────────────

def console_script() -> str:
    """This package's console script, beside the running Python first, else on PATH, else '' ."""
    for d in (sysconfig.get_path("scripts"), os.path.dirname(sys.executable)):
        p = os.path.join(d, COMMAND) if d else ""
        if p and os.access(p, os.X_OK):
            return os.path.abspath(p)
    return shutil.which(COMMAND) or ""


def render_service(exec_start: str) -> str:
    return (f"{MARKER}\n[Unit]\nDescription=ag2-sparrow {SHAPE} (a Claude Code session as an AG2 Space agent)\n"
            "# A session that dies 5 times in 5 minutes (e.g. a bad login) is left failed.\n"
            "StartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
            f"[Service]\nExecStart={exec_start} {SHAPE} run\nRestart=always\nRestartSec=10\n"
            f"# {EXIT_HELD}: another session already serves this inbox; do not relaunch over it.\n"
            f"RestartPreventExitStatus={EXIT_HELD}\n\n[Install]\nWantedBy=default.target\n")


def render_plist(exec_start: str, path_env: str, log: str) -> str:
    body = {"Label": AGENT_LABEL, "ProgramArguments": [exec_start, SHAPE, "run"], "RunAtLoad": True,
            "KeepAlive": True, "ThrottleInterval": 10, "EnvironmentVariables": {"PATH": path_env},
            "StandardOutPath": log, "StandardErrorPath": log}
    text = plistlib.dumps(body, sort_keys=True).decode("utf-8")
    head, rest = text.split("\n", 1)
    return f"{head}\n{PLIST_MARKER}\n{rest}"


def _run(cmd: list) -> "subprocess.CompletedProcess | None":
    """A supervisor command; None when it could not run. Never prompts."""
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(r) -> bool:
    return r is not None and r.returncode == 0


def _why(r) -> str:
    if r is None:
        return "could not run"
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def _launchd_target() -> str:
    return f"gui/{os.getuid()}"


def install_supervisor(force: bool) -> None:
    """Write and start the supervisor; every problem is a NOTE naming what to do by hand."""
    def note(text: str) -> None:
        print(f"NOTE supervisor: {text}")
    exec_start = console_script()
    if not exec_start:
        note(f"{COMMAND} was not found beside {sys.executable} or on PATH; no supervisor was set up. "
             f"Reinstall ag2-sparrow for this Python and run install again")
        return
    path = supervisor_path()
    if is_macos():
        text, marker = render_plist(exec_start, os.environ.get("PATH", "/usr/bin:/bin"), str(log_path())), PLIST_MARKER
    else:
        text, marker = render_service(exec_start), MARKER
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            print(f"unchanged: {path}")
        elif not marked(old, marker) and not force:
            note(f"{path} was not written by install, so it was kept; re-run with --force to replace it")
            return
    if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != text:
        if is_macos() and path.exists():
            _run(["launchctl", "bootout", _launchd_target(), str(path)])
        _write(path, text, 0o644)
        print(f"installed: {path}")
    if is_macos():
        log_path().parent.mkdir(parents=True, exist_ok=True)
        r = _run(["launchctl", "bootstrap", _launchd_target(), str(path)])
        if _ok(r):
            print(f"loaded: {AGENT_LABEL} (it stops at logout and starts again at your next login)")
        else:
            note(f"launchctl bootstrap failed ({_why(r)}); run: launchctl bootstrap {_launchd_target()} "
                 f"{shlex.quote(str(path))}")
        return
    if not shutil.which("systemctl") or not _ok(_run(["systemctl", "--user", "--no-ask-password", "show-environment"])):
        note(f"systemctl --user is not available here; run {exec_start} {SHAPE} run under a supervisor of "
             "your own")
        return
    r = _run(["systemctl", "--user", "--no-ask-password", "daemon-reload"])
    if not _ok(r):
        note(f"daemon-reload failed ({_why(r)}); run it, then systemctl --user enable --now {SERVICE_NAME}")
        return
    r = _run(["systemctl", "--user", "--no-ask-password", "enable", "--now", SERVICE_NAME])
    if not _ok(r):
        note(f"enable failed ({_why(r)}); run systemctl --user enable --now {SERVICE_NAME}")
        return
    print(f"enabled: {SERVICE_NAME} (running)")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    r = _run(["loginctl", "--no-ask-password", "enable-linger", user]) if user else None
    print(f"linger: enabled for {user}, so the session runs while you are logged out" if _ok(r) else
          f"NOTE supervisor: loginctl enable-linger {user or '$USER'} failed ({_why(r)}); the session stops "
          "at logout until an administrator runs it")


def stop_supervisor() -> bool:
    path = supervisor_path()
    if is_macos():
        r = _run(["launchctl", "bootout", _launchd_target(), str(path)])
    else:
        r = _run(["systemctl", "--user", "--no-ask-password", "disable", "--now", SERVICE_NAME])
    return _ok(r)


# ── commands ─────────────────────────────────────────────────────────────────

def install(args) -> int:
    claude = shutil.which(args.claude) or args.claude
    problem = version_problem(claude)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    plugin = Path(args.plugin_dir).expanduser().resolve()
    if not (plugin / ".claude-plugin" / "plugin.json").is_file():
        print(f"error: {plugin} is not a Claude Code plugin (no .claude-plugin/plugin.json)", file=sys.stderr)
        return EXIT_UNUSABLE
    inbox = Path(args.inbox or _dirs.task_dir()).expanduser().resolve()
    results = Path(args.results or _dirs.result_dir()).expanduser().resolve()
    for d in (inbox, results):
        d.mkdir(parents=True, exist_ok=True)
    values = {"CLAUDE": os.path.abspath(claude), "PLUGIN_DIR": str(plugin), "INBOX_DIR": str(inbox),
              "RESULTS_DIR": str(results),
              "CLAUDE_CONFIG_DIR": str(Path(args.claude_config_dir).expanduser().resolve()) if args.claude_config_dir else "",
              "HMAC_KEY_FILE": str(Path(args.hmac_key_file).expanduser().resolve()) if args.hmac_key_file else "",
              "HMAC_STRICT": "1" if args.hmac_strict else "", "PERMISSION_MODE": args.permission_mode}
    _write(config_path(), render_config(values), 0o600)
    print(f"installed: {config_path()}")
    if args.no_service:
        print(f"skipped: the supervisor (--no-service); run by hand: {COMMAND} {SHAPE} run")
    else:
        install_supervisor(args.force)
    return 0


def child_env(cfg: dict, environ=None) -> dict:
    """What the session sees: the login's config dir, the inbox, the results, the key; nothing else."""
    base = os.environ if environ is None else environ
    env = {k: base[k] for k in ENV_KEEP if base.get(k)}
    env.update({"AG2_INBOX_DIR": cfg["INBOX_DIR"], "AG2_RESULTS_DIR": cfg["RESULTS_DIR"], AUTOARM_VAR: "1"})
    for key, var in (("CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"), ("HMAC_KEY_FILE", "AG2_HMAC_KEY_FILE"),
                     ("HMAC_STRICT", "AG2_HMAC_STRICT")):
        if cfg.get(key):
            env[var] = cfg[key]
    return env


def session_argv(cfg: dict) -> list:
    return [cfg["CLAUDE"], "--plugin-dir", cfg["PLUGIN_DIR"], "--permission-mode",
            cfg.get("PERMISSION_MODE") or "bypassPermissions", *STREAM_ARGV]


def run(args, popen=subprocess.Popen) -> int:
    """What the supervisor runs. The lock is taken before the child starts and released after it ends."""
    try:
        cfg = read_config(config_path())
    except OSError as e:
        print(f"error: {config_path()} unreadable ({e}); run {COMMAND} {SHAPE} install", file=sys.stderr)
        return EXIT_UNUSABLE
    problem = version_problem(cfg["CLAUDE"])
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        owner = acquire_lock(cfg["INBOX_DIR"], SHAPE)
    except LockRefused as e:
        print(e.line, file=sys.stderr)
        return EXIT_HELD if e.code == 3 else EXIT_UNUSABLE
    try:
        # Nothing is written to stdin: the plugin arms itself from the env. The pipe stays open,
        # since a stream-json session ends when its input ends.
        child = popen(session_argv(cfg), stdin=subprocess.PIPE, env=child_env(cfg), cwd=cfg["INBOX_DIR"], text=True)
        rc = child.wait()
        print(f"{SHAPE}: claude exited {rc}", file=sys.stderr)
        return rc
    finally:
        release_lock(owner)


def _check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def auth_problem(cfg: dict, run_=subprocess.run) -> str:
    """'' when `claude auth status` says the config dir's login is in place, else why not."""
    try:
        r = run_([cfg["CLAUDE"], "auth", "status"], capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                 env=child_env(cfg))
    except (OSError, subprocess.SubprocessError) as e:
        return f"claude auth status did not run ({type(e).__name__})"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or re.search(r'"?loggedIn"?\s*:\s*false', out):
        return (f"not logged in under CLAUDE_CONFIG_DIR={cfg.get('CLAUDE_CONFIG_DIR') or '(default)'}; run "
                f"claude login there (on macOS the login lives in the Keychain under USER)")
    return ""


def verify(_args) -> int:
    results: list = []
    try:
        cfg = read_config(config_path())
        _check(results, all(cfg.get(k) for k in ("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR")), "config",
               str(config_path()))
    except OSError:
        _check(results, False, "config", f"{config_path()} missing; run {COMMAND} {SHAPE} install")
        return 1
    problem = version_problem(cfg["CLAUDE"])
    _check(results, not problem, "claude", problem or f"{cfg['CLAUDE']} {_v(cli_version(cfg['CLAUDE']))}")
    if not problem:
        problem = auth_problem(cfg)
        _check(results, not problem, "login", problem or "claude auth status: logged in")
    plugin = Path(cfg["PLUGIN_DIR"])
    _check(results, (plugin / ".claude-plugin" / "plugin.json").is_file(), "plugin", str(plugin))
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        _check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    key = cfg.get("HMAC_KEY_FILE")
    _check(results, not key or Path(key).is_file(), "hmac key", key or "none (every task reads unsigned)")
    relay = _dirs.device_env_path()
    try:
        mode = stat.S_IMODE(relay.stat().st_mode)
        _check(results, not mode & 0o077, "relay token", f"{relay} mode {oct(mode)[2:]} (Sparrow's; never copied here)")
    except OSError:
        _check(results, False, "relay token", f"{relay} missing: Sparrow has no token to poll with")
    sup, marker = supervisor_path(), PLIST_MARKER if is_macos() else MARKER
    try:
        _check(results, marked(sup.read_text(encoding="utf-8", errors="replace"), marker), "supervisor", str(sup))
    except OSError:
        _check(results, False, "supervisor", f"{sup} missing; run {COMMAND} {SHAPE} install")
    holder = holder_lock.read_holder(cfg["INBOX_DIR"]) if Path(cfg["INBOX_DIR"]).is_dir() else None
    rec = getattr(holder, "record", None) or {}
    print(f"INFO session: {'held by pid %s' % rec.get('pid') if rec.get('pid') else 'no session holds the inbox now'}")
    return 0 if all(results) else 1


def uninstall(_args) -> int:
    """Stop and remove the supervisor and the config install wrote; nothing else."""
    rc = 0
    sup, marker = supervisor_path(), PLIST_MARKER if is_macos() else MARKER
    if sup.is_file():
        if not marked(sup.read_text(encoding="utf-8", errors="replace"), marker):
            print(f"error: {sup} was not written by install; remove it yourself", file=sys.stderr)
            rc = 1
        else:
            print(f"stopped: {sup.name}" if stop_supervisor() else f"NOTE supervisor: stop did not run for {sup.name}")
            sup.unlink()
            print(f"removed: {sup}")
    else:
        print(f"nothing to remove: {sup} is not installed")
    cfg = config_path()
    if cfg.is_file():
        if marked(cfg.read_text(encoding="utf-8", errors="replace"), MARKER):
            cfg.unlink()
            print(f"removed: {cfg}")
        else:
            print(f"error: {cfg} was not written by install; remove it yourself", file=sys.stderr)
            rc = 1
    print(f"kept: {_dirs.device_env_path()} (Sparrow's token) and the Claude login")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="Connect a local agent to AG2 Space.")
    shapes = ap.add_subparsers(dest="shape", required=True)
    local = shapes.add_parser(SHAPE, help="a Claude Code session on this machine (Mods API, CLI %s+)" % _v(MIN_CLI))
    sub = local.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the paths, write the supervisor and start the session")
    p.add_argument("--plugin-dir", required=True, help="the ag2space-inbox plugin directory from the repo")
    p.add_argument("--inbox", help="Sparrow's task dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--results", help="Sparrow's result dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--claude-config-dir", help="CLAUDE_CONFIG_DIR holding the login the session uses (default: "
                   "the CLI's own)")
    p.add_argument("--hmac-key-file", help="the task-envelope key Sutando stamps with (AG2_HMAC_KEY_FILE)")
    p.add_argument("--hmac-strict", action="store_true", help="submit verified envelopes only")
    p.add_argument("--permission-mode", default="bypassPermissions",
                   help="the session's --permission-mode; bypassPermissions (default) suits an unattended "
                        "session, a narrower mode makes a prompting tool stall the queue")
    p.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    p.add_argument("--force", action="store_true", help="replace a supervisor file install did not write")
    p.add_argument("--no-service", action="store_true", help="write the config only; run the session by hand")
    p.set_defaults(func=install)
    sub.add_parser("run", help="what the supervisor runs: one session, armed on every start").set_defaults(func=run)
    sub.add_parser("verify", help="check every part").set_defaults(func=verify)
    sub.add_parser("uninstall", help="stop and remove what install wrote").set_defaults(func=uninstall)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
