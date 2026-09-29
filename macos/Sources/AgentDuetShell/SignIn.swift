import SwiftUI

/// Signing in, wherever it is asked: the setup window's first step, and Settings' Sign In… sheet
/// (2026-09-29). One model and one panel, so the two cannot offer different ways in or describe
/// the same refusal differently.
@MainActor final class SignInModel: ObservableObject {
    @Published var notice: SettingsModel.Notice?
    @Published var showManual = false
    @Published var uuid = ""
    @Published var key = ""
    @Published var checking = false

    let settings: SettingsModel
    /// Signed in, or a connector and key accepted.
    var onDone: (() -> Void)?

    /// The mask the daemon offered for a key it read from `~/.agentduet`. An UNTOUCHED mask is
    /// sent as blank, which makes the daemon read the stored key; anything typed wins.
    private var keyOffered = ""
    private var signingIn = false

    init(settings: SettingsModel) { self.settings = settings }

    private var api: DaemonAPI { settings.api }
    var canSignIn: Bool { settings.cur.bool("oauth_available") }

    /// Prefilled from what an operator left in their home directory, so a reset instance is one
    /// click. The key itself never reaches this window — only a mask of it.
    func prefill() {
        let cur = settings.cur
        uuid = cur.str("offer_uuid").isEmpty ? cur.str("connector_uuid") : cur.str("offer_uuid")
        keyOffered = cur.str("offer_key")
        key = keyOffered
        showManual = !cur.str("offer_uuid").isEmpty || !keyOffered.isEmpty
        notice = nil
    }

    func signInWithGoogle() {
        guard canSignIn else { notYet("Google"); return }
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
                    onDone?()
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
            // Verified by opening a real session before it is kept, so a wrong pair fails here
            // instead of looking fine and receiving nothing.
            let r = await api.post("/api/setup/connector", ["uuid": uuid, "key": key])
            checking = false
            notice = .init(ok: r.bool("ok"), text: r.str("message"))
            await settings.poll()
            // Only on success. Moving on from a failed credential is how an install ends up
            // looking configured and silent.
            if r.bool("ok") {
                keyOffered = ""
                self.key = ""
                try? await Task.sleep(nanoseconds: 900_000_000)
                notice = nil
                onDone?()
            }
        }
    }
}

/// The three ways in, and the fourth under them.
struct SignInPanel: View {
    @ObservedObject var model: SignInModel

    var body: some View {
        VStack(spacing: 18) {
            VStack(spacing: 10) {
                wide("Continue with Apple", symbol: "apple.logo") { model.notYet("Apple") }
                wide("Continue with Google") { model.signInWithGoogle() }
                wide("Continue with Microsoft") { model.notYet("Microsoft") }
            }
            .frame(width: 300)
            if let n = model.notice {
                Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                    .multilineTextAlignment(.center).textSelection(.enabled)
                    .padding(.horizontal, 40)
            }
            // THE FOURTH WAY IN: the pair a person issued, for an install where sign-in does not
            // reach. Revealed on demand, since most of the time it is not about credentials.
            Button(model.showManual ? "Hide the connector and API key" : "Use a connector and API key") {
                model.showManual.toggle()
            }
            .buttonStyle(.link)
            if model.showManual {
                Form {
                    TextField("Connector uuid", text: $model.uuid,
                              prompt: Text("00000000-0000-0000-0000-000000000000"))
                        .monospaced()
                    SecureField("API key", text: $model.key, prompt: Text("Required"))
                    HStack {
                        Spacer()
                        if model.checking { ProgressView().controlSize(.small) }
                        Button("Check and Continue") { model.checkConnector() }
                            .disabled(model.checking)
                    }
                }
                .formStyle(.grouped)
                .frame(width: 480)
                .scrollDisabled(true)
            }
        }
    }

    private func wide(_ title: String, symbol: String? = nil, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let symbol { Image(systemName: symbol) }
                Text(title)
            }
            .frame(maxWidth: .infinity)
        }
        .controlSize(.large)
    }
}
