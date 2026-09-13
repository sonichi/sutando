#!/usr/bin/env pwsh
# Sutando startup for Windows — PowerShell twin of src/startup.sh.
#
# Starts the cross-platform Sutando services (voice agent, web client,
# dashboard, agent-api, screen-capture server, optional bridges) on Windows.
#
# Usage:
#   pwsh -File src/startup.ps1
#   pwsh -File src/startup.ps1 -SkipPhone  # skip Twilio + ngrok startup
#
# Does NOT install / launch macOS-only services:
#   - Sutando.app menu bar (Swift / Cocoa — Mac only)
#   - launchd services
#   - caffeinate (use Windows power-options instead)
#
# This script intentionally mirrors the macOS startup ordering so log files and
# port assignments stay identical across platforms.

[CmdletBinding()]
param(
    [switch]$SkipPhone,
    [switch]$SkipTelegram,
    [switch]$SkipDiscord,
    [switch]$SkipCore,
    [switch]$SkipDispatcher,
    [switch]$NoBrowser,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'

$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $REPO
$env:SUTANDO_ROOT = $REPO

# --- .env validation ---------------------------------------------------------

$envFile = Join-Path $REPO '.env'
if (-not (Test-Path $envFile)) {
    Write-Host "  X .env not found - cp .env.example .env and add your keys"
    exit 1
}

# Parse .env into the current process environment (mirrors `set -a; source .env`)
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $kv = $line -split '=', 2
        if ($kv.Length -eq 2) {
            $key = $kv[0].Trim()
            $val = $kv[1].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
        }
    }
}

if (-not $env:GEMINI_API_KEY) {
    Write-Host "  X GEMINI_API_KEY not set in .env - get one at https://ai.google.dev"
    exit 1
}

# --- Workspace resolution ----------------------------------------------------

. "$PSScriptRoot/workspace_default.ps1"
$WORKSPACE = Resolve-SutandoWorkspace
foreach ($d in 'logs','tasks','results','data','state') {
    New-Item -ItemType Directory -Force -Path (Join-Path $WORKSPACE $d) | Out-Null
}
$LOGS_DIR = Join-Path $WORKSPACE 'logs'

Write-Host "Sutando startup (Windows)..."
Write-Host ""
Write-Host "  Workspace: $WORKSPACE"

# --- CLI prerequisite checks -------------------------------------------------

$missing = 0
function Test-Tool($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Host "  X $name not found - $hint"
        return $false
    }
    return $true
}

if (-not (Test-Tool 'node' 'install Node.js 22+ from https://nodejs.org')) { $missing = 1 }
if (-not (Test-Tool 'npx' 'comes with node')) { $missing = 1 }
# Accept either a `python` on PATH or the `py` launcher — python.org installs
# without "add to PATH" only expose `py`, and Get-PythonCmd falls back to it.
if (-not ((Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command py -ErrorAction SilentlyContinue))) {
    Write-Host "  X python not found - install Python 3.11+ from https://python.org"
    $missing = 1
}
if (-not (Test-Tool 'claude' 'install Claude Code: https://docs.anthropic.com/en/docs/claude-code/getting-started')) { $missing = 1 }

if ($missing -eq 1) {
    Write-Host ""
    Write-Host "Fix the above and try again."
    exit 1
}

# --- Dependency install ------------------------------------------------------

if (-not (Test-Path (Join-Path $REPO 'node_modules'))) {
    Write-Host "  Installing npm dependencies..."
    npm install
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  X npm install failed"
        exit 1
    }
    Write-Host "  + Dependencies installed"
}

# --- Service launch helpers --------------------------------------------------

function Test-Port($port) {
    $r = netstat -ano | Select-String -SimpleMatch ":$port " | Select-String 'LISTENING'
    return [bool]$r
}

