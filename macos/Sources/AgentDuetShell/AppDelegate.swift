import AppKit
import ServiceManagement
import WebKit

/// The window. It renders the SAME loopback site the browser and the pywebview window render —
/// this replaces the frame, not the app.
///
/// WHY NATIVE AT ALL, given pywebview already works: inside a `--onefile` binary pywebview has
/// no GUI backend to fall back on, the traffic lights are drawn in HTML and have to be hidden
/// when macOS draws its own, and there is nowhere to put a menu bar, a Dock icon or (later) a
/// status item. None of that is reachable from Python here.
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         NSMenuDelegate {

    private var window: NSWindow!
    private var webView: WKWebView!
    /// The menu bar item. THE APP'S ONLY PERSISTENT UI: with `LSUIElement` there is no Dock
    /// icon, so if this is nil the owner has a running phone-answering service and no way to
    /// reach it. Held for the process lifetime deliberately — a released NSStatusItem
    /// disappears from the menu bar.
    private var statusItem: NSStatusItem!
    private var stateItem: NSMenuItem!
    private var loginItem: NSMenuItem!
    private let daemon = Daemon()
    private var siteURL: URL?

    /// `--bg` from app.css. Set on the window so the gap before the first paint is the app's
    /// own colour rather than a white flash.
    private let pageBackground = NSColor(red: 0x18/255.0, green: 0x18/255.0, blue: 0x1b/255.0,
                                         alpha: 1)

    // MARK: - launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildStatusItem()
        buildWindow()
        show(title: "Starting AgentDuet…", detail: "")

        // The daemon takes a second or two to bind, and `Daemon.start()` blocks on a socket
        // probe. Doing that on the main thread would freeze the window it is trying to fill.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.daemon.start()
            DispatchQueue.main.async {
                switch result {
                case .success(let url):
                    self.siteURL = url
                    self.stateItem.title = Self.answering(url)
                    self.webView.load(URLRequest(url: url))
                case .failure(let error):
                    self.stateItem.title = "Not running"
                    self.show(title: "AgentDuet could not start",
                              detail: error.localizedDescription)
                }
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        daemon.stop()
    }

    /// CLOSING THE WINDOW MUST NOT STOP THE PHONE BEING ANSWERED. This returned `true` while
    /// there was nowhere else for the app to live: with no status item, an app with no window
    /// was unreachable, so quitting was at least honest. Now the menu bar item is that place,
    /// so the window is a view onto a service rather than the service itself — and a secretary
    /// that stops taking calls because you closed a window is a bug, not a convention.
    ///
    /// Quitting is explicit: the menu bar item's Quit, or Cmd+Q.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    /// The menu bar item, and the menu behind it.
    ///
    /// Deliberately small: what state it is in, a way back to the window, and a way to quit.
    /// Everything else already exists in the page the window shows, and a menu that grows into
    /// a second interface is how the "one HTML codebase" property gets lost a line at a time.
    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            // A TEMPLATE image, so macOS tints it for a light or dark menu bar. A coloured
            // icon looks wrong in one of the two and there is no way to supply both.
            let symbol = NSImage(systemSymbolName: "phone.badge.waveform",
                                 accessibilityDescription: "AgentDuet Desktop")
                ?? NSImage(systemSymbolName: "phone.fill",
                           accessibilityDescription: "AgentDuet Desktop")
            if let symbol {
                symbol.isTemplate = true
                button.image = symbol
            } else {
                button.title = "AD"      // no SF Symbol available: say something rather than nothing
            }
        }

        let menu = NSMenu()
        // REFRESHED EVERY TIME IT OPENS. The state line used to be written once, when
        // daemon.start() returned, so it said "Answering" for the rest of the session no matter
        // what happened to the daemon afterwards. A status indicator that cannot go wrong is
        // worse than none: it is consulted precisely when something feels broken.
        menu.delegate = self
        stateItem = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open AgentDuet", action: #selector(openWindow), keyEquivalent: "")
        loginItem = NSMenuItem(title: "Start at Login", action: #selector(toggleLoginItem),
                               keyEquivalent: "")
        menu.addItem(loginItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit AgentDuet Desktop",
                     action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        // Items whose action lives on THIS object need it as their target; the Quit item is a
        // responder-chain message and finds NSApp on its own.
        for item in menu.items
        where item.action == #selector(openWindow) || item.action == #selector(toggleLoginItem) {
            item.target = self
        }
        statusItem.menu = menu
    }

    /// The state line, refreshed as the menu opens.
    ///
    /// Free in the common case: we spawned the daemon, so its liveness is a question about a
    /// child process rather than a network round trip — which also keeps a HEAD request per
    /// menu open out of daemon.log. Only the attach case (someone else's daemon, so no Process
    /// to ask) falls back to a probe, shown optimistically and corrected when it answers.
    func menuWillOpen(_ menu: NSMenu) {
        // ASK THE OS, don't remember what we set. A login item can be switched off in System
        // Settings -> General -> Login Items, which is the whole point of using SMAppService
        // rather than writing a plist nobody can see — so our idea of the setting goes stale
        // the moment the owner uses that panel.
        switch SMAppService.mainApp.status {
        case .enabled:
            loginItem.state = .on
            loginItem.title = "Start at Login"
        case .requiresApproval:
            // Registered, but macOS wants the owner to allow it. Saying "on" here would be a
            // lie that costs a support round trip when it does not start.
            loginItem.state = .mixed
            loginItem.title = "Start at Login — allow it in System Settings"
        default:
            loginItem.state = .off
            loginItem.title = "Start at Login"
        }

        switch daemon.spawnedAndAlive {
        case .some(true):
            stateItem.title = siteURL.map(Self.answering) ?? "Answering"
        case .some(false):
            stateItem.title = "Not running"
        case .none:
            stateItem.title = "Checking…"
            daemon.probe { [weak self] up in
                guard let self else { return }
                self.stateItem.title = up ? (self.siteURL.map(Self.answering) ?? "Answering")
                                          : "Not running"
            }
        }
    }

    /// Register or unregister THIS APP as a login item.
    ///
    /// `SMAppService.mainApp` rather than an agent plist we ship: macOS launches the app, which
    /// is LSUIElement and so arrives quietly in the menu bar and starts its own daemon. There is
    /// no path to embed and go stale when the app is moved, and it appears in System Settings ->
    /// General -> Login Items where the owner can switch it off — which a plist written into
    /// ~/Library/LaunchAgents never does.
    @objc private func toggleLoginItem() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
                removeLegacyLaunchAgent()
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not change the login item"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    /// The Python side writes ~/Library/LaunchAgents/<label>.plist for the same purpose
    /// (loginitem.py, which still owns this on Linux and Windows and for a bare CLI install).
    /// Leaving both registered means TWO daemons at login: the second loses the race for 8899
    /// and exits, so the visible symptom is nothing at all — until it is the wrong one that
    /// survived. One mechanism per machine.
    private func removeLegacyLaunchAgent() {
        let plist = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.b3networks.agentduet-desktop.plist")
        guard FileManager.default.fileExists(atPath: plist.path) else { return }
        let unload = Process()
        unload.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        unload.arguments = ["unload", "-w", plist.path]
        try? unload.run()
        unload.waitUntilExit()
        try? FileManager.default.removeItem(at: plist)
    }

    private static func answering(_ url: URL) -> String {
        "Answering — \(url.host ?? "127.0.0.1"):\(url.port ?? 8899)"
    }

    /// Bring the window back after it was closed. Works because `isReleasedWhenClosed` is false
    /// — otherwise this would message a deallocated window and crash.
    @objc private func openWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - window

    private func buildWindow() {
        let config = WKWebViewConfiguration()

        // TELL THE PAGE IT IS IN A NATIVE FRAME. `nativeChrome()` looks for a host object and
        // adds `.native` to <html>, which is what stops the page drawing its own traffic lights
        // under the real ones. In a WKWebView `window.pywebview` does not exist, so without
        // this the window shows TWO sets of lights — the exact bug that hack exists to prevent.
        let script = WKUserScript(source: "window.agentduetNative = true;",
                                  injectionTime: .atDocumentStart,
                                  forMainFrameOnly: false)
        config.userContentController.addUserScript(script)

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")   // no white flash before first paint

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1360, height: 900),
            // `.fullSizeContentView` with a transparent titlebar puts the real traffic lights
            // OVER the page's own titlebar row, which is the layout the mockup draws. app.css
            // reserves the space for them under `html.native`.
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "AgentDuet Desktop"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = pageBackground
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.center()
        // Remembers position and size between launches, keyed by this name. Free, and its
        // absence is noticed immediately by anyone who moves a window.
        window.setFrameAutosaveName("AgentDuetMainWindow")
        // CLOSING MUST NOT DEALLOCATE IT. A programmatically created NSWindow defaults to
        // releasing itself on close, so reopening from the menu bar would message freed memory.
        // With the app no longer quitting on last window close, this is load-bearing.
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// A message rendered in the webview itself, so starting and failing look like the app
    /// rather than like an alert bolted onto it.
    private func show(title: String, detail: String) {
        let escaped = { (s: String) -> String in
            s.replacingOccurrences(of: "&", with: "&amp;")
             .replacingOccurrences(of: "<", with: "&lt;")
             .replacingOccurrences(of: ">", with: "&gt;")
        }
        let html = """
        <!doctype html><meta charset="utf-8">
        <style>
          html,body{height:100%;margin:0;background:#18181b;color:#94a3b8;
            font:14px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;
            display:flex;align-items:center;justify-content:center;}
          .box{max-width:34rem;padding:2rem;text-align:center;}
          h1{font-size:1rem;font-weight:600;color:#e2e8f0;margin:0 0 .6rem;}
          pre{text-align:left;white-space:pre-wrap;font:11px/1.6 ui-monospace,SFMono-Regular,
            monospace;color:#64748b;background:#0f172a;border:1px solid #33333b;
            border-radius:.5rem;padding:.75rem;margin:1rem 0 0;overflow:auto;max-height:16rem;}
        </style>
        <div class="box"><h1>\(escaped(title))</h1>
        \(detail.isEmpty ? "" : "<pre>\(escaped(detail))</pre>")</div>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - navigation

    /// Keep the WINDOW on the local site and send everything else to the real browser.
    ///
    /// The owner's pages link out — a provider's console, ollama.com, a docs page. Opening those
    /// inside the app frame strands the person in a webview with no address bar and no back
    /// button, in what is meant to be their own machine's window.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow); return
        }
        if url.scheme == "http" || url.scheme == "https" {
            let host = url.host ?? ""
            let isLocal = host == "127.0.0.1" || host == "localhost"
            if !isLocal {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel); return
            }
        }
        decisionHandler(.allow)
    }

    /// `target="_blank"` never creates a second webview here; it opens in the browser, for the
    /// same reason as above. Returning nil without this leaves such a link silently dead.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// The page must be able to ask "are you sure?" — deleting a model and turning on recording
    /// both use `confirm()`, and a WKWebView with no UI delegate answers false to every one of
    /// them without showing anything.
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        show(title: "Could not load the AgentDuet window", detail: error.localizedDescription)
    }

    // MARK: - menu

    @objc private func reload() {
        if let url = siteURL { webView.load(URLRequest(url: url)) } else { webView.reload() }
    }

    /// A macOS app with no menu bar has no Cmd+Q, and — less obviously — NO CUT, COPY OR PASTE
    /// inside the webview. Those are menu-driven on this platform, so an app that skips the Edit
    /// menu ships a text field the owner cannot paste an API key into.
    private func buildMenu() {
        let name = "AgentDuet Desktop"
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(name)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(name)", action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(name)", action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        // These are responder-chain messages, so the class named here is only a way to spell
        // the selector — nothing sends them to NSText. Written as strings first, on the theory
        // that #selector would trip over NSObject's own zero-argument `copy`; the compiler
        // disambiguates on the signature and REJECTS the strings, so the theory was wrong.
        // Undo and redo stay strings because no visible declaration exists to point at.
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)),
                         keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSStandardKeyBindingResponding.selectAll(_:)),
                         keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }
}
