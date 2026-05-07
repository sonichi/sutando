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

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from util_paths import shared_personal_path  # noqa: E402

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


def check_memory_sync() -> dict:
    """Verify memory sync is configured and has run recently."""
    name = "memory-sync"
    env_path = REPO_DIR / ".env"
    repo_url = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("SUTANDO_MEMORY_REPO="):
                repo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not repo_url:
        return {"name": name, "status": "warn", "detail": "SUTANDO_MEMORY_REPO not set — cross-machine sync disabled"}
    sync_dir = Path.home() / ".sutando-memory-sync"
    if not sync_dir.exists():
        return {"name": name, "status": "warn", "detail": "repo configured but never synced — run bash ~/.sutando-memory-sync/scripts/sync-memory.sh"}
    git_dir = sync_dir / ".git" / "FETCH_HEAD"
    if git_dir.exists():
        age_h = (time.time() - git_dir.stat().st_mtime) / 3600
        if age_h > 48:
            return {"name": name, "status": "warn", "detail": f"last sync {age_h:.0f}h ago (stale)"}
        return {"name": name, "status": "ok", "detail": f"last sync {age_h:.1f}h ago"}
    return {"name": name, "status": "ok", "detail": "initialized, never fetched"}


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

def mark_stale_if_outdated(check: dict, src_file: Path, pgrep_pattern: str, threshold_sec: int = 1800, binary_path: Optional[Path] = None) -> None:
    """Mark `check` as 'stale' in place if a process matching `pgrep_pattern`
    started more than `threshold_sec` before `src_file`'s mtime.

    Extracted so the same logic covers all tsx-managed services
    (voice-agent, web-client, conversation-server) without duplication.
    30 min default threshold tolerates `git checkout` mtime bumps; real
    stale deploys are hours/days old. Silent on any failure — stale
    detection is advisory, not authoritative.

    If `binary_path` is supplied (compiled artifacts like the Swift
    Sutando.app), the function ALSO checks whether the binary itself is
    older than the source. A stale binary means the running process —
    however recently relaunched — is executing old code. When this fires,
    the message tells the user to rebuild, not just restart.
    """
    if not src_file.exists():
        return
    # Compiled-artifact check: binary older than source → "rebuild needed",
    # regardless of process start. This catches the case where --fix
    # relaunches a stale binary repeatedly (#528 stopped the leak; this
    # makes the message actionable).
    if binary_path is not None and binary_path.exists():
        try:
            src_mtime = src_file.stat().st_mtime
            bin_mtime = binary_path.stat().st_mtime
            if src_mtime - bin_mtime > threshold_sec:
                age_min = int((src_mtime - bin_mtime) / 60)
                check["status"] = "stale"
                check["detail"] = f"running, but binary is {age_min} min older than source — rebuild needed"
                return
        except OSError:
            pass
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
            # Before flagging stale, cross-check with git: mtime gets bumped by
            # `git checkout`/`pull`/`rebase` even when the file content is
            # identical, which produced a steady stream of false positives
            # whenever a branch switch left the working tree unchanged on a
            # specific file. Ask git for the last commit time that actually
            # touched this file. If that's older than proc_start AND there
            # are no uncommitted changes to the file, it's a mtime-only
            # bump — the running code is still current.
            if _file_unchanged_since(src_file, proc_start):
                return
            check["status"] = "stale"
            check["detail"] = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
    except (subprocess.TimeoutExpired, OSError):
        pass


def _file_unchanged_since(src_file: Path, proc_start: float) -> bool:
    """Return True if git's last-commit-time for src_file predates proc_start
    AND the file has no uncommitted changes. Used to suppress stale-detection
    false positives from git operations that bump mtime without changing
    content. Silent-failure: returns False on any git error so real stale
    deploys aren't hidden.
    """
    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "HEAD", "--", str(src_file)],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5
        )
        if log.returncode != 0 or not log.stdout.strip():
            return False
        commit_time = int(log.stdout.strip())
        if commit_time >= proc_start:
            # Real commit landed after proc_start — genuinely stale
            return False
        # No commits since proc_start; check for uncommitted edits
        diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", str(src_file)],
            cwd=REPO_DIR, capture_output=True, timeout=5
        )
        return diff.returncode == 0  # 0 = no diff
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False


