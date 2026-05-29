// Pointer Teacher tracer — OVERLAY (standalone; production target = Sutando.app hudWindow).
// Borderless screenSaver-level click-through window over the main display.
// Polls the IPC command file; on a new Target, flies a triangle there along a
// Clicky-style quadratic bezier, shows the label, holds, fades.
//
// build: swiftc pointer-overlay.swift -o pointer-overlay
// run  : ./pointer-overlay &
// IPC  : /tmp/pointer-cmd.json  {"nx","ny","label","say","ts"}

import Cocoa

let CMD = "/tmp/pointer-cmd.json"
let BLUE = NSColor(calibratedRed: 0.20, green: 0.62, blue: 1.0, alpha: 1.0)

final class PointerView: NSView {
    var pos = CGPoint.zero          // current triangle position (view coords, bottom-left)
    var angle: CGFloat = 0          // radians; 0 = tip up
    var label = ""
    var showLabel = false
    var alpha: CGFloat = 0          // overall opacity 0..1
    var halo: CGFloat = 0           // pulsing ring radius (0 = off)

    override var isFlipped: Bool { false }
    override func hitTest(_ p: NSPoint) -> NSView? { nil }   // never capture input

    override func draw(_ r: NSRect) {
        guard alpha > 0.01 else { return }
        let ctx = NSGraphicsContext.current!.cgContext
        ctx.setAlpha(alpha)

        // pulsing halo ring around the Target (very visible)
        if halo > 0 {
            ctx.saveGState()
            ctx.setStrokeColor(BLUE.withAlphaComponent(0.8).cgColor)
            ctx.setLineWidth(4)
            ctx.setShadow(offset: .zero, blur: 16, color: BLUE.cgColor)
            ctx.strokeEllipse(in: CGRect(x: pos.x - halo, y: pos.y - halo,
                                         width: halo * 2, height: halo * 2))
            ctx.restoreGState()
        }

        // triangle (equilateral, tip along `angle`)
        let s: CGFloat = 44, h = s * sqrt(3) / 2
        ctx.saveGState()
        ctx.translateBy(x: pos.x, y: pos.y)
        ctx.rotate(by: angle)
        let p = CGMutablePath()
        p.move(to: CGPoint(x: 0, y: h * 0.66))
        p.addLine(to: CGPoint(x: -s / 2, y: -h * 0.34))
        p.addLine(to: CGPoint(x: s / 2, y: -h * 0.34))
        p.closeSubpath()
        ctx.addPath(p)
        ctx.setFillColor(BLUE.cgColor)
        ctx.setShadow(offset: .zero, blur: 12, color: BLUE.withAlphaComponent(0.9).cgColor)
        ctx.fillPath()
        ctx.restoreGState()

        // label bubble
        if showLabel, !label.isEmpty {
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 16, weight: .semibold),
                .foregroundColor: NSColor.white]
            let sz = (label as NSString).size(withAttributes: attrs)
            let pad: CGFloat = 8
            let box = NSRect(x: pos.x + 18, y: pos.y + 8,
                             width: sz.width + pad * 2, height: sz.height + pad)
            let rp = NSBezierPath(roundedRect: box, xRadius: 7, yRadius: 7)
            BLUE.setFill(); rp.fill()
            (label as NSString).draw(at: NSPoint(x: box.minX + pad, y: box.minY + pad / 2),
                                     withAttributes: attrs)
        }
    }
}

final class Overlay: NSObject {
    let win: NSWindow
    let view = PointerView()
    var lastTS: Double = 0
    var anim: Timer?
    // Stored so a new fly() can cancel a prior flight's timers — mirrors the
    // production fix in src/Sutando/main.swift. Local timers survive scope and
    // would keep mutating halo/alpha during the next flight.
    var pulseTimer: Timer?
    var holdTimer: Timer?
    var fadeTimer: Timer?

