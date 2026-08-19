// swift-tools-version:5.9
//
// Pointer overlay package.
//
// Single product: the `pointer-overlay` CLI that draws the on-screen pointer
// for the `point_at` tool. It is a borderless, click-through, always-on-top
// window that watches an IPC file and flies a marker to the target.
//
// Packaged the same way as the context-drop `ax-read` helper: source in git,
// binary built on the host by build.sh and gitignored. Nothing ships prebuilt.

import PackageDescription

let package = Package(
    name: "PointerOverlay",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "pointer-overlay", targets: ["pointer-overlay"]),
    ],
    targets: [
        .executableTarget(
            name: "pointer-overlay",
            path: "Sources/pointer-overlay"
        ),
    ]
)
