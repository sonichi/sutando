# Windows and macOS capability support

This is the maintained platform-support matrix for Sutando's source checkout.
The [built-in tools catalog](built-in-tools.md) describes how to use capabilities;
this document records their platform limits and the evidence behind support claims.
[Tracker #5499](https://github.com/sonichi/sutando/issues/5499) owns the parity
backlog. Establishing this matrix does not close that tracker.

## Reading the evidence

Source baseline: `e50b019a472a38bfcb201e9bc9af51796ead8fcb`, inspected 2026-10-27.
Native checks below ran on Windows 11 build 26300, PowerShell 7.6.8, Node 24.19.0,
and Python 3.13.7. No macOS host or packaged Sutando installation was exercised.
The [Windows quickstart](../README.md#windows-support) lists installation requirements;
the tested versions here are evidence for one host, not new minimum versions.

- **Implemented:** an adapter exists; this alone does not establish live operation.
- **Partial:** an implemented path covers only part of the user outcome.
- **Unsupported:** the named path rejects the platform or needs a macOS-only backend.
- **Needs audit:** installation, dependencies, permissions, or alternatives remain unverified.
- **Native fixture:** production code used real Windows APIs/processes and disposable data.
- **Contract only:** subprocesses, process tables, or OS operations were mocked. Running
  such a test on Windows does not make it a desktop or service round trip.

The baseline checks used synthetic files and isolated test workspaces. They did
not start a bot with real credentials, send messages, capture the user's screen,
alter the clipboard, or interrupt a deployed service.

## Core and desktop matrix

| User outcome | macOS implementation | Windows status and limits | Verification at the baseline |
|---|---|---|---|
| Browser voice, web client, dashboard, Agent API | Shared services, macOS startup | Implemented by [startup.ps1](../src/startup.ps1); voice also needs the configured provider and browser microphone permission | Needs a post-restart voice/message round trip; startup argument tests are not this proof |
| Task-file watching | Shell watcher path | Implemented PowerShell `FileSystemWatcher` | [Native watcher fixture](../tests/windows-watcher-live.test.ps1) emits existing, created and atomically renamed tasks once; ignores repeat writes/non-tasks and verifies its PID sentinel |
| Task dispatch | CLI watcher/Monitor path | Implemented [task-dispatcher.ps1](../src/task-dispatcher.ps1); separate one-shot task architecture | Security divergence remains tracked in [#5459](https://github.com/sonichi/sutando/issues/5459); no non-owner isolation claim from parity tests |
| Startup, restart and stop | Shell lifecycle scripts | Implemented PowerShell scripts; lifecycle ownership and installed supervision need per-service verification | Native child argument/Unicode checks pass; restart stop plans are contract only |
| Workspace paths with spaces and Unicode | Shared workspace contract | Implemented in the PowerShell/Python resolver | Native fixture passes under deliberately conflicting cp1252/ASCII settings; caller encoding restored |
| Delivery claims and process identity | POSIX locks and platform process probes | Implemented Windows process handles and locks | Native contention, owner identity, death and claim tests pass; gateway import regression below prevents a blanket gateway claim |
| AG2 Space gateway / Sparrow | Shared packaged gateway | Blocked at this baseline: importing either entry point raises `TypeError` in the quarantine libc probe on Windows | `outbox-windows.test.py`: 18 methods ran; 17 passed, one failed in two gateway-import subcases |
| Telegram, Discord and Slack | Shared bridges | Implemented/documented; each bridge's authorization and delivery require independent validation | No real-account delivery tested; Discord interpreter fallback passes with controlled interpreter responses |
| Still screen capture and description | Native `screencapture`, shared description pipeline | Implemented [System.Drawing backend](../src/sutando_platform.py); description additionally requires its model/service | Source audited; no desktop pixels captured in this verification |
| Display inventory and per-display capture | Native display indices and separate frames | Partial: [capture server](../src/screen-capture-server.py) exposes a virtual screen; `display`/`all` fall back to virtual-screen capture | Source audited; physical/multiple-monitor behavior unverified |
| Image resizing | `sips` | Implemented System.Drawing resize in [platform.ts](../src/platform.ts) | Native synthetic 1200×300 PNG becomes a 400×100 JPEG through a Unicode/apostrophe path |
| Clipboard and desktop notifications | `pbcopy`/`pbpaste`, AppleScript notification | Implemented PowerShell clipboard and balloon notification; Python/TypeScript clipboard contracts still need reconciliation | TypeScript clipboard Unicode/newline/literal-input contract passes with mocked OS calls; clipboard/notification UX unverified |
| Open a file or URL with the default application | `open` | Implemented ShellExecute; `open_file`'s specific-app and fullscreen options remain macOS-only | Literal-path and error contracts pass; real default-handler launch unverified |
| Launch/switch application | AppleScript activation | Implemented [Windows launcher](../src/windows-app-launcher.ps1); requires an interactive desktop and verified foreground focus | Win32 binding loads natively from the checkout and a spaced standalone path; focus/selection/error behavior is contract only |
| Voice singleton and cron state locks | POSIX locking | Implemented Windows locks | Native fixture verifies voice lock contention/release and cron-state contention |
| Email, calendar and contacts through connectors | Cloud connectors independent of native apps | Needs per-connector verification; lack of AppleScript does not mean these cloud capabilities are absent | See [catalog connector policy](built-in-tools.md); no connected account exercised |

## Inline automation limits and discovery

These are the precise inline paths inspected, not a claim that Windows cannot
achieve the same outcome through a separate skill, browser API, or connector.
All macOS entries describe source implementations, without fresh native macOS verification.

| Tools / outcome | macOS backend | Windows behavior at the baseline |
|---|---|---|
| `press_key`, `type_text`, `volume`, `brightness`, `toggle_tasks`, `slide_control`, `fullscreen` | System Events / native app automation | `forHostPlatform()` filters these seven names from the inline and owner tool lists; direct handlers also reject non-macOS |
| `scroll`, `switch_tab`, `close_tab`, `open_url`, `click`, `point_at` | Chrome / System Events AppleScript | Still advertised; [browser-tools.ts](../src/browser-tools.ts) returns `macOSOnly`. Default URL opening is a different supported adapter |
| `join_gmeet`, `call_contact` | Chrome / native Contacts behind consent policy | Still advertised; [meeting-tools.ts](../src/meeting-tools.ts) rejects Windows. Opening a meeting URL is not verified joining with intended audio/video |
| `screen_record`, `scroll_and_describe`, `play_video`, `pause_video`, `resume_video`, `replay_video`, `close_video` | Screen recording, Chrome and QuickTime | Still advertised; [recording-tools.ts](../src/recording-tools.ts) rejects Windows |
| `/capture-video` recording start/stop | `screencapture -v`, signal-driven finalization | Unsupported backend: [HTTP recording path](../src/screen-capture-server.py) invokes `screencapture`; Windows still-image capture does not establish video support |
| Sutando menu bar, global context-drop shortcuts | Swift / Cocoa / Carbon | No equivalent in this checkout; use the web UI. This does not describe the separate AG2 Space desktop app |
| Local Calendar, Reminders, Contacts, Mail and Spotlight | [macos-tools skill](../skills/macos-tools/SKILL.md) | Unsupported by that skill; choose local Windows equivalents only where connectors do not meet the outcome |
| Phone/tunnel automatic startup | macOS startup integration | Partial: Windows startup documents experimental/manual setup; shared phone-server capability needs separate verification |

The Windows warning in [voice-agent.ts](../src/voice-agent.ts) still incorrectly
includes `switch_app` among unsupported tools. The launcher exists and its binding
loads on Windows; actual foreground switching remains desktop-dependent.
The host filter is therefore not a complete capability registry: it does not
evaluate every backend, dependency, permission, desktop state, or installed skill.
Manifest tools and optional external skills require a separate inventory per install.

## Reproducible Windows baseline

Run from an isolated checkout using PowerShell 7 and test credentials only.
The checked-in tests create disposable fixtures; none of these commands needs a
live service. `Start-Process:WindowStyle` keeps native fixture subprocesses hidden.

```powershell
npm ci --ignore-scripts
$env:SUTANDO_TELEMETRY = '0'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$PSDefaultParameterValues['Start-Process:WindowStyle'] = 'Hidden'
pwsh -NoProfile -File tests/windows-workspace-encoding.test.ps1
pwsh -NoProfile -File tests/windows-watcher-live.test.ps1
& tests/windows-startup.test.ps1
pwsh -NoProfile -File tests/windows-restart.test.ps1
pwsh -NoProfile -File tests/windows-discord-python.test.ps1
.\node_modules\.bin\tsx.cmd --test --test-force-exit tests/platform-windows-io.test.ts
python tests/open-app-windows.test.py -v
python tests/voice-lock-windows.test.py
python tests/cron-state-lock-windows.test.py -v
python tests/outbox-windows.test.py -v
```

Recorded output, scoped to the commands above:

```text
Workspace resolver: Unicode config path survived cp1252 and ASCII defaults; caller settings restored.
Windows watcher: initial, created and renamed tasks emitted once; changes and non-tasks ignored.
Windows startup: native argv round trips, 2 start-cli launches and 5 installation cases passed.
Windows restart: 4 stop plans and the detached relaunch passed.
Discord collaborator interpreter fallback: 8 cases passed.
platform-windows-io.test.ts: 8 tests passed (1 native resize, 7 mocked contracts).
open-app-windows.test.py: Ran 3 tests; OK (native bindings, controlled app policy).
PASS: Windows voice lock excludes live duplicates and releases
cron-state-lock-windows.test.py: Ran 1 test; OK
outbox-windows.test.py: Ran 18 tests; FAILED (failures=2)
```

The watcher test copies only its production entry point and resolver into a
disposable fixture, with a clean child environment and no live configuration.
The startup test's two core launches and installation decisions are mocked; the
child argument round trips are real. The restart test never stops real services.
The outbox failure is reproducible through both the source gateway wrapper and
the packaged gateway: `_probe_rename()` calls `ctypes.CDLL(None)` before reaching
its existing Windows `os.rename` branch. Preserve this failed evidence until a
fix has its own Windows result; passing lock tests must not hide a failed import.

Follow-up [PR #5963](https://github.com/sonichi/sutando/pull/5963) fixes that
quarantine probe and passes 13 native no-clobber tests. The gateway then reaches
an unrelated unconditional `fcntl` import in `task_hold.py`; full Windows gateway
startup remains blocked and needs its own fix and end-to-end verification.

The [Windows runtime workflow](../.github/workflows/windows-runtime.yml) runs on
schedule, manual dispatch, or the `windows-ci` PR label. It is skipped on ordinary
PRs and is not a required check. A green Ubuntu check cannot replace a Windows
adapter test, and a binding-load test cannot replace desktop interaction.

## Priorities and completion evidence

Priorities remain proposals for maintainers; owners and child issues must be
assigned before treating the roadmap as a delivery commitment.

| Priority | Next bounded outcome | Tracking / required evidence |
|---|---|---|
| P0 | Shared non-owner dispatcher protection | [#5459](https://github.com/sonichi/sutando/issues/5459); native dummy-secret denial plus intended allowed operation |
| P0 | Restore Windows gateway import | Quarantine probe regression above; native source/package import, no-clobber rename and gateway round trip |
| P0 | Accurate capability advertisement and warning | [#5499](https://github.com/sonichi/sutando/issues/5499); audit all built-in/manifest tools, supported alternatives, and explicit missing-dependency/permission errors |
| P1 | Keyboard, mouse, browser navigation and display selection | Small outcome-specific adapters/skills with contract tests and real Windows desktop interaction |
| P1 | Practical meeting joining | Verify joining, audio/video policy, cancellation and permission failures; a URL launch is insufficient |
| P2 | Recording, playback and narrated walkthroughs | Native start/stop/finalization, dependency failures and artifact cleanup |
| P2 | Tray/hotkeys, volume/brightness/fullscreen and supervised lifecycle | Inventory existing [bridge Scheduled Tasks](../src/install-bridge-task.ps1); cron-runner work already exists in [#5509](https://github.com/sonichi/sutando/pull/5509) |
| P3 | Local PIM/search, credentials and distribution | Audit Windows secure storage, file/env auth, executable resolution, architecture and packaged runtime; choose connector vs local integration per user need |

For each delivered item, update this matrix and its catalog `last_verified` date
with the exact commit, OS/runtime, command and result. Keep native fixture,
mocked contract and post-restart live evidence distinct. A live-path change also
needs an input-to-result round trip through the restarted service, with permissions,
dependencies and rollback recorded under [contribution requirements](../CONTRIBUTING.md).
Do not mark a row verified from source inspection or extend one adapter's result
to every bridge, optional skill or packaged build. Directory decoupling is tracked
separately in [#5500](https://github.com/sonichi/sutando/issues/5500).
