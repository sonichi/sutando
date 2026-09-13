#!/usr/bin/env pwsh
# Sutando notification on Windows - PowerShell twin of src/notify.sh.
# Sends a balloon-tip notification + optional Discord DM.
#
# Usage:
#   pwsh -File src/notify.ps1 "your message"

param([Parameter(Mandatory=$true, Position=0)][string]$Message)

if (-not $Message) {
    Write-Host "Usage: pwsh -File src/notify.ps1 'message'"
    exit 1
}

$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Resolve workspace - same shape as workspace_default.py
. "$PSScriptRoot/workspace_default.ps1"
$WORKSPACE = Resolve-SutandoWorkspace

# 1. Voice agent (proactive message) - if voice agent is up, drop a file under
#    results/ so the next poll picks it up.
try {
    $resp = Invoke-WebRequest -Uri 'http://localhost:9900' -Method Get -TimeoutSec 1 -UseBasicParsing -ErrorAction SilentlyContinue
} catch {
    $resp = $_.Exception.Response
}
if ($resp -and $resp.StatusCode -eq 426) {
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $resultsDir = Join-Path $WORKSPACE 'results'
    New-Item -ItemType Directory -Force -Path $resultsDir | Out-Null
    Set-Content -Path (Join-Path $resultsDir "proactive-$ts.txt") -Value $Message -NoNewline
}

# 2. Discord DM - read token from ~/.claude/channels/discord/.env (if present)
$discEnv = Join-Path $HOME '.claude\channels\discord\.env'
if (Test-Path $discEnv) {
    $discToken = (Get-Content $discEnv | Where-Object { $_ -match 'DISCORD_BOT_TOKEN=' } | Select-Object -First 1) -replace 'DISCORD_BOT_TOKEN=', '' -replace '"', '' -replace "'", ''
    $accessJson = Join-Path $HOME '.claude\channels\discord\access.json'
    if ($discToken -and (Test-Path $accessJson)) {
        try {
            $access = Get-Content $accessJson | ConvertFrom-Json
            $userId = $access.allowFrom[0]
            if ($userId) {
                $headers = @{
                    'Authorization' = "Bot $discToken"
                    'Content-Type' = 'application/json'
                    'User-Agent' = 'DiscordBot (https://github.com/sonichi/sutando, 1.0)'
                }
                $dmBody = @{ recipient_id = $userId } | ConvertTo-Json -Compress
                $dmResp = Invoke-RestMethod -Uri 'https://discord.com/api/v10/users/@me/channels' -Method Post -Headers $headers -Body $dmBody -TimeoutSec 5
                if ($dmResp.id) {
                    $msgBody = @{ content = $Message } | ConvertTo-Json -Compress
                    Invoke-RestMethod -Uri "https://discord.com/api/v10/channels/$($dmResp.id)/messages" -Method Post -Headers $headers -Body $msgBody -TimeoutSec 5 | Out-Null
                }
            }
        } catch {}
    }
}

# 3. Windows toast/balloon notification - same path as src/platform.py
try {
    $safeMsg = $Message.Replace("'", "''")
    Add-Type -AssemblyName System.Windows.Forms
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.BalloonTipTitle = 'Sutando'
    $n.BalloonTipText = $Message
    $n.Visible = $true
    $n.ShowBalloonTip(3000)
    Start-Sleep -Milliseconds 3500
    $n.Dispose()
} catch {}
