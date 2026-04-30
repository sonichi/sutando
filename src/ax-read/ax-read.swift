// ax-read — minimal CLI that returns frontmost-app accessibility data as JSON.
//
// Output schema: {"app": "<frontmostAppName>", "selected": "<text>", "cursor": [x,y]}
// Errors keep the schema (empty strings / null cursor) so callers can use it
// uniformly without branching on stderr.
//
// Used by deictic edit-mode (notes/deictic-edit-mode-design-2026-04-30.md):
// when the voice agent's interim transcript hits a deictic word, it shells
// out to this binary and snapshots the result. The snapshot then enters the
// LLM's per-turn context as numbered deictic refs.
//
// Build:  swiftc -O -o ax-read ax-read.swift -framework Cocoa -framework ApplicationServices
// Test:   ./ax-read    (focus an app with selected text; expect JSON)
//
// Why a separate Swift binary vs osascript/AppleScript: AX read via
// AppleScript has reliability gaps across apps (e.g. Cursor returns
// "Can't get attribute AXFocused"). The C-level AX API does it cleanly
// and consistently.

import Cocoa
import ApplicationServices
import Foundation

// MARK: - JSON output

func emit(app: String, selected: String, cursor: NSPoint?) -> Never {
    var json: [String: Any] = ["app": app, "selected": selected]
    if let c = cursor { json["cursor"] = [c.x, c.y] }
    else { json["cursor"] = NSNull() }
    if let data = try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    } else {
        print("{\"app\":\"\",\"selected\":\"\",\"cursor\":null}")
    }
    exit(0)
}

// MARK: - Frontmost app

let workspace = NSWorkspace.shared
let frontmostApp = workspace.frontmostApplication
let appName = frontmostApp?.localizedName ?? ""
let pid = frontmostApp?.processIdentifier ?? -1

if pid < 0 {
    emit(app: "", selected: "", cursor: nil)
}

// MARK: - AX selected text via the focused element

let appRef = AXUIElementCreateApplication(pid)

var focused: AnyObject?
let focusedErr = AXUIElementCopyAttributeValue(appRef, kAXFocusedUIElementAttribute as CFString, &focused)

var selected = ""
if focusedErr == .success, let element = focused {
    var selValue: AnyObject?
    let selErr = AXUIElementCopyAttributeValue(element as! AXUIElement, kAXSelectedTextAttribute as CFString, &selValue)
    if selErr == .success, let s = selValue as? String {
        selected = s
    }
}

// MARK: - Cursor position (global coordinates, screen-pixel)

// NSEvent.mouseLocation: bottom-left origin, Cocoa coords. The deictic
// design treats this as "where 'here' points" — the consumer can convert
// to whatever coordinate space it needs.
let mouse = NSEvent.mouseLocation

emit(app: appName, selected: selected, cursor: mouse)
