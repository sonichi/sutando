import Cocoa
import Carbon
import UserNotifications

// MARK: - Sutando Drop Menu Bar App
// Replaces Automator Quick Action for context drops.
// Global hotkey (Ctrl+Shift+D) captures selected text, clipboard image, or Finder file.

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    // Hotkeys are configurable via ~/.config/sutando/hotkeys.json.
    // Defaults: drop_context=⌃C, drop_screenshot=⌃S, toggle_voice=⌃V, toggle_mute=⌃M
    var hotKeyRefs: [EventHotKeyRef?] = []  // one entry per registered hotkey
    var hotKeyActions: [UInt32: String] = [:]  // hotkey id → action name
    var lastDropTime: Date = .distantPast
    let workspace: String = {
        // Derive from binary location → repo root
        // Raw binary: src/Sutando/Sutando (3 levels up)
        // .app bundle: src/Sutando/Sutando.app/Contents/MacOS/Sutando (5 levels up)
        var url = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0]).resolvingSymlinksInPath()
        // Walk up until we find CLAUDE.md (repo root marker)
        for _ in 0..<8 {
            url = url.deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("CLAUDE.md").path) {
                return url.path
            }
        }
        // Fallback: 3 levels up from binary
        let fallback = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0]).resolvingSymlinksInPath()
        return fallback.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
    }()

    var resultWatchSource: DispatchSourceFileSystemObject?
    var lastResultCount = 0
    // Avatar animation state (PR #418 plumbing → PR #419 consumer).
    // `currentAgentState` caches the last state from /sse-status so
    // `startAnimation`/`stopAnimation` only fire on transitions, not every poll.
    var currentAgentState: String = "idle"
    var animationTimer: Timer?
    var animationPhase: CGFloat = 1.0
    // Presenter-mode state mirrored from iclr-highlight server (port 7877).
    // Updated every 1s in pollPresenterMode; nil-safe if server is down.
    var presenterModeActive: Bool = false
    weak var presenterMenuItem: NSMenuItem?
    // Voice-agent mode state from state/voice-mode.txt sentinel (written by
    // voice-agent.ts on switch_mode / zoom-auto-flip). "active" or "meeting".
    // Combined with presenterModeActive for the composite 3-mode radio —
    // clicking any of {modeActiveMenuItem, modeMeetingMenuItem,
    // modePresenterMenuItem} requests the switch via state/voice-mode.request
    // (for active/meeting) or POST :7877/presenter/on (for presenter).
    var voiceMode: String = "active"
    weak var modeActiveMenuItem: NSMenuItem?
    weak var modeMeetingMenuItem: NSMenuItem?
    weak var modePresenterMenuItem: NSMenuItem?

    /// Fixed tmux socket path for the sutando-core session. The shell
    /// (via startup.sh -S flag) and the app (launched by macOS with a
    /// different TMPDIR due to sandboxing) must target the same socket
    /// to find the same server. Without this, tmux has-session fails
    /// app-side even when the session is alive shell-side.
    let sutandoTmuxSocket = "/tmp/sutando-tmux.sock"

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Self-preventive single-instance: if another Sutando.app is already
        // running (e.g. manual double-launch or leftover from restartSelf()),
        // quit immediately. Prevents the menu-bar-icon ghost stack that
        // plagued 2026-04-21 morning (3 instances accumulated + user saw
        // duplicate icons). Matches path via pgrep $-anchored pattern — same
        // pattern used by health-check.py per feedback_pkill_then_open_race.
        let myPid = ProcessInfo.processInfo.processIdentifier
        let myPath = ProcessInfo.processInfo.arguments[0]
        let pgrep = Process()
        pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        pgrep.arguments = ["-f", "Sutando/Sutando$"]
        let pipe = Pipe()
        pgrep.standardOutput = pipe
        pgrep.standardError = FileHandle.nullDevice
        try? pgrep.run()
        pgrep.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let pids = out.split(separator: "\n").compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
        let others = pids.filter { $0 != myPid }
        if !others.isEmpty {
            NSLog("Sutando: another instance already running (\(others.map(String.init).joined(separator: ","))) — exiting to prevent duplicate menu-bar icons. Path: \(myPath)")
            exit(0)
        }
        // Request notification permission — only when running as .app bundle
        // (UNUserNotificationCenter crashes when run as raw binary)
        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
                NSLog("Sutando: notification permission granted=\(granted) error=\(String(describing: error))")
            }
        }
        DispatchQueue.main.async { [self] in
            setupMenuBar()
            registerHotKey()
            watchResults()
            logToFile("App started, workspace=\(workspace)")
        }
    }

    // MARK: - Result notifications (when voice is not connected)
    func watchResults() {
        let resultsPath = workspace + "/results"
        let fd = open(resultsPath, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: DispatchQueue.global(qos: .utility))
        source.setEventHandler { [weak self] in self?.checkNewResults() }
        source.setCancelHandler { close(fd) }
        source.resume()
        resultWatchSource = source
        lastResultCount = countResults()
    }

    func countResults() -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(atPath: workspace + "/results")
            .filter { $0.hasPrefix("task-") && $0.hasSuffix(".txt") }) ?? []
        return files.count
    }

    func checkNewResults() {
        let newCount = countResults()
        guard newCount > lastResultCount else { lastResultCount = newCount; return }
        lastResultCount = newCount
        // Only notify if voice is NOT connected
        if !isVoiceConnected() {
            let resultsPath = workspace + "/results"
            if let files = try? FileManager.default.contentsOfDirectory(atPath: resultsPath)
                .filter({ $0.hasPrefix("task-") && $0.hasSuffix(".txt") })
                .sorted(by: >),
               let latest = files.first,
               let content = try? String(contentsOfFile: resultsPath + "/" + latest, encoding: .utf8) {
                let preview = String(content.prefix(120)).replacingOccurrences(of: "\n", with: " ")
                DispatchQueue.main.async { [weak self] in self?.notify("Sutando", preview) }
            }
        }
    }

    func isVoiceConnected() -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/lsof")
        proc.arguments = ["-i", ":9900", "-sTCP:ESTABLISHED"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return out.contains("ESTABLISHED")
    }

    // MARK: - Menu Bar

    func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            let avatarPath = workspace + "/assets/stand-avatar.png"
            if let image = NSImage(contentsOfFile: avatarPath) {
                image.size = NSSize(width: 18, height: 18)
                image.isTemplate = false
                button.image = image
            } else {
                button.title = "S"
                button.font = NSFont.systemFont(ofSize: 14, weight: .bold)
            }
        }

        let menu = NSMenu()
        // Build menu items from the loaded hotkey config so labels stay in sync
        // with whatever's actually registered (config or defaults).
        let hotkeys = loadHotkeyConfig()
        let actionToSelector: [String: (String, Selector)] = [
            "drop_context":    ("Drop Context",    #selector(dropContext)),
            "drop_screenshot": ("Drop Screenshot", #selector(dropScreenshot)),
            "toggle_voice":    ("Toggle Voice",    #selector(toggleVoice)),
            "toggle_mute":     ("Toggle Mute",     #selector(toggleMute)),
        ]
        for hk in hotkeys {
            guard let (label, sel) = actionToSelector[hk.action] else { continue }
            let glyph = displayLabel(key: hk.key, modifiers: hk.modifiers)
            menu.addItem(NSMenuItem(title: "\(label) (\(glyph))", action: sel, keyEquivalent: ""))
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Open Web UI", action: #selector(openWebUI), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Open Core CLI", action: #selector(openCore), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        // Three-mode radio: Active / Meeting / Presenter. Exactly one has
        // ● at a time (the current composite mode). Clicking switches.
        // Active & Meeting → write state/voice-mode.request, voice-agent
        // polls+applies in ~1s. Presenter → POST :7877/presenter/on.
        let activeItem = NSMenuItem(title: "  Mode: Active", action: #selector(switchToActive), keyEquivalent: "")
        activeItem.target = self
        menu.addItem(activeItem)
        modeActiveMenuItem = activeItem

        let meetingItem = NSMenuItem(title: "  Mode: Meeting", action: #selector(switchToMeeting), keyEquivalent: "")
        meetingItem.target = self
        menu.addItem(meetingItem)
        modeMeetingMenuItem = meetingItem

        let presenterItem = NSMenuItem(title: "  Mode: Presenter", action: #selector(switchToPresenter), keyEquivalent: "")
        presenterItem.target = self
        menu.addItem(presenterItem)
        modePresenterMenuItem = presenterItem
        // Back-compat: presenterMenuItem still referenced by avatar-badge
        // re-render path. Point it at the presenter radio item so its
        // title toggle still works, though the composite update is handled
        // by updateModeMenuItem() now.
        presenterMenuItem = presenterItem
        menu.addItem(NSMenuItem.separator())
        // Loop pause/resume — proactive-loop's skip-conditions check
        // `state/loop-paused-until.sentinel` (per skills/proactive-loop/SKILL.md
        // Skip Conditions §(d)). Pause writes a future-dated sentinel; Resume
        // deletes it. Sentinel format: ISO-8601 expiry timestamp (UTC).
        // Auto-expires so a forgotten pause re-enables itself.
        menu.addItem(NSMenuItem(title: "Pause Loop (30 min)", action: #selector(pauseLoop30), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Resume Loop", action: #selector(resumeLoop), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Restart Core CLI", action: #selector(restartCore), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Restart All Services", action: #selector(restartServices), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Stop All Services", action: #selector(stopServices), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Restart Sutando App", action: #selector(restartSelf), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu

        // Poll mute/voice state every 1 second. Previously 3s, but the seeing
        // flash is a transient tool state (TTL ~3s) and a 3s poll has <50%
        // probability of landing inside the TTL window — Chi saw seeing
        // "happen long after" because the first flash was missed entirely.
        // 1s makes the catch deterministic.
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.pollMuteState()
        }

        // Watcher health: every 30s, verify the task watcher is running.
        // If it's dead AND there are pending tasks AND it's been >60s since
        // we last intervened, restart it and fire a notification. Chi's ask
        // 2026-04-18: "can the app remind the CLI about watcher" — this
        // goes one better by auto-restarting so no reminder is needed.
        Timer.scheduledTimer(withTimeInterval: 30.0, repeats: true) { [weak self] _ in
            self?.checkWatcher()
        }

        // Contextual chips: every 120s, refresh contextual-chips.json from
        // cheap mechanical sources (open PRs, top pending question, recent
        // results). No LLM round-trip. Replaces the (never-shipped) draft
        // /personal-reactive-loop skill — the cadence is purely mechanical
        // polling, so the natural home is the menu-bar app that already
        // does watcher liveness. Per Chi's review 2026-05-05: "if it's only
        // scripts, can it be merged with the sutando app?"
        Timer.scheduledTimer(withTimeInterval: 120.0, repeats: true) { [weak self] _ in
            self?.refreshContextualChips()
        }
        // Also fire once at startup so the chip set isn't stale-from-yesterday
        // until the first 120s tick.
        DispatchQueue.global(qos: .background).async { [weak self] in
            self?.refreshContextualChips()
        }

        // Health-check: every 30min, run health-check.py --fix and append
        // to logs/health-check.log. Same pattern as watcher-liveness +
        // chips. Replaces ~/Library/LaunchAgents/com.sutando.health-check
        // .plist (retired in the same change set per trio-design-current
        // .md "Health-check ownership"). After this binary ships:
        //   launchctl bootout gui/$UID/com.sutando.health-check
        //   rm ~/Library/LaunchAgents/com.sutando.health-check.plist
        Timer.scheduledTimer(withTimeInterval: 1800.0, repeats: true) { [weak self] _ in
            self?.runHealthCheck()
        }
        // Fire once at startup so a fresh check is captured immediately
        // rather than waiting 30min.
        DispatchQueue.global(qos: .background).async { [weak self] in
            self?.runHealthCheck()
        }

        // Presenter mode: poll iclr-highlight server for on/off state.
        // Keeps menu item + tooltip fresh; silent if server is down.
        // Also rechecks voice-agent mode sentinel on the same tick.
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.pollPresenterMode()
            self?.pollVoiceMode()
        }
        // Render the initial mode bullet on the dropdown. Without this, the
        // first pollVoiceMode() tick bails early when sentinel matches the
        // default `voiceMode = "active"`, leaving the menu items at their
        // creation-time titles ("  Mode: Active") with no `●` marker. Caught
        // 2026-05-05 — Chi reported the dot-next-to-Active was gone after a
        // restart that landed in active mode (the common case). Calling
        // updateModeMenuItem() once at end-of-launch makes the bullet appear
        // immediately regardless of whether mode ever changes.
        updateModeMenuItem()
    }

    // Write state/voice-mode.request for voice-agent to pick up on its 1s
    // poll (see voice-agent.ts applyModeRequest). Nil-safe if workspace
    // derivation failed — the app just logs and the user retries.
    func requestVoiceMode(_ mode: String) {
        let path = workspace + "/state/voice-mode.request"
        let dir = workspace + "/state"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        do {
            try mode.write(toFile: path, atomically: true, encoding: .utf8)
        } catch {
            NSLog("Sutando: requestVoiceMode(\(mode)) write failed: \(error.localizedDescription)")
        }
    }

    @objc func switchToActive() {
        // If currently in presenter, clear presenter first so the mode radio
        // reflects the click immediately (otherwise presenter wins composite).
        if presenterModeActive { setPresenter(on: false) }
        requestVoiceMode("active")
    }

    @objc func switchToMeeting() {
        if presenterModeActive { setPresenter(on: false) }
        requestVoiceMode("meeting")
    }

    @objc func switchToPresenter() {
        setPresenter(on: true)
    }

    func setPresenter(on: Bool) {
        let target = on ? "on" : "off"
        guard let url = URL(string: "http://localhost:7877/presenter/\(target)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: req) { _, _, err in
            if let err = err {
                NSLog("Sutando: presenter toggle failed: \(err.localizedDescription)")
            }
        }.resume()
    }

    @objc func togglePresenterMode() {
        // Flip server-side state; the 1s pollPresenterMode tick will refresh
        // menu title + avatar badge once the POST returns.
        let target = presenterModeActive ? "off" : "on"
        guard let url = URL(string: "http://localhost:7877/presenter/\(target)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: req) { _, _, err in
            if let err = err {
                NSLog("Sutando: presenter toggle failed: \(err.localizedDescription)")
            }
        }.resume()
    }

    func pollPresenterMode() {
        guard let url = URL(string: "http://localhost:7877/presenter") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.8
        let task = URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            guard let self = self else { return }
            var active = false
            if let data = data, error == nil,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                active = (json["active"] as? Bool) ?? false
            }
            DispatchQueue.main.async {
                guard self.presenterModeActive != active else { return }
                self.presenterModeActive = active
                self.presenterMenuItem?.title = active ? "● Presenter: ON (click to turn off)" : "Presenter: OFF (click to turn on)"
                self.updateModeMenuItem()
                // Avatar badge re-renders on next pollMuteState tick (≤1s).
            }
        }
        task.resume()
    }

    func pollVoiceMode() {
        // Read state/voice-mode.txt sentinel written by voice-agent.ts.
        // Falls back to "active" if file missing (first boot / voice-agent down).
        let sentinel = workspace + "/state/voice-mode.txt"
        var newMode = "active"
        if let contents = try? String(contentsOfFile: sentinel, encoding: .utf8) {
            let trimmed = contents.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed == "meeting" || trimmed == "active" {
                newMode = trimmed
            }
        }
        DispatchQueue.main.async {
            guard self.voiceMode != newMode else { return }
            self.voiceMode = newMode
            self.updateModeMenuItem()
        }
    }

    func updateModeMenuItem() {
        // Composite: presenter > meeting > active (higher priority wins).
        // Radio-style marker: ● on the active item, "  " (two spaces) on
        // the others to keep titles aligned.
        let active: String
        if presenterModeActive { active = "presenter" }
        else if voiceMode == "meeting" { active = "meeting" }
        else { active = "active" }
        modeActiveMenuItem?.title    = (active == "active"    ? "● " : "  ") + "Mode: Active"
        modeMeetingMenuItem?.title   = (active == "meeting"   ? "● " : "  ") + "Mode: Meeting"
        modePresenterMenuItem?.title = (active == "presenter" ? "● " : "  ") + "Mode: Presenter"
    }

    var lastWatcherAlert: Date = .distantPast
    func checkWatcher() {
        // pgrep -f watch-tasks
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        proc.arguments = ["-f", "watch-tasks"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return }
        proc.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        if !out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return  // watcher alive
        }

        // Read CLI's REAL status BEFORE alerting. If Claude Code is currently
        // working (has an active Bash/tool child process under its pane),
        // skip the alert — the CLI will handle the restart in the normal
        // proactive-loop Step 9 without us spamming its stdin with
        // 'watcher' keystrokes. Only alert when the CLI is genuinely idle
        // (waiting on user input). Chi's ask: "does the app read the real
        // state first? and remind about the watcher only when idle?"
        if cliIsWorking() {
            logToFile("watcher dead; CLI is working — skipping alert")
            return
        }

        // Skip when "watcher" is already queued in the CLI input buffer.
        // claude-code queues keystrokes during a turn and processes them
        // when the turn ends. cliIsWorking() catches fresh (<60s) tool
        // children, but a long-running tool (>60s) returns false here —
        // the next watcher tick would then double-send "watcher", so the
        // CLI processes "watcher\nwatcher" serially and spawns watcher
        // twice. Capture-pane the bottom of the pane and skip if
        // "watcher" appears near the prompt area.
        if watcherKeystrokesQueued() {
            logToFile("watcher dead; 'watcher' already queued in pane — skipping send")
            return
        }

        // Throttle: don't alert more than once every 120s so the CLI doesn't
        // get flooded if it's slow to restart.
        if Date().timeIntervalSince(lastWatcherAlert) < 120 { return }
        lastWatcherAlert = Date()

        // If Claude Code is running inside the `sutando-core` tmux session
        // (launch via scripts/start-cli.sh), send the word `watcher` to
        // its pane as if Chi typed it. The CLI parses that as a restart
        // prompt and starts the watcher via its own run_in_background Bash
        // — so the watcher's stdout routes through the task-notification
        // pipe correctly. Any externally-started watcher (nohup etc.)
        // has stdout → /dev/null and is useless.
        if tmuxSendKeys(session: "sutando-core", keys: "watcher") {
            notify("Sutando", "Task watcher down — sent 'watcher' to sutando-core tmux")
            logToFile("watcher dead; tmux send-keys to sutando-core")
            return
        }

        // Fallback: Claude Code isn't in the expected tmux session.
        // Notify so Chi can restart manually.
        notify("Sutando", "Task watcher is down — prompt the CLI to restart it (or start CLI via scripts/start-cli.sh)")
        logToFile("watcher dead; notification fired (tmux session not found)")
    }

    /// Refresh `contextual-chips.json` from cheap mechanical sources. No LLM
    /// round-trip — just shell-out to `gh pr list`, read top `## Title` line
    /// of `pending-questions.md`, scan `results/` for unread items. Atomic
    /// write via tmp + replaceItem. Fires every 120s + once at startup. The
    /// web UI polls the file and pins matching chips at the top of the
    /// starter tab.
    func refreshContextualChips() {
        var chips: [[String: String]] = []

        // 1. Open PRs authored by sonichi (both bots commit under this account).
        // Resolve gh path explicitly — apps launched via `open` inherit a
        // minimal PATH (no /opt/homebrew or /usr/local) so `/usr/bin/env gh`
        // wouldn't find the binary. Fall through Apple-Silicon then Intel.
        let ghPath: String? = {
            for p in ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"] {
                if FileManager.default.fileExists(atPath: p) { return p }
            }
            return nil
        }()
        if let gh = ghPath,
           // Pass --repo explicitly: the app's CWD when launched via `open`
           // is the user's home directory (not a git repo), so without
           // --repo, gh fails with "fatal: not a git repository".
           // No --author filter — community PRs (e.g. #594 Jason, #593 Vasiliy)
           // are also chip-worthy. Both bots commit as sonichi so this still
           // surfaces fleet PRs, plus catches external contributions for triage.
           let prJSON = runShell(gh, ["pr", "list", "--repo", "sonichi/sutando", "--state", "open", "--limit", "5", "--json", "number,title"]),
           let prData = prJSON.data(using: .utf8),
           let prs = try? JSONSerialization.jsonObject(with: prData) as? [[String: Any]] {
            for pr in prs.prefix(3) {
                let n = (pr["number"] as? NSNumber)?.intValue
                if let n = n, let t = pr["title"] as? String {
                    let title = t.count > 60 ? String(t.prefix(57)) + "..." : t
                    chips.append(["label": "Review PR #\(n)", "desc": title])
                }
            }
        }

        // 2. Top pending question (read first `## Title` line of pending-questions.md).
        let pqPath = workspace + "/pending-questions.md"
        if let pq = try? String(contentsOfFile: pqPath, encoding: .utf8) {
            // Skip the leading "# Memory" or similar h1, find first h2.
            for line in pq.split(separator: "\n") {
                if line.hasPrefix("## ") {
                    let title = String(line.dropFirst(3))
                    let preview = title.count > 60 ? String(title.prefix(57)) + "..." : title
                    chips.append(["label": "Pending: \(preview)", "desc": "Resolve in pending-questions.md"])
                    break
                }
            }
        }

        // 3. Most recent unread result (results/task-*.txt newest mtime).
        let resultsDir = workspace + "/results"
        if let entries = try? FileManager.default.contentsOfDirectory(atPath: resultsDir) {
            let taskResults = entries
                .filter { $0.hasPrefix("task-") && $0.hasSuffix(".txt") }
                .compactMap { name -> (String, Date)? in
                    let path = resultsDir + "/" + name
                    guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
                          let mtime = attrs[.modificationDate] as? Date else { return nil }
                    return (name, mtime)
                }
                .sorted { $0.1 > $1.1 }
            if let latest = taskResults.first {
                // Only show if it landed in the last 10 minutes — older
                // results are no longer "unread" by reasonable definition.
                if Date().timeIntervalSince(latest.1) < 600 {
                    chips.append(["label": "Recent result", "desc": latest.0])
                }
            }
        }

        // Serialize + atomic write via tmp+replaceItem.
        let payload: [String: Any] = ["chips": chips, "ts": Int(Date().timeIntervalSince1970)]
        guard let json = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) else { return }
        let dst = URL(fileURLWithPath: workspace + "/contextual-chips.json")
        let tmp = URL(fileURLWithPath: workspace + "/contextual-chips.json.tmp")
        do {
            try json.write(to: tmp, options: [.atomic])
            _ = try FileManager.default.replaceItemAt(dst, withItemAt: tmp)
        } catch {
            // Best-effort. Cleanup tmp if rename failed.
            try? FileManager.default.removeItem(at: tmp)
        }
    }

    /// Run an executable, capture stdout as String. Returns nil on failure
    /// or non-zero exit. Used by refreshContextualChips for `gh` / `gws` /
    /// other CLI shell-outs that are mechanical and need no LLM judgment.
    func runShell(_ path: String, _ args: [String]) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = args
        // Inherit parent env; also force PATH to include homebrew so child
        // tools that themselves shell-out (e.g. `gh` invoking `git`) find
        // their own deps. Apps launched via `open` get a minimal PATH
        // that excludes /opt/homebrew/bin → gh can't find git → exits non-zero.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        proc.environment = env
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        do { try proc.run() } catch { return nil }
        proc.waitUntilExit()
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        // errData is intentionally read to drain the pipe (avoid SIGPIPE)
        // even though we don't surface it on success.
        _ = errPipe.fileHandleForReading.readDataToEndOfFile()
        if proc.terminationStatus != 0 { return nil }
        return String(data: outData, encoding: .utf8)
    }

    /// True if Claude Code in the sutando-core tmux pane has any running
    /// child process — indicating an active Bash/Tool call. False if only
    /// the claude process itself is running (idle, waiting on stdin) or
    /// if the tmux session can't be found.
    func cliIsWorking() -> Bool {
        let tmuxPath: String
        if FileManager.default.fileExists(atPath: "/opt/homebrew/bin/tmux") {
            tmuxPath = "/opt/homebrew/bin/tmux"
        } else if FileManager.default.fileExists(atPath: "/usr/local/bin/tmux") {
            tmuxPath = "/usr/local/bin/tmux"
        } else {
            return false
        }
        // Get the pane's PID (the interactive shell wrapping claude).
        // -S sutandoTmuxSocket so we find the same tmux server startup.sh
        // created (different TMPDIR between shell and sandboxed .app).
        let list = Process()
        list.executableURL = URL(fileURLWithPath: tmuxPath)
        list.arguments = ["-S", sutandoTmuxSocket, "list-panes", "-t", "sutando-core", "-F", "#{pane_pid}"]
        let pipe = Pipe()
        list.standardOutput = pipe
        list.standardError = FileHandle.nullDevice
        do { try list.run() } catch { return false }
        list.waitUntilExit()
        if list.terminationStatus != 0 { return false }
        let panePid = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if panePid.isEmpty { return false }

        // pgrep descendants of the pane PID. Claude Code itself is a child
        // of the shell; its tool invocations are grandchildren. We want
        // any non-claude descendant — a running bash/tool/subprocess.
        // tmux launches the pane command directly — no intermediate shell.
        // So `pane_pid` in a startup.sh-wrapped setup IS the claude process,
        // and its DIRECT children are tool-call subprocesses + long-lived
        // plugin helpers (sourcekit-lsp, caffeinate, bun, npm exec, etc.).
        // The age filter distinguishes: a child with etime < 60s is a
        // fresh tool call; older ones are background services that don't
        // indicate active work.
        let list2 = Process()
        list2.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        list2.arguments = ["-P", panePid]
        let listPipe = Pipe()
        list2.standardOutput = listPipe
        list2.standardError = FileHandle.nullDevice
        do { try list2.run() } catch { return false }
        list2.waitUntilExit()
        let children = String(data: listPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .split(separator: "\n").map(String.init) ?? []
        for childPid in children where !childPid.isEmpty {
            if processAgeSeconds(pid: childPid) < 60 {
                return true  // fresh child under pane_pid → active tool call
            }
        }
        return false
    }

    /// Parse `ps -o etime= -p <pid>` → seconds. Returns Int.max on any
    /// parse failure so old processes stay "old" and don't false-trigger
    /// the cliIsWorking heuristic.
    func processAgeSeconds(pid: String) -> Int {
        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-o", "etime=", "-p", pid]
        let pipe = Pipe()
        ps.standardOutput = pipe
        ps.standardError = FileHandle.nullDevice
        do { try ps.run() } catch { return Int.max }
        ps.waitUntilExit()
        if ps.terminationStatus != 0 { return Int.max }
        // etime format: [DD-]HH:MM:SS | [HH:]MM:SS | MM:SS
        var raw = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        raw = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty { return Int.max }
        var days = 0
        var rest = raw
        if let dashIdx = rest.firstIndex(of: "-") {
            days = Int(rest[..<dashIdx]) ?? 0
            rest = String(rest[rest.index(after: dashIdx)...])
        }
        let parts = rest.split(separator: ":").compactMap { Int($0) }
        switch parts.count {
        case 2: return days * 86400 + parts[0] * 60 + parts[1]
        case 3: return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
        default: return Int.max
        }
    }

    /// Send keystrokes to a tmux pane. Returns true if the session exists
    /// and send-keys succeeded. False otherwise — caller should fall back
    /// to a macOS notification.
    func tmuxSendKeys(session: String, keys: String) -> Bool {
        // Find tmux binary: Homebrew on Apple Silicon, /usr/local on Intel.
        let tmuxPath: String
        if FileManager.default.fileExists(atPath: "/opt/homebrew/bin/tmux") {
            tmuxPath = "/opt/homebrew/bin/tmux"
        } else if FileManager.default.fileExists(atPath: "/usr/local/bin/tmux") {
            tmuxPath = "/usr/local/bin/tmux"
        } else {
            return false
        }
        // Check session exists: `tmux has-session -t <name>` exits 0 if alive.
        let has = Process()
        has.executableURL = URL(fileURLWithPath: tmuxPath)
        has.arguments = ["-S", sutandoTmuxSocket, "has-session", "-t", session]
        has.standardOutput = FileHandle.nullDevice
        has.standardError = FileHandle.nullDevice
        do { try has.run() } catch { return false }
        has.waitUntilExit()
        if has.terminationStatus != 0 { return false }

        // Session exists — send keys + Enter.
        let send = Process()
        send.executableURL = URL(fileURLWithPath: tmuxPath)
        send.arguments = ["-S", sutandoTmuxSocket, "send-keys", "-t", session, keys, "Enter"]
        send.standardOutput = FileHandle.nullDevice
        send.standardError = FileHandle.nullDevice
        do { try send.run() } catch { return false }
        send.waitUntilExit()
        return send.terminationStatus == 0
    }

    /// Detect whether the word "watcher" is already typed at claude-code's
    /// CURRENT prompt line in the sutando-core pane. Only the current prompt
    /// (the bottom-most `❯ ` line) indicates queued input — past prompts in
    /// scrollback don't.
    ///
    /// History of this function:
    /// - PR #553: matched `\bwatcher\b` across bottom 5 lines → over-fired
    ///   on prose like "Ensure the watcher is running" in tool output.
    /// - PR #557: filtered to lines starting with `❯ `. But `capture-pane
    ///   -S -3` returns the visible pane PLUS scrollback (≠ "last 3 lines"),
    ///   so old prompts like `❯ why is watcher reminder not sent?` were
    ///   still treated as queued input → still over-fired.
    /// - This PR: walk all lines, remember the LAST `❯ ` line seen (the
    ///   current prompt), check only that one.
    ///
    /// Returns false on any tmux failure so a missing tmux doesn't suppress
    /// alerts.
    func watcherKeystrokesQueued() -> Bool {
        let tmuxPath: String
        if FileManager.default.fileExists(atPath: "/opt/homebrew/bin/tmux") {
            tmuxPath = "/opt/homebrew/bin/tmux"
        } else if FileManager.default.fileExists(atPath: "/usr/local/bin/tmux") {
            tmuxPath = "/usr/local/bin/tmux"
        } else {
            return false
        }
        let cap = Process()
        cap.executableURL = URL(fileURLWithPath: tmuxPath)
        cap.arguments = ["-S", sutandoTmuxSocket, "capture-pane", "-t", "sutando-core", "-p"]
        let pipe = Pipe()
        cap.standardOutput = pipe
        cap.standardError = FileHandle.nullDevice
        do { try cap.run() } catch { return false }
        cap.waitUntilExit()
        if cap.terminationStatus != 0 { return false }
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        // Find the LAST line starting with "❯" — that's the current prompt.
        // Past prompts in scrollback don't represent queued input.
        //
        // Match "❯" without requiring a trailing space: an EMPTY prompt is
        // rendered as `❯ ` (prompt + space), but `trimmingCharacters` strips
        // the trailing space → we'd miss the empty prompt and fall back to
        // an earlier prompt-with-text in scrollback. Bug from PR #559 that
        // caused continuous "queued in pane — skipping send" even on empty
        // prompt. Fix: trim only LEADING whitespace; check `❯` prefix; the
        // input portion is whatever follows.
        var lastPromptInput: String? = nil
        for line in out.split(separator: "\n") {
            // Trim only leading whitespace (not trailing) so empty prompt
            // `❯ ` is preserved as `❯ ` (prompt + space + nothing).
            let leading = line.drop(while: { $0 == " " || $0 == "\t" })
            if leading.hasPrefix("❯") {
                // Drop the prompt char + any single space that follows it.
                var rest = leading.dropFirst()  // drop "❯"
                if rest.hasPrefix(" ") { rest = rest.dropFirst() }  // drop one space if present
                lastPromptInput = String(rest)
            }
        }
        guard let input = lastPromptInput else { return false }
        return input.range(of: #"\bwatcher\b"#, options: .regularExpression) != nil
    }

    /// Return the avatar image, badged per composite mode:
    ///   presenter  → purple dot (#6a1b9a), matches web UI presenter badge
    ///   meeting    → amber dot  (#b26a00), matches web UI meeting badge
    ///   active     → no badge
    /// Composited onto the top-right corner of the 18×18 avatar so the
    /// menu bar continuously signals mode without taking an extra slot.
    func avatarImage(presenterActive: Bool, meetingActive: Bool = false) -> NSImage? {
        let avatarPath = workspace + "/assets/stand-avatar.png"
        guard let base = NSImage(contentsOfFile: avatarPath) else { return nil }
        base.size = NSSize(width: 18, height: 18)
        base.isTemplate = false
        // Composite priority: presenter > meeting > active (none).
        let dotColor: NSColor?
        if presenterActive {
            dotColor = NSColor(red: 0.416, green: 0.106, blue: 0.604, alpha: 1.0)  // purple
        } else if meetingActive {
            dotColor = NSColor(red: 0.698, green: 0.416, blue: 0.0, alpha: 1.0)  // amber
        } else {
            dotColor = nil
        }
        guard let color = dotColor else { return base }
        let result = NSImage(size: base.size)
        result.lockFocus()
        base.draw(in: NSRect(origin: .zero, size: base.size))
        let dotR: CGFloat = 4.5
        let dotRect = NSRect(x: base.size.width - dotR * 2 - 0.5, y: base.size.height - dotR * 2 - 0.5, width: dotR * 2, height: dotR * 2)
        color.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
        NSColor.white.setStroke()
        let stroke = NSBezierPath(ovalIn: dotRect)
        stroke.lineWidth = 0.8
        stroke.stroke()
        result.unlockFocus()
        result.isTemplate = false
        return result
    }

    func pollMuteState() {
        guard let url = URL(string: "http://localhost:8080/sse-status") else { return }
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, _, error in
            guard let data = data, error == nil,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let isMuted = json["muted"] as? Bool ?? false
            let isVoiceConnected = json["voiceConnected"] as? Bool ?? false
            // `state` added by PR #418. Absent on pre-#418 servers → default 'idle'.
            let agentState = (json["state"] as? String) ?? "idle"
            // `label` added 2026-04-18 per Chi's "running a tool is not precise":
            // optional specific tool name or core-status step.
            let label = (json["label"] as? String) ?? ""
            DispatchQueue.main.async {
                guard let self = self, let button = self.statusItem.button else { return }
                if isVoiceConnected && isMuted {
                    // Voice active + muted: show mute indicator; stop any animation.
                    // Reset cache so un-mute re-triggers animation if agent is
                    // still non-idle (otherwise the transition guard below would
                    // skip startAnimation() and leave the menu bar statically
                    // dim until the NEXT semantic state change).
                    button.title = "🔇"
                    button.image = nil
                    button.toolTip = "Sutando — muted"
                    self.stopAnimation()
                    self.currentAgentState = "idle"
                } else {
                    // Default state (disconnected or unmuted): show avatar
                    // (badged with a purple dot when presenter mode is active).
                    if let image = self.avatarImage(presenterActive: self.presenterModeActive, meetingActive: self.voiceMode == "meeting") {
                        button.image = image
                        button.title = ""
                    } else {
                        button.title = "S"
                    }
                    button.toolTip = self.tooltipFor(state: agentState, muted: isMuted, voiceConnected: isVoiceConnected, label: label)
                    // When voice is disconnected, only tool-track states
                    // (working / seeing) keep animating — those come from
                    // server-side tool code and mean the core loop or a
                    // screen capture is genuinely doing something. Browser-
                    // track states (listening / speaking) depend on a live
                    // WebSocket and would otherwise animate on stale cached
                    // state. Keeps "the agent is working" visible when
                    // voice is off while fixing the "disconnected but
                    // blinking on stale listening" bug.
                    let effectiveState: String
                    if !isVoiceConnected && (agentState == "listening" || agentState == "speaking") {
                        effectiveState = "idle"
                    } else {
                        effectiveState = agentState
                    }
                    if self.currentAgentState != effectiveState {
                        self.currentAgentState = effectiveState
                        if effectiveState == "idle" {
                            self.stopAnimation()
                        } else {
                            self.startAnimation(for: effectiveState)
                        }
                    }
                }
            }
        }
        task.resume()
    }

    /// Start an opacity pulse with timing tuned to the current agent state.
    /// Each non-idle state gets a distinct signature — interval (speed) +
    /// low opacity (swing depth) — so the menu bar conveys what the agent
    /// is doing without tab-switching.
    ///
    ///   listening  — 0.30s tick, 0.45↔1.00 (gentle slow pulse)
    ///   speaking   — 0.15s tick, 0.70↔1.00 (rapid subtle pulse)
    ///   working    — 0.50s tick, 0.25↔1.00 (slow deep swing, "thinking")
    ///   seeing     — 0.10s tick, 0.55↔1.00 (very fast, "scanning")
    ///
    /// Called on every non-idle state transition (including non-idle →
    /// different non-idle), so the timer is rebuilt with the new signature
    /// whenever the agent state changes.
    func startAnimation(for state: String) {
        animationTimer?.invalidate()
        animationPhase = 1.0

        let interval: TimeInterval
        let lowAlpha: CGFloat
        switch state {
        case "speaking":
            interval = 0.15
            lowAlpha = 0.70
        case "working":
            interval = 0.50
            lowAlpha = 0.25
        case "seeing":
            interval = 0.10
            lowAlpha = 0.55
        default: // "listening" and any future non-idle state
            interval = 0.30
            lowAlpha = 0.45
        }

        animationTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            guard let self = self, let button = self.statusItem.button else { return }
            let midpoint = (lowAlpha + 1.0) / 2.0
            self.animationPhase = self.animationPhase > midpoint ? lowAlpha : 1.0
            button.alphaValue = self.animationPhase
        }
    }

    /// Human-readable tooltip for the menu bar icon. Shows the current
    /// semantic state on hover so the user can verify the visual without
    /// guessing which pulse they're seeing.
    func tooltipFor(state: String, muted: Bool, voiceConnected: Bool, label: String = "") -> String {
        // Tool-track states (working / seeing) describe real server-side
        // activity and apply whether voice is up or not. Showing "voice
        // disconnected" while the icon is pulsing working is misleading —
        // the pulse and the tooltip must tell the same story. When a
        // specific label is provided (tool name or core-status step),
        // it replaces the generic "a tool" text per Chi's "running a
        // tool is not precise" ask.
        let voiceSuffix = voiceConnected ? "" : " (voice off)"
        switch state {
        case "working":
            let what = label.isEmpty ? "a tool" : label
            return "Sutando — running \(what)\(voiceSuffix)"
        case "seeing":
            let what = label.isEmpty ? "your screen" : label
            return "Sutando — reading \(what)\(voiceSuffix)"
        default: break
        }
        if !voiceConnected { return "Sutando — voice disconnected" }
        if muted { return "Sutando — muted" }
        switch state {
        case "listening": return "Sutando — listening"
        case "speaking":  return "Sutando — speaking"
        case "idle":      return "Sutando — idle"
        default:          return "Sutando — \(state)"
        }
    }

    /// Stop the pulse and restore full opacity. Idempotent.
    func stopAnimation() {
        animationTimer?.invalidate()
        animationTimer = nil
        animationPhase = 1.0
        statusItem?.button?.alphaValue = 1.0
    }

    // MARK: - Configurable Global Hotkeys

    /// Map a single-letter key name to a Carbon kVK_* virtual keycode.
    /// Add more entries as needed.
    private static let keyNameToCode: [String: Int] = [
        "A": kVK_ANSI_A, "B": kVK_ANSI_B, "C": kVK_ANSI_C, "D": kVK_ANSI_D,
        "E": kVK_ANSI_E, "F": kVK_ANSI_F, "G": kVK_ANSI_G, "H": kVK_ANSI_H,
        "I": kVK_ANSI_I, "J": kVK_ANSI_J, "K": kVK_ANSI_K, "L": kVK_ANSI_L,
        "M": kVK_ANSI_M, "N": kVK_ANSI_N, "O": kVK_ANSI_O, "P": kVK_ANSI_P,
        "Q": kVK_ANSI_Q, "R": kVK_ANSI_R, "S": kVK_ANSI_S, "T": kVK_ANSI_T,
        "U": kVK_ANSI_U, "V": kVK_ANSI_V, "W": kVK_ANSI_W, "X": kVK_ANSI_X,
        "Y": kVK_ANSI_Y, "Z": kVK_ANSI_Z,
    ]

    /// Map a modifier name to its Carbon mask.
    private static let modifierNameToMask: [String: Int] = [
        "control": controlKey, "ctrl": controlKey, "⌃": controlKey,
        "option":  optionKey,  "alt":  optionKey,  "⌥": optionKey,
        "command": cmdKey,     "cmd":  cmdKey,     "⌘": cmdKey,
        "shift":   shiftKey,   "⇧": shiftKey,
    ]

    /// Default hotkey config used when ~/.config/sutando/hotkeys.json is missing.
    /// Keys: action name → (key letter, modifier names).
    private static let defaultHotkeys: [(action: String, key: String, modifiers: [String])] = [
        ("drop_context",     "C", ["control"]),
        ("drop_screenshot",  "S", ["control"]),
        ("toggle_voice",     "V", ["control"]),
        ("toggle_mute",      "M", ["control"]),
    ]

    private func loadHotkeyConfig() -> [(action: String, key: String, modifiers: [String])] {
        let configPath = NSString(string: "~/.config/sutando/hotkeys.json").expandingTildeInPath
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: configPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            logToFile("loadHotkeyConfig: no config at \(configPath), using defaults")
            return AppDelegate.defaultHotkeys
        }
        var result: [(String, String, [String])] = []
        for (action, value) in json {
            guard let entry = value as? [String: Any],
                  let key = entry["key"] as? String,
                  let mods = entry["modifiers"] as? [String] else {
                logToFile("loadHotkeyConfig: skipping malformed entry for action=\(action)")
                continue
            }
            result.append((action, key.uppercased(), mods))
        }
        if result.isEmpty {
            logToFile("loadHotkeyConfig: empty/unreadable config, using defaults")
            return AppDelegate.defaultHotkeys
        }
        logToFile("loadHotkeyConfig: loaded \(result.count) hotkeys from \(configPath)")
        return result
    }

    private func modifierMask(from names: [String]) -> UInt32 {
        var mask = 0
        for n in names {
            if let m = AppDelegate.modifierNameToMask[n.lowercased()] {
                mask |= m
            }
        }
        return UInt32(mask)
    }

    private func displayLabel(key: String, modifiers: [String]) -> String {
        let modSymbols = modifiers.map { name -> String in
            switch name.lowercased() {
            case "control", "ctrl": return "⌃"
            case "option", "alt":   return "⌥"
            case "command", "cmd":  return "⌘"
            case "shift":           return "⇧"
            default: return name
            }
        }.joined()
        return "\(modSymbols)\(key)"
    }

    func registerHotKey() {
        let hotkeys = loadHotkeyConfig()
        var statuses: [String] = []
        for (idx, hk) in hotkeys.enumerated() {
            guard let keyCode = AppDelegate.keyNameToCode[hk.key] else {
                logToFile("registerHotKey: unknown key '\(hk.key)' for action=\(hk.action)")
                continue
            }
            let id = UInt32(idx + 1)
            var hotKeyID = EventHotKeyID()
            hotKeyID.signature = OSType(0x5355_5444) // "SUTD"
            hotKeyID.id = id
            var ref: EventHotKeyRef?
            let status = RegisterEventHotKey(
                UInt32(keyCode),
                modifierMask(from: hk.modifiers),
                hotKeyID,
                GetApplicationEventTarget(),
                0,
                &ref
            )
            if status != noErr {
                let label = displayLabel(key: hk.key, modifiers: hk.modifiers)
                notify("Sutando", "Failed to register \(label) hotkey for \(hk.action) (error \(status))")
                statuses.append("\(hk.action)=\(status)")
                continue
            }
            hotKeyRefs.append(ref)
            hotKeyActions[id] = hk.action
            statuses.append("\(hk.action)=ok")
        }
        logToFile("registerHotKey: \(statuses.joined(separator: " "))")

        // Install handler — dispatch by action name from the config map.
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { (_, event, _) -> OSStatus in
            var hotKeyID = EventHotKeyID()
            GetEventParameter(event!, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
            let appDelegate = NSApplication.shared.delegate as! AppDelegate
            let action = appDelegate.hotKeyActions[hotKeyID.id] ?? "unknown"
            appDelegate.logToFile("HOTKEY FIRED: id=\(hotKeyID.id) action=\(action)")
            switch action {
            case "drop_context":    appDelegate.dropContext()
            case "drop_screenshot": appDelegate.dropScreenshot()
            case "toggle_voice":    appDelegate.toggleVoice()
            case "toggle_mute":     appDelegate.toggleMute()
            default: break
            }
            return noErr
        }, 1, &eventType, nil, nil)
    }

    // MARK: - Context Drop Logic

    /// Capture frontmost-app context to enrich the dropped task with what the user was looking at.
    /// Returns three fields: app name, frontmost-window title (via Accessibility API), and Chrome
    /// active-tab URL when Chrome is the target. Same skip-Zoom heuristic as the fullscreen tool —
    /// during screen share, Zoom can be frontmost while the user is interacting with another window;
    /// we walk back to the next non-Zoom visible app so the captured context matches user intent.
    private func getFrontmostContext() -> (app: String?, windowTitle: String?, chromeURL: String?) {
        var targetApp: NSRunningApplication? = NSWorkspace.shared.frontmostApplication
        let frontName = targetApp?.localizedName?.lowercased() ?? ""
        if frontName.contains("zoom") {
            // Skip Zoom; pick the next visible regular (non-background) app.
            let candidates = NSWorkspace.shared.runningApplications.filter { app in
                app.activationPolicy == .regular &&
                !(app.localizedName?.lowercased().contains("zoom") ?? false) &&
                app.localizedName != nil
            }
            // Order by launch date descending — the user's most recent non-Zoom app is the best guess.
            targetApp = candidates.max(by: { ($0.launchDate ?? Date.distantPast) < ($1.launchDate ?? Date.distantPast) })
        }
        let appName = targetApp?.localizedName

        // Window title via Accessibility API. Requires PID; cheap if granted, silent fail otherwise.
        var windowTitle: String? = nil
        if let pid = targetApp?.processIdentifier {
            let axApp = AXUIElementCreateApplication(pid)
            var focusedRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &focusedRef) == .success,
               let axWindow = focusedRef {
                var titleRef: CFTypeRef?
                if AXUIElementCopyAttributeValue(axWindow as! AXUIElement, kAXTitleAttribute as CFString, &titleRef) == .success,
                   let title = titleRef as? String, !title.isEmpty {
                    windowTitle = title
                }
            }
        }

        // Chrome active-tab URL via AppleScript — only when Chrome is the target. ~200ms.
        var chromeURL: String? = nil
        if appName == "Google Chrome" {
            let script = "tell application \"Google Chrome\" to return URL of active tab of front window"
            let task = Process()
            task.launchPath = "/usr/bin/osascript"
            task.arguments = ["-e", script]
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = Pipe()
            do {
                try task.run()
                task.waitUntilExit()
                if task.terminationStatus == 0 {
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    let url = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                    if let url = url, !url.isEmpty { chromeURL = url }
                }
            } catch {}
        }

        return (appName, windowTitle, chromeURL)
    }

    /// Format the frontmost-context fields as YAML-style header lines for the task file.
    /// Empty if no fields captured. Lines are intentionally outside the `---` separator so
    /// downstream readers can grep them as task metadata, not message body.
    private func formatFrontmostContext(_ ctx: (app: String?, windowTitle: String?, chromeURL: String?)) -> String {
        var lines: [String] = []
        if let a = ctx.app { lines.append("top_app: \(a)") }
        if let w = ctx.windowTitle { lines.append("top_window_title: \(w)") }
        if let u = ctx.chromeURL { lines.append("top_url: \(u)") }
        return lines.isEmpty ? "" : lines.joined(separator: "\n") + "\n"
    }

    @objc func dropContext() {
        // Debounce: ignore if less than 1 second since last drop
        let now = Date()
        if now.timeIntervalSince(lastDropTime) < 1.0 {
            logToFile("dropContext: debounced (too fast)")
            return
        }
        lastDropTime = now

        let timestamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current, formatOptions: [.withFullDate, .withTime, .withSpaceBetweenDateAndTime, .withColonSeparatorInTime])
        let logFile = workspace + "/logs/context-drop.log"
        let tasksDir = workspace + "/tasks"
        let epoch = Int(Date().timeIntervalSince1970 * 1000)
        let dropImage = tasksDir + "/image-\(epoch).png"

        // Capture frontmost-app context once, before the type-specific branches. Adds top_app /
        // top_window_title / top_url (Chrome only) header lines so the core agent knows what the
        // user was looking at when they dropped.
        let ctx = getFrontmostContext()
        let ctxHeader = formatFrontmostContext(ctx)

        // 1. Check Finder selection (only if Finder is frontmost)
        if let frontApp = NSWorkspace.shared.frontmostApplication,
           frontApp.bundleIdentifier == "com.apple.finder" {
            let finderFiles = getFinderSelection()
            if finderFiles.count == 1 {
                let finderFile = finderFiles[0]
                let content = """
                timestamp: \(timestamp)
                type: file
                path: \(finderFile)
                \(ctxHeader)---
                [File selected in Finder: \(finderFile)]
                """
                appendLog(logFile, "[\(timestamp)] Dropped: file (\(finderFile))")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                notify("Sutando", "File dropped: \(URL(fileURLWithPath: finderFile).lastPathComponent)")
                return
            } else if finderFiles.count > 1 {
                // Emit JSON-array on the `paths:` line for unambiguous parsing
                // (handles paths with spaces, colons, etc. without YAML lib).
                // Body trailer keeps a human-readable multi-line list.
                let pathsJSON: String = {
                    let data = try? JSONSerialization.data(withJSONObject: finderFiles, options: [])
                    return data.flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
                }()
                let humanList = finderFiles.map { "  - \($0)" }.joined(separator: "\n")
                let content = """
                timestamp: \(timestamp)
                type: files
                paths: \(pathsJSON)
                \(ctxHeader)---
                [Files selected in Finder: \(finderFiles.count) files]
                \(humanList)
                """
                appendLog(logFile, "[\(timestamp)] Dropped: \(finderFiles.count) files")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                notify("Sutando", "\(finderFiles.count) files dropped")
                return
            }
        }

        // 2. Check clipboard for image (PNG)
        if let imageData = NSPasteboard.general.data(forType: .png) {
            do {
                try imageData.write(to: URL(fileURLWithPath: dropImage))
                let content = """
                timestamp: \(timestamp)
                type: image
                path: \(dropImage)
                \(ctxHeader)---
                [Image dropped from clipboard]
                """
                appendLog(logFile, "[\(timestamp)] Dropped: image (\(imageData.count) bytes)")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                notify("Sutando", "Image dropped (\(imageData.count / 1024)KB)")
                return
            } catch {}
        }

        // 3. Check clipboard for TIFF image (screenshots sometimes use TIFF)
        if let tiffData = NSPasteboard.general.data(forType: .tiff),
           let bitmapRep = NSBitmapImageRep(data: tiffData),
           let pngData = bitmapRep.representation(using: .png, properties: [:]) {
            do {
                try pngData.write(to: URL(fileURLWithPath: dropImage))
                let content = """
                timestamp: \(timestamp)
                type: image
                path: \(dropImage)
                \(ctxHeader)---
                [Image dropped from clipboard]
                """
                appendLog(logFile, "[\(timestamp)] Dropped: image (\(pngData.count) bytes)")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                notify("Sutando", "Image dropped (\(pngData.count / 1024)KB)")
                return
            } catch {}
        }

        // 4. Try to get selected text via Accessibility API
        if let selected = getSelectedText(), !selected.isEmpty {
            let content = """
            timestamp: \(timestamp)
            type: text
            \(ctxHeader)---
            \(selected)
            """
            appendLog(logFile, "[\(timestamp)] Dropped: \(selected.count) chars")
            writeTask(tasksDir, timestamp: timestamp, content: content)
            let snippet = String(selected.prefix(80)).replacingOccurrences(of: "\n", with: " ")
            notify("Sutando", "Dropped: \(snippet)\(selected.count > 80 ? "…" : "")")
            return
        }

        // 5. Fallback: simulate Cmd+C, read clipboard
        simulateCopy()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [self] in
            if let text = NSPasteboard.general.string(forType: .string), !text.isEmpty {
                let content = """
                timestamp: \(timestamp)
                type: text
                \(ctxHeader)---
                \(text)
                """
                appendLog(logFile, "[\(timestamp)] Dropped: \(text.count) chars")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                let snippet = String(text.prefix(80)).replacingOccurrences(of: "\n", with: " ")
                notify("Sutando", "Dropped: \(snippet)\(text.count > 80 ? "…" : "")")
            } else {
                notify("Sutando", "Nothing selected — select text first")
                appendLog(logFile, "[\(timestamp)] Nothing selected")
            }
        }
    }

    // MARK: - Screenshot Drop (⌥C)

    @objc func dropScreenshot() {
        // Debounce — share lastDropTime with text drop to avoid rapid triggers
        let now = Date()
        if now.timeIntervalSince(lastDropTime) < 1.0 {
            logToFile("dropScreenshot: debounced (too fast)")
            return
        }
        lastDropTime = now

        let timestamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current, formatOptions: [.withFullDate, .withTime, .withSpaceBetweenDateAndTime, .withColonSeparatorInTime])
        let logFile = workspace + "/logs/context-drop.log"
        let tasksDir = workspace + "/tasks"

        // Call screen-capture-server to capture the screen and get the file path back.
        // Server runs at localhost:7845, default capture is the main display.
        guard let url = URL(string: "http://localhost:7845/capture") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        URLSession.shared.dataTask(with: req) { [self] data, _, error in
            if let error = error {
                notify("Sutando", "Screenshot drop failed: \(error.localizedDescription)")
                appendLog(logFile, "[\(timestamp)] dropScreenshot: error \(error.localizedDescription)")
                return
            }
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let path = json["path"] as? String else {
                notify("Sutando", "Screenshot drop failed: bad server response")
                appendLog(logFile, "[\(timestamp)] dropScreenshot: bad server response")
                return
            }

            let content = """
            timestamp: \(timestamp)
            type: image
            path: \(path)
            ---
            [Screenshot dropped via ⌥C]
            """
            appendLog(logFile, "[\(timestamp)] dropScreenshot: \(path)")
            writeTask(tasksDir, timestamp: timestamp, content: content)
            notify("Sutando", "Screenshot dropped (\(URL(fileURLWithPath: path).lastPathComponent))")
        }.resume()
    }

    // MARK: - Voice Toggle

    @objc func toggleVoice() {
        NSLog("Sutando: toggleVoice called")
        // NativeMic path is parked — see NativeMic.swift header. Echo cancellation
        // via voice-processing IO unit fails to initialize the output node on
        // this hardware (-10875). Re-enable once that's resolved.
        httpToggle(endpoint: "toggle")
    }

    @objc func toggleMute() {
        NSLog("Sutando: toggleMute called")
        httpToggle(endpoint: "mute")
    }

    func httpToggle(endpoint: String) {
        guard let url = URL(string: "http://localhost:8080/\(endpoint)") else { return }
        let task = URLSession.shared.dataTask(with: url) { data, response, error in
            if let error = error {
                NSLog("Sutando: \(endpoint) failed: \(error.localizedDescription)")
                // Fallback: open the web UI so user can toggle manually
                DispatchQueue.main.async {
                    self.notify("Sutando", "Web client not reachable — open localhost:8080")
                }
                return
            }
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                NSLog("Sutando: \(endpoint) OK")
            }
        }
        task.resume()
        NSSound.beep()
    }

    @objc func openWebUI() {
        NSLog("Sutando: openWebUI called")
        // Switch to existing localhost:8080 tab or open new one
        let script = NSAppleScript(source: """
        tell application "Google Chrome"
            activate
            set found to false
            repeat with w in windows
                set tabList to tabs of w
                repeat with i from 1 to count of tabList
                    if URL of item i of tabList contains "localhost:8080" then
                        set active tab index of w to i
                        set index of w to 1
                        set found to true
                        exit repeat
                    end if
                end repeat
                if found then exit repeat
            end repeat
            if not found then
                open location "http://localhost:8080"
            end if
        end tell
        """)
        var error: NSDictionary?
        script?.executeAndReturnError(&error)
        if let error = error {
            let msg = error[NSAppleScript.errorMessage] as? String ?? "unknown error"
            if msg.contains("not allowed") || msg.contains("permission") {
                notify("Sutando", "Open Web UI needs: System Settings → Privacy & Security → Automation → allow Sutando to control Chrome")
            } else {
                // Fallback: just open the URL directly
                if let url = URL(string: "http://localhost:8080") {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    @objc func openCore() {
        // Activate Terminal running Claude Code
        let script = NSAppleScript(source: """
        tell application "Terminal"
            activate
            -- Find the window running claude
            repeat with w in windows
                if name of w contains "claude" or name of w contains "sutando" then
                    set index of w to 1
                    exit repeat
                end if
            end repeat
        end tell
        """)
        script?.executeAndReturnError(nil)
    }

    @objc func openDashboard() {
        let script = NSAppleScript(source: """
        tell application "Google Chrome"
            activate
            set found to false
            repeat with w in windows
                set tabList to tabs of w
                repeat with i from 1 to count of tabList
                    if URL of item i of tabList contains "localhost:7844" then
                        set active tab index of w to i
                        set index of w to 1
                        set found to true
                        exit repeat
                    end if
                end repeat
                if found then exit repeat
            end repeat
            if not found then
                open location "http://localhost:7844"
            end if
        end tell
        """)
        script?.executeAndReturnError(nil)
    }

    // MARK: - Helpers

    func getFinderSelection() -> [String] {
        let script = """
        tell application "Finder"
            try
                set sel to selection
                set out to ""
                repeat with anItem in sel
                    set out to out & POSIX path of (anItem as alias) & "\n"
                end repeat
                return out
            on error
                return ""
            end try
        end tell
        """
        guard let appleScript = NSAppleScript(source: script) else { return [] }
        var error: NSDictionary?
        let result = appleScript.executeAndReturnError(&error)
        guard let raw = result.stringValue, !raw.isEmpty else { return [] }
        return raw.split(separator: "\n").map(String.init).filter { !$0.isEmpty && FileManager.default.fileExists(atPath: $0) }
    }

    func getSelectedText() -> String? {
        let systemElement = AXUIElementCreateSystemWide()
        var focusedElement: AnyObject?
        guard AXUIElementCopyAttributeValue(systemElement, kAXFocusedUIElementAttribute as CFString, &focusedElement) == .success else {
            return nil
        }
        var selectedText: AnyObject?
        guard AXUIElementCopyAttributeValue(focusedElement as! AXUIElement, kAXSelectedTextAttribute as CFString, &selectedText) == .success else {
            return nil
        }
        return selectedText as? String
    }

    func simulateCopy() {
        let src = CGEventSource(stateID: .hidSystemState)
        let keyDown = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: true) // C key
        let keyUp = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: false)
        keyDown?.flags = .maskCommand
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cghidEventTap)
        keyUp?.post(tap: .cghidEventTap)
    }

    func writeFile(_ path: String, _ content: String) {
        try? content.write(toFile: path, atomically: true, encoding: .utf8)
    }

    func appendLog(_ path: String, _ line: String) {
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write((line + "\n").data(using: .utf8)!)
            handle.closeFile()
        } else {
            try? (line + "\n").write(toFile: path, atomically: true, encoding: .utf8)
        }
    }

    func writeTask(_ tasksDir: String, timestamp: String, content: String) {
        let ts = Int(Date().timeIntervalSince1970 * 1000)
        let taskContent = """
        id: task-\(ts)
        timestamp: \(ISO8601DateFormatter().string(from: Date()))
        task: User dropped context via hotkey. Process this:
        \(content)
        """
        let taskPath = tasksDir + "/task-\(ts).txt"
        try? taskContent.write(toFile: taskPath, atomically: true, encoding: .utf8)
    }

    var lastHealthCheckStart: Date = .distantPast
    func runHealthCheck() {
        // Throttle: never more than once per 60s, even if the Timer +
        // startup-fire happen to align.
        let now = Date()
        if now.timeIntervalSince(lastHealthCheckStart) < 60 { return }
        lastHealthCheckStart = now

        let logPath = workspace + "/logs/health-check.log"
        let scriptPath = workspace + "/src/health-check.py"
        // Match the (retired) launchd plist's interpreter so behavior is
        // identical. Falls back to /usr/bin/env python3 if homebrew python
        // is missing on this host.
        let homebrewPython = "/opt/homebrew/opt/python@3.11/libexec/bin/python3"
        let pythonPath = FileManager.default.fileExists(atPath: homebrewPython)
            ? homebrewPython : "/usr/bin/env"
        // `--emit-task` writes tasks/task-health-{ts}.txt on failure (with
        // built-in dedup: 1h cooldown per failure-set hash). The agent picks
        // it up via the bridge as a regular owner task — gives the trio's
        // surface_owner path a redundant peer in the file-bridge layer, so
        // health failures the trio's coverage scanner suppresses by cooldown
        // (or the LLM step archives by judgment) still reach the agent. Per
        // Chi 2026-05-07 PT.
        let arguments: [String] = (pythonPath == "/usr/bin/env")
            ? ["python3", scriptPath, "--fix", "--emit-task"]
            : [scriptPath, "--fix", "--emit-task"]

        DispatchQueue.global(qos: .background).async { [weak self] in
            guard let self = self else { return }
            if !FileManager.default.fileExists(atPath: logPath) {
                FileManager.default.createFile(atPath: logPath, contents: Data())
            }
            guard let fh = FileHandle(forWritingAtPath: logPath) else {
                self.logToFile("runHealthCheck: failed to open \(logPath)")
                return
            }
            fh.seekToEndOfFile()

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: pythonPath)
            proc.arguments = arguments
            proc.standardOutput = fh
            proc.standardError = fh
            proc.currentDirectoryURL = URL(fileURLWithPath: self.workspace)

            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus != 0 {
                    self.logToFile("runHealthCheck: exit \(proc.terminationStatus)")
                }
            } catch {
                self.logToFile("runHealthCheck: spawn failed — \(error.localizedDescription)")
            }
            try? fh.close()
        }
    }

    func logToFile(_ msg: String) {
        let path = workspace + "/logs/sutando-app-debug.log"
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(msg)\n"
        if let fh = FileHandle(forWritingAtPath: path) {
            fh.seekToEndOfFile()
            fh.write(Data(line.utf8))
            fh.closeFile()
        } else {
            FileManager.default.createFile(atPath: path, contents: Data(line.utf8))
        }
    }

    func notify(_ title: String, _ message: String) {
        logToFile("notify: \(title) — \(message)")
        // Play sound for immediate feedback
        NSSound.beep()
        // Show floating HUD window (no notification permissions needed)
        DispatchQueue.main.async { [self] in
            showHUD(title: title, message: message)
        }
    }

    var hudWindow: NSWindow?
    var hudTimer: Timer?

    func showHUD(title: String, message: String) {
        hudTimer?.invalidate()
        hudWindow?.orderOut(nil)

        let width: CGFloat = 320
        let height: CGFloat = 60
        guard let screen = NSScreen.main else {
            logToFile("showHUD: no main screen")
            return
        }
        let x = screen.visibleFrame.midX - width / 2
        let y = screen.visibleFrame.maxY - height - 12

        let window = NSWindow(contentRect: NSRect(x: x, y: y, width: width, height: height),
                              styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.level = .screenSaver  // above everything
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        // Rounded dark background
        let bg = NSVisualEffectView(frame: window.contentView!.bounds)
        bg.material = .hudWindow
        bg.blendingMode = .behindWindow
        bg.state = .active
        bg.wantsLayer = true
        bg.layer?.cornerRadius = 10
        bg.layer?.masksToBounds = true
        window.contentView?.addSubview(bg)

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.boldSystemFont(ofSize: 13)
        titleLabel.textColor = .white
        titleLabel.frame = NSRect(x: 12, y: 30, width: width - 24, height: 20)

        let bodyLabel = NSTextField(labelWithString: String(message.prefix(120)))
        bodyLabel.font = NSFont.systemFont(ofSize: 11)
        bodyLabel.textColor = NSColor(white: 0.85, alpha: 1)
        bodyLabel.frame = NSRect(x: 12, y: 8, width: width - 24, height: 18)
        bodyLabel.lineBreakMode = .byTruncatingTail

        window.contentView?.addSubview(titleLabel)
        window.contentView?.addSubview(bodyLabel)
        window.orderFrontRegardless()
        hudWindow = window
        logToFile("showHUD: displayed at \(x),\(y) size \(width)x\(height)")

        hudTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: false) { [weak self] _ in
            DispatchQueue.main.async {
                self?.hudWindow?.orderOut(nil)
            }
        }
    }

    @objc func restartServices() {
        notify("Sutando", "Restarting all services...")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [workspace + "/src/restart.sh"]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        DispatchQueue.global(qos: .utility).async {
            try? proc.run()
            proc.waitUntilExit()
        }
    }

    @objc func stopServices() {
        notify("Sutando", "Stopping all services...")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [workspace + "/src/stop.sh"]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        DispatchQueue.global(qos: .utility).async {
            try? proc.run()
            proc.waitUntilExit()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false  // keep running as menu bar app even when HUD closes
    }

    @objc func quit() {
        NSApplication.shared.terminate(nil)
    }

    /// Pause the proactive loop for 30 minutes by writing the sentinel
    /// the loop's skip-conditions check (per
    /// `~/.claude/skills/proactive-loop/SKILL.md` Skip Conditions §(d)).
    /// Format: ISO-8601 expiry timestamp (UTC). Auto-expires — forgetting
    /// to resume just means the loop self-re-enables in 30 min.
    @objc func pauseLoop30() {
        let expiry = Date().addingTimeInterval(30 * 60)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let iso = formatter.string(from: expiry)
        let path = workspace + "/state/loop-paused-until.sentinel"
        let dir = workspace + "/state"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        do {
            try iso.write(toFile: path, atomically: true, encoding: .utf8)
            let humanTime = DateFormatter.localizedString(from: expiry, dateStyle: .none, timeStyle: .short)
            notify("Sutando", "Loop paused until \(humanTime). Click Resume Loop to re-enable sooner.")
        } catch {
            notify("Sutando", "Loop pause failed: \(error.localizedDescription)")
        }
    }

    /// Resume the proactive loop by deleting the pause sentinel. No-op
    /// if the sentinel doesn't exist (loop wasn't paused).
    @objc func resumeLoop() {
        let path = workspace + "/state/loop-paused-until.sentinel"
        if FileManager.default.fileExists(atPath: path) {
            try? FileManager.default.removeItem(atPath: path)
            notify("Sutando", "Loop resumed.")
        } else {
            notify("Sutando", "Loop wasn't paused.")
        }
    }

    /// Restart the Claude Code core session (sutando-core tmux session).
    /// Invokes scripts/start-cli.sh --restart which kills any existing
    /// session and starts fresh detached. User can re-attach via
    /// "Open Core CLI" in the menu (or `tmux -S /tmp/sutando-tmux.sock
    /// attach -t sutando-core` from a terminal).
    ///
    /// **Hazard** (per Mini's #608 review): this MUST be invoked from
    /// outside the sutando-core CLI session — Sutando.app menu, terminal,
    /// future health-check emit-task, etc. If a future agent runs this
    /// from WITHIN the sutando-core session (e.g., processing a "restart
    /// core" task), --restart will kill its own parent session and
    /// terminate the agent mid-task. The menu-bar app is safe; agent
    /// self-invocation is not.
    ///
    /// Per Chi 2026-05-05: voice-agent restart explicitly excluded —
    /// this only restarts the Claude Code CLI session.
    @objc func restartCore() {
        notify("Sutando", "Restarting Core CLI…")
        let script = workspace + "/scripts/start-cli.sh"
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [script, "--restart"]
        // Capture stderr so we can surface failures via notify rather than
        // silently swallowing (per Mini's #608 review nit #1). stdout still
        // discarded — script's success messages aren't useful to the user.
        let errPipe = Pipe()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = errPipe
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                try proc.run()
            } catch {
                self?.notify("Sutando", "Core restart failed to start: \(error.localizedDescription)")
                return
            }
            proc.waitUntilExit()
            if proc.terminationStatus == 0 {
                self?.notify("Sutando", "Core restarted. Attach via Open Core CLI in menu.")
            } else {
                let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
                let errStr = String(data: errData, encoding: .utf8) ?? ""
                let preview = String(errStr.prefix(200))
                self?.notify("Sutando", "Core restart failed (exit \(proc.terminationStatus)): \(preview)")
            }
        }
    }

    /// Restart the Sutando.app menu bar app — useful after editing
    /// ~/.config/sutando/hotkeys.json so the new bindings take effect.
    /// Spawns a detached helper that waits for this process to exit, then
    /// re-launches the same binary, then exits the current process.
    @objc func restartSelf() {
        let myPath = ProcessInfo.processInfo.arguments[0]
        let myPid = ProcessInfo.processInfo.processIdentifier
        // Detached shell: wait for current pid to die, then exec the same binary.
        let script = "while kill -0 \(myPid) 2>/dev/null; do sleep 0.1; done; exec \"\(myPath)\""
        let task = Process()
        task.launchPath = "/bin/sh"
        task.arguments = ["-c", script]
        do {
            try task.run()
            logToFile("restartSelf: spawned relaunch helper (pid will be \(myPid)), terminating")
            NSApplication.shared.terminate(nil)
        } catch {
            notify("Sutando", "Restart failed: \(error.localizedDescription)")
            logToFile("restartSelf: failed to spawn helper: \(error)")
        }
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory) // menu bar only, no dock icon
app.run()
