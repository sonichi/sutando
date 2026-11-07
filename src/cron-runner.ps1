#!/usr/bin/env pwsh
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Python)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repo
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$workspace = & $Python -c 'import sys; sys.path.insert(0, sys.argv[1]); from workspace_default import resolve_workspace; print(resolve_workspace())' $PSScriptRoot
if ($LASTEXITCODE -ne 0 -or -not $workspace) { throw 'Cannot resolve schedule storage.' }
$workspace = $workspace.Trim()
$logs = Join-Path $workspace 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
& $Python (Join-Path $PSScriptRoot 'cron-runner.py') *>> (Join-Path $logs 'cron-runner.log')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
