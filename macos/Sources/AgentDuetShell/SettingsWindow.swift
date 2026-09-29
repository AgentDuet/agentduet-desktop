import AppKit
import Combine
import SwiftUI

/// The native Settings window: a real window, as every Mac app's Settings is — Cmd-comma opens
/// it, the red light closes it, and it can sit beside the main window rather than over it.
///
/// ONE PER APP, reopened rather than rebuilt, so its size and place are remembered and a second
/// Cmd-comma brings the same window forward.
@MainActor final class SettingsWindow: NSObject, NSWindowDelegate {

    private var window: NSWindow?
    private var model: SettingsModel?
    private var titleWatch: AnyCancellable?
    /// Told when the window closes, so the main window can show what changed.
    var onClose: (() -> Void)?

    var nsWindow: NSWindow? { window }

    func show(api: DaemonAPI, host: SettingsHost, section: SettingsModel.Section?) {
        // THE DAEMON MAY HAVE MOVED (a restart keeps the port and token, but a new launch may
        // not), so a model built for an old address is replaced.
        if model?.api.base != api.base || model?.api.token != api.token {
            window?.close()
            window = nil
        }
        if window == nil {
            let model = SettingsModel(api: api)
            model.host = host
            let w = NSWindow(contentViewController: NSHostingController(rootView: SettingsView(model: model)))
            w.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
            w.toolbar = NSToolbar()
            w.toolbarStyle = .unified
            w.title = "Settings"
            w.setContentSize(NSSize(width: 760, height: 540))
            w.minSize = NSSize(width: 680, height: 420)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            w.setFrameAutosaveName("AgentDuetSettings")
            // THE PANE'S NAME IS THE WINDOW'S TITLE, as in System Settings.
            titleWatch = model.$section.sink { [weak w] in w?.title = $0.title }
            self.window = w
            self.model = model
        }
        if let section { model?.section = section }
        model?.start()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func close() { window?.close() }

    func windowWillClose(_ notification: Notification) {
        model?.stop()
        onClose?()
    }
}
