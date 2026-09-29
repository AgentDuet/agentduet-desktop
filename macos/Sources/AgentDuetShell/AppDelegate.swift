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
/// The strip across the top of the page that behaves like a title bar.
///
/// WHY THIS EXISTS. The window is `.fullSizeContentView` with a transparent titlebar, so the
/// page draws its own titlebar row under macOS's real traffic lights — which is the layout the
/// design asks for. The cost is that the WKWebView covers the whole titlebar area and swallows
/// every mouse event in it, so the window could not be DRAGGED and did not respond to a
/// double-click. Both are things every other Mac app does, and their absence reads as the
/// window being broken rather than as a missing feature. Reported by Stanley 2026-09-18.
///
/// The fix is a transparent view sitting ABOVE the web view across that strip, doing the two
/// things AppKit would have done if the titlebar were not covered.
final class TitlebarDragView: NSView {
    /// The page's own titlebar height — `.titlebar{height:2.75rem}` in `app.css`, at a 16px
    /// root. `tests/test_rules.py` compares the two, so changing the CSS fails the suite rather
    /// than silently leaving a drag strip that no longer lines up with what it looks like.
    static let height: CGFloat = 36

    /// Left free on the right so the page's own Settings button stays clickable. Generous on
    /// purpose: a few dead pixels beside a button cost nothing, and a strip that swallows the
    /// button costs the only control up there. Dragging from the brand end is the natural
    /// gesture anyway.
    static let rightInset: CGFloat = 160

    override func mouseDown(with event: NSEvent) {
        guard event.clickCount != 2 else { return doubleClick() }
        // `performDrag` runs its own event loop until the mouse is released, which is what makes
        // this behave exactly like a real titlebar rather than an approximation of one.
        window?.performDrag(with: event)
    }

