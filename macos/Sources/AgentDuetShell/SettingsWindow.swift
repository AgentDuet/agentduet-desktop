import AppKit
import SwiftUI

/// Settings: a SHEET on the hub's window (Stanley, 2026-09-29), so it is part of that window
/// rather than a second one in Cmd-Tab. Cmd-comma, the menu bar menu and the hub's gear open it;
/// Done or Esc closes it. Only while setup is still due, with no hub yet, is it a window of its
/// own.
///
/// ONE PER APP, reopened rather than rebuilt, so a second Cmd-comma brings the same one forward.
@MainActor final class SettingsWindow: NSObject, NSWindowDelegate {

    private var window: NSWindow?
    private var model: SettingsModel?
    /// Told when the window closes, so the main window can show what changed.
    var onClose: (() -> Void)?

    var nsWindow: NSWindow? { window }

    /// Over `parent` as a SHEET when there is one — the hub (Stanley, 2026-09-29: Settings is part
    /// of the hub's window, not a second window in Cmd-Tab) — else a window of its own.
    func show(api: DaemonAPI, host: SettingsHost, section: SettingsModel.Section?, over parent: NSWindow?) {
        // THE DAEMON MAY HAVE MOVED (a restart keeps the port and token, but a new launch may
        // not), so a model built for an old address is replaced.
        if model?.api.base != api.base || model?.api.token != api.token {
            close()
            window = nil
        }
        if window == nil {
            let model = SettingsModel(api: api)
            model.host = host
            model.done = { [weak self] in self?.close() }
            let w = NSWindow(contentViewController: NSHostingController(rootView: SettingsView(model: model)))
            w.styleMask = [.titled, .closable, .resizable, .fullSizeContentView]
            w.title = "Settings"
            w.setContentSize(NSSize(width: 780, height: 560))
            w.minSize = NSSize(width: 680, height: 440)
            w.isReleasedWhenClosed = false
            w.delegate = self
            self.window = w
            self.model = model
        }
        if let section { model?.section = section }
        model?.start()
        guard let w = window else { return }
        if let parent {
            if w.sheetParent == nil { parent.beginSheet(w) }
            parent.makeKeyAndOrderFront(nil)
        } else {
            w.center()
            w.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    var isOpen: Bool { window?.isVisible ?? false }

    func close() {
        guard let w = window, w.isVisible else { return }
        if let parent = w.sheetParent {
            // endSheet does not close the window, so what closing would do is done here.
            parent.endSheet(w)
            finished()
        } else {
            w.close()
        }
    }

    func windowWillClose(_ notification: Notification) { finished() }

    private func finished() {
        model?.stop()
        onClose?()
    }
}
