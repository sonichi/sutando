// swift-tools-version:5.9
//
// Public context-drop ax-read package.
//
// Single product: the `ax-read` CLI used by Sutando.app's dropContext.
// Reads the focused app's text selection via the macOS Accessibility API
// and emits a JSON line on stdout. Sutando.app's invokeAxRead() in
// main.swift parses that JSON.
//
// The private personal-deictic skill (memory-sync) ships a richer SPM
// package that also includes an AxRead library + screenshot/cursor
// capture. This public skill is the text-only subset Sutando.app needs.

import PackageDescription

let package = Package(
    name: "AxReadPublic",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "ax-read", targets: ["ax-read"]),
    ],
    targets: [
        .executableTarget(
            name: "ax-read",
            path: "Sources/ax-read"
        ),
    ]
)
