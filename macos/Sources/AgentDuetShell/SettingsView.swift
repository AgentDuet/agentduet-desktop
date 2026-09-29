import SwiftUI

/// The native Settings window (2026-09-29): macOS's own sidebar and grouped forms, as System
/// Settings draws them — label on the left, control on the right, no Save buttons, and an
/// Edit… sheet wherever a change needs a moment of commitment.
struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(spacing: 0) {
            // ITS OWN TITLE BAR, with Done at the right (Stanley, 2026-09-29): a sheet has none,
            // and without one it looked unfinished. Esc closes it too.
            ZStack {
                Text("Settings").font(.headline)
                HStack {
                    Spacer()
                    Button("Done") { model.done?() }
                        .keyboardShortcut(.defaultAction)
                        .keyboardShortcut(.cancelAction)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(.bar)
            Divider()
            NavigationSplitView {
                List(SettingsModel.Section.allCases, selection: Binding(
                    get: { model.section }, set: { if let s = $0 { model.section = s } })) { s in
                    Label(s.title, systemImage: s.symbol).tag(s)
                }
                .navigationSplitViewColumnWidth(min: 180, ideal: 200, max: 260)
            } detail: {
                VStack(alignment: .leading, spacing: 0) {
                    Text(model.section.title).font(.title2.bold())
                        .padding(.horizontal, 20).padding(.top, 16)
                    Group {
                        switch model.section {
                        case .account: AccountPane(model: model)
                        case .calls: CallsPane(model: model)
                        case .permissions: PermissionsPane(model: model)
                        case .advanced: AdvancedPane(model: model)
                        case .about: AboutPane(model: model)
                        }
                    }
                    .formStyle(.grouped)
                }
                .frame(minWidth: 460)
            }
        }
    }
}

/// A view's own local state. NOT `@State`: in the current SDK that is a macro whose plugin ships
/// with Xcode, and this package is built with the Command Line Tools alone (see Package.swift),
/// where `@State` does not compile. `@StateObject` is a plain property wrapper and does.
final class Local<Value>: ObservableObject {
    @Published var value: Value
    init(_ value: Value) { self.value = value }
}

/// What the last action said, under the section it belongs to. Outcomes only.
private struct NoticeFooter: View {
    let notice: SettingsModel.Notice?
    var body: some View {
        if let n = notice {
            Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                .textSelection(.enabled)
        }
    }
}

// MARK: - Account

private struct AccountPane: View {
    @ObservedObject var model: SettingsModel
    @FocusState private var focus: String?
    @StateObject private var lastFocus = Local<String?>(nil)
    @StateObject private var editingKey = Local(false)
    @StateObject private var confirmSignOut = Local(false)

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $model.name, prompt: Text("Your name"))
                    .focused($focus, equals: "name")
                    .onSubmit { model.commit("name") }
                // LEARNED, NOT TYPED (Stanley, 2026-09-29): the number is mined from the first
                // call, so it is shown here and not edited — and until then there is no row.
                if !model.yourNumber.isEmpty {
                    LabeledContent("Your number") {
                        Text(model.yourNumber).textSelection(.enabled)
                    }
                }
            }
            Section {
                LabeledContent("Status") {
                    HStack(spacing: 6) {
                        Circle().fill(model.connected ? Color.green : Color.secondary)
                            .frame(width: 8, height: 8)
                        Text(model.connectionState)
                    }
                }
                if model.signedIn {
                    LabeledContent("Signed in as") {
                        HStack {
                            Text(model.oauth.str("email"))
                            Button("Sign Out…") { confirmSignOut.value = true }
                        }
                    }
                } else if model.byKey {
                    LabeledContent("API key") {
                        HStack {
                            Text(model.cur.str("key_hint")).monospaced()
                            Button("Change…") { editingKey.value = true }
                        }
                    }
                } else {
                    // ONE BUTTON, and the ways in behind it — the setup window's own panel.
                    LabeledContent("Account") {
                        Button("Sign In…") { editingKey.value = true }
                    }
                }
            } header: {
                Text("AgentDuet account")
            } footer: {
                NoticeFooter(notice: model.notice[.account])
            }
        }
        // LEAVING A FIELD SAVES IT, as Return does — there is no Save button to forget.
        .onChange(of: focus) { now in
            if let was = lastFocus.value, was != now { model.commit(was) }
            lastFocus.value = now
        }
        .onDisappear { if let was = lastFocus.value { model.commit(was) } }
        .sheet(isPresented: $editingKey.value) { SignInSheet(model: model) }
        .confirmationDialog("Sign out of AgentDuet?", isPresented: $confirmSignOut.value) {
            Button("Sign Out", role: .destructive) { model.signOut() }
        } message: {
            Text("Calls stop arriving until you sign in again or enter a key.")
        }
    }

}

