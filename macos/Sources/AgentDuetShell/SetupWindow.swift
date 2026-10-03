import AppKit
import SwiftUI

/// The native setup window: shown instead of the main window until setup is done, and again when
/// the owner walks through it from Settings.
@MainActor final class SetupWindow: NSObject, NSWindowDelegate {

    private var window: NSWindow?
    private var model: SetupModel?
    /// Setup finished, or was cancelled on a walk through again: show the hub.
    var onFinish: (() -> Void)?
    /// Quit from a first run: the daemon has stopped.
    var onQuit: (() -> Void)?

    var nsWindow: NSWindow? { window }
    var isOpen: Bool { window?.isVisible ?? false }

    func show(api: DaemonAPI, rerun: Bool) {
        if let window, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let model = SetupModel(api: api, rerun: rerun)
        model.onFinish = { [weak self] in self?.finish() }
        model.onQuit = { [weak self] in self?.onQuit?() }
        let w = NSWindow(contentViewController: NSHostingController(rootView: SetupView(model: model)))
        w.styleMask = [.titled, .closable, .miniaturizable]
        w.title = "\(Edition.product) Setup"
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        self.window = w
        self.model = model
        Task { await model.start() }
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func finish() {
        let done = onFinish
        onFinish = nil          // closing below must not report it twice
        window?.close()
        done?()
    }

    /// The red light. On a walk through again it is Cancel, so the hub comes back. On a first run
    /// it closes nothing else: the app stays in the menu bar, and opening it brings setup back.
    func windowWillClose(_ notification: Notification) {
        let rerun = model?.rerun ?? false
        model?.stop()
        model = nil
        if rerun, let done = onFinish {
            onFinish = nil
            done()
        }
    }
}
