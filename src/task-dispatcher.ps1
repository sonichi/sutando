#!/usr/bin/env pwsh
# Sutando task dispatcher (Windows) - push-driven task processor.
#
# Solves the latency problem Windows hits because Claude Code 2.x removed the
# Monitor tool. The long-running sutando-core agent only wakes on cron ticks;
# between ticks, new task files sit in tasks/ until the next fire. This script
# is the workaround: it watches tasks/ continuously, and for each new task
# invokes `claude --print` as a one-shot subprocess. Each task completes in
# seconds, independent of cron timing.
#
# Roles:
#   sutando-core (long-running TUI) -> autonomous proactive-loop work, cron
#                                       jobs, health checks. Picks up tasks
#                                       too, but on its own schedule.
#   task-dispatcher (this script)   -> low-latency owner-task processing.
#
# Both can read the same task file; we use a per-file lock (claim/rename
# pattern) to make sure exactly one of them processes any given task.
#
# Usage:
#   pwsh -File src/task-dispatcher.ps1               # foreground (for debugging)
#   pwsh -File src/task-dispatcher.ps1 -Background   # detach to a new window
#
# Output goes to <workspace>/logs/task-dispatcher.log.

[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Continue'

# Force UTF-8 end to end. `claude --print` emits UTF-8 JSON and the Discord/
# Telegram/Slack bridges read results with Python's UTF-8 default; without
# this PowerShell decodes the subprocess stdout in the legacy ANSI codepage,
# so `°`/`—`/`→` arrive at the user as mojibake (`┬░` / `ΓÇö`). Set both the
# console output encoding (affects how `& $CLAUDE` output is decoded) and the
# default $OutputEncoding (affects the pipe into claude).
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
try { [Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false) } catch {}

$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Resolve workspace - matches startup.ps1 / workspace_default.ps1 contract.
. "$PSScriptRoot/workspace_default.ps1"
$WORKSPACE = Resolve-SutandoWorkspace
$TASKS    = Join-Path $WORKSPACE 'tasks'
$RESULTS  = Join-Path $WORKSPACE 'results'
$ARCHIVE  = Join-Path $TASKS 'archive'
$LOGS     = Join-Path $WORKSPACE 'logs'
$LOGFILE  = Join-Path $LOGS 'task-dispatcher.log'
foreach ($d in $TASKS, $RESULTS, $ARCHIVE, $LOGS) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# --- Background detach -------------------------------------------------------
if ($Background) {
    # Self-relaunch in a hidden window; parent returns immediately.
    $psExe = (Get-Command pwsh -ErrorAction SilentlyContinue) ?? (Get-Command powershell -ErrorAction SilentlyContinue)
    # Explicitly redirect the detached child's console handles. In a piped
    # caller (CI, restart.ps1 output capture, service wrappers), inheriting
    # stdout/stderr keeps the parent's pipe open indefinitely even after the
    # launcher exits, so restart appears hung while services are already live.
    $stdoutLog = Join-Path $LOGS 'task-dispatcher.stdout.log'
    $stderrLog = Join-Path $LOGS 'task-dispatcher.stderr.log'
    Start-Process -FilePath $psExe.Source -ArgumentList @(
        '-NoProfile', '-File', $PSCommandPath
    ) -WindowStyle Hidden `
      -RedirectStandardOutput $stdoutLog `
      -RedirectStandardError $stderrLog | Out-Null
    Write-Host "task-dispatcher launched in background. Log: $LOGFILE"
    exit 0
}

# --- PID sentinel + single-instance guard -----------------------------------
$pidFile = Join-Path $WORKSPACE 'state\task-dispatcher.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null
if (Test-Path $pidFile) {
    $stalePid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($stalePid -and (Get-Process -Id $stalePid -ErrorAction SilentlyContinue)) {
        Write-Host "task-dispatcher already running (pid $stalePid). Exiting."
        exit 0
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}
Set-Content -Path $pidFile -Value $PID -NoNewline

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LOGFILE -Value $line
}

# Locate claude once. On Windows `claude` is often a .cmd / .ps1 shim rather
# than a direct .exe (npm/PowerShell install) — accept either, mirroring
# scripts/start-cli.ps1. Prefer a real .exe when present, else take the shim.
$claudeAll = Get-Command claude -All -ErrorAction SilentlyContinue
$claudeCmd = ($claudeAll | Where-Object { $_.Path -and $_.Path.ToLower().EndsWith('.exe') } | Select-Object -First 1)
if (-not $claudeCmd) { $claudeCmd = ($claudeAll | Select-Object -First 1) }
if (-not $claudeCmd) {
    Log "ERROR: claude not found on PATH. Install Claude Code and retry."
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 1
}
$CLAUDE = $claudeCmd.Source
Log "task-dispatcher started. claude=$CLAUDE workspace=$WORKSPACE"

if ($ValidateOnly) {
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Write-Host "Windows task-dispatcher validation passed."
    exit 0
}

# --- Per-task processing -----------------------------------------------------

# Parse the task file body (header lines `key: value` + a `task: <body>` line
# that may span multiple lines until the next `--- recent voice transcript ---`
# marker or EOF). Returns the user-facing prompt text.
#
# The bridges write metadata headers (source/channel_id/...) AFTER the `task:`
# line, so the body must terminate at the first trailing header line - otherwise
# those headers get swallowed into the prompt and the subprocess treats
# `source: discord` / `access_tier: owner` as part of the user's message and
# narrates its own internals back at them. Stop at: a known-header line, the
# `--- recent voice transcript ---` marker, or EOF.
$script:KNOWN_TASK_HEADERS = @(
    'id', 'timestamp', 'source', 'channel_id', 'channel_name',
    'guild_name', 'source_message_id', 'parent_message_id',
    'user_id', 'access_tier', 'priority'
)
$script:HEADER_LINE_RE = '^(?:' + ($script:KNOWN_TASK_HEADERS -join '|') + '):\s'
function Get-TaskPrompt($path) {
    $content = Get-Content -Raw -Path $path -ErrorAction SilentlyContinue
    if (-not $content) { return $null }
    $lines = $content -split '\r?\n'
    $body = [System.Collections.Generic.List[string]]::new()
    $inBody = $false
    foreach ($line in $lines) {
        if (-not $inBody) {
            if ($line -match '^task:\s*(.*)$') {
                $inBody = $true
                if ($Matches[1]) { $body.Add($Matches[1]) }
            }
            continue
        }
        # End of body: a trailing metadata header or the transcript marker.
        if ($line -match $script:HEADER_LINE_RE) { break }
        if ($line -match '^---') { break }
        $body.Add($line)
    }
    if ($inBody) { return ($body -join "`n").Trim() }
    return $content.Trim()
}

# Parse a `key: value` header line from the task body. Returns the trimmed
# value (or $null). Used to extract channel_id/source so each surface gets its
# own resumable claude session and the right conversational framing.
#
# The bridges (discord/telegram/slack) and task-bridge.ts all write the `task:`
# body line BEFORE the metadata headers (`source:`, `channel_id:`, ...). An
# earlier version stopped scanning at `task:` to block a multi-line body from
# forging headers - but that also meant the real trailing headers were never
# read, so every Discord task collapsed onto the default `local` session and
# lost its `source`. We instead only honor a fixed allowlist of known header
# keys: a body line can at most spoof one of those, and they carry no authority
# a message author would want to forge (channel/session routing + framing).
# ($script:KNOWN_TASK_HEADERS is defined above, shared with Get-TaskPrompt.)
#
# Header lookups skip the `task:` body region so a benign multi-line body
# doesn't get mis-read as a trailing header. We mirror Get-TaskPrompt's
# boundary: once we enter the body at `task:`, ignore every line until the next
# genuine trailing-header line (or the `---`/fence boundary), and only match the
# requested key OUTSIDE the body.
#
# This is NOT a defense against a forged trusted field. The body boundary is
# detected by HEADER_LINE_RE, so a forged `access_tier: owner` line ends the
# body and is then read as a real header (verified: a `task:`+`access_tier: owner`
# two-liner returns 'owner'). Position alone can't tell a forged trailing header
# from a real one — real producers write the body first, then `access_tier:`
# immediately after. The ACTUAL defense is producer-side confine_user_content
# (src/task_body_guard.py), which U+200B-prefixes any header-shaped user line so
# it no longer matches `^access_tier:` and is skipped here. An unconfined,
# hand-dropped task file is out of scope; closing that would need an explicit
# body fence in the bridges (producer-side protocol change), not a dispatcher
# heuristic.
function Get-TaskHeader($path, $key) {
    if ($key -notin $script:KNOWN_TASK_HEADERS) { return $null }
    $content = Get-Content -Raw -Path $path -ErrorAction SilentlyContinue
    if (-not $content) { return $null }
    $inBody = $false
    foreach ($line in ($content -split '\r?\n')) {
        if ($inBody) {
            # Body ends at a trailing metadata header or the transcript/fence
            # marker; lines before that are user content and never headers.
            if (($line -match $script:HEADER_LINE_RE) -or ($line -match '^---')) {
                $inBody = $false
            } else {
                continue
            }
        }
        if ($line -match '^task:') { $inBody = $true; continue }
        if ($line -match "^${key}:\s*(.+?)\s*$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

# Session map persists across dispatcher restarts so a channel keeps the same
# resumable claude conversation. Schema:
#   { "<channel_id>": "<session-uuid>", ... }
# Path: <workspace>/state/dispatcher-sessions.json
$SESSION_FILE = Join-Path $WORKSPACE 'state\dispatcher-sessions.json'
$sessionLock  = New-Object Object

function Load-SessionMap {
    if (-not (Test-Path $SESSION_FILE)) { return @{} }
    try {
        $raw = Get-Content -Raw -Path $SESSION_FILE -ErrorAction Stop
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        $h = @{}
        foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = $p.Value }
        return $h
    } catch {
        Log "session map: failed to load ($_); starting empty"
        return @{}
    }
}

function Save-SessionMap($map) {
    try {
        $json = $map | ConvertTo-Json -Depth 3 -Compress
        # Write to a tmp file then move - avoids torn writes if the dispatcher
        # is killed mid-write while another process is reading.
        $tmp = "$SESSION_FILE.tmp"
        Set-Content -Path $tmp -Value $json -NoNewline
        Move-Item -Path $tmp -Destination $SESSION_FILE -Force
    } catch {
        Log "session map: failed to save ($_)"
    }
}

# Single in-memory copy; serialize all reads/writes under $sessionLock so
# concurrent task processing doesn't race on Get/Set.
$script:sessions = Load-SessionMap
Log "session map: loaded $(($script:sessions.Keys | Measure-Object).Count) channel(s)"

# New-Guid v4 - used when a channel needs a fresh session.
function New-SessionId { [guid]::NewGuid().ToString() }

function Invoke-ClaudeTurn($prompt, $sessionId, $createSession, $stderrPath) {
    Remove-Item -Path $stderrPath -ErrorAction SilentlyContinue
    if ($createSession) {
        $stdout = $prompt | & $CLAUDE --print --output-format json `
            --session-id $sessionId `
            --dangerously-skip-permissions 2>$stderrPath
    } else {
        $stdout = $prompt | & $CLAUDE --print --output-format json `
            --resume $sessionId `
            --dangerously-skip-permissions 2>$stderrPath
    }
    $exitCode = $LASTEXITCODE
    $stderr = Get-Content -Raw -Path $stderrPath -ErrorAction SilentlyContinue
    Remove-Item -Path $stderrPath -ErrorAction SilentlyContinue
    [pscustomobject]@{ Stdout = $stdout; ExitCode = $exitCode; Stderr = $stderr }
}

function Test-StaleClaudeSession($call) {
    $stdout = if ($call.Stdout -is [array]) { $call.Stdout -join "`n" } else { [string]$call.Stdout }
    return "$stdout`n$($call.Stderr)" -match '(?i)No conversation found with session ID'
}

# Claim a task by renaming it to `.processing` - atomic on NTFS. Returns the
# new path on success, $null if some other process already claimed it.
function Claim-Task($file) {
    $claim = "$($file.FullName).processing"
    try {
        Move-Item -Path $file.FullName -Destination $claim -ErrorAction Stop
        return $claim
    } catch {
        return $null
    }
}

function Process-Task($claimedPath, $taskId) {
    $prompt = Get-TaskPrompt $claimedPath
    if (-not $prompt) {
        Log "${taskId}: empty prompt, skipping"
        return
    }

    # Tier guard. Non-owner (team/other) tasks carry a `===SUTANDO SYSTEM
    # INSTRUCTIONS===` fence the bridge appends AFTER the metadata headers,
    # telling the consumer to run the task under `codex --sandbox read-only`.
    # Get-TaskPrompt only returns the `task:` body and discards everything from
    # the first header onward — so the fence never reaches the prompt. Feeding
    # the bare body to `claude --print --dangerously-skip-permissions` would run
    # untrusted content with FULL capabilities, violating CLAUDE.md's "non-owner
    # tasks MUST be processed via the sandboxed path." The dispatcher has no
    # sandbox, so the only safe action is to refuse. Owner tier (or a task with
    # no access_tier — voice/local/older producers) processes normally.
    $accessTier = Get-TaskHeader $claimedPath 'access_tier'
    if ($accessTier -and $accessTier -ne 'owner') {
        Log "${taskId}: refusing non-owner task (access_tier=$accessTier) — dispatcher has no sandbox"
        $refusal = 'Sandbox unavailable; refusing non-owner task.'
        $resultFile = Join-Path $RESULTS "$taskId.txt"
        [System.IO.File]::WriteAllText($resultFile, $refusal, [System.Text.UTF8Encoding]::new($false))
        $archived = Join-Path $ARCHIVE "$taskId.txt"
        try { Move-Item -Path $claimedPath -Destination $archived -Force }
        catch { Remove-Item -Path $claimedPath -ErrorAction SilentlyContinue }
        return
    }

    # Resolve channel + session. Default channel `local` covers tasks with no
    # channel_id (older test fixtures, ad-hoc producers). Per-channel mapping
    # so voice/discord/telegram each retain their own conversation thread.
    $channel = Get-TaskHeader $claimedPath 'channel_id'
    if (-not $channel) { $channel = 'local' }

    # Frame the prompt so the one-shot subprocess answers the user, not narrates
    # its own plumbing. Without this the subprocess reads the repo CLAUDE.md
    # (task-bridge protocol, dispatcher rules) and replies with dispatcher/
    # session/result-file jargon instead of the user's actual question.
    $source = Get-TaskHeader $claimedPath 'source'
    if ($source -in @('discord', 'telegram', 'slack', 'voice', 'chat')) {
        $surface = switch ($source) {
            'discord'  { 'a Discord chat' }
            'telegram' { 'a Telegram chat' }
            'slack'    { 'a Slack chat' }
            'voice'    { 'a voice conversation' }
            default    { 'a chat' }
        }
        $preamble = @"
You are Sutando, the user's personal AI assistant, replying to a real person in $surface. Your entire output is delivered verbatim as your reply to them.

Reply directly and naturally to their message below. Answer the actual question or do the actual task they asked for. Keep it short and conversational - say only what answers the message, in as few sentences as it needs. Don't restate the question, don't pad with preamble or caveats, don't explain your reasoning unless they asked for it.

For current weather, you can run ``curl -s "https://wttr.in/<city>?format=%l:+%C+%t+(feels+%f),+humidity+%h,+wind+%w"`` (URL-encode spaces in the city as +). It needs no API key and works for any city worldwide. Use it instead of saying you lack a weather tool.

Never mention your own internal machinery: do NOT talk about task dispatchers, task IDs, sessions, subprocesses, result files, pipelines, ``claude --print``, processing status, the workspace, git branches, or uncommitted changes. The user does not know or care about any of that. If you genuinely cannot answer, say so plainly in one sentence.

The user's message:
$prompt
"@
        $prompt = $preamble
    }

    [System.Threading.Monitor]::Enter($sessionLock)
    try {
        $sessionId = $script:sessions[$channel]
        $isNew = $false
        if (-not $sessionId) {
            $sessionId = New-SessionId
            $script:sessions[$channel] = $sessionId
            Save-SessionMap $script:sessions
            $isNew = $true
        }
    } finally {
        [System.Threading.Monitor]::Exit($sessionLock)
    }

    $promptHead = ($prompt -split '\r?\n')[0]
    $promptHead = $promptHead.Substring(0, [Math]::Min(60, $promptHead.Length))
    Log "${taskId}: processing channel=$channel session=$sessionId$(if ($isNew) { ' (NEW)' }) ($promptHead)"

    # First call to a channel uses --session-id (creates the session with that
    # UUID); subsequent calls use --resume to continue it. Both accept stdin
    # as the prompt body via --print. --output-format json so we can extract
    # the .result field cleanly and detect errors structurally.
    # so the agent doesn't pause asking for tool-call approvals - matches the
    # interactive sutando-core posture.
    $stderrPath = "$claimedPath.stderr"
    $call = Invoke-ClaudeTurn $prompt $sessionId $isNew $stderrPath
    if (-not $isNew -and (Test-StaleClaudeSession $call)) {
        $staleSessionId = $sessionId
        $sessionId = New-SessionId
        [System.Threading.Monitor]::Enter($sessionLock)
        try {
            $script:sessions[$channel] = $sessionId
            Save-SessionMap $script:sessions
        } finally {
            [System.Threading.Monitor]::Exit($sessionLock)
        }
        Log "${taskId}: stale session $staleSessionId — retrying once with fresh session $sessionId"
        $isNew = $true
        $call = Invoke-ClaudeTurn $prompt $sessionId $true $stderrPath
    }
    $stdout = $call.Stdout
    $exitCode = $call.ExitCode
    $stderr = $call.Stderr

    # Default to whatever claude printed; we override below on parse success.
    $result = if ($stdout -is [array]) { $stdout -join "`n" } else { [string]$stdout }

    # --output-format json writes structured error results to STDOUT even when
    # Claude exits non-zero (for example an API 500). Parse before applying the
    # generic exit-code fallback so users see the actionable gateway/API error
    # instead of the misleading "claude exited 1" with empty stderr.
    $parsedJson = $false
    try {
        $obj = $result | ConvertFrom-Json -ErrorAction Stop
        if ($obj.is_error) {
            $errBody = if ($obj.result) { $obj.result } else { 'claude reported is_error=true' }
            Log "${taskId}: claude reported is_error=true: $errBody"
            $result = "task-dispatcher: claude error: $errBody"
            $parsedJson = $true
        } elseif ($null -ne $obj.result) {
            $result = [string]$obj.result
            $parsedJson = $true
            # Capture the returned session_id - it should match what we sent.
            if ($obj.session_id -and $obj.session_id -ne $sessionId) {
                Log "${taskId}: session id rotated $sessionId -> $($obj.session_id)"
                [System.Threading.Monitor]::Enter($sessionLock)
                try {
                    $script:sessions[$channel] = $obj.session_id
                    Save-SessionMap $script:sessions
                } finally {
                    [System.Threading.Monitor]::Exit($sessionLock)
                }
            }
        } else {
            Log "${taskId}: JSON had no .result field, falling through to raw stdout"
        }
    } catch {
        # Not JSON or malformed - keep the raw stdout we already assigned.
        Log "${taskId}: stdout was not JSON ($_), passing through raw"
    }

    if ($exitCode -ne 0 -and -not $parsedJson) {
        $errMsg = if ($stderr) { $stderr.Trim() } else { "claude exited $exitCode" }
        Log "${taskId}: FAILED (exit=$exitCode): $errMsg"
        $result = "task-dispatcher: claude --print exited $exitCode. stderr:`n$errMsg"
    }

    # Write result. Bridges/web UI watch results/task-<id>.txt. UTF-8 without
    # BOM so the Python bridges (read_text() = UTF-8) get clean bytes and a BOM
    # doesn't corrupt the first line.
    $resultFile = Join-Path $RESULTS "$taskId.txt"
    [System.IO.File]::WriteAllText($resultFile, [string]$result, [System.Text.UTF8Encoding]::new($false))
    Log "${taskId}: done -> $resultFile"

    # Archive the task file so we don't reprocess it on rescan.
    $archived = Join-Path $ARCHIVE "$taskId.txt"
    try {
        Move-Item -Path $claimedPath -Destination $archived -Force
    } catch {
        Log "${taskId}: archive failed: $_"
        # Last resort: delete so we don't loop on it.
        Remove-Item -Path $claimedPath -ErrorAction SilentlyContinue
    }
}

# --- Watch loop --------------------------------------------------------------

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $TASKS
$watcher.Filter = 'task-*.txt'
$watcher.IncludeSubdirectories = $false
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::CreationTime

# Drain any tasks already present on disk before we start watching.
function Drain-Pending {
    Get-ChildItem -Path $TASKS -File -Filter 'task-*.txt' -ErrorAction SilentlyContinue | ForEach-Object {
        $claimed = Claim-Task $_
        if (-not $claimed) { return }
        $taskId = $_.BaseName
        try {
            Process-Task $claimed $taskId
        } catch {
            Log "${taskId}: uncaught exception: $_"
        }
    }
}

Drain-Pending

try {
    while ($true) {
        # WaitForChanged blocks until an FS event or timeout (2s). Timeout
        # exists so we can drain anything the watcher missed in a burst.
        $r = $watcher.WaitForChanged(
            [System.IO.WatcherChangeTypes]::Created -bor [System.IO.WatcherChangeTypes]::Renamed -bor [System.IO.WatcherChangeTypes]::Changed,
            2000
        )
        # Tiny settle delay so the producer can finish writing before we read.
        # Tasks are usually <1KB so 50ms is plenty; bump if you see truncated reads.
        if (-not $r.TimedOut) { Start-Sleep -Milliseconds 50 }
        Drain-Pending
    }
} finally {
    $watcher.Dispose()
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
    Log "task-dispatcher stopped."
}
