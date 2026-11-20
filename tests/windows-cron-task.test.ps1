#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $repo 'scripts/native-arguments.ps1')
$installer = Join-Path $repo 'src/install-cron-runner-task.ps1'
$tree = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$null, [ref]$null)
$definition = $tree.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-SutandoCronTask'
}, $false)
. ([scriptblock]::Create($definition.Extent.Text))

$calls = [Collections.Generic.List[string]]::new()
$existing = $null
function Get-ScheduledTask { param($TaskName, $ErrorAction) return $existing }
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    $script:action = @{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
    return $action
}
function New-ScheduledTaskTrigger {
    param([switch]$Once, $At, $RepetitionInterval, [switch]$AtLogOn, $User)
    $script:triggers += ,@{ Once = [bool]$Once; Interval = $RepetitionInterval; AtLogOn = [bool]$AtLogOn }
    return @{ Enabled = $true }
}
function New-ScheduledTaskSettingsSet {
    param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable, $MultipleInstances, $ExecutionTimeLimit)
    if (-not $StartWhenAvailable -or $MultipleInstances -ne 'IgnoreNew' -or
        -not $AllowStartIfOnBatteries -or -not $DontStopIfGoingOnBatteries) { throw 'Unsafe task settings' }
    return @{}
}
function New-ScheduledTaskPrincipal { param($UserId, $LogonType) return @{ LogonType = $LogonType } }
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description, [switch]$Force)
    $calls.Add('register')
}
function Start-ScheduledTask { param($TaskName) $calls.Add('start') }
function Invoke-CronReconciliation($Python, $Reconciler) {
    & $Reconciler
    if ($LASTEXITCODE -ne 0) { throw 'Reconciliation failed' }
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('cron installer 空间 ' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $reconciler = Join-Path $temp 'reconcile fixture.ps1'
    Set-Content $reconciler '$calls.Add("reconcile"); $global:LASTEXITCODE = 0' -Encoding utf8
    $runner = Join-Path $temp 'cron runner.ps1'
    $python = Join-Path $temp 'python fixture.exe'
    $triggers = @()
    Install-SutandoCronTask -Name 'fixture' -Runner $runner -Repo $temp -Python $python -Reconciler $reconciler
    if (($calls -join ',') -ne 'register,reconcile,start') { throw "Wrong activation order: $calls" }
    if ($action.Arguments -notlike '*"-Python"*' -or -not $action.Arguments.Contains('"' + $python + '"') -or
        $action.WorkingDirectory -ne $temp) { throw 'Lost executable/path arguments' }
    if ($triggers.Count -ne 2 -or $triggers[0].Interval.TotalMinutes -ne 1 -or -not $triggers[1].AtLogOn) {
        throw 'Missing indefinite minute/logon triggers'
    }
    $existing = [pscustomobject]@{ Actions = @([pscustomobject]@{ Arguments = $action.Arguments }) }
    Install-SutandoCronTask -Name 'fixture' -Runner $runner -Repo $temp -Python $python -Reconciler $reconciler
    $existing = [pscustomobject]@{ Actions = @([pscustomobject]@{ Arguments = 'another installation' }) }
    $failed = $false
    try { Install-SutandoCronTask -Name 'fixture' -Runner $runner -Repo $temp -Python $python } catch { $failed = $true }
    if (-not $failed -or $calls.Count -ne 6) { throw 'Foreign task was replaced' }
    $existing = $null
    $calls.Clear()
    Set-Content $reconciler '$global:LASTEXITCODE = 9' -Encoding utf8
    $failed = $false
    try { Install-SutandoCronTask -Name 'fixture' -Runner $runner -Repo $temp -Python $python -Reconciler $reconciler } catch { $failed = $true }
    if (-not $failed -or ($calls -join ',') -ne 'register') { throw 'Started after reconciliation failure' }
    $startupTree = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $repo 'src/startup.ps1'), [ref]$null, [ref]$null)
    $startupDefinition = $startupTree.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-WindowsSchedules'
    }, $false)
    if (-not $startupDefinition) { throw 'Startup does not install Windows schedules' }
    . ([scriptblock]::Create($startupDefinition.Extent.Text))
    New-Item -ItemType Directory -Force -Path (Join-Path $temp 'src') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $temp 'skills/schedule-crons/scripts') | Out-Null
    Set-Content (Join-Path $temp 'skills/schedule-crons/scripts/reconcile_launchd.py') ''
    Set-Content (Join-Path $temp 'src/install-cron-runner-task.ps1') 'param($ReconcileScript); Set-Content (Join-Path $PSScriptRoot "received.txt") $ReconcileScript'
    Install-WindowsSchedules $temp
    $receivedReconciler = (Get-Content (Join-Path $temp 'src/received.txt') -Raw).Trim()
    if ($receivedReconciler -ne (Join-Path $temp 'skills/schedule-crons/scripts/reconcile_launchd.py')) {
        throw 'Startup did not delegate to the installer with the skill capability'
    }
    Write-Output 'PASS: Windows scheduler install, migration order, quoting, idempotence, foreign-task and failure guards'
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force
}
