#!/usr/bin/env pwsh
# Sutando restart on Windows - PowerShell twin of src/restart.sh.
# Stops all background services, then re-runs startup.ps1.
#
# Usage:
#   pwsh -File src/restart.ps1
#   pwsh -File src/restart.ps1 -StopOnly
#
# A full restart relaunches itself detached, because a chat request runs inside the services
# it stops; that run logs to <workspace>/logs/restart.log.

[CmdletBinding()]
param(
    [switch]$StopOnly,
    [switch]$Detached,
    [int]$LauncherPid = 0
)

$ErrorActionPreference = 'Continue'

$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Stop-SutandoProcessTree([int]$ProcessId) {
    & taskkill /PID $ProcessId /T /F 2>$null | Out-Null
}

function Stop-SutandoProcess([int]$ProcessId) {
    & taskkill /PID $ProcessId /F 2>$null | Out-Null
}

$script:SERVICE_PATTERNS = @(
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

function Get-AncestorProcessIds($processes, [int]$selfPid) {
    $parents = @{}
    foreach ($process in $processes) { $parents[[int]$process.ProcessId] = [int]$process.ParentProcessId }
    $ancestors = [Collections.Generic.HashSet[int]]::new()
    $current = $selfPid
    while ($parents.ContainsKey($current)) {
        $current = $parents[$current]
        if ($current -le 0 -or -not $ancestors.Add($current)) { break }
    }
    return , $ancestors
}

# Tree-killing an ancestor would kill this script too, so ancestors are stopped alone.
function Get-SutandoStopPlan($processes, [int]$selfPid) {
    $ancestors = Get-AncestorProcessIds $processes $selfPid
    $services = [Collections.Generic.List[object]]::new()
    $cores = [Collections.Generic.List[object]]::new()
    foreach ($process in $processes) {
        $processId = [int]$process.ProcessId
        $commandLine = [string]$process.CommandLine
        if ($processId -eq $selfPid -or -not $commandLine) { continue }
        $step = [pscustomobject]@{ ProcessId = $processId; Tree = -not $ancestors.Contains($processId) }
        $lower = $commandLine.ToLower()
        if (@($script:SERVICE_PATTERNS | Where-Object { $lower.Contains($_) }).Count) {
            $services.Add($step)
        } elseif ($commandLine -match 'claude' -and $commandLine -match 'sutando-core') {
            # Matched by name so the user's other Claude Code sessions survive.
            $cores.Add($step)
        }
    }
    return @($services) + @($cores)
}

if (-not $StopOnly -and -not $Detached) {
    . "$PSScriptRoot/workspace_default.ps1"
    . (Join-Path $REPO 'scripts/native-arguments.ps1')
    $logs = Join-Path (Resolve-SutandoWorkspace) 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $log = Join-Path $logs 'restart.log'
    # Redirected handles keep the detached child off the caller's stdout pipe.
    Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList (ConvertTo-NativeArgumentString @(
        '-NoProfile', '-File', $PSCommandPath, '-Detached', '-LauncherPid', $PID
    )) -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err" | Out-Null
    Write-Host "Sutando restart continues in the background. Log: $log"
    exit 0
}

if ($LauncherPid) {
    # Once the launcher exits, no stopped process tree reaches this instance.
    Wait-Process -Id $LauncherPid -Timeout 30 -ErrorAction SilentlyContinue
}

Write-Host "Stopping Sutando services..."

try {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    foreach ($step in Get-SutandoStopPlan $processes $PID) {
        if ($step.Tree) { Stop-SutandoProcessTree $step.ProcessId } else { Stop-SutandoProcess $step.ProcessId }
    }
} catch {
    Write-Host "  ~ could not enumerate processes: $_"
}

Write-Host "  All services stopped"

if ($StopOnly) {
    Write-Host "Done. Run 'pwsh -File src/restart.ps1' (without -StopOnly) to restart."
    exit 0
}

Start-Sleep -Seconds 2

Write-Host "Starting..."
& pwsh -File (Join-Path $REPO 'src\startup.ps1')
