#!/usr/bin/env pwsh
# scripts/start-cli.ps1 -NoWindow runs src/launch_hooks.py before it starts Claude, and a helper that
# fails never stops the launch. Runs a copy of the launcher against a stub helper and a claude shim.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$temp = Join-Path ([IO.Path]::GetTempPath()) ('sutando launch hooks ' + [guid]::NewGuid())
$savedPath = $env:PATH
try {
    $fake = Join-Path $temp 'repo'
    $bin = Join-Path $temp 'bin'
    foreach ($dir in (Join-Path $fake 'scripts'), (Join-Path $fake 'src'), $bin) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    foreach ($file in 'start-cli.ps1', 'python-binary.ps1', 'native-arguments.ps1') {
        Copy-Item -LiteralPath (Join-Path $repo "scripts/$file") -Destination (Join-Path $fake "scripts/$file")
    }
    $order = Join-Path $temp 'order.txt'
    $helper = Join-Path $fake 'src/launch_hooks.py'
    Set-Content -LiteralPath (Join-Path $bin 'claude.cmd') -Encoding ascii -Value @(
        '@echo claude>>"%ORDER_FILE%"', '@exit /b 0')
    $env:PATH = "$bin;$savedPath"
    $env:ORDER_FILE = $order

    function Invoke-Launch([string]$helperBody) {
        Set-Content -LiteralPath $helper -Encoding utf8 -Value $helperBody
        Remove-Item -LiteralPath $order -ErrorAction SilentlyContinue
        & pwsh -NoProfile -File (Join-Path $fake 'scripts/start-cli.ps1') -NoWindow
        if ($LASTEXITCODE -ne 0) { throw "start-cli.ps1 exited $LASTEXITCODE" }
        return (@(Get-Content -LiteralPath $order) | ForEach-Object { $_.Trim() }) -join ','
    }

    $got = Invoke-Launch "import os`nopen(os.environ['ORDER_FILE'], 'a').write('hooks\n')"
    if ($got -cne 'hooks,claude') { throw "expected the hooks before claude, got '$got'" }

    $got = Invoke-Launch "import sys`nsys.exit(3)"
    if ($got -cne 'claude') { throw "a failing helper must not stop the launch, got '$got'" }

    Write-Host 'windows-launch-hooks: OK'
} finally {
    $env:PATH = $savedPath
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
