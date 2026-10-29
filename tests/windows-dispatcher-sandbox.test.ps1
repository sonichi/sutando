#!/usr/bin/env pwsh
# Exercise the production PowerShell adapter and native child argv in an isolated workspace.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$workspace = Join-Path $tempRoot ('sutando-native-sandbox-' + [guid]::NewGuid().ToString('N'))
$shimDir = Join-Path $workspace 'bin with spaces'
$environmentKeys = @('PATH', 'SUTANDO_SANDBOX_RUNNERS', 'SUTANDO_NATIVE_SANDBOX_CAPTURES',
    'SUTANDO_NATIVE_SANDBOX_MODE', 'SUTANDO_TEST_SHELL_MARKER', 'CLAUDE_CONFIG_DIR', 'SUTANDO_TEST_REAL_PYTHON')
$previousEnvironment = @{}
foreach ($key in $environmentKeys) {
    $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$previousOutputEncoding = $OutputEncoding
. (Join-Path $PSScriptRoot '_helpers\native-sandbox-fixture.ps1')

function Read-SandboxCaptures {
    @(Get-ChildItem -LiteralPath $env:SUTANDO_NATIVE_SANDBOX_CAPTURES -Filter '*.json' |
        Sort-Object Name | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json })
}

try {
    $python = (Get-Command python -ErrorAction Stop).Source
    New-Item -ItemType Directory -Path $shimDir -Force | Out-Null
    $native = New-NativeSandboxFixture $shimDir
    $env:PATH = "$shimDir;$(Split-Path $python);$env:SystemRoot\System32"
    $env:SUTANDO_SANDBOX_RUNNERS = 'agy,codex,claude'
    $env:SUTANDO_NATIVE_SANDBOX_CAPTURES = Join-Path $workspace 'captures'
    $env:SUTANDO_NATIVE_SANDBOX_MODE = Join-Path $workspace 'mode'
    $env:SUTANDO_TEST_SHELL_MARKER = Join-Path $workspace 'SHELL_MUST_NOT_RUN'
    $env:CLAUDE_CONFIG_DIR = Join-Path $workspace 'empty-config'
    New-Item -ItemType Directory -Path $env:CLAUDE_CONFIG_DIR -Force | Out-Null
    $OutputEncoding = [Text.UTF8Encoding]::new($false)
    $shim = '@echo off' + "`r`n" + 'echo SHELL_RAN>"%SUTANDO_TEST_SHELL_MARKER%"' + "`r`nexit /b 42`r`n"
    foreach ($name in @('claude.cmd', 'codex.cmd', 'agy.cmd')) {
        [IO.File]::WriteAllText((Join-Path $shimDir $name), $shim, [Text.Encoding]::ASCII)
    }
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $repo 'src\task-dispatcher.ps1'), [ref]$null, [ref]$null)
    $adapter = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-SharedSandbox'
    }, $true)
    if (-not $adapter) { throw 'Missing production shared sandbox adapter.' }
    Invoke-Expression $adapter.Extent.Text

    $question = 'line one "quoted" & echo INJECTED > "' + $env:SUTANDO_TEST_SHELL_MARKER + '" | more %PATH% !value! ^ caret' +
        "`n" + 'line two: café 雪 😀 trailing slash\'
    $answer = Invoke-SharedSandbox $question
    if ($answer -cne 'WINDOWS_SANDBOX_OK') { throw "Native sandbox failed: $answer" }
    $captures = @(Read-SandboxCaptures)
    if ($captures.Count -ne 1) { throw 'Expected one native invocation through the npm shim location.' }
    Assert-NativeSandboxCapture $captures[0]
    if (-not ($captures[0].prompt.EndsWith($question + "`r`n") -or $captures[0].prompt.EndsWith($question + "`n"))) {
        throw 'Shell metacharacters, Unicode, quotes, or newlines changed in transit.'
    }

    foreach ($mode in @('fail', 'empty')) {
        [IO.File]::WriteAllText($env:SUTANDO_NATIVE_SANDBOX_MODE, $mode)
        $answer = Invoke-SharedSandbox $question
        if ($answer -cne 'Sandbox unavailable; refusing non-owner task.') { throw "Unsafe failure output for mode $mode" }
    }
    foreach ($capture in @(Read-SandboxCaptures)) { Assert-NativeSandboxCapture $capture }
    Remove-Item -LiteralPath $env:SUTANDO_NATIVE_SANDBOX_MODE

    Move-Item -LiteralPath $native -Destination (Join-Path $shimDir 'claude.exe')
    if ((Invoke-SharedSandbox $question) -cne 'WINDOWS_SANDBOX_OK') { throw 'Direct native executable was not selected.' }
    $captures = @(Read-SandboxCaptures)
    if ($captures.Count -ne 4) { throw 'A failed or empty answer retried another runner.' }
    Assert-NativeSandboxCapture $captures[-1]
    [IO.File]::WriteAllText($env:SUTANDO_NATIVE_SANDBOX_MODE, 'wait')
    $timeoutProbe = @'
