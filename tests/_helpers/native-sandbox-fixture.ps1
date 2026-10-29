function New-NativeSandboxFixture([string]$ShimDir) {
    $frameworkRoot = Join-Path $env:WINDIR 'Microsoft.NET'
    $compiler = Get-ChildItem -LiteralPath $frameworkRoot -Filter csc.exe -Recurse -File |
        Where-Object FullName -Match '[\\/]v4\.' | Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $compiler) { throw 'Native sandbox tests require the .NET Framework C# compiler.' }
    $targetDir = Join-Path $ShimDir 'node_modules\@anthropic-ai\claude-code\bin'
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    $target = Join-Path $targetDir 'claude.exe'
    $reference = Join-Path $compiler.DirectoryName 'System.Web.Extensions.dll'
    & $compiler.FullName /nologo /target:exe "/out:$target" "/reference:$reference" `
        (Join-Path $PSScriptRoot 'native-sandbox-fixture.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Native sandbox fixture compilation failed.' }
    return $target
}

function Assert-NativeSandboxCapture($Capture) {
    $expected = @('-p', '--restricted', '--safe-mode', '--strict-mcp-config', '--tools', '',
        '--permission-mode', 'dontAsk', '--disable-slash-commands', '--no-session-persistence', '--')
    if ($Capture.arguments.Count -ne $expected.Count + 1) { throw 'Sandbox received an unexpected argument count.' }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($Capture.arguments[$index] -cne $expected[$index]) { throw "Sandbox argument $index was changed or omitted." }
    }
    if ($Capture.entries.Count -ne 0) { throw 'Sandbox started in a nonempty directory.' }
    if ($Capture.stdin -cne '') { throw 'Sandbox stdin was not closed.' }
    if (Test-Path -LiteralPath $Capture.cwd) { throw 'Sandbox temporary directory survived the invocation.' }
}
