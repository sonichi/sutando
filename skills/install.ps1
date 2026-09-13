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

foreach ($dir in Get-ChildItem -Path $SKILLS_DIR -Directory) {
    $name = $dir.Name
    $skillFile = Join-Path $dir.FullName 'SKILL.md'
    if (-not (Test-Path $skillFile)) { continue }

    $linkPath = Join-Path $TARGET $name
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
