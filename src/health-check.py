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

def run_all_checks() -> list[dict]:
    checks = []

    # Core services (required)
    checks.append(check_port(9900, "voice-agent"))
    checks.append(check_port(8080, "web-client"))

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
        ("ACTIVITY.md", REPO_DIR / "ACTIVITY.md"),
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

    # Phone conversation server (optional — only check if Twilio configured)
    env_path = REPO_DIR / ".env"
    if env_path.exists():
        env_content = env_path.read_text()
        has_twilio = "TWILIO_ACCOUNT_SID=" in env_content and not env_content.split("TWILIO_ACCOUNT_SID=")[1].startswith("\n")
        if has_twilio:
            c = check_port(3100, "conversation-server")
            if c["status"] != "ok":
                c["status"] = "warn"
                c["detail"] = "not running (starts on demand)"
            checks.append(c)

    # Messaging bridges (optional — only check if configured)
    channels_dir = Path.home() / ".claude" / "channels"
    for name, proc_name in [("telegram-bridge", "telegram-bridge"), ("discord-bridge", "discord-bridge")]:
        env_file = channels_dir / name.replace("-bridge", "") / ".env"
        if env_file.exists():
            try:
                result = subprocess.run(["pgrep", "-f", proc_name], capture_output=True)
                running = result.returncode == 0
            except:
                running = False
            if running:
                checks.append({"name": name, "status": "ok", "detail": "running"})
            else:
                checks.append({"name": name, "status": "warn", "detail": "configured but not running"})

    return checks


def main():
    as_json = "--json" in sys.argv
    do_fix = "--fix" in sys.argv

    checks = run_all_checks()
    issues = [c for c in checks if c["status"] not in ("ok", "warn")]

    if as_json:
        print(json.dumps({"checks": checks, "issues": len(issues), "total": len(checks)}, indent=2))
        return

    # Human-readable
    print("Sutando Health Check")
    print("=" * 40)

    for c in checks:
        icon = "✓" if c["status"] == "ok" else "⚠" if c["status"] == "warn" else "✗" if c["status"] in ("down", "missing", "not_loaded") else "~"
        print(f"  {icon} {c['name']:30s} {c['status']:12s} {c['detail']}")

    print()
    if not issues:
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
                elif c["name"] == "telegram-bridge":
                    subprocess.Popen(["python3", str(REPO_DIR / "src" / "telegram-bridge.py")],
                                     stdout=open(str(REPO_DIR / "src" / "telegram-bridge.log"), "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "discord-bridge":
                    subprocess.Popen(["python3", str(REPO_DIR / "src" / "discord-bridge.py")],
                                     stdout=open(str(REPO_DIR / "src" / "discord-bridge.log"), "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: restarted")

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
