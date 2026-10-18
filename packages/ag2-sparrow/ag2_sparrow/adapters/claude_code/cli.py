#!/usr/bin/env python3
"""`ag2-sparrow-claude-local`: host a local Claude Code session as an AG2 Space agent.

One console script per adapter, as the Hatch adapter has, since the adapter-boundary rule forbids a
shared dispatcher that imports adapters; the lifecycle itself (install, run, verify, uninstall) is
`connect_common`'s, parametrised by this adapter's Shape.

`install` records the session's paths and the canonical work root in a 0600 config, writes a service
manager unit (a systemd user unit on Linux, a launchd agent on macOS) whose first line is a marker, has
the manager start it, and confirms a fresh launcher holds the root. `run` is what the manager runs: it
checks the CLI version, takes the one-session-per-root lock for the whole child, starts `claude -p` in
stream-json mode with the AG2 Space inbox plugin, which arms its inbox watch itself from the launcher's
env on every start, and holds stdin open so the session lives. The relay token stays in Sparrow's own
env file, the Claude login stays in its config dir: neither is copied. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ... import _dirs, connect_common as cm
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, Shape, _v  # noqa: F401

LOCAL = Shape(name="claude-local", command="ag2-sparrow-claude-local",
              description="a Claude Code session as an AG2 Space agent", cli="Claude Code CLI",
              min_version=(2, 1, 287), version_anchor=r"\(Claude Code\)",
              version_note="the Mods API it uses arrived there",
              required_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR"),
              config_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "CLAUDE_CONFIG_DIR", "HMAC_KEY_FILE",
                           "HMAC_STRICT", "PERMISSION_MODE"),
              env_keep=("PATH", "HOME", "USER", "ANTHROPIC_BASE_URL"), kept="the Claude login")
SHAPE, COMMAND, MIN_CLI = LOCAL.name, LOCAL.command, LOCAL.min_version
MARKER, PLIST_MARKER, SERVICE_NAME, AGENT_LABEL = LOCAL.marker, LOCAL.plist_marker, LOCAL.service_name, LOCAL.agent_label
# The marker the plugin reads at session.start: with it, the watch arms itself, no model turn.
AUTOARM_VAR = "AG2_AUTOARM"
STREAM_ARGV = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
ENV_KEEP = LOCAL.env_keep


def config_path() -> Path:
    return cm.config_path(LOCAL)


def service_path() -> Path:
    return cm.service_path(LOCAL)


def plist_path() -> Path:
    return cm.plist_path(LOCAL)


def supervisor_path() -> Path:
    return cm.supervisor_path(LOCAL)


def parse_version(text: str) -> "tuple[int, int, int] | None":
    return cm.parse_version(text, LOCAL.version_anchor)


def cli_version(claude: str, run=subprocess.run) -> "tuple[int, int, int] | None":
    return cm.cli_version(claude, LOCAL.version_anchor, run)


def version_problem(claude: str, run=subprocess.run) -> str:
    return cm.version_problem(claude, LOCAL, run)


def render_config(values: dict) -> str:
    return cm.render_config(LOCAL, values)


def read_config(path: Path) -> dict:
    return cm.read_config(path)


def load_config() -> "tuple[dict, str]":
    return cm.load_config(LOCAL)


def key_problem(path: str) -> str:
    """'' when `path` holds the key the plugin's verifier accepts (64 hex characters), else why not."""
    if not path:
        return "no key file: strict mode would submit nothing"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        return f"{path} unreadable ({e.strerror or e}): strict mode would submit nothing"
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return f"{path} is not a 64-hex-character key, so every envelope reads unverifiable"
    return ""


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
    key_file = str(Path(args.hmac_key_file).expanduser().resolve()) if args.hmac_key_file else ""
    if args.hmac_strict:
        problem = key_problem(key_file)
        if problem:
            print(f"error: --hmac-strict needs a usable key: {problem}", file=sys.stderr)
            return EXIT_UNUSABLE
    root, problem = cm.canonical_root(args.work, args.inbox or _dirs.task_dir())
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.work:
        inbox, results = Path(root) / "tasks", Path(root) / "results"
    else:
        inbox = Path(args.inbox or _dirs.task_dir()).expanduser()
        results = Path(args.results or _dirs.result_dir()).expanduser()
    for d in (inbox, results):
        d.mkdir(parents=True, exist_ok=True)
    values = {"CLAUDE": os.path.abspath(claude), "PLUGIN_DIR": str(plugin), "INBOX_DIR": str(inbox),
              "RESULTS_DIR": str(results),
              "CLAUDE_CONFIG_DIR": str(Path(args.claude_config_dir).expanduser().resolve()) if args.claude_config_dir else "",
              "HMAC_KEY_FILE": key_file, "HMAC_STRICT": "1" if args.hmac_strict else "",
              "PERMISSION_MODE": args.permission_mode}
    return cm.install(LOCAL, values, root)