    /// WHAT A DOUBLE-CLICK DOES IS A SYSTEM PREFERENCE, not our choice. macOS offers zoom,
    /// minimise or nothing under Desktop & Dock, and an app that always zooms is wrong for
    /// everyone who set it to something else. Absent key = Maximize, which is the macOS default.
    private func doubleClick() {
        switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize" {
        case "Minimize": window?.performMiniaturize(nil)
        case "None":     break
        default:         window?.performZoom(nil)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         NSMenuDelegate, WKScriptMessageHandler {

    private var window: NSWindow!
    private var webView: WKWebView!
    /// The menu bar item. THE APP'S ONLY PERSISTENT UI: with `LSUIElement` there is no Dock
    /// icon, so if this is nil the owner has a running phone-answering service and no way to
    /// reach it. Held for the process lifetime deliberately — a released NSStatusItem
    /// disappears from the menu bar.
    private var statusItem: NSStatusItem!
    private var stateItem: NSMenuItem!
    private var loginItem: NSMenuItem!
    private var updateItem: NSMenuItem!
    private let daemon = Daemon()
    /// Whether the owner can be heard, for "Answer calls here" — see MicWatch.
    private var micWatch: MicWatch?
    /// Names for callers from the owner's Contacts — see ContactsWatch.
    private var contactsWatch: ContactsWatch?
    /// The native Settings window — see SettingsWindow.
    private let settingsWindow = SettingsWindow()
    /// The native hub, a preview for now — see HubWindow.
    private let hubWindow = HubWindow()
    /// The native setup window — see SetupWindow.
    private let setupWindow = SetupWindow()
    /// A first run's setup is not finished yet, so "Open AgentDuet" brings setup back rather
    /// than an empty main window.
    private var setupPending = false
    private var siteURL: URL?
    /// Where the update item points, set as the menu opens.
    private var releaseURL: URL?

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
        // THE DOCUMENTS GRANT FIRST: access this process holds is inherited only by a daemon
        // started AFTER it. See FolderAccess.
        FolderAccess.restore(home: daemon.instanceHome)
        micWatch = MicWatch(home: daemon.instanceHome)
        micWatch?.start()
        contactsWatch = ContactsWatch(home: daemon.instanceHome)
        contactsWatch?.start()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.daemon.start()
            DispatchQueue.main.async {
                switch result {
                case .success(let url):
                    self.siteURL = url
                    self.stateItem.title = Self.answering(url)
                    self.showHubOrSetup(url)
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
    /// Everything else already exists in the windows, and a menu grows into a second interface
    /// one item at a time. (Settings is its own native window since 2026-09-29; "Settings…"
    /// here opens it, as Cmd-comma does.)
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
        // A NEWER BUILD, WHEN THERE IS ONE. Hidden the rest of the time rather than reading
        // "up to date": that would be a claim about GitHub made from a cache, and on a machine
        // that has never reached it, a wrong one. Clicking opens the release page — this app
        // does not download or install anything, so the owner is never mid-update.
        updateItem = NSMenuItem(title: "", action: #selector(openRelease), keyEquivalent: "")
        updateItem.isHidden = true
        menu.addItem(updateItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open AgentDuet", action: #selector(openWindow), keyEquivalent: "")
        menu.addItem(withTitle: "Settings…", action: #selector(openSettingsItem), keyEquivalent: "")
        loginItem = NSMenuItem(title: "Start at Login", action: #selector(toggleLoginItem),
                               keyEquivalent: "")
        menu.addItem(loginItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit AgentDuet Desktop",
                     action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        // Items whose action lives on THIS object need it as their target; the Quit item is a
        // responder-chain message and finds NSApp on its own.
        for item in menu.items
        where item.action == #selector(openWindow) || item.action == #selector(toggleLoginItem)
              || item.action == #selector(openRelease) || item.action == #selector(openSettingsItem) {
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

        // RE-READ EVERY OPEN, for the same reason the state line is: the daemon writes this
        // hours after launch, so a value read once at startup would be the one thing that
        // could never show a release.
        if let notice = daemon.updateNotice {
            updateItem.title = notice.note
            releaseURL = notice.url
            updateItem.isHidden = false
        } else {
            updateItem.isHidden = true
            releaseURL = nil
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

    @objc private func openRelease() {
        if let url = releaseURL { NSWorkspace.shared.open(url) }
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
        if setupPending { showSetup(rerun: false); return }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Reopening lays the titlebar out afresh, so the lights need putting back.
        centreWindowButtons()
    }

    // MARK: - the page asking the shell

    /// THREE REQUESTS, AND EACH ONLY OPENS A DIALOG: `pickDocuments` from setup's Permissions
    /// step (the panel opens in Documents) and `pickFolder` from Settings' Change (it opens in the
    /// folder in use), both in a sandboxed build; and `askContacts`, the macOS Contacts prompt.
    /// Anything else is ignored. The owner's answer in the system dialog is the grant, so nothing
    /// the page sends can grant access by itself.
    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }
        if type == "openSettings" {
            openSettings(SettingsModel.Section(rawValue: body["section"] as? String ?? ""))
            return
        }
        if type == "askContacts" {
            contactsWatch?.ask { [weak self] ok in
                self?.webView.evaluateJavaScript(
                    "window.agentduetContacts && window.agentduetContacts(\(ok))")
            }
            return
        }
        guard type == "pickDocuments" || type == "pickFolder" else { return }
        let start = type == "pickFolder" ? FolderAccess.current(home: daemon.instanceHome) : nil
        FolderAccess.ask(over: window, startingIn: start) { [weak self] url in
            guard let self else { return }
            guard let url else { self.tellPage(["ok": false]); return }
            self.keepFolder(url) { error in
                if let error { self.tellPage(["ok": false, "error": error]) }
                else { self.tellPage(["ok": true, "path": url.path]) }
            }
        }
    }

    /// Keep a folder the owner chose in the sandbox's panel, and restart the daemon with it.
    /// `done` gets nil, or what went wrong.
    ///
    /// RESTARTED because access this process gains is inherited only by a child started after
    /// it. Same port and token, so the pages carry on where they were.
    private func keepFolder(_ url: URL, done: @escaping (String?) -> Void) {
        do {
            try FolderAccess.save(url, home: daemon.instanceHome)
            FolderAccess.restore(home: daemon.instanceHome)
        } catch {
            done(error.localizedDescription); return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            self.daemon.stop()
            let result = self.daemon.start()
            DispatchQueue.main.async {
                if case .success = result { done(nil) } else { done("The service did not come back.") }
            }
        }
    }

    // MARK: - the native setup window

    /// THE NATIVE SETUP (2026-09-29) when the daemon says setup is needed — the same question
    /// `index` asks to serve setup.html — else the hub. If the daemon cannot be asked, the page
    /// is loaded, and the daemon itself serves whichever of the two applies.
    private func showHubOrSetup(_ url: URL) {
        guard let api = DaemonAPI(site: url) else { webView.load(URLRequest(url: url)); return }
        Task { @MainActor in
            let cur = await api.get("/api/setup/current")
            if cur.bool("needs_setup") { self.showSetup(rerun: false) }
            else {
                self.webView.load(URLRequest(url: url))
                // FOR A LOOK WITHOUT CLICKING: `open … --args --open-hub-preview` opens the
                // native hub at launch, so it can be screenshotted from a script.
                if CommandLine.arguments.contains("--open-hub-preview") { self.openHubPreview() }
            }
        }
    }

    private func showSetup(rerun: Bool) {
        guard let url = siteURL, let api = DaemonAPI(site: url) else { return }
        if !rerun { setupPending = true }
        settingsWindow.close()
        window.orderOut(nil)
        setupWindow.onFinish = { [weak self] in
            guard let self, let url = self.siteURL else { return }
            self.setupPending = false
            self.webView.load(URLRequest(url: url))
            self.openWindow()
        }
        setupWindow.onQuit = { NSApp.terminate(nil) }
        setupWindow.show(api: api, rerun: rerun)
    }

    // MARK: - the native Settings window

    @objc private func openSettingsItem() { openSettings(nil) }

    @objc private func openHubPreview() {
        guard let url = siteURL, let api = DaemonAPI(site: url) else { return }
        hubWindow.openSettings = { [weak self] in self?.openSettings(nil) }
        hubWindow.show(api: api)
    }

    /// THE NATIVE SETTINGS (2026-09-29), from Cmd-comma, the menu bar menu, or the page's own
    /// Settings button. Needs the daemon's address; before it has one there is nothing to set.
    private func openSettings(_ section: SettingsModel.Section?) {
        guard let url = siteURL, let api = DaemonAPI(site: url) else { return }
        settingsWindow.onClose = { [weak self] in
            // What changed there — a name, a folder, a sign-in — shows in the main window now.
            self?.webView.evaluateJavaScript("window.agentduetSettingsClosed && window.agentduetSettingsClosed()")
        }
        settingsWindow.show(api: api, host: self, section: section)
    }

    private func tellPage(_ result: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: result),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.agentduetPicked && window.agentduetPicked(\(json))")
    }

    // MARK: - window

    private func buildWindow() {
        let config = WKWebViewConfiguration()

        // TELL THE PAGE IT IS IN A NATIVE FRAME. `nativeChrome()` looks for a host object and
        // adds `.native` to <html>, which is what stops the page drawing its own traffic lights
        // under the real ones. In a WKWebView `window.pywebview` does not exist, so without
        // this the window shows TWO sets of lights — the exact bug that hack exists to prevent.
        // `agentduetNativeSettings`: the page's Settings button asks for the native window
        // rather than opening the HTML one (see `openSettings`).
        let script = WKUserScript(source: "window.agentduetNative = true; window.agentduetNativeSettings = true;",
                                  injectionTime: .atDocumentStart,
                                  forMainFrameOnly: false)
        config.userContentController.addUserScript(script)
        // THE PAGE CAN ASK FOR DIALOGS ONLY: a folder panel or the Contacts prompt (see
        // `userContentController`).
        config.userContentController.add(self, name: "agentduet")

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
        // A CONTAINER, not the web view itself, so the drag strip can sit ABOVE it. Adding a
        // subview to a WKWebView works but reaches into WebKit's own view tree; a plain host
        // view keeps the two siblings and the z-order ours to state.
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 1360, height: 900))
        webView.frame = container.bounds
        webView.autoresizingMask = [.width, .height]
        container.addSubview(webView)

        let dragStrip = TitlebarDragView(frame: NSRect(
            x: 0, y: container.bounds.height - TitlebarDragView.height,
            width: container.bounds.width - TitlebarDragView.rightInset,
            height: TitlebarDragView.height))
        // Pinned to the TOP and stretching with the width: `.maxYMargin` would pin it to the
        // bottom, which is the easy way to get a drag strip that drifts off the titlebar the
        // first time the window is resized.
        dragStrip.autoresizingMask = [.width, .minYMargin]
        container.addSubview(dragStrip, positioned: .above, relativeTo: webView)

        window.contentView = container
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

        // AFTER makeKeyAndOrderFront, not before: ordering the window in is what builds the
        // titlebar, so a call ahead of it finds no buttons to move and returns silently.
        centreWindowButtons()
        // And again whenever AppKit re-lays the titlebar out on its own schedule — a resize, a
        // fullscreen exit, becoming key — each of which puts the buttons back where IT wants
        // them. Without this the lights drift up the first time the window is resized.
        for name in [NSWindow.didResizeNotification, NSWindow.didBecomeKeyNotification,
                     NSWindow.didExitFullScreenNotification] {
            NotificationCenter.default.addObserver(forName: name, object: window, queue: .main) {
                [weak self] _ in self?.centreWindowButtons()
            }
        }
    }

    /// Put the traffic lights on the page's own titlebar centre line.
    ///
    /// macOS centres them in ITS 28pt titlebar, so in a taller bar they sit high — 4pt high at
    /// 36pt, which is enough to read as "the buttons are stuck to the top" even when everything
    /// else in the row is centred. Chrome solves it the same way.
    ///
    /// Views are NOT flipped here, so `origin.y` counts from the BOTTOM of the titlebar
    /// container. Centring on `height/2` from the top therefore means placing the button
    /// `container.height - height/2 - button.height/2` up from the bottom.
    private func centreWindowButtons() {
        let buttons = [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton]
            .compactMap { window.standardWindowButton($0) }
        guard let container = buttons.first?.superview else { return }
        for b in buttons {
            let y = container.bounds.height - TitlebarDragView.height / 2 - b.frame.height / 2
            b.setFrameOrigin(NSPoint(x: b.frame.origin.x, y: y))
        }
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
        // System Settings, by its own scheme — the phone's "turn the microphone on" link. The web
        // view cannot load it, so hand exactly this scheme to macOS and nothing broader.
        if url.scheme == "x-apple.systempreferences" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel); return
        }
        if url.scheme == "http" || url.scheme == "https" {
            let host = url.host ?? ""
            let isLocal = host == "127.0.0.1" || host == "localhost"
            if !isLocal {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel); return
            }
            // SETUP IS NATIVE on a Mac: a link to the HTML wizard opens the setup window.
            if url.path == "/setup" {
                decisionHandler(.cancel)
                showSetup(rerun: true); return
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

    /// THE IN-APP PHONE needs the microphone. A WKWebView with no answer here DENIES every
    /// `getUserMedia` silently, so pressing Answer would fail with nothing on screen. Granted for
    /// our own loopback page only, and microphone only; macOS still shows its own permission
    /// prompt the first time, driven by NSMicrophoneUsageDescription in Info.plist.
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let ours = origin.host == "127.0.0.1" || origin.host == "localhost"
        decisionHandler(ours && type == .microphone ? .grant : .deny)
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
        // Cmd-comma, where every Mac app keeps its Settings.
        let settingsItem = NSMenuItem(title: "Settings…", action: #selector(openSettingsItem),
                                      keyEquivalent: ",")
        settingsItem.target = self
        appMenu.addItem(settingsItem)
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
        viewMenu.addItem(.separator())
        // THE NATIVE HUB, A PREVIEW until it can reply and take calls (see HubWindow).
        let hubItem = NSMenuItem(title: "Native Hub Preview", action: #selector(openHubPreview),
                                 keyEquivalent: "H")          // Cmd-Shift-H
        hubItem.target = self
        viewMenu.addItem(hubItem)
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }
}

// MARK: - what the Settings window asks of the app

extension AppDelegate: SettingsHost {
    func chooseFolder(startingIn start: URL?, sandboxed: Bool, done: @escaping (URL?) -> Void) {
        let over = NSApp.keyWindow ?? settingsWindow.nsWindow
        if sandboxed {
            // The panel IS the grant: keep it, and restart the daemon with it.
            FolderAccess.ask(over: over, startingIn: start ?? FolderAccess.current(home: daemon.instanceHome)) {
                [weak self] url in
                guard let self, let url else { done(nil); return }
                self.keepFolder(url) { error in done(error == nil ? url : nil) }
            }
            return
        }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = start
        panel.prompt = "Choose"
        panel.message = "Location to store AgentDuet recordings and transcripts"
        let finish: (NSApplication.ModalResponse) -> Void = { r in done(r == .OK ? panel.url : nil) }
        if let over { panel.beginSheetModal(for: over, completionHandler: finish) }
        else { finish(panel.runModal()) }
    }

    func grantDocuments(done: @escaping (Bool) -> Void) {
        FolderAccess.ask(over: NSApp.keyWindow ?? settingsWindow.nsWindow) { [weak self] url in
            guard let self, let url else { done(false); return }
            self.keepFolder(url) { error in done(error == nil) }
        }
    }

    func askContacts(done: @escaping (Bool) -> Void) {
        guard let contactsWatch else { done(false); return }
        contactsWatch.ask(done: done)
    }

    func runSetup() { showSetup(rerun: true) }
}
