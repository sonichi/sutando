// ax-read — read the focused app's text selection via the macOS Accessibility API.
//
// Used by Sutando.app's dropContext (the ⌃C "drop context" hotkey). Runs as
// a subprocess so the AX queries get a stable TCC attribution chain rooted
// at this binary's signed identity rather than at every caller's. Sutando.app
// spawns this via Process() in invokeAxRead() and parses the JSON below.
//
// Output JSON shape (single line, sorted keys):
//
//   { "app": "Discord",
//     "window_title": "@sutando — Discord",
//     "url": "",
//     "selected": "the highlighted text",
//     "path": "ax" }
//
// `path` values:
//   "ax"        — AXSelectedText returned non-empty (native NSTextView path)
//   "clipboard" — AX returned empty, Cmd+C fallback wrote new clipboard content
//   "none"      — both paths returned empty (or AX denied)
//
// Why a separate binary instead of inlining in Sutando.app:
//   - LSUIElement=YES menu-bar agents have a TCC attribution chain that
//     historically misroutes AX queries to the parent bundle even after
//     re-grant. A separately-signed CLI under the app's launch context
//     gets its own clean trust binding.
//   - Reusable: any other tool (e.g. voice agent's read_selection) can shell
//     out to the same binary and get identical output.
//
// Private "personal-deictic" skill ships a richer version that also captures
// the focused-window screenshot and cursor location for deictic phrases like
// "this" and "here." This public version drops those — Sutando.app only
// needs the text-selection bits for ⌃C drops.

import Cocoa
import ApplicationServices
import Foundation

// MARK: - Frontmost app via CGWindowList
//
// NSWorkspace.shared.frontmostApplication returns nil from processes not
// attached to the GUI WindowServer session (LSUIElement agents, launchd
// daemons). CGWindowListCopyWindowInfo works from any process context — it
// queries the window server directly.
//
// We deliberately read ONLY kCGWindowOwnerName + kCGWindowOwnerPID — those
// don't require any TCC grant. kCGWindowName (the window title) DOES
// require Screen Recording permission, and since ax-read is rebuilt via
// `swift build` (ad-hoc signed, hash changes every build), that grant
// would orphan and re-prompt on every rebuild. The window title is fetched
// via the AX path (kAXTitleAttribute) below instead — only needs
// Accessibility permission, which is granted once to Sutando.app.

struct FrontmostInfo {
    let name: String
    let pid: pid_t
}

func frontmostFromCGWindowList() -> FrontmostInfo? {
    guard let windowList = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements],
        kCGNullWindowID
    ) as? [[String: Any]] else { return nil }

    for window in windowList {
        guard let layer = window[kCGWindowLayer as String] as? Int, layer == 0,
              let name = window[kCGWindowOwnerName as String] as? String,
              let pidNum = window[kCGWindowOwnerPID as String] as? Int else {
            continue
        }
        return FrontmostInfo(name: name, pid: pid_t(pidNum))
    }
    return nil
}

// MARK: - Browser URL via osascript
//
// Chromium-derived browsers expose `URL of active tab of front window` via
// their AppleScript dictionary; Safari uses `URL of front document`. We
// shell to osascript with a short timeout to avoid hanging if the browser
// is busy. Returns "" for non-browser apps or any failure.

func runOsascript(_ script: String, timeout: TimeInterval = 0.5) -> String {
    let task = Process()
    task.launchPath = "/usr/bin/osascript"
    task.arguments = ["-e", script]
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe()
    do { try task.run() } catch { return "" }
    let deadline = Date().addingTimeInterval(timeout)
    while task.isRunning && Date() < deadline {
        Thread.sleep(forTimeInterval: 0.02)
    }
    if task.isRunning { task.terminate(); return "" }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

func browserURL(forApp name: String) -> String {
    let chromiumApps: Set<String> = ["Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Chromium"]
    if chromiumApps.contains(name) {
        return runOsascript("tell application \"\(name)\" to get URL of active tab of front window")
    }
    if name == "Safari" {
        return runOsascript("tell application \"Safari\" to get URL of front document")
    }
    return ""
}

// MARK: - Main entry

func emit(_ result: [String: String]) {
    let json: [String: Any] = [
        "app": result["app"] ?? "",
        "window_title": result["window_title"] ?? "",
        "url": result["url"] ?? "",
        "selected": result["selected"] ?? "",
        "path": result["path"] ?? "none",
    ]
    if let data = try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    } else {
        print("{\"app\":\"\",\"window_title\":\"\",\"url\":\"\",\"selected\":\"\",\"path\":\"none\"}")
    }
}