    override init() {
        let f = NSScreen.main!.frame
        win = NSWindow(contentRect: f, styleMask: .borderless, backing: .buffered, defer: false)
        super.init()
        win.level = .screenSaver
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = false
        win.ignoresMouseEvents = true
        win.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        view.frame = NSRect(origin: .zero, size: f.size)
        view.pos = CGPoint(x: f.width / 2, y: f.height / 2)   // start: screen center
        win.contentView = view
        win.orderFrontRegardless()
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.poll() }
        FileManager.default.createFile(atPath: CMD, contents: Data("{}".utf8))
        NSLog("pointer-overlay up on \(Int(f.width))x\(Int(f.height)) — watching \(CMD)")
    }

    func poll() {
        guard let d = try? Data(contentsOf: URL(fileURLWithPath: CMD)),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let ts = o["ts"] as? Double, ts > lastTS,
              let nx = o["nx"] as? Double, let ny = o["ny"] as? Double else { return }
        lastTS = ts
        let f = NSScreen.main!.frame
        // nx,ny = fraction of main display, top-left origin -> view coords (bottom-left)
        let target = CGPoint(x: CGFloat(nx) * f.width,
                             y: f.height - CGFloat(ny) * f.height)
        fly(to: target, label: o["label"] as? String ?? "")
    }

    func fly(to dst: CGPoint, label: String) {
        // Cancel every timer from a prior flight — a stale hold/pulse/fade
        // would otherwise keep mutating halo/alpha during the new flight.
        anim?.invalidate()
        pulseTimer?.invalidate(); pulseTimer = nil
        holdTimer?.invalidate(); holdTimer = nil
        fadeTimer?.invalidate(); fadeTimer = nil
        view.showLabel = false
        view.label = label
        let start = view.pos
        let dist = hypot(dst.x - start.x, dst.y - start.y)
        let dur = min(max(Double(dist) / 600.0, 1.0), 2.0)   // slower = watchable
        let arc = min(dist * 0.2, 80)
        let ctrl = CGPoint(x: (start.x + dst.x) / 2, y: (start.y + dst.y) / 2 + arc)
        let frames = max(Int(dur * 60), 1)
        var i = 0
        view.alpha = 1
        anim = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { t in
            i += 1
            let lp = Double(i) / Double(frames)
            let u = lp * lp * (3 - 2 * lp)            // smoothstep
            let mt = 1 - u
            let bx = mt*mt*start.x + 2*mt*u*ctrl.x + u*u*dst.x
            let by = mt*mt*start.y + 2*mt*u*ctrl.y + u*u*dst.y
            let tx = 2*mt*(ctrl.x - start.x) + 2*u*(dst.x - ctrl.x)
            let ty = 2*mt*(ctrl.y - start.y) + 2*u*(dst.y - ctrl.y)
            self.view.pos = CGPoint(x: bx, y: by)
            self.view.angle = atan2(ty, tx) - .pi / 2   // triangle tip default = up
            self.view.needsDisplay = true
            if i >= frames {
                t.invalidate()
                self.view.angle = 0
                self.view.showLabel = true
                self.view.needsDisplay = true
                NSSound.beep()                       // audible arrival cue
                self.hold()
            }
        }
    }

    func hold() {   // pulse a halo for 8s, then fade out over ~0.7s
        var phase = 0.0
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { _ in
            phase += 0.07
            self.view.halo = 26 + 12 * CGFloat(abs(sin(phase)))   // 26–38 px breathing ring
            self.view.needsDisplay = true
        }
        holdTimer = Timer.scheduledTimer(withTimeInterval: 8.0, repeats: false) { _ in
            self.pulseTimer?.invalidate(); self.pulseTimer = nil
            var a: CGFloat = 1
            self.fadeTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { t in
                a -= 1.0 / 42.0
                self.view.alpha = max(a, 0)
                self.view.needsDisplay = true
                if a <= 0 {
                    t.invalidate()
                    self.fadeTimer = nil
                    self.view.showLabel = false
                    self.view.halo = 0
                }
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let overlay = Overlay()
app.run()
