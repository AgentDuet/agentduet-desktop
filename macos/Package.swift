// swift-tools-version:5.9
//
// The native macOS shell. SwiftPM rather than an Xcode project ON PURPOSE: an .xcodeproj is a
// generated XML blob that cannot be hand-edited or reviewed in a diff, and the only compiler
// this project has is a CI runner — nobody here can open Xcode to repair one. `swift build`
// runs headless, and packaging/make-macos-app.sh assembles the bundle around its output.
import PackageDescription

let package = Package(
    name: "AgentDuetShell",
    // 13 is Ventura. The app targets Apple Silicon, where the floor is 11; 13 is chosen for
    // current WKWebView behaviour rather than to exclude anyone realistic.
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "AgentDuetShell", path: "Sources/AgentDuetShell"),
        // Apple's on-device STT, as a command the PYTHON daemon calls — so it must be its own
        // binary rather than part of the shell: transcription happens on a queue in the daemon,
        // which also runs under the pywebview build and headless with no shell at all.
        .executableTarget(name: "AgentDuetSTT", path: "Sources/AgentDuetSTT")
    ]
)