# Watchers task-bridge starts at voice-agent boot. If any of these is
# missing from the log after the most recent "Sutando — Voice Interface"
# banner, the watcher wasn't registered and the corresponding feature
# (context drop, note view, task results) is silently broken. This
# check was added after a 9-hour incident on 2026-04-09 where the
# note-view watcher was silently absent and nobody noticed until a
# user reported voice hallucinating note titles.
REQUIRED_VOICE_WATCHERS = [
    "Watching for context drops",
    "Watching for note views",
    "Watching for results",
]


def check_voice_watchers(voice_check: dict) -> dict:
    """Verify all 3 task-bridge watchers are registered in the current
    voice-agent process. Parses logs/voice-agent.log for the most recent
    boot banner and confirms each REQUIRED_VOICE_WATCHERS pattern
    appears after it.
    """
    check = {"name": "voice-watchers", "status": "ok", "detail": "all 3 watchers active"}
    # Only run if voice-agent itself is ok; otherwise the check is moot.
    # Distinguish "stale" (process running, old code) from absent.
    vs = voice_check.get("status")
    if vs != "ok":
        check["status"] = "warn"
        check["detail"] = f"voice-agent {vs}" if vs else "voice-agent status unknown"
        return check
    log_file = REPO_DIR / "logs" / "voice-agent.log"
    if not log_file.exists():
        check["status"] = "warn"
        check["detail"] = "voice-agent.log not found"
        return check
    try:
        lines = log_file.read_text(errors="replace").splitlines()
        # Find the most recent startup banner
        banner_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if "Sutando — Voice Interface" in lines[i]:
                banner_idx = i
                break
        if banner_idx < 0:
            check["status"] = "warn"
            check["detail"] = "no startup banner found in log"
            return check
        tail = lines[banner_idx:]
        # task-bridge logs watchers BEFORE the banner prints — check a
        # bounded window both sides to be safe (20 lines before banner)
        window_start = max(0, banner_idx - 20)
        window = lines[window_start:]
        missing = []
        for pat in REQUIRED_VOICE_WATCHERS:
            if not any(pat in line for line in window):
                missing.append(pat.replace("Watching for ", ""))
        if missing:
            check["status"] = "fail"
            check["detail"] = f"missing watcher(s): {', '.join(missing)} — restart voice-agent"
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"log read failed: {e}"
    return check


# Close codes that indicate a healthy voice-agent → Gemini Live transport
# state. Anything else after a startup banner suggests upstream failure
# (quota, auth, network blip, bodhi state-machine wedge).
#   1000 = normal closure
#   4000 = sutando custom goodbye disconnect (bodhi fork commit 44172b8)
VOICE_TRANSPORT_HEALTHY_CLOSE_CODES = {"1000", "4000"}


def _extract_close_code(line: str) -> Optional[str]:
    import re
    m = re.search(r"code=(\d+)", line)
    return m.group(1) if m else None


def _extract_close_reason(line: str) -> Optional[str]:
    import re
    m = re.search(r'reason="([^"]*)"', line)
    return m.group(1) if m else None


