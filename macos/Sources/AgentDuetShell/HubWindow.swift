import AppKit
import SwiftUI

/// The native hub, as a PREVIEW window beside the working one (2026-09-29). It reads; replying,
/// the assistant and the in-app phone come next, and until they do the main window stays the hub.
@MainActor final class HubWindow: NSObject, NSWindowDelegate {

    private var window: NSWindow?
    private var model: HubModel?

    /// Settings, which the app opens.
    var openSettings: (() -> Void)?

    func show(api: DaemonAPI) {
        if model?.api.base != api.base || model?.api.token != api.token {
            window?.close()
            window = nil
        }
        if window == nil {
            let model = HubModel(api: api)
            model.openSettings = { [weak self] in self?.openSettings?() }
            let hosting = NSHostingController(rootView: HubView(model: model))
            // THE TOOLBAR AND SEARCH FIELD ARE SWIFTUI'S, and reach a window we built ourselves
            // only when the hosting controller is told to pass them through (macOS 14 and later;
            // on 13 the window has no Edit or search, and the rest works).
            if #available(macOS 14.0, *) { hosting.sceneBridgingOptions = [.toolbars] }
            let w = NSWindow(contentViewController: hosting)
            w.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
            w.toolbarStyle = .unified
            w.title = "AgentDuet"
            w.setContentSize(NSSize(width: 1100, height: 740))
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            w.setFrameAutosaveName("AgentDuetHubPreview")
            self.window = w
            self.model = model
        }
        model?.start()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) { model?.stop() }
}
