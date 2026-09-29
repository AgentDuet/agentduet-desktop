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
    @Published var name = ""
    @Published var atLogin = true
    @Published var onAir = false
    @Published var finishing = false

    let settings: SettingsModel
    /// The first step — shared with Settings' Sign In… sheet.
    let signIn: SignInModel
    /// A walk through again (from Settings, or on an instance already set up): Finish only goes
    /// back, and there is Cancel rather than Quit.
    private(set) var rerun: Bool
    var onFinish: (() -> Void)?
    var onQuit: (() -> Void)?

    private var nameWas = ""
    private var startedPick = false
    private var startedSpeech = false

    init(api: DaemonAPI, rerun: Bool) {
        settings = SettingsModel(api: api)
        signIn = SignInModel(settings: settings)
        self.rerun = rerun
        signIn.onDone = { [weak self] in self?.step = .permissions }
    }

    private var api: DaemonAPI { settings.api }

    func start() async {
        settings.start()
        await settings.load()
        let cur = settings.cur
        rerun = rerun || cur.bool("setup_done")
        nameWas = cur.str("name")
        name = cur.str("name").isEmpty ? cur.str("os_name") : cur.str("name")
        signIn.prefill()
        // SIGNED IN ALREADY: the first step has nothing left to ask.
        if settings.signedIn { step = .permissions }
        let state = await api.get("/api/state")
        onAir = ["live", "connecting", "retrying"].contains(state.obj("channel").str("channel"))
    }

    func stop() { settings.stop() }

    // MARK: - sign in

    func skipSignIn() { signIn.notice = nil; step = .permissions }

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
