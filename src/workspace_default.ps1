#!/usr/bin/env pwsh
# Sutando workspace resolver for PowerShell — twin of src/workspace_default.py
# and src/workspace_default.ts. Dot-source this and call Resolve-SutandoWorkspace
# so every Windows .ps1 computes the SAME workspace path as the bash/Python side.
#
#   . "$PSScriptRoot/workspace_default.ps1"
#   $WORKSPACE = Resolve-SutandoWorkspace
#
# Resolution (must match the M0 contract — see CLAUDE.md "Workspace contract"):
#   1. `bash scripts/sutando-config.sh workspace` — the canonical resolver. Reads
#      sutando.config.local.json (gitignored, per-clone) and defaults to
#      <repo>/workspace/. This is the single source of truth shared with every
#      other runtime, so PowerShell and bash/Python never split-brain on the path.
#   2. Fallback (bash missing, or the resolver errors): <repo>/workspace/ — the
#      same M0 in-repo default the resolver would have returned.
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