def child_env(cfg: dict, environ=None) -> dict:
    """What the session sees: the login's config dir, the inbox, the results, the key; nothing else."""
    env = cm.child_env_from(ENV_KEEP, environ)
    env.update({"AG2_INBOX_DIR": cfg["INBOX_DIR"], "AG2_RESULTS_DIR": cfg["RESULTS_DIR"], AUTOARM_VAR: "1"})
    for key, var in (("CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"), ("HMAC_KEY_FILE", "AG2_HMAC_KEY_FILE"),
                     ("HMAC_STRICT", "AG2_HMAC_STRICT")):
        if cfg.get(key):
            env[var] = cfg[key]
    return env


def session_argv(cfg: dict) -> list:
    return [cfg["CLAUDE"], "--plugin-dir", cfg["PLUGIN_DIR"], "--permission-mode",
            cfg.get("PERMISSION_MODE") or "bypassPermissions", *STREAM_ARGV]


def run(args, popen=subprocess.Popen, op_lock=None) -> int:
    """What the manager runs: the canonical root's lock is taken and recorded before the child starts,
    the child holds its lease so the flock outlives a dead launcher, and every launcher exit reaps the
    child before the lock goes (cm.serve)."""
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    problem = version_problem(cfg["CLAUDE"])
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    return cm.serve(LOCAL, cm.work_root(cfg), session_argv(cfg), child_env(cfg), popen, op_lock=op_lock)


def debug_run(args, popen=subprocess.Popen) -> int:
    """A foreground session for a look: under the operation lock from the registered/absent check
    through the root lock's acquisition, and only with no registered job and the root free."""
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        with cm.operation_lock(LOCAL) as op:
            problem = cm.debug_run_problem(LOCAL, cm.work_root(cfg))
            if problem:
                print(f"error: {problem}", file=sys.stderr)
                return EXIT_UNUSABLE
            return run(args, popen, op_lock=op)
    except cm.Busy as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_UNUSABLE


def auth_problem(cfg: dict, run_=subprocess.run) -> str:
    """'' when `claude auth status` says the config dir's login is in place, else why not."""
    try:
        r = run_([cfg["CLAUDE"], "auth", "status"], capture_output=True, text=True, timeout=cm.RUN_TIMEOUT_S,
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
    cfg, problem = load_config()
    cm.check(results, not problem, "config", problem or str(config_path()))
    if problem:
        return 1
    problem = version_problem(cfg["CLAUDE"])
    cm.check(results, not problem, "claude", problem or f"{cfg['CLAUDE']} {_v(cli_version(cfg['CLAUDE']))}")
    if not problem:
        problem = auth_problem(cfg)
        cm.check(results, not problem, "login", problem or "claude auth status: logged in")
    plugin = Path(cfg["PLUGIN_DIR"])
    cm.check(results, (plugin / ".claude-plugin" / "plugin.json").is_file(), "plugin", str(plugin))
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    key, strict = cfg.get("HMAC_KEY_FILE"), cfg.get("HMAC_STRICT") == "1"
    problem = key_problem(key) if key or strict else ""
    cm.check(results, not problem, "hmac key",
             (f"strict mode: {problem}" if strict else problem) if problem else key or "none (every task reads unsigned)")
    cm.verify_tail(LOCAL, cfg, results)
    return 0 if all(results) else 1


def uninstall(_args) -> int:
    return cm.uninstall(LOCAL)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="A Claude Code session on this machine as an AG2 Space "
                                 "agent (Mods API, CLI %s+)." % _v(MIN_CLI))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the paths, write the unit, have the manager start the session "
                       "(a reinstall stops the old one first)")
    p.add_argument("--plugin-dir", required=True, help="the ag2space-inbox plugin directory from the repo")
    p.add_argument("--work", help="the work root: its tasks/ is the inbox, its results/ the result dir")
    p.add_argument("--inbox", help="Sparrow's task dir, <work>/tasks (default: as ag2-sparrow resolves it)")
    p.add_argument("--results", help="Sparrow's result dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--claude-config-dir", help="CLAUDE_CONFIG_DIR holding the login the session uses (default: "
                   "the CLI's own)")
    p.add_argument("--hmac-key-file", help="the task-envelope key Sutando stamps with (AG2_HMAC_KEY_FILE)")
    p.add_argument("--hmac-strict", action="store_true", help="submit verified envelopes only")
    p.add_argument("--permission-mode", default="bypassPermissions",
                   help="the session's --permission-mode; bypassPermissions (default) suits an unattended "
                        "session, a narrower mode makes a prompting tool stall the queue")
    p.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    p.set_defaults(func=install)
    sub.add_parser("run", help="what the manager runs: one session, armed on every start").set_defaults(func=run)
    sub.add_parser("debug-run", help="one session in the foreground; refused while the job is registered or the "
                   "work root is held").set_defaults(func=debug_run)
    sub.add_parser("verify", help="check every part").set_defaults(func=verify)
    sub.add_parser("uninstall", help="stop the job and remove what install wrote").set_defaults(func=uninstall)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
