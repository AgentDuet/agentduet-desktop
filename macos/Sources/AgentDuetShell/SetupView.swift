import SwiftUI

/// The native setup window (2026-09-29): sign in, permissions, then the quick setup that names
/// the owner and starts the two downloads. One window, a step at a time, with the buttons at the
/// bottom right as a macOS setup assistant places them.
struct SetupView: View {
    @ObservedObject var model: SetupModel

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch model.step {
                case .signIn: SignInStep(model: model)
                case .permissions: PermissionsStep(model: model)
                case .quick: QuickStep(model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            BottomBar(model: model)
        }
        .frame(width: 580, height: 620)
    }
}

/// The step's title, under the app's own icon.
private struct StepHeader: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(spacing: 8) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 64, height: 64)
            Text(title).font(.title.bold())
            Text(subtitle).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(.top, 28)
        .padding(.horizontal, 40)
    }
}

private struct NoticeLine: View {
    let notice: SettingsModel.Notice?
    var body: some View {
        if let n = notice {
            Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                .multilineTextAlignment(.center).textSelection(.enabled)
        }
    }
}

// MARK: - sign in

private struct SignInStep: View {
    @ObservedObject var model: SetupModel

    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                StepHeader(title: "Welcome to AgentDuet",
                           subtitle: "On-device AI for your calls. Please sign in to link your desktop AI hub.")
                SignInPanel(model: model.signIn)
                Button("Set Up Without Signing In") { model.skipSignIn() }.buttonStyle(.link)
            }
            .padding(.bottom, 20)
        }
    }
}

// MARK: - permissions

private struct PermissionsStep: View {
    @ObservedObject var model: SetupModel
    var body: some View {
        let s = model.settings
        VStack(spacing: 8) {
            StepHeader(title: "Permissions", subtitle: "What AgentDuet needs on this Mac.")
            Form {
                Section {
                    PermissionRow(title: "Documents folder (Required)",
                                  detail: "Your call recordings and transcripts are kept in Documents › AgentDuet.",
                                  state: s.documentsState, allow: s.allowDocuments,
                                  openSettings: { s.openPrivacy("privacy") })
                    if !Quarantine.answerHere {
                        PermissionRow(title: "Microphone (Optional)", detail: "To answer calls in this window.",
                                      state: s.mic, allow: s.allowMic,
                                      openSettings: { s.openPrivacy("privacy-mic") })
                    }
                    PermissionRow(title: "Contacts (Optional)",
                                  detail: "To show the names of people who call you.",
                                  state: s.contacts, allow: s.allowContacts,
                                  openSettings: { s.openPrivacy("privacy-contacts") })
                }
                // TICKED, AND ASKED: a phone answering service that is not running answers
                // nothing, and adding yourself to login items unasked is what adware does.
                Section {
                    Toggle(isOn: $model.atLogin) {
                        Text("Start AgentDuet when I log in (Optional)")
                        Text("So calls are recorded without opening the app.")
                    }
                }
            }
            .formStyle(.grouped)
        }
    }
}

// MARK: - quick setup

private struct QuickStep: View {
    @ObservedObject var model: SetupModel
    var body: some View {
        let s = model.settings
        VStack(spacing: 8) {
            StepHeader(title: "Quick Application Setup", subtitle: "The models AgentDuet runs on this Mac.")
            Form {
                Section {
                    TextField("Your name", text: $model.name)
                }
                // A BAR, NOT A NAME: the owner needs to know it is coming and when it is here,
                // not which model it is.
                Section {
                    LabeledContent("AI model") { aiBar(s) }
                    LabeledContent("Speech recognition") { speechBar(s) }
                    LabeledContent("Decision model") { deciderBar(s) }
                } footer: {
                    if downloading(s) {
                        Text("They download in the background. You can finish setup now.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
            NoticeLine(notice: model.notice).padding(.horizontal, 40).padding(.bottom, 8)
        }
        .onAppear { model.startDownloads() }
    }

    private func downloading(_ s: SettingsModel) -> Bool {
        (s.pickJob != nil && !s.pick.bool("downloaded")) || (s.stt.bool("running") && !s.stt.bool("cached"))
            || (s.decider.bool("running") && !s.decider.bool("ready"))
    }

    @ViewBuilder private func aiBar(_ s: SettingsModel) -> some View {
        if s.pick.bool("downloaded") { bar(1, "Ready") }
        else if let job = s.pickJob { progress(job.num("done_mb"), job.num("total_mb")) }
        else { progress(0, s.pick.num("dl_mb")) }
    }

    @ViewBuilder private func speechBar(_ s: SettingsModel) -> some View {
        if s.stt.bool("cached") { bar(1, "Ready") }
        else { progress(s.sttGotMB, s.stt.num("mb")) }
    }

    @ViewBuilder private func deciderBar(_ s: SettingsModel) -> some View {
        if s.decider.bool("ready") { bar(1, "Ready") }
        else { progress(s.decider.num("got_mb"), s.decider.num("mb")) }
    }

    private func progress(_ done: Double, _ total: Double) -> some View {
        let f = total > 0 ? min(done / total, 0.99) : 0
        return bar(f, "\(Int(done)) / \(Int(total)) MB · \(Int(f * 100))%")
    }

    private func bar(_ fraction: Double, _ label: String) -> some View {
        HStack {
            ProgressView(value: fraction).frame(width: 150)
            Text(label).monospacedDigit().foregroundStyle(.secondary).frame(minWidth: 130, alignment: .trailing)
        }
    }
}

// MARK: - the buttons

private struct BottomBar: View {
    @ObservedObject var model: SetupModel
    @StateObject private var confirmQuit = Local(false)

    var body: some View {
        HStack {
            // WHERE THEY CAME FROM decides the way out: a first run's only exit stops the daemon,
            // and says so; a walk through again only closes this window.
            if model.rerun {
                Button("Cancel") { model.onFinish?() }.keyboardShortcut(.cancelAction)
            } else {
                Button("Quit") { confirmQuit.value = true }
                if model.onAir { Text("This machine is on the air.").foregroundStyle(.secondary) }
            }
            Spacer()
            switch model.step {
            case .signIn:
                EmptyView()
            case .permissions:
                Button("Continue") { model.toQuick() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.documentsAllowed)
            case .quick:
                Button("Back") { model.step = .permissions }
                if model.finishing { ProgressView().controlSize(.small) }
                Button("Done") { model.finish() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.finishing)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .confirmationDialog(model.onAir ? "Stop AgentDuet?" : "Stop setup?", isPresented: $confirmQuit.value) {
            Button("Quit", role: .destructive) { model.quit() }
        } message: {
            Text((model.onAir ? "Calls stop arriving until you start it again."
                              : "Nothing is arriving yet.") + " What you saved stays saved.")
        }
    }
}