def check_voice_transport(voice_check: dict) -> dict:
    """Scan voice-agent.log from the most recent startup banner forward
    for abnormal Gemini transport closes. Flags things like:
        code=1011 "exceeded your current quota"    (the 3.1 tier issue)
        code=1007 "Request contains an invalid argument" (CLOSED→CLOSED)
        code=1006 abnormal / network drop
    Returns ok if the latest transport event since the most recent boot
    is "setup complete", or if an abnormal close was followed by a
    successful "setup complete" (auto-recovery worked).

    Added 2026-04-09 after the Gemini 3.1 dry-run produced a 1011 that
    health-check couldn't see — voice-agent port was up, bodhi was up,
    every existing probe said ok, but the transport was rejected
    server-side. Without this check, that failure mode is only visible
    to whoever manually tails the log.
    """
    check = {"name": "voice-transport", "status": "ok", "detail": "no recent transport errors"}
    vs = voice_check.get("status")
    if vs != "ok":
        check["status"] = "warn"
        check["detail"] = f"voice-agent {vs}" if vs else "voice-agent status unknown"
        return check
    log_file = REPO_DIR / "logs" / "voice-agent.log"
    if not log_file.exists():
        check["status"] = "warn"
        check["detail"] = "voice-agent.log not found"
        return check
    try:
        lines = log_file.read_text(errors="replace").splitlines()
        banner_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if "Sutando — Voice Interface" in lines[i]:
                banner_idx = i
                break
        if banner_idx < 0:
            check["status"] = "warn"
            check["detail"] = "no startup banner found in log"
            return check
        # Walk from the banner forward. Track the most recent transport
        # event and a few state flags so we can distinguish real failures
        # from expected idle-timeout closes.
        #
        # Expected idle path: Gemini Live fires a `GoAway` (60s warning),
        # then ~60s later closes the transport with code=1011
        # "The service is currently unavailable." Bodhi transitions the
        # session to CLOSED waiting for the next client connect. That's
        # a normal lifecycle event, not a failure — the session
        # reconnects fresh when a client comes back. If we flag every
        # 1011-after-GoAway as a fail, the probe reports a false
        # positive every time voice sits idle for 10+ minutes.
        most_recent_abnormal: Optional[str] = None
        abnormal_recovered = False
        goaway_before_close = False  # GoAway seen since the last setup/close
        for line in lines[banner_idx:]:
            if "Gemini setup complete" in line or "LLM transport connected and setup complete" in line:
                if most_recent_abnormal is not None:
                    abnormal_recovered = True
                    most_recent_abnormal = None
                goaway_before_close = False
            elif "GoAway from Gemini" in line:
                goaway_before_close = True
            elif "[VoiceSession] Transport closed" in line:
                m_code = _extract_close_code(line)
                if m_code is None:
                    continue
                if m_code in VOICE_TRANSPORT_HEALTHY_CLOSE_CODES:
                    most_recent_abnormal = None
                    goaway_before_close = False
                elif goaway_before_close:
                    # Idle timeout path — Google warned, then closed. Not an error.
                    most_recent_abnormal = None
                    goaway_before_close = False
                else:
                    most_recent_abnormal = line
                    abnormal_recovered = False
        if most_recent_abnormal is not None:
            reason = _extract_close_reason(most_recent_abnormal) or "unknown"
            code = _extract_close_code(most_recent_abnormal) or "?"
            check["status"] = "fail"
            check["detail"] = f"unrecovered transport close: code={code} reason={reason[:80]}"
        elif abnormal_recovered:
            check["detail"] = "transport recovered after earlier error"
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"log read failed: {e}"
    return check