/// Sign In…, or Change… for an install set up by key: the setup window's first step, as a sheet.
private struct SignInSheet: View {
    @ObservedObject var model: SettingsModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var signIn: SignInModel

    init(model: SettingsModel) {
        self.model = model
        _signIn = StateObject(wrappedValue: SignInModel(settings: model))
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(spacing: 18) {
                    VStack(spacing: 8) {
                        Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56)
                        Text("Sign in to AgentDuet").font(.title2.bold())
                    }
                    .padding(.top, 24)
                    SignInPanel(model: signIn)
                }
                .padding(.bottom, 20)
            }
            Divider()
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 20).padding(.vertical, 14)
        }
        .frame(width: 560, height: 520)
        .onAppear {
            signIn.prefill()
            signIn.onDone = { dismiss() }
            model.notice[.account] = nil
        }
    }
}

// MARK: - Record & Transcribe

private struct CallsPane: View {
    @ObservedObject var model: SettingsModel

    static let languages = [("en", "English"), ("vi", "Vietnamese"), ("zh", "Chinese"),
                            ("ms", "Malay"), ("th", "Thai")]

    var body: some View {
        Form {
            Section {
                Toggle("Save calls to this Mac", isOn: Binding(
                    get: { model.recordCalls }, set: { model.setRecordCalls($0) }))
                LabeledContent("Folder") {
                    HStack {
                        Text(abbreviated(model.storage)).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                            .help(model.storage)
                        Button("Show in Finder") { model.showFolder() }
                        Button("Change…") { model.changeFolder() }
                    }
                }
            }
            Section {
                Picker("Language of your calls", selection: Binding(
                    get: { model.language }, set: { model.setLanguage($0) })) {
                    ForEach(Self.languages, id: \.0) { Text($0.1).tag($0.0) }
                }
            } footer: {
                NoticeFooter(notice: model.notice[.calls])
            }
        }
    }

    private func abbreviated(_ path: String) -> String {
        let home = FolderAccess.realHome.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}

// MARK: - Permissions

private struct PermissionsPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section {
                PermissionRow(title: "Documents folder", detail: "", state: model.documentsState,
                              allow: model.allowDocuments, openSettings: { model.openPrivacy("privacy") })
                PermissionRow(title: "Microphone", detail: "", state: model.mic,
                              allow: model.allowMic, openSettings: { model.openPrivacy("privacy-mic") })
                PermissionRow(title: "Contacts", detail: contactsNote, state: model.contacts,
                              allow: model.allowContacts,
                              openSettings: { model.openPrivacy("privacy-contacts") })
            }
            Section {
                Toggle("Start when I log in", isOn: Binding(
                    get: { model.startAtLogin }, set: { model.setStartAtLogin($0) }))
                if model.loginState == "pending" {
                    Text("Waiting for your approval in System Settings.").foregroundStyle(.secondary)
                }
            } footer: {
                NoticeFooter(notice: model.notice[.permissions])
            }
        }
    }

    private var contactsNote: String {
        let n = Int(model.perms.num("contacts_named"))
        guard model.contacts == "allowed", n > 0 else { return "" }
        return "Names for \(n) \(n == 1 ? "person" : "people")"
    }

}

/// One permission: Allowed, waiting, a way to System Settings after a refusal, or Allow. Shared by
/// Settings and the setup window, so the two cannot describe the same permission differently.
struct PermissionRow: View {
    let title: String
    let detail: String
    let state: String
    let allow: () -> Void
    let openSettings: () -> Void

    var body: some View {
        LabeledContent {
            switch state {
            case "allowed":
                Label("Allowed", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green).labelStyle(.titleAndIcon)
            case "asking":
                Text("Waiting for your answer…").foregroundStyle(.secondary)
            case "refused":
                Button("Open System Settings…") { openSettings() }
            default:
                Button("Allow") { allow() }
            }
        } label: {
            Text(title)
            if !detail.isEmpty { Text(detail) }
        }
    }
}

// MARK: - Advanced

private struct AdvancedPane: View {
    @ObservedObject var model: SettingsModel
    @StateObject private var editing = Local<Override?>(nil)

    enum Override: String, Identifiable {
        case model, speech
        var id: String { rawValue }
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("Walk through setup again") {
                    Button("Run Setup…") { model.runSetup() }
                }
            }
            Section {
                LabeledContent("AI model") {
                    HStack {
                        Text(model.modelOverride.isEmpty ? "Automatic" : model.modelOverride)
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Button("Edit…") { editing.value = .model }
                    }
                }
                LabeledContent("Speech model") {
                    HStack {
                        Text(model.sttOverride.isEmpty ? "Automatic" : model.sttOverride)
                            .foregroundStyle(.secondary)
                        Button("Edit…") { editing.value = .speech }
                    }
                }
                if model.thinkingPossible {
                    Toggle(isOn: Binding(get: { model.thinking }, set: { model.setThinking($0) })) {
                        Text("Thinking mode")
                        if model.thinking {
                            Text("On — answers are much slower, and no better on simple questions")
                        }
                    }
                }
            } header: {
                Text("For developers")
            } footer: {
                NoticeFooter(notice: model.notice[.advanced])
            }
        }
        .sheet(item: $editing.value) { which in
            OverrideSheet(model: model, which: which)
        }
    }
}

