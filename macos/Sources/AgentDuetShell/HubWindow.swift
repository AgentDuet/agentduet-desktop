import AppKit
import SwiftUI

/// The hub: the app's main window (2026-09-29), native since the HTML hub was retired on the Mac.
///
/// THE PHONE OUTLIVES THE WINDOW. It stays connected for as long as the app runs, so a call that
/// rings while the window is closed brings it back to be answered — the page could ring only
/// while it was open, and a closed one let the call pass straight through.
@MainActor final class HubWindow: NSObject, NSWindowDelegate {

    private var window: NSWindow?
    private var model: HubModel?
    private var phone: PhoneModel?
    /// Settings, which the app opens — at a section, or where it was.
    var openSettings: ((String?) -> Void)?

    var isVisible: Bool { window?.isVisible ?? false }
    /// For Settings, which is a sheet on this window.
    var nsWindow: NSWindow? { window }

    func show(api: DaemonAPI) {
        if model?.api.base != api.base || model?.api.token != api.token {
            phone?.stop()
            window?.close()
            window = nil
            phone = nil
        }
        if window == nil {
            let model = HubModel(api: api)
            model.openSettings = { [weak self] in self?.openSettings?($0) }
            let phone = PhoneModel(api: api)
            phone.onRing = { [weak self] in self?.bringForward() }
            let hosting = NSHostingController(rootView: HubView(model: model, phone: phone))
            // THE TOOLBAR IS SWIFTUI'S, and reaches a window we built only when the hosting
            // controller passes it through (macOS 14 and later).
            if #available(macOS 14.0, *) { hosting.sceneBridgingOptions = [.toolbars] }
            let w = NSWindow(contentViewController: hosting)
            w.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
            w.toolbarStyle = .unified
            w.title = "AgentDuet"
            // DRAWN BY THE TOOLBAR instead, beside the connection status (HubView).
            w.titleVisibility = .hidden
            w.setContentSize(NSSize(width: 1100, height: 740))
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            w.setFrameAutosaveName("AgentDuetHub")
            self.window = w
            self.model = model
            self.phone = phone
            phone.start()
        }
        bringForward()
    }

    private func bringForward() {
        model?.start()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Reload, from the View menu: fetch everything again.
    func reload() { Task { await model?.load() } }

    /// Closing stops the polling, not the phone.
    func windowWillClose(_ notification: Notification) { model?.stop() }

    /// Quitting.
    func shutDown() {
        phone?.stop()
        model?.stop()
    }
}