def check_bodhi_dist() -> dict:
    """Verify the installed bodhi-realtime-agent dist has the Gemini 3.1
    wire-format fixes applied. Greps the Gemini sendAudio/sendFile bodies
    for the post-fix `audio:`/`video:` keys rather than the deprecated
    `media:` key.

    Added 2026-04-09 after the 1007 "media_chunks is deprecated" regression:
    package-lock.json pointed at the post-fix bodhi commit, but the dist
    on disk was stale (git pull advanced the lockfile without triggering
    npm install). voice-agent booted fine because sendAudio isn't
    exercised until a client connects — so existing probes silently let
    it through. This probe catches that case on every health tick.

    Fix when this check fails: `npm install github:sonichi/bodhi_realtime_agent`
    then `launchctl kickstart -k gui/$(id -u)/com.sutando.voice-agent`.
    """
    check = {"name": "bodhi-dist", "status": "ok", "detail": "Gemini 3.1 wire-format fixes present"}
    dist = REPO_DIR / "node_modules" / "bodhi-realtime-agent" / "dist" / "index.js"
    if not dist.exists():
        check["status"] = "warn"
        check["detail"] = "bodhi dist not found — run `npm install`"
        return check
    try:
        text = dist.read_text(errors="replace")
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"dist read failed: {e}"
        return check
    # Isolate the Gemini transport's sendAudio body. The OpenAI realtime
    # transport also defines sendAudio but uses `audio: base64Data` as a
    # flat string — a naive grep would false-positive.
    idx = text.find("sendAudio(base64Data) {")
    if idx < 0:
        check["status"] = "warn"
        check["detail"] = "could not locate sendAudio in bodhi dist"
        return check
    # Find the first two sendAudio definitions; the Gemini one wraps its
    # arg in `this.session.sendRealtimeInput(...)`.
    stale_audio = False
    stale_file = False
    # Scan each sendAudio body for the sendRealtimeInput caller (Gemini).
    for start in _find_all(text, "sendAudio(base64Data) {"):
        body = _extract_body(text, start)
        if "sendRealtimeInput" in body:
            if "media: { data" in body or "media:{data" in body:
                stale_audio = True
            break
    for start in _find_all(text, "sendFile(base64Data, mimeType) {"):
        body = _extract_body(text, start)
        if "sendRealtimeInput" in body:
            if "media: { data" in body or "media:{data" in body:
                stale_file = True
            break
    stale = []
    if stale_audio:
        stale.append("sendAudio")
    if stale_file:
        stale.append("sendFile")
    if stale:
        check["status"] = "fail"
        check["detail"] = (
            f"bodhi dist stale: {'/'.join(stale)} still uses deprecated `media` key — "
            "Gemini 3.1 rejects with 1007. Run `npm install github:sonichi/bodhi_realtime_agent`."
        )
    return check


def _find_all(haystack: str, needle: str):
    """Yield every start index where `needle` occurs in `haystack`."""
    i = 0
    while True:
        i = haystack.find(needle, i)
        if i < 0:
            return
        yield i
        i += len(needle)


def _extract_body(text: str, start: int) -> str:
    """Extract the function body (matched-brace region) starting at the
    first `{` at or after `start`. Returns at most the next 2000 chars.
    """
    brace = text.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for j in range(brace, min(brace + 2000, len(text))):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace : j + 1]
    return text[brace : brace + 2000]


