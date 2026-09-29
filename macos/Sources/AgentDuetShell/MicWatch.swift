import AVFoundation
import CoreAudio
import Foundation
import IOKit

/// Whether the owner could be heard right now, told to the daemon (Stanley, 2026-09-29).
///
/// "Answer calls here" is the owner's INTENT and stays as they set it. Whether a call may ring in
/// the window is a separate, live question — and one only this process can answer, because the
/// lid and the input device are macOS facts the page and the daemon cannot see:
///
///   blocked  microphone access refused in System Settings
///   none     no input device at all
///   lid      the lid is closed and the input is the built-in microphone, which macOS then
///            silences in hardware while still listing it as live (CLAUDE.md, the closed-lid
///            gotcha) — an external microphone with the lid closed is fine
///   ok       anything else
///
/// Written to `run/mic-state.json` every two seconds, which doubles as a heartbeat: the daemon
/// treats a stale file as unknown rather than trusting an answer from a shell that has gone.
/// Polled rather than subscribed: two cheap reads, and one loop covers the lid, a device plugged
/// in or pulled, and AirPods dropping, without three kinds of notification to keep alive.
final class MicWatch {

    private let file: URL
    private var timer: Timer?

    init(home: URL) { file = home.appendingPathComponent("run/mic-state.json") }

    func start() {
        publish()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.publish()
        }
    }

    /// `AppleClamshellState` on the power domain — the value `ioreg` shows.
    static func lidClosed() -> Bool {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard root != 0 else { return false }
        defer { IOObjectRelease(root) }
        let value = IORegistryEntryCreateCFProperty(root, "AppleClamshellState" as CFString,
                                                    kCFAllocatorDefault, 0)?.takeRetainedValue()
        return (value as? Bool) ?? false
    }

    /// The input macOS would record from — the one the window's `getUserMedia` gets.
    static func defaultInput() -> (exists: Bool, builtIn: Bool, name: String) {
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil,
                                         &size, &device) == noErr,
              device != AudioDeviceID(kAudioObjectUnknown) else { return (false, false, "") }
        var transport: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        address.mSelector = kAudioDevicePropertyTransportType
        _ = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &transport)
        var name: Unmanaged<CFString>?
        size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        address.mSelector = kAudioObjectPropertyName
        _ = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name)
        return (true, transport == kAudioDeviceTransportTypeBuiltIn,
                (name?.takeRetainedValue() as String?) ?? "")
    }

    func state() -> [String: Any] {
        let access = AVCaptureDevice.authorizationStatus(for: .audio)
        let input = Self.defaultInput()
        let lid = Self.lidClosed()
        let state: String
        if access == .denied || access == .restricted { state = "blocked" }
        else if !input.exists { state = "none" }
        else if input.builtIn && lid { state = "lid" }
        else { state = "ok" }
        return ["state": state, "device": input.name, "lid_closed": lid,
                "access": access == .authorized ? "allowed"
                    : access == .notDetermined ? "not-asked" : "refused",
                "at": Date().timeIntervalSince1970]
    }

    private func publish() {
        guard let data = try? JSONSerialization.data(withJSONObject: state()) else { return }
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? data.write(to: file, options: .atomic)
    }
}
