#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$ValidateOnly,
    [string]$ReconcileScript
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = 'Sutando Cron Runner'
$runner = Join-Path $PSScriptRoot 'cron-runner.ps1'
. (Join-Path $repo 'scripts/native-arguments.ps1')

function Invoke-CronReconciliation($Python, $Reconciler) {
    & $Python $Reconciler
    if ($LASTEXITCODE -ne 0) { throw 'Cron ownership reconciliation failed; schedules were not activated.' }
}

function Install-SutandoCronTask {
    param([string]$Name, [string]$Runner, [string]$Repo, [string]$Python, [string]$Reconciler)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        $owned = @($existing.Actions | Where-Object {
            $_.Arguments -and ($_.Arguments.Contains($Runner) -or
                $_.Arguments.Contains((Join-Path $Repo 'src\cron-runner.py')))
        })
        if ($existing.Actions.Count -ne 1 -or $owned.Count -ne 1) {
            throw "Scheduled task '$Name' belongs to another installation; not replacing it."
        }
    }
    $powershell = (Get-Process -Id $PID).Path
    $arguments = ConvertTo-NativeArgumentString @('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-File', $Runner, '-Python', $Python)
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $Repo
    $timer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $logon = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger @($timer, $logon) `
        -Settings $settings -Principal $principal -Description 'Runs configured Sutando schedules every minute without expiry (while signed in).' -Force | Out-Null
    if ($Reconciler) {
        Invoke-CronReconciliation $Python $Reconciler
    }
    Start-ScheduledTask -TaskName $Name
}

if ($ValidateOnly) {
    . (Join-Path $repo 'scripts/python-binary.ps1')
    Resolve-SutandoPython | Out-Null
    if (-not (Test-Path -LiteralPath $runner)) { throw "Missing runner: $runner" }
    Write-Output 'Windows cron runner validation passed.'
} elseif ($Status) {
    Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Get-ScheduledTaskInfo -TaskName $taskName
} elseif ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if ($existing.Actions.Count -ne 1 -or -not ($existing.Actions[0].Arguments.Contains($runner) -or
        $existing.Actions[0].Arguments.Contains((Join-Path $repo 'src\cron-runner.py')))) {
        throw "Scheduled task '$taskName' belongs to another installation; not removing it."
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
} else {
    . (Join-Path $repo 'scripts/python-binary.ps1')
    $python = Resolve-SutandoPython
    $prefix = @()
    if ($python -eq 'py') { $prefix = @('-3') }
    $pythonPath = & $python @prefix -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not $pythonPath) { throw 'Cannot resolve Python executable.' }
    Install-SutandoCronTask -Name $taskName -Runner $runner -Repo $repo -Python $pythonPath.Trim() -Reconciler $ReconcileScript
    Write-Output "Registered $taskName (every minute and at logon; no expiry)."
}
