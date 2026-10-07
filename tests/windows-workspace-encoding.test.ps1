#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$temp = Join-Path ([IO.Path]::GetTempPath()) ('sutando 工作区 ' + [guid]::NewGuid())
$previousEncoding = [Console]::OutputEncoding
$savedEnv = @{}
foreach ($key in 'PYTHONIOENCODING', 'PYTHONUTF8', 'SUTANDO_RESOLVE_REPO', 'SUTANDO_WORKSPACE', 'SUTANDO_TEST_MODE') {
    $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    New-Item -ItemType Directory -Path (Join-Path $temp 'src') | Out-Null
    Copy-Item (Join-Path $repo 'src/workspace_default.ps1') (Join-Path $temp 'src')
    Copy-Item (Join-Path $repo 'src/sutando_config.py') (Join-Path $temp 'src')
    $expected = Join-Path $temp '配置的空间'
    @{ workspace = @{ path = $expected } } | ConvertTo-Json | Set-Content (Join-Path $temp 'sutando.config.json') -Encoding utf8NoBOM
    $env:SUTANDO_WORKSPACE = $null
    $env:SUTANDO_TEST_MODE = $null
    $env:PYTHONIOENCODING = 'cp1252'
    $env:PYTHONUTF8 = '0'
    $env:SUTANDO_RESOLVE_REPO = 'previous value'
    [Console]::OutputEncoding = [Text.Encoding]::ASCII
    $python = Microsoft.PowerShell.Core\Get-Command python, python3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $python) { throw 'Python required for the workspace encoding regression test.' }
    $expected = (& $python.Source -c 'import json,os,sys; print(json.dumps(os.path.realpath(sys.argv[1])))' $expected) | ConvertFrom-Json
    function Get-Command {
        param($Name)
        if ($Name -eq 'python') { return $python }
        return $null
    }
    . (Join-Path $temp 'src/workspace_default.ps1')
    $actual = Resolve-SutandoWorkspace
    if ($actual -cne $expected) { throw "Unicode workspace did not round trip: $actual" }
    if ($env:PYTHONIOENCODING -ne 'cp1252' -or $env:PYTHONUTF8 -ne '0' -or
        $env:SUTANDO_RESOLVE_REPO -ne 'previous value' -or [Console]::OutputEncoding.CodePage -ne 20127) {
        throw 'Workspace resolver leaked its temporary UTF-8 settings.'
    }
    Write-Output 'Workspace resolver: Unicode config path survived cp1252 and ASCII defaults; caller settings restored.'
} finally {
    [Console]::OutputEncoding = $previousEncoding
    foreach ($key in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnv[$key], 'Process') }
    Remove-Item -Recurse -Force $temp
}
