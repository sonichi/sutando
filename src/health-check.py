#!/usr/bin/env python3
"""
Sutando health check — verifies all components are running correctly.

Usage:
  python3 src/health-check.py            # full check, human-readable
  python3 src/health-check.py --json     # machine-readable output
  python3 src/health-check.py --fix      # attempt to fix issues

Checks:
  - Voice agent (port 9900), web client, agent API, dashboard
  - Critical files (CLAUDE.md, build_log.md, ACTIVITY.md)
  - Memory system (MEMORY.md index, key memory files)
  - Notes directory
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent

def _default_memory_dir() -> str:
    """Auto-detect Claude Code memory dir from repo path."""
    repo = Path(__file__).parent.parent.resolve()
    slug = str(repo).replace("/", "-")
    return str(Path.home() / ".claude" / "projects" / slug / "memory")

MEMORY_DIR = Path(os.environ.get("SUTANDO_MEMORY_DIR", _default_memory_dir()))

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_port(port: int, name: str) -> dict:
    """Check if a port is listening."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
            up = result == 0
        return {"name": name, "status": "ok" if up else "down", "detail": f"port {port}"}
    except Exception as e:
        return {"name": name, "status": "error", "detail": str(e)}


def check_launchd(label: str) -> dict:
    """Check if a launchd job is loaded and running."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if label in line:
                parts = line.split("\t")
                pid = parts[0].strip() if len(parts) > 0 else "-"
                exit_code = parts[1].strip() if len(parts) > 1 else "?"
                running = pid != "-" and pid != ""
                status = "ok" if running or exit_code == "0" else "stopped"
                return {"name": label, "status": status, "detail": f"pid={pid} exit={exit_code}"}
        return {"name": label, "status": "not_loaded", "detail": "not found in launchctl list"}
    except Exception as e:
        return {"name": label, "status": "error", "detail": str(e)}


def check_file(path: Path, name: str) -> dict:
    """Check if a file exists and is non-empty."""
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    size = path.stat().st_size
    if size == 0:
        return {"name": name, "status": "empty", "detail": str(path)}
    return {"name": name, "status": "ok", "detail": f"{size} bytes"}


def check_directory(path: Path, name: str) -> dict:
    """Check if a directory exists and has files."""
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    count = len(list(path.glob("*.md")))
    return {"name": name, "status": "ok", "detail": f"{count} .md files"}


# ---------------------------------------------------------------------------
# Fix attempts
# ---------------------------------------------------------------------------

def fix_launchd(label: str) -> str:
    """Try to reload a launchd job."""
    plist_map = {
        "com.sutando.voice-agent": Path.home() / "Library/LaunchAgents/com.sutando.voice-agent.plist",
        "com.sutando.web-client": Path.home() / "Library/LaunchAgents/com.sutando.web-client.plist",
    }
    plist = plist_map.get(label)
    if not plist or not plist.exists():
        return f"no plist found for {label}"

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    # Try kickstart
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return f"restarted {label}"
    # Try bootstrap
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return f"bootstrapped {label}"
    return f"failed to restart {label}: {result.stderr.strip()}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def mark_stale_if_outdated(check: dict, src_file: Path, pgrep_pattern: str, threshold_sec: int = 1800) -> None:
    """Mark `check` as 'stale' in place if a process matching `pgrep_pattern`
    started more than `threshold_sec` before `src_file`'s mtime.

    Extracted so the same logic covers all tsx-managed services
    (voice-agent, web-client, conversation-server) without duplication.
    30 min default threshold tolerates `git checkout` mtime bumps; real
    stale deploys are hours/days old. Silent on any failure — stale
    detection is advisory, not authoritative.
    """
    if not src_file.exists():
        return
    try:
        pids = subprocess.run(
            ["pgrep", "-f", pgrep_pattern],
            capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")
        pids = [p for p in pids if p]
        if not pids:
            return
        ps_out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", ",".join(pids)],
            capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")
        from datetime import datetime as _dt
        starts = []
        for line in ps_out:
            line = line.strip()
            if line:
                try:
                    starts.append(_dt.strptime(line, "%a %b %d %H:%M:%S %Y").timestamp())
                except ValueError:
                    pass
        if not starts:
            return
        # Pick the OLDEST start time — the tsx wrapper spawns a child node
        # process; we want the parent's launch time, not the child's.
        proc_start = min(starts)
        src_mtime = src_file.stat().st_mtime
        if src_mtime - proc_start > threshold_sec:
            check["status"] = "stale"
            check["detail"] = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_all_checks() -> list[dict]:
    checks = []

    # Core services (required)
    voice_check = check_port(9900, "voice-agent")
    if voice_check["status"] == "ok":
        mark_stale_if_outdated(voice_check, REPO_DIR / "src" / "voice-agent.ts", "voice-agent.ts")
    checks.append(voice_check)

    web_check = check_port(8080, "web-client")
    if web_check["status"] == "ok":
        mark_stale_if_outdated(web_check, REPO_DIR / "src" / "web-client.ts", "web-client.ts")
    checks.append(web_check)

    # Optional services (downgrade missing to warning, not failure)
    for port, name in [(7843, "agent-api"), (7844, "dashboard"), (7845, "screen-capture")]:
        c = check_port(port, name)
        if c["status"] != "ok":
            c["status"] = "warn"
            c["detail"] = "not running (optional)"
        checks.append(c)

    # Critical files
    for name, path in [
        ("CLAUDE.md", REPO_DIR / "CLAUDE.md"),
        ("build_log.md", REPO_DIR / "build_log.md"),
        (".env", REPO_DIR / ".env"),
    ]:
        checks.append(check_file(path, name))

    # Memory system (check if dir exists — specific files are optional)
    if MEMORY_DIR.exists():
        checks.append(check_directory(MEMORY_DIR, "memory-dir"))
    else:
        checks.append({"name": "memory-dir", "status": "ok", "detail": "not yet created (normal for new installs)"})

    # Notes
    checks.append(check_directory(REPO_DIR / "notes", "notes-dir"))

    # Phone conversation server (optional — only check if Twilio configured and not skipped)
    env_path = REPO_DIR / ".env"
    if env_path.exists():
        env_content = env_path.read_text()
        has_twilio = "TWILIO_ACCOUNT_SID=" in env_content and not env_content.split("TWILIO_ACCOUNT_SID=")[1].startswith("\n")
        skip_phone = "SKIP_PHONE=1" in env_content or os.environ.get("SKIP_PHONE") == "1"
        if has_twilio and not skip_phone:
            c = check_port(3100, "conversation-server")
            if c["status"] != "ok":
                c["status"] = "warn"
                c["detail"] = "not running (starts on demand)"
            else:
                mark_stale_if_outdated(
                    c,
                    REPO_DIR / "skills" / "phone-conversation" / "scripts" / "conversation-server.ts",
                    "conversation-server.ts",
                )
            checks.append(c)
            # Tunnel check — depends on TWILIO_WEBHOOK_URL host (Funnel) or ngrok
            if c["status"] == "ok":
                webhook_url = ""
                for line in env_content.splitlines():
                    if line.startswith("TWILIO_WEBHOOK_URL="):
                        webhook_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                is_funnel = "ts.net" in webhook_url
                if is_funnel:
                    # Tailscale Funnel — verify funnel is serving and reachable
                    funnel_c = {"name": "tailscale-funnel", "status": "ok", "detail": f"serving {webhook_url}"}
                    try:
                        import urllib.request
                        req = urllib.request.Request(f"{webhook_url}/health", headers={"User-Agent": "sutando-healthcheck"})
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            if resp.status != 200:
                                funnel_c["status"] = "down"
                                funnel_c["detail"] = f"webhook returned {resp.status}"
                    except Exception as e:
                        funnel_c["status"] = "down"
                        funnel_c["detail"] = f"unreachable: {str(e)[:60]}"
                    checks.append(funnel_c)
                else:
                    ngrok_c = check_port(4040, "ngrok")
                    if ngrok_c["status"] == "ok":
                        ngrok_c["detail"] = "tunnel active (port 4040)"
                    else:
                        # Critical: phone calls fail without ngrok
                        ngrok_c["status"] = "down"
                        ngrok_c["detail"] = "not running — phone calls won't reach server"
                    checks.append(ngrok_c)

    # Messaging bridges (optional — only check if configured and not skipped)
    skip_telegram = (env_path.exists() and "SKIP_TELEGRAM=1" in env_path.read_text()) or os.environ.get("SKIP_TELEGRAM") == "1"
    channels_dir = Path.home() / ".claude" / "channels"
    for name, proc_name in [("telegram-bridge", "telegram-bridge"), ("discord-bridge", "discord-bridge")]:
        channel_name = name.replace("-bridge", "")
        if channel_name == "telegram" and skip_telegram:
            continue
        env_file = channels_dir / channel_name / ".env"
        access_file = channels_dir / channel_name / "access.json"
        # Check if configured via either .env or access.json
        if not env_file.exists() and not access_file.exists():
            continue
        try:
            # Anchor on the .py suffix so we don't match unrelated processes
            # whose command line happens to contain "discord-bridge" (shell
            # invocations, ps/grep pipelines, etc). Otherwise pgrep -f bare
            # name produces false-positive "multiple processes" warnings
            # that scared us into thinking the bridges were zombied today.
            result = subprocess.run(["pgrep", "-f", f"{proc_name}\\.py$"], capture_output=True, text=True)
            pids = result.stdout.strip().split("\n") if result.returncode == 0 else []
            pids = [p for p in pids if p]
        except:
            pids = []

        if not pids:
            checks.append({"name": name, "status": "warn", "detail": "configured but not running"})
            continue

        # Check 1: Multiple processes (zombie/duplicate)
        if len(pids) > 1:
            checks.append({"name": name, "status": "warn", "detail": f"multiple processes ({len(pids)} PIDs: {','.join(pids)})"})
            continue

        # Check 2: Log file freshness
        import time
        log_file = REPO_DIR / "src" / f"{name}.log"
        detail = "running"
        status = "ok"
        if log_file.exists():
            age_sec = time.time() - log_file.stat().st_mtime
            if age_sec > 300:  # 5 minutes
                status = "warn"
                detail = f"running but log stale ({int(age_sec)}s old)"

        # Check 3: Heartbeat file freshness (overrides log staleness if fresh)
        heartbeat_file = REPO_DIR / "src" / f"{name}.heartbeat"
        if heartbeat_file.exists():
            hb_age = time.time() - heartbeat_file.stat().st_mtime
            if hb_age <= 120:  # heartbeat is fresh — bridge is alive
                status = "ok"
                detail = "running"
            else:
                status = "warn"
                detail = f"running but heartbeat stale ({int(hb_age)}s old)"

        # Check 4: Stale code — process started before the source file's last
        # modification. This catches the case where a fix is on disk but the
        # running process is from a previous version (e.g., PR #203 silently
        # not in effect because nobody restarted the bridge after merge).
        try:
            src_file = REPO_DIR / "src" / f"{name}.py"
            if src_file.exists() and pids:
                src_mtime = src_file.stat().st_mtime
                # Use ps to get process start time as Unix epoch
                ps_out = subprocess.run(
                    ["ps", "-o", "lstart=", "-p", pids[0]],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                if ps_out:
                    from datetime import datetime as _dt
                    proc_start = _dt.strptime(ps_out, "%a %b %d %H:%M:%S %Y").timestamp()
                    # Threshold tuned to avoid false positives from `git checkout`
                    # which bumps the mtime of every file that differs between
                    # branches, even when content is identical. Real stale deploys
                    # (the original target of #228) are usually hours/days old,
                    # so 30 min comfortably catches them while tolerating routine
                    # branch switching.
                    if src_mtime - proc_start > 1800:  # source >30 min newer
                        status = "stale"
                        detail = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass

        checks.append({"name": name, "status": status, "detail": detail})

    # Sutando menu bar app (optional — only check if binary exists)
    sutando_bin = REPO_DIR / "src" / "Sutando" / "Sutando"
    if sutando_bin.exists():
        try:
            result = subprocess.run(["pgrep", "-f", "Sutando/Sutando"], capture_output=True, text=True)
            pids = [p for p in result.stdout.strip().split("\n") if p]
        except:
            pids = []
        if pids:
            checks.append({"name": "sutando-app", "status": "ok", "detail": f"running (⌃C/⌃V/⌃M)"})
        else:
            checks.append({"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"})

    return checks


def main():
    as_json = "--json" in sys.argv
    do_fix = "--fix" in sys.argv
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    checks = run_all_checks()
    issues = [c for c in checks if c["status"] not in ("ok", "warn")]

    if as_json:
        print(json.dumps({"checks": checks, "issues": len(issues), "total": len(checks)}, indent=2))
        return

    # --quiet: print only issues (or nothing if clean). Exit code reflects state.
    # Useful for cron callers and automation that only cares about problems.
    if quiet:
        if issues:
            for c in issues:
                icon = "♻" if c["status"] == "stale" else "✗"
                print(f"{icon} {c['name']}: {c['status']} ({c['detail']})")
            if do_fix:
                # Fall through to existing fix path below
                pass
            else:
                sys.exit(1)
        else:
            sys.exit(0)

    # Human-readable
    if not quiet:
        print("Sutando Health Check")
        print("=" * 40)

        for c in checks:
            icon = "✓" if c["status"] == "ok" else "⚠" if c["status"] == "warn" else "✗" if c["status"] in ("down", "missing", "not_loaded") else "♻" if c["status"] == "stale" else "~"
            print(f"  {icon} {c['name']:30s} {c['status']:12s} {c['detail']}")

        print()
    if not issues:
        if not quiet:
            print("All systems operational.")
    else:
        print(f"{len(issues)} issue(s) found:")
        for c in issues:
            print(f"  - {c['name']}: {c['status']} ({c['detail']})")

        if do_fix:
            print()
            print("Attempting fixes...")
            for c in issues:
                if c["name"].startswith("com.sutando."):
                    result = fix_launchd(c["name"])
                    print(f"  {c['name']}: {result}")
                elif c["name"] in ("telegram-bridge", "discord-bridge"):
                    # If stale (process older than source code), kill old PID first
                    # so the new process doesn't conflict with a still-running zombie.
                    if c["status"] == "stale":
                        try:
                            # Anchor to `\.py$` to match the detect path at
                            # line ~277. Without this, a bare `pgrep -f
                            # discord-bridge` also catches grep pipelines
                            # and shell invocations whose command line
                            # contains the bridge name, and we'd kill them
                            # instead of (or in addition to) the real
                            # bridge process. PR #243 fixed the detect
                            # side; this keeps the kill side consistent.
                            old_pids = subprocess.run(
                                ["pgrep", "-f", f"{c['name']}\\.py$"], capture_output=True, text=True
                            ).stdout.strip().split("\n")
                            for pid in old_pids:
                                if pid:
                                    subprocess.run(["kill", pid], check=False)
                            import time as _t; _t.sleep(1)
                        except Exception:
                            pass
                    subprocess.Popen(["python3", str(REPO_DIR / "src" / f"{c['name']}.py")],
                                     stdout=open(str(REPO_DIR / "src" / f"{c['name']}.log"), "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: {'restarted (stale code)' if c['status'] == 'stale' else 'restarted'}")
                elif c["name"] == "sutando-app":
                    sutando_bin = REPO_DIR / "src" / "Sutando" / "Sutando"
                    subprocess.Popen([str(sutando_bin)], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "ngrok":
                    # Read ngrok domain from .env if set, otherwise use default
                    env_path = REPO_DIR / ".env"
                    domain_arg = []
                    if env_path.exists():
                        for line in env_path.read_text().splitlines():
                            if line.startswith("NGROK_DOMAIN="):
                                domain = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if domain:
                                    domain_arg = [f"--domain={domain}"]
                                break
                    subprocess.Popen(["ngrok", "http", "3100"] + domain_arg,
                                     stdout=open("/tmp/ngrok.log", "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "tailscale-funnel":
                    # Re-enable Tailscale Funnel for port 3100
                    ts_bin = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
                    subprocess.run([ts_bin, "funnel", "--bg", "3100"],
                                   capture_output=True, timeout=10)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "conversation-server":
                    # If stale, kill old PIDs first so the new process doesn't
                    # bind-fail or end up alongside a still-running zombie.
                    if c["status"] == "stale":
                        try:
                            old_pids = subprocess.run(
                                ["pgrep", "-f", "conversation-server.ts"],
                                capture_output=True, text=True
                            ).stdout.strip().split("\n")
                            for pid in old_pids:
                                if pid:
                                    subprocess.run(["kill", pid], check=False)
                            import time as _t; _t.sleep(1)
                        except Exception:
                            pass
                    subprocess.Popen(["npx", "tsx", "skills/phone-conversation/scripts/conversation-server.ts"],
                                     cwd=str(REPO_DIR),
                                     stdout=open("/tmp/conversation-server.log", "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: {'restarted (stale code)' if c['status'] == 'stale' else 'restarted'}")

    # Email alert if critical issues found and --fix didn't resolve them
    if issues and do_fix:
        # Re-check after fix attempts
        import time
        time.sleep(2)
        rechecks = run_all_checks()
        remaining = [c for c in rechecks if c["status"] not in ("ok",)]
        if remaining:
            alert_lines = ["Sutando health check found issues that auto-fix couldn't resolve:", ""]
            for c in remaining:
                alert_lines.append(f"  - {c['name']}: {c['status']} ({c['detail']})")
            alert_lines.append("")
            alert_lines.append("Check manually or run: python3 src/health-check.py")
            alert_body = "\\n".join(alert_lines)
            try:
                subject = "Sutando: health check alert"
                script = (
                    'tell application "Mail"\n'
                    f'    set m to make new outgoing message with properties {{subject:"{subject}", content:"{alert_body}", visible:false}}\n'
                    '    tell m\n'
                    f'        make new to recipient at end of to recipients with properties {{address:"{os.environ.get("NOTIFICATION_EMAIL", "")}"}}\n'
                    '    end tell\n'
                    '    send m\n'
                    'end tell'
                )
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
                print("Alert email sent.")
            except Exception:
                pass

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
