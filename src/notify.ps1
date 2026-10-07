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

# 2. Discord DM — shared owner resolution, allowlist, chunking, and REST client.
# Pass text through the environment so Windows argument quoting cannot alter it.
try {
    . (Join-Path $REPO 'scripts/python-binary.ps1')
    $python = Resolve-SutandoPython
    $pythonArgs = @('-')
    if ($python -eq 'py') { $pythonArgs = @('-3') + $pythonArgs }
    $env:SUTANDO_REPO_DIR = $REPO
    $env:SUTANDO_NOTIFY_MESSAGE = $Message
    $env:PYTHONIOENCODING = 'utf-8'
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $sendDm = @'
import importlib.util
import os
import sys
repo = os.environ["SUTANDO_REPO_DIR"]
sys.path.insert(0, os.path.join(repo, "src"))
spec = importlib.util.spec_from_file_location("dm_result_notify", os.path.join(repo, "src", "dm-result.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.exit(0 if mod.send_dm(os.environ["SUTANDO_NOTIFY_MESSAGE"]) else 1)
'@
    $sendDm | & $python @pythonArgs
} catch {}

# 3. Windows toast/balloon notification - same path as src/platform.py
try {
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
