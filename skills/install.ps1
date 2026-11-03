#!/usr/bin/env pwsh
# Windows twin of skills/install.sh.
# Installs Sutando skills into ~/.claude/skills/ so the core Claude Code
# session can find them (especially /schedule-crons, which startup invokes).
#
# Uses directory junctions (mklink /J) instead of symlinks so it works
# without admin rights or Windows Developer Mode. Junctions are local-fs only
# (no SMB targets) but that matches every realistic Sutando install layout.
#
# Idempotent: re-running prints "exists" for already-installed skills.

$ErrorActionPreference = 'Continue'
$SKILLS_DIR = $PSScriptRoot
$TARGET = Join-Path $HOME '.claude\skills'

New-Item -ItemType Directory -Force -Path $TARGET | Out-Null

# Developer-only skills are decided by src/skill_audience.py, the rule every installer shares.
$REPO = Split-Path -Parent $SKILLS_DIR
. (Join-Path $REPO 'scripts/python-binary.ps1')
$skillDirs = @(Get-ChildItem -Path $SKILLS_DIR -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') } | ForEach-Object { $_.FullName })
$excluded = @{}
try {
    $py = Resolve-SutandoPython
    $pyArgs = @()
    if ($py -eq 'py') { $pyArgs = @('-3') }
    $lines = & $py @pyArgs (Join-Path $REPO 'src/skill_audience.py') excluded @skillDirs
    if ($LASTEXITCODE -ne 0) { throw "skill_audience.py exited $LASTEXITCODE" }
    foreach ($line in @($lines)) {
        $parts = "$line" -split "`t", 2
        if ($parts.Count -eq 2) { $excluded[$parts[0]] = $parts[1] }
    }
} catch {
    Write-Host "  X audience check failed ($_); linking nothing"
    exit 1
}

foreach ($dir in Get-ChildItem -Path $SKILLS_DIR -Directory) {
    $name = $dir.Name
    $skillFile = Join-Path $dir.FullName 'SKILL.md'
    if (-not (Test-Path $skillFile)) { continue }

    $linkPath = Join-Path $TARGET $name
    if ($excluded.ContainsKey($name)) {
        if (Test-Path $linkPath) {
            Write-Host "  ! $name ($($excluded[$name]); already installed, left in place - remove it yourself, or set skills.developer)"
        } else {
            Write-Host "  - $name ($($excluded[$name]); not installed - set skills.developer to opt in)"
        }
        continue
    }
    if (Test-Path $linkPath) {
        # Already a junction / symlink / directory - leave it alone.
        Write-Host "  - $name (already installed)"
        continue
    }

    # Use cmd.exe mklink /J - the only no-admin-required option on Windows
    # for directory-to-directory linking.
    $result = cmd.exe /c mklink /J "$linkPath" "$($dir.FullName)" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  + $name"
    } else {
        Write-Host "  X $name (mklink failed: $result)"
    }
}

Write-Host ""
Write-Host "Installed. Skills available in any Claude Code session."
