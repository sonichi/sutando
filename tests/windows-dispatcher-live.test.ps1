#!/usr/bin/env pwsh
# Real Windows integration path with a deterministic Claude shim:
# FileSystemWatcher -> atomic claim -> owner result -> archive -> tier refusal.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspace = Join-Path $env:TEMP "sutando-dispatcher-live-$PID"
$shimDir = Join-Path $workspace 'bin'
$errorModeFile = Join-Path $workspace 'fake-error-mode'
$staleModeFile = Join-Path $workspace 'fake-stale-once'
$dispatcherPid = 0
$oldPath = $env:PATH
$oldTestMode = $env:SUTANDO_TEST_MODE
$oldWorkspace = $env:SUTANDO_WORKSPACE

function Stop-ProcessTree([int]$rootPid) {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $queue = @($rootPid)
    $ordered = @()
    while ($queue.Count) {
        $current = $queue[0]
        if ($queue.Count -gt 1) { $queue = @($queue[1..($queue.Count - 1)]) }
        else { $queue = @() }
        $ordered += $current
        $queue += @($all | Where-Object ParentProcessId -eq $current | ForEach-Object ProcessId)
    }
    [array]::Reverse($ordered)
    foreach ($processId in $ordered) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-ForPath([string]$path, [int]$seconds = 30) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $path) { return }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out waiting for $path"
}

