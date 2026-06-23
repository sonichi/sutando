# Sutando Architecture & Data Flow

## High-Level Overview

```
┌─────────────┬─────────────┬─────────────┬─────────────┐
│   Voice     │  Discord    │  Telegram   │   Chat      │
│  (Browser   │   Bridge    │   Bridge    │ (Direct)    │
│   :9900)    │   (DMs)     │  (Messages) │             │
└─────────────┴─────────────┴─────────────┴─────────────┘
      │             │             │             │
      │             │             │             │
      ▼             ▼             ▼             ▼
┌─────────────────────────────────────────────────────────┐
│            TASK FILES (tasks/ directory)                │
│                                                         │
│  task-1234567890.txt:                                  │
│  ┌───────────────────────────────────────────────────┐ │
│  │ id: task-1234567890                               │ │
│  │ timestamp: 2024-06-08T10:30:00Z                   │ │
│  │ source: voice|discord|telegram|chat               │ │
│  │ priority: urgent|normal|low                       │ │
│  │ access_tier: owner|team|other                     │ │
│  │ task: Fix the bug in auth module                  │ │
│  └───────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
                            │
                            │ fswatch monitors
                            ▼
┌─────────────────────────────────────────────────────────┐
│                 CLAUDE CODE CORE                        │
│                                                         │
│  ┌─────────────────┐    ┌─────────────────────────────┐ │
│  │ Task Watcher    │───▶│    Execution Engine         │ │
│  │ (stream mode)   │    │                             │ │
│  └─────────────────┘    │  • Full system access      │ │
│                         │  • File operations          │ │
│                         │  • Tool calling             │ │
│                         │  • External APIs            │ │
│                         │  • Skill orchestration     │ │
│                         └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
                            │
                            │ writes results
                            ▼
┌─────────────────────────────────────────────────────────┐
│          RESULT FILES (results/ directory)              │
│                                                         │
│  task-1234567890.txt (in results/):                    │
│  ┌───────────────────────────────────────────────────┐ │
│  │ Task completed successfully.                      │ │
│  │                                                   │ │
│  │ [Special markers]:                                │ │
│  │ • [file: /path/to/attachment]                     │ │
│  │ • [no-send] (skip delivery)                      │ │
│  │ • [deduped: other-task-id]                       │ │
│  │ • [channel: redirect-id]                         │ │
│  └───────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
      │             │             │             │
      │ polls for   │ polls for   │ polls for   │ reads
      │ results     │ results     │ results     │ results
      ▼             ▼             ▼             ▼
┌─────────────┬─────────────┬─────────────┬─────────────┐
│   Voice     │  Discord    │  Telegram   │   Chat      │
│  Response   │  Message    │  Message    │ Response    │
│ (Gemini)    │   Reply     │   Reply     │             │
└─────────────┴─────────────┴─────────────┴─────────────┘
```

## Two-Space Workspace Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          SUTANDO SPACES                         │
├──────────────────────────┬──────────────────────────────────────┤
│       CODE SPACE         │           WORKSPACE                  │
│                          │                                      │
│ Git Repository           │ Per-user runtime + content           │
│ $REPO_DIR                │ $WORKSPACE (default: <repo>/         │
│                          │  workspace/; configurable)           │
│ • src/                   │                                      │
│ • skills/                │ Ephemeral (per-host):                │
│ • docs/                  │  • tasks/        • results/          │
│ • CLAUDE.md              │  • state/        • logs/             │
│                          │  • data/                             │
│                          │                                      │
│                          │ Persistent (syncs via vault.sync.*): │
│                          │  • notes/        • build_log.md      │
│                          │  • .claude-sutando/.../memory/       │
└──────────────────────────┴──────────────────────────────────────┘