private struct OverrideSheet: View {
    @ObservedObject var model: SettingsModel
    let which: AdvancedPane.Override
    @Environment(\.dismiss) private var dismiss
    @StateObject private var value = Local("")
    @StateObject private var busy = Local(false)

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField(which == .model ? "AI model" : "Speech model", text: $value.value,
                              prompt: Text(which == .model ? "owner/repo or owner/repo:Q4_K_M"
                                                           : "qwen3-asr-1.7b, large-v3-turbo…"))
                        .monospaced()
                } footer: {
                    NoticeFooter(notice: model.notice[.advanced])
                }
            }
            .formStyle(.grouped)
            HStack {
                if busy.value { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    busy.value = true
                    Task {
                        let name = value.value.trimmingCharacters(in: .whitespaces)
                        let ok = which == .model ? await model.setModelOverride(name)
                                                 : await model.setSttOverride(name)
                        busy.value = false
                        if ok { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy.value)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 480)
        .onAppear {
            value.value = which == .model ? model.modelOverride : model.sttOverride
            model.notice[.advanced] = nil
        }
    }
}

// MARK: - About

private struct AboutPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section {
                LabeledContent("Version") {
                    Text(model.version).textSelection(.enabled)
                }
                LabeledContent("AI model") { aiModel }
                LabeledContent("Speech model") { speechModel }
            } footer: {
                NoticeFooter(notice: model.notice[.about])
            }
            // AN APP STORE BUILD IS UPDATED BY THE STORE, so it has no row to check for one.
            if !model.storeUpdates {
                Section {
                    LabeledContent {
                        HStack {
                            if model.update.bool("newer"), !model.update.str("version").isEmpty,
                               let url = URL(string: model.update.str("url")) {
                                Link(model.update.str("version"), destination: url)
                            } else {
                                Text(model.update["checked"] == nil ? "Not checked yet" : "None found")
                                    .foregroundStyle(.secondary)
                            }
                            Button("Check Now") { model.checkForUpdate() }.disabled(model.checking)
                        }
                    } label: {
                        Text("Newer version")
                        if let when = lastLooked { Text(when) }
                    }
                }
            }
        }
    }

    @ViewBuilder private var aiModel: some View {
        let pick = model.pick
        if let job = model.pickJob {
            progress(done: job.num("done_mb"), total: job.num("total_mb"))
        } else if !pick.str("model").isEmpty && !pick.bool("downloaded") {
            HStack {
                Text(model.modelDescription).foregroundStyle(.secondary)
                Button("Download · \(gb(pick.num("dl_mb")))") { model.downloadPick() }
            }
        } else {
            Text(model.modelDescription).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var speechModel: some View {
        let s = model.stt
        if s.bool("running") {
            progress(done: model.sttGotMB, total: s.num("mb"))
        } else if !s.bool("cached") {
            HStack {
                Text("\(model.sttName), not downloaded").foregroundStyle(.secondary)
                Button("Download · \(gb(s.num("mb")))") { model.downloadStt() }
            }
        } else {
            Text(model.sttName).foregroundStyle(.secondary)
        }
    }

    private func progress(done: Double, total: Double) -> some View {
        HStack {
            ProgressView(value: total > 0 ? min(done / total, 1) : 0).frame(width: 160)
            Text("\(Int(done)) / \(Int(total)) MB").monospacedDigit().foregroundStyle(.secondary)
        }
    }

    private func gb(_ mb: Double) -> String { String(format: "%.1f GB", mb / 1024) }

    /// "Last looked 5 minutes ago", from whatever the daemon stored: epoch seconds or ISO 8601.
    private var lastLooked: String? {
        let raw = model.update["checked"]
        var date: Date?
        if let n = raw as? NSNumber { date = Date(timeIntervalSince1970: n.doubleValue) }
        if let s = raw as? String {
            date = ISO8601DateFormatter().date(from: s)
                ?? { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"; return f.date(from: s) }()
        }
        guard let date else { return nil }
        let rel = RelativeDateTimeFormatter().localizedString(for: date, relativeTo: Date())
        return "Last looked \(rel)" + (model.update["reachable"] as? Bool == false
                                        ? " — could not reach GitHub" : "")
    }
}
