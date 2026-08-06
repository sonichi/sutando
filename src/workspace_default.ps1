#!/usr/bin/env pwsh
# Sutando workspace resolver for PowerShell — twin of src/workspace_default.py
# and src/workspace_default.ts. Dot-source this and call Resolve-SutandoWorkspace
# so every Windows .ps1 computes the SAME workspace path as the bash/Python side.
#
#   . "$PSScriptRoot/workspace_default.ps1"
#   $WORKSPACE = Resolve-SutandoWorkspace
#
# Resolution (must match the M0 contract — see CLAUDE.md "Workspace contract"):
#   1. Call src.sutando_config.resolve_workspace() with a working Windows
#      Python (`python`, then `py -3`). This is the same canonical loader used
#      by bash and every runtime, without requiring Git Bash or WSL.
#   2. Fall back to `bash scripts/sutando-config.sh workspace` for installations
#      whose Python is only discoverable inside Git Bash.
#   3. If neither runtime is usable, return <repo>/workspace/ — the same M0
#      baked default the canonical loader would have returned.
#
# NOTE: $SUTANDO_WORKSPACE is intentionally NOT honored. It was dropped as a
# workspace override in v0.8 / #1440; the resolver ignores its value (it only
# fires a one-time deprecation warning when set). The pre-M0 home-directory
# default is gone for the same reason — readers/writers that still target it land
# in a directory no service watches.

function Resolve-SutandoWorkspace {
    # $PSScriptRoot here is src/ (this file's directory), so the repo root is its
    # parent — independent of the caller's CWD.
    $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $configScript = Join-Path $repo 'scripts/sutando-config.sh'

    $code = @'
import os
import sys
sys.path.insert(0, os.environ["SUTANDO_RESOLVE_REPO"])
from src.sutando_config import resolve_workspace
print(resolve_workspace(), end="")
'@
    $previousRepo = $env:SUTANDO_RESOLVE_REPO
    $env:SUTANDO_RESOLVE_REPO = $repo
    try {
        $candidates = @()
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($python) { $candidates += ,@($python.Source) }
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) { $candidates += ,@($py.Source, '-3') }

        foreach ($candidate in $candidates) {
            try {
                $exe = $candidate[0]
                $prefixArgs = @($candidate | Select-Object -Skip 1)
                $ws = (& $exe @prefixArgs -c $code 2>$null)
                if ($LASTEXITCODE -eq 0 -and $ws) {
                    return $ws.Trim()
                }
            } catch {
                # Try the next interpreter.
            }
        }
    } finally {
        $env:SUTANDO_RESOLVE_REPO = $previousRepo
    }

    if (Get-Command bash -ErrorAction SilentlyContinue) {
        try {
            $ws = (& bash $configScript workspace 2>$null)
            if ($LASTEXITCODE -eq 0 -and $ws) {
                return $ws.Trim()
            }
        } catch {
            # fall through to the in-repo default
        }
    }

    return (Join-Path $repo 'workspace')
}
