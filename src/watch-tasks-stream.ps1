#!/usr/bin/env pwsh
# Sutando task watcher for Windows - PowerShell twin of src/watch-tasks-stream.sh.
#
# Watches <workspace>/tasks/ and emits "TASK_FILE: <name>" per new file
# to STDOUT, line-buffered, so the core Claude Code session can stream it via
# Bash(run_in_background: true) + TaskOutput.
#
# Implementation note: a previous version used Register-ObjectEvent + an
# -Action script block. That fires in a separate runspace - Write-Output from
# inside the action lands in that runspace's output stream, not the parent
# script's stdout, so the core agent never saw any events. This version uses
# WaitForChanged in a foreground loop instead, which keeps emissions on the
# script's own stdout pipeline.
#
# Usage:
#   pwsh -File src/watch-tasks-stream.ps1

# Force unbuffered stdout. Without this, lines can sit in the .NET output
# buffer for seconds, which defeats the whole "streaming" point.
[Console]::Out.NewLine = "`n"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

. "$PSScriptRoot/workspace_default.ps1"
$WORKSPACE = Resolve-SutandoWorkspace

$TASKS = Join-Path $WORKSPACE 'tasks'
New-Item -ItemType Directory -Force -Path $TASKS | Out-Null

# Write a PID sentinel so startup.ps1 / task-bridge.ts can detect a stale watcher.
$pidFile = Join-Path $WORKSPACE 'state\watch-tasks-stream.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null
Set-Content -Path $pidFile -Value $PID -NoNewline

# Track files we've already announced so the initial sweep + restart doesn't
# re-emit the same file. The set is keyed by basename.
$seen = New-Object 'System.Collections.Generic.HashSet[string]'

function Emit-IfNew($name) {
    if (-not $name) { return }
    if (-not ($name -like 'task-*.txt')) { return }
    if (-not $seen.Add($name)) { return }   # Add returns false if already present
    [Console]::Out.WriteLine("TASK_FILE: $name")
    [Console]::Out.Flush()
}

# Emit any tasks already on disk before we start watching - matches the macOS
# behavior where startup.sh races vs. tasks landing during boot.
Get-ChildItem -Path $TASKS -File -Filter 'task-*.txt' -ErrorAction SilentlyContinue | ForEach-Object {
    Emit-IfNew $_.Name
}

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $TASKS
$watcher.Filter = 'task-*.txt'
$watcher.IncludeSubdirectories = $false
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::CreationTime
# EnableRaisingEvents is implicit for WaitForChanged; no need to set it.

try {
    while ($true) {
        # 2s timeout means we wake up periodically to rescan the directory.
        # That guards against the rare missed event (e.g. file created during
        # the FSW's internal buffer flush) without leaning entirely on polling.
        $r = $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::Created -bor [System.IO.WatcherChangeTypes]::Renamed -bor [System.IO.WatcherChangeTypes]::Changed, 2000)
        if (-not $r.TimedOut -and $r.Name) {
            Emit-IfNew $r.Name
        }
        # Backstop directory sweep. Cheap (one stat per file) and covers the
        # case where the watcher missed an event during a burst.
        Get-ChildItem -Path $TASKS -File -Filter 'task-*.txt' -ErrorAction SilentlyContinue | ForEach-Object {
            Emit-IfNew $_.Name
        }
    }
} finally {
    $watcher.Dispose()
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
}