import sys
sys.path.insert(0, sys.argv[1])
from policy import sandbox_runner
try:
    sandbox_runner.answer("native timeout fixture", timeout=1)
except sandbox_runner.SandboxUnavailable:
    print(sandbox_runner.UNAVAILABLE)
else:
    raise SystemExit("timeout did not refuse the child")
'@
    $timeoutAnswer = & $python -X utf8 -c $timeoutProbe (Join-Path $repo 'src')
    if ($LASTEXITCODE -ne 0 -or $timeoutAnswer -cne 'Sandbox unavailable; refusing non-owner task.') {
        throw 'Native child timeout did not produce the generic refusal.'
    }
    $captures = @(Read-SandboxCaptures)
    if ($captures.Count -ne 5) { throw 'Timed-out native child did not start.' }
    Assert-NativeSandboxCapture $captures[-1]
    if (Get-Process -Id $captures[-1].pid -ErrorAction SilentlyContinue) { throw 'Timed-out native child survived.' }
    Remove-Item -LiteralPath $env:SUTANDO_NATIVE_SANDBOX_MODE

    $env:SUTANDO_TEST_REAL_PYTHON = $python
    [IO.File]::WriteAllText((Join-Path $shimDir 'python.exe'), 'not an executable')
    $launcher = @'
param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$Arguments,
      [Parameter(ValueFromPipeline=$true)][string]$Question)
if ($Arguments[0] -ne '-3') { exit 9 }
$remaining = @($Arguments | Select-Object -Skip 1)
$Question | & $env:SUTANDO_TEST_REAL_PYTHON @remaining
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText((Join-Path $shimDir 'py.ps1'), $launcher, [Text.UTF8Encoding]::new($false))
    $launcherProbe = & (Join-Path $shimDir 'py.ps1') -3 -c 'print("LAUNCHER_PROBE")'
    if ($LASTEXITCODE -ne 0 -or $launcherProbe -cne 'LAUNCHER_PROBE') { throw "Launcher fixture failed: $launcherProbe ($LASTEXITCODE)" }
    if ((Invoke-SharedSandbox $question) -cne 'WINDOWS_SANDBOX_OK') { throw 'Broken python executable prevented py launcher fallback.' }
    [IO.File]::WriteAllText($env:SUTANDO_NATIVE_SANDBOX_MODE, 'fail')
    if ((Invoke-SharedSandbox $question) -cne 'Sandbox unavailable; refusing non-owner task.') {
        throw 'A failed sandbox answer did not refuse after interpreter fallback.'
    }
    $captures = @(Read-SandboxCaptures)
    if ($captures.Count -ne 7) { throw 'Interpreter fallback retried a failed sandbox answer.' }
    foreach ($capture in $captures) { Assert-NativeSandboxCapture $capture }
    Remove-Item -LiteralPath $env:SUTANDO_NATIVE_SANDBOX_MODE
    Remove-Item -LiteralPath (Join-Path $shimDir 'python.exe'), (Join-Path $shimDir 'py.ps1')
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try { Remove-Item -LiteralPath (Join-Path $shimDir 'claude.exe'); break }
        catch {
            if ($attempt -eq 9) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
    if ((Invoke-SharedSandbox $question) -cne 'Sandbox unavailable; refusing non-owner task.') {
        throw 'Shell-only installation was not refused.'
    }
    $env:SUTANDO_SANDBOX_RUNNERS = 'agy,codex'
    if ((Invoke-SharedSandbox $question) -cne 'Sandbox unavailable; refusing non-owner task.') {
        throw 'Unsafe-only runners were not refused.'
    }
    if (Test-Path -LiteralPath $env:SUTANDO_TEST_SHELL_MARKER) { throw 'Untrusted prompt reached a command shell.' }
    if (@(Read-SandboxCaptures).Count -ne 7) { throw 'A refused task ran a native child.' }
    [pscustomobject]@{
        native_invocations = 7
        empty_tools_argument_preserved = $true
        hostile_unicode_prompt_preserved = $true
        fresh_empty_cwd_closed_stdin_and_cleanup = $true
        failed_stdout_and_stderr_hidden = $true
        empty_answer_refused = $true
        timed_out_child_stopped_and_cwd_removed = $true
        broken_python_uses_launcher_once = $true
        unsafe_and_shell_only_installations_refused = $true
    } | ConvertTo-Json -Compress
} finally {
    $OutputEncoding = $previousOutputEncoding
    foreach ($key in $environmentKeys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], 'Process')
    }
    if (Test-Path -LiteralPath $workspace) {
        $resolved = (Resolve-Path -LiteralPath $workspace).Path
        if (-not $resolved.StartsWith($tempRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
            -not $resolved.Equals([IO.Path]::GetFullPath($workspace), [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing cleanup outside the exact test workspace under TEMP.'
        }
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            try { Remove-Item -LiteralPath $resolved -Recurse -Force; break }
            catch {
                if ($attempt -eq 9) { throw }
                Start-Sleep -Milliseconds 200
            }
        }
    }
}