Sync is a property of sub-paths within Workspace, not a separate
top-level container. See docs/workspace-design.md for the 2-space
rationale (PR #1440, 2026-06-04).
```

## Skills & Tools Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                        EXECUTION LAYERS                           │
├─────────────────┬─────────────────────────────────────────────────┤
│ INLINE TOOLS    │                SKILLS SYSTEM                    │
│                 │                                                 │
│ Instant (<1s)   │ Complex Logic (>1s, separate processes)        │
│                 │                                                 │
│ • describe_     │ ┌─────────────┐  ┌─────────────┐  ┌───────────┐ │
│   screen()      │ │ phone-      │  │ discord-    │  │ screen-   │ │
│ • get_time()    │ │ conversation│  │ voice       │  │ record    │ │
│ • hang_up()     │ └─────────────┘  └─────────────┘  └───────────┘ │
│ • dtmf()        │                                                 │
│ • clipboard()   │ ┌─────────────┐  ┌─────────────┐  ┌───────────┐ │
│                 │ │ email-      │  │ image-      │  │ macos-    │ │
│                 │ │ sender      │  │ generation  │  │ tools     │ │
│                 │ └─────────────┘  └─────────────┘  └───────────┘ │
└─────────────────┴─────────────────────────────────────────────────┘
```

## Access Control & Multi-User Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     ACCESS CONTROL TIERS                        │
├─────────────────┬─────────────────┬─────────────────────────────┤
│     OWNER       │      TEAM       │           OTHER             │
├─────────────────┼─────────────────┼─────────────────────────────┤
│ • Full access   │ • Read-only     │ • Info about Sutando only   │
│ • All tools     │ • Sandboxed     │ • No system access          │
│ • System mods   │ • No mutations  │ • Knowledge-base queries    │
│                 │                 │                             │
│      ┌──────────────────┐                ┌──────────────────┐   │
│      │ Normal Execution │                │ Sandboxed Mode   │   │
│      └──────────────────┘                │ codex exec       │   │
│                                          │ --sandbox        │   │
│                                          │ read-only        │   │
│                                          └──────────────────┘   │
└─────────────────┴─────────────────┴─────────────────────────────┘
```

## Task Lifecycle & Priority Management

```
┌─────────────────────────────────────────────────────────────────┐
│                      TASK PRIORITY FLOW                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ ┌─────────────┐ Urgent     ┌─────────────┐ Normal              │
│ │   Voice     │ (instant)  │   Chat/DM   │ (minutes)           │
│ │   Phone     │ ──────────▶│   Discord   │ ──────────┐         │
│ └─────────────┘            │   Telegram  │           │         │
│                            └─────────────┘           │         │
│                                                      ▼         │
│ ┌─────────────┐ Low                    ┌─────────────────────┐ │
│ │   Cron      │ (background)           │   Task Queue        │ │
│ │   Health    │ ──────────────────────▶│   (FIFO + Priority) │ │
│ │   Proactive │                        └─────────────────────┘ │
│ └─────────────┘                                  │             │
│                                                  ▼             │
│                               ┌─────────────────────────────┐   │
│                               │    Execution Engine         │   │
│                               │                             │   │
│                               │ • Timeout: 10 min default   │   │
│                               │ • Archive on timeout        │   │
│                               │ • Orphan recovery           │   │
│                               └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Voice Agent Architecture Detail

```
┌─────────────────────────────────────────────────────────────────┐
│                     VOICE AGENT FLOW                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ User Speech ──▶ Browser WebSocket (:8080) ──▶ Voice Agent (:9900) │
│                                                     │           │
│                ┌─────────────────────────────────────┘           │
│                ▼                                                │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Gemini Live Session                        │    │
│  │                                                         │    │
│  │ • Real-time voice processing                            │    │
│  │ • Tool calling                                          │    │
│  │ • Context maintenance                                   │    │
│  │ • Error classification                                  │    │
│  └─────────────────────────────────────────────────────────┘    │
│                │                                                │
│                ▼                                                │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Decision Point                              │    │
│  │                                                         │    │
│  │ Inline Tool?  ──Yes──▶ Execute immediately              │    │
│  │      │                                                  │    │
│  │      No                                                 │    │
│  │      ▼                                                  │    │
│  │ Write task file ──▶ Task Bridge ──▶ Claude Code        │    │
│  │      │                                                  │    │
│  │      ▼                                                  │    │
│  │ Watch for result ──▶ Inject into Gemini conversation    │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## Observability & Health Monitoring

```
┌─────────────────────────────────────────────────────────────────┐
│                   MONITORING & OBSERVABILITY                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐   │
│ │ Core Heartbeat  │  │ Health Check    │  │ Dashboard       │   │
│ │                 │  │                 │  │                 │   │
│ │ Every 30s:      │  │ Every 5 min:    │  │ Real-time:      │   │
│ │ • alive file    │  │ • Service status│  │ • Task queue    │   │
│ │ • PID tracking  │  │ • Port checks   │  │ • Active work   │   │
│ │ • Status update │  │ • Memory usage  │  │ • System load   │   │
│ └─────────────────┘  └─────────────────┘  └─────────────────┘   │
│          │                    │                    │           │
│          └────────────────────┼────────────────────┘           │
│                               ▼                                │
│              ┌─────────────────────────────────┐               │
│              │    state/core-status.json       │               │
│              │                                 │               │
│              │ • Current work status           │               │
│              │ • Last activity timestamp       │               │
│              │ • Service health indicators     │               │
│              └─────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────┘
```

## Key Insights

1. **File-Based Decoupling**: All communication between components uses files, ensuring restart-safety and debuggability

2. **Priority-Based Processing**: Voice tasks get immediate attention, chat tasks are queued normally, background tasks run when idle

3. **Layered Architecture**: Simple inline tools for instant responses, complex skills for heavy lifting

4. **Multi-Channel Unity**: All input channels converge to the same task format, all output goes through the same result mechanism

5. **Extensible Design**: Adding new channels only requires implementing the task/result file protocol

6. **Safety by Design**: Access controls, timeouts, sandboxing, and archival ensure system stability and security

This architecture allows Sutando to act as a unified AI agent interface that can scale across multiple input modalities while maintaining a clean separation of concerns and robust error handling.