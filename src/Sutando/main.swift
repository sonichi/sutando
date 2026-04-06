import Cocoa
import Carbon

// MARK: - Sutando Drop Menu Bar App
// Replaces Automator Quick Action for context drops.
// Global hotkey (Ctrl+Shift+D) captures selected text, clipboard image, or Finder file.

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var hotKeyRef: EventHotKeyRef?
    var voiceHotKeyRef: EventHotKeyRef?
    var muteHotKeyRef: EventHotKeyRef?
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenuBar()
        registerHotKey()
        watchResults()
        notify("Sutando Drop", "Ready — press ⌃C to drop context")
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
            let avatarPath = workspace + "/docs/stand-avatar.png"
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
        menu.addItem(NSMenuItem(title: "Drop Context (⌃C)", action: #selector(dropContext), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Toggle Voice (⌃V)", action: #selector(toggleVoice), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Toggle Mute (⌃M)", action: #selector(toggleMute), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Open Web UI", action: #selector(openWebUI), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Open Core CLI", action: #selector(openCore), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Restart All Services", action: #selector(restartServices), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Stop All Services", action: #selector(stopServices), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
    }

    // MARK: - Global Hotkey (Ctrl+Shift+D)

    func registerHotKey() {
        var hotKeyID = EventHotKeyID()
        hotKeyID.signature = OSType(0x5355_5444) // "SUTD"
        hotKeyID.id = 1

        // Ctrl+C: modifiers = controlKey, keycode = 8 (C)
        let status = RegisterEventHotKey(
            UInt32(kVK_ANSI_C),
            UInt32(controlKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )

        if status != noErr {
            notify("Sutando Drop", "Failed to register hotkey (⌃C). Another app may have claimed it.")
            return
        }

        // Register ⌃V for voice toggle (hotkey ID 2)
        var voiceHotKeyID = EventHotKeyID()
        voiceHotKeyID.signature = OSType(0x5355_5444) // "SUTD"
        voiceHotKeyID.id = 2
        let statusV = RegisterEventHotKey(
            UInt32(kVK_ANSI_V),
            UInt32(controlKey),
            voiceHotKeyID,
            GetApplicationEventTarget(),
            0,
            &voiceHotKeyRef
        )
        if statusV != noErr {
            notify("Sutando", "Failed to register ⌃V hotkey (error \(statusV))")
        }

        // Register ⌃M for mute toggle (hotkey ID 3)
        var muteHotKeyID = EventHotKeyID()
        muteHotKeyID.signature = OSType(0x5355_5444) // "SUTD"
        muteHotKeyID.id = 3
        let statusM = RegisterEventHotKey(
            UInt32(kVK_ANSI_M),
            UInt32(controlKey),
            muteHotKeyID,
            GetApplicationEventTarget(),
            0,
            &muteHotKeyRef
        )

        notify("Sutando", "Hotkeys: ⌃C\(status == noErr ? "✓" : "✗") ⌃V\(statusV == noErr ? "✓" : "✗") ⌃M\(statusM == noErr ? "✓" : "✗")")

        // Install handler — dispatch by hotkey ID
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { (_, event, _) -> OSStatus in
            var hotKeyID = EventHotKeyID()
            GetEventParameter(event!, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
            let appDelegate = NSApplication.shared.delegate as! AppDelegate
            switch hotKeyID.id {
            case 1: appDelegate.dropContext()
            case 2: appDelegate.toggleVoice()
            case 3: appDelegate.toggleMute()
            default: break
            }
            return noErr
        }, 1, &eventType, nil, nil)
    }

    // MARK: - Context Drop Logic

    @objc func dropContext() {
        let timestamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current, formatOptions: [.withFullDate, .withTime, .withSpaceBetweenDateAndTime, .withColonSeparatorInTime])
        let dropFile = workspace + "/context-drop.txt"
        let logFile = workspace + "/src/context-drop.log"
        let tasksDir = workspace + "/tasks"
        let epoch = Int(Date().timeIntervalSince1970 * 1000)
        let dropImage = tasksDir + "/image-\(epoch).png"

        // 1. Check Finder selection (only if Finder is frontmost)
        if let frontApp = NSWorkspace.shared.frontmostApplication,
           frontApp.bundleIdentifier == "com.apple.finder" {
            if let finderFile = getFinderSelection() {
                let content = """
                timestamp: \(timestamp)
                type: file
                path: \(finderFile)
                ---
                [File selected in Finder: \(finderFile)]
                """
                appendLog(logFile, "[\(timestamp)] Dropped: file (\(finderFile))")
                writeTask(tasksDir, timestamp: timestamp, content: content)
                notify("Sutando", "File dropped: \(URL(fileURLWithPath: finderFile).lastPathComponent)")
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
                ---
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
                ---
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
            ---
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
                ---
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

    // MARK: - Voice Toggle

    @objc func toggleVoice() {
        // Click the voice button in Chrome via AppleScript
        let script = NSAppleScript(source: """
        tell application "Google Chrome"
            set found to false
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "localhost:8080" then
                        tell t to execute javascript "document.querySelector('.btn-hero, .btn-voice').click()"
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
            let msg = error[NSAppleScript.errorMessage] as? String ?? ""
            if msg.contains("not allowed") || msg.contains("JavaScript") || msg.contains("permission") {
                notify("Sutando", "⌃V needs: Chrome → View → Developer → Allow JavaScript from Apple Events")
            }
        }
        NSSound.beep()
    }

    @objc func toggleMute() {
        // Click the mute button in Chrome via AppleScript
        let script = NSAppleScript(source: """
        tell application "Google Chrome"
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "localhost:8080" then
                        tell t to execute javascript "document.querySelector('.btn-mute').click()"
                        exit repeat
                    end if
                end repeat
            end repeat
        end tell
        """)
        script?.executeAndReturnError(nil)
        NSSound.beep()
    }

    @objc func openWebUI() {
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
        script?.executeAndReturnError(nil)
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

    func getFinderSelection() -> String? {
        let script = """
        tell application "Finder"
            try
                set sel to selection
                if (count of sel) > 0 then
                    return POSIX path of (item 1 of sel as alias)
                end if
            on error
                return ""
            end try
        end tell
        """
        guard let appleScript = NSAppleScript(source: script) else { return nil }
        var error: NSDictionary?
        let result = appleScript.executeAndReturnError(&error)
        let path = result.stringValue
        if let path = path, !path.isEmpty, FileManager.default.fileExists(atPath: path) {
            return path
        }
        return nil
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

    func notify(_ title: String, _ message: String) {
        // Spawn osascript subprocess — in-process NSAppleScript doesn't show banners
        let escaped = message.replacingOccurrences(of: "\"", with: "\\\"")
        let script = "display notification \"\(escaped)\" with title \"\(title)\" sound name \"Ping\""
        // Audio beep as immediate confirmation
        NSSound.beep()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
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

    @objc func quit() {
        NSApplication.shared.terminate(nil)
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory) // menu bar only, no dock icon
app.run()