def run_all_checks() -> list[dict]:
    checks = []

    # Core services (required)
    voice_check = check_port(9900, "voice-agent")
    if voice_check["status"] == "ok":
        mark_stale_if_outdated(voice_check, REPO_DIR / "src" / "voice-agent.ts", "voice-agent.ts")
    checks.append(voice_check)
    checks.append(check_voice_watchers(voice_check))
    checks.append(check_voice_transport(voice_check))
    checks.append(check_bodhi_dist())

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

    # Notes — canonical home is shared private dir post-migration
    checks.append(check_directory(Path(shared_personal_path("notes", REPO_DIR)), "notes-dir"))

    # Memory sync
    checks.append(check_memory_sync())

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
                from urllib.parse import urlparse as _urlparse
                _host = _urlparse(webhook_url).hostname or ""
                is_funnel = _host.endswith(".ts.net")
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

        # Check 2: Log file freshness — prefer logs/ (where startup.sh writes)
        # and fall back to src/ for legacy. The src/ default was silently a
        # no-op since 2026-04 when startup.sh was changed to write logs/<name>.log,
        # so log-stale warnings never fired (caught 2026-05-05 when Mini's
        # logs/discord-bridge.log was 36h stale but health-check stayed "ok").
        import time
        log_file = REPO_DIR / "logs" / f"{name}.log"
        if not log_file.exists():
            log_file = REPO_DIR / "src" / f"{name}.log"
        detail = "running"
        status = "ok"
        if log_file.exists():
            age_sec = time.time() - log_file.stat().st_mtime
            if age_sec > 300:  # 5 minutes
                status = "warn"
                detail = f"running but log stale ({int(age_sec)}s old)"

        # Check 3: Heartbeat file freshness (overrides log staleness if fresh)
        heartbeat_file = REPO_DIR / "state" / f"{name}.heartbeat"
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
                        # Cross-check with git before flagging — #253 added this
                        # for voice-agent + web-client via mark_stale_if_outdated,
                        # this path does the same check inline to reach bridges.
                        if not _file_unchanged_since(src_file, proc_start):
                            status = "stale"
                            detail = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass

        # Check 5: Dead-log-inode detection — last so heartbeat doesn't override.
        # Bridge process FDs point to a path that's been renamed/deleted (the
        # 2026-05-05 case where discord-bridge stdout was going to
        # /discord-bridge.log.bak after the file was unlinked, so logging
        # silently went to /dev/null while bridge appeared healthy). Heartbeats
        # don't catch this because they're written to a separate file via
        # state/<name>.heartbeat.
        try:
            lsof_out = subprocess.run(
                ["lsof", "-p", pids[0]], capture_output=True, text=True, timeout=5
            ).stdout
            for line in lsof_out.splitlines():
                parts = line.split()
                # Columns: COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME
                # FD column is index 3 — "1w" or "2w" carry log writes
                if len(parts) < 9 or parts[3] not in ("1w", "2w"):
                    continue
                # NAME starts at col 8 — join remaining tokens to handle paths
                # with spaces (per MacBook's PR #596 review nit).
                log_path = " ".join(parts[8:])
                if log_path.endswith(".log") or log_path.endswith(".log.bak"):
                    if not Path(log_path).exists():
                        status = "warn"
                        detail = (
                            f"running but log inode dead ({log_path} unlinked) — "
                            f"restart with: launchctl kickstart -k gui/$(id -u)/com.sutando.{name} "
                            "(or nohup+disown on Mini)"
                        )
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
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
            check = {"name": "sutando-app", "status": "ok", "detail": f"running (⌃C/⌃V/⌃M)"}
            mark_stale_if_outdated(
                check,
                REPO_DIR / "src" / "Sutando" / "main.swift",
                "src/Sutando/Sutando",
                binary_path=REPO_DIR / "src" / "Sutando" / "Sutando",
            )
            checks.append(check)
        else:
            checks.append({"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"})

    return checks


