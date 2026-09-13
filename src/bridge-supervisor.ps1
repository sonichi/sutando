#!/usr/bin/env pwsh
# Sutando bridge supervisor (Windows) — keeps a chat bridge online.
#
# The bridges (discord/telegram/slack) are long-running Python processes that
# can crash (network drop, Discord gateway 1008, an unhandled edge in a task).
# Nothing on Windows restarted them — health-check.py only *reports* "not
# running". This script is the missing supervisor: it runs the bridge in a
# restart-on-exit loop with capped backoff, so a crash means seconds of
# downtime instead of "offline until someone notices".
#
# The bridge's own single_instance lock (src/single_instance.py) guarantees we
# never end up with two copies even if this supervisor races a manual start.
#
# Usage:
#   pwsh -File src/bridge-supervisor.ps1                 # supervises discord-bridge
#   pwsh -File src/bridge-supervisor.ps1 -Bridge discord-bridge
#   pwsh -File src/bridge-supervisor.ps1 -Bridge telegram-bridge
#
# Registered to run at logon via src/install-bridge-task.ps1.

[CmdletBinding()]
param(
    [string]$Bridge = 'discord-bridge'
)

$ErrorActionPreference = 'Continue'
$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Workspace resolution — matches startup.ps1 / workspace_default contract.
. "$PSScriptRoot/workspace_default.ps1"
$WORKSPACE = Resolve-SutandoWorkspace
$LOGS = Join-Path $WORKSPACE 'logs'
New-Item -ItemType Directory -Force -Path $LOGS | Out-Null
$SUPLOG  = Join-Path $LOGS "$Bridge.supervisor.log"
$BRIDGELOG = Join-Path $LOGS "$Bridge.log"

# Load .env so the bridge child inherits the same keys startup.ps1 provides.
$envFile = Join-Path $REPO '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $kv = $line -split '=', 2
            if ($kv.Length -eq 2) {
                [Environment]::SetEnvironmentVariable($kv[0].Trim(), $kv[1].Trim().Trim('"').Trim("'"), 'Process')
            }
        }
    }
}
# Force UTF-8 so Chinese task bodies / Unicode logs don't crash the child.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $SUPLOG -Value $line
}

# Resolve the Python interpreter that actually has discord.py. The bridge's own
# self-rescue only knows macOS Homebrew paths, so on Windows we must hand it the
# right interpreter. Prefer an explicit override, then the conda env that ships
# discord.py, then PATH `python`.
function Resolve-Python {
    if ($env:SUTANDO_PYTHON -and (Test-Path $env:SUTANDO_PYTHON)) { return $env:SUTANDO_PYTHON }
    $candidates = @(
        (Join-Path $HOME 'Miniforge3\envs\flaml313\python.exe'),
        (Join-Path $HOME 'miniconda3\python.exe'),
        (Join-Path $HOME 'Anaconda3\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            $ok = & $c -c "import discord" 2>$null; if ($LASTEXITCODE -eq 0) { return $c }
        }
    }
    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($onPath) { return $onPath }
    return 'python'
}

$PY = Resolve-Python
$script = Join-Path $REPO "src\$Bridge.py"
if (-not (Test-Path $script)) {
    Log "FATAL: bridge script not found: $script"
    exit 1
}

Log "supervisor started for $Bridge — interpreter=$PY script=$script"

# Restart loop with capped exponential backoff. A bridge that exits within
# $minRunSeconds is treated as a crash-loop and backed off; one that ran longer
# resets the backoff (a normal long-lived run that finally dropped its socket).
$backoff = 2
$maxBackoff = 60
$minRunSeconds = 30

while ($true) {
    $start = Get-Date
    Log "launching $Bridge (backoff now ${backoff}s)"
    try {
        # Run in the foreground of THIS supervisor process so we block until the
        # bridge exits. stdout+stderr go to the bridge's normal log file.
        & $PY $script *>> $BRIDGELOG
        $code = $LASTEXITCODE
    } catch {
        $code = -1
        Log "launch threw: $_"
    }
    $ran = (New-TimeSpan -Start $start -End (Get-Date)).TotalSeconds
    Log ("$Bridge exited code=$code after {0:N0}s" -f $ran)

    if ($ran -ge $minRunSeconds) {
        $backoff = 2  # healthy run — reset
    } else {
        $backoff = [Math]::Min($backoff * 2, $maxBackoff)
    }
    Start-Sleep -Seconds $backoff
}
