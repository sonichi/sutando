"""Cross-platform OS abstraction for Sutando Python services.

Twin of src/platform.ts. Sutando was originally Mac-only and the legacy code
calls `osascript`, `screencapture`, `pbcopy/pbpaste`, `pgrep`, `pkill`, `lsof`
directly. Call sites now delegate to the helpers below — they branch on
`sys.platform` and pick the right backend (osascript on macOS, PowerShell on
Windows). Helpers never raise; they return False/empty/None and let the
caller decide whether the failure is fatal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


# ---------- Notifications ----------

def notify(message: str, title: str = "Sutando") -> None:
    """Fire a desktop notification. Best-effort — never raises."""
    try:
        if is_macos():
            safe = message.replace('"', '\\"')
            safe_title = title.replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe}" with title "{safe_title}"'],
                timeout=2.0,
                capture_output=True,
                check=False,
            )
            return
        if is_windows():
            safe = message.replace("'", "''")
            safe_title = title.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$n.BalloonTipTitle = '{safe_title}'; "
                f"$n.BalloonTipText = '{safe}'; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(3000); "
                "Start-Sleep -Milliseconds 3500; "
                "$n.Dispose();"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=5.0,
                capture_output=True,
                check=False,
            )
            return
        # Linux best-effort
        try:
            subprocess.run(["notify-send", title, message], timeout=2.0, capture_output=True, check=False)
        except FileNotFoundError:
            pass
    except Exception:
        pass  # Notifications are advisory; failure is non-fatal.


# ---------- Clipboard ----------

def clipboard_read() -> str:
    """Return the current clipboard text; "" on failure."""
    try:
        if is_macos():
            r = subprocess.run(["pbpaste"], timeout=2.0, capture_output=True, text=True, check=False)
            return r.stdout or ""
        if is_windows():
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
                timeout=3.0,
                capture_output=True,
                text=True,
                check=False,
            )
            return r.stdout or ""
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            timeout=2.0, capture_output=True, text=True, check=False,
        )
        return r.stdout or ""
    except Exception:
        return ""


def clipboard_write(text: str) -> None:
    """Write `text` to the clipboard. Best-effort — never raises."""
    try:
        if is_macos():
            subprocess.run(["pbcopy"], input=text, timeout=2.0, text=True, check=False)
            return
        if is_windows():
            # `$input | Set-Clipboard` reads from STDIN via the $input automatic
            # variable; plain `Set-Clipboard` (without `-Value`) ignores stdin.
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                input=text,
                timeout=3.0,
                text=True,
                check=False,
            )
            return
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text, timeout=2.0, text=True, check=False,
        )
    except Exception:
        pass


# ---------- Process listing / killing ----------

WINDOWS_CIM_TIMEOUT = 15.0


def process_executable(pid: str | int) -> str | None:
    """Return the executable path for a PID, or None when unavailable."""
    try:
        if is_windows():
            pid_value = int(pid)
            if pid_value < 0:
                return None
            script = (
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid_value}' "
                "-ErrorAction Stop; if ($p) { $p.ExecutablePath }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=WINDOWS_CIM_TIMEOUT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        elif is_macos() or is_linux():
            result = subprocess.run(
                ["/bin/ps", "-o", "comm=", "-p", str(pid)],
                timeout=5.0,
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            return None
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def process_snapshot() -> str | None:
    """Return a `PID PPID ARGS` process table, or None when unavailable."""
    try:
        if is_macos() or is_linux():
            result = subprocess.run(
                ["ps", "-Ao", "pid,ppid,args"],
                timeout=5.0,
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout if result.returncode == 0 else None
        if not is_windows():
            return None
        script = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
            "$rows = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
            "Where-Object { $_.CommandLine } | "
            "Select-Object ProcessId, ParentProcessId, CommandLine); "
            "ConvertTo-Json -Compress -InputObject $rows"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=WINDOWS_CIM_TIMEOUT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip().lstrip("\ufeff")
        if not raw:
            return None
        payload = json.loads(raw)
        records = payload if isinstance(payload, list) else [payload]
        rows: list[tuple[int, int, str]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            command = record.get("CommandLine")
            if not isinstance(command, str) or not command.strip():
                continue
            rows.append((
                int(record["ProcessId"]),
                int(record.get("ParentProcessId") or 0),
                " ".join(command.splitlines()),
            ))
        rows.sort(key=lambda row: row[0])
        return "PID PPID ARGS\n" + "".join(
            f"{pid} {ppid} {command}\n" for pid, ppid, command in rows)
    except Exception:
        return None


def probe_pids(pattern: str) -> tuple[list[str], bool]:
    """Return matching PIDs and whether process enumeration succeeded.

    macOS/Linux: `pgrep -f <pattern>` (pattern is a regex, per pgrep).
    Windows: substring match on CommandLine (case-insensitive) via
    `Get-CimInstance Win32_Process` — PowerShell has no pgrep. A trailing `$`
    in the pattern is honored as an end-of-command-line anchor (mirroring
    pgrep's `$`), and a leading `^` as a start anchor, so one `foo\\.py$`-style
    pattern means the same thing on both platforms. Regex backslash-escapes
    (`\\.`) are normalized to plain chars for the Windows literal compare.
    The boolean distinguishes a clean no-match from a failed probe.
    """
    try:
        if is_macos() or is_linux():
            r = subprocess.run(["pgrep", "-f", pattern], timeout=3.0, capture_output=True, text=True, check=False)
            if r.returncode not in (0, 1):
                return [], False
            pids = [p for p in (r.stdout or "").strip().split("\n") if p]
            return pids, True
        if is_windows():
            anchor_start = pattern.startswith("^")
            anchor_end = pattern.endswith("$")
            core = pattern[1:] if anchor_start else pattern
            core = core[:-1] if anchor_end else core
            core = core.replace("\\.", ".").replace("\\", "")  # de-regex for literal compare
            safe = core.replace("'", "''").lower()
            if anchor_start and anchor_end:
                cond = f"$cl -eq '{safe}'"
            elif anchor_end:
                cond = f"$cl.EndsWith('{safe}')"
            elif anchor_start:
                cond = f"$cl.StartsWith('{safe}')"
            else:
                cond = f"$cl.Contains('{safe}')"
            # Tag the query and exclude its tag/$PID so the interpolated pattern
            # cannot make the helper or its wrapper self-match.
            sentinel = "__sutando_find_pids__"
            script = (
                f"# {sentinel}\n"
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -and $_.ProcessId -ne $PID } | "
                "ForEach-Object { $cl = $_.CommandLine.ToLower().Trim(); "
                f"if ($cl -notlike '*{sentinel}*' -and {cond}) {{ $_.ProcessId }} }}"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=WINDOWS_CIM_TIMEOUT,
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode != 0:
                return [], False
            pids = [p for p in (r.stdout or "").strip().split("\n")
                    if p.strip().isdigit()]
            return pids, True
    except Exception:
        pass
    return [], False


def find_pids(pattern: str) -> list[str]:
    """Return matching process IDs; preserve the legacy empty-on-failure API."""
    return probe_pids(pattern)[0]


def is_process_running(pattern: str) -> bool:
    """True iff some running process command line contains `pattern`."""
    try:
        if is_macos() or is_linux():
            r = subprocess.run(["pgrep", "-f", pattern], timeout=3.0, capture_output=True, check=False)
            return r.returncode == 0
        if is_windows():
            safe = pattern.replace("'", "''").lower()
            script = (
                "Get-CimInstance Win32_Process | Where-Object { "
                f"$_.CommandLine -and $_.CommandLine.ToLower().Contains('{safe}') }} | "
                "Select-Object -First 1 ProcessId"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=5.0,
                capture_output=True,
                text=True,
                check=False,
            )
            return "ProcessId" in (r.stdout or "")
    except Exception:
        pass
    return False


def kill_process(pattern: str) -> None:
    """Best-effort kill of every process whose command line matches `pattern`."""
    try:
        if is_macos() or is_linux():
            subprocess.run(["pkill", "-f", pattern], timeout=3.0, capture_output=True, check=False)
            return
        if is_windows():
            safe = pattern.replace("'", "''").lower()
            script = (
                "Get-CimInstance Win32_Process | Where-Object { "
                f"$_.CommandLine -and $_.CommandLine.ToLower().Contains('{safe}') }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=5.0,
                capture_output=True,
                check=False,
            )
    except Exception:
        pass


# ---------- Port-in-use check ----------

def is_port_in_use(port: int) -> bool:
    """True iff any process is listening on `port`."""
    try:
        if is_macos() or is_linux():
            r = subprocess.run(["lsof", "-i", f":{port}"], timeout=3.0, capture_output=True, check=False)
            return r.returncode == 0
        if is_windows():
            r = subprocess.run(
                ["netstat", "-ano"],
                timeout=5.0, capture_output=True, text=True, check=False,
            )
            needle = f":{port} "
            for line in (r.stdout or "").splitlines():
                if needle in line and "LISTENING" in line.upper():
                    return True
    except Exception:
        pass
    return False


# ---------- Screen capture ----------

def capture_screen(out_path: str, fmt: str = "png") -> bool:
    """Capture the entire primary display to `out_path`. Returns True on success.

    `fmt` is 'png' or 'jpg'. On macOS uses /usr/sbin/screencapture; on Windows
    uses PowerShell + System.Drawing.
    """
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        if is_macos():
            type_flag = "jpg" if fmt in ("jpg", "jpeg") else "png"
            r = subprocess.run(
                ["screencapture", "-x", "-t", type_flag, out_path],
                timeout=5.0, capture_output=True, check=False,
            )
            return r.returncode == 0 and Path(out_path).exists()
        if is_windows():
            fmt_token = "Jpeg" if fmt in ("jpg", "jpeg") else "Png"
            safe = out_path.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
                "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
                f"$bmp.Save('{safe}', [System.Drawing.Imaging.ImageFormat]::{fmt_token}); "
                "$g.Dispose(); $bmp.Dispose();"
            )
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=10.0, capture_output=True, check=False,
            )
            return r.returncode == 0 and Path(out_path).exists()
    except Exception:
        pass
    return False


# ---------- Open with default handler ----------

def open_with_default(target: str) -> None:
    """Open `target` (file path or URL) with the OS default handler."""
    try:
        if is_macos():
            subprocess.run(["open", target], timeout=5.0, capture_output=True, check=False)
            return
        if is_windows():
            # os.startfile is the canonical Windows API; falls through to ShellExecute.
            os.startfile(target)  # type: ignore[attr-defined]
            return
        subprocess.run(["xdg-open", target], timeout=5.0, capture_output=True, check=False)
    except Exception:
        pass
