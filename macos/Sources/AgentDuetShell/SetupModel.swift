import AppKit
import Foundation

/// The native setup window's steps and actions (2026-09-29) — the HTML wizard's behaviour, drawn
/// with macOS's own controls.
///
/// The daemon's state, the permissions and their actions come from a `SettingsModel`, so setup
/// and Settings read one source and ask for a permission the same way. What is setup's own is the
/// order of the steps and what finishing means.
@MainActor final class SetupModel: ObservableObject {

    enum Step { case signIn, permissions, quick }

    @Published var step: Step = .signIn
    @Published var notice: SettingsModel.Notice?
    @Published var showManual = false
    @Published var uuid = ""
    @Published var key = ""
    @Published var checking = false
    @Published var name = ""
    @Published var atLogin = true
    @Published var onAir = false
    @Published var finishing = false

    let settings: SettingsModel
    /// A walk through again (from Settings, or on an instance already set up): Finish only goes
    /// back, and there is Cancel rather than Quit.
    private(set) var rerun: Bool
    var onFinish: (() -> Void)?
    var onQuit: (() -> Void)?

    /// The mask the daemon offered for a key it read from `~/.agentduet`. An UNTOUCHED mask is
    /// sent as blank, which makes the daemon read the stored key; anything typed wins.
    private var keyOffered = ""
    private var nameWas = ""
    private var startedPick = false
    private var startedSpeech = false
    private var signingIn = false

    init(api: DaemonAPI, rerun: Bool) {
        settings = SettingsModel(api: api)
        self.rerun = rerun
    }

    private var api: DaemonAPI { settings.api }

    func start() async {
        settings.start()
        await settings.load()
        let cur = settings.cur
        rerun = rerun || cur.bool("setup_done")
        nameWas = cur.str("name")
        name = cur.str("name").isEmpty ? cur.str("os_name") : cur.str("name")
        // Prefilled from what an operator left in their home directory, so a reset instance is
        // one click. The key itself never reaches this window — only a mask of it.
        uuid = cur.str("offer_uuid")
        keyOffered = cur.str("offer_key")
        key = keyOffered
        showManual = !uuid.isEmpty || !keyOffered.isEmpty
        // SIGNED IN ALREADY: the first step has nothing left to ask.
        if settings.signedIn { step = .permissions }
        let state = await api.get("/api/state")
        onAir = ["live", "connecting", "retrying"].contains(state.obj("channel").str("channel"))
    }

    func stop() { settings.stop() }

    // MARK: - sign in

    var canSignIn: Bool { settings.cur.bool("oauth_available") }

    func signInWithGoogle() {
        guard !signingIn else { return }
        Task {
            let r = await api.post("/api/connector/signin/open", query: ["provider": "google"])
            guard r.bool("ok") else {
                let url = r.str("url")
                notice = .init(ok: false, text: url.isEmpty
                    ? (r.str("message").isEmpty ? "Could not open your browser." : r.str("message"))
                    : "Open this in your browser to finish signing in: \(url)")
                return
            }
            notice = .init(ok: true, text: "Waiting for your browser…")
            // THE BROWSER FINISHES IT, and the daemon's own callback is what completes it — so
            // this window finds out by asking, for up to five minutes.
            signingIn = true
            defer { signingIn = false }
            for _ in 0..<150 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await settings.poll()
                if settings.signedIn {
                    notice = nil
                    step = .permissions
                    return
                }
            }
            notice = .init(ok: false, text: "Sign-in did not finish. Try again.")
        }
    }

    /// Only Google works upstream; Apple and Microsoft stay visible and say so when pressed.
    func notYet(_ who: String) {
        notice = .init(ok: false, text: canSignIn
            ? "\(who) sign-in is not available yet — only Google is."
            : "Signing in with \(who) is not connected yet.")
    }

    func skipSignIn() { notice = nil; step = .permissions }

    func checkConnector() {
        let uuid = self.uuid.trimmingCharacters(in: .whitespaces)
        let typed = key.trimmingCharacters(in: .whitespaces)
        let key = (!keyOffered.isEmpty && typed == keyOffered) ? "" : typed
        guard !uuid.isEmpty, !key.isEmpty || !keyOffered.isEmpty else {
            notice = .init(ok: false, text: "Both the connector uuid and the API key are needed.")
            return
        }
        checking = true
        notice = .init(ok: true, text: "Checking with the platform…")
        Task {
            // The same route Settings uses: verified by opening a real session before it is kept.
            let r = await api.post("/api/setup/connector", ["uuid": uuid, "key": key])
            checking = false
            notice = .init(ok: r.bool("ok"), text: r.str("message"))
            // Only on success. Moving on from a failed credential is how an install ends up
            // looking configured and silent.
            if r.bool("ok") {
                keyOffered = ""
                self.key = ""
                try? await Task.sleep(nanoseconds: 900_000_000)
                notice = nil
                step = .permissions
            }
        }
    }

    // MARK: - permissions

    var documentsAllowed: Bool { settings.documentsState == "allowed" }

    func toQuick() {
        step = .quick
        startDownloads()
    }

    // MARK: - quick setup

    /// BOTH DOWNLOADS START WHEN THIS STEP IS ON SCREEN, not at Finish: 7 GB is minutes, and they
    /// run in the daemon, so leaving setup does not stop them. A refusal is retried at Finish.
    func startDownloads() {
        let pick = settings.pick
        if !startedPick, !pick.str("model").isEmpty, !pick.bool("downloaded"), settings.pickJob == nil {
            startedPick = true
            Task {
                let r = await api.post("/api/models", ["action": "download", "name": pick.str("model")])
                if r["ok"] as? Bool == false { startedPick = false }
                await settings.poll()
            }
        }
        let stt = settings.stt
        if !startedSpeech, !stt.str("model").isEmpty, !stt.bool("cached"), !stt.bool("running") {
            startedSpeech = true
            Task {
                let r = await api.post("/api/setup/stt")
                if r["ok"] as? Bool == false { startedSpeech = false }
                await settings.poll()
            }
        }
    }

    func finish() {
        finishing = true
        Task {
            defer { finishing = false }
            let typed = name.trimmingCharacters(in: .whitespaces)
            if !typed.isEmpty && typed != nameWas {
                _ = await api.post("/api/setup/setting", ["field": "name", "value": typed])
            }
            // A RE-RUN JUST GOES BACK. Handover is the installer's last act; on an instance
            // already set up it would be a daemon restart for no reason.
            if rerun { onFinish?(); return }
            startDownloads()
            // BEFORE HANDOVER, which may stand this daemon down for the installed copy.
            _ = await api.post("/api/setup/login-item", ["want": atLogin])
            let r = await api.post("/api/handover")
            if r.bool("ok") { onFinish?() } else { notice = .init(ok: false, text: r.str("message")) }
        }
    }

    /// Quit, from a first run: stop the daemon. What was saved stays saved.
    func quit() {
        Task {
            _ = await api.post("/api/quit")
            onQuit?()
        }
    }
}
