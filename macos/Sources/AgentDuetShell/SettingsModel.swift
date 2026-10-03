import AVFoundation
import AppKit
import Foundation

/// What only the app itself can do for the Settings window: the system panels and the window.
@MainActor protocol SettingsHost: AnyObject {
    /// A folder for recordings. In the sandbox the host also keeps the grant and restarts the
    /// daemon with it, since the panel IS the grant there; `done` gets nil when cancelled.
    func chooseFolder(startingIn: URL?, sandboxed: Bool, done: @escaping (URL?) -> Void)
    /// The Documents panel, in the sandbox only — see FolderAccess.
    func grantDocuments(done: @escaping (Bool) -> Void)
    func askContacts(done: @escaping (Bool) -> Void)
    /// Setup again, in the main window.
    func runSetup()
    /// Help › Export Logs…: the daemon's zip, saved where the owner chooses.
    func exportLogs()
}

/// The native Settings window's state and actions (2026-09-29).
///
/// Every value comes from, and every change goes to, the daemon's API — the same routes the HTML
/// Settings uses — so the two cannot disagree about what a setting is. What is NOT asked of the
/// daemon is what macOS answers directly: microphone and Contacts access, and the login item's
/// real registration is the daemon's own question of this app, so it is left to it.
///
/// NO SAVE BUTTONS. A switch or a menu applies when changed; a text field when the owner presses
/// Return or leaves it. What needs a moment of commitment — a key checked with the server, a
/// model override that may start a download — is an Edit… sheet with Cancel and Save.
@MainActor final class SettingsModel: ObservableObject {

    enum Section: String, CaseIterable, Identifiable {
        case account, calls, permissions, advanced, about
        var id: String { rawValue }
        var title: String {
            switch self {
            case .account: return "Account"
            // THE RECORDER RECORDS AND NOTHING MORE: no transcription to name.
            case .calls: return Edition.recorder ? "Recording" : "Record & Transcribe"
            case .permissions: return "Permissions"
            case .advanced: return "Advanced"
            case .about: return "About"
            }
        }
        var symbol: String {
            switch self {
            case .account: return "person.crop.circle"
            case .calls: return "phone"
            case .permissions: return "hand.raised"
            case .advanced: return "gearshape.2"
            case .about: return "info.circle"
            }
        }
    }

    struct Notice: Equatable { let ok: Bool; let text: String }

    @Published var section: Section = .account
    @Published private(set) var cur: JSON = [:]
    @Published private(set) var panel: JSON = [:]
    @Published private(set) var about: JSON = [:]
    @Published private(set) var perms: JSON = [:]
    #if !RECORDER
    @Published private(set) var stt: JSON = [:]
    /// The decision model's download: {ready, mb, got_mb, running}.
    @Published private(set) var decider: JSON = [:]
    #endif
    @Published private(set) var mic = "not-asked"
    @Published private(set) var contacts = "not-asked"
    @Published var notice: [Section: Notice] = [:]
    @Published var checking = false

    /// What the text fields show. Loaded once and after each save, never by the poll — the poll
    /// must not overwrite what the owner is typing.
    @Published var name = ""
    @Published var phone = ""
    private var saved: [String: String] = [:]

    let api: DaemonAPI
    weak var host: SettingsHost?
    /// Done: close the sheet.
    var done: (() -> Void)?
    private var timer: Timer?

    init(api: DaemonAPI) { self.api = api }

    // MARK: - loading