function Wait-Port($port, $timeoutSeconds = 15) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    do {
        if (Test-Port $port) { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

# Detect a running bridge by command-line substring, in-process. The old
# approach spawned `& powershell -Command "Get-CimInstance ..."` which, under
# $ErrorActionPreference='Stop', threw NativeCommandExitException whenever the
# child exited non-zero (e.g. no match). Query CIM directly and swallow errors.
function Test-BridgeRunning($needle) {
    try {
        return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } |
            Select-Object -First 1)
    } catch { return $false }
}

function Start-Service-Bg($name, $port, $cmd, $arglist, $logFile) {
    if (Test-Port $port) {
        Write-Host "  + $name (port $port, already running)"
        return
    }
    Write-Host "  Starting $name (port $port)..."
    $logPath = Join-Path $LOGS_DIR $logFile

    # Resolve the executable. On Windows, npx/npm are typically distributed as
    # multiple sibling shims: npx.cmd (bash/cmd), npx.ps1 (PowerShell),
    # npx (bash). PowerShell's Get-Command prefers .ps1, but Start-Process
    # -FilePath only accepts real Win32 executables — handing it a .ps1 fails
    # with "%1 is not a valid Win32 application". Probe for a .cmd/.bat/.exe
    # sibling first; only fall back to .ps1 (via pwsh -File) if no shim exists.
    function Resolve-WindowsExe($name) {
        $hit = Get-Command $name -All -ErrorAction SilentlyContinue
        if (-not $hit) { return $null }
        # Prefer real executables in this priority order.
        foreach ($ext in '.exe', '.cmd', '.bat') {
            $match = $hit | Where-Object { $_.Path -and $_.Path.ToLower().EndsWith($ext) } | Select-Object -First 1
            if ($match) { return @{ Path = $match.Path; Kind = $ext.TrimStart('.') } }
        }
        # No Win32 shim — fall back to the first .ps1 / external script via pwsh.
        $ps1 = $hit | Where-Object { $_.Path -and $_.Path.ToLower().EndsWith('.ps1') } | Select-Object -First 1
        if ($ps1) { return @{ Path = $ps1.Path; Kind = 'ps1' } }
        # Last resort: take whatever Get-Command surfaced.
        return @{ Path = $hit[0].Path; Kind = 'other' }
    }

    $r = Resolve-WindowsExe $cmd
    if (-not $r) {
        Write-Host "  X ${name}: '$cmd' not found on PATH"
        return
    }

    if ($r.Kind -eq 'exe') {
        Start-Process -FilePath $r.Path -ArgumentList $arglist `
            -WindowStyle Hidden -RedirectStandardOutput $logPath `
            -RedirectStandardError "$logPath.err" | Out-Null
    } elseif ($r.Kind -eq 'cmd' -or $r.Kind -eq 'bat') {
        # cmd.exe /c invokes the shim and respects PATHEXT. Always quote the
        # shim path (it usually lives under "C:\Program Files\nodejs\..." with
        # a space) and quote args that contain whitespace. Without quoting cmd
        # splits at the space and reports "'C:\Program' is not recognized".
        $quotedPath = '"' + $r.Path + '"'
        $quotedArgs = $arglist | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
        $cmdArgs = @('/c', $quotedPath) + $quotedArgs
        Start-Process -FilePath 'cmd.exe' -ArgumentList $cmdArgs `
            -WindowStyle Hidden -RedirectStandardOutput $logPath `
            -RedirectStandardError "$logPath.err" | Out-Null
    } elseif ($r.Kind -eq 'ps1') {
        # Run the .ps1 shim via pwsh -File. Args after -File are passed through.
        $ps1Args = @('-NoProfile', '-File', $r.Path) + $arglist
        Start-Process -FilePath 'pwsh.exe' -ArgumentList $ps1Args `
            -WindowStyle Hidden -RedirectStandardOutput $logPath `
            -RedirectStandardError "$logPath.err" | Out-Null
    } else {
        Write-Host "  X ${name}: cannot launch '$($r.Path)' (unknown shim type)"
        return
    }
    Write-Host "  + $name"
}

