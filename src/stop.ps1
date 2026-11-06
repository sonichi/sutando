#!/usr/bin/env pwsh
# Stop all Sutando services (shortcut for restart.ps1 -StopOnly).
# PowerShell twin of src/stop.sh.
#
# Usage:
#   pwsh -File src/stop.ps1

& pwsh -File (Join-Path $PSScriptRoot 'restart.ps1') -StopOnly
exit $LASTEXITCODE
