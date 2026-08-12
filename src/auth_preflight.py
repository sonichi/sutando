#!/usr/bin/env python3
"""auth_preflight.py — probe whether a CLAUDE_CONFIG_DIR can boot the claude CLI authenticated (OK vs LOGIN_REQUIRED + exact remedy), before a restart terminates the session that could still fix it.

Why (sonichi#2396 / #2402, 2026-07-30 outage): a restart into a fresh
CLAUDE_CONFIG_DIR deterministically lands on the interactive /login wall —
stored auth does not carry over, and on macOS the OAuth token lives in the
Keychain so the `.credentials.json` file the auth-carry looks for never
exists. The only moment this is cheap to catch is BEFORE firing a
self-terminating restart. This module is the decide-only probe (no callers
wired here — step-1 pattern): #2402's pre-fire gate, #2400's preflight
report, and the easy-restart flow can all call it.

Static checks (default, fast, read-only):
  * `.claude.json` in the target dir carries a non-empty `oauthAccount`
  * credentials exist: `.credentials.json` on disk OR the macOS Keychain
    item (`Claude Code-credentials`) — existence only, value never read
  * SSH context (`$SSH_CONNECTION`) — a locked keychain cannot be unlocked
    from an SSH-spawned process, so completing /login needs a GUI Terminal

Static PASS is a heuristic (an expired token still passes); `--live` runs
`claude -p ok` against the target dir as ground truth (spawns the CLI —
costs a model call and needs the binary on PATH; opt-in for that reason).

CLI:
  python3 src/auth_preflight.py [--config-dir DIR] [--json] [--live]
Exit codes: 0 = ok, 2 = login required, 3 = probe error.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys

KEYCHAIN_SERVICE = "Claude Code-credentials"


def keychain_has_credentials() -> bool:  # pragma: no cover - external I/O (security CLI)
    """True when the macOS Keychain holds a Claude Code credentials item.

    Existence check only — `-s <service>` lookup, output discarded, the
    secret value is never requested. Non-macOS (no `security`) → False.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _oauth_account_present(config_dir: str) -> bool:
    """True when <config_dir>/.claude.json exists and carries a non-empty
    oauthAccount — the linkage the CLI uses to consider an install logged-in."""
    path = os.path.join(config_dir, ".claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return isinstance(data, dict) and bool(data.get("oauthAccount"))
    except (OSError, ValueError):
        return False


def check_auth_state(config_dir: str, *, keychain_check=keychain_has_credentials,
                     env=None) -> dict:
    """Pure decision over the target config dir's auth state.

    Returns {verdict: "ok"|"login_required", reasons: [...], remedy: str|None,
    ssh: bool, config_dir: str}. `keychain_check` and `env` are injectable so
    the decision is unit-testable without a Mac keychain or a live SSH session.
    """
    env = os.environ if env is None else env
    ssh = bool(env.get("SSH_CONNECTION"))
    reasons = []

    oauth_ok = _oauth_account_present(config_dir)
    if not oauth_ok:
        reasons.append("no oauthAccount in .claude.json (fresh or never-logged-in config dir)")

    creds_file = os.path.isfile(os.path.join(config_dir, ".credentials.json"))
    creds_keychain = bool(keychain_check())
    if not creds_file and not creds_keychain:
        reasons.append("no credentials: .credentials.json absent and no Keychain item")

    if oauth_ok and (creds_file or creds_keychain):
        return {"verdict": "ok", "reasons": [], "remedy": None, "ssh": ssh,
                "config_dir": config_dir}

    return {"verdict": "login_required", "reasons": reasons,
            "remedy": _login_remedy(ssh, config_dir), "ssh": ssh,
            "config_dir": config_dir}


def _login_remedy(ssh: bool, config_dir: str) -> str:
    """The exact owner-facing fix for a login-class state (supersedes the
    #2403 copy: that text said restart-then-login, which loops straight
    back into the boot gate this branch adds — restart execs startup.sh,
    startup.sh aborts on login_required, and no login-capable CLI ever
    appears). The remedy must reach /login WITHOUT routing through the
    gate: a bare CLI launch runs no services, so there is nothing to
    abort; restart comes only after login succeeds."""
    host = platform.node().split(".")[0] or "the host"
    # shlex.quote: the remedy is copy/paste shell syntax — an unquoted
    # config dir with spaces/metacharacters splits the assignment and
    # breaks the recovery path exactly when the operator needs it.
    remedy = (f"needs GUI /login on {host}: open Terminal there and run"
              f" `CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude` (bare CLI, no"
              " services — the boot gate does not run, so this cannot loop"
              " back here), complete /login, then run `bash src/restart.sh`"
              " to bring the core up.")
    if ssh:
        remedy = ("SSH session detected — a locked keychain cannot be unlocked"
                  " from here, so /login WILL stall if started over SSH. " + remedy)
    return remedy


def live_probe(config_dir: str, timeout: int = 90):  # pragma: no cover - spawns the real CLI
    """Ground-truth check: run `claude -p ok` under the target config dir.
    Returns (ok: bool, detail: str). Never raises."""
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    try:
        r = subprocess.run(["claude", "-p", "ok"], env=env,
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "claude binary not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"claude -p timed out after {timeout}s (interactive wall?)"
    if r.returncode == 0:
        return True, "claude -p ok succeeded"
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return False, f"claude -p exited {r.returncode}: {tail[-1] if tail else 'no output'}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probe a CLAUDE_CONFIG_DIR's CLI auth state.")
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR", ""),
                    help="target config dir (default: $CLAUDE_CONFIG_DIR)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--live", action="store_true",
                    help="also run `claude -p ok` under the target dir (ground truth)")
    a = ap.parse_args(argv)
    if not a.config_dir:
        print("auth-preflight: no --config-dir and $CLAUDE_CONFIG_DIR unset", file=sys.stderr)
        return 3

    result = check_auth_state(a.config_dir)
    if a.live:
        ok, detail = live_probe(a.config_dir)
        result["live"] = {"ok": ok, "detail": detail}
        if not ok and result["verdict"] == "ok":
            # Static PASS was a false positive (expired token class): downgrade
            # and build the remedy directly — recomputing the static check
            # cannot yield one when the on-disk state still looks fine.
            result["verdict"] = "login_required"
            result["reasons"].append(f"live probe failed: {detail}")
            result["remedy"] = _login_remedy(result["ssh"], a.config_dir)

    if a.json:
        print(json.dumps(result, indent=2))
    elif result["verdict"] == "ok":
        print(f"auth-preflight OK: {a.config_dir} can boot authenticated")
    else:
        print(f"auth-preflight LOGIN_REQUIRED for {a.config_dir}")
        for r in result["reasons"]:
            print(f"  - {r}")
        print(f"  remedy: {result['remedy']}")
    return 0 if result["verdict"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