let frontInfo = frontmostFromCGWindowList()
let appName: String
let pid: pid_t
var windowTitle: String

if let f = frontInfo {
    appName = f.name
    pid = f.pid
} else {
    let frontmostApp = NSWorkspace.shared.frontmostApplication
    appName = frontmostApp?.localizedName ?? ""
    pid = frontmostApp?.processIdentifier ?? -1
}
windowTitle = ""

if pid < 0 {
    emit(["path": "none"])
    exit(0)
}

let appRef = AXUIElementCreateApplication(pid)

// AX window title — kAXTitleAttribute needs only Accessibility permission,
// not Screen Recording. This is the sole source of the title.
var focusedWindow: AnyObject?
if AXUIElementCopyAttributeValue(appRef, kAXFocusedWindowAttribute as CFString, &focusedWindow) == .success,
   let win = focusedWindow {
    var titleValue: AnyObject?
    if AXUIElementCopyAttributeValue(win as! AXUIElement, kAXTitleAttribute as CFString, &titleValue) == .success,
       let t = titleValue as? String {
        windowTitle = t
    }
}

let url = browserURL(forApp: appName)

// AX selected text via the focused element.
var focused: AnyObject?
let focusedErr = AXUIElementCopyAttributeValue(appRef, kAXFocusedUIElementAttribute as CFString, &focused)

var selected = ""
var path = "none"
if focusedErr == .success, let element = focused {
    var selValue: AnyObject?
    let selErr = AXUIElementCopyAttributeValue(element as! AXUIElement, kAXSelectedTextAttribute as CFString, &selValue)
    if selErr == .success, let s = selValue as? String, !s.isEmpty {
        selected = s
        path = "ax"
    }
}

// Clipboard fallback (Cmd+C → read NSPasteboard → restore full prior items).
//
// AX-on-Electron (Chrome, VS Code, Cursor, Slack, Discord) doesn't expose
// AXSelectedText reliably — Monaco/CodeMirror/web text-fields render outside
// the native AXTextArea hierarchy. Fallback: snapshot pasteboard, send Cmd+C
// to copy the visible selection, read, restore. The changeCount check
// distinguishes "Cmd+C wrote something" from "clipboard was already
// populated" — otherwise the previous clipboard contents would be reported
// as the new selection (the stale-clipboard regression class).
//
// We snapshot ALL pasteboard items (not just `.string`) and restore them
// verbatim. Sutando.app's dropContext invokes ax-read BEFORE checking the
// clipboard for images, so if we only saved/restored string content, a
// no-selection ax-read run would silently drop image/file clipboard data
// before Sutando got a chance to handle it. Per Mini's review on PR #907.
if selected.isEmpty {
    let pb = NSPasteboard.general
    let priorChangeCount = pb.changeCount

    // Snapshot every (type, data) pair on each existing pasteboard item.
    // We rebuild from this set if Cmd+C dirties the pasteboard.
    let priorSnapshot: [[NSPasteboard.PasteboardType: Data]] = (pb.pasteboardItems ?? []).map { item in
        var dict: [NSPasteboard.PasteboardType: Data] = [:]
        for type in item.types {
            if let data = item.data(forType: type) {
                dict[type] = data
            }
        }
        return dict
    }

    let src = CGEventSource(stateID: .hidSystemState)
    let cDown = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: true)
    cDown?.flags = .maskCommand
    let cUp = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: false)
    cUp?.flags = .maskCommand
    cDown?.post(tap: .cghidEventTap)
    cUp?.post(tap: .cghidEventTap)

    Thread.sleep(forTimeInterval: 0.12)

    if pb.changeCount > priorChangeCount {
        if let copied = pb.string(forType: .string), !copied.isEmpty {
            selected = copied
            path = "clipboard"
        }
        // Restore the full pasteboard contents we snapshotted. Only do this
        // if Cmd+C actually dirtied the clipboard — if it didn't fire, the
        // pasteboard is unchanged and we skip the clear/rewrite entirely.
        pb.clearContents()
        for entry in priorSnapshot {
            let item = NSPasteboardItem()
            for (type, data) in entry {
                item.setData(data, forType: type)
            }
            pb.writeObjects([item])
        }
    }
}

emit([
    "app": appName,
    "window_title": windowTitle,
    "url": url,
    "selected": selected,
    "path": path,
])