# Resolve the python interpreter — `python` on Windows usually maps to the
# Microsoft Store launcher; `py -3` is the canonical launcher for installed
# CPython. Prefer `python` only if it runs Python 3 directly.
function Get-PythonCmd {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $pyVer = (& python -c "import sys; print(sys.version_info[0])" 2>$null)
        if ($pyVer -eq '3') { return 'python' }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) { return 'py' }
    return 'python'
}
$PY = Get-PythonCmd

# Force UTF-8 stdio in child Python processes. The Windows console defaults to
# cp1252; the Sutando services print Unicode arrows (→) at startup which would
# otherwise crash with UnicodeEncodeError before they bind their ports. Child
# processes started via Start-Process inherit the parent env, so setting these
# here is enough — no per-call plumbing.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if ($ValidateOnly) {
    Write-Host "Windows startup validation passed."
    Write-Host "  Workspace: $WORKSPACE"
    Write-Host "  Python: $PY"
    exit 0
}

# --- Install Sutando skills into ~/.claude/skills/ --------------------------
# Required so the core Claude Code session can resolve /schedule-crons and
# other Sutando slash commands. Idempotent - safe to run on every startup.
Write-Host ""
Write-Host "Installing Sutando skills..."
& pwsh -NoProfile -File (Join-Path $REPO 'skills\install.ps1')

# --- Core services -----------------------------------------------------------

# 1. Voice agent (port 9900)
Start-Service-Bg 'voice agent' 9900 'npx' @('tsx', (Join-Path $REPO 'src\voice-agent.ts')) 'voice-agent.log'

# 2. Web client (port 8080)
Start-Service-Bg 'web client' 8080 'npx' @('tsx', (Join-Path $REPO 'src\web-client.ts')) 'web-client.log'

# 3. Dashboard (port 7844)
$dashArgs = @((Join-Path $REPO 'src\dashboard.py'))
if ($PY -eq 'py') { $dashArgs = @('-3') + $dashArgs }
Start-Service-Bg 'dashboard' 7844 $PY $dashArgs 'dashboard.log'

# 4. Agent API (port 7843)
$apiArgs = @((Join-Path $REPO 'src\agent-api.py'))
if ($PY -eq 'py') { $apiArgs = @('-3') + $apiArgs }
Start-Service-Bg 'agent API' 7843 $PY $apiArgs 'agent-api.log'

# 5. Screen capture (port 7845) — uses PowerShell screenshot under the hood
$scArgs = @((Join-Path $REPO 'src\screen-capture-server.py'))
if ($PY -eq 'py') { $scArgs = @('-3') + $scArgs }
Start-Service-Bg 'screen capture' 7845 $PY $scArgs 'screen-capture.log'

# --- Optional bridges --------------------------------------------------------

if (-not $SkipTelegram -and (Test-Path (Join-Path $HOME '.claude\channels\telegram\.env'))) {
    $tgEnv = Get-Content (Join-Path $HOME '.claude\channels\telegram\.env')
    if ($tgEnv -match 'TELEGRAM_BOT_TOKEN=') {
        $tgArgs = @((Join-Path $REPO 'src\telegram-bridge.py'))
        if ($PY -eq 'py') { $tgArgs = @('-3') + $tgArgs }
        if (-not (Test-BridgeRunning 'telegram-bridge')) {
            Write-Host "  Starting Telegram bridge..."
            Start-Process -FilePath $PY -ArgumentList $tgArgs -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LOGS_DIR 'telegram-bridge.log') `
                -RedirectStandardError (Join-Path $LOGS_DIR 'telegram-bridge.log.err') | Out-Null
            Write-Host "  + telegram bridge"
        } else {
            Write-Host "  + telegram bridge (already running)"
        }
    }
} else {
    Write-Host "  ~ telegram bridge (skipped or no token - optional)"
}

if (-not $SkipDiscord -and (Test-Path (Join-Path $HOME '.claude\channels\discord\.env'))) {
    $dcEnv = Get-Content (Join-Path $HOME '.claude\channels\discord\.env')
    if ($dcEnv -match 'DISCORD_BOT_TOKEN=') {
        $dcArgs = @((Join-Path $REPO 'src\discord-bridge.py'))
        if ($PY -eq 'py') { $dcArgs = @('-3') + $dcArgs }
        if (-not (Test-BridgeRunning 'discord-bridge')) {
            Write-Host "  Starting Discord bridge..."
            Start-Process -FilePath $PY -ArgumentList $dcArgs -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LOGS_DIR 'discord-bridge.log') `
                -RedirectStandardError (Join-Path $LOGS_DIR 'discord-bridge.log.err') | Out-Null
            Write-Host "  + discord bridge"
        } else {
            Write-Host "  + discord bridge (already running)"
        }
    }
} else {
    Write-Host "  ~ discord bridge (skipped or no token - optional)"
}

