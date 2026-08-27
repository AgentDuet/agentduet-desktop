import AppKit
import WebKit

/// The window. It renders the SAME loopback site the browser and the pywebview window render —
/// this replaces the frame, not the app.
///
/// WHY NATIVE AT ALL, given pywebview already works: inside a `--onefile` binary pywebview has
/// no GUI backend to fall back on, the traffic lights are drawn in HTML and have to be hidden
/// when macOS draws its own, and there is nowhere to put a menu bar, a Dock icon or (later) a
/// status item. None of that is reachable from Python here.
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {

    private var window: NSWindow!
    private var webView: WKWebView!
    private let daemon = Daemon()
    private var siteURL: URL?

    /// `--bg` from app.css. Set on the window so the gap before the first paint is the app's
    /// own colour rather than a white flash.
    private let pageBackground = NSColor(red: 0x18/255.0, green: 0x18/255.0, blue: 0x1b/255.0,
                                         alpha: 1)

    // MARK: - launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
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
                    self.webView.load(URLRequest(url: url))
                case .failure(let error):
                    self.show(title: "AgentDuet could not start",
                              detail: error.localizedDescription)
                }
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        daemon.stop()
    }

    /// Closing the window ends the run, which is what a person expects of an app window.
    /// Running headless is `agentduet-desktop run --no-window`; a status-bar item is the better
    /// answer for "keep answering with no window" and is not built, so the honest behaviour is
    /// the predictable one.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

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
