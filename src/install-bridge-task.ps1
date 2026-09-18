#!/usr/bin/env pwsh
# Register (or remove) a Windows Scheduled Task that keeps a Sutando bridge
# online across logouts and reboots.
#
# The task runs src/bridge-supervisor.ps1 at logon. The supervisor itself does
# the restart-on-crash loop; the scheduled task is what makes it come back after
# a reboot or logout. Two layers: supervisor = crash recovery, task = boot
# recovery.
#
# Usage:
#   pwsh -File src/install-bridge-task.ps1                      # install discord
#   pwsh -File src/install-bridge-task.ps1 -Bridge discord-bridge
#   pwsh -File src/install-bridge-task.ps1 -Uninstall          # remove discord task
#
# Runs under the current user (no admin needed for a logon-triggered per-user
# task). The task starts whether or not this Claude session is open.

[CmdletBinding()]
param(
    [string]$Bridge = 'discord-bridge',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = "Sutando $Bridge"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed scheduled task '$taskName'."
    } else {
        Write-Host "No scheduled task '$taskName' to remove."
    }
    exit 0
}

$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwsh) { $pwsh = (Get-Command powershell).Source }
$supervisor = Join-Path $REPO 'src\bridge-supervisor.ps1'

$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$supervisor`" -Bridge $Bridge" `
    -WorkingDirectory $REPO

# Trigger at user logon. AtStartup would need the task to run as SYSTEM, which
# wouldn't see the user's HOME / conda env — logon keeps it in the user context.
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Settings: restart the task itself if it ever stops, allow it to run
# indefinitely, and don't stop it on battery (laptops).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Keeps the Sutando $Bridge online (restart-on-crash supervisor, starts at logon)." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$taskName'."
Write-Host "  Runs: $supervisor -Bridge $Bridge"
Write-Host "  Trigger: at logon; restarts on failure."
Write-Host ""
Write-Host "Start it now without waiting for a re-logon:"
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
