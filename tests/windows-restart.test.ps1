#!/usr/bin/env pwsh
# Drives src/restart.ps1 against synthetic process tables; no real process is stopped.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$restart = Join-Path $repo 'src/restart.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('sutando restart ' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
$previousWorkspace = $env:SUTANDO_WORKSPACE
$previousTestMode = $env:SUTANDO_TEST_MODE
$env:SUTANDO_WORKSPACE = $temp
$env:SUTANDO_TEST_MODE = '1'

# Mocks run inside restart.ps1's script scope, so shared state lives in one reference object.
$RestartTest = @{
    Table = @()
    Kills = [Collections.Generic.List[string]]::new()
    Launches = [Collections.Generic.List[object]]::new()
    Startups = 0
}
function Get-CimInstance { [CmdletBinding()] param([Parameter(Position = 0)]$ClassName) $RestartTest.Table }
function taskkill { $RestartTest.Kills.Add(($args -join ' ')) }
function Wait-Process { [CmdletBinding()] param($Id, $Timeout) }
function Start-Sleep { param($Seconds) }
function pwsh { $RestartTest.Startups++ }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    $RestartTest.Launches.Add([pscustomobject]@{ ArgumentList = $ArgumentList; Log = $RedirectStandardOutput })
}

function Row([int]$id, [int]$parent, [string]$commandLine) {
    [pscustomobject]@{ ProcessId = $id; ParentProcessId = $parent; CommandLine = $commandLine }
}

function Assert-Kills($name, $table, $restartArgs, $expected) {
    $RestartTest.Table = $table
    $RestartTest.Kills.Clear()
    $RestartTest.Startups = 0
    & $restart @restartArgs | Out-Null
    $actual = @($RestartTest.Kills | Sort-Object)
    $wanted = @($expected | Sort-Object)
    if (($actual -join '|') -cne ($wanted -join '|')) {
        throw "${name}: expected kills [$($wanted -join '; ')] but got [$($actual -join '; ')]"
    }
}

try {
    $self = $PID
    $services = @(
        (Row 20 1 'node tsx C:\sutando\src\web-client.ts'),
        (Row 40 1 'claude --resume unrelated-session'),
        (Row 41 1 $null)
    )

    # Chat restart: task-dispatcher -> claude --print -> bash -> restart.ps1.
    $table = $services + @(
        (Row 10 1 'pwsh -NoProfile -File C:\sutando\src\task-dispatcher.ps1'),
        (Row 11 10 'claude --print --output-format json --resume abc'),
        (Row 12 11 'bash -c "pwsh -File src/restart.ps1"'),
        (Row $self 12 'pwsh -File C:\sutando\src\restart.ps1 -Detached'),
        (Row 30 5 'claude --name sutando-core --dangerously-skip-permissions')
    )
    Assert-Kills 'dispatcher ancestor' $table @{ Detached = $true } @(
        '/PID 10 /F', '/PID 20 /T /F', '/PID 30 /T /F')
    if ($RestartTest.Startups -ne 1) { throw 'detached restart did not run startup exactly once' }

    # Core restart: sutando-core -> restart.ps1 (StopOnly never relaunches).
    $table = $services + @(
        (Row 10 1 'pwsh -NoProfile -File C:\sutando\src\task-dispatcher.ps1'),
        (Row 30 5 'claude --name sutando-core --dangerously-skip-permissions'),
        (Row 31 30 'pwsh -File C:\sutando\src\stop.ps1'),
        (Row $self 31 'pwsh -File C:\sutando\src\restart.ps1 -StopOnly')
    )
    Assert-Kills 'core ancestor' $table @{ StopOnly = $true } @('/PID 10 /T /F', '/PID 20 /T /F', '/PID 30 /F')
    if ($RestartTest.Startups -ne 0 -or $RestartTest.Launches.Count -ne 0) { throw 'StopOnly started or relaunched' }

    # A detached instance whose launcher exited owns no service ancestor.
    $table = $services + @(
        (Row 10 1 'pwsh -NoProfile -File C:\sutando\src\task-dispatcher.ps1'),
        (Row $self 99999 'pwsh -File restart.ps1 -Detached')
    )
    Assert-Kills 'orphan' $table @{ Detached = $true } @('/PID 10 /T /F', '/PID 20 /T /F')

    # Recycled parent ids can form a cycle; the ancestor walk must still terminate.
    $cycle = $services + @(
        (Row 60 $self 'pwsh -File C:\sutando\src\watch-tasks-stream.ps1'),
        (Row $self 60 'pwsh -File restart.ps1 -Detached')
    )
    Assert-Kills 'cycle' $cycle @{ Detached = $true } @('/PID 20 /T /F', '/PID 60 /F')

    # A plain restart only relaunches itself detached; the stop happens in that instance.
    Assert-Kills 'launcher' $table @{} @()
    if ($RestartTest.Launches.Count -ne 1 -or $RestartTest.Startups -ne 0) { throw 'restart did not relaunch detached exactly once' }
    . (Join-Path $repo 'scripts/native-arguments.ps1')
    $expectedArgs = ConvertTo-NativeArgumentString @('-NoProfile', '-File', $restart, '-Detached', '-LauncherPid', $PID)
    if ($RestartTest.Launches[0].ArgumentList -cne $expectedArgs -or
        -not $RestartTest.Launches[0].Log.EndsWith((Join-Path 'logs' 'restart.log'))) {
        throw "detached relaunch changed: $($RestartTest.Launches[0] | ConvertTo-Json -Compress)"
    }
    Write-Output 'Windows restart: 4 stop plans and the detached relaunch passed.'
} finally {
    $env:SUTANDO_WORKSPACE = $previousWorkspace
    $env:SUTANDO_TEST_MODE = $previousTestMode
    Remove-Item -Recurse -Force $temp
}
