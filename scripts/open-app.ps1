#!/usr/bin/env pwsh
# CLI adapter for the shared Windows app-switching backend.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$App,
    [ValidateRange(1, 30)]
    [int]$TimeoutSeconds = 10,
    [switch]$ValidateOnly
)

& (Join-Path $PSScriptRoot '..\src\windows-app-launcher.ps1') @PSBoundParameters
exit $LASTEXITCODE
