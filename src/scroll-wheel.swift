// scroll-wheel.swift — Send OS-level scroll wheel events to Chrome
// Usage: swift scroll-wheel.swift <pixels> [direction]
// Positive pixels = scroll down, negative = scroll up
// This forces Chrome to visually repaint during Zoom screen share
// without stealing focus from other processes (narration recording).

import CoreGraphics
import Foundation

let args = CommandLine.arguments
let pixels = args.count > 1 ? Int(args[1]) ?? -600 : -600

// CGEvent scroll uses line units by default; pixel units give finer control
let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: Int32(pixels), wheel2: 0, wheel3: 0)
if let event = event {
    event.post(tap: .cghidEventTap)
} else {
    fputs("Failed to create scroll event\n", stderr)
    exit(1)
}