def emit_task_for_failures(checks: list[dict], state_file: Optional[Path] = None, tasks_dir: Optional[Path] = None) -> None:
    """Emit a task file describing health-check failures so the proactive
    loop's CLI session sees them via the watcher and can decide what to do
    (restart, DM owner, ignore as transient).

    Bridges the detection-vs-action gap: dashboard + morning-briefing already
    surface failures to the user, but no path drove the AGENT to act on them.
    Now health-check at any cron tick can produce a task file → watcher fires
    → CLI processes as owner-tier task → LLM judgment at the act step.

    Dedup via failure-SET hash to avoid spamming a task every tick when a
    failure persists. The hash covers the full active set (sorted member
    names) — if the set changes (one service recovers, another fails), the
    hash changes and a new task fires. Cooldown is 1h per hash so a
    persistent failure re-alerts after a reasonable window.

    `state_file` and `tasks_dir` default to the workspace paths used in
    production. Tests inject temp paths.
    """
    # `warn` is the status used for "service is up but has a real issue"
    # (e.g., the dead-log-inode case from PR #596 — bridge running but
    # logging silently to a deleted file). Including warn means the
    # watchdog catches the bug class that motivated this PR. Excluding
    # would have missed Mini's discord-bridge issue this morning. Per
    # her PR review note 2026-05-05.
    failures = [c for c in checks if c["status"] in ("down", "missing", "not_loaded", "fail", "stale", "warn")]
    if not failures:
        return

    if state_file is None or tasks_dir is None:
        REPO = Path(__file__).resolve().parent.parent
        if state_file is None:
            state_file = REPO / "state" / "health-last-alerted.json"
        if tasks_dir is None:
            tasks_dir = REPO / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Hash the full failure set (sorted) — Mini's #2 review note: hash MUST
    # cover the active set, not member-by-member, else suppressing legit
    # re-alerts when the set is identical to last alert.
    set_key = "|".join(sorted(c["name"] for c in failures))
    hash_key = hashlib.sha256(set_key.encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)
    cooldown_ms = 3600 * 1000  # 1h

    # Read prior alert state.
    history: dict = {}
    try:
        if state_file.exists():
            history = json.loads(state_file.read_text())
    except Exception:
        history = {}

    last_alerted = history.get(hash_key, 0)
    if now_ms - last_alerted < cooldown_ms:
        # Same failure set, within cooldown — skip.
        return

    # Build task content.
    ts_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    bullet_lines = [f"- {c['name']}: {c['status']} ({c['detail']})" for c in failures]
    body = (
        f"id: task-health-{int(time.time())}\n"
        f"timestamp: {ts_iso}\n"
        f"task: Health check found issues. Decide whether to restart, DM owner, or treat as transient:\n"
        + "\n".join(bullet_lines) + "\n"
        f"source: health-check\n"
        f"user_id: health-check\n"
        f"access_tier: owner\n"
    )
    task_path = tasks_dir / f"task-health-{int(time.time())}.txt"
    task_path.write_text(body)

    # Update history. Prune entries older than 24h to bound file size.
    history[hash_key] = now_ms
    cutoff = now_ms - (24 * 3600 * 1000)
    history = {k: v for k, v in history.items() if v >= cutoff}
    try:
        state_file.write_text(json.dumps(history))
    except Exception:
        pass


def main():
    as_json = "--json" in sys.argv
    do_fix = "--fix" in sys.argv
    do_emit = "--emit-task" in sys.argv
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    checks = run_all_checks()
    issues = [c for c in checks if c["status"] not in ("ok", "warn")]

    # Optional: emit a task for the CLI to act on. Runs before --json early-
    # return so cron-driven --json --emit-task callers get both behaviors.
    if do_emit:
        emit_task_for_failures(checks)

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
                    # Use sys.executable to avoid launchd's minimal PATH
                    # resolving `python3` to /usr/bin/python3 (3.9), which
                    # doesn't have the homebrew site-packages (discord,
                    # dotenv, etc.) — restart would crash on import.
                    # Log path uses logs/ (post-PR #251 refactor).
                    subprocess.Popen([sys.executable, str(REPO_DIR / "src" / f"{c['name']}.py")],
                                     stdout=open(str(REPO_DIR / "logs" / f"{c['name']}.log"), "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: {'restarted (stale code)' if c['status'] == 'stale' else 'restarted'}")
                elif c["name"] == "sutando-app":
                    # Stale here means main.swift is newer than the binary's
                    # process start time. The bare Popen below was leaking
                    # duplicates (macOS doesn't enforce singleton on this
                    # bundle — observed 3 concurrent on 2026-04-19) AND it
                    # was relaunching the same stale binary, so the stale
                    # signal kept re-firing every cron pass.
                    #
                    # Real fix needs (a) pkill the existing PID, (b) swiftc
                    # rebuild if source > binary, (c) `open src/Sutando/Sutando`.
                    # Until that lands, surface the warning instead of
                    # pretending we fixed it. Chi rebuilds + relaunches
                    # manually per feedback_sutando_app_launch_method.md.
                    print(f"  {c['name']}: not auto-fixed — needs manual rebuild + relaunch (see memory feedback_sutando_app_launch_method.md)")
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

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
