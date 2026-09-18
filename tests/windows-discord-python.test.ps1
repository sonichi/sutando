#!/usr/bin/env pwsh
# Run the production authorization adapter with controlled interpreter commands.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tokens = $null
$errors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repo 'src/task-dispatcher.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Dispatcher has PowerShell parse errors.' }
$adapter = $tree.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-VerifiedDiscordCollaborator'
}, $false)
if (-not $adapter) { throw 'Collaborator authorization adapter is missing.' }

& {
    . ([scriptblock]::Create($adapter.Extent.Text))
    $cases = @(
        @{ mode = 'python-authorizes'; calls = 'python'; authorized = $true },
        @{ mode = 'py-only'; calls = 'py'; authorized = $true },
        @{ mode = 'python-fails'; calls = 'python,py'; authorized = $true },
        @{ mode = 'python-empty'; calls = 'python,py'; authorized = $true },
        @{ mode = 'python-invalid-json'; calls = 'python,py'; authorized = $true },
        @{ mode = 'python-denies'; calls = 'python'; authorized = $false },
        @{ mode = 'both-fail'; calls = 'python,py'; authorized = $false },
        @{ mode = 'neither-installed'; calls = ''; authorized = $false }
    )
    function Join-Path {
        param([string]$Path, [string]$ChildPath)
        if ($ChildPath -ne 'discord_access.py') { throw "Unexpected helper: $ChildPath" }
        return 'discord_access.py'
    }
    function Get-Command {
        param([string]$Name)
        if ($case.mode -eq 'neither-installed') { return $null }
        if ($Name -eq 'python') {
            if ($case.mode -eq 'py-only') { return $null }
            return [pscustomobject]@{ Source = 'Invoke-FixturePython' }
        }
        if ($Name -eq 'py') { return [pscustomobject]@{ Source = 'Invoke-FixturePy' } }
        throw "Unexpected executable lookup: $Name"
    }
    function Invoke-FixturePython {
        $calls.Add('python')
        $global:LASTEXITCODE = 0
        switch ($case.mode) {
            'python-fails' { $global:LASTEXITCODE = 1; return }
            'both-fail' { $global:LASTEXITCODE = 1; return }
            'python-empty' { return }
            'python-invalid-json' { return '{' }
            'python-denies' { return '{"authorized":false}' }
            default { return '{"authorized":true,"body":"fixture","channel_id":"123"}' }
        }
    }
    function Invoke-FixturePy {
        $calls.Add('py')
        if ($args[0] -ne '-3' -or $args[-2] -ne '--task-file' -or $args[-1] -ne 'fixture.txt') {
            throw 'Python launcher arguments were not preserved.'
        }
        $global:LASTEXITCODE = if ($case.mode -eq 'both-fail') { 1 } else { 0 }
        return '{"authorized":true,"body":"fixture","channel_id":"123"}'
    }
    foreach ($case in $cases) {
        $calls = [Collections.Generic.List[string]]::new()
        $result = Get-VerifiedDiscordCollaborator 'fixture.txt'
        if ([bool]$result -ne $case.authorized -or ($calls -join ',') -ne $case.calls) {
            throw "Interpreter fallback failed for $($case.mode): calls=$($calls -join ',')"
        }
    }
    Write-Output "Discord collaborator interpreter fallback: $($cases.Count) cases passed."
}
