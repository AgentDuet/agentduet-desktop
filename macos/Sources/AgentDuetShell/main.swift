import AppKit

// Top-level code IS the entry point of a SwiftPM executable target, so there is no @main here.
//
// `.regular` rather than `.accessory`: this app has a window and belongs in the Dock and the
// app switcher. When a status-bar mode arrives it will be a choice made here.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
