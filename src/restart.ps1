#!/usr/bin/env pwsh
# Sutando restart on Windows - PowerShell twin of src/restart.sh.
# Stops all background services, then re-runs startup.ps1.
#
# Usage:
#   pwsh -File src/restart.ps1
#   pwsh -File src/restart.ps1 -StopOnly

[CmdletBinding()]
param([switch]$StopOnly)

$ErrorActionPreference = 'Continue'

$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Write-Host "Stopping Sutando services..."

$patterns = @(
    'voice-agent',
    'web-client.ts',
    'dashboard.py',
    'agent-api.py',
    'screen-capture-server',
    'telegram-bridge',
    'discord-bridge',
    'slack-bridge',
    'conversation-server',
    'credential-proxy',
    'watch-tasks-stream',
    'task-dispatcher'
)

foreach ($pat in $patterns) {
    $needle = $pat.ToLower()
    try {
        Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine.ToLower().Contains($needle)
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

# Core agent (Claude Code) - matched by `claude --name sutando-core` rather
# than a bare 'claude' substring so we don't kill the user's other Claude Code
# sessions (e.g. the one running this script via Claude Code itself).
try {
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'claude' -and
        $_.CommandLine -match 'sutando-core'
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
} catch {}

Write-Host "  All services stopped"

if ($StopOnly) {
    Write-Host "Done. Run 'pwsh -File src/restart.ps1' (without -StopOnly) to restart."
    exit 0
}

Start-Sleep -Seconds 2

Write-Host "Starting..."
& pwsh -File (Join-Path $REPO 'src\startup.ps1')
