#!/usr/bin/env pwsh
# scripts/start-cli.ps1 - Windows twin of start-cli.sh.
# Launches the sutando-core Claude Code session in a new PowerShell window.
#
# Usage:
#   pwsh -File scripts/start-cli.ps1            # start (or attach hint if running)
#   pwsh -File scripts/start-cli.ps1 -Restart   # kill existing then start fresh
#   pwsh -File scripts/start-cli.ps1 -NoWindow  # start in current window (foreground)
#
# Windows has no tmux. Strategy:
#   1. Detect any running `claude --name sutando-core` and skip / restart.
#   2. Launch claude in a new Windows Terminal / PowerShell window so the user
#      sees the Claude Code TUI and can interact with it (approve prompts,
#      type /restart, etc.).
#   3. The new window's process IS the core - close it to stop the core.
#
# Health-check / Sutando.app's "Restart Core" menu is not ported; trigger
# this script manually instead.

[CmdletBinding()]
param(
    [switch]$Restart,
    [switch]$NoWindow
)

$ErrorActionPreference = 'Stop'
$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Same pattern startup.sh / health-check.py use to identify the core process.
function Get-CorePids {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'claude' -and
        $_.CommandLine -match 'sutando-core'
    } | Select-Object -ExpandProperty ProcessId
}

if ($Restart) {
    $existing = Get-CorePids
    if ($existing) {
        Write-Host "Killing existing sutando-core session (pid $($existing -join ', '))..."
        foreach ($corePid in $existing) {
            Stop-Process -Id $corePid -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }
}

# Already running? Skip with an attach hint (Windows has no attach equivalent;
# the user finds the existing window).
$existing = Get-CorePids
if ($existing) {
    Write-Host "sutando-core already running (pid $($existing -join ', '))."
    Write-Host "Find the Claude Code window, or pass -Restart to start fresh."
    exit 0
}

# Locate claude executable. On Windows, `claude` is often a .cmd / .ps1 shim.
$claudeCmd = Get-Command claude -All -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $claudeCmd) {
    Write-Host "X claude not found on PATH - install Claude Code:"
    Write-Host "  https://docs.anthropic.com/en/docs/claude-code/getting-started"
    exit 1
}

# Model override (mirrors the bash script's SUTANDO_CORE_MODEL handling).
$modelArgs = @()
if ($env:SUTANDO_CORE_MODEL) {
    $modelArgs = @('--model', $env:SUTANDO_CORE_MODEL)
}

$claudeArgs = @(
    '--name', 'sutando-core'
) + $modelArgs + @(
    '--dangerously-skip-permissions',
    '--add-dir', $HOME,
    '--',
    # /proactive-loop calls /schedule-crons internally AND immediately starts
    # the streaming task watcher (`Bash run_in_background: true` on the
    # platform-appropriate watch-tasks-stream script) + the per-pass body. On
    # macOS the equivalent macOS-side start-cli.sh launches with /schedule-crons
    # and waits ~5min for the first proactive-loop cron tick — which is too slow
    # for the chat path (tasks visibly hang for minutes). Going straight to
    # /proactive-loop closes that gap on first start and the cron picks up from
    # there.
    '/proactive-loop'
)

if ($NoWindow) {
    # Foreground: replace the current shell with claude (TTY needed for interactive prompts).
    Write-Host "Launching sutando-core in this window. Press Ctrl+C to exit."
    & $claudeCmd.Source @claudeArgs
    exit $LASTEXITCODE
}

# Background path: spawn a new Windows Terminal / PowerShell window so the
# user can see and interact with the Claude Code TUI. Prefer Windows Terminal
# (wt.exe) for a proper tabbed experience; fall back to a plain pwsh window.
$wt = Get-Command wt -ErrorAction SilentlyContinue
$psExe = (Get-Command pwsh -ErrorAction SilentlyContinue) ?? (Get-Command powershell -ErrorAction SilentlyContinue)
if (-not $psExe) {
    Write-Host "X No PowerShell found - install pwsh from https://github.com/PowerShell/PowerShell"
    exit 1
}

# Build a self-contained launcher script the new window will run via -File.
# Going through a temp .ps1 avoids the shell-quoting hell of stuffing the full
# command into a -Command string (especially when wt.exe sits in between and
# applies its own argv parser to the rest of the line). The launcher just does:
#   1. cd to the repo
#   2. invoke claude.exe directly with the prepared argv
#   3. Pause on exit so the user sees any startup error before the window closes
$launcherDir = Join-Path $env:TEMP 'sutando-launcher'
New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null
$launcher = Join-Path $launcherDir "core-$([Diagnostics.Process]::GetCurrentProcess().Id).ps1"

# PowerShell's & call operator handles spaces in $claudePath natively when the
# path is a variable, no quoting required. Same for each $claudeArgs element.
$launcherBody = @"
`$ErrorActionPreference = 'Continue'
Set-Location '$REPO'
Write-Host 'Launching sutando-core...' -ForegroundColor Cyan
Write-Host '  claude: $($claudeCmd.Source)'
Write-Host '  args  : $($claudeArgs -join ' ')'
Write-Host ''
`$claudePath = '$($claudeCmd.Source)'
`$claudeArgs = @(
$(($claudeArgs | ForEach-Object { "    '" + ($_ -replace "'", "''") + "'" }) -join ",`n")
)
& `$claudePath @claudeArgs
`$ec = `$LASTEXITCODE
Write-Host ''
Write-Host "sutando-core exited (code `$ec). Press Enter to close this window." -ForegroundColor Yellow
[void](Read-Host)
"@
Set-Content -Path $launcher -Value $launcherBody -Encoding UTF8

if ($wt) {
    Write-Host "Launching sutando-core in a new Windows Terminal window..."
    # wt.exe argv-parses everything after `new-tab` until `;` as the command
    # for that tab. -- in PowerShell stops -File/-Command argument capture so
    # the launcher path lands as wt's command-to-run.
    Start-Process -FilePath $wt.Source -ArgumentList @(
        'new-tab', '--title', 'sutando-core', '--',
        $psExe.Source, '-NoExit', '-NoProfile', '-File', $launcher
    )
} else {
    Write-Host "Launching sutando-core in a new PowerShell window..."
    Start-Process -FilePath $psExe.Source -ArgumentList @(
        '-NoExit', '-NoProfile', '-File', $launcher
    )
}

Start-Sleep -Seconds 2
$pids = Get-CorePids
if ($pids) {
    Write-Host "  + sutando-core started (pid $($pids -join ', '))"
} else {
    Write-Host "  ~ Could not confirm sutando-core started; check the new window for errors."
}