function Write-Task([string]$id, [string]$body, [string]$tier) {
    $content = @(
        "id: $id"
        "timestamp: $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
        "task: $body"
        "source: chat"
        "channel_id: windows-ci"
        "user_id: windows-ci"
        "access_tier: $tier"
        "priority: normal"
    ) -join "`n"
    $path = Join-Path $workspace "tasks\$id.txt"
    [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
}

try {
    New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
    $shim = @'
@echo off
if exist "%SUTANDO_FAKE_STALE_FILE%" (
  echo %* | findstr /C:"--resume" >nul
  if not errorlevel 1 (
    del "%SUTANDO_FAKE_STALE_FILE%"
    echo No conversation found with session ID: stale-windows-session 1>&2
    exit /b 1
  )
)
if exist "%SUTANDO_FAKE_ERROR_FILE%" (
  echo {"type":"result","subtype":"success","is_error":true,"api_error_status":500,"result":"API Error: 500 fake gateway","session_id":"windows-ci-session"}
  exit /b 1
)
echo {"type":"result","subtype":"success","is_error":false,"result":"WINDOWS_OWNER_OK","session_id":"windows-ci-session"}
exit /b 0
'@
    Set-Content -Path (Join-Path $shimDir 'claude.cmd') -Value $shim -Encoding ascii
    # Isolate command lookup so the real user-level claude.exe cannot outrank
    # the shim merely because the dispatcher prefers a native executable.
    $pwshDir = Split-Path (Get-Command pwsh -ErrorAction Stop).Source -Parent
    $pythonDir = Split-Path (Get-Command python -ErrorAction Stop).Source -Parent
    $env:PATH = "$shimDir;$pwshDir;$pythonDir;$env:SystemRoot\System32"
    $env:SUTANDO_TEST_MODE = '1'
    $env:SUTANDO_WORKSPACE = $workspace
    $env:SUTANDO_FAKE_ERROR_FILE = $errorModeFile
    $env:SUTANDO_FAKE_STALE_FILE = $staleModeFile

    New-Item -ItemType Directory -Force -Path (Join-Path $workspace 'state') | Out-Null
    Set-Content -Path (Join-Path $workspace 'state\dispatcher-sessions.json') `
        -Value '{"windows-ci":"stale-windows-session"}' -Encoding ascii
    New-Item -ItemType File -Path $staleModeFile | Out-Null

    & pwsh -NoProfile -File (Join-Path $repo 'src\task-dispatcher.ps1') -Background
    if ($LASTEXITCODE -ne 0) { throw "dispatcher launch exited $LASTEXITCODE" }

    $pidFile = Join-Path $workspace 'state\task-dispatcher.pid'
    Wait-ForPath $pidFile
    $dispatcherPid = [int](Get-Content $pidFile)
    if (-not (Get-Process -Id $dispatcherPid -ErrorAction SilentlyContinue)) {
        throw "dispatcher PID $dispatcherPid is not alive"
    }

    $ownerId = "task-windows-owner-$PID"
    Write-Task $ownerId 'Return the owner integration marker.' 'owner'
    $ownerResult = Join-Path $workspace "results\$ownerId.txt"
    $ownerArchive = Join-Path $workspace "tasks\archive\$ownerId.txt"
    Wait-ForPath $ownerResult
    Wait-ForPath $ownerArchive
    $ownerBody = (Get-Content $ownerResult -Raw).Trim()
    if ($ownerBody -ne 'WINDOWS_OWNER_OK') {
        throw "unexpected owner result: $ownerBody"
    }
    $sessionMap = Get-Content (Join-Path $workspace 'state\dispatcher-sessions.json') -Raw |
        ConvertFrom-Json
    if ($sessionMap.'windows-ci' -eq 'stale-windows-session') {
        throw 'stale session mapping was not rotated'
    }

    $nonOwnerId = "task-windows-team-$PID"
    Write-Task $nonOwnerId 'WINDOWS_NONOWNER_BODY_MUST_NOT_RUN' 'team'
    $nonOwnerResult = Join-Path $workspace "results\$nonOwnerId.txt"
    $nonOwnerArchive = Join-Path $workspace "tasks\archive\$nonOwnerId.txt"
    Wait-ForPath $nonOwnerResult
    Wait-ForPath $nonOwnerArchive
    $nonOwnerBody = (Get-Content $nonOwnerResult -Raw).Trim()
    if ($nonOwnerBody -ne 'Sandbox unavailable; refusing non-owner task.') {
        throw "unexpected non-owner result: $nonOwnerBody"
    }
    if ($nonOwnerBody -match 'WINDOWS_NONOWNER_BODY_MUST_NOT_RUN') {
        throw 'non-owner task body leaked into the result'
    }

    New-Item -ItemType File -Path $errorModeFile | Out-Null
    $errorId = "task-windows-error-$PID"
    Write-Task $errorId 'Return a structured gateway error.' 'owner'
    $errorResult = Join-Path $workspace "results\$errorId.txt"
    $errorArchive = Join-Path $workspace "tasks\archive\$errorId.txt"
    Wait-ForPath $errorResult
    Wait-ForPath $errorArchive
    $errorBody = (Get-Content $errorResult -Raw).Trim()
    if ($errorBody -ne 'task-dispatcher: claude error: API Error: 500 fake gateway') {
        throw "structured error was not preserved: $errorBody"
    }

    [pscustomobject]@{
        dispatcher_pid = $dispatcherPid
        dispatcher_alive = $true
        owner_result = $ownerBody
        owner_archived = (Test-Path $ownerArchive)
        stale_session_recovered = ($sessionMap.'windows-ci' -ne 'stale-windows-session')
        non_owner_result = $nonOwnerBody
        non_owner_archived = (Test-Path $nonOwnerArchive)
        structured_error = $errorBody
        error_archived = (Test-Path $errorArchive)
    } | ConvertTo-Json -Compress
} finally {
    if ($dispatcherPid) {
        Stop-ProcessTree $dispatcherPid
    }
    $env:PATH = $oldPath
    $env:SUTANDO_TEST_MODE = $oldTestMode
    $env:SUTANDO_WORKSPACE = $oldWorkspace
    Remove-Item Env:SUTANDO_FAKE_ERROR_FILE -ErrorAction SilentlyContinue
    Remove-Item Env:SUTANDO_FAKE_STALE_FILE -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    if (Test-Path $workspace) {
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            try {
                Remove-Item -Recurse -Force $workspace -ErrorAction Stop
                break
            } catch {
                if ($attempt -eq 9) { throw }
                Start-Sleep -Milliseconds 300
            }
        }
    }
}