    func start() {
        Task { await load() }
        timer?.invalidate()
        // Two seconds, while the window is open: downloads, a permission answered in System
        // Settings, a sign-in finishing in the browser. Stopped when the window closes.
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.poll() }
        }
    }

    func stop() { timer?.invalidate(); timer = nil }

    func load() async {
        await poll()
        name = cur.str("name"); phone = cur.str("phone")
        saved = ["name": name, "phone": phone]
        about = await api.get("/api/about")
    }

    /// EACH ANSWER IS SHOWN AS IT ARRIVES (2026-09-30). All four were awaited together, so one
    /// slow route froze the whole window: a tester pressed Allow for Documents, the grant went
    /// through, and the row still read "Allow" until the app was restarted.
    func poll() async {
        mic = Self.micAccess()
        contacts = ContactsWatch.access()
        let api = self.api
        await withTaskGroup(of: Void.self) { group in
            group.addTask { let v = await api.get("/api/permissions"); await MainActor.run { self.perms = v } }
            group.addTask { let v = await api.get("/api/setup/current"); await MainActor.run { self.cur = v } }
            group.addTask { let v = await api.get("/api/panel"); await MainActor.run { self.panel = v } }
            #if !RECORDER
            group.addTask { let v = await api.get("/api/setup/stt"); await MainActor.run { self.stt = v } }
            group.addTask { let v = await api.get("/api/setup/decider"); await MainActor.run { self.decider = v } }
            #endif
        }
    }

    static func micAccess() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return "allowed"
        case .notDetermined: return "not-asked"
        default: return "refused"
        }
    }

    private func say(_ section: Section, _ reply: JSON, fallback: String = "") {
        let text = reply.str("message").isEmpty ? fallback : reply.str("message")
        notice[section] = text.isEmpty ? nil : Notice(ok: reply["ok"] as? Bool ?? true, text: text)
    }

    // MARK: - Account

    /// Yours where one was saved, else the line learned from calls.
    var yourNumber: String { phone.isEmpty ? panel.str("line") : phone }

    var oauth: JSON { cur.obj("oauth") }
    var signedIn: Bool { oauth.bool("signed_in") }
    var byKey: Bool { !signedIn && !cur.str("key_hint").isEmpty }
    var canSignIn: Bool { oauth.bool("available") && oauth.bool("browser") && !signedIn }
    var connected: Bool { cur.bool("connected") }
    /// NOT SIGNED IN IS NOT "NOT CONNECTED": with no credential the fix is to sign in; with one
    /// and the channel down, it is something else.
    var connectionState: String {
        if connected { return "Connected" }
        return !signedIn && !byKey ? "Not Signed In" : "Not Connected"
    }

    /// Return, or leaving the field. Only a field that changed is written.
    func commit(_ field: String) {
        let value = field == "name" ? name : phone
        guard value != saved[field] else { return }
        Task {
            let r = await api.post("/api/setup/setting", ["field": field, "value": value])
            if r["ok"] as? Bool != false { saved[field] = value }
            say(.account, r)
        }
    }

    /// Signing out says nothing on success — the Status row reads Not Signed In, which is the
    /// outcome. Only a failure is reported.
    func signOut() {
        Task {
            let r = await api.post("/api/connector/signout")
            notice[.account] = r["ok"] as? Bool == false ? Notice(ok: false, text: r.str("message")) : nil
            await poll()
        }
    }

    // MARK: - Record & Transcribe

    var storage: String { panel.str("storage") }
    var language: String { cur.str("language").isEmpty ? "en" : cur.str("language") }

    #if !RECORDER
    func setLanguage(_ code: String) {
        Task {
            say(.calls, await api.post("/api/setup/setting", ["field": "language", "value": code]))
            await poll()
        }
    }
    #endif

    func changeFolder() {
        let sandboxed = perms.bool("sandboxed")
        let start = storage.isEmpty ? nil : URL(fileURLWithPath: storage)
        host?.chooseFolder(startingIn: start, sandboxed: sandboxed) { [weak self] url in
            guard let self, let url else { return }
            Task {
                // In the sandbox the host has already kept the grant and restarted the daemon.
                if !sandboxed {
                    self.say(.calls, await self.api.post("/api/setup/setting",
                                                         ["field": "recordings", "value": url.path]))
                } else {
                    self.notice[.calls] = Notice(ok: true, text:
                        "Recordings from now on go to \(url.path). Anything already recorded stays where it is.")
                }
                await self.poll()
            }
        }
    }

    func showFolder() {
        guard !storage.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: storage))
    }

    // MARK: - Permissions

    var documents: String { perms.str("documents") }
    /// The daemon's words for Documents, in the four the permission rows use.
    var documentsState: String {
        switch documents {
        case "granted": return "allowed"
        case "denied": return "refused"
        case "asking": return "asking"
        default: return "not-asked"
        }
    }
    var loginState: String { cur.str("start_at_login_state") }
    var startAtLogin: Bool { loginState == "on" || loginState == "pending" || cur.bool("start_at_login") }

    func allowDocuments() {
        if perms.bool("sandboxed") {
            host?.grantDocuments { [weak self] _ in Task { await self?.poll() } }
        } else {
            Task {
                // A REFUSED REQUEST IS SAID, not swallowed: pressing Allow and seeing nothing is
                // the report this came from.
                let r = await api.post("/api/permissions", ["action": "documents"])
                if r["ok"] as? Bool == false { say(.permissions, r) }
                await poll()
                // BACK IN FRONT once macOS has its answer: its prompt hands the focus to the last
                // ordinary app when it closes, which left this window behind others.
                for _ in 0..<120 where documents == "asking" {
                    try? await Task.sleep(nanoseconds: 500_000_000)
                    await poll()
                }
                AppDelegate.comeBack()
            }
        }
    }

    func allowMic() {
        guard !Quarantine.answerHere else { return }
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] _ in
            Task { @MainActor in
                AppDelegate.comeBack()      // the prompt left the focus with another app
                await self?.poll()
            }
        }
    }

    func allowContacts() {
        host?.askContacts { [weak self] _ in Task { await self?.poll() } }
    }

    func openPrivacy(_ pane: String) {
        let anchors = ["privacy": "Privacy_FilesAndFolders", "privacy-mic": "Privacy_Microphone",
                       "privacy-contacts": "Privacy_Contacts"]
        guard let anchor = anchors[pane], let url = URL(string:
            "x-apple.systempreferences:com.apple.preference.security?\(anchor)") else { return }
        NSWorkspace.shared.open(url)
    }

    func setStartAtLogin(_ want: Bool) {
        Task {
            let r = await api.post("/api/setup/login-item", ["want": want])
            await poll()
            // Only an answer that needs the owner is worth saying: "on" is what the switch shows.
            // A SWITCH THAT DID NOT TAKE IS SAID TOO: it flipped back in silence (2026-10-03).
            if r.str("message").range(of: "approval", options: .caseInsensitive) != nil
                || r["ok"] as? Bool == false || startAtLogin != want {
                say(.permissions, r)
            } else {
                notice[.permissions] = nil
            }
        }
    }

    // MARK: - Advanced

    #if !RECORDER
    var modelOverride: String { cur.str("model_override") }
    var sttOverride: String { cur.str("transcription") }
    var thinkingPossible: Bool { cur.bool("thinking_possible") }
    var thinking: Bool { cur.bool("thinking") }

    func setModelOverride(_ name: String) async -> Bool {
        let r = await api.post("/api/model-override", ["name": name])
        say(.advanced, r)
        await poll()
        return r["ok"] as? Bool != false
    }

    func setSttOverride(_ name: String) async -> Bool {
        let r = await api.post("/api/stt-override", ["name": name])
        say(.advanced, r)
        await poll()
        return r["ok"] as? Bool != false
    }

    func setThinking(_ on: Bool) {
        Task {
            say(.advanced, await api.post("/api/setup/setting",
                                          ["field": "thinking", "value": on ? "yes" : "no"]))
            await poll()
        }
    }
    #endif

    func runSetup() { host?.runSetup() }

    #if !RECORDER
    func startFresh() {
        Task {
            let r = await api.post("/api/chat_new")
            notice[.advanced] = r["turns"] == nil
                ? Notice(ok: false, text: r.str("message").isEmpty ? "The assistant did not answer." : r.str("message"))
                : Notice(ok: true, text: "The assistant starts fresh.")
        }
    }
    #endif
    func exportLogs() { host?.exportLogs() }

    // MARK: - About

    var version: String { about.str("version") }
    #if !RECORDER
    var pick: JSON { cur.obj("pick") }
    var pickJob: JSON? { cur.obj("pick")["job"] as? JSON }
    var modelDescription: String { cur.str("model") }

    var sttName: String {
        let tier = (stt["tiers"] as? [JSON] ?? []).first { $0.str("model") == stt.str("model") }
        return tier?.str("name") ?? stt.str("model")
    }
    var sttGotMB: Double {
        let tier = (stt["tiers"] as? [JSON] ?? []).first { $0.str("model") == stt.str("model") }
        return tier?.num("got_mb") ?? 0
    }

    func downloadPick() {
        Task {
            let r = await api.post("/api/models", ["action": "download", "name": pick.str("model")])
            if r["ok"] as? Bool == false { say(.about, r) }
            await poll()
        }
    }

    func downloadStt() {
        Task {
            let r = await api.post("/api/setup/stt")
            if r["ok"] as? Bool == false { say(.about, r) }
            await poll()
        }
    }
    #endif

    var update: JSON { about.obj("update") }
    var storeUpdates: Bool { about.bool("store_updates") }

    func checkForUpdate() {
        checking = true
        Task {
            _ = await api.post("/api/about/check")
            about = await api.get("/api/about")
            checking = false
        }
    }
}
