import AppKit
import AVFoundation
import ServiceManagement

// Top-level code IS the entry point of a SwiftPM executable target, so there is no @main here.

// A COMMAND, NOT THE APP — answered before anything becomes an application.
//
// `uninstall` must unregister the login item while this bundle still exists, and only this
// bundle can: SMAppService.mainApp means "the caller's own app", so the Python CLI asking for it
// would be asking about a binary with no bundle. Once the .app is in the Trash, nothing can
// unregister it and the entry dangles in System Settings -> Login Items for ever.
// THE OTHER HALF OF THE SAME COMMAND. The wizard asks the owner once, with the box ticked, and
// something has to act on the answer — but the Python daemon cannot: SMAppService.mainApp is
// "the caller's own app", and that CLI binary has no bundle. So it runs this, exactly as
// uninstall already runs --unregister-login-item, and the plist mechanism in loginitem.py is
// left for the platforms and installs that have no bundle at all.
//
// Registering here rather than reconciling a setting at launch is deliberate: the owner ticks
// the box and it is registered NOW, there is one source of truth instead of a settings file that
// can disagree with the system, and the menu bar toggle keeps working unchanged.
// ASKING, not changing. The settings page had its own idea of whether this machine starts at
// login — a `## Start at login` line in settings.md — while the menu bar reported the real
// SMAppService registration, and nothing reconciled the two. An instance restored from before
// the setting existed therefore showed "off" beside a menu bar saying "on". Only the bundle can
// ask the question, so the bundle answers it and the setting becomes the fallback for platforms
// that have no bundle at all.
// THE MICROPHONE, WITHOUT WEBKIT. A diagnostic: the in-app phone's page got a live, unmuted track
// that carried exact zeros, with no orange dot — so macOS never opened the microphone for it. This
// opens it natively, from this bundle, so its permission is the one being tested, and reports
// the permission state and the loudest sample over two seconds. Run it as the app, so macOS
// attributes it to the bundle rather than to a terminal:
//     open -n -W "AgentDuet Dev.app" --args --mic-probe /path/to/result.txt
if let i = CommandLine.arguments.firstIndex(of: "--mic-probe") {
    final class Peak: @unchecked Sendable { var value: Float = 0 }
    let out = i + 1 < CommandLine.arguments.count ? CommandLine.arguments[i + 1] : "/dev/stdout"
    let asked = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { _ in asked.signal() }
    asked.wait()
    let auth = AVCaptureDevice.authorizationStatus(for: .audio).rawValue   // 3 = authorized
    let engine = AVAudioEngine()
    let input = engine.inputNode
    let format = input.outputFormat(forBus: 0)
    let peak = Peak()
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
        guard let samples = buffer.floatChannelData?[0] else { return }
        for k in 0..<Int(buffer.frameLength) { peak.value = max(peak.value, abs(samples[k])) }
    }
    var failure = ""
    do { try engine.start() } catch { failure = "\(error)" }
    Thread.sleep(forTimeInterval: 2.0)
    engine.stop()
    let line = "auth=\(auth) rate=\(format.sampleRate) peak=\(Int(peak.value * 32767)) error=\(failure)\n"
    try? line.write(toFile: out, atomically: true, encoding: .utf8)
    exit(0)
}

if CommandLine.arguments.contains("--login-item-status") {
    switch SMAppService.mainApp.status {
    case .enabled:          print("enabled")
    case .requiresApproval: print("requires-approval")
    case .notFound:         print("not-registered")
    case .notRegistered:    print("not-registered")
    @unknown default:       print("unknown")
    }
    exit(0)
}

if CommandLine.arguments.contains("--register-login-item") {
    // ALREADY ENABLED IS NOT A FAILURE, the mirror of the case below: register() on a live
    // registration throws, and setup may legitimately be re-run by an owner who already had it
    // on. Saying so and exiting 0 is the honest answer to "make sure this is on".
    if SMAppService.mainApp.status == .enabled {
        print("already starts at login")
        exit(0)
    }
    do {
        try SMAppService.mainApp.register()
        // BOTH REGISTERED MEANS TWO DAEMONS AT LOGIN, and the loser of the race for 8899 exits
        // silently. The menu bar toggle clears this same plist for the same reason.
        let plist = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.b3networks.agentduet-desktop.plist")
        if FileManager.default.fileExists(atPath: plist.path) {
            let unload = Process()
            unload.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            unload.arguments = ["unload", "-w", plist.path]
            try? unload.run()
            unload.waitUntilExit()
            try? FileManager.default.removeItem(at: plist)
        }
        // .requiresApproval is not an error: macOS can park a new registration pending the
        // owner's approval in System Settings. Reported rather than swallowed, because the
        // difference between "on" and "waiting for you" is the whole answer.
        if SMAppService.mainApp.status == .requiresApproval {
            print("needs approval in System Settings -> General -> Login Items")
        } else {
            print("starts at login")
        }
        exit(0)
    } catch {
        FileHandle.standardError.write(
            "could not set it to start at login: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
}

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
