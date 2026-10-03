import AppKit
import Combine
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
    #if !RECORDER
    private var startedPick = false
    private var startedSpeech = false
    private var startedDecider = false
    #endif

    /// The settings model's changes, passed on as this one's.
    private var relay: AnyCancellable?

    init(api: DaemonAPI, rerun: Bool) {
        settings = SettingsModel(api: api)
        signIn = SignInModel(settings: settings)
        self.rerun = rerun
        signIn.onDone = { [weak self] in self?.step = .permissions }
        // THE STEPS READ `settings`, BUT THE WINDOW WATCHES THIS MODEL. Without passing its changes
        // on, a permission granted in the background was never drawn: Allow stayed "Allow" while
        // the daemon said granted (2026-10-03, and the report from Samip before it).
        relay = settings.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
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

    #if !RECORDER
    func toQuick() {
        step = .quick
        startDownloads()
    }
    #endif

    // MARK: - quick setup

    #if !RECORDER
    /// ALL THREE DOWNLOADS START WHEN THIS STEP IS ON SCREEN, not at Finish: 7 GB is minutes, and they
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
        if !startedDecider, !settings.decider.bool("ready"), !settings.decider.bool("running") {
            startedDecider = true
            Task {
                _ = await api.post("/api/setup/decider")
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
    #endif

    func finish() {
        finishing = true
        Task {
            defer { finishing = false }
            // THE NAME IS QUICK SETUP'S, which the recorder does not show — so it saves none.
            #if !RECORDER
            let typed = name.trimmingCharacters(in: .whitespaces)
            if !typed.isEmpty && typed != nameWas {
                _ = await api.post("/api/setup/setting", ["field": "name", "value": typed])
            }
            #endif
            // A RE-RUN JUST GOES BACK. Handover is the installer's last act; on an instance
            // already set up it would be a daemon restart for no reason.
            if rerun { onFinish?(); return }
            #if !RECORDER
            startDownloads()
            #endif
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
