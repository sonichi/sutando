$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\src\windows-app-launcher.ps1')

function Assert-That([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-Fails([scriptblock]$Action, [string]$Message) {
    $failure = $null
    try { & $Action | Out-Null } catch { $failure = $_.Exception.Message }
    Assert-That ($failure -and $failure.Contains($Message)) "Expected '$Message', got '$failure'."
}

$exe = (Get-Process -Id $PID).Path
$script:shellSession = 8
function Get-Process {
    [CmdletBinding()]
    param([string]$Name, [int]$Id)
    [pscustomobject]@{ SessionId = if ($Id) { 7 } else { $script:shellSession } }
}
Assert-Fails { Assert-AppDesktop } 'No interactive desktop'
$script:shellSession = 7
Assert-AppDesktop
Remove-Item Function:\Get-Process

$script:entries = @([pscustomobject]@{ Name = 'Wanted'; AppID = 'package!Wanted' })
$script:catalog = [pscustomobject]@{ Item = 'registered-item'; ImagePath = $exe; Arguments = '' }
function Get-StartApps { $script:entries }
function Get-AppCatalogEntry([string]$AppId) {
    Assert-That ($AppId -ceq 'package!Wanted') 'Catalog lookup must use the exact registered app ID.'
    $script:catalog
}

$target = Resolve-AppTarget 'Wanted'
Assert-That ($target.AppId -ceq 'package!Wanted' -and $target.ImagePath -eq $exe) 'Registered identity was lost.'
Assert-That ((Resolve-AppTarget 'wanted.exe').AppId -ceq 'package!Wanted') 'Executable alias should resolve its registration.'
$script:catalog.Arguments = '--other-experience'
Assert-That (-not (Resolve-AppTarget 'Wanted').ImagePath) 'An argument-bearing shortcut must not match every window of its launcher.'
$script:catalog.Arguments = ''
$script:entries += $script:entries[0]
Assert-Fails { Resolve-AppTarget 'Wanted' } 'More than one'
$script:entries = @()
Assert-Fails { Resolve-AppTarget 'Want*' } 'wildcards'
Assert-Fails { Resolve-AppTarget '' } 'Pass an installed'
Assert-Fails { Resolve-AppTarget 'sutando-app-that-does-not-exist-762190' } 'App not found'
Assert-That ((Resolve-AppTarget $exe).ImagePath -eq $exe) 'An exact executable path must remain supported.'

function New-Window([string]$AppId = 'package!Wanted', [string]$Image = '') {
    [pscustomobject]@{
        Handle = 101; ProcessId = 42; AppId = ''; Visible = $true; Minimized = $false
        Title = 'An unrelated document title'
        Owners = @([pscustomobject]@{ ProcessId = 42; AppId = $AppId; ImagePath = $Image })
    }
}

$target = [pscustomobject]@{ Name = 'Wanted'; AppId = 'package!Wanted'; ImagePath = $exe }
$fake = New-Window -AppId 'package!Other'
$fake.Title = 'Wanted'
Assert-That (-not (Test-AppWindow $fake $target)) 'A matching title must never identify an app.'
$fake.Owners[0].ImagePath = $exe
Assert-That (-not (Test-AppWindow $fake $target)) 'A conflicting process app ID must outrank executable fallback.'
$fake = New-Window -AppId 'package!Wanted'
$fake.AppId = 'package!Other'
Assert-That (-not (Test-AppWindow $fake $target)) 'A window app ID must outrank its process.'
$fake.AppId = 'package!Wanted'
$fake.Owners[0].AppId = 'package!Other'
Assert-That (Test-AppWindow $fake $target) 'A matching window app ID must outrank its process.'
$fake = New-Window -AppId '' -Image $exe
Assert-That (Test-AppWindow $fake $target) 'Exact executable identity should identify desktop apps.'
$fake.Owners[0].ImagePath = ''
Assert-That (-not (Test-AppWindow $fake $target)) 'Uninspectable owners cannot identify an app.'
$fake = New-Window
$fake.Visible = $false
Assert-That (-not (Test-AppWindow $fake $target)) 'Invisible windows cannot satisfy app switching.'

function Reset-Scenario {
    $script:windows = @()
    $script:foreground = $null
    $script:launches = 0
    $script:focuses = 0
    $script:restores = 0
    $script:allowFocus = $true
    $script:createWindow = $true
    $script:tick = [DateTime]::UtcNow
}
function Assert-AppDesktop {}
function Resolve-AppTarget([string]$Name) { $target }
function Get-AppWindows { $script:windows }
function Get-AppForeground { $script:foreground }
function Start-Sleep {}
function Get-AppClock {
    $script:tick = $script:tick.AddMilliseconds(200)
    $script:tick
}
function Start-AppTarget($Target) {
    $script:launches++
    if ($script:createWindow) { $script:windows = @(New-Window) }
}
function Restore-AppWindow($Window) {
    $script:restores++
    $Window.Minimized = $false
}
function Request-AppForeground($Window) {
    $script:focuses++
    if ($script:allowFocus) { $script:foreground = $Window }
}

Reset-Scenario
$script:foreground = New-Window
$result = Invoke-WindowsAppSwitch 'Wanted' 1
Assert-That ($result.foreground_verified -and $script:focuses -eq 0 -and $script:launches -eq 0) 'Already foreground must not relaunch or disturb other windows.'

Reset-Scenario
$script:windows = @(New-Window)
$script:windows[0].Minimized = $true
$result = Invoke-WindowsAppSwitch 'Wanted' 1
Assert-That ($result.status -eq 'switched' -and $script:restores -eq 1 -and $script:focuses -eq 1 -and $script:launches -eq 0) 'A minimized existing window must restore and focus without relaunch.'

Reset-Scenario
$result = Invoke-WindowsAppSwitch 'Wanted' 1
Assert-That ($script:launches -eq 1 -and $result.window_process_id -eq 42 -and $result.window_handle -eq 101) 'Launch must re-identify the actual window, not rely on the launch PID.'

Reset-Scenario
$script:windows = @(New-Window)
$script:allowFocus = $false
$script:foreground = New-Window -AppId 'package!Other'
Assert-Fails { Invoke-WindowsAppSwitch 'Wanted' 1 } 'Windows did not allow foreground focus'
Assert-That ($script:launches -eq 0 -and $script:focuses -eq 1) 'Focus denial must not relaunch or repeatedly force focus.'

Reset-Scenario
$script:createWindow = $false
Assert-Fails { Invoke-WindowsAppSwitch 'Wanted' 1 } 'no window with its app identity'
Assert-That ($script:launches -eq 1 -and $script:focuses -eq 0) 'Unidentified launches cannot report focus.'

Write-Output 'PASS: app identification and foreground verification'
