import AppKit
import ServiceManagement

// Top-level code IS the entry point of a SwiftPM executable target, so there is no @main here.

// A COMMAND, NOT THE APP — answered before anything becomes an application.
//
// `uninstall` must unregister the login item while this bundle still exists, and only this
// bundle can: SMAppService.mainApp means "the caller's own app", so the Python CLI asking for it
// would be asking about a binary with no bundle. Once the .app is in the Trash, nothing can
// unregister it and the entry dangles in System Settings -> Login Items for ever.
if CommandLine.arguments.contains("--unregister-login-item") {
    // NOT REGISTERED IS NOT A FAILURE. unregister() on a service that was never registered
    // raises "Operation not permitted", and uninstall calls this unconditionally — so without
    // this check the common case (nobody ever switched the toggle on) reports an alarming error
    // for having nothing to do.
    if SMAppService.mainApp.status != .enabled {
        print("login item was not registered")
        exit(0)
    }
    do {
        try SMAppService.mainApp.unregister()
        print("login item unregistered")
        exit(0)
    } catch {
        FileHandle.standardError.write(
            "could not unregister the login item: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

// `.accessory`, NOT `.regular` — this is the runtime half of being a menu bar app, and the plist
// key alone does not achieve it. Info.plist says LSUIElement=true, but this line used to say
// `.regular` and PROMOTED the app straight back into the Dock, so the icon was still there and
// `NSRunningApplication.activationPolicy` read 0. The plist was checked and believed; the
// running app was not, which is the whole lesson.
//
// Accessory apps still show windows and still activate — what they lose is the Dock tile and the
// app-switcher entry, which is exactly the trade for living in the menu bar.
app.setActivationPolicy(.accessory)
app.run()
