#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { Write-Output 'SKIP: native Windows task watcher'; exit 0 }

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$fixture = Join-Path $tempRoot ('sutando-watcher-' + [guid]::NewGuid().ToString('N'))
$fixtureSource = Join-Path $fixture 'checkout\src'
$workspace = Join-Path $fixture 'workspace 空间'
$tasks = Join-Path $workspace 'tasks'
$process = $null

function Assert-Event([string]$expected) {
    $line = $process.StandardOutput.ReadLineAsync()
    if (-not $line.Wait(10000)) { throw "Watcher did not emit $expected within 10 seconds." }
    if ($line.Result -cne "TASK_FILE: $expected") { throw "Unexpected watcher output: $($line.Result)" }
}

try {
    New-Item -ItemType Directory -Force -Path $fixtureSource, $tasks | Out-Null
    foreach ($name in 'watch-tasks-stream.ps1', 'workspace_default.ps1', 'sutando_config.py') {
        Copy-Item -LiteralPath (Join-Path $repo "src\$name") -Destination $fixtureSource
    }
    @{ workspace = @{ path = $workspace } } | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $fixture 'checkout\sutando.config.json') -Encoding utf8NoBOM
    Set-Content -LiteralPath (Join-Path $tasks 'task-existing.txt') -Value 'synthetic initial task'

    $start = [Diagnostics.ProcessStartInfo]::new((Get-Command pwsh -ErrorAction Stop).Source)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $start.Environment.Clear()
    foreach ($key in 'PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP') {
        $value = [Environment]::GetEnvironmentVariable($key, 'Process')
        if ($null -ne $value) { $start.Environment[$key] = $value }
    }
    $start.Environment['USERPROFILE'] = $fixture
    $start.Environment['SUTANDO_TELEMETRY'] = '0'
    $start.Environment['DO_NOT_TRACK'] = '1'
    foreach ($argument in '-NoProfile', '-NonInteractive', '-File', (Join-Path $fixtureSource 'watch-tasks-stream.ps1')) {
        $start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::Start($start)
    $stderr = $process.StandardError.ReadToEndAsync()
    Assert-Event 'task-existing.txt'
    $pidFile = Join-Path $workspace 'state\watch-tasks-stream.pid'
    if ([int](Get-Content -LiteralPath $pidFile -Raw) -ne $process.Id) { throw 'Watcher sentinel has the wrong owner.' }

    Set-Content -LiteralPath (Join-Path $tasks 'task-created.txt') -Value 'synthetic created task'
    Assert-Event 'task-created.txt'
    $pending = Join-Path $tasks 'pending.tmp'
    Set-Content -LiteralPath $pending -Value 'synthetic atomic publication'
    Move-Item -LiteralPath $pending -Destination (Join-Path $tasks 'task-renamed.txt')
    Assert-Event 'task-renamed.txt'

    Add-Content -LiteralPath (Join-Path $tasks 'task-created.txt') -Value 'second write'
    Set-Content -LiteralPath (Join-Path $tasks 'ignored.txt') -Value 'not a task'
    $extra = $process.StandardOutput.ReadLineAsync()
    if ($extra.Wait(3000)) { throw "Duplicate or non-task event: $($extra.Result)" }
    if ($process.HasExited) { throw "Watcher exited unexpectedly: $($stderr.Result)" }
    Write-Output 'Windows watcher: initial, created and renamed tasks emitted once; changes and non-tasks ignored.'
} finally {
    if ($process) {
        if (-not $process.HasExited) { $process.Kill($true) }
        $process.WaitForExit()
        $process.Dispose()
    }
    $resolvedFixture = [IO.Path]::GetFullPath($fixture)
    if (-not $resolvedFixture.StartsWith($tempRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside the temporary directory.'
    }
    if (Test-Path -LiteralPath $resolvedFixture) { Remove-Item -LiteralPath $resolvedFixture -Recurse -Force }
}