if (-not $SkipPhone -and ($env:TWILIO_ACCOUNT_SID)) {
    Write-Host "  ~ phone services on Windows are experimental (requires ngrok + Twilio webhook setup)"
} else {
    Write-Host "  ~ conversation server (skipped or no Twilio creds - optional)"
}

# --- Verify ports actually came up ------------------------------------------

Start-Sleep -Seconds 3
Write-Host ""
Write-Host "Verifying services..."
$VERIFY = @{ '9900'='voice-agent'; '8080'='web-client'; '7844'='dashboard'; '7843'='agent-api'; '7845'='screen-capture' }
foreach ($p in $VERIFY.Keys) {
    if (Wait-Port $p) {
        Write-Host "  + $($VERIFY[$p]) (port $p)"
    } else {
        Write-Host "  X $($VERIFY[$p]) (port $p) - check $LOGS_DIR\$($VERIFY[$p]).log"
    }
}
Write-Host ""
if (-not $NoBrowser) {
    Start-Process "http://localhost:8080"
}

# --- Core agent (Claude Code) -----------------------------------------------
# The services above are the data plane (voice in/out, task storage, UI);
# the core agent is what actually processes tasks from `tasks/` and writes
# `results/`. Without it, the web UI accepts your chats but nothing happens.
# Launches in a new Windows Terminal window so you can see the TUI + approve
# any one-off prompts. Skip with -SkipCore if you want to run it yourself.
if (-not $SkipCore) {
    Write-Host ""
    Write-Host "Starting sutando-core (Claude Code) in a new window..."
    & pwsh -NoProfile -File (Join-Path $REPO 'scripts\start-cli.ps1')
} else {
    Write-Host ""
    Write-Host "  ~ sutando-core skipped (use -SkipCore=$false or run scripts/start-cli.ps1 manually)"
    Write-Host "    Without the core, tasks queue but never run."
}

# --- Task dispatcher (Windows-only Monitor-equivalent) ----------------------
# Claude Code 2.x dropped the Monitor tool, so the long-running sutando-core
# only picks up new tasks on its 5min cron tick - too slow for chat. The
# dispatcher watches tasks/ and shells out to `claude --print` per new task
# for sub-5s latency, independent of cron timing. The core still runs the
# autonomous proactive-loop work; this just handles user-driven chat tasks.
if (-not $SkipDispatcher) {
    Write-Host ""
    Write-Host "Starting task-dispatcher (push-driven chat-task processor)..."
    & pwsh -NoProfile -File (Join-Path $REPO 'src\task-dispatcher.ps1') -Background
} else {
    Write-Host ""
    Write-Host "  ~ task-dispatcher skipped (use -SkipDispatcher=$false to enable)"
    Write-Host "    Without it, chat tasks wait up to 1min for the next core cron tick."
}

Write-Host ""
Write-Host "Sutando services are running."
Write-Host "Web UI: http://localhost:8080"
